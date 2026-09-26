from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def save_experiment_results(
    output_dir: str | Path,
    daily_results: pd.DataFrame,
    factor_summary: pd.DataFrame,
    performance: dict[str, float],
) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    daily_results.to_csv(out / "daily_results.csv", index=False)
    factor_summary.to_csv(out / "factor_summary.csv", index=False)
    with (out / "performance.json").open("w", encoding="utf-8") as f:
        json.dump(performance, f, indent=2)

    report = [
        "# Quant Research Experiment Report",
        "",
        "## Performance",
    ]
    report.extend([f"- {k}: {v:.6f}" if isinstance(v, float) else f"- {k}: {v}" for k, v in performance.items()])
    report.extend([
        "",
        "## Output Files",
        "- daily_results.csv",
        "- factor_summary.csv",
        "- performance.json",
        "- equity_drawdown.png",
        "- factor_performance.png",
        "- factor_correlation.png",
    ])
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")
