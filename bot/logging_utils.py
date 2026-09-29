"""Structured, audit-friendly logging for Screen 1 compliance review.

The competition's first screen checks that the trade log is consistent with the
declared strategy, so the bot writes four durable artefacts under ``bot/logs``:

  * ``decisions.jsonl`` - one record per rebalance with every input and output
    (per-coin S / ss / sd, selection reason, regime/vol/DD coefficients, target
    and current weights, and the trade/no-trade decision per coin).
  * ``orders.jsonl``    - one record per placed order (request, response, fill
    price/qty, commission).  No secrets, no signatures.
  * ``api.jsonl``       - one record per API request (endpoint, status, latency,
    success).  No keys/signatures.
  * ``equity.csv``     - one row per cycle (timestamp, equity, cash, positions).

All files rotate by size (JSONL) and never log secrets.  Writes are append-only
and cheap; a logging failure must never break the trading loop.
"""
from __future__ import annotations

import csv
import json
import os
from typing import Any, Dict, List, Optional

import pandas as pd


class RotatingJsonl:
    """Append-only JSON-lines writer that rotates to ``.1`` past ``max_bytes``."""

    def __init__(self, path: str, max_bytes: int = 5_000_000) -> None:
        self.path = path
        self.max_bytes = max_bytes
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, record: Dict[str, Any]) -> None:
        try:
            line = json.dumps(record, default=str)
            if (os.path.exists(self.path) and
                    os.path.getsize(self.path) > self.max_bytes):
                backup = self.path + ".1"
                try:
                    if os.path.exists(backup):
                        os.remove(backup)
                    os.replace(self.path, backup)
                except OSError:
                    pass
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            # Never let logging break the loop.
            pass


class EquityLog:
    """One CSV row per cycle: timestamp, equity, cash, per-coin position value."""

    def __init__(self, path: str, coins: List[str]) -> None:
        self.path = path
        self.coins = coins
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._header = ["timestamp", "equity", "cash"] + \
            [f"{c}_val" for c in coins]

    def write(self, ts: pd.Timestamp, equity: float, cash: float,
              pos_values: Dict[str, float]) -> None:
        try:
            exists = os.path.exists(self.path)
            row = [ts.isoformat(), f"{equity:.6f}", f"{cash:.6f}"] + \
                [f"{pos_values.get(c, 0.0):.6f}" for c in self.coins]
            with open(self.path, "a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if not exists:
                    w.writerow(self._header)
                w.writerow(row)
        except Exception:
            pass

    def recent_equity(self, days: int = 14) -> List[Dict[str, Any]]:
        """Return rows within the last ``days`` days as dicts (empty if absent)."""
        if not os.path.exists(self.path):
            return []
        try:
            out: List[Dict[str, Any]] = []
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
            with open(self.path, "r", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        t = pd.Timestamp(row["timestamp"])
                    except Exception:
                        continue
                    if t >= cutoff:
                        out.append(row)
            return out
        except Exception:
            return []


def write_heartbeat(path: str, data: Dict[str, Any]) -> None:
    """Atomically write the heartbeat file (used by the Docker HEALTHCHECK)."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, default=str)
            fh.flush()
        os.replace(tmp, path)
    except Exception:
        pass


class Loggers:
    """Bundle of the four audit artefacts plus the API hook for the client."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.decisions = RotatingJsonl(f"{cfg.log_dir}/decisions.jsonl",
                                       cfg.log_max_bytes)
        self.orders = RotatingJsonl(f"{cfg.log_dir}/orders.jsonl",
                                    cfg.log_max_bytes)
        self.api = RotatingJsonl(f"{cfg.log_dir}/api.jsonl",
                                 cfg.log_max_bytes)
        self.equity = EquityLog(cfg.equity_csv, [])

    def set_coins(self, coins: List[str]) -> None:
        self.equity = EquityLog(self.cfg.equity_csv, coins)

    def log_decision(self, record: Dict[str, Any]) -> None:
        self.decisions.write(record)

    def log_order(self, record: Dict[str, Any]) -> None:
        self.orders.write(record)

    def api_hook(self, endpoint: str, status: int, latency: float,
                 success: bool) -> None:
        self.api.write({"endpoint": endpoint, "status": status,
                        "latency_s": round(latency, 4), "success": success})
