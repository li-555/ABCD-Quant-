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

## Project Structure

- `/quant_research`: core library modules
- `/run_example.py`: runnable end-to-end example using synthetic data
- `/tests/test_pipeline.py`: focused tests for core pipeline behavior
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
