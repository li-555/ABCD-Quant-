"""Translate target weights into real orders and execute them.

Two responsibilities, kept separate so the pure part is trivially testable:

* :func:`build_rebalance_intents` / :func:`build_stop_intents` - pure functions
  that turn the strategy's ``(w, tgt)`` vectors into a list of
  :class:`OrderIntent` objects (sells first, then buys; stops are aggressive
  MARKET orders).  No I/O.
* :func:`execute_intent` - performs one intent against an
  :class:`~bot.execution.client.ExchangeClient`: rounds the quantity to the
  pair's ``AmountPrecision``, enforces ``MiniOrder`` and the per-order notional
  cap (``max_order_frac``) by chunking, and for non-aggressive intents uses a
  LIMIT (maker) order with a short in-cycle grace, falling back to MARKET if it
  has not filled.

Execution rules (per CODING_AGENT_PROMPT / docs/Roostoo-API.md):
  * Sell before buy (free cash first).
  * Entries / additions -> LIMIT (lower fee), resting at the bid (BUY) or ask
    (SELL); if unfilled after the grace window, cancel and cross with MARKET.
  * Stops / circuit-breaker exits -> MARKET immediately.
  * Every request goes through the client's rate limiter / retry (base class).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from bot.config.settings import Config
from bot.execution.client import ExchangeClient

# In-cycle grace (seconds) we wait for a maker LIMIT to fill before we cancel
# and cross with a MARKET order.  The docs allow up to `limit_timeout_minutes`;
# blocking the 30-minute loop for 10 minutes is undesirable, so we use a short
# grace and convert to MARKET, which still guarantees execution.  See README.
LIMIT_GRACE_SECONDS = 2.0

OrderLogHook = Callable[[Dict[str, Any]], None]


@dataclass
class OrderIntent:
    """A desired trade, produced by the pure builders below."""
    pair: str
    side: str            # "BUY" / "SELL"
    notional_usd: float  # target notional in USD
    aggressive: bool     # True -> MARKET (stops / breaker), False -> LIMIT then MARKET
    reason: str          # "rebalance" / "stop" / "activity_guard" / ...


def build_rebalance_intents(pairs: List[str], w: np.ndarray, tgt: np.ndarray,
                            equity: float, min_trade: float) -> List[OrderIntent]:
    """Pure: diff ``tgt`` vs ``w`` into ordered rebalance intents.

    Sells come first (free cash before buys).  Positions whose target weight
    differs from the current weight by less than ``min_trade`` are skipped.
    """
    intents: List[OrderIntent] = []
    n = len(pairs)
    deltas = [(tgt[i] - w[i]) * equity for i in range(n)]
    for i in range(n):
        if abs(tgt[i] - w[i]) < min_trade:
            continue
        side = "BUY" if deltas[i] > 0 else "SELL"
        intents.append(OrderIntent(pairs[i], side, abs(deltas[i]),
                                   aggressive=False, reason="rebalance"))
    # sells first then buys
    intents.sort(key=lambda it: it.side != "SELL")
    return intents


def build_stop_intents(pairs: List[str], w: np.ndarray, equity: float,
                       hit_mask: np.ndarray) -> List[OrderIntent]:
    """Pure: liquidate every position flagged by the trailing-stop check."""
    intents: List[OrderIntent] = []
    for i in np.where(hit_mask)[0]:
        notional = abs(w[i]) * equity
        if notional <= 0:
            continue
        intents.append(OrderIntent(pairs[i], "SELL", notional,
                                   aggressive=True, reason="stop"))
    return intents


def _round_qty(notional: float, price: float, amount_prec: int) -> float:
    qty = notional / price
    return round(qty, amount_prec)


def _limit_price(side: str, ticker: Optional[Dict[str, Any]], last: float,
                 price_prec: int) -> float:
    bid = (ticker or {}).get("MaxBid")
    ask = (ticker or {}).get("MinAsk")
    if side == "BUY":
        ref = bid if bid is not None else last
    else:
        ref = ask if ask is not None else last
    if ref is None or ref <= 0:
        ref = last
    return round(ref, price_prec)


def execute_intent(client: ExchangeClient, intent: OrderIntent,
                   prices: Dict[str, float], ticker: Dict[str, Dict[str, Any]],
                   exchange_info: Dict[str, Any], cfg: Config, equity: float,
                   log_order: Optional[OrderLogHook] = None) -> Dict[str, Any]:
    """Execute one :class:`OrderIntent` against ``client``.

    Returns a summary dict (always present, even on skip/failure) and invokes
    ``log_order`` once per placed order for the ``orders.jsonl`` audit trail.
    """
    pair = intent.pair
    info = (exchange_info.get("TradePairs") or {}).get(pair)
    if info is None:
        return {"ok": False, "pair": pair, "err": "pair not in exchange_info"}
    amount_prec = int(info.get("AmountPrecision", 6))
    price_prec = int(info.get("PricePrecision", 2))
    mini_order = float(info.get("MiniOrder", 1.0))
    last = prices.get(pair)
    if last is None or last <= 0:
        return {"ok": False, "pair": pair, "err": "no valid price"}

    max_chunk = max(cfg.max_order_frac * equity, mini_order)
    n_chunks = max(1, math.ceil(intent.notional_usd / max_chunk)) if max_chunk > 0 else 1
    chunks = max(1, n_chunks)
    per = intent.notional_usd / chunks

    summary: Dict[str, Any] = {"ok": True, "pair": pair, "side": intent.side,
                               "reason": intent.reason, "orders": []}
    filled_qty = 0.0
    filled_notional = 0.0
    commission = 0.0
    for _ in range(chunks):
        qty = _round_qty(per, last, amount_prec)
        if qty <= 0:
            continue
        notional = qty * last
        if notional < mini_order:
            summary["orders"].append({"skipped": True, "err": "below MiniOrder"})
            continue

        if intent.aggressive:
            resp = client.place_order(pair, intent.side, "MARKET", qty)
        else:
            lim = _limit_price(intent.side, ticker.get(pair), last, price_prec)
            resp = client.place_order(pair, intent.side, "LIMIT", qty, lim)
            detail = (resp.get("OrderDetail") or {}) if resp.get("Success") else {}
            if detail.get("Status") == "PENDING":
                # grace window, then cancel + cross with MARKET
                time.sleep(LIMIT_GRACE_SECONDS)
                oid = detail.get("OrderID")
                if oid is not None:
                    client.cancel_order(order_id=oid)
                resp = client.place_order(pair, intent.side, "MARKET", qty)
        _record_order(resp, intent, qty, log_order)
        detail = resp.get("OrderDetail") or {}
        fq = float(detail.get("FilledQuantity") or 0.0)
        fp = float(detail.get("FilledAverPrice") or 0.0)
        filled_qty += fq
        filled_notional += fq * fp
        commission += float(detail.get("CommissionChargeValue") or 0.0)
        summary["orders"].append({"success": bool(resp.get("Success")),
                                   "status": detail.get("Status"),
                                   "order_id": detail.get("OrderID")})

    summary["filled_qty"] = filled_qty
    summary["filled_notional"] = filled_notional
    summary["commission"] = commission
    return summary


def _record_order(resp: Dict[str, Any], intent: OrderIntent, qty: float,
                  log_order: Optional[OrderLogHook]) -> None:
    if log_order is None:
        return
    detail = resp.get("OrderDetail") or {}
    log_order({
        "pair": intent.pair,
        "side": intent.side,
        "reason": intent.reason,
        "requested_qty": qty,
        "success": bool(resp.get("Success")),
        "err": resp.get("ErrMsg"),
        "order_id": detail.get("OrderID"),
        "status": detail.get("Status"),
        "role": detail.get("Role"),
        "fill_price": detail.get("FilledAverPrice"),
        "filled_qty": detail.get("FilledQuantity"),
        "commission": detail.get("CommissionChargeValue"),
    })
