"""Fixed-protocol factor ablation using the live bot's signal/portfolio/risk code.

python -m research.factor_ablation --data cache/factor_history --output outputs/factor_study
No exchange client is imported or instantiated. No orders are sent.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from bot.config.settings import Config
from bot.strategy.signals import build_signals
from bot.strategy.portfolio import compute_target_weights
from bot.strategy.risk import stop_hits, update_extremes


def replay(bars, cfg, start, end, cost_multiplier=1.0, signals=None):
    """Cash/quantity ledger, completed-bar decision -> following bar open fill.

    Uses all taker fees and adverse slippage, sells before buys, never borrows.
    Stops are close-based (the v1 rule). No optimistic intrabar stop price.
    Activity guard is excluded explicitly in both baseline and candidates.
    """
    if cfg.activity_enabled:
        raise ValueError("factor replay requires activity_enabled=False")
    P, O = bars["close"], bars["open"]
    if not P.index.equals(O.index) or not P.columns.equals(O.columns):
        raise ValueError("unaligned execution data")
    if not np.isfinite(P.to_numpy()).all() or not np.isfinite(O.to_numpy()).all():
        raise ValueError("replay requires complete prices; no silent forward fill")
    start, end = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    if start not in P.index or end not in P.index:
        raise ValueError("evaluation boundaries must exist in the price grid")
    sg = signals if signals is not None else build_signals(P, cfg, bars.get("volume"))
    values = {k: sg[k].to_numpy() for k in ("S", "S_slow", "trend", "sig_d")}
    lr = np.log(P).diff()
    px, op = P.to_numpy(), O.to_numpy()
    n = P.shape[1]
    qty = np.zeros(n)
    reserve = cfg.cash_reserve_usd
    cash = initial = cfg.paper_start_equity - reserve
    ext = np.full(n, np.nan)
    entry = np.full(n, -10**9)
    cooldown = np.full(n, -10**9)
    breaker = -1
    equity = [initial]
    dates = [start]
    costs, turnover, gross = [0.0], [0.0], [0.0]
    trades = []
    fee = cfg.taker_bps * cost_multiplier / 1e4
    slip = cfg.slip_bps * cost_multiplier / 1e4
    window = max(1, cfg.h_to_bars(cfg.dd_window_h))
    a, b = P.index.get_loc(start), P.index.get_loc(end)
    for i in range(a + 1, b + 1):
        t = i - 1
        stamp = P.index[t]
        prev_eq = cash + qty @ px[t]
        w = qty * px[t] / prev_eq
        sig_row = {k: v[t] for k, v in values.items()}
        ext = update_extremes(ext, px[t], qty > 0)
        hit = stop_hits(w, px[t], ext, sig_row["sig_d"], cfg)
        desired = w.copy()
        desired[hit] = 0
        cooldown[hit] = t + cfg.h_to_bars(cfg.cool_h)
        is_rebalance = stamp.hour == cfg.rebalance_hour_utc and stamp.minute == 0
        if is_rebalance:
            peak = max(equity[-window:])
            desired, _, breaker = compute_target_weights(
                sig_row, desired, cooldown, entry, breaker, prev_eq, peak,
                lr, np.ones(n, bool), t, cfg)
        # Only adjust assets whose decision changed; untouched holdings drift.
        changed = np.abs(desired - w) > 1e-12
        fill_eq = cash + qty @ op[i]
        desired_qty = qty.copy()
        desired_qty[changed] = desired[changed] * fill_eq / op[i, changed]
        delta = desired_qty - qty
        total_cost = total_turn = 0.0
        # Executable quantities: sales fund buys, cash includes fees/slippage.
        for j in sorted(np.where(changed)[0], key=lambda j: delta[j]):
            old_qty = qty[j]
            dq = delta[j]
            execution = op[i, j] * (1 + slip if dq > 0 else 1 - slip)
            if dq > 0:
                dq = min(dq, max(cash, 0) / (execution * (1 + fee)))
            else:
                dq = max(dq, -qty[j])
            if abs(dq) < 1e-12:
                continue
            commission = abs(dq) * execution * fee
            cash -= dq * execution + commission
            qty[j] += dq
            if qty[j] < 1e-10:
                qty[j] = 0
                entry[j], ext[j] = -10**9, np.nan
            elif old_qty == 0:
                entry[j], ext[j] = t, execution
            total_cost += commission + abs(dq) * abs(execution - op[i, j])
            total_turn += abs(dq) * op[i, j] / fill_eq
            trades.append({"decision_time": stamp, "fill_time": stamp,
                           "fill_bar_close": P.index[i], "asset": P.columns[j],
                           "quantity": dq, "price": execution, "commission": commission,
                           "reason": "stop" if hit[j] else "rebalance"})
        if cash < -1e-7 or (qty < 0).any():
            raise AssertionError("cash or position constraint violated")
        eq = cash + qty @ px[i]
        equity.append(float(eq))
        dates.append(P.index[i])
        costs.append(total_cost)
        turnover.append(total_turn)
        gross.append(float(qty @ px[i] / (eq + reserve)))
    return {"equity": pd.Series(np.asarray(equity) + reserve, index=dates, name="equity"),
            "cost": pd.Series(costs, index=dates, name="cost"),
            "turnover": pd.Series(turnover, index=dates, name="turnover"),
            "gross": pd.Series(gross, index=dates, name="gross"),
            "trades": pd.DataFrame(trades)}


def metrics(result):
    eq = result["equity"]
    # UTC midnight endpoints; final boundary is included exactly once.
    daily = eq.loc[(eq.index.hour == 0) & (eq.index.minute == 0)]
    r = daily.pct_change().dropna()
    sd = r.std(ddof=1)
    downside = np.sqrt(np.minimum(r, 0).pow(2).mean())
    return {"return": float(eq.iloc[-1] / eq.iloc[0] - 1),
            "sharpe": float(r.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0,
            "sortino": float(r.mean() / downside * np.sqrt(365)) if downside > 0 else 0.0,
            "max_drawdown": float(-(eq / eq.cummax() - 1).min()),
            "turnover": float(result["turnover"].sum()),
            "cost_dollars": float(result["cost"].sum()),
            "trades": len(result["trades"]), "end_equity": float(eq.iloc[-1])}


def qualifies(candidate, baseline, protocol):
    return (candidate["return"] > baseline["return"]
            and candidate["sharpe"] >= baseline["sharpe"] + protocol["minimum_sharpe_gain"]
            and candidate["max_drawdown"] <= baseline["max_drawdown"] + protocol["maximum_extra_drawdown"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="cache/factor_history")
    parser.add_argument("--output", default="outputs/factor_study")
    parser.add_argument("--protocol", default="research/factor_protocol.json")
    args = parser.parse_args()
    root, out = Path(args.data), Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    protocol_bytes = Path(args.protocol).read_bytes()
    protocol = json.loads(protocol_bytes)
    universe = yaml.safe_load(Path("bot/config/universe.yaml").read_text())["coins"]
    raw = {}
    for item in universe:
        df = pd.read_csv(root / f"{item['coin']}.csv", index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        raw[item["coin"]] = df
    bars = {key: pd.DataFrame({c: f[key] for c, f in raw.items()})
            for key in ("open", "high", "low", "close", "volume")}
    cfg = Config(activity_enabled=False)
    configs = {"baseline": cfg, **{name: replace(cfg, **params)
                                  for name, params in protocol["candidates"].items()}}
    records, results = [], {}

    def run(name, period, bounds, mult):
        # Truncate at each evaluation end: screening cannot read holdout prices.
        data = {k: v.loc[:pd.Timestamp(bounds[1], tz="UTC")] for k, v in bars.items()}
        result = replay(data, configs[name], *bounds, cost_multiplier=mult)
        stat = metrics(result)
        record = {"candidate": name, "period": period, "cost_multiplier": mult, **stat}
        records.append(record)
        results[name, period, mult] = result
        stem = f"{period}_{name}_{mult:g}x"
        pd.concat([result[k] for k in ("equity", "cost", "turnover", "gross")], axis=1).to_csv(out / f"{stem}.csv")
        result["trades"].to_csv(out / f"{stem}_trades.csv", index=False)
        print(json.dumps(record), flush=True)
        return stat

    # Development is a predeclared diagnostic, never used to tune parameters.
    for name in configs:
        run(name, "development", protocol["development"], 1.0)
    scores = {}
    for mult in protocol["cost_multipliers"]:
        for name in configs:
            scores[name, mult] = run(name, "validation", protocol["validation"], mult)
    eligible = []
    for name in protocol["candidates"]:
        passed = all(qualifies(scores[name, m], scores["baseline", m], protocol)
                     for m in protocol["cost_multipliers"])
        # Compare contiguous validation equity halves without resetting holdings.
        for left, right in (("2025-01-01", "2025-07-01"), ("2025-07-01", "2026-01-01")):
            def segment(n):
                s = results[n, "validation", 1.0]["equity"].loc[left:right]
                return s.iloc[-1] / s.iloc[0] - 1
            passed = passed and segment(name) > segment("baseline")
        if passed:
            eligible.append(name)
    selected = max(eligible, key=lambda name: scores[name, 1.0]["sharpe"]) if eligible else None
    selection = {"selected_before_holdout": selected, "eligible": eligible,
                 "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest()}
    # Persist selection before any holdout strategy replay.
    (out / "selection_before_holdout.json").write_text(json.dumps(selection, indent=2))
    keep = bool(selected)
    for mult in protocol["cost_multipliers"]:
        baseline = run("baseline", "holdout", protocol["holdout"], mult)
        if selected:
            candidate = run(selected, "holdout", protocol["holdout"], mult)
            keep = keep and qualifies(candidate, baseline, protocol)
    decision = {**selection, "retained": selected if keep else None,
                "recommended_overrides": protocol["candidates"][selected] if keep else {},
                "activity_guard_tested": False,
                "reason": "passed validation and holdout gates" if keep else
                          "no candidate passed all gates; retain original v1 signal weights"}
    (out / "decision.json").write_text(json.dumps(decision, indent=2))
    (out / "recommended_factor_overrides.yaml").write_text(yaml.safe_dump({
        "factor_rs_weight": 0.0, "factor_mr_weight": 0.0,
        "factor_volume_weight": 0.0, "factor_session_weight": 0.0,
        **decision["recommended_overrides"]}))
    pd.DataFrame(records).to_csv(out / "metrics.csv", index=False)
    (out / "protocol.json").write_bytes(protocol_bytes)
    (out / "data_manifest.json").write_bytes((root / "manifest.json").read_bytes())
    print(json.dumps(decision, indent=2), flush=True)


if __name__ == "__main__":
    main()
