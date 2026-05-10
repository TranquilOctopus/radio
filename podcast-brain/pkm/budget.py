from __future__ import annotations

import sqlite3
from pathlib import Path

import anthropic


class BudgetTracker:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # claude_calls table is created by Queue.init_schema(); we only ensure
        # it exists here so BudgetTracker can work standalone in tests.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS claude_calls (
              id INTEGER PRIMARY KEY,
              ts TEXT NOT NULL DEFAULT (datetime('now')),
              model TEXT NOT NULL,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              cache_read_tokens INTEGER NOT NULL DEFAULT 0,
              cache_write_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              cost_usd REAL NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.commit()

    def record(self, model: str, usage: anthropic.types.Usage, cost_usd: float) -> None:
        self._conn.execute(
            """
            INSERT INTO claude_calls
              (model, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                model,
                usage.input_tokens,
                getattr(usage, "cache_read_input_tokens", 0) or 0,
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
                usage.output_tokens,
                cost_usd,
            ),
        )
        self._conn.commit()

    def can_spend(self, projected_usd: float = 0.0) -> bool:
        # Step 9 will enforce the monthly cap; always permit for now.
        return True

    def mtd_spend_usd(self) -> float:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM claude_calls "
            "WHERE ts >= strftime('%Y-%m-01', 'now')"
        )
        return float(cur.fetchone()[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BudgetTracker":
        return self

    def __exit__(self, *_) -> None:
        self.close()
