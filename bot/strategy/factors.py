"""Causal optional factors for the crypto-only bot; no labels or future returns."""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.config.settings import Config


WEIGHTS = {
    "relative_strength": "factor_rs_weight",
    "mean_reversion": "factor_mr_weight",
    "volume": "factor_volume_weight",
    "session": "factor_session_weight",
}


def enabled(cfg: Config) -> bool:
    return any(getattr(cfg, key) > 0 for key in WEIGHTS.values())


def build_factors(close: pd.DataFrame, cfg: Config,
                  volume: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Inputs are regular UTC close-time grids. Missing inputs remain unavailable.

    Session = trailing mean of completed UTC days' 00-08 returns minus 08-24
    returns. This is a crypto time-of-day proxy, not an equity overnight gap.
    A day's observation first becomes available at its ending midnight.
    """
    if not isinstance(close.index, pd.DatetimeIndex) or close.index.tz is None:
        raise ValueError("factors require timezone-aware close times")
    if not close.index.is_unique or not close.index.is_monotonic_increasing:
        raise ValueError("factor timestamps must be unique and increasing")
    expected = pd.Timedelta(minutes=cfg.bar_min)
    if len(close) > 1 and not (close.index.to_series().diff().iloc[1:] == expected).all():
        raise ValueError("factors require a regular grid; reindex gaps to NaN")
    logp = np.log(close.where(close > 0))
    ret = logp.diff()
    nvol = max(2, cfg.h_to_bars(cfg.vol_h))
    sigma = np.sqrt(ret.pow(2).ewm(span=nvol, min_periods=nvol, adjust=False).mean()).clip(lower=1e-4)
    out = {}
    if cfg.factor_rs_weight:
        if cfg.factor_benchmark not in close:
            raise ValueError(f"relative strength needs benchmark {cfg.factor_benchmark}")
        n = max(2, cfg.h_to_bars(cfg.factor_rs_h))
        momentum = logp.diff(n)
        excess = momentum.sub(momentum[cfg.factor_benchmark], axis=0)
        out["relative_strength"] = np.tanh(excess / (sigma * np.sqrt(n)))
    if cfg.factor_mr_weight:
        n = max(2, cfg.h_to_bars(cfg.factor_mr_h))
        mean = logp.rolling(n, min_periods=n).mean()
        out["mean_reversion"] = -np.tanh((logp - mean) / (sigma * np.sqrt(n)))
    if cfg.factor_volume_weight:
        if volume is None:
            raise ValueError("volume factor enabled but no volume grid supplied")
        if not volume.index.equals(close.index) or not volume.columns.equals(close.columns):
            raise ValueError("volume must match close timestamps and asset order exactly")
        v = volume.where(np.isfinite(volume) & (volume >= 0))
        n = max(2, cfg.h_to_bars(cfg.factor_volume_h))
        den = v.rolling(n, min_periods=n).sum().replace(0, np.nan)
        out["volume"] = (np.sign(ret) * v).rolling(n, min_periods=n).sum() / den
    if cfg.factor_session_weight:
        utc = ret.copy()
        utc.index = utc.index.tz_convert("UTC")
        bar_start = utc.index - expected
        day = bar_start.floor("D")
        night = pd.Series(bar_start.hour < cfg.factor_session_split_utc, index=utc.index)
        count_n = cfg.factor_session_split_utc * 60 // cfg.bar_min
        count_d = (24 - cfg.factor_session_split_utc) * 60 // cfg.bar_min
        overnight = utc.where(night, axis=0).groupby(day).sum(min_count=count_n)
        intraday = utc.where(~night, axis=0).groupby(day).sum(min_count=count_d)
        contrast = (overnight - intraday).rolling(cfg.factor_session_days,
                                                min_periods=cfg.factor_session_days).mean()
        contrast.index = contrast.index + pd.Timedelta(days=1)
        available = contrast.reindex(utc.index, method="ffill")
        available.index = close.index
        out["session"] = np.tanh(available / (sigma * np.sqrt(24 * cfg.bars_per_hour)))
    return {k: value.replace([np.inf, -np.inf], np.nan).clip(-1, 1).where(close.notna())
            for k, value in out.items()}


def blend(base: pd.DataFrame, cfg: Config, factors: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Keep the original slow direction gate; augment only ranking/entry score.

    Unavailable components contribute zero AND retain their base share, so a
    missing volume observation never becomes invented volume or liquidates a
    position merely because a new factor is warming up.
    """
    result = base.copy()
    for name, value in factors.items():
        weight = getattr(cfg, WEIGHTS[name])
        result = result + weight * (value - base).where(value.notna(), 0.0)
    return result
