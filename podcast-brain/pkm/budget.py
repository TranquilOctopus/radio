from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import anthropic

from pkm.config import BudgetConfig

log = logging.getLogger(__name__)


class BudgetTracker:
    def __init__(
        self,
        db_path: Path,
        config: BudgetConfig | None = None,
    ) -> None:
        self._db_path = db_path
        self._config = config or BudgetConfig()
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
        self._warned_this_month = False

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
        self._maybe_warn()

    def can_spend(self, projected_usd: float = 0.0) -> bool:
        cap = self._config.monthly_cap_usd
        if cap <= 0:
            return True
        return (self.mtd_spend_usd() + projected_usd) <= cap

    def mtd_spend_usd(self) -> float:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM claude_calls "
            "WHERE ts >= strftime('%Y-%m-01', 'now')"
        )
        return float(cur.fetchone()[0])

    def mtd_remaining_usd(self) -> float:
        cap = self._config.monthly_cap_usd
        if cap <= 0:
            return float("inf")
        return max(0.0, cap - self.mtd_spend_usd())

    def usage_breakdown(self) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT model,
                   COUNT(*) AS calls,
                   SUM(input_tokens) AS input_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cost_usd) AS cost_usd
            FROM claude_calls
            WHERE ts >= strftime('%Y-%m-01', 'now')
            GROUP BY model
            ORDER BY cost_usd DESC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _maybe_warn(self) -> None:
        cap = self._config.monthly_cap_usd
        if cap <= 0 or self._warned_this_month:
            return
        threshold = cap * (self._config.warn_at_pct / 100.0)
        if self.mtd_spend_usd() >= threshold:
            log.warning(
                "Claude spend has crossed %d%% of the $%.2f monthly cap (currently $%.2f)",
                self._config.warn_at_pct,
                cap,
                self.mtd_spend_usd(),
            )
            self._warned_this_month = True

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BudgetTracker":
        return self

    def __exit__(self, *_) -> None:
        self.close()
