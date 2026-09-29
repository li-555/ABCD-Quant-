"""Roostoo ticker recorder and fallback price source.

Every cycle the bot records the Roostoo ``/v3/ticker`` snapshot to local disk.
These self-recorded prices are the *fallback* data source: when Binance is
unreachable the bot can continue trading using its own recorded history (and only
skips a rebalance if that history is insufficient).

Only the last price per pair is stored; no order or position data is written here.
"""
from __future__ import annotations

from typing import Dict

import pandas as pd

from bot.config.settings import Config
from bot.data import store


def record_ticker(cfg: Config, ticker_data: Dict[str, dict], ts: pd.Timestamp) -> None:
    """Persist a ticker snapshot (``pair -> {..., 'LastPrice': x}``) to disk.

    Keys are Roostoo pairs (e.g. ``BTC/USD``); we strip the ``/USD`` suffix to use
    the bare coin as the storage symbol.
    """
    for pair, info in (ticker_data or {}).items():
        if "/" not in pair:
            continue
        coin = pair.split("/")[0]
        last = info.get("LastPrice")
        if last is None:
            continue
        try:
            store.append_snapshot(cfg, coin, ts, float(last))
        except Exception:
            # Never let logging/storage break the main loop.
            continue


def load_recorded_grid(coins: Dict[str, str], cfg: Config) -> pd.DataFrame:
    """Reconstruct a 30m price grid from recorded snapshots for ``coins``.

    ``coins`` maps coin -> binance symbol (unused here) and is accepted for a
    uniform interface.  Returns a DataFrame indexed by bar-close time (UTC).
    """
    cols = {}
    for coin in coins:
        s = store.load_snapshot(cfg, coin)
        grid = store.snapshot_to_grid(s, cfg.bar_min)
        if not grid.empty:
            cols[coin] = grid
    if not cols:
        return pd.DataFrame()
    raw = pd.concat(cols, axis=1)
    freq = f"{cfg.bar_min}min"
    grid = pd.date_range(raw.index.min(), raw.index.max(), freq=freq, tz="UTC")
    return raw.reindex(grid)
