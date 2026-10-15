"""Load test: simulates 30 players joining and playing a full battle.

Target: wedding-production-13d6.up.railway.app (override with --url).

Usage:
    python3 load_test.py                     # 30 players against Railway
    python3 load_test.py --players 10       # fewer players
    python3 load_test.py --url ws://localhost:8000/ws   # local run
    python3 load_test.py --no-start         # only fill the waiting room

What it does:
1. Opens N WebSocket connections with a small ramp-up (like guests joining).
2. Everyone joins room MAIN with a unique nickname and a random team.
3. The first player (host) presses "start" once everyone is in.
4. During the game every player sends score_increment at a human-like rate
   (~2 catches/sec) until game_over arrives.
5. Prints a report: connect errors, join latency, broadcast lag,
   messages received, and the final scores each client saw.

Requires: pip install websockets  (already in requirements.txt)
"""

import argparse
import asyncio
import json
import random
import statistics
import time

import websockets

DEFAULT_URL = "wss://wedding-production-13d6.up.railway.app/ws"


class PlayerStats:
    def __init__(self, name):
        self.name = name
        self.connected = False
        self.join_latency = None      # join_room -> room_joined, seconds
        self.messages = 0
        self.leaderboard_lags = []    # server ts vs local arrival, seconds
        self.got_game_start = False
        self.got_game_over = False
        self.final = None             # game_over payload
        self.error = None


async def run_player(idx, url, stats, start_event, ready_counter, args):
    name = f"Гость{idx + 1}"
    st = PlayerStats(name)
    stats.append(st)
    team = random.choice(["groom", "bride"])
    is_host = idx == 0
    try:
        async with websockets.connect(url, open_timeout=15, close_timeout=5) as ws:
            st.connected = True
            t_join = time.monotonic()
            await ws.send(json.dumps({
                "type": "join_room", "room_code": "MAIN",
                "nickname": name[:12], "team": team,
            }))

            score_task = None
            async for raw in ws:
                msg = json.loads(raw)
                st.messages += 1
                mtype = msg.get("type")

                if mtype == "room_joined":
                    st.join_latency = time.monotonic() - t_join
                    ready_counter["joined"] += 1
                elif mtype == "room_full":
                    st.error = "room_full"
                    return
                elif mtype == "lobby_update" and is_host and not args.no_start:
                    # Host waits until every player is in, then starts once
                    if ready_counter["joined"] >= args.players and not ready_counter["started"]:
                        ready_counter["started"] = True
                        await ws.send(json.dumps({"type": "start_game"}))
                elif mtype == "game_start":
                    st.got_game_start = True
                    start_event.set()
                    # Human-like scoring: a catch every ~0.5s

                    async def send_scores():
                        while True:
                            await asyncio.sleep(random.uniform(0.3, 0.8))
                            await ws.send(json.dumps({
                                "type": "score_increment",
                                "points": random.choice([10, 15, 20, 25]),
                            }))
                    score_task = asyncio.create_task(send_scores())
                elif mtype == "leaderboard_update":
                    ts = msg.get("timestamp")
                    if ts:
                        st.leaderboard_lags.append(max(0.0, time.time() - ts))
                elif mtype == "game_over":
                    st.got_game_over = True
                    st.final = {
                        "groom": msg.get("total_groom_score"),
                        "bride": msg.get("total_bride_score"),
                    }
                    break

            if score_task:
                score_task.cancel()
    except Exception as exc:  # noqa: BLE001 - report every failure kind
        st.error = f"{type(exc).__name__}: {exc}"


async def main():
    parser = argparse.ArgumentParser(description="Wedding game load test")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--players", type=int, default=30)
    parser.add_argument("--ramp", type=float, default=0.2,
                        help="delay between connections, sec (default 0.2)")
    parser.add_argument("--no-start", action="store_true",
                        help="fill the waiting room but do not start the game")
    args = parser.parse_args()

    print(f"Target: {args.url}")
    print(f"Players: {args.players}, ramp-up: {args.ramp}s between connects\n")

    stats: list[PlayerStats] = []
    start_event = asyncio.Event()
    ready_counter = {"joined": 0, "started": False}

    t0 = time.monotonic()
    tasks = []
    for i in range(args.players):
        tasks.append(asyncio.create_task(
            run_player(i, args.url, stats, start_event, ready_counter, args)))
        await asyncio.sleep(args.ramp)

    # Waiting room only: hold connections for 30s, then disconnect
    if args.no_start:
        await asyncio.sleep(30)
        for t in tasks:
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - t0

    # --- Report ---
    connected = [s for s in stats if s.connected]
    joined = [s for s in stats if s.join_latency is not None]
    started = [s for s in stats if s.got_game_start]
    finished = [s for s in stats if s.got_game_over]
    errors = [s for s in stats if s.error]

    print("=" * 52)
    print(f"Total time:            {elapsed:.1f}s")
    print(f"Connected:             {len(connected)}/{args.players}")
    print(f"Joined room:           {len(joined)}/{args.players}")
    print(f"Received game_start:   {len(started)}/{args.players}")
    print(f"Received game_over:    {len(finished)}/{args.players}")
    if joined:
        lat = [s.join_latency for s in joined]
        print(f"Join latency:          avg {statistics.mean(lat)*1000:.0f}ms, "
              f"max {max(lat)*1000:.0f}ms")
    all_lags = [l for s in stats for l in s.leaderboard_lags]
    if all_lags:
        print(f"Broadcast lag:         avg {statistics.mean(all_lags)*1000:.0f}ms, "
              f"p95 {statistics.quantiles(all_lags, n=20)[18]*1000:.0f}ms "
              f"(includes clock skew)")
    total_msgs = sum(s.messages for s in stats)
    print(f"Messages received:     {total_msgs} total")
    finals = {json.dumps(s.final) for s in finished}
    if finals:
        consistent = "YES" if len(
            finals) == 1 else f"NO ({len(finals)} variants!)"
        print(f"Same final scores:     {consistent} -> {finals}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for s in errors:
            print(f"  {s.name}: {s.error}")
    else:
        print("\nNo errors 🎉")
    print("=" * 52)


if __name__ == "__main__":
    asyncio.run(main())
