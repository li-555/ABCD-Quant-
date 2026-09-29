#!/usr/bin/env python
"""Read-only health probe for the trading bot.

Reads ``bot/logs/heartbeat.json`` (written at the end of every cycle) and
reports whether the bot is alive.  Mirrors the Dockerfile HEALTHCHECK (45 min
window) so it can be reused by external monitors / cron.

Exit codes:
  0  heartbeat is fresh (< 45 min old)
  1  heartbeat missing, unreadable, or stale

Usage:
  python scripts/check_heartbeat.py [path-to-heartbeat.json]
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

DEFAULT_PATH = "bot/logs/heartbeat.json"
MAX_AGE_SECONDS = 45 * 60


def _iso_to_epoch(value: str) -> float:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_PATH
    if not os.path.exists(path):
        print(f"HEARTBEAT MISSING: {path}")
        return 1
    try:
        with open(path, "r", encoding="utf-8") as fh:
            hb = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"HEARTBEAT UNREADABLE: {exc}")
        return 1

    when = hb.get("time")
    try:
        age = time.time() - _iso_to_epoch(when)
    except Exception:
        age = float("inf")
    fresh = age < MAX_AGE_SECONDS
    summary = {
        "time": when,
        "age_seconds": int(age),
        "fresh": fresh,
        "equity": hb.get("equity"),
        "trades_today": hb.get("trades_today"),
        "last_rebalance_date": hb.get("last_rebalance_date"),
        "live": hb.get("live"),
    }
    print(json.dumps(summary, indent=2))
    return 0 if fresh else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
