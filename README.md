# ABCD-Quant — Roostoo Live Trading Bot (v1)

Roostoo connection and external strategies: [integration guide](docs/ROOSTOO_INTEGRATION.md).
Use `python -m bot.main --check-connection --account test` for a read-only check.

Optional factors and historical screening results:
[docs/FACTOR_STUDY.md](docs/FACTOR_STUDY.md). The retained volume paper configuration
is `bot/config/volume_research.yaml`; the default config remains the v1 baseline.

A 7×24, AWS-deployable trading bot that ports the **validated** strategy in
`research/backtest.py` to the Roostoo mock exchange for the Susquehanna ×
Roostoo quant hackathon (HK / AU / IN). It runs unattended on a single
`python -m bot.main` process, rebalances once per UTC day, and never requires a
human in the loop.

> **Strategy v1 = research `backtest.py` variant A** — slow layer only, 30-minute
> bars, daily rebalance at UTC 00:00, **crypto-only**, **long-only**. The bot is
> a faithful, line-for-line port of that engine; it is *not* an "improved"
> version. `research/backtest.py` is committed **unchanged**, and an offline test
> (`tests/test_alignment.py`) proves the bot reproduces the research engine's
> rebalance decisions to within float noise.

---

## 1. Strategy overview (v1)

Cross-sectional, long-only crypto rotation driven by the **slow signal layer**
of the research strategy:

| Component | Formula |
| --- | --- |
| Trend | `EMA(fast) / EMA(slow)` z-score over the slow window |
| Cross-sectional momentum | rank-based momentum score |
| Donchian position | price location within its N-day range |
| Composite slow score | `S_slow = 0.4·trend + 0.4·mom + 0.2·don` |
| Overheat penalty | halve exposure if `|z1| > 2.5` and same sign as `S_slow` |
| Inverse-vol weights | `w_i ∝ |score_i| / σ_i`, normalised |
| Breadth regime `m_l` | scale down when few assets trend up |
| Vol target `g_vol` | `min(1, σ_target / portfolio_σ)` |
| Drawdown scale `m_dd` | shrink on drawdown + hard circuit breaker |
| Caps | single 0.35, cluster net 0.70 / gross 1.05, total gross 1.0 |
| Trade filter | no-trade band, min-trade, min-hold 12h, trailing stop 1.5·σ, cooldown 3h |

The **exchange is the single source of truth** for positions and prices. Each
30-minute cycle the bot: fetches the latest closed 30m bars (Binance, with a
recorded-Roostoo fallback), checks data freshness, reads balance + ticker,
applies a price-sanity check (Roostoo vs Binance close), runs trailing stops,
runs the daily rebalance (with midnight catch-up), optionally runs the activity
guard, then writes the equity snapshot, heartbeat, and atomic state.

---

## 2. Project structure

```
ABCD-Quant-/
├── bot/                      # the live bot (the deliverable)
│   ├── main.py               # entrypoint: python -m bot.main
│   ├── scheduler.py          # 7x24 loop, cycle orchestration
│   ├── state.py              # atomic JSON state
│   ├── logging_utils.py      # rotating logs + heartbeat
│   ├── config/
│   │   ├── settings.py       # Config dataclass + load_config (env overrides)
│   │   ├── config.yaml       # all strategy/risk/execution params
│   │   └── universe.yaml     # tradable coins (Binance symbol + Roostoo pair)
│   ├── data/                 # Binance klines + recorded-Roostoo fallback
│   ├── execution/            # RoostooClient (live) + PaperClient (dry-run)
│   └── strategy/             # signals, portfolio, risk, activity_guard
├── research/                 # research/backtest.py — COPIED UNCHANGED
├── tests/                    # offline pytest suite (pytest -q)
│   └── test_alignment.py     # bot == research parity proof (§7.4)
├── docs/
│   └── ROOSTOO_API.md        # verified Roostoo v3 API reference
├── scripts/                  # READ-ONLY helper scripts (no order placement)
│   ├── check_heartbeat.py    # liveness probe (mirrors Docker HEALTHCHECK)
│   ├── show_state.py         # print bot/state.json summary
│   └── validate_config.py    # verify config == v1 contract
├── Dockerfile                # python:3.11-slim, non-root, UTC, HEALTHCHECK
├── .dockerignore
├── .gitignore
├── .env.example
├── requirements.txt          # pinned runtime deps (no matplotlib/yfinance)
├── requirements-dev.txt      # pytest (test only)
└── README.md                 # this file
```

---

## 3. Quickstart

### 3.1 Paper mode (local, no real orders)

```bash
python -m venv .venv && . .venv/Scripts/activate     # or your managed venv
pip install -r requirements-dev.txt
cp .env.example .env          # leave LIVE=0 (paper mode)
python -m bot.main --once     # one cycle (smoke test)
python -m bot.main            # 7x24 loop (paper)
```

In paper mode the bot uses `PaperClient`, which fills orders against the latest
Binance close. **No real orders are ever sent.**

### 3.2 Docker

```bash
docker build -t abcd-quant-bot .
# paper
docker run -d --name bot -e LIVE=0 --env-file .env abcd-quant-bot
# watch health
docker inspect --format '{{.State.Health.Status}}' bot
```

### 3.3 EC2 (production)

1. Launch an Ubuntu 22.04 t3.micro (or similar) in the competition region.
2. Install Docker, clone this repo, `docker build -t abcd-quant-bot .`.
3. **Paper first** to prove the pipeline end-to-end against the mock gateway:
   ```bash
   docker run -d --restart unless-stopped --name bot-paper \
     -e LIVE=0 --env-file .env abcd-quant-bot
   docker logs -f bot-paper      # confirm cycles run, heartbeat written
   ```
4. Once paper is healthy for ≥24h, switch to **live** (real orders):
   ```bash
   docker stop bot-paper && docker rm bot-paper
   docker run -d --restart unless-stopped --name bot-live \
     -e LIVE=1 \
     -e ROOSTOO_LIVE_TRADING=true \
     -e ROOSTOO_LIVE_CONFIRM=I_UNDERSTAND_AND_ACCEPT_LIVE_TRADING_RISK \
     -e ROOSTOO_API_KEY=<key> -e ROOSTOO_API_SECRET=<secret> \
     -e ROOSTOO_BASE_URL=https://mock-api.roostoo.com \
     abcd-quant-bot
   docker logs -f bot-live
   ```
   如果缺少 `ROOSTOO_LIVE_TRADING=true` 或确认字符串不匹配，系统会保持只读/不下单模式。

> The `restart unless-stopped` policy + the Docker `HEALTHCHECK` (heartbeat
> fresher than 45 min) keep the bot alive across reboots and crashes.

---

## 4. Configuration (`bot/config/config.yaml`)

All strategy, risk and execution parameters live in `config.yaml`; **secrets are
never stored there** (env only). A subset:

| Key | Default | Meaning |
| --- | --- | --- |
| `bar_min` | 30 | bar length (30m klines) |
| `rebalance_hour_utc` | 0 | daily rebalance fires after UTC 00:00 |
| `n_long` | 6 | max long positions |
| `hold_rank_long` | 10 | incumbent-protection rank |
| `gate_thr` / `entry_thr` | 0.2 / 0.2 | eligibility / entry thresholds |
| `sigma_target` | 0.04 | daily portfolio vol target |
| `single_long` | 0.35 | per-asset long cap |
| `coin_cap` | 0.70 | coin-cluster net exposure cap |
| `gross_mult` | 1.5 | cluster gross cap = `gross_mult·net` |
| `dd_scale` / `dd_hard` | 0.10 / 0.12 | drawdown scale / breaker trigger |
| `dd_window_h` | 336 | 14-day peak window (hours) |
| `breaker_h` | 12 | circuit-breaker duration (hours) |
| `band_abs` / `band_rel` | 0.08 / 0.30 | no-trade band |
| `min_trade` | 0.005 | minimum traded weight |
| `min_hold_h` | 12 | minimum holding time (hours) |
| `stop_long` | 1.5 | trailing-stop distance (daily σ) |
| `cool_h` | 3 | cooldown after a stop (hours) |
| `allow_short` | false | **long-only (hard rule)** |
| `max_gross` | 1.0 | total gross exposure cap |
| `history_days` | 60 | warm-up klines on startup |
| `staleness_minutes` | 90 | skip cycle if feed is stale |
| `max_daily_trades` | 200 | safety cap on fills/day |
| `rate_limit_seconds` | 1.0 | min spacing between API calls |
| `activity_enabled` / `min_daily_trades` | true / 2 | compliance activity guard |

`scripts/validate_config.py` verifies the live config still matches the v1
contract — run it in CI / before deploy.

---

## 5. Risk controls

* **Long-only, spot 1x** — no leverage, no shorts, no stocks, no derivatives.
* **Exposure caps** — single 0.35, cluster net 0.70 / gross 1.05, total 1.0.
* **Drawdown scaling + circuit breaker** — `m_dd` shrinks exposure on drawdown; a
  hard breaker forces the minimum scale for `breaker_h` hours after a deep drawdown.
* **Trailing stops** — 1.5 daily σ trailing stop with a 3h cooldown before
  re-entry.
* **No-trade band / min-hold / min-trade** — avoids churn and over-trading.
* **Data safety** — cycles are **skipped** (never traded) on stale feeds, bad
  balances, missing prices, or Roostoo/Binance price deviations > 2%.
* **Activity guard** — ensures a minimal, strategy-aligned number of fills per day
  so the account is not flagged as dormant, while never inflating risk.
* **Kill switch** — touching `bot/KILL` puts the bot in close-only mode
  (stops rebalancing; lets stops/activity finish) without killing the process.

---

## 6. Logs & monitoring

* `bot/logs/heartbeat.json` — written every cycle (`time`, `equity`,
  `trades_today`, `last_rebalance_date`, `live`). The Docker `HEALTHCHECK` and
  `scripts/check_heartbeat.py` read this.
* `bot/logs/equity.csv` — per-cycle equity / cash / position snapshot.
* `bot/logs/*.log` — rotating structured logs.
* `bot/state.json` — atomic persistent state (positions, entry times, cooldowns,
  breaker, peak, daily trade counter). Inspected with `scripts/show_state.py`.
* `bot/main.py --once` — single-cycle smoke test (safe in any mode).

---

## 7. Compliance

This bot is built for the hackathon rules and **never** does any of the
prohibited behaviours:

* Spot 1x **LONG only** (`allow_short=false`; the `/v6/short_*` endpoints are not
  implemented).
* **No stocks, no leverage, no shorts.**
* **No HFT, no market-making, no arbitrage** — one rebalance per UTC day plus
  trailing-stop liquidations; order pacing respects `rate_limit_seconds`.
* **No manual-ordering scripts** — all orders are generated by the strategy; the
  only external scripts are read-only (`scripts/`).
* **Two-step live gate** — real orders require `LIVE=1`, `ROOSTOO_LIVE_TRADING=true`
  and `ROOSTOO_LIVE_CONFIRM=I_UNDERSTAND_AND_ACCEPT_LIVE_TRADING_RISK`.
* **Secrets via env vars only** — `ROOSTOO_API_KEY` / `ROOSTOO_API_SECRET` are
  never written to logs or the repo.
* **Activity guard** keeps the account active within the rules.

---

## 8. Honest research overview

`research/backtest.py` is the source of truth and is committed **unchanged**.
The live `bot/strategy/portfolio.py` is a faithful re-implementation of its
rebalance block; `tests/test_alignment.py` replays the research trade log and
asserts the bot's target weights match to within `1e-6`.

**Synthetic validation harness** (deterministic, `seed=7`, 90 days of
random-walk crypto, 30m bars, v1 params, 5 bps maker / 10 bps taker / 2 bps
slip). Reproduce with `python -c "from research import backtest; ..."` using
`Params(bar_min=30, rebal_min=1440, use_fast=False, allow_short=False, ...)`:

| Metric | Value |
| --- | --- |
| Total return | **−1.79%** over ~71 sample days |
| Daily-annualised Sharpe | **−0.62** |
| Max drawdown | **−5.80%** |
| Trades | 226 |
| Final equity | 98,215 (from 100,000) |

> These numbers come from the **synthetic** test harness, not the competition's
> real market data. They are meant to demonstrate that the engine is
> well-behaved and **stable** — contained drawdown (≈6%), no blow-ups, sensible
> trade count — on an independent random-walk price process. The synthetic
> process is deliberately mean-zero, so a near-flat / slightly negative result is
> expected and is **not** a forecast of live performance. Real backtest results
> were produced by the research team on historical Binance data and should be
> cited from that work; this bot ports that strategy unchanged.

---

## 9. Limitations

* **Data dependency** — the bot needs 30m Binance klines (with a recorded-Roostoo
  fallback) and a live Roostoo ticker/balance. If both are unavailable the cycle
  is skipped; it does **not** trade on stale or missing data.
* **Mock gateway only** — tuned against `https://mock-api.roostoo.com`; the same
  code path drives paper and live modes.
* **Single-process, single-region** — one container, one API key. No
  failover/replication (out of scope for v1).
* **Single-process reconciliation** — each submitted order is checked again via
  order query + refreshed wallet before continuing; mismatch blocks later orders
  until manual investigation.
* **Strategy is fixed at v1** — per the brief, parameters were **not** tuned or
  extended to stocks/short/leverage/arbitrage.

---

## 10. Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite is fully **offline** (synthetic data, paper client, no network). The
key test is `tests/test_alignment.py`, which proves the bot's portfolio
construction equals the research backtest. `scripts/validate_config.py` adds a
config-contract regression check.

---

## 11. API reference

See [`docs/ROOSTOO_API.md`](docs/ROOSTOO_API.md) for the verified Roostoo v3 API
(endpoints, HMAC-SHA256 signing, timestamp rules, response schemas) used by
`bot/execution/`.

## 12. Optional factors and drawdown study

See [factor selection](docs/FACTOR_STUDY.md) and
[drawdown diagnosis / cash reserve](docs/DRAWDOWN_STUDY.md) for the historical
protocol, rejected candidates, costs, limitations, and paper-only configurations.
New features default to off. The cash-reserve example reduces capital at risk;
it does not guarantee a drawdown ceiling or higher returns in every period.
