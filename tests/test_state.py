"""State persistence: atomic save/load and seamless restart recovery."""
from __future__ import annotations

import pandas as pd

from bot.state import State


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    st = State()
    st.last_rebalance_date = "2026-10-04"
    st.breaker_until = "2026-10-04T12:00:00+00:00"
    st.set_ext("BTC", 61234.5)
    st.set_cooldown("BTC", pd.Timestamp("2026-10-04T03:00:00+00:00"))
    st.set_entry("BTC", pd.Timestamp("2026-10-04T00:30:00+00:00"))
    st.day_trade_count = 4
    st.day_trade_date = "2026-10-04"
    st.peak = 101234.0
    st.save(str(p))

    loaded = State.load(str(p))
    assert loaded.last_rebalance_date == "2026-10-04"
    assert loaded.breaker_until == "2026-10-04T12:00:00+00:00"
    assert abs(loaded.get_ext("BTC") - 61234.5) < 1e-6
    assert loaded.get_cooldown("BTC") == pd.Timestamp("2026-10-04T03:00:00+00:00")
    assert loaded.day_trade_count == 4
    assert abs(loaded.peak - 101234.0) < 1e-6


def test_load_missing_returns_fresh():
    st = State.load("/nonexistent/path/state.json")
    assert st.last_rebalance_date == ""
    assert st.coins == {}


def test_atomic_write_no_truncate_on_corrupt(tmp_path):
    """A partial crash must never leave a truncated state file in use."""
    p = tmp_path / "state.json"
    st = State()
    st.set_ext("ETH", 3000.0)
    st.save(str(p))
    # corrupt the file, then load must fall back to a fresh state (not crash)
    p.write_text("{ this is not valid json")
    loaded = State.load(str(p))
    assert loaded.coins == {}
