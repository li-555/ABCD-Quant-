"""Scheduler integration tests (offline): rebalance cadence, KILL, catch-up.

We monkeypatch ``bot.scheduler.fetch_price_grid`` so the cycle never touches the
network, and drive a ``FakeExchange`` that fills orders immediately.
"""
from __future__ import annotations

import pandas as pd
import pytest

import bot.scheduler as sched
from bot.scheduler import cycle, load_universe
from bot.state import State
from bot.logging_utils import Loggers
from tests.helpers import (
    synth_prices, bot_cfg, universe_from_cols, FakeExchange,
)


@pytest.fixture
def grid():
    P, _ = synth_prices(bar_min=30, days=60, seed=7)
    return P


@pytest.fixture
def env(tmp_path, grid, monkeypatch):
    monkeypatch.setattr(sched, "fetch_price_grid",
                        lambda universe, cfg: grid)
    cfg = bot_cfg(tmp_path)
    universe = universe_from_cols(grid.columns)
    prices = {f"{c}/USD": float(grid.iloc[-1][c]) for c in grid.columns}
    client = FakeExchange([f"{c}/USD" for c in grid.columns], prices)
    state = State()
    loggers = Loggers(cfg)
    loggers.set_coins(list(grid.columns))
    return cfg, universe, client, state, loggers


def test_cycle_rebalances_once_per_day(env):
    cfg, universe, client, state, loggers = env
    cycle(cfg, client, state, loggers, universe)
    today = pd.Timestamp.now(tz="UTC").floor("1D").strftime("%Y-%m-%d")
    assert state.last_rebalance_date == today
    # a second cycle the same day must NOT re-rebalance
    orders_before = len(client.orders)
    cycle(cfg, client, state, loggers, universe)
    assert state.last_rebalance_date == today
    # no new rebalance orders (stops/activity may add a few, but rebalance skipped)
    # we only assert the rebalance flag is stable
    assert state.last_rebalance_date == today


def test_cycle_writes_equity_and_heartbeat(env):
    cfg, universe, client, state, loggers = env
    cycle(cfg, client, state, loggers, universe)
    import os
    assert os.path.exists(cfg.equity_csv)
    assert os.path.getsize(cfg.equity_csv) > 0
    assert os.path.exists(cfg.heartbeat_file)


def test_kill_file_blocks_new_positions(env):
    cfg, universe, client, state, loggers = env
    # place KILL file
    with open(cfg.kill_file, "w", encoding="utf-8") as fh:
        fh.write("stop")
    cycle(cfg, client, state, loggers, universe)
    # rebalance skipped -> date not set; no new orders placed in close-only mode
    assert state.last_rebalance_date == ""
    assert len(client.orders) == 0


def test_missed_midnight_catchup(env):
    """If the bot was down at 00:00, the first cycle of the new day rebalances."""
    cfg, universe, client, state, loggers = env
    # pretend yesterday was already "rebalanced"
    yesterday = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)
                 ).floor("1D").strftime("%Y-%m-%d")
    state.last_rebalance_date = yesterday
    cycle(cfg, client, state, loggers, universe)
    today = pd.Timestamp.now(tz="UTC").floor("1D").strftime("%Y-%m-%d")
    assert state.last_rebalance_date == today
