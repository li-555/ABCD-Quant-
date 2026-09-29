"""Offline simulated Roostoo exchange for dry-run and tests.

``PaperClient`` implements the same :class:`~bot.execution.client.ExchangeClient`
interface as the live client, but keeps its own book in memory:

  * ``cash``  - USD balance (starts at ``paper_start_equity``)
  * ``positions`` - coin -> quantity held (Free == quantity, Lock == 0)
  * ``prices`` - pair -> last price, refreshed each cycle by the bot via
    :meth:`set_market_prices` (the bot never reads the sealed exchange for paper
    mode, but the *interface* is identical so the scheduler code is unchanged)

Market orders fill immediately at the last known price.  Limit orders are
*rested* (returned as ``PENDING``) so the order manager's limit-then-market
fallback path is genuinely exercised; after the bot cancels them it re-sends a
MARKET order that fills.  This is the behaviour we want to test, and it keeps the
simulation honest (a resting maker does not fill until the bot acts).

Fees use the same taker/maker bps from ``Config`` so paper equity tracks the
real cost model.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from bot.config.settings import Config
from bot.execution.client import ExchangeClient, ApiLogHook


def _paper_exchange_info(pairs: List[str], price_prec: int = 2,
                         amount_prec: int = 6, mini_order: float = 1.0) -> Dict[str, Any]:
    trade_pairs = {}
    for p in pairs:
        coin = p.split("/")[0]
        trade_pairs[p] = {
            "Coin": coin,
            "CoinFullName": coin,
            "Unit": "USD",
            "UnitFullName": "US Dollar",
            "CanTrade": True,
            "PricePrecision": price_prec,
            "AmountPrecision": amount_prec,
            "MiniOrder": mini_order,
        }
    return {"IsRunning": True, "InitialWallet": {"USD": 100_000},
            "TradePairs": trade_pairs}


class PaperClient(ExchangeClient):
    """In-memory spot exchange used when ``LIVE=0`` (the default)."""

    def __init__(self, cfg: Config, pairs: Optional[List[str]] = None,
                 log_api: Optional[ApiLogHook] = None) -> None:
        self.cfg = cfg
        self._log_api = log_api
        self.cash: float = float(cfg.paper_start_equity)
        self.positions: Dict[str, float] = {}   # coin -> quantity (Free)
        self.prices: Dict[str, float] = {}       # pair -> last price
        self._pairs = list(pairs or [])
        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}

    # -- test / scheduler hook ----------------------------------------------
    def set_market_prices(self, prices: Dict[str, float]) -> None:
        """Update last prices (called by the scheduler each cycle in paper mode)."""
        self.prices.update(prices)

    # -- interface ----------------------------------------------------------
    def get_exchange_info(self) -> Dict[str, Any]:
        return _paper_exchange_info(self._pairs,
                                    price_prec=2, amount_prec=6, mini_order=1.0)

    def get_ticker(self, pair: Optional[str] = None) -> Dict[str, Any]:
        data = {}
        for p, price in self.prices.items():
            if pair is not None and p != pair:
                continue
            data[p] = {"MaxBid": price * 0.999, "MinAsk": price * 1.001,
                       "LastPrice": price, "Change": 0.0,
                       "CoinTradeValue": 0.0, "UnitTradeValue": 0.0}
        return {"Success": True, "ErrMsg": "", "ServerTime": 0, "Data": data}

    def get_balance(self) -> Dict[str, Any]:
        wallet: Dict[str, Any] = {"USD": {"Free": self.cash, "Lock": 0.0}}
        for coin, qty in self.positions.items():
            if qty:
                wallet[coin] = {"Free": qty, "Lock": 0.0}
        return {"Success": True, "ErrMsg": "", "Wallet": wallet}

    def get_pending_count(self) -> Dict[str, Any]:
        pairs_count: Dict[str, int] = {}
        for o in self._pending.values():
            pairs_count[o["Pair"]] = pairs_count.get(o["Pair"], 0) + 1
        return {"Success": True, "ErrMsg": "", "TotalPending": len(self._pending),
                "OrderPairs": pairs_count}

    def place_order(self, pair: str, side: str, type_: str,
                    quantity: float, price: Optional[float] = None) -> Dict[str, Any]:
        side = side.upper()
        type_ = type_.upper()
        if quantity <= 0:
            return {"Success": False, "ErrMsg": "non-positive quantity", "OrderDetail": {}}
        oid = self._next_id
        self._next_id += 1
        if type_ == "MARKET":
            return self._fill(oid, pair, side, type_, quantity, price=None)
        # LIMIT: rest it as PENDING so the limit->market fallback is exercised.
        if price is None:
            return {"Success": False, "ErrMsg": "LIMIT requires price", "OrderDetail": {}}
        self._pending[oid] = {
            "Pair": pair, "OrderID": oid, "Side": side, "Type": type_,
            "Price": float(price), "Quantity": float(quantity), "Status": "PENDING",
        }
        return {"Success": True, "ErrMsg": "", "OrderDetail": dict(self._pending[oid])}

    def query_order(self, order_id: Optional[str] = None,
                    pair: Optional[str] = None,
                    pending_only: Optional[bool] = None) -> Dict[str, Any]:
        matched: List[Dict[str, Any]] = []
        for o in self._pending.values():
            if order_id is not None and str(o["OrderID"]) != str(order_id):
                continue
            if pair is not None and o["Pair"] != pair:
                continue
            matched.append(dict(o))
        if pending_only:
            matched = [o for o in matched if o.get("Status") == "PENDING"]
        if not matched:
            return {"Success": False, "ErrMsg": "no order matched"}
        return {"Success": True, "ErrMsg": "", "OrderMatched": matched}

    def cancel_order(self, order_id: Optional[str] = None,
                     pair: Optional[str] = None) -> Dict[str, Any]:
        canceled: List[int] = []
        for oid in list(self._pending.keys()):
            o = self._pending[oid]
            if order_id is not None and str(o["OrderID"]) != str(order_id):
                continue
            if pair is not None and o["Pair"] != pair:
                continue
            canceled.append(oid)
            del self._pending[oid]
        return {"Success": True, "ErrMsg": "", "CanceledList": canceled}

    # -- fill engine ---------------------------------------------------------
    def _fill(self, oid: int, pair: str, side: str, type_: str, quantity: float,
              price: Optional[float]) -> Dict[str, Any]:
        last = self.prices.get(pair)
        if last is None or last <= 0:
            return {"Success": False, "ErrMsg": f"no price for {pair}", "OrderDetail": {}}
        fill_price = float(price) if (price is not None and type_ == "LIMIT") else last
        notional = quantity * fill_price
        bps = self.cfg.maker_bps if type_ == "LIMIT" else self.cfg.taker_bps
        fee = notional * bps / 1e4
        coin = pair.split("/")[0]
        if side == "BUY":
            self.cash -= notional + fee
            self.positions[coin] = self.positions.get(coin, 0.0) + quantity
        else:  # SELL
            self.cash += notional - fee
            self.positions[coin] = self.positions.get(coin, 0.0) - quantity
        detail = {
            "Pair": pair, "OrderID": oid, "Status": "FILLED",
            "Role": "MAKER" if type_ == "LIMIT" else "TAKER",
            "ServerTimeUsage": 0.0, "CreateTimestamp": 0, "FinishTimestamp": 0,
            "Side": side, "Type": type_, "StopType": "GTC",
            "Price": fill_price, "Quantity": quantity,
            "FilledQuantity": quantity, "FilledAverPrice": fill_price,
            "CoinChange": quantity, "UnitChange": notional,
            "CommissionCoin": "USD", "CommissionChargeValue": fee,
            "CommissionPercent": bps / 1e4,
        }
        if self._log_api:
            self._log_api(pair, 200, 0.0, True)
        return {"Success": True, "ErrMsg": "", "OrderDetail": detail}
