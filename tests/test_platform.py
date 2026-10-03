"""Offline Roostoo contract and strategy-to-execution integration tests."""
from dataclasses import replace
import sqlite3

import numpy as np
import pandas as pd
import pytest
import requests

from bot.config.settings import Config, load_credentials, load_config
from bot.execution.client import SignedMixin
from bot.execution.roostoo_client import RoostooClient
from bot.execution.paper_client import PaperClient
from bot.platform import RoostooData, TickerMomentumStrategy
from bot.strategy_bridge import MarketContext, StrategyBridge, TargetWeights, to_targets
from bot.data.store import snapshot_to_grid
from quant_research.multi_strategy.strategy import StrategySignal
from tests.test_execution import FakeSession


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(SignedMixin, 'sync_time', lambda self: None)
    return RoostooClient(Config(rate_limit_seconds=0, roostoo_api_key='fake-key', roostoo_api_secret='fake-secret'))


def test_current_spot_wallet_and_unsigned_ticker(client):
    wallet = {'USD': {'Free': 100, 'Lock': 0}}
    client._session = FakeSession(lambda method,url,kw: (200,
        {'Success': True, 'SpotWallet': wallet, 'MarginWallet': {'USD': {'Free': 999}}}))
    assert client.get_balance()['Wallet'] == wallet
    client.get_ticker('BTC/USD')
    assert client._session.calls[-1][2]['headers'] == {}
    assert 'timestamp' in client._session.calls[-1][2]['params']


def test_legacy_wallet_is_still_supported(client):
    wallet = {'USD': {'Free': 100, 'Lock': 0}}
    client._session = FakeSession(lambda *args: (200, {'Success': True, 'Wallet': wallet}))
    assert client.get_balance()['Wallet'] == wallet


def test_order_timeout_is_not_retried(client):
    calls = []
    def handler(*args):
        calls.append(args)
        raise requests.Timeout('could contain credentials in an upstream error')
    client._session = FakeSession(handler)
    result = client.place_order('BTC/USD','BUY','MARKET',.01)
    assert len(calls) == 1
    assert result['UnknownExecution'] is True
    assert result['ErrMsg'] == 'request failed: Timeout'


def test_readonly_client_rejects_all_mutations(client):
    client.read_only = True
    with pytest.raises(RuntimeError):
        client.place_order('BTC/USD','BUY','MARKET',1)
    with pytest.raises(RuntimeError):
        client.cancel_order()


def test_profiles_never_mix_and_repr_redacts(monkeypatch):
    monkeypatch.setenv('ROOSTOO_API_KEY','generic')
    monkeypatch.setenv('ROOSTOO_API_SECRET','generic-secret')
    monkeypatch.setenv('ROOSTOO_TEST_API_KEY','test-only')
    monkeypatch.delenv('ROOSTOO_TEST_API_SECRET',raising=False)
    cfg = load_credentials(Config(), 'test')
    assert cfg.roostoo_api_key == 'test-only' and cfg.roostoo_api_secret == ''
    monkeypatch.setenv('ROOSTOO_COMPETITION_API_KEY','competition-only')
    monkeypatch.setenv('ROOSTOO_COMPETITION_API_SECRET','competition-secret')
    cfg = load_credentials(cfg, 'competition')
    assert cfg.roostoo_api_key == 'competition-only'
    assert 'competition-secret' not in repr(cfg) and 'competition-only' not in repr(cfg)


def test_yaml_cannot_store_credentials(tmp_path):
    path = tmp_path/'config.yaml'
    path.write_text('roostoo_api_secret: do-not-store-me')
    with pytest.raises(ValueError, match='environment'):
        load_config(str(path))


def make_context(cfg):
    paper = PaperClient(cfg, ['BTC/USD','ETH/USD'])
    paper.set_market_prices({'BTC/USD': 100, 'ETH/USD': 10})
    stamp = pd.Timestamp.now(tz='UTC')
    tk = paper.get_ticker()['Data']
    context = MarketContext(stamp, {}, paper.get_balance()['Wallet'], paper.get_exchange_info(), tk)
    return paper, context


def test_scores_and_weights_explicit_risk_limits():
    cfg = Config()
    _, ctx = make_context(cfg)
    signal = StrategySignal('other', pd.DataFrame([[2,-3]], index=[ctx.asof], columns=['BTC','ETH']))
    target = to_targets(signal, ctx, ['BTC','ETH'], cfg)
    assert target.values.to_dict() == {'BTC': cfg.single_long, 'ETH': 0}
    with pytest.raises(ValueError):
        to_targets(TargetWeights('short',ctx.asof,pd.Series({'BTC':-.1})),ctx,['BTC','ETH'],cfg)


@pytest.mark.parametrize('kind', ['future','stale','nan','unknown'])
def test_invalid_signal_never_reaches_execution(kind,tmp_path):
    cfg=Config()
    paper,ctx=make_context(cfg)
    stamp = ctx.asof + pd.Timedelta(days=1) if kind=='future' else ctx.asof
    if kind=='stale': stamp -= pd.Timedelta(days=1)
    values=pd.Series({'BAD' if kind=='unknown' else 'BTC': np.nan if kind=='nan' else .2})
    with pytest.raises(ValueError):
        StrategyBridge(cfg,paper,['BTC','ETH'],tmp_path/'orders.db').submit_signal(TargetWeights('s',stamp,values),ctx)
    assert paper.positions == {}


def test_preview_and_live_gate(tmp_path):
    cfg=Config()
    paper,ctx=make_context(cfg)
    bridge=StrategyBridge(cfg,paper,['BTC','ETH'],tmp_path/'orders.db')
    signal=TargetWeights('s',ctx.asof,pd.Series({'BTC':.3}))
    assert bridge.submit_signal(signal,ctx)['status']=='preview'
    assert paper.positions == {}
    with pytest.raises(RuntimeError,match='LIVE'):
        bridge.submit_signal(signal,ctx,execute=True)


def test_actual_paper_fills_reserve_and_durable_dedup(tmp_path):
    cfg=Config(live=True,cash_reserve_usd=15000)
    paper,ctx=make_context(cfg)
    journal=tmp_path/'orders.db'
    signal=TargetWeights('s',ctx.asof,pd.Series({'BTC':.35,'ETH':.35}))
    bridge=StrategyBridge(cfg,paper,['BTC','ETH'],journal)
    result=bridge.submit_signal(signal,ctx,execute=True)
    assert result['submitted'] >= 4
    assert paper.cash >= 15000
    before=paper.cash
    assert StrategyBridge(cfg,paper,['BTC','ETH'],journal).submit_signal(signal,ctx,execute=True)['status']=='duplicate'
    assert paper.cash == before


def test_ambiguous_order_blocks_later_batches(tmp_path):
    cfg=Config(live=True)
    paper,ctx=make_context(cfg)
    paper.place_order=lambda *a,**k: {'Success':False,'UnknownExecution':True}
    bridge=StrategyBridge(cfg,paper,['BTC','ETH'],tmp_path/'orders.db')
    signal=TargetWeights('s',ctx.asof,pd.Series({'BTC':.2}))
    with pytest.raises(RuntimeError,match='uncertain'):
        bridge.submit_signal(signal,ctx,execute=True)
    with pytest.raises(RuntimeError,match='unreconciled'):
        bridge.submit_signal(replace(signal,asof=ctx.asof+pd.Timedelta(seconds=1)),replace(ctx,asof=ctx.asof+pd.Timedelta(seconds=1)),execute=True)


def test_snapshot_has_no_fabricated_volume_or_future_bar(tmp_path):
    cfg=Config(data_dir=str(tmp_path))
    paper,ctx=make_context(cfg)
    paper.get_ticker=lambda: {'Success':True,'ServerTime':int(ctx.asof.timestamp()*1000),'Data':ctx.ticker}
    context=RoostooData(cfg,paper,['BTC/USD','ETH/USD']).snapshot()
    assert 'volume' not in context.data
    assert 'coin_trade_value' in context.data
    assert (context.data['close'].index <= context.asof.floor('30min')).all()
    assert (tmp_path/'roostoo'/'observations.csv').exists()
    assert isinstance(TickerMomentumStrategy().generate(context.data),StrategySignal)


def test_snapshot_gaps_are_not_filled():
    s=pd.Series([100,105],index=pd.to_datetime(['2026-01-01T00:01Z','2026-01-01T01:01Z']))
    grid=snapshot_to_grid(s,30)
    assert pd.isna(grid.loc['2026-01-01T01:00Z'])


def test_collector_runs_before_v1_warmup(monkeypatch,tmp_path):
    from bot import scheduler
    from bot.state import State
    from bot.logging_utils import Loggers
    cfg=Config(data_dir=str(tmp_path/'data'),log_dir=str(tmp_path/'logs'),
               market_data_source='roostoo',equity_csv=str(tmp_path/'eq.csv'))
    paper,_=make_context(cfg)
    monkeypatch.setattr(scheduler,'MIN_BARS',500)
    scheduler.cycle(cfg,paper,State(),Loggers(cfg),[
        {'coin':'BTC','binance_symbol':'BTCUSDT','roostoo_pair':'BTC/USD'}])
    assert (tmp_path/'data'/'rt_snap_BTC.csv').exists()
    assert paper.positions == {}


def test_pending_orders_block_new_batch(tmp_path):
    cfg=Config(live=True)
    paper,ctx=make_context(cfg)
    paper.place_order('ETH/USD','BUY','LIMIT',1,1)
    with pytest.raises(RuntimeError,match='pending orders'):
        StrategyBridge(cfg,paper,['BTC','ETH'],tmp_path/'orders.db').submit_signal(
            TargetWeights('s',ctx.asof,pd.Series({'BTC':.2})),ctx,execute=True)
    assert paper.positions == {}


def test_daily_cap_is_shared_between_batches(tmp_path):
    cfg=Config(live=True,max_daily_trades=1)
    paper,ctx=make_context(cfg)
    bridge=StrategyBridge(cfg,paper,['BTC','ETH'],tmp_path/'orders.db')
    bridge.submit_signal(TargetWeights('s',ctx.asof,pd.Series({'BTC':.1})),ctx,execute=True)
    later=replace(ctx,asof=ctx.asof+pd.Timedelta(seconds=1))
    with pytest.raises(RuntimeError,match='daily order cap'):
        bridge.submit_signal(TargetWeights('s',later.asof,pd.Series({'BTC':.2})),later,execute=True)


def test_live_multifactor_keeps_latest_row_without_future_label():
    from quant_research.multi_strategy.strategies import MultiFactorStrategy
    rng=np.random.default_rng(123)
    idx=pd.date_range('2026-01-01',periods=70,freq='30min',tz='UTC')
    close=pd.DataFrame(np.exp(rng.normal(0,.01,(70,3)).cumsum(axis=0))*100,index=idx,columns=['BTC','ETH','SOL'])
    volume=pd.DataFrame(rng.uniform(10,100,(70,3)),index=idx,columns=close.columns)
    strategy=MultiFactorStrategy(prefer_zoo_engine=False)
    a=strategy.generate({'close':close.iloc[:60],'volume':volume.iloc[:60]}).values
    b=strategy.generate({'close':close,'volume':volume}).values
    assert a.index[-1] == close.index[59]
    pd.testing.assert_frame_equal(a,b.loc[a.index])


def test_confidence_alignment_and_invalid_weight(tmp_path):
    cfg=Config()
    _,ctx=make_context(cfg)
    scores=pd.DataFrame([[1,1]],index=[ctx.asof],columns=['BTC','ETH'])
    signal=StrategySignal('s',scores,confidence=pd.DataFrame([[1,0]],index=scores.index,columns=scores.columns))
    target=to_targets(signal,ctx,['BTC','ETH'],cfg)
    assert target.values['ETH']==0
    signal=StrategySignal('s',scores,confidence=pd.DataFrame([[1,2]],index=scores.index,columns=scores.columns))
    with pytest.raises(ValueError,match='confidence'):
        to_targets(signal,ctx,['BTC','ETH'],cfg)
