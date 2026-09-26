from __future__ import annotations

import numpy as np
import pandas as pd


def build_portfolio_weights(
    signal_df: pd.DataFrame,
    signal_col: str = "signal",
    long_quantile: float = 0.8,
    short_quantile: float = 0.2,
) -> pd.DataFrame:
    """Build simple dollar-neutral long-short weights from signal quantiles."""
    req = {"date", "asset", signal_col}
    missing = req - set(signal_df.columns)
    if missing:
        raise ValueError(f"Missing columns for portfolio build: {sorted(missing)}")

    def _daily_w(x: pd.DataFrame) -> pd.DataFrame:
        x = x.copy()
        long_th = x[signal_col].quantile(long_quantile)
        short_th = x[signal_col].quantile(short_quantile)
        x["weight"] = 0.0
        x.loc[x[signal_col] >= long_th, "weight"] = 1.0
        x.loc[x[signal_col] <= short_th, "weight"] = -1.0

        gross = x["weight"].abs().sum()
        if gross > 0:
            x["weight"] = x["weight"] / gross
        return x[["date", "asset", "weight"]]

    weighted = []
    for dt, frame in signal_df.groupby("date"):
        out = _daily_w(frame)
        out["date"] = dt
        weighted.append(out)
    return pd.concat(weighted, ignore_index=True)
