from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def make_synthetic_data(n_assets: int = 25, n_days: int = 260, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    assets = [f"A{i:03d}" for i in range(n_assets)]

    rows = []
    for a in assets:
        daily_ret = rng.normal(0.0003, 0.015, size=n_days)
        price = 100 * np.cumprod(1 + daily_ret)
        volume = rng.lognormal(mean=11, sigma=0.4, size=n_days)
        rows.append(pd.DataFrame({"date": dates, "asset": a, "close": price, "volume": volume}))
    return pd.concat(rows, ignore_index=True)


def load_market_data(path: str | Path) -> pd.DataFrame:
    """Load OHLCV-style market data with columns: date, asset, close, volume."""
    df = pd.read_csv(path)
    if "date" not in df.columns:
        raise ValueError("Input data must include a 'date' column")
    df["date"] = pd.to_datetime(df["date"])
    return df


def clean_market_data(df: pd.DataFrame) -> pd.DataFrame:
    """Basic cleaning: sorting, deduplication, forward fills, and return creation."""
    required = {"date", "asset", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    clean = df.copy()
    clean = clean.drop_duplicates(subset=["date", "asset"])
    clean = clean.sort_values(["asset", "date"])
    clean[["close", "volume"]] = (
        clean.groupby("asset")[["close", "volume"]].ffill().bfill()
    )
    clean["ret_1d"] = clean.groupby("asset")["close"].pct_change().replace([np.inf, -np.inf], np.nan)
    clean["ret_fwd_1d"] = clean.groupby("asset")["close"].shift(-1) / clean["close"] - 1.0
    return clean.dropna(subset=["ret_1d", "ret_fwd_1d"]).reset_index(drop=True)
