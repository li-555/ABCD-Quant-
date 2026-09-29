"""Exchange client abstraction and the signed-request base.

Defines :class:`ExchangeClient`, the interface the rest of the bot programs
against (so the live Roostoo client and the offline paper client are
interchangeable), and :class:`SignedMixin` which implements HMAC-SHA256 signing,
server-time synchronisation, retry/back-off and rate limiting exactly as the
Roostoo docs require.

API facts used here (from docs/Roostoo-API-Documents):
  * Base URL ``https://mock-api.roostoo.com``.
  * Auth ``RCL_TopLevelCheck``: header ``RST-API-KEY`` + ``MSG-SIGNATURE``.
  * Signature = HMAC-SHA256(secret, sorted ``k=v`` joined by ``&``), hex.
  * POST body is the raw sorted string, ``Content-Type:
    application/x-www-form-urlencoded``.
  * A 13-digit millisecond ``timestamp`` is required and must be within 60s of
    server time, so we sync an offset once and reuse it.
  * Only the *documented* parameters for each endpoint are signed; extra
    parameters are rejected by the server.
"""
from __future__ import annotations

import abc
import hashlib
import hmac
import time
from typing import Any, Callable, Dict, Optional

import requests

from bot.config.settings import Config

ApiLogHook = Callable[[str, int, float, bool], None]


class ExchangeClient(abc.ABC):
    """Interface the bot uses to talk to an exchange (live or simulated)."""

    @abc.abstractmethod
    def get_exchange_info(self) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def get_ticker(self, pair: Optional[str] = None) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def get_balance(self) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def get_pending_count(self) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def place_order(self, pair: str, side: str, type_: str,
                    quantity: float, price: Optional[float] = None) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def query_order(self, order_id: Optional[str] = None,
                    pair: Optional[str] = None,
                    pending_only: Optional[bool] = None) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def cancel_order(self, order_id: Optional[str] = None,
                     pair: Optional[str] = None) -> Dict[str, Any]: ...


class SignedMixin:
    """HMAC signing, time sync, retry/rate-limit, request plumbing."""

    def __init__(self, cfg: Config, log_api: Optional[ApiLogHook] = None) -> None:
        self.cfg = cfg
        self._log_api = log_api
        self._session = requests.Session()
        self._time_offset_ms = 0.0
        self._last_call_ts = 0.0
        self.sync_time()

    # -- signing ------------------------------------------------------------
    @staticmethod
    def _sign(params: Dict[str, str], secret: str) -> str:
        items = []
        for k in sorted(params.keys()):
            items.append(f"{k}={params[k]}")
        raw = "&".join(items)
        return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"),
                       hashlib.sha256).hexdigest()

    def _now_ts(self) -> int:
        return int(time.time() * 1000 + self._time_offset_ms)

    def sync_time(self) -> None:
        """Fetch server time and compute a local->server offset."""
        try:
            r = self._session.get(f"{self.cfg.roostoo_base_url}/v3/serverTime",
                                  timeout=10)
            if r.status_code == 200:
                server = float(r.json().get("ServerTime", 0))
                self._time_offset_ms = server - time.time() * 1000
        except Exception:
            # Keep the previous offset; we will retry on the next cycle.
            pass

    # -- request wrapper ----------------------------------------------------
    def _rate_limit(self) -> None:
        wait = self.cfg.rate_limit_seconds - (time.time() - self._last_call_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.time()

    def _signed_headers(self, params: Dict[str, str]) -> Dict[str, str]:
        sig = self._sign(params, self.cfg.roostoo_api_secret)
        return {
            "RST-API-KEY": self.cfg.roostoo_api_key,
            "MSG-SIGNATURE": sig,
        }

    def _request(self, method: str, endpoint: str, params: Dict[str, Any],
                 signed: bool, max_retries: int = 3) -> Dict[str, Any]:
        url = f"{self.cfg.roostoo_base_url}{endpoint}"
        delay = 1.0
        for attempt in range(max_retries):
            self._rate_limit()
            ts = self._now_ts()
            q = {k: v for k, v in params.items() if v is not None}
            if signed:
                q["timestamp"] = str(ts)
                headers = self._signed_headers(q)
                if method == "GET":
                    body_arg, data_arg = q, None
                else:
                    items = "&".join(f"{k}={q[k]}" for k in sorted(q.keys()))
                    body_arg, data_arg = None, items
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                headers = {}
                body_arg, data_arg = q, None
            t0 = time.time()
            try:
                if method == "GET":
                    r = self._session.get(url, headers=headers, params=body_arg,
                                          timeout=10)
                else:
                    r = self._session.post(url, headers=headers, data=data_arg,
                                           timeout=10)
                latency = time.time() - t0
                ok = (r.status_code == 200)
                if self._log_api:
                    self._log_api(endpoint, r.status_code, latency, ok)
                if not ok:
                    r.raise_for_status()
                payload = r.json()
                # A 200 with Success=false is still an application-level failure.
                if isinstance(payload, dict) and payload.get("Success") is False:
                    if self._log_api:
                        self._log_api(endpoint, r.status_code, latency, False)
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        delay *= 2
                        # Re-sync time on a possible timestamp error.
                        if "timestamp" in str(payload.get("ErrMsg", "")).lower():
                            self.sync_time()
                        continue
                return payload
            except Exception as e:  # noqa: BLE001 - network/HTTP errors
                latency = time.time() - t0
                if self._log_api:
                    self._log_api(endpoint, -1, latency, False)
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                # Last attempt: return a structured failure so callers can skip.
                return {"Success": False, "ErrMsg": f"request failed: {e}"}
        return {"Success": False, "ErrMsg": "request failed after retries"}
