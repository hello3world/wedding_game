import asyncio
import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import results_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WeddingArchitect")

# Game metadata
GAME_TITLE = os.getenv("GAME_TITLE", "Свадебный Переполох Касьяновых")

# Tunable via env (tests use short values)
# NOTE: Change ADMIN_PASSWORD in .env file for production!
GAME_DURATION = float(os.getenv("GAME_DURATION", "60"))
COUNTDOWN_SECONDS = float(os.getenv("COUNTDOWN_SECONDS", "5"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise ValueError(
        "ADMIN_PASSWORD environment variable is required. "
        "Create .env file from .env.example and set a secure password."
    )
MAX_PLAYERS_PER_ROOM = int(os.getenv("MAX_PLAYERS_PER_ROOM", "30"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await results_store.init()
    yield
    await results_store.close()


app = FastAPI(lifespan=lifespan)


class ActiveRoom:
    """Room lifecycle: waiting -> countdown -> active -> (deleted after game_over)"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.state = "waiting"
        # guest_id -> score (with tie-breaker)
        self.scores: Dict[str, float] = {}
        self.nicknames: Dict[str, str] = {}  # guest_id -> original nickname
        self.teams: Dict[str, str] = {}  # guest_id -> 'groom' | 'bride'
        self.connections: Set[WebSocket] = set()
        # Connected players in join order (guest_id -> websocket); first = host
        self.guest_sockets: Dict[str, WebSocket] = {}
        self.host_id: Optional[str] = None
        self.is_active = False
        self.start_time = 0.0
        self.countdown_start = 0.0
        self.duration = GAME_DURATION

    def team_totals(self) -> Dict[str, int]:
        """Aggregate team scores. Disconnected players keep contributing
        their earned points, so the team total never breaks."""
        totals = {"groom": 0, "bride": 0}
        for g_id, score_val in self.scores.items():
            team = self.teams.get(g_id)
            if team in totals:
                totals[team] += math.floor(score_val)
        return totals

    def top_players(self, team: str, limit: int = 3):
        """Top-N players of a single team, sorted by score desc."""
        members = [
            (g_id, score_val)
            for g_id, score_val in self.scores.items()
            if self.teams.get(g_id) == team
        ]
        members.sort(key=lambda x: x[1], reverse=True)
        return [
            {"nickname": self.nicknames[g_id], "score": math.floor(score_val)}
            for g_id, score_val in members[:limit]
        ]

    def lobby_payload(self) -> dict:
        """Waiting-room roster: names only, teams are hidden on purpose."""
        return {
            "type": "lobby_update",
            "players": [self.nicknames[g] for g in self.guest_sockets],
            "host_id": self.host_id,
            "max_players": MAX_PLAYERS_PER_ROOM,
        }


# In-memory room cache (single uvicorn worker keeps state consistent)
ACTIVE_ROOMS: Dict[str, ActiveRoom] = {}


class WebSocketConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(
            f"New connection established. Active sockets: {len(self.active_connections)}"
        )

    def disconnect(
        self, websocket: WebSocket, room_code: str = None, guest_id: str = None
    ):
        self.active_connections.discard(websocket)
        if room_code in ACTIVE_ROOMS:
            room = ACTIVE_ROOMS[room_code]
            room.connections.discard(websocket)
            room.guest_sockets.pop(guest_id, None)
            if room.state == "waiting":
                # In the lobby: remove player entirely so the roster stays accurate
                room.scores.pop(guest_id, None)
                room.nicknames.pop(guest_id, None)
                room.teams.pop(guest_id, None)
            # During the game scores stay (soft removal) so team totals hold
            if guest_id == room.host_id:
                # Host left — pass the crown to the next joined player
                room.host_id = next(iter(room.guest_sockets), None)
            if not room.guest_sockets and room.state == "waiting":
                del ACTIVE_ROOMS[room_code]
        logger.info(
            f"Connection closed. Remaining active sockets: {len(self.active_connections)}"
        )

    async def broadcast_to_room(self, room_code: str, message: dict):
        if room_code not in ACTIVE_ROOMS:
            return
        payload = json.dumps(message)
        disconnected_sockets = set()
        for ws in ACTIVE_ROOMS[room_code].connections:
            try:
                await ws.send_text(payload)
            except Exception:
                disconnected_sockets.add(ws)
        for ws in disconnected_sockets:
            self.disconnect(ws, room_code)


manager = WebSocketConnectionManager()


async def start_after_countdown(room_code: str):
    """Server-driven countdown -> game start."""
    await asyncio.sleep(COUNTDOWN_SECONDS)
    room = ACTIVE_ROOMS.get(room_code)
    if not room or room.state != "countdown":
        return
    room.state = "active"
    room.is_active = True
    room.start_time = time.time()
    asyncio.create_task(tick_room_leaderboard(room_code))
    await manager.broadcast_to_room(
        room_code,
        {"type": "game_start", "duration": room.duration, "time_left": room.duration},
    )


async def tick_room_leaderboard(room_code: str):
    """Periodic leaderboard broadcast task to reduce network load"""
    room = ACTIVE_ROOMS.get(room_code)
    if not room:
        return
    while room.is_active:
        await asyncio.sleep(0.2)  # Broadcast at 5 Hz (every 200ms)
        elapsed = time.time() - room.start_time
        time_left = max(0.0, room.duration - elapsed)

        # Sort participants by descending score (overall, for compatibility)
        sorted_users = sorted(
            room.scores.items(), key=lambda x: x[1], reverse=True
        )
        rankings = [
            {"nickname": room.nicknames[g_id], "score": math.floor(score_val)}
            for g_id, score_val in sorted_users[:10]
        ]

        # Team battle aggregation
        totals = room.team_totals()

        await manager.broadcast_to_room(
            room_code,
            {
                "type": "leaderboard_update",
                "rankings": rankings,
                "time_left": math.ceil(time_left),
                "total_groom_score": totals["groom"],
                "total_bride_score": totals["bride"],
                "top_groom_players": room.top_players("groom"),
                "top_bride_players": room.top_players("bride"),
            },
        )

        if time_left <= 0:
            room.is_active = False
            room.state = "finished"
            # Nobody goes home upset: negative scores are clamped to zero
            # before the final totals and top lists are computed.
            for g_id in room.scores:
                room.scores[g_id] = max(0.0, room.scores[g_id])
            final_rankings = [
                {"nickname": room.nicknames[g_id],
                    "score": math.floor(score_val)}
                for g_id, score_val in sorted(
                    room.scores.items(), key=lambda x: x[1], reverse=True
                )[:10]
            ]
            final_totals = room.team_totals()
            final_payload = {
                "type": "game_over",
                "final_rankings": final_rankings,
                "total_groom_score": final_totals["groom"],
                "total_bride_score": final_totals["bride"],
                "top_groom_players": room.top_players("groom", 10),
                "top_bride_players": room.top_players("bride", 10),
            }
            # Persist battle result (PostgreSQL or in-memory fallback)
            try:
                await results_store.save(
                    {
                        **{k: v for k, v in final_payload.items() if k not in ("type", "final_rankings")},
                        "finished_at": time.time(),
                    }
                )
            except Exception as e:
                logger.error(f"Failed to persist battle result: {e}")
            await manager.broadcast_to_room(room_code, final_payload)
            # Reset room so the next join starts a fresh battle
            ACTIVE_ROOMS.pop(room_code, None)


@app.websocket("/ws")
async def websocket_handler(websocket: WebSocket):
    await manager.connect(websocket)
    current_room_code = None
    current_guest_id = None

    try:
        while True:
            raw_data = await websocket.receive_text()
            event = json.loads(raw_data)
            event_type = event.get("type")

            if event_type == "join_room":
                room_code = event.get("room_code", "MAIN").upper()
                nickname = event.get("nickname", "Гость").strip()[:15]
                # Team is mandatory: default to 'groom' if client sends garbage
                team = event.get("team", "groom")
                if team not in ("groom", "bride"):
                    team = "groom"

                if room_code not in ACTIVE_ROOMS:
                    ACTIVE_ROOMS[room_code] = ActiveRoom(session_id=room_code)
                room = ACTIVE_ROOMS[room_code]

                # Capacity guard (25 concurrent players)
                if len(room.guest_sockets) >= MAX_PLAYERS_PER_ROOM:
                    await websocket.send_text(
                        json.dumps(
                            {"type": "room_full", "max_players": MAX_PLAYERS_PER_ROOM})
                    )
                    continue

                current_room_code = room_code
                resume_id = event.get("resume_id")
                if (
                    resume_id
                    and resume_id in room.scores
                    and resume_id not in room.guest_sockets
                ):
                    # Page refresh mid-game: the disconnect was soft (scores,
                    # nickname and team were kept), so reattach the old
                    # identity and let the player continue with their score.
                    current_guest_id = resume_id
                    nickname = room.nicknames.get(resume_id, nickname)
                    team = room.teams.get(resume_id, team)
                else:
                    current_guest_id = (
                        f"{nickname}_{int(time.time() * 1000) % 100000}"
                    )
                    room.nicknames[current_guest_id] = nickname
                    room.teams[current_guest_id] = team
                    room.scores[current_guest_id] = 0.0

                room.connections.add(websocket)
                room.guest_sockets[current_guest_id] = websocket
                if room.host_id is None:
                    room.host_id = current_guest_id

                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "room_joined",
                            "guest_id": current_guest_id,
                            "nickname": nickname,
                            "team": team,
                            "is_host": current_guest_id == room.host_id,
                            "room_state": room.state,
                            # Restored score after a page refresh (0 for new players)
                            "score": math.floor(room.scores[current_guest_id]),
                        }
                    )
                )

                if room.state == "waiting":
                    await manager.broadcast_to_room(room_code, room.lobby_payload())
                elif room.state == "countdown":
                    remaining = max(
                        0.0, COUNTDOWN_SECONDS -
                        (time.time() - room.countdown_start)
                    )
                    await websocket.send_text(
                        json.dumps({"type": "countdown", "seconds": remaining})
                    )
                elif room.state == "active":
                    # Late joiner: start the game immediately with remaining time.
                    # Fixes the "dark screen while others play" bug.
                    time_left = max(
                        0.0, room.duration - (time.time() - room.start_time)
                    )
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "game_start",
                                "duration": room.duration,
                                "time_left": time_left,
                            }
                        )
                    )

            elif event_type == "start_game":
                # Only the host can launch the countdown
                room = ACTIVE_ROOMS.get(current_room_code)
                if (
                    room
                    and room.state == "waiting"
                    and current_guest_id == room.host_id
                ):
                    # Roster check: at least 2 players and both teams present,
                    # otherwise the team battle makes no sense. Skipped on
                    # localhost so the game can be solo-tested during dev.
                    host_header = websocket.headers.get(
                        "host", "").split(":")[0]
                    is_local_dev = host_header in ("localhost", "127.0.0.1")
                    connected_teams = {
                        room.teams.get(g) for g in room.guest_sockets
                    }
                    if not is_local_dev and (
                        len(room.guest_sockets) < 2
                        or not {"groom", "bride"} <= connected_teams
                    ):
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "start_denied",
                                    "reason": "Нужно минимум 2 игрока и хотя бы по одному из каждой команды!",
                                }
                            )
                        )
                        continue
                    room.state = "countdown"
                    room.countdown_start = time.time()
                    await manager.broadcast_to_room(
                        current_room_code,
                        {"type": "countdown", "seconds": COUNTDOWN_SECONDS},
                    )
                    asyncio.create_task(
                        start_after_countdown(current_room_code))

            elif event_type == "score_increment":
                # Room may already be deleted after game over — ignore late packets
                if current_room_code and current_guest_id and current_room_code in ACTIVE_ROOMS:
                    room = ACTIVE_ROOMS[current_room_code]
                    if room.is_active and current_guest_id in room.scores:
                        points = int(event.get("points", 0))
                        # Apply Tie-Breaking algorithm
                        elapsed_time = time.time() - room.start_time
                        tie_breaker = 1.0 - (elapsed_time / 10000.0)

                        current_score = math.floor(
                            room.scores[current_guest_id]
                        )
                        room.scores[current_guest_id] = (
                            current_score + points + tie_breaker
                        )

            elif event_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        manager.disconnect(websocket, current_room_code, current_guest_id)
        if current_room_code in ACTIVE_ROOMS:
            room = ACTIVE_ROOMS[current_room_code]
            if room.state == "waiting":
                await manager.broadcast_to_room(current_room_code, room.lobby_payload())
    except Exception as e:
        logger.error(f"Internal event handler error: {e}")
        manager.disconnect(websocket, current_room_code, current_guest_id)


# Serve static files
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.middleware("http")
async def no_stale_cache(request, call_next):
    """Force browsers to revalidate HTML/JS so clients never run a stale
    game.js against a fresh index.html (ETag still allows cheap 304s)."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/config")
async def get_config():
    """Client-side config: game title and other metadata."""
    return {"game_title": GAME_TITLE}


@app.get("/api/results")
async def get_last_results():
    """Last finished battle (for the lobby results tab)."""
    result = await results_store.latest()
    if not result:
        return {"available": False}
    return {"available": True, **result}


@app.get("/api/results/history")
async def get_results_history():
    """Every stored battle, newest first — browsable in the results tab."""
    battles = await results_store.history()
    return {"battles": battles}


@app.get("/api/assets")
async def get_optional_assets():
    """Which optional assets (custom heads, music) are uploaded.
    The client only requests files that actually exist — no 404 noise."""
    optional = {
        "groom_head": "img/groom-head.png",
        "bride_head": "img/bride-head.png",
        "game_theme": "audio/game-theme.mp3",
        "results_theme": "audio/results-theme.mp3",
        # Victory splash screens shown after the round (per winning team)
        "groom_victory": "img/groom-victory.png",
        "bride_victory": "img/bride-victory.png",
    }
    return {
        key: os.path.isfile(os.path.join(STATIC_DIR, rel))
        for key, rel in optional.items()
    }


class ResetRequest(BaseModel):
    password: str


@app.post("/api/results/reset")
async def reset_results(body: ResetRequest):
    """Wipe all stored battle results. Password-protected."""
    if body.password != ADMIN_PASSWORD:
        return JSONResponse(status_code=403, content={"ok": False, "error": "Неверный пароль"})
    await results_store.reset()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
