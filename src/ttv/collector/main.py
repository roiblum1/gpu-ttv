"""
Entry point `ttv-collector`: read every configured manager on a cadence and store the reads.

The engine classifies the stored reads (ADR-0002 rows 3-5); this job never writes to a manager
and stores reads in our own Postgres regardless of `TTV_DRY_RUN` (ADR-0004, "The job").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from ttv.collector import CollectorSource
from ttv.collector.openmanage import KIND as OPENMANAGE_KIND
from ttv.collector.openmanage import OpenManageSource
from ttv.collector.ucs_central import KIND as UCS_CENTRAL_KIND
from ttv.collector.ucs_central import UcsCentralSource
from ttv.config import ManagersFile, Settings, load_managers
from ttv.logs import configure_logging
from ttv.models import CollectorRead
from ttv.sources.http import SourceError
from ttv.store import COLLECTOR_LOCK_KEY, MIGRATIONS, PostgresStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Manager:
    """
    One configured manager and the reader for it.

    Attributes:
        kind (str): `ucs_central` or `openmanage` (`collector_reads.kind`).
        source (CollectorSource): Its reader.
    """

    kind: str
    source: CollectorSource


def build_managers(managers: ManagersFile, settings: Settings) -> list[Manager]:
    """
    Build one reader per configured manager; nothing is contacted.

    Args:
        managers (ManagersFile): The managers file.
        settings (Settings): Runtime settings (deadlines).

    Returns:
        list[Manager]: UCS Centrals first, then OpenManage instances.

    Raises:
        ValueError: A manager is misconfigured, or two share a name.
    """
    timeout = settings.collector_timeout_seconds
    built = [
        Manager(
            UCS_CENTRAL_KIND,
            UcsCentralSource(c, timeout_seconds=timeout, domain_concurrency=c.domain_concurrency),
        )
        for c in managers.ucs_central
    ]
    built += [
        Manager(
            OPENMANAGE_KIND,
            OpenManageSource(
                o,
                timeout_seconds=timeout,
                inventory_types=o.inventory_types,
                empty_inventory_means_no_gpu=o.empty_inventory_means_no_gpu,
                pcie_fallback=o.pcie_fallback,
                gpu_concurrency=o.gpu_concurrency,
                request_timeout_seconds=settings.http_timeout_seconds,
            ),
        )
        for o in managers.openmanage
    ]
    names = [m.source.name for m in built]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"managers file names {duplicates} more than once")
    return built


async def read_one(manager: Manager, now: datetime) -> CollectorRead:
    """
    Read one manager; any failure becomes an incomplete read with no servers.

    An incomplete read never retires a server, and a manager with no complete read for
    `collector_stale_after` turns its servers UNKNOWN in the engine.

    Args:
        manager (Manager): The manager.
        now (datetime): Read time.

    Returns:
        CollectorRead: The read, or a failed one carrying the error.
    """
    started = time.monotonic()
    try:
        return await manager.source.read(now)
    except SourceError as exc:
        error = str(exc)
        log.warning("collector.read_failed", manager=manager.source.name, error=error)
    except Exception as exc:  # e.g. UnsafeRequestError: a bug, recorded and never fatal
        error = f"{manager.source.name}: {type(exc).__name__}: {exc}"
        log.exception("collector.read_crashed", manager=manager.source.name)
    finished = now + timedelta(seconds=time.monotonic() - started)
    return CollectorRead(manager.source.name, manager.kind, now, finished, False, (), (error,))


def summarize(read: CollectorRead, *, servers: bool = False) -> dict[str, Any]:
    """
    One read as printable JSON.

    Args:
        read (CollectorRead): The read.
        servers (bool): Include one line per server.

    Returns:
        dict[str, Any]: Manager, completeness, counts, errors and coverage.
    """
    out: dict[str, Any] = {
        "manager": read.manager,
        "complete": read.complete,
        "servers": len(read.servers),
        "gpu_servers": sum(bool(s.gpu_models) for s in read.servers),
        "gpu_unread": sum(s.gpu_models is None for s in read.servers),
        "errors": list(read.errors),
        "coverage": dict(read.coverage),
    }
    if servers:
        out["records"] = [
            {
                "asset": f"{s.vendor}:{s.serial_normalized}",
                "source_id": s.source_id,
                "name": s.name,
                "profile": s.profile_name,
                "profile_unresolved": s.profile_unresolved,
                "gpus": None if s.gpu_models is None else list(s.gpu_models),
            }
            for s in read.servers
        ]
    return out


async def run(
    managers: Sequence[Manager],
    store: PostgresStore | None,
    *,
    interval_seconds: float,
    once: bool,
    show_servers: bool = False,
) -> int:
    """
    Read every manager concurrently each round and store the reads; one round with `once`.

    Args:
        managers (Sequence[Manager]): The managers.
        store (PostgresStore | None): Where reads go; None stores nothing (`--dry-run`).
        interval_seconds (float): Time between round starts.
        once (bool): Run one round, print its summary and return.
        show_servers (bool): With `once`, print every server record too.

    Returns:
        int: With `once`, 0 when every read was complete, else 1; never returns otherwise.
    """
    while True:
        started = time.monotonic()
        now = datetime.now(UTC)
        reads = await asyncio.gather(*(read_one(m, now) for m in managers))
        for read in reads:
            read_id = await store.record_collector_read(read) if store else None
            log.info(
                "collector.read_stored" if store else "collector.read_not_stored",
                read_id=read_id,
                manager=read.manager,
                complete=read.complete,
                servers=len(read.servers),
                errors=len(read.errors),
            )
        if once:
            print(json.dumps([summarize(r, servers=show_servers) for r in reads], indent=2))
            return 0 if all(r.complete for r in reads) else 1
        await asyncio.sleep(max(0.0, interval_seconds - (time.monotonic() - started)))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """
    Parse command-line flags; everything else comes from `TTV_*` settings.

    Args:
        argv (list[str] | None): Arguments, or None for sys.argv.

    Returns:
        argparse.Namespace: Parsed flags.
    """
    parser = argparse.ArgumentParser(description="GPU TTV profile-less collector (read-only)")
    parser.add_argument("--once", action="store_true", help="Run one round, print it and exit.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read the managers but store nothing (no database needed).",
    )
    parser.add_argument(
        "--servers", action="store_true", help="With --once, print every server record."
    )
    parser.add_argument(
        "--print-config", action="store_true", help="Print settings and managers, then exit."
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """
    Wire the collector from settings and run it.

    Args:
        args (argparse.Namespace): Parsed flags.

    Returns:
        int: Process exit code.
    """
    settings = Settings()
    try:
        managers_file = load_managers(settings.managers_file)
    except (OSError, ValueError) as exc:
        print(f"cannot load TTV_MANAGERS_FILE: {exc}", file=sys.stderr)
        return 2
    if args.print_config:
        config = {
            "settings": settings.redacted(),
            "managers": managers_file.model_dump(mode="json"),
        }
        print(json.dumps(config, indent=2))
        return 0
    if not managers_file.ucs_central and not managers_file.openmanage:
        print("no manager configured: set TTV_MANAGERS_FILE", file=sys.stderr)
        return 2
    if not args.dry_run and not settings.database_url:
        print("TTV_DATABASE_URL is required unless --dry-run", file=sys.stderr)
        return 2
    try:
        managers = build_managers(managers_file, settings)
    except ValueError as exc:
        print(f"invalid managers file: {exc}", file=sys.stderr)
        return 2
    store: PostgresStore | None = None
    try:
        if not args.dry_run and settings.database_url:
            store = await PostgresStore.connect(settings.database_url)
            log.info("db.migrated", applied=await store.migrate(MIGRATIONS))
            while not await store.try_acquire_leadership(COLLECTOR_LOCK_KEY):
                log.info("collector.standby", reason="another collector holds the lock")
                await asyncio.sleep(settings.collector_interval_seconds)
        log.info(
            "collector.start",
            managers=[m.source.name for m in managers],
            interval_seconds=settings.collector_interval_seconds,
            timeout_seconds=settings.collector_timeout_seconds,
            storing=store is not None,
        )
        return await run(
            managers,
            store,
            interval_seconds=settings.collector_interval_seconds,
            once=args.once,
            show_servers=args.servers,
        )
    finally:
        for m in managers:
            await m.source.aclose()
        if store:
            await store.close()


def main(argv: list[str] | None = None) -> int:
    """
    Run the collector.

    Args:
        argv (list[str] | None): Arguments, or None for sys.argv.

    Returns:
        int: Exit code.
    """
    configure_logging()
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
