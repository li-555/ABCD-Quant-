"""Compatibility entry point for other strategies; no embedded credentials.

New code should use bot.platform.RoostooData and bot.strategy_bridge.StrategyBridge.
Read-only unless LIVE=1 is explicitly set. Never create a client on import.
"""
from bot.config.settings import load_config
from bot.execution.roostoo_client import RoostooClient


def _client():
    cfg = load_config()
    return RoostooClient(cfg, read_only=not cfg.live)


def _call(method, *args, **kwargs):
    client = _client()
    try:
        return getattr(client, method)(*args, **kwargs)
    finally:
        client._session.close()


def check_server_time():
    client = _client()
    try:
        return client._request('GET', '/v3/serverTime', {}, signed=False)
    finally:
        client._session.close()


def get_ticker(pair=None):
    return _call('get_ticker', pair)


def get_balance():
    return _call('get_balance')


def place_order(pair, side, quantity, order_type='MARKET', price=None):
    return _call('place_order', pair, side, order_type, quantity, price)


def get_market_context(pairs):
    """Shared prices/history, spot wallet and exchange rules for any strategy."""
    from bot.platform import RoostooData
    client = _client()
    try:
        return RoostooData(client.cfg, client, pairs).snapshot()
    finally:
        client._session.close()


def run_strategy(strategy, pairs, *, execute=False):
    """Accept an existing Strategy.generate(data) implementation."""
    from bot.platform import RoostooData
    client = _client()
    try:
        context = RoostooData(client.cfg, client, pairs).snapshot()
        return _submit(client, strategy.generate(context.data), context, pairs, execute)
    finally:
        client._session.close()


def submit_signal(signal, pairs, *, execute=False):
    """Accept StrategySignal scores or explicit TargetWeights; default preview."""
    from bot.platform import RoostooData
    client = _client()
    try:
        context = RoostooData(client.cfg, client, pairs).snapshot()
        return _submit(client, signal, context, pairs, execute)
    finally:
        client._session.close()


def _submit(client, signal, context, pairs, execute):
    from pathlib import Path
    from bot.strategy_bridge import StrategyBridge
    cfg = client.cfg
    bridge = StrategyBridge(cfg, client, [p[:-4] for p in pairs],
        Path(cfg.data_dir) / f'roostoo_{cfg.roostoo_account}_orders.sqlite3')
    return bridge.submit_signal(signal, context, execute=execute)


if __name__ == '__main__':
    from bot.platform import main
    raise SystemExit(main(['--check-connection']))
