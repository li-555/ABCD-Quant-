"""Backtest alignment (CODING_AGENT_PROMPT 7.4).

Strategy v1 = research ``backtest.py`` variant A (slow layer only, 30m bars,
daily rebalance at UTC 00:00, crypto-only, long-only).  This test proves the
live bot's portfolio construction (``bot.strategy.portfolio.compute_target_weights``)
reproduces the research engine's rebalance decisions *exactly*.

Design
------
``research.run_backtest`` is the source of truth.  We run it fee-less (the fee
model is orthogonal to portfolio construction -- research computes its
drawdown scaler on the *pre-cost* equity of the current bar, whereas a live
account naturally reads its *post-cost* balance; with zero fees the two
conventions coincide) and obtain its trade log.

We then *replay* the backtest's weight evolution bar by bar to reconstruct, at
every rebalance bar, the exact state the research engine had entering the
rebalance:

  * ``w_before``  -- full weight vector, **including mark-to-market drift**
                     (research scales ``w`` every bar via
                     ``w = w*(1+r)/(1+w@r)`` *before* stops/rebalance),
  * ``entry``     -- per-asset entry bar (research ``entry``),
  * ``cooldown``  -- per-asset cooldown-expiry bar (research ``cooldown``,
                     set only on trailing-stop exits),
  * ``breaker``   -- circuit-breaker expiry bar,
  * ``equity`` / ``peak`` -- the *research* equity and 14-day trailing peak.

Those are fed straight into the bot's ``compute_target_weights``.  The replay
includes the MTM drift (the original design omitted it and therefore fed the
bot a stale ``w_before``, which silently broke the no-trade band).  If the bot
is a faithful port, its returned target equals research's post-rebalance ``w``.
We assert selection (which assets are held) is identical and target weights
agree within ``1e-6`` (the math is in fact identical, so the real diff is
float noise far below that).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research import backtest as rb
from bot.strategy.signals import build_signals
from bot.strategy.portfolio import compute_target_weights
from bot.config.settings import Config
from tests.helpers import synth_prices


def _v1_params():
    # Fee fields set to 0 so the reference backtest is fee-less (see module doc).
    # These must stay in lock-step with bot.config.settings.Config defaults.
    return rb.Params(
        bar_min=30, rebal_min=1440, use_fast=False, allow_short=False,
        coin_cap=0.70, single_long=0.35, max_gross=1.0,
        band_abs=0.08, band_rel=0.30, min_trade=0.005, min_hold_h=12,
        sigma_target=0.04, dd_scale=0.10, dd_hard=0.12, dd_window_h=336,
        breaker_h=12, min_scale=0.5, regime_lo=0.25, regime_span=0.5,
        regime_floor=0.5, gate_thr=0.2, entry_thr=0.2, n_long=6,
        hold_rank_long=10, cov_h=120,
        maker_bps=0.0, taker_bps=0.0, slip_bps=0.0,
    )


def test_alignment_with_research_backtest():
    P, real = synth_prices(bar_min=30, days=90, seed=7)
    cfg = Config()
    prm = _v1_params()

    sg = rb.build_signals(P, real, prm)
    res = rb.run_backtest(P, real, sg, prm, capital=100_000.0)
    trades = res["trades"].sort_values("time")
    eq_norm = res["equity"] / 100_000.0           # research equity curve

    cols = list(P.columns)
    n = len(cols)
    idx = P.index
    lr = np.log(P).diff()
    sig = build_signals(P, cfg)

    bph = 60.0 / prm.bar_min                      # bars per hour = 2
    cool_bars = int(round(prm.cool_h * bph))       # trailing-stop cooldown
    dd_win = int(round(prm.dd_window_h * bph))     # 14-day peak window
    eq_vals = eq_norm.values
    ts_to_bar = {ts: i for i, ts in enumerate(idx)}

    # Group trades by bar (research logs one row per changed asset).
    bar_trades: dict = {}
    for _, row in trades.iterrows():
        bar_trades.setdefault(ts_to_bar[row.time], []).append(row)

    # Pre-compute simple returns for the mark-to-market weight drift.
    Pv = P.values.astype(float)
    ret = np.zeros((len(idx), n))
    with np.errstate(all="ignore"):
        ret[1:] = Pv[1:] / Pv[:-1] - 1
    ret = np.nan_to_num(ret, nan=0.0, posinf=0.0, neginf=0.0)

    # Replay research's weight evolution, mirroring run_backtest bar order:
    #   (1) mark-to-market drift  w = w*(1+r)/(1+w@r)
    #   (2) trailing stops (set w->0, cooldown)
    #   (3) rebalance  -> feed post-drift/post-stop w to the bot, then apply.
    w_state = np.zeros(n)
    entry_state = np.full(n, -10 ** 9, dtype=float)
    cool_state = np.full(n, -10 ** 9, dtype=float)
    breaker = -1

    bot_target: dict = {}
    ref_target: dict = {}

    for t in range(len(idx)):
        r = ret[t]
        pr = float(w_state @ r)
        if 1.0 + pr > 0:
            w_state = w_state * (1.0 + r) / (1.0 + pr)

        grp = bar_trades.get(t)
        if grp is None:
            continue

        stops = [x for x in grp if x.reason == "stop"]
        rebals = [x for x in grp if x.reason == "rebal"]

        # (2) trailing stops first (mirrors research.run_backtest bar order)
        for x in stops:
            i = cols.index(x.asset)
            w_state[i] = float(x.w_to)
            cool_state[i] = t + cool_bars
            if float(x.w_to) == 0.0:
                entry_state[i] = -10 ** 9

        # (3a) feed the bot the post-drift/post-stop state and capture its target
        if rebals and t >= 600 and idx[t] in eq_norm.index:
            w_before = w_state.copy()
            entry_before = entry_state.copy()
            cool_before = cool_state.copy()

            sig_row = {k: sig[k].iloc[t].values.astype(float)
                       for k in ("S", "S_slow", "trend", "sig_d")}
            eqv = float(eq_norm.at[idx[t]])
            pos = eq_norm.index.get_loc(idx[t])
            lo = max(0, pos - dd_win + 1)
            win = eq_vals[lo:pos]                  # research window excludes cur
            peak = max(float(np.nanmax(win)) if len(win) else 1.0, eqv)

            tgt, _diag, breaker = compute_target_weights(
                sig_row, w_before, cool_before, entry_before, breaker, eqv,
                peak, lr, np.ones(n, bool), t, cfg, np.ones(n, bool))
            bot_target[idx[t]] = tgt

        # (3b) apply rebalance trades to advance research's state
        for x in rebals:
            i = cols.index(x.asset)
            w_from, w_to = float(x.w_from), float(x.w_to)
            w_state[i] = w_to
            if w_to == 0.0:
                entry_state[i] = -10 ** 9
            elif w_from == 0.0 or w_from * w_to < 0.0:
                entry_state[i] = t
        if rebals:
            ref_target[idx[t]] = w_state.copy()

    common = set(ref_target) & set(bot_target)
    assert common, "no common rebalance bars to compare"

    max_diff = 0.0
    sel_mismatch = 0
    for ts in sorted(common):
        a = ref_target[ts]
        b = bot_target[ts]
        max_diff = max(max_diff, float(np.max(np.abs(a - b))))
        sel_a = set(np.where(a > 1e-9)[0])
        sel_b = set(np.where(b > 1e-9)[0])
        if sel_a != sel_b:
            sel_mismatch += 1
            print("selection mismatch at", ts, "ref", sorted(sel_a), "bot", sorted(sel_b))

    assert sel_mismatch == 0, f"{sel_mismatch} bars with selection mismatch"
    assert max_diff < 1e-6, f"max target-weight diff {max_diff}"
