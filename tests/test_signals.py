"""Signal parity: bot.strategy.signals must equal research.backtest.build_signals.

This is the single most important correctness check (CODING_AGENT_PROMPT 7.1).
We generate synthetic crypto data with the research project's own
``make_synthetic``, compute signals both with the research engine (variant A:
slow layer only, no fast layer) and with the bot, and assert the four output
frames (S, S_slow, trend, sig_d) are numerically identical.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research import backtest as rb
from bot.strategy.signals import build_signals
from bot.config.settings import Config
from tests.helpers import synth_prices


def _research_signals(P, real):
    prm = rb.Params(bar_min=30, use_fast=False, allow_short=False)
    return rb.build_signals(P, real, prm)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_signal_parity_allclose_1e9(seed):
    P, real = synth_prices(bar_min=30, days=60, seed=seed)
    cfg = Config()
    bot_sig = build_signals(P, cfg)
    ref_sig = _research_signals(P, real)

    for key in ("S", "S_slow", "trend", "sig_d"):
        a = bot_sig[key].to_numpy(dtype=float)
        b = ref_sig[key].to_numpy(dtype=float)
        # compare only where the reference is finite (NaN positions are allowed
        # to differ in exact NaN representation, but finite values must match).
        mask = np.isfinite(b)
        assert np.allclose(a[mask], b[mask], atol=1e-9, rtol=0), \
            f"{key} differs beyond 1e-9"
        # and the bot must be NaN exactly where the reference is NaN
        assert np.array_equal(np.isfinite(a), np.isfinite(b)), \
            f"{key} finiteness differs"


def test_variant_a_is_slow_only():
    """Variant A: S must equal S_slow (no fast layer, no reversal term)."""
    P, real = synth_prices(seed=7)
    cfg = Config()
    bot_sig = build_signals(P, cfg)
    # .equals treats NaN == NaN as equal, unlike np.allclose.
    assert bot_sig["S"].equals(bot_sig["S_slow"])
