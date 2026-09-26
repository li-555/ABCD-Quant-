from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class StrategySignal:
    """Normalized strategy output as wide DataFrame (date index, instrument columns)."""

    name: str
    values: pd.DataFrame
    confidence: pd.DataFrame | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.values, pd.DataFrame):
            raise TypeError("values must be a pandas DataFrame")
        values = self.values.copy()
        if values.index.name is None:
            values.index.name = "date"
        object.__setattr__(self, "values", values)


class Strategy(ABC):
    """Common strategy interface."""

    name: str

    @abstractmethod
    def generate(self, data: dict[str, pd.DataFrame]) -> StrategySignal:
        """Generate signal from data/features map."""


def dict_series_to_wide(signals: dict[str, pd.Series], *, fill_missing: bool = False) -> pd.DataFrame:
    """Convert legacy dict[symbol, Series] output to wide panel."""
    if not signals:
        return pd.DataFrame(dtype=float)
    panel = pd.concat(signals, axis=1).sort_index()
    panel.columns = panel.columns.astype(str)
    if fill_missing:
        panel = panel.fillna(0.0)
    panel.index.name = "date"
    return panel.astype(float)
