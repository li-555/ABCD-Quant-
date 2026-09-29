"""Shared offline helpers for the bot test-suite (no network, no secrets)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from bot.config.settings import Config
from bot.execution.client import ExchangeClient


def synth_prices(bar_min: int = 30, days: int = 60, seed: int = 7):
    """Generate a synthetic crypto price grid via research.backtest.make_synthetic.

    Returns (P, real) where P is a close-indexed DataFrame (columns = coins) and
    real is an all-True DataFrame of the same shape (every coin tradable).
    """
    from research import backtest as rb
    P = rb.make_synthetic(bar_min, days, "crypto", seed=seed)
    real = pd.DataFrame(True, index=P.index, columns=P.columns)
    return P, real


def bot_cfg(tmp, live: bool = False) -> Config:
    """A Config that writes everything under ``tmp`` (logs/state/cache)."""
    cfg = Config()
    cfg.log_dir = str(tmp / "logs")
    cfg.state_file = str(tmp / "state.json")
    cfg.data_dir = str(tmp / "cache")
    cfg.heartbeat_file = str(tmp / "heartbeat.json")
    cfg.equity_csv = str(tmp / "logs" / "equity.csv")
    cfg.kill_file = str(tmp / "KILL")
    cfg.live = live
    return cfg


def universe_from_cols(cols) -> List[Dict[str, str]]:
    return [{"coin": c, "binance_symbol": f"{c}USDT",
             "roostoo_pair": f"{c}/USD"} for c in cols]


def make_exchange_info(pairs: List[str]) -> Dict[str, Any]:
    trade_pairs = {}
    for p in pairs:
        coin = p.split("/")[0]
        trade_pairs[p] = {
            "Coin": coin, "CoinFullName": coin, "Unit": "USD",
            "UnitFullName": "US Dollar", "CanTrade": True,
            "PricePrecision": 2, "AmountPrecision": 6, "MiniOrder": 1.0,
        }
    return {"IsRunning": True, "InitialWallet": {"USD": 100_000},
            "TradePairs": trade_pairs}


class FakeExchange(ExchangeClient):
    """In-memory exchange for scheduler / order-manager tests.

    Market and LIMIT orders both fill immediately at the last known price; this
    keeps scheduler tests deterministic.  (The limit->market fallback is tested
    separately with a ``RestingExchange``.)
    """

    def __init__(self, pairs: List[str], prices: Dict[str, float],
                 cash: float = 100_000.0) -> None:
        self.pairs = pairs
        self.prices = dict(prices)
        self.cash = float(cash)
        self.positions: Dict[str, float] = {}
        self.orders: List[Dict[str, Any]] = []
        self._next = 1

    def set_market_prices(self, prices: Dict[str, float]) -> None:
        self.prices.update(prices)

    def get_exchange_info(self) -> Dict[str, Any]:
        return make_exchange_info(self.pairs)

    def get_ticker(self, pair: Optional[str] = None) -> Dict[str, Any]:
        data = {p: {"MaxBid": self.prices[p] * 0.999,
                    "MinAsk": self.prices[p] * 1.001,
                    "LastPrice": self.prices[p], "Change": 0.0}
                 for p in self.pairs if p in self.prices}
        if pair is not None:
            data = {k: v for k, v in data.items() if k == pair}
        return {"Success": True, "ErrMsg": "", "ServerTime": 0, "Data": data}

    def get_balance(self) -> Dict[str, Any]:
        wallet = {"USD": {"Free": self.cash, "Lock": 0.0}}
        for coin, q in self.positions.items():
            if q:
                wallet[coin] = {"Free": q, "Lock": 0.0}
        return {"Success": True, "ErrMsg": "", "Wallet": wallet}

    def get_pending_count(self) -> Dict[str, Any]:
        return {"Success": True, "TotalPending": 0, "OrderPairs": {}}

    def place_order(self, pair, side, type_, quantity, price=None):
        side = side.upper()
        last = self.prices.get(pair)
        if last is None or quantity <= 0:
            return {"Success": False, "ErrMsg": "bad", "OrderDetail": {}}
        notional = quantity * last
        bps = 5.0 if type_.upper() == "LIMIT" else 10.0
        fee = notional * bps / 1e4
        coin = pair.split("/")[0]
        if side == "BUY":
            self.cash -= notional + fee
            self.positions[coin] = self.positions.get(coin, 0.0) + quantity
        else:
            self.cash += notional - fee
            self.positions[coin] = self.positions.get(coin, 0.0) - quantity
        oid = self._next
        self._next += 1
        self.orders.append({"pair": pair, "side": side, "type": type_,
                             "qty": quantity})
        return {"Success": True, "ErrMsg": "", "OrderDetail": {
            "Pair": pair, "OrderID": oid, "Status": "FILLED",
            "Role": "MAKER" if type_.upper() == "LIMIT" else "TAKER",
            "Side": side, "Type": type_, "Price": last, "Quantity": quantity,
            "FilledQuantity": quantity, "FilledAverPrice": last,
            "CoinChange": quantity, "UnitChange": notional,
            "CommissionCoin": "USD", "CommissionChargeValue": fee,
            "CommissionPercent": bps / 1e4}}

    def query_order(self, order_id=None, pair=None, pending_only=None):
        return {"Success": False, "ErrMsg": "no order matched"}

    def cancel_order(self, order_id=None, pair=None):
        return {"Success": True, "CanceledList": []}


class RestingExchange(FakeExchange):
    """LIMIT orders rest as PENDING; cancel + MARKET then fills.

    Used to exercise the order manager's limit-then-market fallback.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._pending_ids = []

    def place_order(self, pair, side, type_, quantity, price=None):
        if type_.upper() == "LIMIT":
            oid = self._next
            self._next += 1
            self._pending_ids.append(oid)
            # record the rested order so tests can count both place_order calls
            self.orders.append({"pair": pair, "side": side.upper(),
                                "type": "LIMIT", "qty": quantity})
            return {"Success": True, "ErrMsg": "", "OrderDetail": {
                "Pair": pair, "OrderID": oid, "Status": "PENDING",
                "Role": "MAKER", "Side": side, "Type": "LIMIT", "Price": price,
                "Quantity": quantity, "FilledQuantity": 0,
                "FilledAverPrice": 0, "CoinChange": 0, "UnitChange": 0,
                "CommissionCoin": "USD", "CommissionChargeValue": 0,
                "CommissionPercent": 0.0}}
        return super().place_order(pair, side, type_, quantity, price)

    def query_order(self, order_id=None, pair=None, pending_only=None):
        if self._pending_ids:
            matched = [{"Pair": self.pairs[0], "OrderID": i, "Status": "PENDING"}
                       for i in self._pending_ids]
            return {"Success": True, "ErrMsg": "", "OrderMatched": matched}
        return {"Success": False, "ErrMsg": "no order matched"}

    def cancel_order(self, order_id=None, pair=None):
        canceled = list(self._pending_ids)
        self._pending_ids.clear()
        return {"Success": True, "ErrMsg": "", "CanceledList": canceled}
