"""Strategy -> validated portfolio targets -> Roostoo orders.

Strategies receive data, never credentials or a trading client. One runner owns
one spot account; combine strategies upstream instead of racing account writers.
"""
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from bot.config.settings import Config
from quant_research.multi_strategy.strategy import StrategySignal


@dataclass(frozen=True)
class TargetWeights:
    name: str
    asof: pd.Timestamp
    values: pd.Series  # complete desired portfolio; absent configured coins -> 0


@dataclass(frozen=True)
class MarketContext:
    asof: pd.Timestamp
    data: dict[str, pd.DataFrame]
    wallet: dict
    exchange_info: dict
    ticker: dict


def to_targets(signal, context: MarketContext, coins: list[str], cfg: Config):
    """Scores and target weights have explicit, distinct semantics."""
    if isinstance(signal, StrategySignal):
        frame = signal.values
        if frame.empty or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError("signal must have a nonempty unique sorted timestamp index")
        if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise ValueError("signal timestamps must be timezone-aware")
        if frame.index[-1] > context.asof:
            raise ValueError("future-dated strategy signal")
        asof, values, name = frame.index[-1], frame.iloc[-1].copy(), signal.name
        if signal.confidence is not None:
            confidence = signal.confidence
            if not confidence.index.equals(frame.index) or not confidence.columns.equals(frame.columns):
                raise ValueError("confidence must align exactly with signal values")
            last = confidence.iloc[-1].astype(float)
            if not np.isfinite(last.to_numpy()).all() or ((last < 0) | (last > 1)).any():
                raise ValueError("confidence must be finite and in [0, 1]")
            values *= last
        mode = "scores"
    elif isinstance(signal, TargetWeights):
        asof, values, name, mode = pd.Timestamp(signal.asof), signal.values.copy(), signal.name, "weights"
    else:
        raise TypeError("strategy must return StrategySignal or TargetWeights")
    if asof.tzinfo is None or asof > context.asof:
        raise ValueError("invalid strategy timestamp")
    if (context.asof - asof).total_seconds() > cfg.staleness_minutes * 60:
        raise ValueError("stale strategy signal")
    if not name or values.index.has_duplicates or not set(values.index).issubset(coins):
        raise ValueError("strategy name and unique configured coin symbols required")
    values = values.astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("non-finite strategy output; warm up before submitting")
    if mode == "weights" and ((values < 0).any() or (values > 1).any()):
        raise ValueError("spot target weights must be between zero and one")
    values = values.reindex(coins, fill_value=0.0).clip(lower=0)
    budget = min(cfg.coin_cap, cfg.max_gross, 1.0)
    if mode == "scores" and values.sum() > 0:
        values *= budget / values.sum()
    values = values.clip(upper=cfg.single_long)
    if values.sum() > budget:
        values *= budget / values.sum()
    return TargetWeights(name, asof, values)


def portfolio(context, coins, cfg):
    wallet, ticker = context.wallet, context.ticker
    # Unknown holdings must not disappear from equity or risk calculations.
    for coin, item in wallet.items():
        held = float(item.get("Free", 0)) + float(item.get("Lock", 0))
        if not math.isfinite(held) or held < 0:
            raise ValueError("invalid wallet quantity")
        if coin != "USD" and coin not in coins and held > 0:
            raise ValueError(f"unmanaged holding {coin}; include it in the universe")
    free_cash = float(wallet.get("USD", {}).get("Free", 0))
    equity = free_cash + float(wallet.get("USD", {}).get("Lock", 0))
    quantities, prices = {}, {}
    for coin in coins:
        item = wallet.get(coin, {})
        quantities[coin] = float(item.get("Free", 0)) + float(item.get("Lock", 0))
        price = float(ticker.get(coin + "/USD", {}).get("LastPrice", float("nan")))
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"missing valid Roostoo price for {coin}")
        prices[coin] = price
        equity += quantities[coin] * price
    equity -= cfg.cash_reserve_usd
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("no positive strategy equity")
    return equity, free_cash, quantities, prices


class StrategyBridge:
    """Default: return an order plan only. execute=True additionally needs LIVE.

    Market orders only: no unsafe cancel-and-resubmit path. Durable write-ahead
    claims stop duplicate submissions after retries/restarts. Uncertain results
    block later batches until an operator reconciles the journal with Roostoo.
    """
    def __init__(self, cfg, client, coins, journal_path):
        self.cfg, self.client, self.coins = cfg, client, list(coins)
        self.journal_path = Path(journal_path)

    def plan(self, signal, context):
        target = to_targets(signal, context, self.coins, self.cfg)
        equity, cash, qty, prices = portfolio(context, self.coins, self.cfg)
        orders = []
        for coin in self.coins:
            pair = coin + "/USD"
            diff = target.values[coin] * equity - qty[coin] * prices[coin]
            if abs(diff) < self.cfg.min_trade * equity:
                continue
            info = context.exchange_info.get("TradePairs", {}).get(pair, {})
            if not info.get("CanTrade", False):
                raise ValueError(f"pair is not tradable: {pair}")
            orders.append({"pair": pair, "side": "BUY" if diff > 0 else "SELL",
                           "notional": abs(float(diff))})
        orders.sort(key=lambda x: x["side"] != "SELL")
        return target, equity, orders

    def submit_signal(self, signal, context, *, execute=False):
        target, equity, orders = self.plan(signal, context)
        result = {"strategy": target.name, "asof": target.asof.isoformat(),
                  "targets": target.values.to_dict(), "orders": orders, "submitted": 0,
                  "status": "preview"}
        if not execute:
            return result
        if not self.cfg.live or getattr(self.client, "read_only", False):
            raise RuntimeError("execution requires LIVE=1 and an order-enabled client")
        if Path(self.cfg.kill_file).exists():
            return {**result, "status": "kill_file"}
        if context.exchange_info.get("IsRunning") is not True:
            raise ValueError("Roostoo exchange is not running")
        if abs((pd.Timestamp.now(tz='UTC') - context.asof).total_seconds()) > self.cfg.staleness_minutes * 60:
            raise ValueError("execution context is stale")
        # Serialize claims across processes sharing this account journal.
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.journal_path, timeout=5) as db:
            db.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, status TEXT, detail TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS submissions (day TEXT, batch TEXT, pair TEXT, side TEXT, quantity REAL, order_id TEXT, status TEXT)")
            db.execute("BEGIN IMMEDIATE")
            batch = target.name + ":" + target.asof.isoformat()
            if db.execute("SELECT 1 FROM batches WHERE status != 'complete'").fetchone():
                raise RuntimeError("unreconciled batch in journal; inspect Roostoo orders first")
            if db.execute("SELECT 1 FROM batches WHERE id=?", (batch,)).fetchone():
                return {**result, "status": "duplicate"}
            pending = self.client.get_pending_count()
            if not pending.get("Success") or pending.get("TotalPending") != 0:
                raise RuntimeError("pending orders unknown/nonzero; refusing another batch")
            db.execute("INSERT INTO batches VALUES (?, 'running', '')", (batch,))
            db.commit()  # persist BEFORE any order is sent
            try:
                sent = self._execute(target, context, db, batch)
            except Exception as exc:
                db.execute("UPDATE batches SET status='attention', detail=? WHERE id=?",
                           (type(exc).__name__, batch))
                db.commit()
                raise
            db.execute("UPDATE batches SET status='complete' WHERE id=?", (batch,))
            return {**result, "submitted": sent, "status": "complete"}

    def _execute(self, target, context, db, batch):
        # Refresh balances and quotes after EACH fill; never spend locked cash
        # or the cash reserve, and never sell more than currently free holdings.
        sent = 0
        for side in ("SELL", "BUY"):
            for coin in self.coins:
                while True:
                    balance = self.client.get_balance()
                    ticker = self.client.get_ticker()
                    if not balance.get("Success") or not ticker.get("Success"):
                        raise RuntimeError("account refresh failed during batch")
                    current = MarketContext(context.asof, context.data,
                        balance.get("SpotWallet", balance.get("Wallet", {})),
                        context.exchange_info, ticker.get("Data", {}))
                    equity, cash, qty, prices = portfolio(current, self.coins, self.cfg)
                    diff = target.values[coin] * equity - qty[coin] * prices[coin]
                    if (diff > 0) != (side == "BUY") or abs(diff) < self.cfg.min_trade * equity:
                        break
                    pair = coin + "/USD"
                    info = current.exchange_info["TradePairs"][pair]
                    price = prices[coin]
                    notional = min(abs(diff), self.cfg.max_order_frac * equity)
                    if side == "BUY":
                        rate = (self.cfg.taker_bps + self.cfg.slip_bps) / 10000
                        quote = float(current.ticker[pair].get("MinAsk") or price)
                        if not math.isfinite(quote) or quote <= 0:
                            raise ValueError("invalid ask price")
                        price = max(price, quote)
                        notional = min(notional, max(0, cash-self.cfg.cash_reserve_usd)/(1+rate))
                    amount = notional / price
                    if side == "SELL":
                        amount = min(amount, float(current.wallet.get(coin, {}).get("Free", 0)))
                    scale = 10 ** int(info["AmountPrecision"])
                    amount = math.floor(amount * scale) / scale
                    if amount <= 0 or amount * price < float(info.get("MiniOrder", 1)):
                        break
                    day = pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d')
                    count = db.execute("SELECT COUNT(*) FROM submissions WHERE day=?", (day,)).fetchone()[0]
                    if count >= self.cfg.max_daily_trades:
                        raise RuntimeError("daily order cap reached")
                    record = db.execute("INSERT INTO submissions VALUES (?, ?, ?, ?, ?, '', 'sending')",
                                        (day, batch, pair, side, amount)).lastrowid
                    db.commit()
                    response = self.client.place_order(pair, side, "MARKET", amount)
                    sent += 1
                    detail = response.get("OrderDetail", {})
                    db.execute("UPDATE submissions SET order_id=?, status=? WHERE rowid=?",
                               (str(detail.get('OrderID', '')), str(detail.get('Status', 'unknown')), record))
                    db.commit()
                    if not response.get("Success") or detail.get("Status") != "FILLED":
                        raise RuntimeError("order rejected/pending/uncertain; reconcile before retry")
                    # Avoid loops if an exchange returns FILLED without changing the book.
                    if not float(detail.get("FilledQuantity", 0)) > 0:
                        raise RuntimeError("missing fill quantity; reconcile before retry")
        return sent
