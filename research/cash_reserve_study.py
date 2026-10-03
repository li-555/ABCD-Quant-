"""Cash reserve scenario: fixed 85% initial strategy capital + 15% idle cash.

This is risk allocation, not a newly discovered alpha. No parameter search.
python -m research.cash_reserve_study --output outputs/drawdown_study
"""
from pathlib import Path
import argparse
import json
import numpy as np
import pandas as pd
import yaml
from bot.config.settings import Config
from research.factor_ablation import replay, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default='cache/factor_history')
    parser.add_argument('--recent-data', default='cache/drawdown_recent')
    parser.add_argument('--output', default='outputs/drawdown_study')
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    universe = yaml.safe_load(Path('bot/config/universe.yaml').read_text())['coins']
    frames = {}
    for u in universe:
        pieces = []
        for path in (Path(args.data)/f"{u['coin']}.csv",
                     Path(args.recent_data)/f"ohlcv_{u['binance_symbol']}_30m.csv"):
            df = pd.read_csv(path, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True)
            pieces.append(df)
        df = pd.concat(pieces)
        frames[u['coin']] = df[~df.index.duplicated(keep='first')].sort_index()
    bars = {k: pd.DataFrame({c:f[k] for c,f in frames.items()}) for k in ('open','close','volume')}
    cfg = Config(activity_enabled=False, factor_volume_weight=.2, cash_reserve_usd=15000)
    periods = {'2024': ['2024-01-01','2025-01-01'], '2025': ['2025-01-01','2026-01-01'],
               'reused_diagnostic': ['2026-01-01','2026-09-01'],
               'new_short_check': ['2026-09-01','2026-09-29']}
    protocol = {'initial_total': 100000, 'fixed_cash_reserve': 15000,
                'strategy_initial_capital': 85000, 'periods': periods,
                'note': 'Scenario added after eight risk parameter candidates failed. Historical comparisons are exploratory; no claim of fresh holdout or alpha improvement. No interest on idle cash.'}
    (out/'cash_reserve_protocol.json').write_text(json.dumps(protocol,indent=2))
    records = []
    for period, bounds in periods.items():
        data = {k:v.loc[:pd.Timestamp(bounds[1],tz='UTC')] for k,v in bars.items()}
        for mult in [1.0,2.0]:
            result = replay(data,cfg,*bounds,cost_multiplier=mult)
            reference = pd.read_csv(out/f'{period}_volume20_{mult:g}x.csv',index_col=0,parse_dates=True)
            # Test the implemented quantity ledger, not only an equity rescaling.
            np.testing.assert_allclose(result['equity'], .85*reference.equity+15000, atol=1e-6)
            np.testing.assert_allclose(result['cost'], .85*reference.cost, atol=1e-6)
            record = {'candidate':'cash85','period':period,'cost_multiplier':mult,**metrics(result)}
            records.append(record)
            pd.concat([result[k] for k in ('equity','cost','turnover','gross')],axis=1).to_csv(out/f'{period}_cash85_{mult:g}x.csv')
            result['trades'].to_csv(out/f'{period}_cash85_{mult:g}x_trades.csv',index=False)
            print(json.dumps(record),flush=True)
    pd.DataFrame(records).to_csv(out/'cash85_metrics.csv',index=False)


if __name__ == '__main__':
    main()
