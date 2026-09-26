from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from quant_research.factors import compute_example_factors
from quant_research.signals import combine_factors_to_signal

from .strategy import Strategy, StrategySignal, dict_series_to_wide


def _extract_close_volume(data: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    close = data.get("close")
    volume = data.get("volume")
    if close is None or volume is None:
        raise ValueError("data must provide wide 'close' and 'volume' DataFrames")
    return close.sort_index(), volume.sort_index()


class LegacyMultiFactorEngine:
    """Legacy-compatible multifactor engine with per-symbol output adapter."""

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        panel = self.compute_signal(data_map)
        return {c: panel[c] for c in panel.columns}

    def compute_signal(self, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
        close, volume = _extract_close_volume(panel)
        stacked = (
            pd.concat(
                [
                    close.stack().rename("close"),
                    volume.stack().rename("volume"),
                ],
                axis=1,
            )
            .reset_index()
            .rename(columns={"level_0": "date", "level_1": "asset"})
            .sort_values(["asset", "date"])
        )
        stacked["ret_1d"] = stacked.groupby("asset")["close"].pct_change()
        stacked["ret_fwd_1d"] = stacked.groupby("asset")["close"].shift(-1) / stacked["close"] - 1.0
        factored = compute_example_factors(stacked.dropna(subset=["ret_1d", "ret_fwd_1d"]))

        factor_cols = [
            "factor_momentum",
            "factor_mean_reversion",
            "factor_volatility",
            "factor_volume_price_corr",
        ]
        sig = combine_factors_to_signal(factored, factor_cols)
        return sig.pivot(index="date", columns="asset", values="signal").sort_index().astype(float)


@dataclass
class MultiFactorStrategy(Strategy):
    name: str = "multifactor"
    prefer_zoo_engine: bool = True
    _legacy_engine: Any = field(default_factory=LegacyMultiFactorEngine)

    def _resolve_engine(self) -> Any:
        if self.prefer_zoo_engine:
            try:
                from quant_research.zoo_signal_engine import SignalEngine as ZooSignalEngine  # type: ignore

                return ZooSignalEngine()
            except Exception:
                pass
        return self._legacy_engine

    def generate(self, data: dict[str, pd.DataFrame]) -> StrategySignal:
        engine = self._resolve_engine()
        if hasattr(engine, "compute_signal"):
            values = engine.compute_signal(data)
        else:
            values = dict_series_to_wide(engine.generate(data), fill_missing=False)

        return StrategySignal(
            name=self.name,
            values=values,
            metadata={"engine": engine.__class__.__name__},
        )


class LegacyTechnicalEngine:
    """Legacy technical signal engine returning dict[symbol, Series]."""

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        close, volume = _extract_close_volume(data_map)
        out: dict[str, pd.Series] = {}
        for symbol in close.columns:
            px = close[symbol].astype(float)
            vol = volume[symbol].astype(float)
            ema_fast = px.ewm(span=12, adjust=False).mean()
            ema_slow = px.ewm(span=26, adjust=False).mean()
            trend = np.sign(ema_fast - ema_slow)

            basis = px.rolling(20).mean()
            stdev = px.rolling(20).std()
            upper = basis + 2.0 * stdev
            lower = basis - 2.0 * stdev
            band_signal = pd.Series(0.0, index=px.index)
            band_signal = band_signal.where(px >= lower, 1.0)
            band_signal = band_signal.where(px <= upper, -1.0)

            delta = px.diff()
            up = delta.clip(lower=0).rolling(14).mean()
            down = (-delta.clip(upper=0)).rolling(14).mean()
            rs = up / down.replace(0.0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            rsi_signal = np.where(rsi < 30, 1.0, np.where(rsi > 70, -1.0, 0.0))

            obv = (np.sign(delta.fillna(0.0)) * vol).cumsum()
            obv_trend = np.sign(obv.diff().fillna(0.0))

            combined = (trend + band_signal + rsi_signal + obv_trend) / 4.0
            out[symbol] = pd.Series(np.clip(combined, -1.0, 1.0), index=px.index)

        return out


@dataclass
class TechnicalStrategy(Strategy):
    name: str = "technical"
    fill_missing: bool = False
    _legacy_engine: LegacyTechnicalEngine = field(default_factory=LegacyTechnicalEngine)

    def generate(self, data: dict[str, pd.DataFrame]) -> StrategySignal:
        signals = self._legacy_engine.generate(data)
        values = dict_series_to_wide(signals, fill_missing=self.fill_missing)
        return StrategySignal(
            name=self.name,
            values=values,
            metadata={"adapter": "dict_series_to_wide", "fill_missing": self.fill_missing},
        )


@dataclass
class PairTradingPlaceholderStrategy(Strategy):
    """Extension point placeholder. Does not emit active signals."""

    name: str = "pair_trading"

    def generate(self, data: dict[str, pd.DataFrame]) -> StrategySignal:
        close = data.get("close")
        if close is None:
            raise ValueError("data must provide 'close' DataFrame")
        values = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        values.index.name = "date"
        return StrategySignal(name=self.name, values=values, metadata={"placeholder": True})
