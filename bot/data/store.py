"""Local on-disk cache for price bars and recorded ticker snapshots.

A tiny CSV-backed store used to persist (a) Binance historical bars (handled in
``binance.py``) and (b) the bot's own Roostoo ticker snapshots, which serve as a
fallback data source when Binance is unreachable.  Everything is plain CSV under
``cfg.data_dir`` so it is trivial to inspect and never requires extra libraries.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

import pandas as pd

from bot.config.settings import Config


def snapshot_path(cfg: Config, symbol: str) -> str:
    os.makedirs(cfg.data_dir, exist_ok=True)
    return os.path.join(cfg.data_dir, f"rt_snap_{symbol}.csv")


def append_snapshot(cfg: Config, symbol: str, ts: pd.Timestamp,
                    last: float) -> None:
    """Append one (timestamp, last) row to the per-symbol snapshot file."""
    path = snapshot_path(cfg, symbol)
    line = f"{ts.isoformat()},{last}\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def load_snapshot(cfg: Config, symbol: str) -> Optional[pd.Series]:
    """Load a recorded snapshot series (index = UTC timestamp, value = last)."""
    path = snapshot_path(cfg, symbol)
    if not os.path.exists(path):
        return None
    try:
        s = pd.read_csv(path, header=None, names=["ts", "last"])
        s["ts"] = pd.to_datetime(s["ts"], utc=True)
        s = s.set_index("ts")["last"]
        return s.sort_index()
    except Exception:
        return None


def snapshot_to_grid(series: pd.Series, bar_min: int) -> pd.Series:
    """Resample an irregular recorded series to a regular bar grid (ffill then bfill).

    Used as a fallback price source: we keep the most recent recorded price for
    each 30-minute bucket.
    """
    if series is None or series.empty:
        return pd.Series(dtype=float)
    freq = f"{bar_min}min"
    resampled = series.resample(freq, label="right", closed="right").last().ffill()
    resampled = resampled.bfill()
    return resampled
