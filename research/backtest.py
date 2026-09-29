#!/usr/bin/env python3
"""
Two-layer intraday long/short backtester (stocks via yfinance, coins via Binance).

Slow layer : trend + cross-sectional momentum + Donchian position  -> direction gate
Fast layer : short-horizon EMA trend + 6h channel position         -> timing
Portfolio  : top-N by score, inverse-vol weights, vol targeting, market-breadth
             regime scaling, drawdown scaling, cluster caps, no-trade band,
             min holding time, sigma-based trailing stops, maker/taker costs.

Variants (for A/B/C comparison):
  A : slow layer only, 30m bars, rebalance every 4h
  B : slow + fast,     30m bars, rebalance every 30m
  C : slow + fast,     15m bars, rebalance every 15m

Usage:
  pip install pandas numpy requests yfinance matplotlib
  python backtest.py --variant all --days 90
  python backtest.py --variant B --universe crypto --days 180
  python backtest.py --synthetic --variant all        # offline smoke test

Conventions (no look-ahead):
  * Every bar is indexed by its CLOSE time (UTC).
  * Weights decided with data up to bar t are filled at close(t) (plus slippage/fees)
    and earn the return of bar t+1.
  * Stocks only trade on bars that really exist (US regular hours). Between sessions
    the price is forward-filled, positions are frozen (except they still earn the
    overnight gap at the next open). Coins trade 24/7.
Caveats:
  * yfinance only serves ~60 days of 15m/30m bars (730 days of 60m bars), so stocks
    only have that much history. Coins can go back further (--days).
  * Maker fills are approximated by --maker-fill (share of volume assumed to fill as
    maker; the rest pays taker). Real limit-order fill rates must be measured live.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import warnings
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
STOCKS = ["CRCL", "SPCX", "SNDK", "MSTR", "TSLA", "MU", "NVDA", "SKHY", "INTC", "GOOGL",
          "AMD", "NBIS", "LITE", "MSFT", "COIN", "CBRS", "META", "QCOM", "WDC", "GLW"]
COINS = ["BTC", "ETH", "SOL", "ZEC", "XRP", "NEAR", "SUI", "WLD", "UNI", "DOGE", "BNB"]

# name -> (members, net-exposure cap). Gross cap of a cluster = 1.5 x net cap.
CLUSTERS = {
    "semis_ai": (["NVDA", "AMD", "MU", "SNDK", "WDC", "INTC", "QCOM", "NBIS", "LITE",
                  "GLW", "SKHY", "CBRS"], 0.40),
    "crypto_proxy": (["COIN", "MSTR", "CRCL"], 0.25),
    "megacap": (["MSFT", "GOOGL", "META", "TSLA"], 0.40),
    "coins": (COINS, 0.40),
}


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@dataclass
class Params:
    bar_min: int = 30
    rebal_min: int = 30
    use_fast: bool = True
    allow_short: bool = True
    # signal windows (hours)
    vol_h: float = 48
    slow_fast_h: float = 48
    slow_slow_h: float = 192
    mom_h: float = 120
    don_h: float = 480
    fast_fast_h: float = 4
    fast_slow_h: float = 16
    ch_fast_h: float = 6
    overheat_h: float = 24
    overheat_z: float = 2.5
    w_slow: float = 0.7
    rev_w: float = 0.0             # weight of rank(-CLV) reversal term (WorldQuant-style alpha); 0 = off
    rev_h: float = 24              # window (hours) for the close-location-in-range
    # selection
    n_long: int = 6
    n_short: int = 3
    hold_rank_long: int = 10
    hold_rank_short: int = 5
    gate_thr: float = 0.2
    entry_thr: float = 0.2
    # sizing / risk
    sigma_target: float = 0.02     # daily portfolio vol target
    cov_h: float = 120
    short_frac: float = 0.4        # short book <= 0.4 x long book
    max_gross: float = 1.0
    single_long: float = 0.20
    single_short: float = 0.12
    coin_cap: float = 0.40         # net cap for the whole coin cluster (raise for crypto-only runs)
    regime_lo: float = 0.25        # breadth (share of assets in uptrend) below which long book is cut
    regime_span: float = 0.50      # long book reaches 100% once breadth >= regime_lo + regime_span
    regime_floor: float = 0.20     # minimum long-book multiplier from the breadth filter
    dd_scale: float = 0.10
    dd_hard: float = 0.12
    dd_window_h: float = 336       # drawdown measured vs peak of last N hours (0 = all-time peak)
    breaker_h: float = 12
    min_scale: float = 0.25
    # trading frictions / behavior
    band_abs: float = 0.03
    band_rel: float = 0.30
    min_trade: float = 0.005
    min_hold_h: float = 2.0
    stop_long: float = 1.5         # stop distance in daily sigmas from the extreme
    stop_short: float = 1.2
    cool_h: float = 3.0
    maker_bps: float = 5.0
    taker_bps: float = 10.0
    maker_fill: float = 0.6
    slip_bps: float = 2.0


VARIANTS = {
    "A": dict(bar_min=30, rebal_min=240, use_fast=False),
    "B": dict(bar_min=30, rebal_min=30, use_fast=True),
    "C": dict(bar_min=15, rebal_min=15, use_fast=True),
}


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com",
                 "https://api.binance.us"]
BIN_INTERVAL = {15: "15m", 30: "30m", 60: "1h"}
YF_INTERVAL = {15: "15m", 30: "30m", 60: "60m"}


def _cache_path(cache_dir, kind, sym, bar_min, tag=""):
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{kind}_{sym}_{bar_min}m{tag}.csv")


def _load_cache(path):
    s = pd.read_csv(path, index_col=0)
    s.index = pd.to_datetime(s.index, utc=True)
    return s.iloc[:, 0]


def fetch_binance(sym, bar_min, days, cache_dir, refresh):
    import requests
    path = _cache_path(cache_dir, "bn", sym, bar_min, f"_{days}d")
    if os.path.exists(path) and not refresh:
        return _load_cache(path)
    interval = BIN_INTERVAL[bar_min]
    step_ms = bar_min * 60_000
    end = int(time.time() * 1000)
    cur = end - int(days * 86_400_000)
    rows = []
    base_ok = None
    while cur < end:
        data = None
        for base in ([base_ok] if base_ok else BINANCE_BASES):
            try:
                r = requests.get(f"{base}/api/v3/klines", timeout=15,
                                 params=dict(symbol=f"{sym}USDT", interval=interval,
                                             startTime=cur, limit=1000))
                if r.status_code == 200:
                    data, base_ok = r.json(), base
                    break
            except Exception:
                continue
        if not data:
            break
        rows += data
        cur = data[-1][0] + step_ms
        if len(data) < 1000:
            break
        time.sleep(0.1)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    close_t = pd.to_datetime(df[0], unit="ms", utc=True) + pd.Timedelta(minutes=bar_min)
    s = pd.Series(df[4].astype(float).values, index=close_t, name=sym)
    s = s[s.index <= pd.Timestamp.now(tz="UTC")]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s.to_csv(path)
    return s


def fetch_stock(sym, bar_min, cache_dir, refresh):
    import yfinance as yf
    path = _cache_path(cache_dir, "yf", sym, bar_min)
    if os.path.exists(path) and not refresh:
        return _load_cache(path)
    period = "730d" if bar_min >= 60 else "60d"
    try:
        h = yf.Ticker(sym).history(period=period, interval=YF_INTERVAL[bar_min],
                                   auto_adjust=True, actions=False)
    except Exception:
        return None
    if h is None or h.empty:
        return None
    idx = h.index
    if idx.tz is None:
        idx = idx.tz_localize("America/New_York")
    close_t = idx + pd.Timedelta(minutes=bar_min)
    cap = idx.normalize() + pd.Timedelta(hours=16)      # session closes 16:00 ET
    close_t = close_t.where(close_t <= cap, cap)
    s = pd.Series(h["Close"].values.astype(float), index=close_t.tz_convert("UTC"), name=sym)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s.to_csv(path)
    return s


def to_grid(series, freq):
    return series.resample(freq, label="right", closed="right").last()


def load_raw_prices(universe, bar_min, days, cache_dir, refresh):
    freq = f"{bar_min}min"
    cols, missing = {}, []
    if universe in ("all", "crypto"):
        for c in COINS:
            s = fetch_binance(c, bar_min, days, cache_dir, refresh)
            if s is None or s.empty:
                missing.append(c)
            else:
                cols[c] = to_grid(s, freq)
    if universe in ("all", "stocks"):
        for t in STOCKS:
            s = fetch_stock(t, bar_min, cache_dir, refresh)
            if s is None or s.empty:
                missing.append(t)
            else:
                cols[t] = to_grid(s, freq)
    if missing:
        print(f"[data] no data for: {', '.join(missing)} (skipped)")
    if not cols:
        sys.exit("No data downloaded. Check network / tickers, or use --synthetic.")
    raw = pd.concat(cols, axis=1)
    grid = pd.date_range(raw.index.min(), raw.index.max(), freq=freq, tz="UTC")
    return raw.reindex(grid)


DRIFT_SD = 0.000005


def make_synthetic(bar_min, days, universe, seed=7):
    rng = np.random.default_rng(seed)
    end = pd.Timestamp.now(tz="UTC").floor(f"{bar_min}min")
    grid = pd.date_range(end=end, periods=int(days * 24 * 60 / bar_min), freq=f"{bar_min}min")
    names = (COINS if universe in ("all", "crypto") else []) + \
            (STOCKS if universe in ("all", "stocks") else [])
    T, n = len(grid), len(names)
    sc = np.sqrt(bar_min / 30)
    mkt = rng.normal(0, 0.0015 * sc, T)
    beta = rng.uniform(0.5, 1.5, n)
    drift = np.zeros((T, n))
    x = np.zeros(n)
    for t in range(T):
        x = 0.999 * x + rng.normal(0, DRIFT_SD * sc, n)
        drift[t] = x
    noise = rng.normal(0, 1, (T, n)) * np.where(np.isin(names, COINS), 0.004, 0.0025) * sc
    logp = np.cumsum(mkt[:, None] * beta + drift + noise, axis=0)
    px = pd.DataFrame(100 * np.exp(logp), index=grid, columns=names)
    hm = grid.hour * 60 + grid.minute
    open_ = (grid.dayofweek < 5) & (hm > 13 * 60 + 30) & (hm <= 20 * 60)
    for c in names:
        if c in STOCKS:
            px.loc[~open_, c] = np.nan
    return px


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
def build_signals(P, real, prm: Params):
    bph = 60.0 / prm.bar_min
    H = lambda h: max(2, int(round(h * bph)))
    logP = np.log(P)
    R = logP.diff()
    vb = H(prm.vol_h)
    sig = np.sqrt((R ** 2).ewm(span=vb, min_periods=max(2, vb // 2), adjust=False).mean())
    sig = sig.clip(lower=1e-4)

    def ema(x, h):
        n = H(h)
        return x.ewm(span=n, min_periods=n, adjust=False).mean()

    # ---- slow layer
    ns = H(prm.slow_slow_h)
    trend = np.tanh((ema(logP, prm.slow_fast_h) - ema(logP, prm.slow_slow_h)) / (sig * np.sqrt(ns)))
    nm = H(prm.mom_h)
    m = (logP - logP.shift(nm)) / (sig * np.sqrt(nm))
    mom = 2 * m.rank(axis=1, pct=True) - 1
    nd = H(prm.don_h)
    hi, lo = P.rolling(nd).max(), P.rolling(nd).min()
    don = ((P - (hi + lo) / 2) / (0.5 * (hi - lo))).replace([np.inf, -np.inf], np.nan).clip(-1, 1)
    S_slow = 0.4 * trend + 0.4 * mom + 0.2 * don
    n1 = H(prm.overheat_h)
    z1 = (logP - logP.shift(n1)) / (sig * np.sqrt(n1))
    hot = (z1.abs() > prm.overheat_z) & (np.sign(z1) == np.sign(S_slow))
    S_slow = S_slow.where(~hot, S_slow * 0.5)

    # ---- fast layer
    if prm.use_fast:
        nf = H(prm.fast_slow_h)
        tf = np.tanh((ema(logP, prm.fast_fast_h) - ema(logP, prm.fast_slow_h)) / (sig * np.sqrt(nf)))
        nc = H(prm.ch_fast_h)
        h6, l6 = P.rolling(nc).max(), P.rolling(nc).min()
        ch = ((P - (h6 + l6) / 2) / (0.5 * (h6 - l6))).replace([np.inf, -np.inf], 0).clip(-1, 1)
        S_fast = 0.6 * tf + 0.4 * ch
        S = prm.w_slow * S_slow + (1 - prm.w_slow) * S_fast
    else:
        S = S_slow

    if prm.rev_w:
        # WorldQuant-style: rank(-(2*close - low - high)/(high - low)), here with rolling
        # close-based high/low over rev_h hours (approximation: no true intrabar high/low)
        nr = H(prm.rev_h)
        hi_r, lo_r = P.rolling(nr).max(), P.rolling(nr).min()
        clv = ((2 * P - hi_r - lo_r) / (hi_r - lo_r)).replace([np.inf, -np.inf], np.nan)
        rev = 2 * (-clv).rank(axis=1, pct=True) - 1
        S = S + prm.rev_w * rev

    return dict(S=S, S_slow=S_slow, trend=trend, sig_d=sig * np.sqrt(24 * bph))


# --------------------------------------------------------------------------- #
# Backtest engine
# --------------------------------------------------------------------------- #
def select(cand, score, held, N, hold_n):
    ci = np.where(cand)[0]
    if len(ci) == 0:
        return []
    order = ci[np.argsort(-score[ci], kind="stable")]
    rank = {int(i): r for r, i in enumerate(order)}
    kept = [int(i) for i in order if held[i] and rank[int(i)] < hold_n][:N]
    ks = set(kept)
    fill = [int(i) for i in order if int(i) not in ks][:N - len(kept)]
    return kept + fill


def run_backtest(P, real, sg, prm: Params, capital=100_000.0):
    idx, cols = P.index, list(P.columns)
    T, n = P.shape
    Pv = P.values.astype(float)
    rv = real.values
    S, Ss, TR, SD = (sg[k].values for k in ("S", "S_slow", "trend", "sig_d"))

    ret = np.zeros((T, n))
    lr = np.zeros((T, n))
    with np.errstate(all="ignore"):
        ret[1:] = Pv[1:] / Pv[:-1] - 1
        lr[1:] = np.log(Pv[1:] / Pv[:-1])
    ret = np.nan_to_num(ret, nan=0.0, posinf=0.0, neginf=0.0)
    lr = np.nan_to_num(lr, nan=0.0, posinf=0.0, neginf=0.0)

    bph = 60.0 / prm.bar_min
    bpd = 24 * bph
    cov_n = max(10, int(round(prm.cov_h * bph)))
    min_hold = int(round(prm.min_hold_h * bph))
    cool = int(round(prm.cool_h * bph))
    brk = int(round(prm.breaker_h * bph))
    minute = (idx.hour * 60 + idx.minute).values
    reb = (minute % prm.rebal_min == 0)
    clusters = [([cols.index(c) for c in names if c in cols],
                 prm.coin_cap if cname == "coins" else cap)
                for cname, (names, cap) in CLUSTERS.items()]
    clusters = [(ix, cap) for ix, cap in clusters if ix]

    valid_cnt = np.isfinite(S).sum(1)
    ok_start = np.where(valid_cnt >= 3)[0]
    if len(ok_start) == 0:
        sys.exit("Not enough history to compute signals. Increase --days.")
    start = int(ok_start[0])

    fee_mix = prm.maker_fill * prm.maker_bps + (1 - prm.maker_fill) * prm.taker_bps
    w = np.zeros(n)
    entry = np.full(n, -10 ** 9)
    ext = np.full(n, np.nan)
    cooldown = np.full(n, -1)
    eq, peak, breaker_until = 1.0, 1.0, -1
    dd_win = int(round(prm.dd_window_h * bph)) if prm.dd_window_h > 0 else 0
    is_crypto = np.array([c in COINS for c in cols])
    c_long, c_short = np.zeros(T), np.zeros(T)
    c_crypto, c_stock = np.zeros(T), np.zeros(T)
    asset_pnl = np.zeros(n)
    c_lc, c_sc, c_ls, c_ss = np.zeros(T), np.zeros(T), np.zeros(T), np.zeros(T)
    eq_arr = np.full(T, np.nan)
    stock_cols = ~is_crypto
    pend = False          # a rebalance tick fell outside stock hours -> redo at next open
    turn_arr = np.zeros(T)
    cost_arr = np.zeros(T)
    gross_arr = np.zeros(T)
    net_arr = np.zeros(T)
    trades = []

    def apply_caps(v, frozen, trad_mask):
        # Cluster net/gross caps must account for positions that can't be traded
        # this bar (frozen = carried-over weight on non-tradeable assets, e.g. a
        # closed-market stock) -- otherwise a full cluster of frozen exposure is
        # invisible to the cap and the *realized* portfolio can end up over cap.
        v = v.copy()
        for _ in range(3):
            v = np.clip(v, -prm.single_short, prm.single_long)
            for ix, cap in clusters:
                ix = np.asarray(ix)
                tr = trad_mask[ix]
                total = np.where(tr, v[ix], frozen[ix])
                net, gross = total.sum(), np.abs(total).sum()
                f = 1.0
                if abs(net) > cap:
                    f = min(f, cap / abs(net))
                if gross > 1.5 * cap:
                    f = min(f, 1.5 * cap / gross)
                if f < 1 and tr.any():
                    v[ix[tr]] *= f
        return np.clip(v, -prm.single_short, prm.single_long)

    for t in range(start, T):
        # 1) mark to market bar t
        r = ret[t]
        pr = float(w @ r)
        contrib = w * r                      # P&L contribution (fraction of equity)
        c_long[t], c_short[t] = contrib[w > 0].sum(), contrib[w < 0].sum()
        c_crypto[t], c_stock[t] = contrib[is_crypto].sum(), contrib[~is_crypto].sum()
        asset_pnl += contrib
        lm_, sm_ = w > 0, w < 0
        c_lc[t], c_sc[t] = contrib[lm_ & is_crypto].sum(), contrib[sm_ & is_crypto].sum()
        c_ls[t], c_ss[t] = contrib[lm_ & ~is_crypto].sum(), contrib[sm_ & ~is_crypto].sum()
        eq *= (1 + pr)
        if 1 + pr > 0:
            w = w * (1 + r) / (1 + pr)
        if dd_win and t > start:
            peak = max(float(np.nanmax(eq_arr[max(start, t - dd_win + 1):t])), eq)
        else:
            peak = max(peak, eq)
        dd = 1 - eq / peak
        cost_t, turn_t = 0.0, 0.0

        # 2) trailing stops (market orders -> taker)
        for i in np.where(w != 0)[0]:
            if not rv[t, i] or not np.isfinite(SD[t, i]) or not np.isfinite(ext[i]):
                continue
            p = Pv[t, i]
            if w[i] > 0:
                ext[i] = max(ext[i], p)
                hit = p <= ext[i] * (1 - prm.stop_long * SD[t, i])
            else:
                ext[i] = min(ext[i], p)
                hit = p >= ext[i] * (1 + prm.stop_short * SD[t, i])
            if hit:
                tn = abs(w[i])
                cost_t += tn * (prm.taker_bps + prm.slip_bps) / 1e4
                turn_t += tn
                trades.append((idx[t], cols[i], w[i], 0.0, "stop"))
                w[i], entry[i], ext[i] = 0.0, -10 ** 9, np.nan
                cooldown[i] = t + cool

        # 3) rebalance
        stock_real = bool(stock_cols.any() and rv[t, stock_cols].any())
        if reb[t]:
            pend = True
        if reb[t] or (pend and stock_real):
            if stock_real:
                pend = False
            if dd > prm.dd_hard and t >= breaker_until:
                breaker_until = t + brk
            m_dd = prm.min_scale if t < breaker_until else \
                float(np.clip(1 - dd / prm.dd_scale, prm.min_scale, 1.0))

            s, ss, tr, sd = S[t], Ss[t], TR[t], SD[t]
            valid = np.isfinite(s) & np.isfinite(ss) & np.isfinite(sd) & np.isfinite(Pv[t])
            trad = valid & rv[t]
            al = valid & (ss > prm.gate_thr)
            as_ = (valid & (ss < -prm.gate_thr)) if prm.allow_short else np.zeros(n, bool)
            okm = trad & (cooldown <= t)
            s0 = np.nan_to_num(s)
            long_sel = select(okm & al & (s0 > prm.entry_thr), s0, w > 0, prm.n_long, prm.hold_rank_long)
            short_sel = select(okm & as_ & (s0 < -prm.entry_thr), -s0, w < 0, prm.n_short,
                               prm.hold_rank_short) if prm.allow_short else []

            vt = np.isfinite(tr)
            b = float((tr[vt] > 0).mean()) if vt.sum() >= 3 else 0.5
            m_l = float(np.clip((b - prm.regime_lo) / prm.regime_span, prm.regime_floor, 1.0))
            m_s = float(np.clip(((1 - b) - prm.regime_lo) / prm.regime_span, prm.regime_floor, 1.0))

            wl, ws = np.zeros(n), np.zeros(n)
            if long_sel:
                u = np.abs(s0[long_sel]) / sd[long_sel]
                wl[long_sel] = u / u.sum()
            if short_sel:
                u = np.abs(s0[short_sel]) / sd[short_sel]
                ws[short_sel] = u / u.sum()

            sel = long_sel + short_sel
            g_vol = 1.0
            if sel:
                unit = (wl - prm.short_frac * ws)[sel]
                Rw = lr[max(0, t - cov_n + 1):t + 1][:, sel]
                if Rw.shape[0] > 5:
                    C = np.atleast_2d(np.cov(Rw.T)) * bpd
                    C = 0.5 * C + 0.5 * np.diag(np.diag(C))
                    sp = float(np.sqrt(max(unit @ C @ unit, 1e-12)))
                    g_vol = min(1.0, prm.sigma_target / sp)

            tgt = g_vol * m_dd * (m_l * wl - prm.short_frac * m_s * ws)
            tgt = apply_caps(tgt, w, trad)
            frozen_g = float(np.abs(w[~trad]).sum())
            avail = max(prm.max_gross - frozen_g, 0.0)
            g = float(np.abs(tgt[trad]).sum())
            if g > avail and g > 0:
                tgt[trad] *= avail / g
            tgt[~trad] = w[~trad]

            new_w = w.copy()
            for i in np.where(trad)[0]:
                cur, tg = w[i], tgt[i]
                d = abs(tg - cur)
                if tg == 0 and abs(cur) < prm.min_trade:
                    new_w[i] = 0.0
                    continue
                if d < prm.min_trade:
                    continue
                exit_full, flip = (tg == 0), (cur * tg < 0)
                band = max(prm.band_abs, prm.band_rel * abs(tg))
                if not (cur == 0 or exit_full or flip or d > band):
                    continue
                if cur != 0 and (t - entry[i]) < min_hold and \
                        (exit_full or flip or abs(tg) < abs(cur)):
                    if (cur > 0 and al[i]) or (cur < 0 and as_[i]):
                        continue
                new_w[i] = tg

            dlt = new_w - w
            tn = float(np.abs(dlt).sum())
            if tn > 0:
                cost_t += tn * (fee_mix + prm.slip_bps) / 1e4
                turn_t += tn
                for i in np.where(dlt != 0)[0]:
                    trades.append((idx[t], cols[i], w[i], new_w[i], "rebal"))
                    if w[i] == 0 or w[i] * new_w[i] < 0:
                        entry[i], ext[i] = t, Pv[t, i]
                    if new_w[i] == 0:
                        entry[i], ext[i] = -10 ** 9, np.nan
                w = new_w

        eq *= (1 - cost_t)
        eq_arr[t], turn_arr[t], cost_arr[t] = eq, turn_t, cost_t
        gross_arr[t], net_arr[t] = np.abs(w).sum(), w.sum()

    sl = slice(start, T)
    tdf = pd.DataFrame(trades, columns=["time", "asset", "w_from", "w_to", "reason"])
    return dict(
        equity=pd.Series(eq_arr[sl] * capital, index=idx[sl]),
        turnover=pd.Series(turn_arr[sl], index=idx[sl]),
        cost=pd.Series(cost_arr[sl], index=idx[sl]),
        gross=pd.Series(gross_arr[sl], index=idx[sl]),
        net=pd.Series(net_arr[sl], index=idx[sl]),
        pnl_long=float(c_long.sum()), pnl_short=float(c_short.sum()),
        pnl_crypto=float(c_crypto.sum()), pnl_stock=float(c_stock.sum()),
        pnl_long_crypto=float(c_lc.sum()), pnl_short_crypto=float(c_sc.sum()),
        pnl_long_stock=float(c_ls.sum()), pnl_short_stock=float(c_ss.sum()),
        asset_pnl=pd.Series(asset_pnl, index=cols).sort_values(),
        trades=tdf,
    )


# --------------------------------------------------------------------------- #
# Metrics (Composite = 0.4 Sortino + 0.3 Sharpe + 0.3 Calmar)
# --------------------------------------------------------------------------- #
def perf(eq: pd.Series, freq="1D"):
    eq = eq.dropna()
    nan = dict(ret=np.nan, sharpe=np.nan, sortino=np.nan, calmar=np.nan, mdd=np.nan, composite=np.nan)
    if len(eq) < 4:
        return nan
    e = eq.resample(freq).last().dropna()
    vals = np.r_[eq.iloc[0], e.values]
    r = np.diff(vals) / vals[:-1]
    if len(r) < 2:
        return nan
    step_h = pd.Timedelta(freq).total_seconds() / 3600
    ppy = 365 * 24 / step_h
    tot = eq.iloc[-1] / eq.iloc[0] - 1
    mdd = abs((eq / eq.cummax() - 1).min())
    sd = r.std(ddof=1)
    dn = np.sqrt(np.mean(np.minimum(r, 0) ** 2))
    sharpe = r.mean() / sd * np.sqrt(ppy) if sd > 0 else np.nan
    sortino = r.mean() / dn * np.sqrt(ppy) if dn > 0 else np.nan
    ann = (1 + tot) ** (ppy / len(r)) - 1 if 1 + tot > 0 else -1.0
    calmar = ann / mdd if mdd > 0 else np.nan
    clip = lambda x: float(np.clip(x, -100, 100)) if np.isfinite(x) else np.nan
    sharpe, sortino, calmar = clip(sharpe), clip(sortino), clip(calmar)
    comp = 0.4 * sortino + 0.3 * sharpe + 0.3 * calmar
    return dict(ret=float(tot), sharpe=sharpe, sortino=sortino, calmar=calmar,
                mdd=float(mdd), composite=float(comp))


def rolling_windows(eq, win_days=14, freq="1D"):
    starts = pd.date_range(eq.index[0].ceil("1D"), eq.index[-1] - pd.Timedelta(days=win_days), freq="1D")
    rows = []
    for s in starts:
        seg = eq[s:s + pd.Timedelta(days=win_days)]
        if len(seg) > 10:
            rows.append(perf(seg, freq))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run_variant(name, args, data_cache):
    prm = replace(Params(), **VARIANTS[name])
    overrides = dict(n_long=args.n_long, n_short=args.n_short, sigma_target=args.sigma_target,
                     stop_long=args.stop_long, stop_short=args.stop_short,
                     maker_fill=args.maker_fill, allow_short=not args.no_short)
    prm = replace(prm, **{k: v for k, v in overrides.items() if v is not None})
    fields = Params.__dataclass_fields__
    for kv in args.set:
        k, v = kv.split("=")
        if k not in fields:
            sys.exit(f"unknown param: {k}")
        typ = type(getattr(prm, k))
        prm = replace(prm, **{k: (v.lower() in ("1", "true", "yes")) if typ is bool else typ(v)})

    if prm.bar_min not in data_cache:
        if args.synthetic:
            raw = make_synthetic(prm.bar_min, args.days, args.universe)
        else:
            raw = load_raw_prices(args.universe, prm.bar_min, args.days, args.cache_dir, args.refresh)
        if args.start:
            raw = raw[raw.index >= pd.Timestamp(args.start, tz="UTC")]
        if args.end:
            raw = raw[raw.index <= pd.Timestamp(args.end, tz="UTC")]
        data_cache[prm.bar_min] = raw
    raw = data_cache[prm.bar_min]
    print(f"[data] {raw.shape[1]} assets, {raw.index[0]:%Y-%m-%d} -> {raw.index[-1]:%Y-%m-%d} ({(raw.index[-1]-raw.index[0]).days} days)")
    if args.exclude:
        raw = raw.drop(columns=[c for c in args.exclude.split(",") if c in raw.columns])
    real = raw.notna()
    P = raw.ffill()
    sg = build_signals(P, real, prm)
    res = run_backtest(P, real, sg, prm, capital=args.capital)

    # true equal-weight buy&hold: buy once at the start of the backtest window
    # (equal dollar amount per asset with a valid price at that bar), hold the
    # resulting quantities unchanged for the rest of the period. This is NOT
    # the same as rebalancing to equal weight every bar (which was the old,
    # incorrect implementation here) -- a rebalanced-every-bar equal-weight
    # index earns a "rebalancing premium" in choppy/dispersed baskets and so
    # runs ahead of true buy&hold.
    start_ts = res["equity"].index[0]
    Pw = P.loc[start_ts:]
    p0 = Pw.iloc[0]
    assets0 = p0.index[p0.notna() & (p0 > 0)]
    qty = (1.0 / len(assets0)) / p0[assets0]
    bench = (Pw[assets0] * qty).sum(axis=1)
    bench = bench.reindex(res["equity"].index).ffill()
    res["bench_ret"] = float(bench.iloc[-1] / bench.iloc[0] - 1)
    res["prm"] = prm
    return res


def summarize(name, res, args):
    eq = res["equity"]
    full = perf(eq, args.metric_freq)
    days = max((eq.index[-1] - eq.index[0]).total_seconds() / 86400, 1e-9)
    tr = res["trades"]
    row = dict(
        variant=name,
        days=round(days, 1),
        ret=full["ret"], maxDD=full["mdd"],
        sharpe=full["sharpe"], sortino=full["sortino"], calmar=full["calmar"],
        composite=full["composite"],
        bench_ret=res["bench_ret"],
        turnover_per_day=res["turnover"].sum() / days,
        cost_drag=res["cost"].sum(),
        avg_gross=res["gross"].mean(), avg_net=res["net"].mean(),
        pnl_long=res["pnl_long"], pnl_short=res["pnl_short"],
        pnl_crypto=res["pnl_crypto"], pnl_stock=res["pnl_stock"],
        pnl_long_crypto=res["pnl_long_crypto"], pnl_short_crypto=res["pnl_short_crypto"],
        pnl_long_stock=res["pnl_long_stock"], pnl_short_stock=res["pnl_short_stock"],
        gross_pre_cost=res["pnl_long"] + res["pnl_short"],
        edge_bps_per_turnover=(res["pnl_long"] + res["pnl_short"]) * 1e4 / max(res["turnover"].sum(), 1e-9),
        trades=len(tr), stops=int((tr.reason == "stop").sum()) if len(tr) else 0,
    )
    rw = rolling_windows(eq, args.window_days, args.metric_freq)
    if len(rw):
        row.update({
            f"win{args.window_days}d_n": len(rw),
            f"win_ret_med": rw["ret"].median(),
            f"win_ret_mean": rw["ret"].mean(),
            f"win_ret_p90": rw["ret"].quantile(0.90),
            f"win_ret_p10": rw["ret"].quantile(0.10),
            f"win_pos_share": (rw["ret"] > 0).mean(),
            f"win_comp_med": rw["composite"].median(),
            f"win_comp_p10": rw["composite"].quantile(0.10),
        })
    return row


def run_grid(args):
    import copy
    import itertools
    if args.variant == "all":
        sys.exit("--grid requires an explicit --variant (A, B, or C); "
                 "it does not sweep A/B/C together. Re-run with e.g. --variant A --grid ...")
    keys = [g.split("=")[0] for g in args.grid]
    vals = [g.split("=")[1].split(",") for g in args.grid]
    nm = args.variant
    data_cache, rows = {}, []
    for combo in itertools.product(*vals):
        a = copy.copy(args)
        a.set = list(args.set) + [f"{k}={v}" for k, v in zip(keys, combo)]
        res = run_variant(nm, a, data_cache)
        row = summarize(nm, res, a)
        row["variant"] = " ".join(f"{k}={v}" for k, v in zip(keys, combo))
        rows.append(row)
    cols = ["variant", "days", "ret", "maxDD", "sharpe", "composite", "edge_bps_per_turnover",
            "turnover_per_day", "cost_drag", "avg_gross", "pnl_long", "pnl_short", "pnl_crypto",
            "pnl_stock", "win_ret_med", "win_ret_mean", "win_ret_p10", "win_ret_p90", "win_pos_share", "win_comp_med"]
    tab = pd.DataFrame(rows)
    tab = tab[[c for c in cols if c in tab.columns]].set_index("variant")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print("\n=== Grid results ===")
    print(tab.round(4).to_string())
    tab.to_csv(os.path.join(args.out_dir, "grid.csv"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", default="all", choices=["A", "B", "C", "all"])
    ap.add_argument("--universe", default="all", choices=["all", "stocks", "crypto"])
    ap.add_argument("--days", type=int, default=90, help="history to fetch for coins")
    ap.add_argument("--start", default=None, help="trim data to start at YYYY-MM-DD (UTC)")
    ap.add_argument("--end", default=None, help="trim data to end at YYYY-MM-DD (UTC), for split-sample tests")
    ap.add_argument("--synthetic", action="store_true", help="offline synthetic data smoke test")
    ap.add_argument("--refresh", action="store_true", help="ignore cached downloads")
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--out-dir", default="outputs")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--metric-freq", default="1D", help="return sampling for ratios, e.g. 1D or 4h")
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--n-long", type=int, default=None)
    ap.add_argument("--n-short", type=int, default=None)
    ap.add_argument("--sigma-target", type=float, default=None)
    ap.add_argument("--stop-long", type=float, default=None)
    ap.add_argument("--stop-short", type=float, default=None)
    ap.add_argument("--maker-fill", type=float, default=None)
    ap.add_argument("--exclude", default="", help="comma-separated symbols to drop, e.g. ZEC,UNI")
    ap.add_argument("--grid", nargs="*", default=[], metavar="KEY=V1,V2",
                    help="parameter sweep on one variant, e.g. --grid rebal_min=360,720,1440 band_abs=0.03,0.05")
    ap.add_argument("--set", nargs="*", default=[], metavar="KEY=VAL",
                    help="override any Params field, e.g. --set rebal_min=720 band_abs=0.05 dd_window_h=0")
    args, _unknown = ap.parse_known_args(argv)   # ignores Jupyter/Colab/Kaggle's '-f kernel.json'

    os.makedirs(args.out_dir, exist_ok=True)
    if args.grid:
        return run_grid(args)
    names = ["A", "B", "C"] if args.variant == "all" else [args.variant]
    data_cache, rows, results = {}, [], {}
    for nm in names:
        print(f"[run] variant {nm} ...")
        res = run_variant(nm, args, data_cache)
        results[nm] = res
        rows.append(summarize(nm, res, args))
        res["equity"].to_csv(os.path.join(args.out_dir, f"equity_{nm}.csv"))
        res["trades"].to_csv(os.path.join(args.out_dir, f"trades_{nm}.csv"), index=False)

    table = pd.DataFrame(rows).set_index("variant")
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 40)
    print("\n=== Summary (Composite = 0.4*Sortino + 0.3*Sharpe + 0.3*Calmar) ===")
    print(table.T.round(4).to_string())
    table.to_csv(os.path.join(args.out_dir, "summary.csv"))
    print("\n=== P&L attribution by asset (% of initial equity, before compounding) ===")
    for nm, res in results.items():
        ap_ = (res["asset_pnl"] * 100).round(2)
        print(f"variant {nm}  worst 5: {ap_.head(5).to_dict()}")
        print(f"variant {nm}  best 5 : {ap_.tail(5).iloc[::-1].to_dict()}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(11, 5))
        for nm, res in results.items():
            (res["equity"] / res["equity"].iloc[0]).plot(ax=ax, label=f"variant {nm}")
        ax.set_title("Equity (normalized)")
        ax.legend()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(args.out_dir, "equity.png"), dpi=120)
        print(f"[out] {args.out_dir}/equity.png")
    except Exception as e:
        print(f"[plot skipped] {e}")
    print(f"[out] summary/equity/trades CSVs in {args.out_dir}/")


if __name__ == "__main__":
    main()