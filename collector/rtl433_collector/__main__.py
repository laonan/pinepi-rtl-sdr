"""Command-line entrypoint for the collector.

    python -m rtl433_collector run          # long-running service (systemd)
    python -m rtl433_collector snapshot     # render+push once, now, then exit
    python -m rtl433_collector snapshot --dry-run   # render only, log the push

Configuration comes from the environment (see :mod:`rtl433_collector.config`),
normally supplied by systemd's EnvironmentFile.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .config import Config
from .notifiers import LogNotifier, build_notifier
from .service import CollectorService, DailySnapshot
from .store import RawStore


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cmd_run(cfg: Config) -> int:
    notifier = build_notifier(cfg.push_channel, cfg.as_notifier_config())
    service = CollectorService(
        raw_file=cfg.raw_file,
        snapshot_dir=cfg.snapshot_dir,
        notifier=notifier,
        host=cfg.listen_host,
        port=cfg.listen_port,
        node_name=cfg.node_name,
        deliver_hour=cfg.deliver_hour,
        deliver_minute=cfg.deliver_minute,
        window_hours=cfg.window_hours,
        keep_snapshots=cfg.keep_snapshots,
    )

    def _handle_signal(signum, _frame):
        logging.getLogger("rtl433_collector").info("signal %s, shutting down", signum)
        service.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    service.run_forever()
    return 0


def _cmd_snapshot(cfg: Config, *, dry_run: bool) -> int:
    notifier = LogNotifier() if dry_run else build_notifier(
        cfg.push_channel, cfg.as_notifier_config()
    )
    store = RawStore(cfg.raw_file)
    snapshot = DailySnapshot(
        store,
        notifier,
        snapshot_dir=cfg.snapshot_dir,
        node_name=cfg.node_name,
        window_hours=cfg.window_hours,
        keep_snapshots=cfg.keep_snapshots,
    )
    path = snapshot.run_once()
    store.close()
    if path:
        print(path)
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rtl433_collector")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the long-running collector service")

    snap = sub.add_parser("snapshot", help="render + push one snapshot now")
    snap.add_argument(
        "--dry-run",
        action="store_true",
        help="render only; log the push instead of sending",
    )

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = Config.from_env()

    if args.command == "run":
        return _cmd_run(cfg)
    if args.command == "snapshot":
        return _cmd_snapshot(cfg, dry_run=args.dry_run)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
