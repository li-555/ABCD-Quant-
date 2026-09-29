#!/usr/bin/env python
"""Read-only configuration validator.

Loads ``bot/config/config.yaml`` and ``bot/config/universe.yaml`` and checks
that the live configuration matches the *validated* research v1 parameter
contract (variant A of ``research/backtest.py``: slow layer only, 30m bars,
daily rebalance at UTC 00:00, crypto-only, long-only).  This is a guard rail so
a stray edit to config.yaml cannot silently change the strategy the backtest
proved.

Exit codes:
  0  all critical invariants hold
  1  at least one FAIL (strategy-contract violation)

Usage:
  python scripts/validate_config.py
"""
from __future__ import annotations

import os
import sys

# Allow running from the repo root without an install.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config.settings import load_config  # noqa: E402
from bot.scheduler import load_universe  # noqa: E402

# Research v1 "variant A" contract (research/backtest.py defaults).
EXPECTED = {
    "bar_min": 30,
    "rebalance_hour_utc": 0,
    "n_long": 6,
    "hold_rank_long": 10,
    "gate_thr": 0.2,
    "entry_thr": 0.2,
    "coin_cap": 0.70,
    "single_long": 0.35,
    "gross_mult": 1.5,
    "regime_floor": 0.5,
    "min_scale": 0.5,
    "dd_scale": 0.10,
    "dd_hard": 0.12,
    "dd_window_h": 336,
    "breaker_h": 12,
    "min_hold_h": 12,
    "cool_h": 3,
    "max_gross": 1.0,
    "allow_short": False,
}


def main() -> int:
    cfg = load_config("bot/config/config.yaml")
    universe = load_universe("bot/config/universe.yaml")

    fails = 0
    print("== Config contract check (research v1 variant A) ==")
    for key, expected in EXPECTED.items():
        actual = getattr(cfg, key, None)
        ok = actual == expected
        if not ok:
            fails += 1
        print(f"  [{'OK ' if ok else 'FAIL'}] {key}: config={actual} expected={expected}")

    print("\n== Universe check ==")
    if not universe:
        print("  [FAIL] universe is empty")
        fails += 1
    else:
        print(f"  [OK ] universe size: {len(universe)}")
    for item in universe:
        pair = item["roostoo_pair"]
        if not pair.endswith("/USD"):
            print(f"  [WARN] non-USD pair {pair} (expected crypto XXX/USD)")

    status = "PASS" if fails == 0 else f"FAIL ({fails})"
    print(f"\nVALIDATION: {status}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
