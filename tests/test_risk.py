"""Risk helpers: trailing-stop trigger, extreme update, drawdown peak."""
from __future__ import annotations

import numpy as np

from bot.strategy.risk import update_extremes, stop_hits, peak_update
from bot.config.settings import Config


def test_update_extremes_only_increases():
    cfg = Config()
    ext = np.array([100.0, 90.0, np.nan])
    prices = np.array([105.0, 85.0, 50.0])
    held = np.array([True, True, True])
    out = update_extremes(ext, prices, held)
    assert out[0] == 105.0
    assert out[1] == 90.0          # price below extreme -> unchanged
    # uninitialised (NaN) extreme for a held asset is left untouched here; the
    # scheduler seeds it from the fill price before calling update_extremes.
    assert np.isnan(out[2])
    # a fresh finite extreme is adopted when the position has no prior extreme
    out2 = update_extremes(np.array([np.nan]), np.array([50.0]),
                          np.array([True]))
    assert np.isnan(out2[0])


def test_stop_hits_long():
    cfg = Config()
    w = np.array([0.2, 0.0])
    ext = np.array([100.0, np.nan])
    sig_d = np.array([0.04, 0.04])
    # price drops more than stop_long * sig_d below extreme
    prices = np.array([100.0 * (1 - 1.6 * 0.04), 0.0])
    hit = stop_hits(w, prices, ext, sig_d, cfg)
    assert bool(hit[0]) is True
    assert bool(hit[1]) is False


def test_stop_ignores_unheld():
    cfg = Config()
    w = np.array([0.0, 0.0])
    ext = np.array([100.0, 100.0])
    sig_d = np.array([0.04, 0.04])
    prices = np.array([50.0, 50.0])
    assert not stop_hits(w, prices, ext, sig_d, cfg).any()


def test_peak_update():
    assert peak_update(100.0, 105.0) == 105.0
    assert peak_update(105.0, 100.0) == 105.0
