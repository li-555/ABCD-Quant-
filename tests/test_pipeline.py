from quant_research.backtest import backtest_strategy
from quant_research.data import clean_market_data, make_synthetic_data
from quant_research.evaluation import evaluate_factor_predictability
from quant_research.factors import compute_example_factors
from quant_research.portfolio import build_portfolio_weights
from quant_research.signals import combine_factors_to_signal


def test_factor_and_backtest_pipeline_shapes():
    raw = make_synthetic_data(n_assets=8, n_days=90, seed=7)
    clean = clean_market_data(raw)
    factored = compute_example_factors(clean, window=10)

    factor_cols = [
        "factor_momentum",
        "factor_mean_reversion",
        "factor_volatility",
        "factor_volume_price_corr",
    ]

    metrics = evaluate_factor_predictability(factored, "factor_momentum")
    assert "mean_ic" in metrics
    assert "mean_rank_ic" in metrics

    signaled = combine_factors_to_signal(factored, factor_cols)
    weights = build_portfolio_weights(signaled)

    benchmark = factored.groupby("date")["ret_fwd_1d"].mean()
    result = backtest_strategy(factored, weights, benchmark_returns=benchmark)

    daily = result["daily_results"]
    assert not daily.empty
    assert {"gross_return", "cost", "strategy_return", "equity_curve", "drawdown"}.issubset(daily.columns)
    assert isinstance(result["performance"], dict)
    assert "sharpe" in result["performance"]


def test_portfolio_weights_are_bounded_and_sum_controlled():
    raw = make_synthetic_data(n_assets=10, n_days=50, seed=3)
    clean = clean_market_data(raw)
    factored = compute_example_factors(clean, window=8)
    factor_cols = [
        "factor_momentum",
        "factor_mean_reversion",
        "factor_volatility",
        "factor_volume_price_corr",
    ]
    signaled = combine_factors_to_signal(factored, factor_cols)
    weights = build_portfolio_weights(signaled)

    pivot = weights.pivot(index="date", columns="asset", values="weight").fillna(0.0)
    gross = pivot.abs().sum(axis=1)
    assert (gross <= 1.0000001).all()
