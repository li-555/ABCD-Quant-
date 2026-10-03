# Roostoo Public Trading API (v3) — Reference

## Integration update (2026-10-03)

The current test-account balance response uses `SpotWallet` and `MarginWallet`.
The client aliases **only SpotWallet** to the legacy `Wallet` field. Ticker is a
timestamp-only public endpoint. Reads may retry; placing an order never retries
automatically because a timeout may conceal a successful fill. This supersedes
the generic retry description below. No historical OHLCV endpoint is documented.
See [platform integration](ROOSTOO_INTEGRATION.md) for account profiles, external
signals, data limitations, execution gates and 100 passing offline tests.

This document records the Roostoo REST API surface that `bot/execution/` talks
to. It is extracted verbatim from the official
[`Roostoo-API-Documents`](https://github.com/roostoo/Roostoo-API-Documents)
repository (README.md) and the accompanying Python demo. **Nothing here is
invented** — if a field or endpoint is not listed in the official docs it is
left out and flagged in the final report.

> The competition runs against the mock gateway, so the base URL is
> `https://mock-api.roostoo.com` (set via `ROOSTOO_BASE_URL`, default in
> `.env.example`). The public v3 API lives under the `/v3/*` path.

## 1. Base URL and security model

| Item | Value |
| --- | --- |
| Base URL | `https://mock-api.roostoo.com` |
| API version prefix | `/v3` |
| Auth (signed) | Header `RST-API-KEY` + `MSG-SIGNATURE` (HMAC-SHA256) |
| Timestamp | 13-digit **millisecond** integer, within **±60 s** of server time |

The API uses a three-tier progressive security model:

| Level | Headers / params | Endpoints |
| --- | --- | --- |
| `RCL_NoVerification` | none | `GET /v3/serverTime`, `GET /v3/exchangeInfo` |
| `RCL_TSCheck` | `timestamp` (query) | `GET /v3/ticker` |
| `RCL_TopLevelCheck` | `RST-API-KEY`, `MSG-SIGNATURE`, `timestamp` | `GET /v3/balance`, `GET /v3/pending_count`, `POST /v3/place_order`, `POST /v3/query_order`, `POST /v3/cancel_order` |

## 2. Authentication & request signing

`RCL_TopLevelCheck` endpoints require a signature computed as:

1. Collect **all** request parameters (including `timestamp`) as key/value pairs.
2. Sort the parameters **alphabetically by key**.
3. Build the string `totalParams = "k1=v1&k2=v2&..."` using the sorted keys
   (values are sent **as-is**, no extra encoding beyond the literal value).
4. `MSG-SIGNATURE = HMAC-SHA256(secretKey, totalParams)`, **hex**-encoded.
5. Send headers `RST-API-KEY: <API_KEY>` and `MSG-SIGNATURE: <signature>`.

Rules enforced by the server:

- The `timestamp` parameter is mandatory on every signed call and must be a
  13-digit millisecond value within ±60 s of server time. The client
  (`bot/execution/client.py`) pre-synchronises a clock offset from
  `GET /v3/serverTime` and re-syncs on timestamp errors.
- For **GET** signed calls the params go in the query string.
- For **POST** signed calls the sorted `totalParams` string is sent as the
  request **body** with header
  `Content-Type: application/x-www-form-urlencoded`.
- Only the *documented* parameters for an endpoint may be sent; extra
  parameters are rejected.

### Python reference (from the official demo)

```python
import hashlib, hmac, time

def _get_signed_headers(params, api_key, secret):
    # params: dict already containing 'timestamp'
    items = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    signature = hmac.new(secret.encode("utf-8"),
                         items.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return {"RST-API-KEY": api_key, "MSG-SIGNATURE": signature}, items
```

## 3. Endpoints used by the bot

### 3.1 `GET /v3/serverTime` — `RCL_NoVerification`
Returns the server clock. Used once at startup to sync the local→server offset.

```
{ "ServerTime": 1570083944052 }
```

### 3.2 `GET /v3/exchangeInfo` — `RCL_NoVerification`
Returns trading-pair rules and precision. Used at startup to intersect the
configured universe with actually-tradable pairs.

```
{ "IsRunning": true, "TradePairs": { "BTC/USD": {...} }, ... }
```

### 3.3 `GET /v3/ticker` — `RCL_TSCheck`
Real-time prices. Optional `pair` (e.g. `BTC/USD`); if omitted, all tickers are
returned.

```
{ "Success": true, "ServerTime": ...,
  "Data": { "BTC/USD": { "MaxBid": 9318.45, "MinAsk": 9319.42,
                         "LastPrice": 9319.35, "Change": -0.0132 } } }
```

`Data[pair]` fields: `MaxBid`, `MinAsk`, `LastPrice`, `Change`
(24 h % change, e.g. `-0.0178` = −1.78 %).

### 3.4 `GET /v3/balance` — `RCL_TopLevelCheck`
Wallet balances.

```
{ "Success": true,
  "Wallet": { "USD": { "Free": 100000.0, "Lock": 0.0 },
              "BTC": { "Free": 0.5, "Lock": 0.0 } } }
```

### 3.5 `GET /v3/pending_count` — `RCL_TopLevelCheck`
Returns pending-order count by pair.

```
{ "Success": true, "TotalPending": 0, "OrderPairs": { ... } }
```

### 3.6 `POST /v3/place_order` — `RCL_TopLevelCheck`
Execute a trade. The bot uses **spot 1x LONG only** and submits **LIMIT**
orders (no shorting; the `/v6/short_*` endpoints are intentionally not
implemented).

Parameters (all strings):

| Name | Type | Notes |
| --- | --- | --- |
| `pair` | STRING | e.g. `BTC/USD` |
| `side` | STRING | `BUY` / `SELL` |
| `type` | STRING | `LIMIT` / `MARKET` |
| `quantity` | STRING | order quantity, numeric as string |
| `price` | DECIMAL (string) | **required only when `type=LIMIT`** |
| `timestamp` | 13-digit ms | always |

```
{ "Success": true, "OrderDetail": { "OrderID": "...", "Status": "FILLED" } }
```

`OrderDetail.Status` ∈ `{ FILLED, PENDING, CANCELED }`.

### 3.7 `POST /v3/query_order` — `RCL_TopLevelCheck`
Retrieve order details / history. `order_id` and `pair` are mutually
exclusive; `pending_only` is the upper-case string `"TRUE"` / `"FALSE"`.

```
{ "Success": true, "OrderMatched": [ ... ] }
```

### 3.8 `POST /v3/cancel_order` — `RCL_TopLevelCheck`
Cancel pending orders. `order_id` and `pair` are mutually exclusive; if both
are omitted, all pending orders are cancelled.

```
{ "Success": true, "CanceledList": [ ... ] }
```

Only **pending** orders can be cancelled.

## 4. Common response envelope

Every endpoint returns JSON of the form:

```json
{ "Success": true,  "ErrMsg": "", "ServerTime": 1580762734517 }
{ "Success": false, "ErrMsg": "..." }
```

The bot treats `Success == false` as an application-level failure (it retries
with exponential back-off and re-syncs the clock on timestamp errors).

## 5. Pair & type conventions

- Pairs are written `XXX/USD` (e.g. `BTC/USD`).
- Boolean parameters (e.g. `pending_only`) are the **upper-case strings**
  `"TRUE"` / `"FALSE"`.
- Numeric parameters are passed as plain strings; `quantity`/`price` are
  accepted as decimal strings.

## 6. Notes for the competition

- **Spot 1x LONG only.** The bot never uses leverage, never shorts, never
  trades stocks, and never performs market-making / arbitrage / HFT. This
  matches the hackathon rules.
- The mock gateway at `https://mock-api.roostoo.com` mirrors the public v3 API
  above; the same code path is used for both paper and live mode (the only
  difference is `LIVE=1`, which switches `PaperClient` → `RoostooClient`).
- Secrets (`ROOSTOO_API_KEY`, `ROOSTOO_API_SECRET`) are injected via
  environment variables only and are never written to logs or the repo.
