"""Slow-layer cross-sectional signals (variant A of research/backtest.py).

This module is a faithful, dependency-light re-implementation of
``research.backtest.build_signals`` for the slow layer only (``use_fast=False``,
``allow_short`` irrelevant).  It is deliberately a set of *pure* functions: no
I/O, no global state.  ``build_signals`` returns pandas DataFrames with the same
index (bar close time, UTC) and columns (asset) as the input price frame, so the
output can be compared cell-by-cell against ``research.backtest`` in the parity
test (``tests/test_signals.py``).

Conventions (identical to the research backtester, no look-ahead):
  * ``P`` is indexed by bar CLOSE time (UTC); only closed bars are passed in.
  * Signals at bar ``t`` use data up to and including ``t``.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from bot.config.settings import Config


def _H(bar_min: int, hours: float) -> int:
    """Integer window in bars for a horizon of ``hours`` (min 2 bars)."""
    bph = 60.0 / bar_min
    return max(2, int(round(hours * bph)))


def build_signals(P: pd.DataFrame, cfg: Config,
                  volume: pd.DataFrame | None = None) -> Dict[str, pd.DataFrame]:
    """Compute the slow-layer signals for a price frame.

    Parameters
    ----------
    P : pd.DataFrame
        Close prices, indexed by bar-close time (UTC), columns are assets.
    cfg : Config
        Strategy parameters (windows, weights, overheat).

    Returns
    -------
    dict with keys ``S``, ``S_slow``, ``trend``, ``sig_d``; each a DataFrame
    aligned to ``P`` (same index / columns).
    """
    bar_min = cfg.bar_min
    bph = 60.0 / bar_min
    H = lambda h: _H(bar_min, h)

    logP = np.log(P)
    R = logP.diff()

    vb = H(cfg.vol_h)
    sig = np.sqrt(
        (R ** 2).ewm(span=vb, min_periods=max(2, vb // 2), adjust=False).mean()
    )
    sig = sig.clip(lower=1e-4)

    def ema(x: pd.DataFrame, h: float) -> pd.DataFrame:
        n = H(h)
        return x.ewm(span=n, min_periods=n, adjust=False).mean()

    ns = H(cfg.slow_slow_h)
    trend = np.tanh(
        (ema(logP, cfg.slow_fast_h) - ema(logP, cfg.slow_slow_h))
        / (sig * np.sqrt(ns))
    )

    nm = H(cfg.mom_h)
    m = (logP - logP.shift(nm)) / (sig * np.sqrt(nm))
    mom = 2 * m.rank(axis=1, pct=True) - 1

    nd = H(cfg.don_h)
    hi = P.rolling(nd).max()
    lo = P.rolling(nd).min()
    don = ((P - (hi + lo) / 2) / (0.5 * (hi - lo)))
    don = don.replace([np.inf, -np.inf], np.nan).clip(-1, 1)

    S_slow = cfg.w_trend * trend + cfg.w_mom * mom + cfg.w_don * don

    n1 = H(cfg.overheat_h)
    z1 = (logP - logP.shift(n1)) / (sig * np.sqrt(n1))
    hot = (z1.abs() > cfg.overheat_z) & (np.sign(z1) == np.sign(S_slow))
    S_slow = S_slow.where(~hot, S_slow * 0.5)

    # Variant A: no fast layer, no reversal term -> S is just S_slow.
    S = S_slow.copy()
    from bot.strategy.factors import enabled, build_factors, blend
    if enabled(cfg):
        S = blend(S, cfg, build_factors(P, cfg, volume))

    sig_d = sig * np.sqrt(24 * bph)

    return {
        "S": S,
        "S_slow": S_slow,
        "trend": trend,
        "sig_d": sig_d,
    }
