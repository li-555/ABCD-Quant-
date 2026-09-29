"""Persistent bot state (atomic JSON writes).

Everything the bot needs to resume seamlessly after a restart lives here:

  * ``last_rebalance_date`` - the UTC date the daily rebalance last ran.
  * ``breaker_until``       - ISO time the circuit breaker stays active until.
  * ``coins``               - per coin: ``entry_time`` (ISO), ``ext`` (trailing
    extreme price, or ``None``), ``cooldown_until`` (ISO, or ``""``).
  * ``peak``                - trailing 14-day equity peak (USD).
  * ``day_trade_date`` / ``day_trade_count`` - daily fill counter for the
    activity guard.
  * ``activity_done_date``  - date the activity guard last fired.

All times are ISO 8601 with UTC offset.  Writes are atomic: we serialise to a
temporary file and ``os.replace`` it over the target so a crash mid-write can
never leave a truncated state file.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Optional

import pandas as pd


@dataclass
class CoinState:
    entry_time: str = ""
    ext: Optional[float] = None
    cooldown_until: str = ""


@dataclass
class State:
    version: int = 1
    last_rebalance_date: str = ""
    breaker_until: str = ""
    peak: float = 0.0
    day_trade_date: str = ""
    day_trade_count: int = 0
    activity_done_date: str = ""
    last_heartbeat: str = ""
    coins: Dict[str, CoinState] = field(default_factory=dict)

    # -- per-coin helpers ----------------------------------------------------
    def coin(self, symbol: str) -> CoinState:
        if symbol not in self.coins:
            self.coins[symbol] = CoinState()
        return self.coins[symbol]

    def set_entry(self, symbol: str, ts: pd.Timestamp) -> None:
        self.coin(symbol).entry_time = ts.isoformat()

    def set_ext(self, symbol: str, value: Optional[float]) -> None:
        self.coin(symbol).ext = None if value is None or not np_isfinite(value) else float(value)

    def get_ext(self, symbol: str) -> float:
        v = self.coin(symbol).ext
        return float(v) if v is not None else float("nan")

    def set_cooldown(self, symbol: str, ts: Optional[pd.Timestamp]) -> None:
        self.coin(symbol).cooldown_until = ts.isoformat() if ts is not None else ""

    def get_cooldown(self, symbol: str) -> Optional[pd.Timestamp]:
        s = self.coin(symbol).cooldown_until
        return pd.Timestamp(s) if s else None

    # -- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "last_rebalance_date": self.last_rebalance_date,
            "breaker_until": self.breaker_until,
            "peak": self.peak,
            "day_trade_date": self.day_trade_date,
            "day_trade_count": self.day_trade_count,
            "activity_done_date": self.activity_done_date,
            "last_heartbeat": self.last_heartbeat,
            "coins": {
                k: {"entry_time": c.entry_time, "ext": c.ext,
                    "cooldown_until": c.cooldown_until}
                for k, c in self.coins.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        st = cls(
            version=int(d.get("version", 1)),
            last_rebalance_date=d.get("last_rebalance_date", ""),
            breaker_until=d.get("breaker_until", ""),
            peak=float(d.get("peak", 0.0) or 0.0),
            day_trade_date=d.get("day_trade_date", ""),
            day_trade_count=int(d.get("day_trade_count", 0) or 0),
            activity_done_date=d.get("activity_done_date", ""),
            last_heartbeat=d.get("last_heartbeat", ""),
        )
        for k, c in (d.get("coins") or {}).items():
            st.coins[k] = CoinState(
                entry_time=c.get("entry_time", ""),
                ext=c.get("ext"),
                cooldown_until=c.get("cooldown_until", ""),
            )
        return st

    # -- atomic persistence -------------------------------------------------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = json.dumps(self.to_dict(), indent=2)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @classmethod
    def load(cls, path: str) -> "State":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except Exception:
            # Corrupt state is safer to discard than to crash the bot.
            return cls()


def np_isfinite(x: float) -> bool:
    try:
        return x == x and abs(x) < float("inf")
    except Exception:
        return False
