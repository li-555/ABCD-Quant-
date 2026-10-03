"""Bot entrypoint: ``python -m bot.main``.

Loads config + universe, wires up the audit loggers and the right exchange client
(live ``RoostooClient`` when ``LIVE=1``, otherwise the offline ``PaperClient``),
loads persistent state, and either runs a single cycle (``--once``, useful for
smoke tests) or the 7x24 scheduler loop.

Secrets come only from the environment (``ROOSTOO_API_KEY`` / ``ROOSTOO_API_SECRET``
/ ``ROOSTOO_BASE_URL``); they are never read from, written to, or logged by this
module.
"""
from __future__ import annotations

import argparse
import logging
import sys

from bot.config.settings import load_config
from bot.state import State
from bot.logging_utils import Loggers
from bot.scheduler import cycle, run, load_universe
from bot.execution.roostoo_client import RoostooClient
from bot.execution.paper_client import PaperClient

LOG = logging.getLogger("bot")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ABCD-Quant Roostoo live bot (v1)")
    p.add_argument("--config", default="bot/config/config.yaml",
                   help="path to config.yaml")
    p.add_argument("--universe", default="bot/config/universe.yaml",
                   help="path to universe.yaml")
    p.add_argument("--once", action="store_true",
                   help="run a single cycle and exit (smoke test)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--strategy", help="external strategy module:ClassName; uses shared Roostoo data")
    p.add_argument("--check-connection", action="store_true", help="read-only Roostoo authentication check")
    p.add_argument("--account", choices=["test", "competition"])
    p.add_argument("--execute", action="store_true", help="external strategy orders; also requires LIVE=1")
    p.add_argument("--pairs", nargs="+", default=["BTC/USD", "ETH/USD"])
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.strategy or args.check_connection:
        from bot.platform import main as platform_main
        forwarded = ["--config", args.config, "--pairs", *args.pairs]
        if args.strategy:
            forwarded += ["--strategy", args.strategy]
        if args.account:
            forwarded += ["--account", args.account]
        for enabled, flag in [(args.check_connection, "--check-connection"),
                              (args.once, "--once"), (args.execute, "--execute")]:
            if enabled:
                forwarded.append(flag)
        return platform_main(forwarded)
    if args.execute or args.account:
        raise ValueError("--execute/--account require --strategy or --check-connection; v1 uses environment")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        cfg = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        LOG.error("config load failed: %s", e)
        return 2

    universe = load_universe(args.universe)
    loggers = Loggers(cfg)
    loggers.set_coins([u["coin"] for u in universe])
    state = State.load(cfg.state_file)

    if cfg.live:
        LOG.info("LIVE trading enabled")
        client = RoostooClient(cfg, log_api=loggers.api_hook)
    else:
        LOG.info("paper (dry-run) mode: no real orders will be placed")
        pairs = [u["roostoo_pair"] for u in universe]
        client = PaperClient(cfg, pairs=pairs, log_api=loggers.api_hook)

    data_client = (RoostooClient(cfg, read_only=True)
                   if not cfg.live and cfg.market_data_source == 'roostoo' else None)
    try:
        if args.once:
            cycle(cfg, client, state, loggers, universe, data_client=data_client)
        else:
            run(cfg, client, state, loggers, universe, immediate=True, data_client=data_client)
    except KeyboardInterrupt:
        LOG.info("interrupted; shutting down")
    finally:
        if data_client is not None:
            data_client._session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
