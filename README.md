# ABCD-Quant-

A modular Python quantitative research project for alpha factor analysis and backtesting.

## Features

- Load and clean market data (`quant_research/data.py`)
- Compute example alpha factors (`quant_research/factors.py`):
  - Momentum
  - Mean reversion
  - Volatility
  - Volume-price correlation
- Evaluate factor predictability (`quant_research/evaluation.py`):
  - IC
  - Rank IC
  - Regression t-value / p-value / R²
- Combine factors into trading signals (`quant_research/signals.py`)
- Build simple long-short portfolios (`quant_research/portfolio.py`)
- Backtest with transaction costs and slippage (`quant_research/backtest.py`):
  - Returns
  - Sharpe ratio
  - Drawdown
  - Turnover
  - Benchmark comparison
- Visualize equity curve, drawdown, factor performance, and factor correlations (`quant_research/visualization.py`)
- Save experiment results and generate a report (`quant_research/reporting.py`)
- Multi-strategy architecture (`quant_research/multi_strategy/`) with:
  - Common strategy contract (`Strategy`, `StrategySignal`)
  - MultiFactor + Technical strategy adapters (plus pair-trading placeholder extension point)
  - Strategy combiner with explicit weight normalization/conflict policy/NaN policy
  - Portfolio construction, basic risk limits, and execution simulator stages

## Project Structure

- `/quant_research`: core library modules
- `/quant_research/multi_strategy`: modular multi-strategy pipeline components
- `/run_example.py`: runnable end-to-end example using synthetic data
- `/tests/test_pipeline.py`: focused tests for core pipeline behavior
- `/tests/test_multi_strategy.py`: tests for strategy contract, adapters, combiner, portfolio, risk, and execution
- `/outputs`: generated outputs (charts, CSV, JSON, markdown report)

## Installation

```bash
pip install -r requirements.txt
```

## Run Example

```bash
python run_example.py
```

This generates:

- `outputs/daily_results.csv`
- `outputs/factor_summary.csv`
- `outputs/performance.json`
- `outputs/equity_drawdown.png`
- `outputs/factor_performance.png`
- `outputs/factor_correlation.png`
- `outputs/report.md`

## Multi-Strategy Pipeline Example

Architecture:

```text
Data -> Features -> [MultiFactor, PairTrading(placeholder), Technical] -> StrategyCombiner -> Portfolio -> Risk -> Execution
```

Run a minimal in-memory orchestration example:

```python
from quant_research.multi_strategy import run_multistrategy_example

result = run_multistrategy_example()
print(result["trades"].head())
```

Notes:
- Strategy outputs use wide pandas DataFrames (`date` index, instrument columns).
- Legacy per-symbol `dict[str, Series]` engines are adapted via `dict_series_to_wide`.
- NaNs are not silently converted to zero in the core signal path unless `nan_policy="ignore"` is configured in `StrategyCombiner`.

## Run Tests

```bash
pytest -q
```

## Extend the Project

- Add custom factors in `quant_research/factors.py`
- Adjust signal weighting in `quant_research/signals.py`
- Swap portfolio logic in `quant_research/portfolio.py`
- Add advanced cost models in `quant_research/backtest.py`
- Add additional reporting/plots in `quant_research/reporting.py` and `quant_research/visualization.py`
