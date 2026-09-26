import pandas as pd

from quant_research.data import clean_market_data, make_synthetic_data
from quant_research.multi_strategy import (
    BasicRiskManager,
    ExecutionSimulator,
    MultiFactorStrategy,
    MultiStrategyPipeline,
    PairTradingPlaceholderStrategy,
    PortfolioConstructor,
    StrategyCombiner,
    StrategySignal,
    TechnicalStrategy,
    dict_series_to_wide,
)


def _make_panel_data(n_assets: int = 6, n_days: int = 100, seed: int = 9) -> dict[str, pd.DataFrame]:
    clean = clean_market_data(make_synthetic_data(n_assets=n_assets, n_days=n_days, seed=seed))
    close = clean.pivot(index="date", columns="asset", values="close").sort_index()
    volume = clean.pivot(index="date", columns="asset", values="volume").sort_index()
    return {"close": close, "volume": volume}


def test_strategy_signal_contract_and_legacy_adapter():
    idx = pd.date_range("2024-01-01", periods=3)
    legacy = {
        "A": pd.Series([1.0, 0.0, -1.0], index=idx),
        "B": pd.Series([None, 0.5, 0.0], index=idx),
    }
    wide = dict_series_to_wide(legacy, fill_missing=False)
    signal = StrategySignal(name="x", values=wide)

    assert signal.values.index.name == "date"
    assert pd.isna(signal.values.loc[idx[0], "B"])


def test_multifactor_and_technical_strategies_generate_wide_signals():
    data = _make_panel_data()

    mf = MultiFactorStrategy(prefer_zoo_engine=False).generate(data)
    tech = TechnicalStrategy().generate(data)

    assert isinstance(mf.values, pd.DataFrame)
    assert isinstance(tech.values, pd.DataFrame)
    assert set(mf.values.columns) == set(data["close"].columns)
    assert set(tech.values.columns) == set(data["close"].columns)


def test_strategy_combiner_alignment_nan_policy_and_bounds():
    idx_a = pd.date_range("2024-01-01", periods=2)
    idx_b = pd.date_range("2024-01-02", periods=2)
    a = StrategySignal("a", pd.DataFrame({"X": [1.0, 1.0]}, index=idx_a))
    b = StrategySignal("b", pd.DataFrame({"X": [1.0, -1.0]}, index=idx_b))

    combiner = StrategyCombiner(weights={"a": 1.0, "b": 1.0}, nan_policy="ignore", clip=(-0.5, 0.5))
    out = combiner.combine([a, b]).values

    assert out.index.min() == idx_a.min()
    assert out.index.max() == idx_b.max()
    assert (out.abs() <= 0.5).all().all()


def test_portfolio_constructor_and_risk_limits():
    idx = pd.date_range("2024-01-01", periods=2)
    signal = pd.DataFrame({"A": [1.0, 0.2], "B": [1.0, -0.7], "C": [0.0, -0.1]}, index=idx)

    portfolio = PortfolioConstructor(leverage=1.0, max_abs_weight=0.6).construct(signal)
    risked = BasicRiskManager(max_abs_weight=0.3, max_gross_exposure=0.8, max_net_exposure=0.2).apply(portfolio)

    assert (risked.abs() <= 0.300001).all().all()
    assert (risked.abs().sum(axis=1) <= 0.800001).all()
    assert (risked.sum(axis=1).abs() <= 0.200001).all()


def test_execution_simulator_outputs_trades_and_costs():
    idx = pd.date_range("2024-01-01", periods=2)
    target = pd.DataFrame({"A": [0.2, 0.1], "B": [-0.2, 0.0]}, index=idx)

    trades = ExecutionSimulator(transaction_cost_bps=10, slippage_bps=5).execute(target)

    assert not trades.empty
    assert {"date", "asset", "trade_weight", "cost"}.issubset(trades.columns)
    assert (trades["cost"] > 0.0).all()


def test_multistrategy_pipeline_end_to_end():
    data = _make_panel_data(n_assets=5, n_days=90, seed=11)

    pipeline = MultiStrategyPipeline(
        strategies=[MultiFactorStrategy(prefer_zoo_engine=False), TechnicalStrategy(), PairTradingPlaceholderStrategy()],
        combiner=StrategyCombiner(weights={"multifactor": 0.6, "technical": 0.3, "pair_trading": 0.1}, nan_policy="ignore"),
        portfolio_constructor=PortfolioConstructor(leverage=1.0, max_abs_weight=0.2, min_signal_abs=0.05),
        risk_manager=BasicRiskManager(max_abs_weight=0.15, max_gross_exposure=1.0, max_net_exposure=0.25),
        execution_engine=ExecutionSimulator(),
    )

    result = pipeline.run(data)

    assert {"strategy_signals", "combined_signal", "target_weights", "risk_adjusted_weights", "trades"}.issubset(result.keys())
    assert not result["combined_signal"].empty
