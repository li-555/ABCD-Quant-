"""Live Roostoo trading client (v3 REST API).

This is the *only* module that talks to the real Roostoo exchange.  Every
endpoint, parameter name, response field, signing rule and pair format below is
taken verbatim from ``docs/ROOSTOO_API.md`` (which itself mirrors the official
``Roostoo-API-Documents`` repository).  Do not guess, add or rename anything
here - if a field is missing from the docs it must be left out and flagged in
the final report.

Auth model (RCL_TopLevelCheck):
  * Header ``RST-API-KEY`` + ``MSG-SIGNATURE`` (HMAC-SHA256, secretKey over the
    sorted ``k=v`` body/query string, hex encoded).
  * A 13-digit millisecond ``timestamp`` is required on every signed call and
    must be within 60s of server time (handled by the base ``SignedMixin``).
  * All string values are passed as-is; booleans (e.g. ``pending_only``) are
    sent as the upper-case strings ``TRUE`` / ``FALSE`` the docs expect.

Only spot 1x LONG trading is used.  The ``/v6/short_*`` endpoints are
deliberately NOT implemented - shorting is forbidden by the competition rules
and by ``Config.allow_short = False``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from bot.config.settings import Config
from bot.execution.client import ExchangeClient, SignedMixin, ApiLogHook


class RoostooClient(SignedMixin, ExchangeClient):
    """Concrete Roostoo client.  See ``docs/Roostoo-API.md`` for the contract."""

    def __init__(self, cfg: Config, log_api: Optional[ApiLogHook] = None,
                 *, read_only: bool = False) -> None:
        self.read_only = read_only
        SignedMixin.__init__(self, cfg, log_api)
        # ExchangeClient is an ABC; SignedMixin provides the plumbing.

    # -- unsigned reference endpoints ---------------------------------------
    def get_exchange_info(self) -> Dict[str, Any]:
        """GET /v3/exchangeInfo (RCL_NoVerification, no signature needed)."""
        return self._request("GET", "/v3/exchangeInfo", {}, signed=False)

    # -- signed read endpoints ----------------------------------------------
    def get_ticker(self, pair: Optional[str] = None) -> Dict[str, Any]:
        """GET /v3/ticker.  Returns ``{"Success", "Data": {pair: {...}}}``.

        ``Data[pair]`` contains ``MaxBid``, ``MinAsk``, ``LastPrice``, ``Change``.
        """
        params: Dict[str, Any] = {"timestamp": self._now_ts()}
        if pair is not None:
            params["pair"] = pair
        return self._request("GET", "/v3/ticker", params, signed=False)

    def get_balance(self) -> Dict[str, Any]:
        """GET /v3/balance.  Returns ``{"Success", "Wallet": {coin: {Free, Lock}}}``."""
        params = {"timestamp": self._now_ts()}
        result = self._request("GET", "/v3/balance", params, signed=True)
        if result.get("Success"):
            wallet = result.get("SpotWallet", result.get("Wallet"))
            if not isinstance(wallet, dict):
                return {"Success": False, "ErrMsg": "Missing spot wallet in balance response"}
            result = {**result, "Wallet": wallet}
        return result

    def get_pending_count(self) -> Dict[str, Any]:
        """GET /v3/pending_count.  Returns ``{"Success", "TotalPending", "OrderPairs"}``."""
        params = {"timestamp": self._now_ts()}
        return self._request("GET", "/v3/pending_count", params, signed=True)

    # -- signed trade endpoints ---------------------------------------------
    def place_order(self, pair: str, side: str, type_: str,
                    quantity: float, price: Optional[float] = None) -> Dict[str, Any]:
        """POST /v3/place_order.

        Parameters (per docs): ``pair`` (``XXX/USD``), ``side`` (``BUY/SELL``),
        ``type`` (``LIMIT/MARKET``), ``quantity`` (STRING), ``timestamp``, and
        ``price`` (DECIMAL) only when ``type=LIMIT``.

        Returns ``{"Success", "OrderDetail": {...}}`` with ``OrderDetail.Status``
        of ``FILLED`` / ``PENDING`` / ``CANCELED``.
        """
        if self.read_only:
            raise RuntimeError("read-only Roostoo client cannot place orders")
        params: Dict[str, Any] = {
            "timestamp": self._now_ts(),
            "pair": str(pair),
            "side": str(side).upper(),
            "type": str(type_).upper(),
            "quantity": _fmt_num(quantity),
        }
        if type_.upper() == "LIMIT":
            if price is None:
                raise ValueError("LIMIT order requires a price")
            params["price"] = _fmt_num(price)
        # No idempotency key is documented. A timeout may already be a fill.
        return self._request("POST", "/v3/place_order", params, signed=True, max_retries=1)

    def query_order(self, order_id: Optional[str] = None,
                    pair: Optional[str] = None,
                    pending_only: Optional[bool] = None) -> Dict[str, Any]:
        """POST /v3/query_order.  Returns ``{"Success", "OrderMatched": [...]}``.

        ``order_id`` and ``pair`` are mutually exclusive; ``pending_only`` is the
        upper-case string ``TRUE`` / ``FALSE``.
        """
        params: Dict[str, Any] = {"timestamp": self._now_ts()}
        if order_id is not None:
            params["order_id"] = str(order_id)
        if pair is not None:
            params["pair"] = str(pair)
        if pending_only is not None:
            params["pending_only"] = "TRUE" if pending_only else "FALSE"
        return self._request("POST", "/v3/query_order", params, signed=True)

    def cancel_order(self, order_id: Optional[str] = None,
                     pair: Optional[str] = None) -> Dict[str, Any]:
        """POST /v3/cancel_order.  Returns ``{"Success", "CanceledList": [...]}``.

        Only pending orders can be cancelled.  ``order_id`` and ``pair`` are
        mutually exclusive (or both omitted to cancel all pending).
        """
        if self.read_only:
            raise RuntimeError("read-only Roostoo client cannot cancel orders")
        params: Dict[str, Any] = {"timestamp": self._now_ts()}
        if order_id is not None:
            params["order_id"] = str(order_id)
        if pair is not None:
            params["pair"] = str(pair)
        return self._request("POST", "/v3/cancel_order", params, signed=True)


def _fmt_num(x: float) -> str:
    """Format a number for the signed body without scientific notation."""
    if x == int(x):
        return str(int(x))
    return f"{x:.10f}".rstrip("0").rstrip(".")
