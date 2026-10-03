"""Download the fixed September extension; no authenticated exchange access.

Run after research.download_factor_data. Existing monthly archives take priority
on overlapping dates. Each replay checks complete coverage before proceeding.
"""
import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd
import yaml

from bot.config.settings import Config
from bot.data.market_bars import fetch_bars


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='cache/drawdown_recent')
    args = parser.parse_args()
    cfg = Config(data_dir=args.output, history_days=29)
    end = pd.Timestamp('2026-09-29', tz='UTC')
    required = pd.date_range('2026-09-01 00:30', end, freq='30min', tz='UTC')
    universe = yaml.safe_load(Path('bot/config/universe.yaml').read_text())['coins']
    manifest = {}
    for item in universe:
        symbol = item['binance_symbol']
        bars = fetch_bars(symbol, cfg, now=end)
        if not required.isin(bars.index).all():
            raise ValueError(f'{symbol}: incomplete September data; retry download')
        path = Path(args.output) / f'ohlcv_{symbol}_30m.csv'
        manifest[symbol] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'start': str(bars.index.min()), 'end': str(bars.index.max()), 'rows': len(bars)}
        print(f'{symbol}: {len(bars)} completed bars', flush=True)
    (Path(args.output) / 'manifest.json').write_text(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
