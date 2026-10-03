"""Execution tests: Roostoo client signing/retry, order manager, paper client.

All network access is mocked (FakeSession); no real API calls.
"""
from __future__ import annotations

import hashlib
import hmac

import numpy as np
import pytest

from bot.config.settings import Config
from bot.config.settings import live_orders_enabled
from bot.execution.roostoo_client import RoostooClient
from bot.execution.paper_client import PaperClient
from bot.execution.order_manager import (
    build_rebalance_intents, build_stop_intents, execute_intent, OrderIntent,
)
from tests.helpers import FakeExchange, RestingExchange, make_exchange_info


# --------------------------------------------------------------------------- #
# mock HTTP session for the live client
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, **kw):
        status, payload = self.handler("GET", url, kw)
        self.calls.append(("GET", url, kw))
        return FakeResponse(status, payload)

    def post(self, url, **kw):
        status, payload = self.handler("POST", url, kw)
        self.calls.append(("POST", url, kw))
        return FakeResponse(status, payload)


def _make_client(cfg):
    client = RoostooClient(cfg)
    return client


def test_roostoo_signing_and_headers():
    cfg = Config()
    cfg.roostoo_api_key = "KEY123"
    cfg.roostoo_api_secret = "SEC456"
    captured = {}

    def handler(method, url, kw):
        if url.endswith("/v3/serverTime"):
            return 200, {"ServerTime": int(__import__("time").time() * 1000)}
        if url.endswith("/v3/place_order"):
            captured["headers"] = kw.get("headers")
            captured["data"] = kw.get("data")
            return 200, {"Success": True, "OrderDetail": {"OrderID": 1,
                      "Status": "FILLED"}}
        return 200, {"Success": True, "Data": {}}

    client = _make_client(cfg)
    client._session = FakeSession(handler)
    client.place_order("BTC/USD", "BUY", "MARKET", 0.01)

    assert captured["headers"]["RST-API-KEY"] == "KEY123"
    sig = captured["headers"]["MSG-SIGNATURE"]
    expected = hmac.new(cfg.roostoo_api_secret.encode(), captured["data"].encode(),
                        hashlib.sha256).hexdigest()
    assert sig == expected
    # body is the sorted k=v string used for signing
    assert "timestamp=" in captured["data"] and "pair=BTC/USD" in captured["data"]


def test_roostoo_success_false_returned():
    cfg = Config()
    cfg.roostoo_api_key = "K"
    cfg.roostoo_api_secret = "S"

    def handler(method, url, kw):
        if url.endswith("/v3/serverTime"):
            return 200, {"ServerTime": int(__import__("time").time() * 1000)}
        return 200, {"Success": False, "ErrMsg": "rejected"}

    client = _make_client(cfg)
    client._session = FakeSession(handler)
    resp = client.place_order("BTC/USD", "BUY", "MARKET", 0.01)
    assert resp.get("Success") is False


def test_roostoo_request_error_never_leaks_secret():
    cfg = Config()
    cfg.roostoo_api_key = "K"
    cfg.roostoo_api_secret = "VERY_SECRET_VALUE"

    def handler(method, url, kw):
        if url.endswith("/v3/serverTime"):
            return 200, {"ServerTime": int(__import__("time").time() * 1000)}
        raise RuntimeError("boom VERY_SECRET_VALUE")

    client = _make_client(cfg)
    client._session = FakeSession(handler)
    resp = client.place_order("BTC/USD", "BUY", "MARKET", 0.01)
    assert resp.get("Success") is False
    assert "VERY_SECRET_VALUE" not in resp.get("ErrMsg", "")


def test_live_orders_two_step_gate():
    cfg = Config(live=True, roostoo_live_trading=True,
                 roostoo_live_confirm="I_UNDERSTAND_AND_ACCEPT_LIVE_TRADING_RISK")
    assert live_orders_enabled(cfg) is True
    cfg.roostoo_live_confirm = "WRONG"
    assert live_orders_enabled(cfg) is False


def test_roostoo_read_retries_then_succeeds():
    cfg = Config()
    cfg.roostoo_api_key = "K"
    cfg.roostoo_api_secret = "S"
    attempts = {"n": 0}

    def handler(method, url, kw):
        if url.endswith("/v3/serverTime"):
            return 200, {"ServerTime": int(__import__("time").time() * 1000)}
        attempts["n"] += 1
        if attempts["n"] < 3:
            return 200, {"Success": False, "ErrMsg": "retry me"}
        return 200, {"Success": True, "OrderDetail": {"OrderID": 9,
                      "Status": "FILLED"}}

    client = _make_client(cfg)
    client._session = FakeSession(handler)
    resp = client.query_order(pair="BTC/USD")
    assert resp.get("Success") is True
    assert attempts["n"] == 3


# --------------------------------------------------------------------------- #
# order manager
# --------------------------------------------------------------------------- #
def test_build_rebalance_intents_sell_before_buy():
    pairs = ["BTC/USD", "ETH/USD"]
    w = np.array([0.3, 0.0])
    tgt = np.array([0.1, 0.2])   # BTC reduce, ETH buy
    intents = build_rebalance_intents(pairs, w, tgt, equity=100_000, min_trade=0.005)
    assert intents[0].pair == "BTC/USD" and intents[0].side == "SELL"
    assert intents[1].pair == "ETH/USD" and intents[1].side == "BUY"


def test_build_rebalance_intents_skips_small():
    pairs = ["BTC/USD"]
    w = np.array([0.100])
    tgt = np.array([0.103])   # diff 0.003 < min_trade 0.005
    assert build_rebalance_intents(pairs, w, tgt, equity=100_000,
                                   min_trade=0.005) == []


def test_execute_rounding_and_min_order():
    cfg = Config()
    cfg.max_order_frac = 0.15
    ex = FakeExchange(["BTC/USD"], {"BTC/USD": 100.0})
    info = ex.get_exchange_info()
    prices = {"BTC/USD": 100.0}
    ticker = ex.get_ticker()["Data"]
    # tiny notional below MiniOrder (1 USD) -> skipped
    intent = OrderIntent("BTC/USD", "BUY", 0.5, aggressive=False, reason="rebalance")
    out = execute_intent(ex, intent, prices, ticker, info, cfg, equity=100_000)
    assert any(o.get("skipped") for o in out["orders"])


def test_execute_chunks_large_order():
    cfg = Config()
    cfg.max_order_frac = 0.15
    ex = FakeExchange(["BTC/USD"], {"BTC/USD": 100.0})
    info = ex.get_exchange_info()
    prices = {"BTC/USD": 100.0}
    ticker = ex.get_ticker()["Data"]
    # 0.35 * 100000 = 35000 USD -> 3 chunks of ~15000
    intent = OrderIntent("BTC/USD", "BUY", 35000.0, aggressive=False,
                         reason="rebalance")
    out = execute_intent(ex, intent, prices, ticker, info, cfg, equity=100_000)
    assert len(ex.orders) == 3
    assert out["filled_qty"] > 0


def test_execute_limit_then_market_fallback(monkeypatch):
    import bot.execution.order_manager as om
    monkeypatch.setattr(om, "LIMIT_GRACE_SECONDS", 0.0)
    cfg = Config()
    ex = RestingExchange(["BTC/USD"], {"BTC/USD": 100.0})
    info = ex.get_exchange_info()
    prices = {"BTC/USD": 100.0}
    ticker = ex.get_ticker()["Data"]
    intent = OrderIntent("BTC/USD", "BUY", 1000.0, aggressive=False,
                         reason="rebalance")
    out = execute_intent(ex, intent, prices, ticker, info, cfg, equity=100_000)
    # 1 LIMIT (rested) + 1 MARKET (after cancel) -> 2 place_order calls
    assert len(ex.orders) == 2
    assert out["filled_qty"] > 0


def test_build_stop_intents():
    pairs = ["BTC/USD", "ETH/USD"]
    w = np.array([0.2, 0.1])
    hit = np.array([True, False])
    intents = build_stop_intents(pairs, w, equity=100_000, hit_mask=hit)
    assert len(intents) == 1
    assert intents[0].pair == "BTC/USD" and intents[0].side == "SELL"
    assert intents[0].aggressive is True


# --------------------------------------------------------------------------- #
# paper client
# --------------------------------------------------------------------------- #
def test_paper_client_fills_and_balance():
    cfg = Config()
    paper = PaperClient(cfg, pairs=["BTC/USD", "ETH/USD"])
    paper.set_market_prices({"BTC/USD": 100.0, "ETH/USD": 50.0})
    r = paper.place_order("BTC/USD", "BUY", "MARKET", 1.0)
    assert r["Success"] and r["OrderDetail"]["Status"] == "FILLED"
    bal = paper.get_balance()["Wallet"]
    assert abs(bal["BTC"]["Free"] - 1.0) < 1e-9
    assert bal["USD"]["Free"] < cfg.paper_start_equity
    # sell back
    r2 = paper.place_order("BTC/USD", "SELL", "MARKET", 1.0)
    assert r2["OrderDetail"]["Status"] == "FILLED"
    assert abs(paper.get_balance()["Wallet"].get("BTC", {"Free": 0.0})["Free"]) < 1e-9
