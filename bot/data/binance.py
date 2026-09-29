"""Binance 30-minute klines with pagination and host fallback.

Provides historical spot close prices used to warm up the signals.  The public
data endpoints require no authentication and are fetched over HTTPS with a
round-robin fallback across several Binance hosts.  Results are cached to CSV so
the warm-up does not re-download on every restart.

This module performs no I/O at import time and is safe to import in offline unit
tests (the network helpers are only invoked by the live scheduler).
"""
from __future__ import annotations

import os
import time
from typing import List, Optional

import numpy as np
import pandas as pd
import requests

from bot.config.settings import Config

_BIN_INTERVAL = {15: "15m", 30: "30m", 60: "1h"}
_HTTP_TIMEOUT = 15


def cache_path(data_dir: str, symbol: str, bar_min: int, days: int) -> str:
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"bn_{symbol}_{bar_min}m_{days}d.csv")


def load_cache(path: str) -> Optional[pd.Series]:
    if not os.path.exists(path):
        return None
    try:
        s = pd.read_csv(path, index_col=0)
        s.index = pd.to_datetime(s.index, utc=True)
        return s.iloc[:, 0]
    except Exception:
        return None


def save_cache(path: str, series: pd.Series) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    series.to_csv(path)


def _fetch_klines_once(symbol: str, bar_min: int, start_ms: int, host: str,
                       session: requests.Session) -> Optional[list]:
    interval = _BIN_INTERVAL[bar_min]
    try:
        r = session.get(
            f"{host}/api/v3/klines",
            params=dict(symbol=symbol, interval=interval,
                        startTime=start_ms, limit=1000),
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def fetch_symbol_series(symbol: str, bar_min: int, days: int, cfg: Config,
                        session: Optional[requests.Session] = None,
                        refresh: bool = False) -> Optional[pd.Series]:
    """Fetch ``days`` of ``bar_min`` closes for ``symbol`` (e.g. ``BTCUSDT``).

    Returns a Series indexed by bar CLOSE time (UTC), or ``None`` on failure.
    """
    path = cache_path(cfg.data_dir, symbol, bar_min, days)
    if not refresh:
        cached = load_cache(path)
        if cached is not None and not cached.empty:
            return cached

    session = session or requests.Session()
    interval = _BIN_INTERVAL[bar_min]
    step_ms = bar_min * 60_000
    end = int(time.time() * 1000)
    cur = end - int(days * 86_400_000)
    rows: List[list] = []
    base_ok: Optional[str] = None

    while cur < end:
        data = None
        for host in ([base_ok] if base_ok else cfg.binance_hosts):
            data = _fetch_klines_once(symbol, bar_min, cur, host, session)
            if data:
                base_ok = host
                break
        if not data:
            break
        rows += data
        cur = data[-1][0] + step_ms
        if len(data) < 1000:
            break
        time.sleep(0.1)

    if not rows:
        return None

    df = pd.DataFrame(rows)
    close_t = pd.to_datetime(df[0].astype("int64"), unit="ms", utc=True) + \
        pd.Timedelta(minutes=bar_min)
    s = pd.Series(df[4].astype(float).values, index=close_t, name=symbol)
    s = s[s.index <= pd.Timestamp.now(tz="UTC")]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    save_cache(path, s)
    return s


def load_price_grid(coins: List[str], cfg: Config, days: Optional[int] = None,
                    refresh: bool = False,
                    session: Optional[requests.Session] = None) -> pd.DataFrame:
    """Build a regular ``bar_min`` price grid for ``coins`` (binance symbols).

    Missing/short symbols are dropped; the result is reindexed to a common,
    gap-free grid so the signal code always sees aligned bars.
    """
    days = days or cfg.history_days
    cols = {}
    for c in coins:
        s = fetch_symbol_series(c, cfg.bar_min, days, cfg, session=session,
                                refresh=refresh)
        if s is None or s.empty:
            continue
        cols[c] = s
    if not cols:
        raise RuntimeError("No Binance data could be downloaded for any symbol.")
    raw = pd.concat(cols, axis=1)
    freq = f"{cfg.bar_min}min"
    grid = pd.date_range(raw.index.min(), raw.index.max(), freq=freq, tz="UTC")
    return raw.reindex(grid)
