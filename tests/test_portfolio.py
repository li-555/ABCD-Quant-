"""Portfolio construction unit tests (pure functions, offline)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.strategy.portfolio import (
    select_long, inverse_vol_weights, apply_caps, breadth_regime,
    vol_target, drawdown_scale, compute_target_weights,
)
from bot.config.settings import Config


def test_select_long_ranking_and_cap():
    cand = np.array([True, True, True, True, False])
    score = np.array([0.5, 0.1, 0.9, 0.3, 0.0])
    held = np.array([False, False, False, False, False])
    out = select_long(cand, score, held, n=3, hold_n=10)
    # top scores among candidates: 0.9(idx2), 0.5(idx0), 0.3(idx3)
    assert set(out) == {2, 0, 3}


def test_select_long_incumbent_protection():
    cand = np.array([True, True, True])
    score = np.array([0.9, 0.8, 0.1])
    held = np.array([False, True, False])
    # only n=1 slot; incumbent(idx1) is protected even though idx0 scores higher
    out = select_long(cand, score, held, n=1, hold_n=10)
    assert out == [1]


def test_inverse_vol_weights_normalised():
    idx = [0, 2, 4]
    score = np.array([0.6, 0.0, 0.3, 0.0, 0.1])
    sigma = np.array([0.1, 0.1, 0.2, 0.1, 0.05])
    w = inverse_vol_weights(idx, score, sigma)
    assert abs(w.sum() - 1.0) < 1e-12
    # higher |score|/sigma => higher weight (idx4 has the smallest sigma)
    assert w[0] > w[4] > w[2]


def test_apply_caps_single_and_cluster():
    cfg = Config()
    n = 4
    v = np.array([0.5, 0.5, 0.5, 0.5])  # all above single_long 0.35
    out = apply_caps(v, cfg, [(np.ones(n, bool), cfg.coin_cap)])
    assert np.all(out <= cfg.single_long + 1e-12)
    # net cluster cap (0.70) and gross cap (1.05) respected
    assert out.sum() <= cfg.coin_cap + 1e-9
    assert np.abs(out).sum() <= cfg.coin_cap * cfg.gross_mult + 1e-9


def test_breadth_regime_floor_fallback():
    cfg = Config()
    trend = np.array([1.0, -1.0])  # 1 of 2 positive
    valid = np.array([True, True])
    # fewer than 3 valid -> fallback b = 0.5
    assert abs(breadth_regime(trend, valid, cfg) - 0.5) < 1e-12


def test_breadth_regime_scaling():
    cfg = Config()
    trend = np.ones(5)  # all up -> b = 1 -> m_l = 1
    valid = np.ones(5, bool)
    assert abs(breadth_regime(trend, valid, cfg) - 1.0) < 1e-12


def test_vol_target_shrinks_with_volatility():
    cfg = Config()
    rng = np.random.default_rng(0)
    idx = [0, 1, 2]
    unit = np.array([0.4, 0.3, 0.3])
    low = pd.DataFrame(rng.normal(0, 0.001, (300, 3)))
    high = pd.DataFrame(rng.normal(0, 0.02, (300, 3)))
    g_low = vol_target(idx, unit, low, t=299, cfg=cfg)
    g_high = vol_target(idx, unit, high, t=299, cfg=cfg)
    assert 0 < g_high <= g_low <= 1.0


def test_drawdown_scale_breaker_and_clamp():
    cfg = Config()
    # small drawdown -> m_dd in (min_scale, 1]
    m_dd, _ = drawdown_scale(equity=0.95, peak=1.0, breaker_until_bar=-1, t=10, cfg=cfg)
    assert cfg.min_scale <= m_dd <= 1.0
    # hard drawdown triggers breaker -> a non-negative breaker window opens
    _, bu = drawdown_scale(equity=0.8, peak=1.0, breaker_until_bar=-1, t=10, cfg=cfg)
    assert bu >= 12  # breaker window opened (bar offset)
    m_dd2, _ = drawdown_scale(equity=0.8, peak=1.0, breaker_until_bar=20, t=15, cfg=cfg)
    assert abs(m_dd2 - cfg.min_scale) < 1e-12


def _scenario(cfg, n=5, seed=3):
    rng = np.random.default_rng(seed)
    lr = pd.DataFrame(rng.normal(0, 0.01, (260, n)))
    sig_row = {
        "S": np.array([0.6, 0.5, 0.4, 0.3, 0.2]),
        "S_slow": np.array([0.6, 0.5, 0.4, 0.3, 0.2]),
        "trend": np.array([0.6, 0.5, 0.4, 0.3, -0.2]),
        "sig_d": np.array([0.03] * n),
    }
    return lr, sig_row


def test_compute_target_weights_caps_and_band():
    cfg = Config()
    n = 5
    lr, sig_row = _scenario(cfg, n)
    w = np.zeros(n)
    tgt, _diag, _b = compute_target_weights(
        sig_row, w, cooldown_bars=np.full(n, -10), entry_bars=np.full(n, -10),
        breaker_until_bar=-1, equity=100_000, peak=100_000, lr=lr,
        real_mask=np.ones(n, bool), t=200, cfg=cfg,
        cluster_members=np.ones(n, bool))
    # caps
    assert np.all(tgt <= cfg.single_long + 1e-9)
    assert tgt.sum() <= cfg.coin_cap + 1e-9
    assert np.abs(tgt).sum() <= cfg.coin_cap * cfg.gross_mult + 1e-9
    # all selected assets cleared the gate
    assert np.all(tgt[sig_row["S_slow"] <= cfg.gate_thr] == 0)


def test_compute_target_weights_no_trade_band_and_min_hold():
    cfg = Config()
    n = 5
    lr, sig_row = _scenario(cfg, n)
    # current weights equal target-ish: small diffs must be ignored (band/min_trade)
    w = sig_row["S_slow"] * 0.5  # some positions already on
    w = w / w.sum() if w.sum() else w  # rough
    tgt, _diag, _b = compute_target_weights(
        sig_row, w, cooldown_bars=np.full(n, -10), entry_bars=np.full(n, -10),
        breaker_until_bar=-1, equity=100_000, peak=100_000, lr=lr,
        real_mask=np.ones(n, bool), t=200, cfg=cfg,
        cluster_members=np.ones(n, bool))
    # a position just entered (entry at t) whose target is a small reduction but
    # still qualifies must NOT be reduced (min-hold)
    i = 0
    w2 = np.zeros(n)
    w2[i] = 0.3  # held
    entry = np.full(n, -10)
    entry[i] = 200  # entered this bar
    tgt2, _d, _b2 = compute_target_weights(
        sig_row, w2, cooldown_bars=np.full(n, -10), entry_bars=entry,
        breaker_until_bar=-1, equity=100_000, peak=100_000, lr=lr,
        real_mask=np.ones(n, bool), t=200, cfg=cfg,
        cluster_members=np.ones(n, bool))
    # target for i should be kept at current (no reduction while in min-hold)
    assert abs(tgt2[i] - w2[i]) < 1e-12
