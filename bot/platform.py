"""Shared Roostoo data service and pluggable strategy runner.

python -m bot.platform --check-connection
python -m bot.platform --strategy bot.platform:TickerMomentumStrategy --once
The default creates plans only. Remote orders require both LIVE=1 and --execute.
"""
import argparse
import importlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from bot.config.settings import Config, load_config, load_credentials
from bot.execution.roostoo_client import RoostooClient
from bot.strategy_bridge import MarketContext, StrategyBridge
from quant_research.multi_strategy.strategy import StrategySignal


class RoostooData:
    """One shared snapshot per polling cycle; closed sampled prices for history.

    These are ticker observations, NOT exchange OHLCV candles. Volume fields
    keep their original names and never masquerade as per-bar traded volume.
    """
    def __init__(self, cfg, client, pairs):
        self.cfg, self.client, self.pairs = cfg, client, list(pairs)
        if len(set(pairs)) != len(pairs) or any(
                not p.endswith('/USD') or not p[:-4].isalnum() for p in pairs):
            raise ValueError('unique COIN/USD pairs required')

    def snapshot(self):
        ticker = self.client.get_ticker()
        info = self.client.get_exchange_info()
        balance = self.client.get_balance()
        if not ticker.get('Success') or not balance.get('Success'):
            raise RuntimeError('Roostoo ticker/balance request failed')
        now = pd.Timestamp.now(tz='UTC')
        stamp = pd.to_datetime(ticker.get('ServerTime'), unit='ms', utc=True)
        if pd.isna(stamp) or abs((now-stamp).total_seconds()) > self.cfg.staleness_minutes * 60:
            raise ValueError('missing/stale Roostoo ticker timestamp')
        quotes = ticker.get('Data', {})
        cols = [p[:-4] for p in self.pairs]
        records = []
        for pair in self.pairs:
            if pair not in info.get('TradePairs', {}):
                raise ValueError(f'unknown Roostoo pair: {pair}')
            quote = quotes.get(pair, {})
            price = float(quote.get('LastPrice', float('nan')))
            if not np.isfinite(price) or price <= 0:
                raise ValueError(f'missing Roostoo quote: {pair}')
            records.append({'time': stamp, 'coin': pair[:-4], 'close': price})
        folder = Path(self.cfg.data_dir) / 'roostoo'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'observations.csv'
        observed = pd.DataFrame(records)
        if path.exists():
            old = pd.read_csv(path)
            old['time'] = pd.to_datetime(old['time'], utc=True)
            observed = pd.concat([old, observed], ignore_index=True)
        observed = observed.drop_duplicates(['time', 'coin'], keep='last').sort_values('time')
        observed = observed[observed.time >= stamp-pd.Timedelta(days=self.cfg.history_days)]
        tmp = path.with_suffix('.tmp')
        observed.to_csv(tmp, index=False)
        tmp.replace(path)
        close = observed.pivot(index='time', columns='coin', values='close').reindex(columns=cols)
        close = close.resample(f'{self.cfg.bar_min}min', label='right', closed='right').last()
        close = close.loc[close.index <= stamp.floor(f'{self.cfg.bar_min}min')]
        close.index.name, close.columns.name = 'date', 'asset'
        data = {'close': close}
        for name, field in [('ticker_last','LastPrice'), ('bid','MaxBid'), ('ask','MinAsk'),
                            ('change_24h','Change'), ('coin_trade_value','CoinTradeValue'),
                            ('unit_trade_value','UnitTradeValue')]:
            data[name] = pd.DataFrame({p[:-4]: [quotes[p].get(field, np.nan)] for p in self.pairs},
                                      index=pd.DatetimeIndex([stamp]))
        wallet = balance.get('SpotWallet', balance.get('Wallet'))
        if not isinstance(wallet, dict):
            raise ValueError('missing spot wallet')
        return MarketContext(stamp, data, wallet, info, quotes)


class TickerMomentumStrategy:
    """Integration example only; no historical performance claim."""
    name = 'ticker_momentum_example'

    def generate(self, data):
        return StrategySignal(self.name, data['change_24h'].clip(-1, 1))


def load_strategy(spec):
    module, sep, name = spec.partition(':')
    if not sep:
        raise ValueError('strategy format is module:ClassName')
    strategy = getattr(importlib.import_module(module), name)()
    if not callable(getattr(strategy, 'generate', None)):
        raise TypeError('strategy requires generate(data)')
    return strategy


def check_connection(client):
    """Only reads; return counts/status, never credentials or account amounts."""
    info, ticker, balance = client.get_exchange_info(), client.get_ticker(), client.get_balance()
    return {'exchange_running': info.get('IsRunning') is True,
            'pair_count': len(info.get('TradePairs', {})),
            'ticker_ok': ticker.get('Success') is True,
            'ticker_pairs': len(ticker.get('Data', {})),
            'balance_ok': balance.get('Success') is True,
            'spot_wallet_recognized': isinstance(balance.get('Wallet'), dict),
            'orders_sent': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='bot/config/config.yaml')
    parser.add_argument('--account', choices=['test','competition'], default=None)
    parser.add_argument('--pairs', nargs='+', default=['BTC/USD','ETH/USD'])
    parser.add_argument('--strategy', default='bot.platform:TickerMomentumStrategy')
    parser.add_argument('--check-connection', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--interval', type=float, default=60)
    args = parser.parse_args(argv)
    if args.interval < 5:
        parser.error('--interval must be at least 5 seconds')
    cfg = load_config(args.config, account=args.account)
    if not cfg.roostoo_api_key or not cfg.roostoo_api_secret:
        parser.error('set the selected account API key/secret environment variables')
    if args.execute and not cfg.live:
        parser.error('--execute requires LIVE=1')
    client = RoostooClient(cfg, read_only=not args.execute or args.check_connection)
    try:
        if args.check_connection:
            result = check_connection(client)
            print(json.dumps(result))
            return 0 if all(result[k] for k in ('exchange_running','ticker_ok','balance_ok','spot_wallet_recognized')) else 1
        data = RoostooData(cfg, client, args.pairs)
        strategy = load_strategy(args.strategy)
        bridge = StrategyBridge(cfg, client, [p[:-4] for p in args.pairs],
            Path(cfg.data_dir) / f'roostoo_{cfg.roostoo_account}_orders.sqlite3')
        while True:
            context = data.snapshot()  # collection always runs before strategy warmup
            minimum = getattr(strategy, 'min_bars', 0)
            required = getattr(strategy, 'required_fields', ())
            missing = set(required) - set(context.data)
            if missing:
                raise ValueError(f'strategy needs unavailable data fields: {sorted(missing)}')
            if len(context.data['close'].dropna()) < minimum:
                result = {'status': 'warming_up', 'observed_bars': len(context.data['close'].dropna()), 'required_bars': minimum}
            else:
                signal = strategy.generate(context.data)
                result = ({'status': 'warming_up'} if signal is None else
                          bridge.submit_signal(signal, context, execute=args.execute))
            print(json.dumps(result), flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
    finally:
        client._session.close()


if __name__ == '__main__':
    raise SystemExit(main())
