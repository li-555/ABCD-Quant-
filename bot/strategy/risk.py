"""Risk helpers: trailing stops and extreme-price tracking.

The trailing stop follows ``research.backtest.run_backtest`` exactly (long side
only): for each open long position the running extreme ``ext`` is updated to the
maximum close seen, and the position is flagged for liquidation when the price
drops below ``ext * (1 - stop_long * sigma_d)``, where ``sigma_d`` is the daily
volatility estimate.  The caller performs the actual liquidation + cooldown
because that step emits orders.

These functions are pure (no I/O, no side effects) so they can be unit tested
against the research backtester.
"""
from __future__ import annotations

import numpy as np

from bot.config.settings import Config


def update_extremes(ext: np.ndarray, prices: np.ndarray,
                    held_mask: np.ndarray) -> np.ndarray:
    """Update the running extreme price for currently-held positions.

    ``ext`` is only ever increased, using the latest closed-bar close.
    """
    new_ext = ext.copy()
    m = held_mask & np.isfinite(prices)
    new_ext[m] = np.maximum(new_ext[m], prices[m])
    return new_ext


def stop_hits(w: np.ndarray, prices: np.ndarray, ext: np.ndarray,
              sig_d: np.ndarray, cfg: Config) -> np.ndarray:
    """Return a boolean mask of long positions that breached their trailing stop.

    A long position at index ``i`` is stopped when
    ``price[i] <= ext[i] * (1 - stop_long * sig_d[i])``.
    """
    held = w > 0
    valid = (held & np.isfinite(prices) & np.isfinite(ext) &
             np.isfinite(sig_d) & (ext > 0))
    hit = valid & (prices <= ext * (1.0 - cfg.stop_long * sig_d))
    return hit


def peak_update(peak: float, equity: float) -> float:
    """Update the trailing peak equity (max of prior peak and current equity)."""
    return max(peak, equity)
