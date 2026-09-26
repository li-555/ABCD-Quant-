from __future__ import annotations

import pandas as pd

from .evaluation import calculate_drawdown, calculate_turnover, summarize_performance


def backtest_strategy(
    df: pd.DataFrame,
    weights: pd.DataFrame,
    transaction_cost_bps: float = 5.0,
    slippage_bps: float = 2.0,
    benchmark_returns: pd.Series | None = None,
) -> dict[str, pd.DataFrame | pd.Series | dict[str, float]]:
    """Backtest strategy using lagged weights and cost model."""
    merged = df[["date", "asset", "ret_fwd_1d"]].merge(weights, on=["date", "asset"], how="inner")
    pnl = (
        merged.groupby("date")
        .apply(lambda x: (x["weight"] * x["ret_fwd_1d"]).sum())
        .rename("gross_return")
    )

    w_pivot = weights.pivot(index="date", columns="asset", values="weight").fillna(0.0).sort_index()
    turnover = calculate_turnover(w_pivot)
    cost_rate = (transaction_cost_bps + slippage_bps) / 10000.0
    costs = turnover * cost_rate

    net = (pnl - costs).rename("strategy_return")
    equity = (1 + net).cumprod().rename("equity_curve")
    drawdown = calculate_drawdown(equity).rename("drawdown")

    perf = summarize_performance(net, benchmark_returns=benchmark_returns)
    perf["avg_turnover"] = float(turnover.mean())

    results = pd.concat([pnl, costs.rename("cost"), net, equity, drawdown], axis=1).reset_index()
    return {
        "daily_results": results,
        "turnover": turnover,
        "performance": perf,
    }
