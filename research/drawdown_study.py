"""Bounded drawdown reduction study, reusing the existing cost-aware replay.

python -m research.drawdown_study --output outputs/drawdown_study
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import yaml

from bot.config.settings import Config
from research.factor_ablation import replay, metrics


def qualifies(candidate, volume, original, protocol):
    required_return = (volume["return"] * protocol["minimum_return_retention"]
                       if volume["return"] > 0 else volume["return"])
    return (candidate["max_drawdown"] <= volume["max_drawdown"] - protocol["minimum_drawdown_reduction"]
            and candidate["max_drawdown"] <= original["max_drawdown"]
            and candidate["return"] >= required_return
            and candidate["sharpe"] >= volume["sharpe"] - protocol["maximum_sharpe_loss"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="cache/factor_history")
    parser.add_argument("--recent-data", default="cache/drawdown_recent")
    parser.add_argument("--output", default="outputs/drawdown_study")
    parser.add_argument("--protocol", default="research/drawdown_protocol.json")
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    protocol_bytes = Path(args.protocol).read_bytes()
    protocol = json.loads(protocol_bytes)
    universe = yaml.safe_load(Path("bot/config/universe.yaml").read_text())["coins"]
    frames, manifest = {}, {}
    for item in universe:
        coin = item["coin"]
        path = Path(args.data) / f"{coin}.csv"
        live = Path(args.recent_data) / f"ohlcv_{item['binance_symbol']}_30m.csv"
        pieces = []
        for f in (path, live):
            raw = pd.read_csv(f, index_col=0)
            raw.index = pd.to_datetime(raw.index, utc=True)
            pieces.append(raw)
            manifest[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
        # Preserve the earlier checksum-verified archive when ranges overlap.
        raw = pd.concat(pieces)
        frames[coin] = raw[~raw.index.duplicated(keep="first")].sort_index()
    idx = pd.date_range(min(f.index.min() for f in frames.values()),
                        pd.Timestamp(protocol["new_short_check"][1], tz="UTC"), freq="30min")
    bars = {key: pd.DataFrame({c: f[key] for c, f in frames.items()}).reindex(idx)
            for key in ("open", "close", "volume")}
    if any(frame.isna().any().any() for frame in bars.values()):
        raise ValueError("missing history; do not fill gaps silently")
    cfg = Config(activity_enabled=False)
    volume = replace(cfg, factor_volume_weight=.2)
    configs = {"original": cfg, "volume20": volume,
               **{n: replace(volume, **v) for n,v in protocol["candidates"].items()}}
    records, scores = [], {}

    def run(name, period, bounds, multiplier):
        data = {k: v.loc[:pd.Timestamp(bounds[1], tz="UTC")] for k,v in bars.items()}
        result = replay(data, configs[name], *bounds, cost_multiplier=multiplier)
        m = metrics(result)
        scores[name, period, multiplier] = m
        record = {"candidate": name, "period": period, "cost_multiplier": multiplier, **m}
        records.append(record)
        stem = f"{period}_{name}_{multiplier:g}x"
        pd.concat([result[k] for k in ("equity", "cost", "turnover", "gross")], axis=1).to_csv(out/f"{stem}.csv")
        result["trades"].to_csv(out/f"{stem}_trades.csv", index=False)
        print(json.dumps(record), flush=True)
        return m

    for period, bounds in protocol["selection_periods"].items():
        for multiplier in protocol["cost_multipliers"]:
            for name in configs:
                run(name, period, bounds, multiplier)
    eligible = []
    for name in protocol["candidates"]:
        if all(qualifies(scores[name, p, m], scores["volume20", p, m], scores["original", p, m], protocol)
               for p in protocol["selection_periods"] for m in protocol["cost_multipliers"]):
            eligible.append(name)
    selected = max(eligible, key=lambda n: min(scores[n,p,m]["sharpe"]
                   for p in protocol["selection_periods"] for m in protocol["cost_multipliers"])) if eligible else None
    decision = {"selected": selected, "eligible": eligible,
                "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest()}
    (out/'selection_before_checks.json').write_text(json.dumps(decision, indent=2))
    keep = bool(selected)
    names = ["original", "volume20"] + ([selected] if selected else [])
    for period in ("reused_diagnostic", "new_short_check"):
        for m in protocol["cost_multipliers"]:
            for name in names:
                run(name, period, protocol[period], m)
            if selected and period == "reused_diagnostic":
                keep = keep and qualifies(scores[selected,period,m], scores["volume20",period,m], scores["original",period,m], protocol)
    decision.update(paper_candidate=selected if keep else None,
                    reason="passed predeclared retrospective gates" if keep else "no promotion under predeclared gates",
                    unseen_check_days=28, live_approved=False)
    (out/'decision.json').write_text(json.dumps(decision, indent=2))
    (out/'protocol.json').write_bytes(protocol_bytes)
    (out/'data_sha256.json').write_text(json.dumps(manifest, indent=2))
    pd.DataFrame(records).to_csv(out/'metrics.csv', index=False)
    print(json.dumps(decision, indent=2), flush=True)


if __name__ == "__main__":
    main()
