"""30-minute trading scheduler (UTC-aligned, 7x24).

One :func:`cycle` runs per period.  Each cycle, in order (per CODING_AGENT_PROMPT
section 5.1), the bot:

  1. fetches the latest closed 30m bars (Binance, with a recorded-Roostoo
     fallback) and checks data freshness;
  2. reads the exchange balance + ticker - *the exchange is the single source of
     truth* for positions and prices;
  3. sanity-checks each coin's Roostoo price against the Binance close;
  4. records the equity snapshot and updates trailing-stop extremes;
  5. runs the trailing-stop check (market liquidation + cooldown);
  6. runs the daily rebalance once per UTC day, after 00:00, catching up if the
     bot was down at midnight;
  7. runs the activity guard at/after UTC 20:00 if the day is too quiet;
  8. writes the equity row, heartbeat and (atomic) state, then sleeps until the
     next bar close + 20s.

Any step that cannot be performed safely (bad data, balance fetch failure,
stale feed) is logged and the cycle is skipped - the bot never trades on
uncertain data.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yaml

from bot.config.settings import Config
from bot.strategy.signals import build_signals
from bot.strategy.portfolio import compute_target_weights, TargetDiag
from bot.strategy.risk import update_extremes, stop_hits, peak_update
from bot.strategy.activity_guard import propose_activity_trades
from bot.execution.order_manager import (
    build_rebalance_intents, build_stop_intents, execute_intent, OrderIntent,
)
from bot.data import binance, roostoo_prices
from bot.state import State
from bot.logging_utils import Loggers, write_heartbeat

logger = logging.getLogger("bot")

MIN_BARS = 500  # minimum closed bars needed for the Donchian (480) window


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def utcnow() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def pair_of(coin: str) -> str:
    return f"{coin}/USD"


def coin_of(pair: str) -> str:
    return pair.split("/")[0]


def load_universe(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or []
    # Accept either a top-level list of coin dicts or a mapping with a
    # "coins" key (the format used by bot/config/universe.yaml).
    items = data.get("coins", []) if isinstance(data, dict) else data
    return [{"coin": d["coin"], "binance_symbol": d["binance_symbol"],
             "roostoo_pair": d["roostoo_pair"]} for d in items]


def ts_to_bar(ts: Optional[pd.Timestamp], grid0: pd.Timestamp,
              bar_min: int) -> int:
    """Map an absolute time to a bar index in the current grid (negative = past)."""
    if ts is None:
        return -10 ** 9
    try:
        return int(round((pd.Timestamp(ts) - grid0).total_seconds() /
                         (bar_min * 60)))
    except Exception:
        return -10 ** 9


def bar_to_iso(bar: int, grid0: pd.Timestamp, bar_min: int) -> str:
    return (grid0 + pd.Timedelta(minutes=bar_min) * bar).isoformat()


def kill_present(cfg: Config) -> bool:
    return os.path.exists(cfg.kill_file)


def fetch_price_grid(universe: List[Dict[str, str]], cfg: Config
                     ) -> Optional[pd.DataFrame]:
    """Binance grid (with recorded-Roostoo fallback); columns renamed to coins."""
    symbols = [u["binance_symbol"] for u in universe]
    grid = binance.load_price_grid(symbols, cfg, days=cfg.history_days)
    if grid is None or grid.empty:
        grid = roostoo_prices.load_recorded_grid(
            {u["coin"]: u["binance_symbol"] for u in universe}, cfg)
    if grid is None or grid.empty:
        return None
    coin_by_symbol = {u["binance_symbol"]: u["coin"] for u in universe}
    grid = grid.rename(columns=coin_by_symbol)
    grid = grid.reindex(columns=[u["coin"] for u in universe])
    return grid


# --------------------------------------------------------------------------- #
# rebalance + activity helpers
# --------------------------------------------------------------------------- #
def compute_target(cfg: Config, sig_row: Dict[str, np.ndarray], w: np.ndarray,
                   real_mask: np.ndarray, lr: pd.DataFrame, t: int,
                   grid0: pd.Timestamp, equity: float, peak: float,
                   state: State, universe: List[Dict[str, str]]) -> tuple:
    """Return (tgt, diag, breaker_until_bar) using the persistent state."""
    n = len(universe)
    coins = [u["coin"] for u in universe]
    breaker_until_bar = ts_to_bar(
        pd.Timestamp(state.breaker_until) if state.breaker_until else None,
        grid0, cfg.bar_min)
    cooldown_bars = np.array(
        [ts_to_bar(state.get_cooldown(c), grid0, cfg.bar_min) for c in coins])
    entry_bars = np.array(
        [ts_to_bar(_entry_ts(state, c), grid0, cfg.bar_min) for c in coins])
    cluster = np.ones(n, dtype=bool)
    tgt, diag, breaker_until_bar = compute_target_weights(
        sig_row, w, cooldown_bars, entry_bars, breaker_until_bar,
        equity, peak, lr, real_mask, t, cfg, cluster)
    return tgt, diag, breaker_until_bar


def _entry_ts(state: State, coin: str) -> Optional[pd.Timestamp]:
    s = state.coin(coin).entry_time
    return pd.Timestamp(s) if s else None


def run_rebalance(cfg: Config, client, state: State, loggers: Loggers,
                  universe: List[Dict[str, str]], sig_row: Dict[str, np.ndarray],
                  w: np.ndarray, price_arr: np.ndarray, prices: Dict[str, float],
                  ticker: Dict[str, dict], exinfo: dict, lr: pd.DataFrame, t: int,
                  grid0: pd.Timestamp, equity: float, real_mask: np.ndarray,
                  now: pd.Timestamp) -> int:
    """Execute the daily rebalance; return the number of orders placed."""
    n = len(universe)
    tgt, diag, breaker_until_bar = compute_target(
        cfg, sig_row, w, real_mask, lr, t, grid0, equity, state.peak, state, universe)

    pairs = [pair_of(u["coin"]) for u in universe]
    intents = build_rebalance_intents(pairs, w, tgt, equity, cfg.min_trade)
    placed = 0
    for it in intents:
        ex = execute_intent(client, it, prices, ticker, exinfo, cfg, equity,
                             loggers.log_order)
        if ex.get("ok"):
            placed += sum(1 for o in ex.get("orders", []) if o.get("success"))

    # persist entry times, trailing extremes, breaker
    state.breaker_until = bar_to_iso(breaker_until_bar, grid0, cfg.bar_min)
    for i, u in enumerate(universe):
        c = u["coin"]
        if tgt[i] > 0 and w[i] == 0:
            state.set_entry(c, now)
            if not np.isfinite(state.get_ext(c)):
                state.set_ext(c, float(price_arr[i]))
        if tgt[i] == 0:
            state.set_ext(c, None)
    state.last_rebalance_date = now.floor("1D").strftime("%Y-%m-%d")

    _log_decision(loggers, now, universe, sig_row, w, tgt, real_mask, diag,
                  state, equity, intents)
    return placed


def run_activity_guard(cfg: Config, client, state: State, loggers: Loggers,
                       universe: List[Dict[str, str]], w: np.ndarray,
                       price_arr: np.ndarray, prices: Dict[str, float],
                       ticker: Dict[str, dict], exinfo: dict,
                       sig_row: Dict[str, np.ndarray], real_mask: np.ndarray,
                       lr: pd.DataFrame, t: int, grid0: pd.Timestamp,
                       equity: float, now: pd.Timestamp) -> int:
    """Ensure a minimal, strategy-aligned number of fills for the day."""
    btc_index = next((i for i, u in enumerate(universe) if u["coin"] == "BTC"),
                     None)
    tgt, _diag, _b = compute_target(cfg, sig_row, w, real_mask, lr, t, grid0,
                                    equity, state.peak, state, universe)
    acts = propose_activity_trades(w, tgt, state.day_trade_count, cfg, btc_index)
    if not acts:
        return 0
    pairs = [pair_of(u["coin"]) for u in universe]
    intents: List[OrderIntent] = []
    for a in acts:
        i = int(a["index"])
        notional = a["notional"] * equity if a["notional"] <= 1 else a["notional"]
        intents.append(OrderIntent(pairs[i], a["side"], float(notional),
                                   aggressive=False, reason=a["reason"]))
    placed = 0
    for it in intents:
        ex = execute_intent(client, it, prices, ticker, exinfo, cfg, equity,
                             loggers.log_order)
        if ex.get("ok"):
            placed += sum(1 for o in ex.get("orders", []) if o.get("success"))
    state.activity_done_date = now.floor("1D").strftime("%Y-%m-%d")
    return placed


def _log_decision(loggers: Loggers, now, universe, sig_row, w, tgt, real_mask,
                  diag: TargetDiag, state: State, equity: float,
                  intents: List[OrderIntent]) -> None:
    traded = {it.pair: it.reason for it in intents}
    coins = []
    for i, u in enumerate(universe):
        c = u["coin"]
        coins.append({
            "coin": c,
            "S": float(sig_row["S"][i]),
            "S_slow": float(sig_row["S_slow"][i]),
            "sig_d": float(sig_row["sig_d"][i]),
            "w_current": float(w[i]),
            "w_target": float(tgt[i]),
            "real": bool(real_mask[i]),
            "decision": traded.get(pair_of(c), "hold"),
        })
    loggers.log_decision({
        "time": now.isoformat(),
        "equity": equity,
        "peak": state.peak,
        "breaker_until": state.breaker_until,
        "m_l": diag.m_l, "g_vol": diag.g_vol, "m_dd": diag.m_dd,
        "selected": [universe[i]["coin"] for i in diag.selected],
        "coins": coins,
    })


# --------------------------------------------------------------------------- #
# one cycle
# --------------------------------------------------------------------------- #
def cycle(cfg: Config, client, state: State, loggers: Loggers,
          universe: List[Dict[str, str]]) -> None:
    now = utcnow()
    n = len(universe)
    pairs = [pair_of(u["coin"]) for u in universe]

    # 1) data
    grid = fetch_price_grid(universe, cfg)
    if grid is None or grid.shape[0] < MIN_BARS:
        logger.error("insufficient price history; skipping cycle")
        return
    last_bar = grid.index[-1]
    age_min = (now - last_bar).total_seconds() / 60.0
    if age_min > cfg.staleness_minutes:
        logger.error("price feed stale (%.1f min > %d); skipping cycle",
                     age_min, cfg.staleness_minutes)
        return

    # In paper mode feed the latest Binance close as the simulated market price
    # so the offline client can value positions and fill orders consistently.
    if hasattr(client, "set_market_prices"):
        client.set_market_prices(
            {pair_of(u["coin"]): float(grid.iloc[-1, i])
             for i, u in enumerate(universe)})

    # 2) signals (closed bars only)
    sig = build_signals(grid, cfg)
    t = grid.shape[0] - 1
    sig_row = {k: sig[k].iloc[t].values.astype(float)
               for k in ("S", "S_slow", "trend", "sig_d")}
    lr = np.log(grid).diff()

    # 3) exchange = truth
    bal = client.get_balance()
    tk = client.get_ticker()
    if not isinstance(bal, dict) or not bal.get("Success"):
        logger.error("balance fetch failed; skipping cycle")
        return
    wallet = bal.get("Wallet", {})
    data = tk.get("Data", {}) if isinstance(tk, dict) else {}
    cash = float(wallet.get("USD", {}).get("Free", 0.0)) + \
        float(wallet.get("USD", {}).get("Lock", 0.0))

    qty = np.zeros(n)
    price_arr = np.zeros(n)
    prices: Dict[str, float] = {}
    for i, u in enumerate(universe):
        pair = u["roostoo_pair"]
        info = data.get(pair, {})
        last = info.get("LastPrice")
        prices[pair] = last
        held = wallet.get(u["coin"], {})
        qty[i] = float(held.get("Free", 0.0)) + float(held.get("Lock", 0.0))
        price_arr[i] = last if (last is not None and not np.isnan(last)) else np.nan

    valid_price = np.isfinite(price_arr)
    if not valid_price.any():
        logger.error("no usable prices; skipping cycle")
        return
    equity = cash + float(np.nansum(qty * price_arr))
    w = np.zeros(n)
    nz = valid_price & (equity > 0)
    w[nz] = (qty[nz] * price_arr[nz]) / equity

    # 4) price sanity (Roostoo vs Binance close)
    real_mask = np.ones(n, dtype=bool)
    for i, u in enumerate(universe):
        bn = grid.iloc[-1, i]
        rt = price_arr[i]
        if np.isfinite(rt) and np.isfinite(bn) and bn > 0:
            if abs(rt - bn) / bn > cfg.price_deviation_pct / 100.0:
                real_mask[i] = False
                logger.warning("price deviation for %s (rt=%.4f bn=%.4f); "
                               "skipping this cycle trade", u["coin"], rt, bn)

    # record ticker snapshots (fallback source)
    roostoo_prices.record_ticker(cfg, data, now)

    # 5) trailing-stop extremes
    held_mask = w > 0
    ext_arr = np.array([state.get_ext(u["coin"]) for u in universe], dtype=float)
    ext_arr = update_extremes(ext_arr, price_arr, held_mask)
    for i, u in enumerate(universe):
        if held_mask[i] and not np.isfinite(ext_arr[i]):
            ext_arr[i] = price_arr[i]
        state.set_ext(u["coin"], ext_arr[i])

    # 6) trailing stops (market liquidation + cooldown)
    cycle_trades = 0
    hit = stop_hits(w, price_arr, ext_arr, sig_row["sig_d"], cfg)
    if hit.any():
        stop_intents = build_stop_intents(pairs, w, equity, hit)
        for it in stop_intents:
            ex = execute_intent(client, it, prices, data, client.get_exchange_info(),
                                cfg, equity, loggers.log_order)
            if ex.get("ok"):
                cycle_trades += sum(1 for o in ex.get("orders", [])
                                    if o.get("success"))
            c = coin_of(it.pair)
            state.set_cooldown(c, now + pd.Timedelta(hours=cfg.cool_h))
            state.set_ext(c, None)

    # 7) daily rebalance (once per UTC day, after 00:00, with catch-up)
    today = now.floor("1D")
    today_str = today.strftime("%Y-%m-%d")
    exinfo = client.get_exchange_info()
    if state.last_rebalance_date != today_str and last_bar >= today:
        if kill_present(cfg):
            logger.info("KILL file present: close-only mode, skipping rebalance")
        else:
            cycle_trades += run_rebalance(
                cfg, client, state, loggers, universe, sig_row, w, price_arr,
                prices, data, exinfo, lr, t, grid.index[0], equity, real_mask, now)

    # 8) activity guard (UTC 20:00, if day too quiet)
    if (now.hour >= 20 and state.day_trade_date != today_str
            and state.activity_done_date != today_str and not kill_present(cfg)
            and state.day_trade_count < cfg.min_daily_trades):
        cycle_trades += run_activity_guard(
            cfg, client, state, loggers, universe, w, price_arr, prices, data,
            exinfo, sig_row, real_mask, lr, t, grid.index[0], equity, now)

    # 9) equity peak (14-day window) + snapshot
    peak = state.peak
    rows = loggers.equity.recent_equity(int(cfg.dd_window_h / 24))
    vals = [float(r["equity"]) for r in rows if r.get("equity")]
    if vals:
        peak = max(peak, max(vals), equity)
    else:
        peak = peak_update(peak, equity)
    state.peak = peak
    pos_values = {u["coin"]: float(qty[i] * price_arr[i])
                  for i, u in enumerate(universe) if np.isfinite(price_arr[i])}
    loggers.equity.write(now, equity, cash, pos_values)

    # 10) daily trade counter + heartbeat + state
    if state.day_trade_date != today_str:
        state.day_trade_date = today_str
        state.day_trade_count = 0
    state.day_trade_count += cycle_trades
    state.last_heartbeat = now.isoformat()
    state.save(cfg.state_file)
    write_heartbeat(cfg.heartbeat_file, {
        "time": now.isoformat(), "equity": equity, "trades_today": state.day_trade_count,
        "last_rebalance_date": state.last_rebalance_date, "live": cfg.live,
    })


# --------------------------------------------------------------------------- #
# loop
# --------------------------------------------------------------------------- #
def next_sleep(bar_min: int, delay: int = 20) -> float:
    now = utcnow()
    bucket = now.floor(f"{bar_min}min")
    nxt = bucket + pd.Timedelta(minutes=bar_min) + pd.Timedelta(seconds=delay)
    if nxt <= now:
        nxt = nxt + pd.Timedelta(minutes=bar_min)
    return max(0.0, (nxt - now).total_seconds())


def run(cfg: Config, client, state: State, loggers: Loggers,
        universe: List[Dict[str, str]], immediate: bool = True) -> None:
    """Run the 7x24 loop.  ``immediate`` runs one cycle right away on start."""
    logging.info("scheduler start (live=%s, universe=%d coins)",
                 cfg.live, len(universe))
    first = True
    while True:
        try:
            if first and immediate:
                cycle(cfg, client, state, loggers, universe)
            else:
                cycle(cfg, client, state, loggers, universe)
        except Exception as e:  # noqa: BLE001 - never let the loop die
            logger.exception("cycle error: %s", e)
        first = False
        time.sleep(next_sleep(cfg.bar_min))
