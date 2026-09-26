from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def plot_equity_and_drawdown(daily_results: pd.DataFrame, output_dir: str | Path) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    axes[0].plot(daily_results["date"], daily_results["equity_curve"], label="Strategy")
    axes[0].set_title("Equity Curve")
    axes[0].legend()

    axes[1].fill_between(daily_results["date"], daily_results["drawdown"], 0.0, alpha=0.4)
    axes[1].set_title("Drawdown")

    fig.tight_layout()
    fig.savefig(out / "equity_drawdown.png", dpi=150)
    plt.close(fig)


def plot_factor_correlation(df: pd.DataFrame, factor_cols: list[str], output_dir: str | Path) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    corr = df[factor_cols].corr()
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(corr.values, cmap="coolwarm", vmin=-1, vmax=1)
    ax.set_xticks(range(len(factor_cols)))
    ax.set_yticks(range(len(factor_cols)))
    ax.set_xticklabels(factor_cols, rotation=45, ha="right")
    ax.set_yticklabels(factor_cols)
    ax.set_title("Factor Correlation")
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(out / "factor_correlation.png", dpi=150)
    plt.close(fig)


def plot_factor_performance(summary_df: pd.DataFrame, output_dir: str | Path) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(8, 4))
    summary_df[["mean_ic", "mean_rank_ic"]].plot(kind="bar", ax=ax)
    ax.set_title("Factor Predictability (IC / Rank IC)")
    ax.set_ylabel("Value")
    fig.tight_layout()
    fig.savefig(out / "factor_performance.png", dpi=150)
    plt.close(fig)
