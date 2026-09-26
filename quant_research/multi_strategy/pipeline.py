from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quant_research.data import clean_market_data, make_synthetic_data

from .combiner import StrategyCombiner
from .execution import ExecutionSimulator
from .portfolio import PortfolioConstructor
from .risk import BasicRiskManager
from .strategies import MultiFactorStrategy, PairTradingPlaceholderStrategy, TechnicalStrategy
from .strategy import Strategy


@dataclass
class MultiStrategyPipeline:
    """Data -> Features -> Strategies -> Combiner -> Portfolio -> Risk -> Execution."""

    strategies: list[Strategy]
    combiner: StrategyCombiner
    portfolio_constructor: PortfolioConstructor
    risk_manager: BasicRiskManager
    execution_engine: ExecutionSimulator

    def run(self, data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame | dict[str, pd.DataFrame]]:
        generated = [s.generate(data) for s in self.strategies]
        strategy_signals = {s.name: s.values for s in generated}
        combined = self.combiner.combine(generated).values
        target = self.portfolio_constructor.construct(combined)
        risk_adjusted = self.risk_manager.apply(target)
        trades = self.execution_engine.execute(risk_adjusted)
        return {
            "strategy_signals": strategy_signals,
            "combined_signal": combined,
            "target_weights": target,
            "risk_adjusted_weights": risk_adjusted,
            "trades": trades,
        }


def run_multistrategy_example(n_assets: int = 8, n_days: int = 120, seed: int = 7) -> dict[str, pd.DataFrame | dict[str, pd.DataFrame]]:
    raw = make_synthetic_data(n_assets=n_assets, n_days=n_days, seed=seed)
    clean = clean_market_data(raw)
    close = clean.pivot(index="date", columns="asset", values="close").sort_index()
    volume = clean.pivot(index="date", columns="asset", values="volume").sort_index()

    pipeline = MultiStrategyPipeline(
        strategies=[
            MultiFactorStrategy(),
            TechnicalStrategy(),
            PairTradingPlaceholderStrategy(),
        ],
        combiner=StrategyCombiner(
            weights={"multifactor": 0.5, "technical": 0.4, "pair_trading": 0.1},
            normalize_weights=True,
            nan_policy="ignore",
            conflict_mode="weighted_sum",
            clip=(-1.0, 1.0),
        ),
        portfolio_constructor=PortfolioConstructor(leverage=1.0, max_abs_weight=0.2, min_signal_abs=0.05),
        risk_manager=BasicRiskManager(max_abs_weight=0.15, max_gross_exposure=1.0, max_net_exposure=0.3),
        execution_engine=ExecutionSimulator(transaction_cost_bps=5.0, slippage_bps=2.0),
    )
    return pipeline.run({"close": close, "volume": volume})
