from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler


def combine_factors_to_signal(
    df: pd.DataFrame,
    factor_cols: list[str],
    weights: list[float] | None = None,
    by_date: bool = True,
) -> pd.DataFrame:
    """Standardize factors then combine using weighted average."""
    out = df.copy()
    if weights is None:
        weights = [1.0 / len(factor_cols)] * len(factor_cols)
    if len(weights) != len(factor_cols):
        raise ValueError("weights length must match factor_cols length")

    w = np.array(weights, dtype=float)
    w = w / w.sum()

    def _combine(frame: pd.DataFrame) -> pd.DataFrame:
        scaler = StandardScaler()
        z = scaler.fit_transform(frame[factor_cols])
        frame = frame.copy()
        frame["signal"] = z @ w
        return frame

    if by_date:
        grouped = []
        for dt, frame in out.groupby("date"):
            c = _combine(frame)
            c["date"] = dt
            grouped.append(c)
        out = pd.concat(grouped, ignore_index=True)
    else:
        out = _combine(out)
    return out
