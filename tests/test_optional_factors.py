from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from bot.config.settings import Config
from bot.strategy.signals import build_signals
from bot.strategy.factors import build_factors
from bot.data.market_bars import parse_klines, fetch_bars
from research.factor_ablation import replay, qualifies


@pytest.fixture
def market():
    rng = np.random.default_rng(491)
    idx = pd.date_range("2024-01-01", periods=48 * 65 + 1, freq="30min", tz="UTC")
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0001, .004, (len(idx), 3)), axis=0)),
                         index=idx, columns=["BTC", "ETH", "SOL"])
    volume = pd.DataFrame(rng.uniform(1, 20, close.shape), index=idx, columns=close.columns)
    return {"close": close, "open": close.shift(1).fillna(close.iloc[0]), "volume": volume}


def active():
    return Config(factor_rs_weight=.05, factor_mr_weight=.05,
                  factor_volume_weight=.05, factor_session_weight=.05)


def test_off_is_exact_v1(market):
    a = build_signals(market["close"], Config())
    b = build_signals(market["close"], Config(), market["volume"] * 100)
    for key in a:
        pd.testing.assert_frame_equal(a[key], b[key], check_exact=True)
    pd.testing.assert_frame_equal(a["S"], a["S_slow"], check_exact=True)


@pytest.mark.parametrize("cut", [1200, 1217, 1248, 2500])
def test_prefix_invariance_including_session_boundaries(market, cut):
    full = build_signals(market["close"], active(), market["volume"])
    short = build_signals(market["close"].iloc[:cut], active(), market["volume"].iloc[:cut])
    for key in full:
        pd.testing.assert_frame_equal(full[key].iloc[:cut], short[key])


def test_missing_volume_uses_base_and_not_fabricated_data(market):
    cfg = Config(factor_volume_weight=.2)
    with pytest.raises(ValueError, match="no volume"):
        build_signals(market["close"], cfg)
    v = market["volume"].copy()
    v.iloc[-48:] = np.nan
    result = build_signals(market["close"], cfg, v)
    pd.testing.assert_series_equal(result["S"].iloc[-1], result["S_slow"].iloc[-1])
    with pytest.raises(ValueError, match="match close"):
        build_signals(market["close"], cfg, v.iloc[:, ::-1])


def test_factor_direction_and_session_visibility(market):
    f = build_factors(market["close"], active(), market["volume"])
    assert (f["relative_strength"]["BTC"].dropna() == 0).all()
    for value in f.values():
        assert value.abs().max().max() <= 1
    # Every day has positive 00-08 and negative 08-24 returns.
    idx = market["close"].index
    starts = idx - pd.Timedelta(minutes=30)
    r = np.where(starts.hour < 8, .001, -.001)
    p = pd.DataFrame({"BTC": np.exp(np.cumsum(r))}, index=idx)
    cfg = Config(factor_session_weight=.2, factor_session_days=2)
    s = build_factors(p, cfg)["session"]["BTC"]
    assert s.loc["2024-01-02 23:30" : "2024-01-02 23:30"].isna().all()
    assert s.loc["2024-01-03 00:00"] > 0


@pytest.mark.parametrize("kwargs", [{"factor_mr_weight": -1}, {"factor_rs_weight": float("nan")},
    {"factor_rs_weight": .8, "factor_volume_weight": .8}, {"factor_session_days": 1.5},
    {"factor_volume_h": 0}, {"factor_session_split_utc": 24}, {"bar_min": 0}])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


def test_archive_timestamp_units():
    ms = 1735689600000
    a = parse_klines([[ms, 100, 102, 99, 101, 50]], 30)
    b = parse_klines([[ms * 1000, 100, 102, 99, 101, 50]], 30)
    pd.testing.assert_frame_equal(a, b)
    assert a.index[0] == pd.Timestamp("2025-01-01 00:30", tz="UTC")


def test_live_feed_refreshes_cache_and_filters_open_candle(tmp_path):
    cfg = Config(data_dir=str(tmp_path), history_days=1)
    now = pd.Timestamp("2025-01-01 01:15", tz="UTC")
    start = pd.Timestamp("2025-01-01", tz="UTC")
    rows = [[int((start + pd.Timedelta(minutes=30 * i)).timestamp() * 1000),
             100, 102, 99, 101, 50] for i in range(3)]
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return rows
    class Session:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            return Response()
    session = Session()
    first = fetch_bars("BTCUSDT", cfg, session, now)
    assert first.index[-1] == now.floor("30min")
    assert len(first) == 2
    count = session.calls
    fetch_bars("BTCUSDT", cfg, session, now)
    assert session.calls > count


def test_replay_prefix_invariance_and_costs(market):
    cfg = Config(activity_enabled=False, factor_mr_weight=.2)
    a = replay(market, cfg, "2024-02-01", "2024-02-20")
    b = replay(market, cfg, "2024-02-01", "2024-02-25")
    pd.testing.assert_series_equal(a["equity"], b["equity"].loc[:"2024-02-20 00:00"])
    assert len(a["trades"]) > 0
    assert a["cost"].sum() > 0
    assert a["gross"].between(0, 1 + 1e-10).all()
    assert (a["trades"].fill_bar_close > a["trades"].decision_time).all()


def test_replay_requires_actual_data(market):
    bad = {k: v.copy() for k, v in market.items()}
    bad["open"].iloc[-2, 0] = np.nan
    with pytest.raises(ValueError, match="complete"):
        replay(bad, Config(activity_enabled=False), "2024-02-01", "2024-02-25")


def test_selection_rejects_return_only_improvement():
    protocol = {"minimum_sharpe_gain": .1, "maximum_extra_drawdown": .02}
    baseline = {"return": .1, "sharpe": 1, "max_drawdown": .1}
    assert not qualifies({"return": .2, "sharpe": .9, "max_drawdown": .1}, baseline, protocol)
    assert not qualifies({"return": .2, "sharpe": 1.5, "max_drawdown": .2}, baseline, protocol)
    assert qualifies({"return": .2, "sharpe": 1.5, "max_drawdown": .11}, baseline, protocol)


def test_ledger_uses_next_open_and_exact_adverse_cost(monkeypatch):
    import research.factor_ablation as ablation
    idx = pd.date_range("2024-01-01", "2024-01-02", freq="30min", tz="UTC")
    close = pd.DataFrame(100.0, index=idx, columns=["BTC"])
    opened = close.copy()
    opened.iloc[1] = 101  # gap at the execution bar's open
    signals = {k: close * 0 + 1 for k in ("S", "S_slow", "trend", "sig_d")}
    monkeypatch.setattr(ablation, "compute_target_weights", lambda *a, **kw: (np.array([.5]), None, -1))
    cfg = Config(activity_enabled=False)
    result = replay({"close": close, "open": opened}, cfg,
                    "2024-01-01", "2024-01-02", signals=signals)
    quantity = 50000 / 101
    price = 101 * 1.0002
    fee = quantity * price * .001
    expected = 100000 - quantity * price - fee + quantity * 100
    assert result["equity"].iloc[-1] == pytest.approx(expected)
    assert result["cost"].sum() == pytest.approx(fee + quantity * (price - 101))
    assert len(result["trades"]) == 1


def test_cash_reserve_scales_actual_ledger_not_only_chart(market):
    cfg = Config(activity_enabled=False, factor_volume_weight=.2)
    a = replay(market, cfg, "2024-02-01", "2024-02-25")
    b = replay(market, replace(cfg, cash_reserve_usd=15000), "2024-02-01", "2024-02-25")
    np.testing.assert_allclose(b["equity"], .85*a["equity"] + 15000, atol=1e-7)
    np.testing.assert_allclose(b["cost"], .85*a["cost"], atol=1e-7)
    pd.testing.assert_series_equal(a["trades"].asset, b["trades"].asset)
    np.testing.assert_allclose(b["trades"].quantity, .85*a["trades"].quantity, atol=1e-7)
    pd.testing.assert_series_equal(a["trades"].decision_time, b["trades"].decision_time)


@pytest.mark.parametrize("reserve", [-1, float("nan"), 100000])
def test_invalid_cash_reserve(reserve):
    with pytest.raises(ValueError):
        Config(cash_reserve_usd=reserve)
