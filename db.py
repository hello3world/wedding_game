"""Battle results storage.

Uses PostgreSQL when DATABASE_URL is set (Railway / docker-compose),
falls back to an in-memory list for local dev and CI tests.
"""

import json
import logging
import os
from typing import List, Optional

logger = logging.getLogger("ResultsStore")

try:
    import asyncpg
except ImportError:  # pragma: no cover - asyncpg is in requirements
    asyncpg = None


class ResultsStore:
    def __init__(self):
        self.pool = None
        self._memory: List[dict] = []

    async def init(self):
        dsn = os.getenv("DATABASE_URL")
        if not dsn or asyncpg is None:
            logger.info("DATABASE_URL not set — using in-memory results store")
            return
        try:
            self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS battle_results (
                        id SERIAL PRIMARY KEY,
                        finished_at DOUBLE PRECISION NOT NULL,
                        total_groom_score INTEGER NOT NULL,
                        total_bride_score INTEGER NOT NULL,
                        top_groom_players JSONB NOT NULL,
                        top_bride_players JSONB NOT NULL
                    )
                    """
                )
            logger.info("Connected to PostgreSQL results store")
        except Exception as e:
            logger.error(
                f"PostgreSQL unavailable ({e}) — falling back to memory")
            self.pool = None

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def save(self, result: dict):
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO battle_results
                        (finished_at, total_groom_score, total_bride_score,
                         top_groom_players, top_bride_players)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    result["finished_at"],
                    result["total_groom_score"],
                    result["total_bride_score"],
                    json.dumps(result["top_groom_players"],
                               ensure_ascii=False),
                    json.dumps(result["top_bride_players"],
                               ensure_ascii=False),
                )
        else:
            self._memory.append(result)

    async def latest(self) -> Optional[dict]:
        if self.pool:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM battle_results ORDER BY id DESC LIMIT 1"
                )
            if not row:
                return None
            return {
                "finished_at": row["finished_at"],
                "total_groom_score": row["total_groom_score"],
                "total_bride_score": row["total_bride_score"],
                "top_groom_players": json.loads(row["top_groom_players"]),
                "top_bride_players": json.loads(row["top_bride_players"]),
            }
        return self._memory[-1] if self._memory else None

    async def history(self, limit: int = 100) -> List[dict]:
        """All stored battles, newest first (for the browsable results tab)."""
        if self.pool:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM battle_results ORDER BY id DESC LIMIT $1",
                    limit,
                )
            return [
                {
                    "finished_at": row["finished_at"],
                    "total_groom_score": row["total_groom_score"],
                    "total_bride_score": row["total_bride_score"],
                    "top_groom_players": json.loads(row["top_groom_players"]),
                    "top_bride_players": json.loads(row["top_bride_players"]),
                }
                for row in rows
            ]
        return list(reversed(self._memory[-limit:]))

    async def reset(self):
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute("TRUNCATE battle_results")
        else:
            self._memory.clear()


results_store = ResultsStore()
