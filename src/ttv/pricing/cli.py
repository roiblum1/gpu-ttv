"""
`ttv-pricing`: capture (connected side), verify, import (offline) and list pricing snapshots.

Rollback is selecting an older snapshot with `TTV_PRICING_SNAPSHOT`; nothing is ever deleted.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from ttv.pricing.capture import CaptureError, capture
from ttv.pricing.importer import import_snapshot, list_imported
from ttv.pricing.mapping import DEFAULT_MAP_FILE, load_instance_map
from ttv.pricing.snapshot import (
    Snapshot,
    SnapshotError,
    load_snapshot,
    load_snapshot_dir,
    write_snapshot,
)
from ttv.store import MIGRATIONS, PostgresStore

DEFAULT_DIR = Path("config/pricing/snapshots")


def _parser() -> argparse.ArgumentParser:
    """
    Build the argument parser.

    Returns:
        argparse.ArgumentParser: The parser with its four subcommands.
    """
    parser = argparse.ArgumentParser(prog="ttv-pricing", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="Download the AWS price list and write a snapshot.")
    cap.add_argument("--region", required=True, help="AWS region code, e.g. us-east-1.")
    cap.add_argument("--out", type=Path, default=DEFAULT_DIR, help="Snapshot directory.")
    cap.add_argument("--map", type=Path, default=DEFAULT_MAP_FILE, help="GPU instance map.")
    cap.add_argument("--url", help="Price list URL (default: the official regional CSV).")
    cap.add_argument("--from-file", type=Path, help="Parse a saved raw CSV, no download.")
    cap.add_argument("--keep-raw", type=Path, help="Also save the raw download here.")
    cap.add_argument(
        "--allow-missing",
        action="store_true",
        help="Record a mapped instance with no On-Demand row as unpriced instead of failing.",
    )

    ver = sub.add_parser("verify", help="Check snapshot files' checksums and contents.")
    ver.add_argument("files", nargs="+", type=Path)

    imp = sub.add_parser("import", help="Import a snapshot file or directory into Postgres.")
    imp.add_argument("path", type=Path)
    imp.add_argument("--database-url", default=os.environ.get("TTV_DATABASE_URL"))

    lst = sub.add_parser("list", help="List snapshots in a directory and/or the database.")
    lst.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    lst.add_argument("--database-url", default=os.environ.get("TTV_DATABASE_URL"))
    return parser


def _summary(snap: Snapshot) -> str:
    """
    Describe a snapshot in a few lines.

    Args:
        snap (Snapshot): The snapshot.

    Returns:
        str: Id, publication, checksums and one line per rate.
    """
    lines = [
        f"{snap.snapshot_id}",
        f"  published {snap.publication_date.isoformat()}  offer version {snap.offer_version}",
        f"  raw sha256 {snap.raw_sha256} ({snap.raw_bytes} bytes)",
        f"  content sha256 {snap.content_sha256}",
        f"  {len(snap.rates)} priced, {len(snap.unpriced)} unpriced models",
    ]
    lines += [
        f"    {r.gpu_model:<36} {r.instance_type:<17} {r.kind:<5} "
        f"${r.instance_hourly_usd}/h / {r.instance_gpus} = ${r.per_gpu_hour_usd}/GPU-h"
        for r in snap.rates
    ]
    return "\n".join(lines)


def _capture(args: argparse.Namespace) -> int:
    """
    Run `capture`.

    Args:
        args (argparse.Namespace): Parsed arguments.

    Returns:
        int: Exit code.
    """
    snap = capture(
        load_instance_map(args.map),
        region=args.region,
        url=args.url,
        from_file=args.from_file,
        keep_raw=args.keep_raw,
        allow_missing=args.allow_missing,
    )
    path, written = write_snapshot(snap, args.out)
    print(_summary(snap))
    print(f"{'wrote' if written else 'unchanged'} {path}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    """
    Run `verify`.

    Args:
        args (argparse.Namespace): Parsed arguments.

    Returns:
        int: 0 when every file verifies, else 1.
    """
    failed = 0
    for path in args.files:
        try:
            print(_summary(load_snapshot(path)))
            print(f"OK {path}")
        except SnapshotError as exc:
            print(f"FAILED {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


async def _import(args: argparse.Namespace) -> int:
    """
    Run `import`: migrate the ledger schema, then import each verified snapshot.

    Args:
        args (argparse.Namespace): Parsed arguments.

    Returns:
        int: Exit code.
    """
    if not args.database_url:
        print("--database-url or TTV_DATABASE_URL is required", file=sys.stderr)
        return 2
    path: Path = args.path
    snapshots = load_snapshot_dir(path) if path.is_dir() else [load_snapshot(path)]
    store = await PostgresStore.connect(args.database_url)
    try:
        await store.migrate(MIGRATIONS)
        now = datetime.now(UTC)
        for snap in snapshots:
            done = await import_snapshot(store.conn, snap, now)
            print(f"{'imported' if done else 'already imported'} {snap.snapshot_id}")
    finally:
        await store.close()
    return 0


async def _list(args: argparse.Namespace) -> int:
    """
    Run `list`.

    Args:
        args (argparse.Namespace): Parsed arguments.

    Returns:
        int: Exit code.
    """
    for snap in load_snapshot_dir(args.dir):
        print(
            f"file  {snap.snapshot_id}  published {snap.publication_date.isoformat()}  "
            f"{len(snap.rates)} priced / {len(snap.unpriced)} unpriced"
        )
    if args.database_url:
        store = await PostgresStore.connect(args.database_url)
        try:
            for row in await list_imported(store.conn):
                print(
                    f"db    {row['snapshot_id']}  published {row['publication_date']}  "
                    f"{row['priced']} priced / {row['unpriced']} unpriced  "
                    f"imported {row['imported_at']}"
                )
        finally:
            await store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """
    Entry point of `ttv-pricing`.

    Args:
        argv (list[str] | None): Arguments, or None for sys.argv.

    Returns:
        int: Exit code (1 on a capture or snapshot error).
    """
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture":
            return _capture(args)
        if args.command == "verify":
            return _verify(args)
        if args.command == "import":
            return asyncio.run(_import(args))
        return asyncio.run(_list(args))
    except (CaptureError, SnapshotError) as exc:
        print(f"ttv-pricing {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
