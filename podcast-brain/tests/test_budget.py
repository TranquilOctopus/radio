from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import anthropic
import pytest

from pkm.budget import BudgetTracker


def _fake_usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_write: int = 0,
) -> MagicMock:
    usage = MagicMock(spec=anthropic.types.Usage)
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_read_input_tokens = cache_read
    usage.cache_creation_input_tokens = cache_write
    return usage


def test_record_and_read_back() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = BudgetTracker(Path(tmpdir) / "jobs.db")
        usage = _fake_usage(input_tokens=1000, output_tokens=200)
        tracker.record("claude-sonnet-4-6", usage, cost_usd=0.05)

        spend = tracker.mtd_spend_usd()
        assert spend == pytest.approx(0.05)
        tracker.close()


def test_multiple_records_accumulate() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = BudgetTracker(Path(tmpdir) / "jobs.db")
        tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=0.10)
        tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=0.20)

        spend = tracker.mtd_spend_usd()
        assert spend == pytest.approx(0.30)
        tracker.close()


def test_can_spend_with_no_cap_is_always_true() -> None:
    from pkm.config import BudgetConfig
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = BudgetTracker(Path(tmpdir) / "jobs.db", BudgetConfig(monthly_cap_usd=0))
        assert tracker.can_spend(projected_usd=9999.0) is True
        tracker.close()


def test_can_spend_blocks_when_over_cap() -> None:
    from pkm.config import BudgetConfig
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = BudgetTracker(Path(tmpdir) / "jobs.db", BudgetConfig(monthly_cap_usd=1.00))
        tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=0.95)
        assert tracker.can_spend() is True
        assert tracker.can_spend(projected_usd=0.10) is False
        tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=0.10)
        assert tracker.can_spend() is False
        tracker.close()


def test_warn_at_threshold_logs_once(caplog) -> None:
    from pkm.config import BudgetConfig
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = BudgetTracker(
            Path(tmpdir) / "jobs.db", BudgetConfig(monthly_cap_usd=1.00, warn_at_pct=80)
        )
        with caplog.at_level("WARNING", logger="pkm.budget"):
            tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=0.50)
            assert "crossed" not in caplog.text
            tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=0.35)
            assert "crossed 80%" in caplog.text
            caplog.clear()
            tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=0.05)
            assert "crossed" not in caplog.text
        tracker.close()


def test_remaining_and_breakdown() -> None:
    from pkm.config import BudgetConfig
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = BudgetTracker(Path(tmpdir) / "jobs.db", BudgetConfig(monthly_cap_usd=10.00))
        tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=2.50)
        tracker.record("claude-haiku-4-5", _fake_usage(), cost_usd=0.30)
        assert tracker.mtd_remaining_usd() == pytest.approx(7.20)
        breakdown = tracker.usage_breakdown()
        assert len(breakdown) == 2
        assert breakdown[0]["model"] == "claude-sonnet-4-6"
        assert breakdown[0]["cost_usd"] == pytest.approx(2.50)
        tracker.close()


def test_zero_spend_when_no_records() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = BudgetTracker(Path(tmpdir) / "jobs.db")
        assert tracker.mtd_spend_usd() == pytest.approx(0.0)
        tracker.close()


def test_context_manager() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        with BudgetTracker(Path(tmpdir) / "jobs.db") as tracker:
            tracker.record("claude-sonnet-4-6", _fake_usage(), cost_usd=0.01)
            assert tracker.mtd_spend_usd() == pytest.approx(0.01)
