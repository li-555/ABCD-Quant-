"""Activity-guard unit tests (compliance helper, CODING_AGENT_PROMPT 5.7)."""
from __future__ import annotations

import numpy as np

from bot.strategy.activity_guard import propose_activity_trades
from bot.config.settings import Config


def test_disabled_when_enabled_false():
    cfg = Config()
    cfg.activity_enabled = False
    assert propose_activity_trades(np.zeros(3), np.zeros(3), 0, cfg) == []


def test_disabled_when_enough_trades():
    cfg = Config()
    cfg.min_daily_trades = 2
    assert propose_activity_trades(np.zeros(3), np.array([0.1, 0, 0]), 5, cfg) == []


def test_proposes_btc_when_all_cash():
    cfg = Config()
    cfg.min_daily_trades = 2
    cfg.activity_notional_frac = 0.005
    btc = 0  # BTC is index 0 in the universe order
    out = propose_activity_trades(np.zeros(3), np.zeros(3), 0, cfg, btc_index=btc)
    assert len(out) == 1
    assert out[0]["side"] == "BUY"
    assert out[0]["reason"] == "activity_guard"
    # notional is the configured fraction of equity
    assert abs(out[0]["notional"] - 0.005) < 1e-12


def test_proposes_largest_gap_when_target_differs():
    cfg = Config()
    cfg.min_daily_trades = 2
    w = np.array([0.0, 0.0, 0.0])
    tgt = np.array([0.2, 0.01, 0.0])  # large gap on asset 0
    out = propose_activity_trades(w, tgt, 0, cfg, btc_index=0)
    assert out[0]["index"] == 0
    assert out[0]["side"] == "BUY"
