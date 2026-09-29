# Final Report — Roostoo Live Trading Bot (v1)

**Task:** Port the validated strategy in `research/backtest.py` into a 7×24,
AWS-deployable Roostoo trading bot (Susquehanna × Roostoo hackathon, HK/AU/IN).
Strategy v1 = variant A: slow layer only, 30m bars, daily rebalance at UTC
00:00, crypto-only, long-only. Repo: `ABCD-Quant-`, branch target
`feat/roostoo-live-bot-v1`.

All code, comments, docs and commit messages are in **English** per the judge's
requirement. `research/backtest.py` is committed **unchanged**.

---

## 1. File list

### New / changed deliverable files

```
bot/                              # the live bot
  main.py                         # entrypoint: python -m bot.main
  scheduler.py                    # 7x24 loop + per-cycle orchestration (+ load_universe fix)
  state.py                        # atomic JSON persistence
  logging_utils.py                # rotating logs + heartbeat writer
  config/
    settings.py                   # Config dataclass + env overrides (LIVE, secrets)
    config.yaml                   # all strategy/risk/execution params (matches v1 contract)
    universe.yaml                 # 11 coins (Binance symbol + Roostoo pair)
  data/{binance,roostoo_prices,store}.py   # klines + recorded-Roostoo fallback
  execution/
    client.py                     # ExchangeClient ABC + HMAC-SHA256 SignedMixin
    roostoo_client.py             # live client (Roostoo v3)
    paper_client.py               # dry-run client (no real orders)
    order_manager.py              # intent building + execution
  strategy/
    signals.py                    # build_signals (byte-identical to research variant A)
    portfolio.py                  # compute_target_weights (faithful re-implementation)
    risk.py                       # trailing-stop / peak logic
    activity_guard.py             # compliance activity guard
research/
  backtest.py                     # COPIED UNCHANGED (validation baseline)
  __init__.py
tests/                            # offline pytest suite (pytest -q)
  test_alignment.py               # bot == research parity proof (§7.4)
  test_universe.py                # universe/config v1-contract check (new)
  test_portfolio.py test_risk.py test_signals.py test_state.py
  test_execution.py test_scheduler.py test_activity_guard.py
  helpers.py __init__.py
docs/
  ROOSTOO_API.md                  # verified Roostoo v3 API reference
  FINAL_REPORT.md                 # this file
scripts/                          # READ-ONLY helpers (no order placement)
  check_heartbeat.py              # liveness probe (mirrors Docker HEALTHCHECK)
  show_state.py                   # bot/state.json viewer
  validate_config.py              # config == v1 contract check
Dockerfile                       # python:3.11-slim, non-root, UTC, HEALTHCHECK
.dockerignore
.gitignore                       # expanded (secrets, logs, state, caches)
.env.example                     # LIVE / ROOSTOO_API_KEY / SECRET / BASE_URL
requirements.txt                 # pinned runtime deps (no matplotlib/yfinance)
requirements-dev.txt             # pytest (test only)
README.md                        # bot deliverable doc (replaces old quant_research README)
```

**Modified (pre-existing tracked files):** `.gitignore`, `README.md`,
`requirements.txt`.

**Removed:** temporary debug helper `tests/debug_align.py` and a stray
`pytest-cache-files-1tzdmz46/` directory.

---

## 2. Test results (`pytest -q`)

```
50 passed in 19.28s
```

Coverage highlights:
- `tests/test_alignment.py` — replays the research trade log (with mark-to-market
  weight drift) and asserts the bot's target weights equal the research engine's
  to within `1e-6` at every common rebalance bar, and that the selected assets
  match exactly (`sel_mismatch == 0`).
- `tests/test_universe.py` — `load_universe` reads the `coins:` mapping, and the
  live config matches the v1 parameter contract.
- Remaining tests cover signals, portfolio construction, risk/trailing-stop,
  state, execution, scheduler, and the activity guard.

The suite is **fully offline** (synthetic data, paper client, no network).

---

## 3. Branch / commits / push

**Local commits** (on top of `53fc056`, as local branch `feat/roostoo-live-bot-v1`):

| Hash | Message |
| --- | --- |
| `92f3f93` | `feat(bot): implement Roostoo live trading bot v1 (core + research parity tests)` |
| `7d8b9a3` | `build(docs): add Dockerfile, pinned requirements, Roostoo API doc, README and read-only scripts` |

Note: a repository-local git identity (`ABCD-Quant Bot <bot@abcd-quant.local>`)
was set because none was configured; change it if the judges require a specific
author.

**Push status — BLOCKED (external credentials, 2nd attempt):**

Attempt on 2026-09-29 with the "newly permitted" account still returned the
same 403 — the credential Git is actually using is **still `Yung-Ting-Kai`**,
which is denied write access to `li-555/ABCD-Quant-`:

```
$ git push -u origin feat/roostoo-live-bot-v1
remote: Permission to li-555/ABCD-Quant-.git denied to Yung-Ting-Kai.
fatal: unable to access 'https://github.com/li-555/ABCD-Quant-/':
       The requested URL returned error: 403
```

So the local Git credential store / osxkeychain / GCM was **not** switched to the
permitted account — it is still serving the `Yung-Ting-Kai` token. The branch is
fully committed locally (all 4 commits below) and ready to push the moment the
correct credential is in place.

**Remedy (do this yourself in a terminal — do NOT paste a token into chat):**
1. Erase the cached `github.com` credential so the next push re-prompts:
   ```bash
   printf "protocol=https\nhost=github.com\n" | git credential reject
   # (or: `git config --global --unset credential.helper` then re-prompt once)
   ```
2. Authenticate as the **permitted** account (the one that has write access to
   `li-555/ABCD-Quant-`), e.g. via `gh auth login` or the Git Credential
   Manager pop-up, then:
   ```bash
   git push -u origin feat/roostoo-live-bot-v1
   ```
3. If the permitted account is a **different** GitHub user, an owner must add
   that user as a collaborator (Write) on the repo, OR you fork the repo and
   push the branch to your fork and open a PR.

Everything is committed locally; no code changes are outstanding. Once the
credential is fixed, the single `git push` above publishes the branch.

---

## 4. Open items

1. **Remote push permission (blocker above).** Everything is committed locally
   and ready; only the remote write access is outstanding.
2. **Live credentials.** `ROOSTOO_API_KEY` / `ROOSTOO_API_SECRET` for the
   competition mock gateway must be supplied via env (`.env` / `--env-file` /
   EC2 environment). They are never stored in the repo or logs.
3. **Real-market backtest numbers.** The research overview in the README reports
   **synthetic** harness metrics (deterministic, `seed=7`). Cite the research
   team's historical-Binance backtest results for the official performance claim.
4. **`git` author identity** — set to a local placeholder; adjust if required.

---

## 5. EC2 startup commands

```bash
# 1) Launch Ubuntu 22.04 (t3.micro or similar), install Docker, clone, build
sudo apt-get update && sudo apt-get install -y docker.io
git clone <repo-url> && cd ABCD-Quant-
git checkout feat/roostoo-live-bot-v1
docker build -t abcd-quant-bot .

# 2) PAPER FIRST — prove the pipeline against the mock gateway (no real orders)
docker run -d --restart unless-stopped --name bot-paper -e LIVE=0 --env-file .env abcd-quant-bot
docker logs -f bot-paper                 # confirm cycles run + heartbeat.json written

# 3) After >=24h healthy on paper, switch to LIVE (real orders)
docker stop bot-paper && docker rm bot-paper
docker run -d --restart unless-stopped --name bot-live \
  -e LIVE=1 \
  -e ROOSTOO_API_KEY=<key> -e ROOSTOO_API_SECRET=<secret> \
  -e ROOSTOO_BASE_URL=https://mock-api.roostoo.com \
  abcd-quant-bot
docker logs -f bot-live

# 4) Health / state inspection (any time)
docker inspect --format '{{.State.Health.Status}}' bot-live   # "healthy"
docker exec bot-live python scripts/check_heartbeat.py
docker exec bot-live python scripts/show_state.py
```

The `restart unless-stopped` policy plus the Docker `HEALTHCHECK` (heartbeat
fresher than 45 min) keep the bot alive across reboots/crashes.

---

## 6. Honest research overview (synthetic validation harness)

Run with `research/backtest.py` v1 params (`bar_min=30, rebal_min=1440,
use_fast=False, allow_short=False`, the confirmed parameter set) on 90 days of
deterministic random-walk crypto (30m bars, 5/10/2 bps cost model). Reproduced
on 2026-09-29 against the refreshed `research/backtest.py`:

| Metric | Value |
| --- | --- |
| Total return | −1.79% (~71 sample days) |
| Daily-annualised Sharpe | −0.62 |
| Max drawdown | −5.80% |
| Trades | 226 |
| Final equity | 98,215 (from 100,000) |

The synthetic process is mean-zero by construction, so a near-flat / slightly
negative result is expected and simply demonstrates the engine is **stable and
contained** (≈6% drawdown, sensible trade count, no blow-ups). It is **not** a
forecast of live performance on real data.
