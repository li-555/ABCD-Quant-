"""Offline checks for universe/config loading and the v1 strategy contract.

These guard rails ensure (a) the bot can actually read ``universe.yaml`` at
startup (it uses a ``coins:`` mapping, which ``load_universe`` must unwrap) and
(b) the live config still matches the validated research v1 parameter set.
"""
from __future__ import annotations

import os

from bot.config.settings import load_config
from bot.scheduler import load_universe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "bot", "config", "config.yaml")
UNIVERSE = os.path.join(ROOT, "bot", "config", "universe.yaml")

EXPECTED = {
    "bar_min": 30, "rebalance_hour_utc": 0, "n_long": 6, "hold_rank_long": 10,
    "gate_thr": 0.2, "entry_thr": 0.2, "coin_cap": 0.70, "single_long": 0.35,
    "gross_mult": 1.5, "regime_floor": 0.5, "min_scale": 0.5, "dd_scale": 0.10,
    "dd_hard": 0.12, "dd_window_h": 336, "breaker_h": 12, "min_hold_h": 12,
    "cool_h": 3, "max_gross": 1.0, "allow_short": False,
}


def test_load_universe_reads_coins_mapping():
    uni = load_universe(UNIVERSE)
    assert uni, "universe must not be empty"
    for item in uni:
        assert {"coin", "binance_symbol", "roostoo_pair"} <= set(item)
        assert item["roostoo_pair"].endswith("/USD"), "crypto pair must be XXX/USD"


def test_config_matches_v1_contract():
    cfg = load_config(CONFIG)
    for key, expected in EXPECTED.items():
        assert getattr(cfg, key) == expected, f"{key} differs from v1 contract"
