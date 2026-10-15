#!/usr/bin/env python3
"""Load test: N concurrent WebSocket players in the single MAIN room.

Simulates a full battle:
  1. all clients connect + join (evenly split groom/bride),
  2. the host launches the round,
  3. every client spams score_increment at a heavy rate for the whole round,
  4. metrics are collected and a report is printed on game_over.

Usage: python loadtest.py [ws_url] [num_clients]
       python loadtest.py ws://localhost:8000/ws 30
"""
import asyncio
import json
import sys
import time

import websockets

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8000/ws"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
SEND_INTERVAL = 0.08   # ~12.5 score packets/sec per client
PING_INTERVAL = 2.0
# Safety ceiling: countdown (5) + round (60) + generous buffer
HARD_TIMEOUT = 110.0


class Client:
    def __init__(self, idx):
        self.idx = idx
        self.team = "groom" if idx % 2 == 0 else "bride"
        self.nick = f"LT{idx:02d}"
        self.ws = None
        self.is_host = False
        # metrics
        self.connect_ms = None
        self.join_ms = None
        self.joined = asyncio.Event()
        self.over = asyncio.Event()
        self.got_countdown = False
        self.got_game_start = False
        self.lb_updates = 0
        self.sent = 0
        self.errors = []
        self.rtts = []
        self._pending_pings = []
        self._active = False
        self.final = None

    async def run(self, stop: asyncio.Event):
        t0 = time.perf_counter()
        try:
            self.ws = await websockets.connect(URL, open_timeout=15)
            self.connect_ms = (time.perf_counter() - t0) * 1000
        except Exception as e:
            self.errors.append(f"connect: {e!r}")
            self.joined.set()
            self.over.set()
            return

        self._join_t = time.perf_counter()
        try:
            await self.ws.send(json.dumps({
                "type": "join_room", "room_code": "MAIN",
                "nickname": self.nick, "team": self.team,
            }))
        except Exception as e:
            self.errors.append(f"join-send: {e!r}")
            self.joined.set()
            self.over.set()
            return

        sender = asyncio.create_task(self._send_loop(stop))
        pinger = asyncio.create_task(self._ping_loop(stop))
        try:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except (websockets.ConnectionClosed, Exception) as e:
                    self.errors.append(f"recv: {e!r}")
                    break
                self._handle(raw)
                if self.over.is_set():
                    break
        finally:
            self._active = False
            for t in (sender, pinger):
                t.cancel()
            try:
                await self.ws.close()
            except Exception:
                pass

    def _handle(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        mtype = msg.get("type")
        if mtype == "room_joined":
            self.join_ms = (time.perf_counter() - self._join_t) * 1000
            self.is_host = msg.get("is_host") is True
            self.joined.set()
        elif mtype == "countdown":
            self.got_countdown = True
        elif mtype == "game_start":
            self.got_game_start = True
            self._active = True
        elif mtype == "leaderboard_update":
            self.lb_updates += 1
        elif mtype == "game_over":
            self._active = False
            self.final = (msg.get("total_groom_score"),
                          msg.get("total_bride_score"))
            self.over.set()
        elif mtype == "pong":
            if self._pending_pings:
                self.rtts.append(
                    (time.perf_counter() - self._pending_pings.pop(0)) * 1000)
        elif mtype == "room_full":
            self.errors.append("room_full")
            self.joined.set()
            self.over.set()

    async def _send_loop(self, stop):
        try:
            while not stop.is_set():
                if self._active:
                    try:
                        await self.ws.send(json.dumps({"type": "score_increment", "points": 5}))
                        self.sent += 1
                    except Exception as e:
                        self.errors.append(f"score-send: {e!r}")
                        return
                    await asyncio.sleep(SEND_INTERVAL)
                else:
                    await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    async def _ping_loop(self, stop):
        try:
            while not stop.is_set():
                await asyncio.sleep(PING_INTERVAL)
                try:
                    self._pending_pings.append(time.perf_counter())
                    await self.ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    return
        except asyncio.CancelledError:
            pass

    async def send_start(self):
        await self.ws.send(json.dumps({"type": "start_game"}))


def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[k]


async def main():
    print(f"Load test: {N} clients -> {URL}\n")
    stop = asyncio.Event()
    clients = [Client(i) for i in range(N)]

    wall0 = time.perf_counter()
    # Connect + join everyone; the server makes the first joiner the host,
    # which we then detect via the is_host flag (no assumption on ordering).
    tasks = [asyncio.create_task(c.run(stop)) for c in clients]
    try:
        await asyncio.wait_for(asyncio.gather(*[c.joined.wait() for c in clients]), 30)
    except asyncio.TimeoutError:
        print("WARN: not all clients joined within 30s")
    joined_n = sum(1 for c in clients if c.joined.is_set() and not c.errors)
    connect_ms = [c.connect_ms for c in clients if c.connect_ms is not None]
    join_ms = [c.join_ms for c in clients if c.join_ms is not None]
    print(f"Joined: {joined_n}/{N}  "
          f"connect ms p50/p95/max={pct(connect_ms, 50):.0f}/{pct(connect_ms, 95):.0f}/{max(connect_ms or [0]):.0f}  "
          f"join ms p50/p95/max={pct(join_ms, 50):.0f}/{pct(join_ms, 95):.0f}/{max(join_ms or [0]):.0f}")

    host = next((c for c in clients if c.is_host), None)
    if host is None:
        print("FAIL: no client became host (another client already holds the "
              "MAIN room). Run against a clean instance.")
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        return
    print(f"Host is {host.nick}")

    # Launch the round
    print("Host -> start_game; playing the full round (spamming scores)...")
    await host.send_start()

    all_tasks = tasks
    try:
        await asyncio.wait_for(
            asyncio.gather(*[c.over.wait() for c in clients]), HARD_TIMEOUT)
    except asyncio.TimeoutError:
        print("WARN: game_over not received by all within timeout")
    stop.set()
    await asyncio.gather(*all_tasks, return_exceptions=True)
    wall = time.perf_counter() - wall0

    # ---- Report ----
    countdown_n = sum(1 for c in clients if c.got_countdown)
    start_n = sum(1 for c in clients if c.got_game_start)
    over_n = sum(1 for c in clients if c.final is not None)
    total_sent = sum(c.sent for c in clients)
    total_lb = sum(c.lb_updates for c in clients)
    all_rtts = [r for c in clients for r in c.rtts]
    err_clients = [c for c in clients if c.errors]
    finals = {c.final for c in clients if c.final is not None}

    active_secs = max(wall - 5.0, 1.0)  # subtract ~countdown
    print("\n================ LOAD TEST REPORT ================")
    print(f"Wall clock:            {wall:.1f} s")
    print(f"Connected:             {len(connect_ms)}/{N}")
    print(f"Joined OK:             {joined_n}/{N}")
    print(f"Got countdown:         {countdown_n}/{N}")
    print(f"Got game_start:        {start_n}/{N}")
    print(f"Got game_over:         {over_n}/{N}")
    print(
        f"Score packets sent:    {total_sent}  (~{total_sent/active_secs:.0f}/s inbound to server)")
    print(f"Leaderboard broadcasts:{total_lb}  received across all clients")
    print(f"Ping RTT ms p50/p95/max: "
          f"{pct(all_rtts, 50):.1f}/{pct(all_rtts, 95):.1f}/{max(all_rtts or [0]):.1f}  ({len(all_rtts)} samples)")
    print(f"Final team totals seen: {finals or 'NONE'}")
    print(f"Clients with errors:   {len(err_clients)}")
    for c in err_clients[:8]:
        print(f"   {c.nick}: {c.errors[:3]}")
    ok = (joined_n == N and start_n == N and over_n == N and not err_clients)
    print("RESULT:", "PASS ✅" if ok else "ISSUES ⚠️")
    print("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
