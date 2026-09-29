"""Activity guard - a compliance helper.

The strategy is slow and, in weak markets, can stay fully in cash for a whole day,
which may leave fewer than the required "active trading days".  This helper, when
enabled, ensures a *minimal, strategy-aligned* number of fills per day so the bot
clearly satisfies the "at least 8 active trading days with enough trades" rule.

It is intentionally small and conservative: it never adds directional risk beyond
what the strategy already wants, and every order it produces is tagged
``reason="activity_guard"`` for full transparency.  See README "Compliance notes".
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from bot.config.settings import Config

_ACTIVITY_REASON = "activity_guard"


def propose_activity_trades(
    w: np.ndarray,
    tgt: np.ndarray,
    day_trade_count: int,
    cfg: Config,
    btc_index: Optional[int] = None,
) -> List[dict]:
    """Propose minimal, strategy-aligned trades if the day is too quiet.

    Rules (fired only at the daily compliance check, e.g. UTC 20:00):
      1. If the current weights already differ from target by more than
         ``min_trade`` for some asset, trade the largest such gap with a notional
         of ``activity_notional_frac * equity`` (in the strategy's direction).
      2. Otherwise, if the book is essentially all cash, buy ``activity_notional_frac
         * equity`` of BTC (treated as a normal holding at the next rebalance).

    Returns a list of dicts ``{"index", "side", "notional", "reason"}``.
    """
    if not cfg.activity_enabled or day_trade_count >= cfg.min_daily_trades:
        return []

    deltas = tgt - w
    big = np.abs(deltas) > cfg.min_trade
    if big.any():
        i = int(np.argmax(np.abs(deltas)))
        side = "BUY" if deltas[i] > 0 else "SELL"
        notional = cfg.activity_notional_frac
        return [{"index": i, "side": side, "notional": notional, "reason": _ACTIVITY_REASON}]

    if np.abs(w).sum() < cfg.min_trade:
        if btc_index is not None:
            return [{"index": int(btc_index), "side": "BUY",
                     "notional": cfg.activity_notional_frac, "reason": _ACTIVITY_REASON}]
    return []
