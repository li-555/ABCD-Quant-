"""Modular quantitative research toolkit for factor analysis and backtesting."""

from .backtest import backtest_strategy
from .data import clean_market_data, load_market_data, make_synthetic_data
from .evaluation import (
    calculate_drawdown,
    calculate_ic,
    calculate_rank_ic,
    calculate_sharpe,
    calculate_turnover,
    evaluate_factor_predictability,
)
from .factors import compute_example_factors
from .portfolio import build_portfolio_weights
from .reporting import save_experiment_results
from .signals import combine_factors_to_signal

__all__ = [
    "load_market_data",
    "clean_market_data",
    "make_synthetic_data",
    "compute_example_factors",
    "evaluate_factor_predictability",
    "calculate_ic",
    "calculate_rank_ic",
    "calculate_sharpe",
    "calculate_drawdown",
    "calculate_turnover",
    "combine_factors_to_signal",
    "build_portfolio_weights",
    "backtest_strategy",
    "save_experiment_results",
]
