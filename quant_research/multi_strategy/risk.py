from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class BasicRiskManager:
    """Basic post-portfolio risk constraints."""

    max_abs_weight: float = 0.15
    max_gross_exposure: float = 1.0
    max_net_exposure: float = 0.3

    def apply(self, target_weights: pd.DataFrame) -> pd.DataFrame:
        w = target_weights.astype(float).clip(-self.max_abs_weight, self.max_abs_weight)

        gross = w.abs().sum(axis=1)
        gross_scale = (self.max_gross_exposure / gross).where(gross > self.max_gross_exposure, 1.0)
        w = w.mul(gross_scale, axis=0)

        net = w.sum(axis=1)
        n_assets = float(max(len(w.columns), 1))
        offset = ((net.abs() - self.max_net_exposure).clip(lower=0.0) * np.sign(net)) / n_assets
        w = w.sub(offset, axis=0).clip(-self.max_abs_weight, self.max_abs_weight)

        w.index.name = "date"
        return w
