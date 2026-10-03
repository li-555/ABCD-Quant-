"""Configuration loading and validation for the Roostoo live trading bot.

All strategy / risk / execution parameters live in ``config.yaml``.  This module
exposes a single :class:`Config` dataclass plus :func:`load_config` which reads the
YAML file, validates it, and applies a couple of environment-driven runtime flags
(``LIVE``, secrets).  Secrets are NEVER stored in the YAML file - they are injected
through environment variables only.
"""
from __future__ import annotations

import os
import math
from dataclasses import dataclass, field, fields
from typing import List

import yaml


@dataclass
class Config:
    # ---- timing -----------------------------------------------------------
    bar_min: int = 30                 # bar length in minutes (30m klines)
    loop_seconds: int = 1800          # scheduler period (aligned to bar closes)
    rebalance_hour_utc: int = 0       # daily rebalance fires after this UTC hour

    # ---- selection --------------------------------------------------------
    n_long: int = 6                    # max long positions
    hold_rank_long: int = 10          # incumbent protection rank
    gate_thr: float = 0.2             # asset eligible if slow signal > gate_thr
    entry_thr: float = 0.2            # asset entered if score > entry_thr

    # ---- slow-layer signal windows (hours) --------------------------------
    vol_h: float = 48
    slow_fast_h: float = 48
    slow_slow_h: float = 192
    mom_h: float = 120
    don_h: float = 480
    overheat_h: float = 24
    overheat_z: float = 2.5
    w_trend: float = 0.4
    w_mom: float = 0.4
    w_don: float = 0.2

    # Optional candidates. Zero preserves v1 exactly; research decides promotion.
    factor_rs_weight: float = 0.0
    factor_mr_weight: float = 0.0
    factor_volume_weight: float = 0.0
    factor_session_weight: float = 0.0
    factor_benchmark: str = "BTC"
    factor_rs_h: float = 120
    factor_mr_h: float = 24
    factor_volume_h: float = 24
    factor_session_days: int = 20
    factor_session_split_utc: int = 8

    # ---- risk / sizing ---------------------------------------------------
    sigma_target: float = 0.04        # daily portfolio vol target
    cov_h: float = 120                # covariance window (hours)
    single_long: float = 0.35         # per-asset long cap
    single_short: float = 0.12        # per-asset short cap (unused, long-only)
    coin_cap: float = 0.70            # net exposure cap for the coin cluster
    gross_mult: float = 1.5          # cluster gross cap = gross_mult * net cap
    regime_lo: float = 0.25
    regime_span: float = 0.5
    regime_floor: float = 0.5
    dd_scale: float = 0.10
    dd_hard: float = 0.12
    dd_window_h: float = 336          # drawdown peak window (hours)
    breaker_h: float = 12             # circuit-breaker duration (hours)
    min_scale: float = 0.5            # minimum post-drawdown scale

    # ---- trading behaviour ------------------------------------------------
    band_abs: float = 0.08            # no-trade band (absolute weight)
    band_rel: float = 0.30            # no-trade band (relative to target)
    min_trade: float = 0.005          # minimum traded weight
    min_hold_h: float = 12            # minimum holding time (hours)
    stop_long: float = 1.5           # trailing stop distance (daily sigmas)
    stop_short: float = 1.2          # short stop (unused, long-only)
    cool_h: float = 3                 # cooldown after a stop (hours)
    allow_short: bool = False
    max_gross: float = 1.0            # total gross exposure cap

    # ---- cost model (bps) - only a model; live records actual commission --
    maker_bps: float = 5.0
    taker_bps: float = 10.0
    slip_bps: float = 2.0

    # ---- data ------------------------------------------------------------
    history_days: int = 60            # klines to warm up on startup
    market_data_source: str = "binance"  # or recorded Roostoo observations
    max_history_days: int = 90
    price_deviation_pct: float = 2.0  # Binance vs Roostoo price sanity check
    staleness_minutes: int = 90       # skip cycle if newest bar older than this
    binance_hosts: List[str] = field(default_factory=lambda: [
        "https://data-api.binance.vision",
        "https://api.binance.com",
        "https://api.binance.us",
    ])

    # ---- execution --------------------------------------------------------
    limit_timeout_minutes: float = 10.0
    max_order_frac: float = 0.15      # max notional per single order (frac of equity)
    max_daily_trades: int = 200       # safety cap on fills per day
    rate_limit_seconds: float = 1.0   # min spacing between API calls

    # ---- activity guard (compliance) --------------------------------------
    activity_enabled: bool = True
    min_daily_trades: int = 2
    activity_notional_frac: float = 0.005

    # ---- runtime / paths -------------------------------------------------
    live: bool = False
    roostoo_live_trading: bool = False
    roostoo_live_confirm: str = ""
    paper_start_equity: float = 100_000.0
    cash_reserve_usd: float = 0.0    # fixed cash excluded from strategy capital
    kill_file: str = "bot/KILL"
    heartbeat_file: str = "bot/logs/heartbeat.json"
    log_dir: str = "bot/logs"
    log_max_bytes: int = 5_000_000
    equity_csv: str = "bot/logs/equity.csv"
    state_file: str = "bot/state.json"
    data_dir: str = "cache"

    # ---- secrets (env only, never logged / stored) ------------------------
    roostoo_account: str = "test"
    roostoo_api_key: str = field(default="", repr=False)
    roostoo_api_secret: str = field(default="", repr=False)
    roostoo_base_url: str = "https://mock-api.roostoo.com"

    # ---- derived helpers --------------------------------------------------
    @property
    def bars_per_hour(self) -> float:
        """Bars per hour implied by ``bar_min`` (2 for 30m)."""
        return 60.0 / self.bar_min

    def h_to_bars(self, hours: float) -> int:
        """Convert a horizon in hours to a number of bars (rounded)."""
        return int(round(hours * self.bars_per_hour))

    def __post_init__(self) -> None:
        # ---- type / range sanity checks -----------------------------------
        errors: List[str] = []
        if self.roostoo_account not in ("test", "competition"):
            errors.append("roostoo_account must be test or competition")
        if self.market_data_source not in ("binance", "roostoo"):
            errors.append("market_data_source must be binance or roostoo")
        if not math.isfinite(self.cash_reserve_usd) or self.cash_reserve_usd < 0:
            errors.append("cash_reserve_usd must be finite and nonnegative")
        if not math.isfinite(self.paper_start_equity) or self.paper_start_equity <= self.cash_reserve_usd:
            errors.append("paper_start_equity must exceed cash_reserve_usd")
        weights = [self.factor_rs_weight, self.factor_mr_weight,
                   self.factor_volume_weight, self.factor_session_weight]
        if any(not math.isfinite(w) or not 0 <= w <= 1 for w in weights) or sum(weights) > 1:
            errors.append("optional factor weights must be finite, nonnegative and sum <= 1")
        for name in ("factor_rs_h", "factor_mr_h", "factor_volume_h"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                errors.append(f"{name} must be finite and positive")
        if not isinstance(self.factor_session_days, int) or self.factor_session_days < 2:
            errors.append("factor_session_days must be an integer >= 2")
        if (not isinstance(self.factor_session_split_utc, int) or
                not 1 <= self.factor_session_split_utc <= 23):
            errors.append("factor_session_split_utc must be an integer in [1, 23]")
        if any(weights) and self.bar_min not in (15, 30, 60):
            errors.append("optional factors support 15, 30 or 60 minute bars")
        if self.bar_min <= 0:
            errors.append("bar_min must be > 0")
        if self.bar_min > 0 and not (0 < self.bars_per_hour <= 60):
            errors.append("bar_min implies an implausible bars_per_hour")
        if self.n_long < 1:
            errors.append("n_long must be >= 1")
        if not (0.0 < self.sigma_target < 1.0):
            errors.append("sigma_target must be in (0, 1)")
        if not (0.0 < self.coin_cap <= self.max_gross):
            errors.append("coin_cap must be in (0, max_gross]")
        if not (0.0 < self.single_long <= self.coin_cap):
            errors.append("single_long must be in (0, coin_cap]")
        if not (0.0 <= self.regime_lo < 1.0):
            errors.append("regime_lo must be in [0, 1)")
        if self.min_trade <= 0:
            errors.append("min_trade must be > 0")
        if self.history_days <= 0 or self.history_days > self.max_history_days:
            errors.append("history_days out of range")
        if self.max_daily_trades < 1:
            errors.append("max_daily_trades must be >= 1")
        if not math.isfinite(self.max_order_frac) or not 0 < self.max_order_frac <= 1:
            errors.append("max_order_frac must be in (0, 1]")
        if any(not math.isfinite(v) or v < 0 for v in (self.taker_bps, self.slip_bps, self.maker_bps)):
            errors.append("fees/slippage must be finite and nonnegative")
        if self.gross_mult <= 0:
            errors.append("gross_mult must be > 0")
        if abs(self.w_trend + self.w_mom + self.w_don - 1.0) > 1e-6:
            errors.append("signal weights must sum to 1.0")
        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))


def load_config(path: str = "bot/config/config.yaml", *, account: str | None = None) -> Config:
    """Load ``Config`` from a YAML file and environment overrides.

    Environment variables (when set) override the YAML values for the runtime
    flags and secrets:

      * ``LIVE``  -> cfg.live (enable exchange connectivity)
      * ``ROOSTOO_LIVE_TRADING`` + ``ROOSTOO_LIVE_CONFIRM`` -> order placement
      * ``ROOSTOO_API_KEY`` / ``ROOSTOO_API_SECRET`` / ``ROOSTOO_BASE_URL``
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    known = {f.name for f in fields(Config)}
    kwargs = {}
    for key, value in raw.items():
        if key in ("roostoo_api_key", "roostoo_api_secret") and value:
            raise ValueError("Roostoo credentials must come from environment variables")
        if key in known:
            kwargs[key] = value
        else:
            # tolerate extra keys silently; config.yaml may hold comments/sections
            continue

    cfg = Config(**kwargs)

    # ---- environment overrides -------------------------------------------
    live_env = os.environ.get("LIVE")
    if live_env is not None:
        cfg.live = str(live_env).strip().lower() in ("1", "true", "yes", "on")
    live_trade_env = os.environ.get("ROOSTOO_LIVE_TRADING")
    if live_trade_env is not None:
        cfg.roostoo_live_trading = str(live_trade_env).strip().lower() in (
            "1", "true", "yes", "on"
        )
    live_confirm = os.environ.get("ROOSTOO_LIVE_CONFIRM")
    if live_confirm is not None:
        cfg.roostoo_live_confirm = str(live_confirm)
    load_credentials(cfg, account)
    base = os.environ.get("ROOSTOO_BASE_URL")
    if base:
        cfg.roostoo_base_url = base

    if cfg.live and not (cfg.roostoo_api_key and cfg.roostoo_api_secret):
        raise RuntimeError(
            "LIVE=1 requires ROOSTOO_API_KEY and ROOSTOO_API_SECRET to be set"
        )
    return cfg


def live_orders_enabled(cfg: Config) -> bool:
    """Two-step live gate for order placement."""
    return (
        cfg.live
        and cfg.roostoo_live_trading
        and cfg.roostoo_live_confirm == "I_UNDERSTAND_AND_ACCEPT_LIVE_TRADING_RISK"
    )


def load_credentials(cfg: Config, account: str | None = None) -> Config:
    """Select one credential pair; never mix test and competition credentials.

    Generic names remain a backwards-compatible option only when no profile
    is explicitly selected and neither profile-specific variable is present.
    """
    explicit = account is not None or "ROOSTOO_ACCOUNT" in os.environ
    cfg.roostoo_account = account or os.environ.get("ROOSTOO_ACCOUNT", cfg.roostoo_account)
    if cfg.roostoo_account not in ("test", "competition"):
        raise ValueError("ROOSTOO_ACCOUNT must be test or competition")
    prefix = "ROOSTOO_" + cfg.roostoo_account.upper()
    key, secret = os.environ.get(prefix + "_API_KEY", ""), os.environ.get(prefix + "_API_SECRET", "")
    if not explicit and cfg.roostoo_account == "test" and not key and not secret:
        key = os.environ.get("ROOSTOO_API_KEY", "")
        secret = os.environ.get("ROOSTOO_API_SECRET", "")
    cfg.roostoo_api_key, cfg.roostoo_api_secret = key, secret
    return cfg
