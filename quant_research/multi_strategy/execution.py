from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class ExecutionSimulator:
    """Translate target weights into trades with isolated cost model."""

    transaction_cost_bps: float = 5.0
    slippage_bps: float = 2.0

    def execute(self, target_weights: pd.DataFrame, current_weights: pd.DataFrame | None = None) -> pd.DataFrame:
        target = target_weights.astype(float).copy().sort_index()
        current = (
            current_weights.astype(float).reindex_like(target).fillna(0.0)
            if current_weights is not None
            else target.shift(1).fillna(0.0)
        )
        trades = (target - current).stack().rename("trade_weight").reset_index()
        trades.columns = ["date", "asset", "trade_weight"]
        trades = trades[trades["trade_weight"] != 0.0].reset_index(drop=True)

        rate = (self.transaction_cost_bps + self.slippage_bps) / 10000.0
        trades["cost"] = trades["trade_weight"].abs() * rate
        return trades
