from __future__ import annotations

import numpy as np
import pandas as pd


def _rolling_corr(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    return a.rolling(window).corr(b)


def compute_example_factors(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Compute momentum, mean reversion, volatility, and volume-price correlation factors."""
    req = {"date", "asset", "close", "volume", "ret_1d"}
    missing = req - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns for factor calc: {sorted(missing)}")

    out = df.copy()
    g = out.groupby("asset", group_keys=False)

    out["factor_momentum"] = g["close"].pct_change(window)
    out["factor_mean_reversion"] = -g["ret_1d"].rolling(window).mean().reset_index(level=0, drop=True)
    out["factor_volatility"] = -g["ret_1d"].rolling(window).std().reset_index(level=0, drop=True)

    vp_corr = g.apply(lambda x: _rolling_corr(x["ret_1d"], x["volume"].pct_change(), window))
    out["factor_volume_price_corr"] = vp_corr.reset_index(level=0, drop=True)

    factor_cols = [
        "factor_momentum",
        "factor_mean_reversion",
        "factor_volatility",
        "factor_volume_price_corr",
    ]
    out[factor_cols] = out[factor_cols].replace([np.inf, -np.inf], np.nan)
    return out.dropna(subset=factor_cols + ["ret_fwd_1d"]).reset_index(drop=True)
