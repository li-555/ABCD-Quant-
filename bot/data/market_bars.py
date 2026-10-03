"""OHLCV feed for optional factors. Cache and retain actual missing bars."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import requests

from bot.config.settings import Config


FIELDS = ("open", "high", "low", "close", "volume")


def parse_klines(rows, bar_min: int) -> pd.DataFrame:
    """Binance REST (ms) and public archives (ms or us); close-time convention."""
    raw = pd.DataFrame(rows)
    if raw.empty:
        return pd.DataFrame(columns=FIELDS, index=pd.DatetimeIndex([], tz="UTC"))
    times = pd.to_numeric(raw.iloc[:, 0], errors="raise").astype("int64")
    ns = np.where(times >= 10**14, times * 1000, times * 1_000_000)
    idx = pd.to_datetime(ns, utc=True) + pd.Timedelta(minutes=bar_min)
    frame = raw.iloc[:, 1:6].astype(float)
    frame.columns = FIELDS
    frame.index = idx
    if frame.index.has_duplicates:
        raise ValueError("duplicate kline timestamps")
    if not np.isfinite(frame.to_numpy()).all():
        raise ValueError("non-finite OHLCV data")
    if (frame[list(FIELDS[:4])] <= 0).any().any() or (frame.volume < 0).any():
        raise ValueError("invalid OHLCV prices or volume")
    if (frame.high < frame[["open", "close", "low"]].max(axis=1)).any() or (
            frame.low > frame[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("inconsistent OHLC bounds")
    return frame.sort_index()


def fetch_bars(symbol: str, cfg: Config, session=None, now=None) -> pd.DataFrame:
    """Incrementally update cache; never accept an in-progress candle."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    end = now.floor(f"{cfg.bar_min}min")
    begin = end - pd.Timedelta(days=cfg.history_days)
    path = Path(cfg.data_dir) / f"ohlcv_{symbol}_{cfg.bar_min}m.csv"
    old = pd.DataFrame(columns=FIELDS, index=pd.DatetimeIndex([], tz="UTC"))
    if path.exists():
        old = pd.read_csv(path, index_col=0)
        old.index = pd.to_datetime(old.index, utc=True)
        old = old.loc[(old.index > begin) & (old.index <= end), list(FIELDS)]
    # Re-fetch the last close as well; cache freshness is not assumed.
    cur = begin if old.empty else max(begin, old.index[-1] - pd.Timedelta(minutes=cfg.bar_min))
    client = session or requests.Session()
    chunks = [old] if not old.empty else []
    while cur < end:
        rows = None
        for host in cfg.binance_hosts:
            try:
                response = client.get(f"{host}/api/v3/klines", params={
                    "symbol": symbol, "interval": {15: "15m", 30: "30m", 60: "1h"}[cfg.bar_min],
                    "startTime": int(cur.timestamp() * 1000),
                    "endTime": int(end.timestamp() * 1000) - 1, "limit": 1000}, timeout=15)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list) and data:
                    rows = data
                    break
            except (requests.RequestException, ValueError):
                continue
        if not rows:
            break  # stale cache is checked by the scheduler, never disguised
        frame = parse_klines(rows, cfg.bar_min)
        frame = frame.loc[(frame.index > begin) & (frame.index <= end)]
        if frame.empty or frame.index[-1] <= cur:
            break
        chunks.append(frame)
        cur = frame.index[-1]
    if not chunks:
        return old
    bars = pd.concat(chunks)
    bars = bars.loc[~bars.index.duplicated(keep="last")].sort_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    bars.to_csv(tmp)
    tmp.replace(path)
    return bars


def load_market_grid(universe, cfg: Config) -> dict[str, pd.DataFrame]:
    frames = {u["coin"]: fetch_bars(u["binance_symbol"], cfg) for u in universe}
    nonempty = [f for f in frames.values() if not f.empty]
    if not nonempty:
        raise RuntimeError("No OHLCV history available")
    idx = pd.date_range(min(f.index.min() for f in nonempty),
                        max(f.index.max() for f in nonempty),
                        freq=f"{cfg.bar_min}min", tz="UTC")
    return {key: pd.DataFrame({c: f[key] for c, f in frames.items()}).reindex(idx)
            for key in FIELDS}
