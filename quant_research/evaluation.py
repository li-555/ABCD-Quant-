from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import pearsonr, spearmanr


def calculate_ic(factor: pd.Series, fwd_returns: pd.Series) -> float:
    if len(factor) < 2:
        return np.nan
    return float(pearsonr(factor, fwd_returns).statistic)


def calculate_rank_ic(factor: pd.Series, fwd_returns: pd.Series) -> float:
    if len(factor) < 2:
        return np.nan
    return float(spearmanr(factor, fwd_returns).statistic)


def evaluate_factor_predictability(df: pd.DataFrame, factor_col: str) -> dict[str, float]:
    """Evaluate IC/RankIC and cross-sectional predictive regression stats."""
    req = {factor_col, "ret_fwd_1d", "date"}
    missing = req - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns for evaluation: {sorted(missing)}")

    # Cross-sectional daily IC
    daily = (
        df.groupby("date")
        .apply(lambda x: pd.Series({
            "ic": calculate_ic(x[factor_col], x["ret_fwd_1d"]),
            "rank_ic": calculate_rank_ic(x[factor_col], x["ret_fwd_1d"]),
        }))
        .dropna()
    )

    x = sm.add_constant(df[factor_col].astype(float))
    y = df["ret_fwd_1d"].astype(float)
    model = sm.OLS(y, x, missing="drop").fit()

    return {
        "mean_ic": float(daily["ic"].mean()) if not daily.empty else np.nan,
        "mean_rank_ic": float(daily["rank_ic"].mean()) if not daily.empty else np.nan,
        "t_value": float(model.tvalues.iloc[1]),
        "p_value": float(model.pvalues.iloc[1]),
        "r_squared": float(model.rsquared),
    }


def calculate_sharpe(returns: pd.Series, annualization: int = 252) -> float:
    vol = returns.std(ddof=0)
    if vol == 0 or np.isnan(vol):
        return np.nan
    return float(np.sqrt(annualization) * returns.mean() / vol)


def calculate_drawdown(equity_curve: pd.Series) -> pd.Series:
    running_max = equity_curve.cummax()
    return equity_curve / running_max - 1.0


def calculate_turnover(weights: pd.DataFrame) -> pd.Series:
    return weights.diff().abs().sum(axis=1).fillna(0.0)


def summarize_performance(strategy_returns: pd.Series, benchmark_returns: pd.Series | None = None) -> dict[str, float]:
    equity = (1 + strategy_returns).cumprod()
    dd = calculate_drawdown(equity)
    results = {
        "total_return": float(equity.iloc[-1] - 1.0),
        "sharpe": calculate_sharpe(strategy_returns),
        "max_drawdown": float(dd.min()),
    }
    if benchmark_returns is not None:
        bench_eq = (1 + benchmark_returns).cumprod()
        results["benchmark_total_return"] = float(bench_eq.iloc[-1] - 1.0)
        results["active_return"] = results["total_return"] - results["benchmark_total_return"]
    return results
