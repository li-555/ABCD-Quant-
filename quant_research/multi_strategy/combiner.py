from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

import numpy as np
import pandas as pd

from .strategy import StrategySignal

NaNPolicy = Literal["propagate", "ignore"]
ConflictMode = Literal["weighted_sum", "majority_vote"]


@dataclass
class StrategyCombiner:
    """Combine multiple strategy signals with explicit alignment and NaN policy."""

    weights: dict[str, float]
    normalize_weights: bool = True
    nan_policy: NaNPolicy = "propagate"
    conflict_mode: ConflictMode = "weighted_sum"
    clip: tuple[float, float] | None = (-1.0, 1.0)

    def combine(self, signals: Iterable[StrategySignal]) -> StrategySignal:
        signal_list = [s for s in signals if s.name in self.weights]
        if not signal_list:
            raise ValueError("no input signals matched configured strategy weights")

        abs_weight_sum = sum(abs(self.weights[s.name]) for s in signal_list)
        if self.normalize_weights and abs_weight_sum <= 0.0:
            raise ValueError("strategy weights must not all be zero")

        aligned_frames = [s.values.astype(float) for s in signal_list]
        union_index = aligned_frames[0].index
        union_columns = aligned_frames[0].columns
        for frame in aligned_frames[1:]:
            union_index = union_index.union(frame.index)
            union_columns = union_columns.union(frame.columns)

        weighted_frames: list[pd.DataFrame] = []
        for signal in signal_list:
            weight = float(self.weights[signal.name])
            if self.normalize_weights:
                weight = weight / abs_weight_sum
            frame = signal.values.astype(float).reindex(index=union_index, columns=union_columns)
            weighted_frames.append(frame * weight)

        if self.conflict_mode == "majority_vote":
            stacked = np.stack([np.sign(frame).to_numpy() for frame in weighted_frames], axis=0)
            combined_np = np.sign(np.nansum(stacked, axis=0))
            combined = pd.DataFrame(combined_np, index=union_index, columns=union_columns)
        else:
            combined = weighted_frames[0]
            for frame in weighted_frames[1:]:
                if self.nan_policy == "ignore":
                    combined = combined.add(frame, fill_value=0.0)
                else:
                    combined = combined + frame

        if self.nan_policy == "ignore":
            counts = sum(frame.notna().astype(float) for frame in weighted_frames)
            combined = combined.where(counts > 0)

        if self.clip is not None:
            combined = combined.clip(self.clip[0], self.clip[1])

        combined.index.name = "date"
        return StrategySignal(
            name="combined",
            values=combined,
            metadata={
                "weights": self.weights,
                "normalize_weights": self.normalize_weights,
                "nan_policy": self.nan_policy,
                "conflict_mode": self.conflict_mode,
            },
        )
