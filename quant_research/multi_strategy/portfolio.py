from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class PortfolioConstructor:
    """Convert combined signal panel into target portfolio weights."""

    leverage: float = 1.0
    max_abs_weight: float = 0.2
    min_signal_abs: float = 0.0

    def construct(self, combined_signal: pd.DataFrame) -> pd.DataFrame:
        signal = combined_signal.astype(float).copy()
        if self.min_signal_abs > 0.0:
            signal = signal.where(signal.abs() >= self.min_signal_abs, 0.0)

        gross = signal.abs().sum(axis=1).replace(0.0, pd.NA)
        target = signal.div(gross, axis=0).fillna(0.0) * self.leverage
        target = target.clip(lower=-self.max_abs_weight, upper=self.max_abs_weight)

        renorm = target.abs().sum(axis=1)
        safe_renorm = renorm.replace(0.0, pd.NA)
        scale = (self.leverage / safe_renorm).where(renorm > self.leverage, 1.0).fillna(1.0)
        target = target.mul(scale, axis=0)
        target.index.name = "date"
        return target
