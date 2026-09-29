"""Daily portfolio construction (long-only, variant A of research/backtest.py).

This is a faithful re-implementation of the rebalance block of
``research.backtest.run_backtest`` for the long-only, slow-layer configuration.
It is organised as *pure* functions so it can be unit tested and driven both by
the live scheduler and by the offline parity/alignment tests.

The orchestrator is :func:`compute_target_weights`.  It mirrors, bar for bar, the
following logic from the research backtester:

  * eligibility gate  : ``S_slow > gate_thr`` and ``score > entry_thr``
  * incumbent lag    : keep held positions inside ``hold_rank_long`` before filling
  * inverse-vol size : ``w_i = |score_i| / sigma_i`` then normalise
  * regime scaling   : breadth-of-uptrend filter ``m_l``
  * vol targeting    : ``g_vol = min(1, sigma_target / portfolio_sigma)``
  * drawdown scaling : ``m_dd`` with a hard circuit breaker
  * caps             : single-asset, cluster net/gross, total gross exposure
  * trade filter     : no-trade band, minimum trade, minimum holding time
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bot.config.settings import Config


def select_long(cand: np.ndarray, score: np.ndarray, held: np.ndarray,
                n: int, hold_n: int) -> List[int]:
    """Select up to ``n`` long candidates, protecting current holdings.

    Mirrors ``research.backtest.select`` for the long side: candidates are ranked
    by ``score`` descending; incumbents within ``hold_n`` are kept first, then the
    remaining slots are filled by the highest-scoring non-incumbents.
    """
    ci = np.where(cand)[0]
    if len(ci) == 0:
        return []
    order = ci[np.argsort(-score[ci], kind="stable")]
    rank = {int(i): r for r, i in enumerate(order)}
    kept = [int(i) for i in order if held[i] and rank[int(i)] < hold_n][:n]
    ks = set(kept)
    fill = [int(i) for i in order if int(i) not in ks][: n - len(kept)]
    return kept + fill


def inverse_vol_weights(idx: List[int], score: np.ndarray,
                        sigma_d: np.ndarray) -> np.ndarray:
    """Inverse-volatility weights for the selected assets (sum to 1)."""
    w = np.zeros(score.shape[0])
    if not idx:
        return w
    idx_a = np.array(idx)
    u = np.abs(score[idx_a]) / sigma_d[idx_a]
    w[idx_a] = u / u.sum()
    return w


def apply_caps(w: np.ndarray, cfg: Config,
              clusters: List[Tuple[np.ndarray, float]]) -> np.ndarray:
    """Apply single-asset, cluster net/gross and total-gross caps (3 passes).

    ``clusters`` is a list of ``(member_mask, net_cap)`` pairs.  Each cluster's
    gross cap is ``gross_mult * net_cap``.  For the v1 coin-only universe there is
    a single cluster (all coins, net cap ``coin_cap``).
    """
    v = w.copy()
    for _ in range(3):
        v = np.clip(v, -cfg.single_short, cfg.single_long)
        for mask, cap in clusters:
            if not mask.any():
                continue
            net = v[mask].sum()
            gross = np.abs(v[mask]).sum()
            f = 1.0
            if abs(net) > cap:
                f = min(f, cap / abs(net))
            if gross > cfg.gross_mult * cap:
                f = min(f, cfg.gross_mult * cap / gross)
            if f < 1:
                v[mask] *= f
    return np.clip(v, -cfg.single_short, cfg.single_long)


def breadth_regime(trend: np.ndarray, valid: np.ndarray, cfg: Config) -> float:
    """Market-breadth regime multiplier ``m_l``.

    ``b`` = share of assets with a positive trend (only over valid assets).  If
    fewer than 3 valid assets, ``b`` falls back to 0.5.
    """
    vt = np.isfinite(trend) & valid
    if vt.sum() >= 3:
        b = float(np.mean(trend[vt] > 0))
    else:
        b = 0.5
    return float(np.clip((b - cfg.regime_lo) / cfg.regime_span,
                         cfg.regime_floor, 1.0))


def vol_target(idx: List[int], unit: np.ndarray, lr: pd.DataFrame,
               t: int, cfg: Config) -> float:
    """Vol-targeting multiplier ``g_vol`` using a shrunk covariance estimate."""
    if not idx:
        return 1.0
    bph = cfg.bars_per_hour
    cov_n = cfg.h_to_bars(cfg.cov_h)
    rows = lr.iloc[max(0, t - cov_n + 1): t + 1]
    if rows.shape[0] <= 5:
        return 1.0
    Rw = rows.iloc[:, idx].to_numpy(dtype=float)
    if np.isnan(Rw).any():
        return 1.0
    bpd = 24.0 * bph
    C = np.atleast_2d(np.cov(Rw.T, bias=False)) * bpd
    C = 0.5 * C + 0.5 * np.diag(np.diag(C))
    sp = float(np.sqrt(max(float(unit @ C @ unit), 1e-12)))
    if sp <= 0:
        return 1.0
    return float(min(1.0, cfg.sigma_target / sp))


def drawdown_scale(equity: float, peak: float, breaker_until_bar: int, t: int,
                   cfg: Config) -> Tuple[float, int]:
    """Drawdown multiplier ``m_dd`` and (possibly updated) ``breaker_until_bar``."""
    dd = 1.0 - equity / peak if peak > 0 else 0.0
    if dd > cfg.dd_hard and t >= breaker_until_bar:
        breaker_until_bar = t + cfg.h_to_bars(cfg.breaker_h)
    if t < breaker_until_bar:
        m_dd = cfg.min_scale
    else:
        m_dd = float(np.clip(1.0 - dd / cfg.dd_scale, cfg.min_scale, 1.0))
    return m_dd, breaker_until_bar


@dataclass
class TargetDiag:
    """Diagnostics emitted alongside the target weights (for decisions.jsonl)."""
    selected: List[int]
    m_l: float
    g_vol: float
    m_dd: float
    sigma_port: Optional[float] = None


def compute_target_weights(
    sig_row: Dict[str, np.ndarray],
    w: np.ndarray,
    cooldown_bars: np.ndarray,
    entry_bars: np.ndarray,
    breaker_until_bar: int,
    equity: float,
    peak: float,
    lr: pd.DataFrame,
    real_mask: np.ndarray,
    t: int,
    cfg: Config,
    cluster_members: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, TargetDiag, int]:
    """Compute the target weights for a single rebalance at bar ``t``.

    Parameters
    ----------
    sig_row : dict
        Signal vectors at bar ``t`` keyed by ``S``, ``S_slow``, ``trend``,
        ``sig_d`` (each length ``n``).
    w : np.ndarray
        Current portfolio weights (length ``n``).
    cooldown_bars, entry_bars : np.ndarray
        Per-asset bar index of last cooldown-expiry / position entry.
    breaker_until_bar : int
        Bar index until which the circuit breaker is active.
    equity, peak : float
        Current equity and trailing peak (over the 14-day window).
    lr : pd.DataFrame
        Log-return frame (index = bar, columns = assets) for the cov estimate.
    real_mask : np.ndarray
        Assets that have a real, tradable price at bar ``t``.
    t : int
        Current bar index.
    cfg : Config
    cluster_members : np.ndarray, optional
        Boolean mask of assets belonging to the (coin) cluster.  Defaults to all.

    Returns
    -------
    tgt : np.ndarray
        Target weights after caps and the no-trade / min-hold filter.
    diag : TargetDiag
    breaker_until_bar : int
        Possibly updated breaker-expiry bar.
    """
    n = w.shape[0]
    S = sig_row["S"]
    Ss = sig_row["S_slow"]
    Tr = sig_row["trend"]
    Sd = sig_row["sig_d"]

    valid = np.isfinite(S) & np.isfinite(Ss) & np.isfinite(Sd) & real_mask
    trad = valid & real_mask
    al = valid & (Ss > cfg.gate_thr)

    okm = trad & (cooldown_bars <= t)
    s0 = np.nan_to_num(S)

    long_sel = select_long(
        okm & al & (s0 > cfg.entry_thr), s0, w > 0, cfg.n_long, cfg.hold_rank_long
    )

    wl = inverse_vol_weights(long_sel, s0, Sd)
    m_l = breadth_regime(Tr, valid, cfg)

    g_vol = vol_target(long_sel, wl[long_sel] if long_sel else np.array([]),
                       lr, t, cfg)

    m_dd, breaker_until_bar = drawdown_scale(equity, peak, breaker_until_bar, t, cfg)

    tgt = g_vol * m_dd * m_l * wl

    if cluster_members is None:
        cluster_members = np.ones(n, dtype=bool)
    tgt = apply_caps(tgt, cfg, [(cluster_members, cfg.coin_cap)])

    # Keep positions we cannot trade frozen at their current weight.
    frozen_g = float(np.abs(w[~trad]).sum())
    avail = max(cfg.max_gross - frozen_g, 0.0)
    g = float(np.abs(tgt[trad]).sum())
    if g > avail and g > 0:
        tgt[trad] *= avail / g
    tgt[~trad] = w[~trad]

    # ---- trade filter (no-trade band, minimum trade, minimum holding) ------
    min_hold = cfg.h_to_bars(cfg.min_hold_h)
    new_w = w.copy()
    for i in np.where(trad)[0]:
        cur = w[i]
        tg = tgt[i]
        d = abs(tg - cur)
        if tg == 0 and abs(cur) < cfg.min_trade:
            new_w[i] = 0.0
            continue
        if d < cfg.min_trade:
            continue
        exit_full = (tg == 0)
        flip = (cur * tg < 0)
        band = max(cfg.band_abs, cfg.band_rel * abs(tg))
        if not (cur == 0 or exit_full or flip or d > band):
            continue
        if cur != 0 and (t - entry_bars[i]) < min_hold and \
                (exit_full or flip or abs(tg) < abs(cur)):
            # Long-only: skip the reduction if the asset still qualifies as a
            # long candidate (its slow signal remains above the gate).
            if cur > 0 and al[i]:
                continue
        new_w[i] = tg

    tgt = new_w
    diag = TargetDiag(selected=long_sel, m_l=m_l, g_vol=g_vol, m_dd=m_dd)
    return tgt, diag, breaker_until_bar
