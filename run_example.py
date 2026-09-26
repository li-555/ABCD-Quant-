from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_research.backtest import backtest_strategy
from quant_research.data import clean_market_data, make_synthetic_data
from quant_research.evaluation import evaluate_factor_predictability
from quant_research.factors import compute_example_factors
from quant_research.portfolio import build_portfolio_weights
from quant_research.reporting import save_experiment_results
from quant_research.signals import combine_factors_to_signal
from quant_research.visualization import (
    plot_equity_and_drawdown,
    plot_factor_correlation,
    plot_factor_performance,
)
def main() -> None:
    output_dir = Path("outputs")

    raw = make_synthetic_data()
    clean = clean_market_data(raw)
    factored = compute_example_factors(clean)

    factor_cols = [
        "factor_momentum",
        "factor_mean_reversion",
        "factor_volatility",
        "factor_volume_price_corr",
    ]

    summaries = []
    for factor in factor_cols:
        metrics = evaluate_factor_predictability(factored, factor)
        metrics["factor"] = factor
        summaries.append(metrics)
    factor_summary = pd.DataFrame(summaries)

    signaled = combine_factors_to_signal(factored, factor_cols)
    weights = build_portfolio_weights(signaled)

    benchmark = (
        clean.groupby("date")["ret_fwd_1d"].mean().reindex(weights["date"].unique()).fillna(0.0)
    )
    bt = backtest_strategy(factored, weights, transaction_cost_bps=5, slippage_bps=2, benchmark_returns=benchmark)

    plot_equity_and_drawdown(bt["daily_results"], output_dir)
    plot_factor_correlation(factored, factor_cols, output_dir)
    plot_factor_performance(factor_summary.set_index("factor"), output_dir)

    save_experiment_results(output_dir, bt["daily_results"], factor_summary, bt["performance"])
    print("Experiment complete. Outputs written to ./outputs")


if __name__ == "__main__":
    main()
