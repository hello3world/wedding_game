"""End-to-end tests for the Wedding Battle game server.

Run with short timings so the full game flow completes in ~1 second:
GAME_DURATION=1, COUNTDOWN_SECONDS=0.
"""

import os

# Set required env vars BEFORE importing main (which validates them)
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("GAME_TITLE", "Тестовая Игра")
os.environ.pop("DATABASE_URL", None)  # force in-memory results store

import main
from fastapi.testclient import TestClient
import pytest

# Override module constants directly: robust against import reordering
main.GAME_DURATION = 1.0
main.COUNTDOWN_SECONDS = 0.0


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c
    # Clean leftover rooms between tests
    main.ACTIVE_ROOMS.clear()


def join(ws, room, nickname, team):
    ws.send_json({"type": "join_room", "room_code": room,
                 "nickname": nickname, "team": team})


def recv_until(ws, msg_type, limit=100):
    """Read messages until the expected type shows up."""
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"Message of type {msg_type} was not received")


def test_index_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "game-title" in resp.text


def test_api_config_returns_game_title(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "game_title" in data
    assert data["game_title"] == "Тестовая Игра"


def test_results_empty_initially(client):
    client.post("/api/results/reset", json={"password": "test-password"})
    resp = client.get("/api/results")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_join_creates_waiting_lobby(client):
    with client.websocket_connect("/ws") as ws:
        join(ws, "T1", "Alice", "bride")
        joined = recv_until(ws, "room_joined")
        assert joined["nickname"] == "Alice"
        assert joined["team"] == "bride"
        assert joined["is_host"] is True  # first player is the host
        assert joined["room_state"] == "waiting"

        lobby = recv_until(ws, "lobby_update")
        assert lobby["players"] == ["Alice"]
        assert lobby["host_id"] == joined["guest_id"]


def test_second_player_is_not_host_and_roster_updates(client):
    with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        join(ws1, "T2", "Host", "groom")
        recv_until(ws1, "room_joined")

        join(ws2, "T2", "Guest", "bride")
        joined2 = recv_until(ws2, "room_joined")
        assert joined2["is_host"] is False

        # Both see the updated roster with two names (teams are not exposed)
        lobby = recv_until(ws1, "lobby_update")
        while lobby["players"] != ["Host", "Guest"]:
            lobby = recv_until(ws1, "lobby_update")
        assert "teams" not in lobby


def test_only_host_can_start(client):
    with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        join(ws1, "T3", "Host", "groom")
        recv_until(ws1, "room_joined")
        join(ws2, "T3", "Guest", "bride")
        recv_until(ws2, "room_joined")

        # Non-host tries to start: nothing happens, room stays waiting
        ws2.send_json({"type": "start_game"})
        ws2.send_json({"type": "ping"})
        recv_until(ws2, "pong")
        assert main.ACTIVE_ROOMS["T3"].state == "waiting"

        # Host starts: countdown broadcast reaches both players
        ws1.send_json({"type": "start_game"})
        assert recv_until(ws1, "countdown")["seconds"] == 0
        recv_until(ws2, "countdown")


def test_full_game_flow_with_team_scores(client):
    client.post("/api/results/reset", json={"password": "test-password"})
    with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        join(ws1, "T4", "GroomFan", "groom")
        recv_until(ws1, "room_joined")
        join(ws2, "T4", "BrideFan", "bride")
        recv_until(ws2, "room_joined")

        ws1.send_json({"type": "start_game"})
        recv_until(ws1, "game_start")
        recv_until(ws2, "game_start")

        ws1.send_json({"type": "score_increment", "points": 50})
        ws2.send_json({"type": "score_increment", "points": 30})

        over1 = recv_until(ws1, "game_over")
        assert over1["total_groom_score"] == 50
        assert over1["total_bride_score"] == 30
        assert over1["top_groom_players"][0]["nickname"] == "GroomFan"
        assert over1["top_bride_players"][0]["nickname"] == "BrideFan"

    # Battle result was persisted and is available in the results tab
    resp = client.get("/api/results")
    data = resp.json()
    assert data["available"] is True
    assert data["total_groom_score"] == 50
    assert data["total_bride_score"] == 30


def test_late_joiner_gets_game_start_not_dark_screen(client):
    """Regression: a player joining an active game must receive game_start
    with the remaining time instead of silently waiting (dark screen bug)."""
    with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        join(ws1, "T5", "First", "groom")
        recv_until(ws1, "room_joined")
        join(ws2, "T5", "Second", "bride")
        recv_until(ws2, "room_joined")
        ws1.send_json({"type": "start_game"})
        recv_until(ws1, "game_start")

        with client.websocket_connect("/ws") as ws3:
            join(ws3, "T5", "Late", "bride")
            joined = recv_until(ws3, "room_joined")
            assert joined["room_state"] == "active"
            started = recv_until(ws3, "game_start")
            assert 0 < started["time_left"] <= 1

        recv_until(ws1, "game_over")


def test_room_capacity_limit(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_PLAYERS_PER_ROOM", 2)
    with client.websocket_connect("/ws") as ws1, \
            client.websocket_connect("/ws") as ws2, \
            client.websocket_connect("/ws") as ws3:
        join(ws1, "T6", "P1", "groom")
        recv_until(ws1, "room_joined")
        join(ws2, "T6", "P2", "bride")
        recv_until(ws2, "room_joined")
        join(ws3, "T6", "P3", "groom")
        full = recv_until(ws3, "room_full")
        assert full["max_players"] == 2


def test_reset_requires_password(client):
    resp = client.post("/api/results/reset", json={"password": "wrong"})
    assert resp.status_code == 403

    resp = client.post("/api/results/reset", json={"password": "test-password"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert client.get("/api/results").json()["available"] is False


def test_results_history_keeps_all_battles(client):
    """Every finished battle is stored; history returns them newest first."""
    client.post("/api/results/reset", json={"password": "test-password"})

    for round_no, points in enumerate([10, 20], start=1):
        with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
            join(ws1, f"H{round_no}", "GroomFan", "groom")
            recv_until(ws1, "room_joined")
            join(ws2, f"H{round_no}", "BrideFan", "bride")
            recv_until(ws2, "room_joined")
            ws1.send_json({"type": "start_game"})
            recv_until(ws1, "game_start")
            ws1.send_json({"type": "score_increment", "points": points})
            recv_until(ws1, "game_over")

    data = client.get("/api/results/history").json()
    battles = data["battles"]
    assert len(battles) == 2
    # Newest first: the 20-point round was played last
    assert battles[0]["total_groom_score"] == 20
    assert battles[1]["total_groom_score"] == 10


def test_host_reassigned_when_host_leaves(client):
    with client.websocket_connect("/ws") as ws1:
        join(ws1, "T7", "OldHost", "groom")
        recv_until(ws1, "room_joined")
        with client.websocket_connect("/ws") as ws2:
            join(ws2, "T7", "NewHost", "bride")
            joined2 = recv_until(ws2, "room_joined")
            assert joined2["is_host"] is False
            # Old host disconnects -> ws2 becomes host
            ws1.close()
            lobby = recv_until(ws2, "lobby_update")
            while lobby["host_id"] != joined2["guest_id"]:
                lobby = recv_until(ws2, "lobby_update")
            assert lobby["players"] == ["NewHost"]


def test_start_requires_two_players_from_both_teams(client):
    """Start is denied for a solo host and for a one-team roster."""
    with client.websocket_connect("/ws") as ws1:
        join(ws1, "T8", "Solo", "groom")
        recv_until(ws1, "room_joined")
        # Alone in the room -> denied
        ws1.send_json({"type": "start_game"})
        denied = recv_until(ws1, "start_denied")
        assert "2 игрока" in denied["reason"]
        assert main.ACTIVE_ROOMS["T8"].state == "waiting"

        with client.websocket_connect("/ws") as ws2:
            # Two players but the same team -> still denied
            join(ws2, "T8", "Buddy", "groom")
            recv_until(ws2, "room_joined")
            ws1.send_json({"type": "start_game"})
            recv_until(ws1, "start_denied")
            assert main.ACTIVE_ROOMS["T8"].state == "waiting"

            with client.websocket_connect("/ws") as ws3:
                # A bride player joins -> both teams present, start allowed
                join(ws3, "T8", "Bride", "bride")
                recv_until(ws3, "room_joined")
                ws1.send_json({"type": "start_game"})
                recv_until(ws1, "countdown")
                recv_until(ws1, "game_over")


def test_negative_final_score_clamped_to_zero(client):
    """A player who ends up below zero contributes 0 to the final results."""
    client.post("/api/results/reset", json={"password": "test-password"})
    with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        join(ws1, "T9", "Unlucky", "groom")
        recv_until(ws1, "room_joined")
        join(ws2, "T9", "Lucky", "bride")
        recv_until(ws2, "room_joined")

        ws1.send_json({"type": "start_game"})
        recv_until(ws1, "game_start")

        ws1.send_json({"type": "score_increment", "points": -50})
        ws2.send_json({"type": "score_increment", "points": 30})

        over = recv_until(ws1, "game_over")
        # -50 is clamped to 0 everywhere: totals, top players, rankings
        assert over["total_groom_score"] == 0
        assert over["total_bride_score"] == 30
        assert over["top_groom_players"][0] == {
            "nickname": "Unlucky", "score": 0}
        assert all(r["score"] >= 0 for r in over["final_rankings"])


def test_localhost_skips_roster_check_for_solo_dev(client):
    """On localhost the min-players/teams check is skipped (solo testing)."""
    with client.websocket_connect("/ws", headers={"host": "localhost:8000"}) as ws:
        join(ws, "T11", "Dev", "groom")
        recv_until(ws, "room_joined")
        ws.send_json({"type": "start_game"})
        recv_until(ws, "countdown")
        recv_until(ws, "game_start")
        recv_until(ws, "game_over")


def test_refresh_mid_game_restores_identity_and_score(client):
    """A page refresh (reconnect with resume_id) keeps the player's score."""
    with client.websocket_connect("/ws") as ws1:
        join(ws1, "T10", "Host", "groom")
        recv_until(ws1, "room_joined")

        with client.websocket_connect("/ws") as ws2:
            join(ws2, "T10", "Refresher", "bride")
            joined = recv_until(ws2, "room_joined")
            old_guest_id = joined["guest_id"]

            ws1.send_json({"type": "start_game"})
            recv_until(ws2, "game_start")
            ws2.send_json({"type": "score_increment", "points": 40})
            ws2.send_json({"type": "ping"})
            recv_until(ws2, "pong")  # ensure the score packet is processed

        # Simulated refresh: old socket closed, new one resumes the identity
        with client.websocket_connect("/ws") as ws3:
            ws3.send_json({
                "type": "join_room", "room_code": "T10",
                "nickname": "Refresher", "team": "bride",
                "resume_id": old_guest_id,
            })
            rejoined = recv_until(ws3, "room_joined")
            assert rejoined["guest_id"] == old_guest_id
            assert rejoined["score"] == 40
            assert rejoined["room_state"] == "active"
            # And the game resumes with the remaining time
            started = recv_until(ws3, "game_start")
            assert started["time_left"] > 0
            recv_until(ws3, "game_over")
