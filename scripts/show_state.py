#!/usr/bin/env python
"""Read-only summary of the bot's persistent state (``bot/state.json``).

Prints trading-relevant state so an operator can inspect the bot without
placing or affecting any order.  Pure read; never mutates state.

Usage:
  python scripts/show_state.py [path-to-state.json]
"""
from __future__ import annotations

import json
import os
import sys

DEFAULT_PATH = "bot/state.json"

TOP_LEVEL = [
    "last_rebalance_date",
    "breaker_until",
    "peak",
    "day_trade_date",
    "day_trade_count",
    "activity_done_date",
    "last_heartbeat",
]


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_PATH
    if not os.path.exists(path):
        print(f"STATE MISSING: {path}  (the bot has not persisted state yet)")
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"STATE UNREADABLE: {exc}")
        return 1

    print("== Bot state summary ==")
    for key in TOP_LEVEL:
        if key in st:
            print(f"  {key}: {st[key]}")

    coins = st.get("coins", {})
    print(f"  tracked coins: {len(coins)}")
    for coin, info in sorted(coins.items()):
        entry = info.get("entry_time") or "-"
        cool = info.get("cooldown_until") or "-"
        ext = info.get("ext")
        ext_s = "none" if ext is None else f"{float(ext):.4f}"
        print(f"    {coin:6s} entry={entry}  cooldown={cool}  trailing_ext={ext_s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
