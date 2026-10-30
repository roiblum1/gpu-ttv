"""Offline import of verified snapshots into Postgres, and the present-day selection (ADR-0003)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ttv.pricing.snapshot import Snapshot, SnapshotError, latest, load_snapshot_dir

# Serialises concurrent imports (two engines starting, or the CLI beside an engine).
_IMPORT_LOCK_KEY = 7_211_042_303

_INSERT_SNAPSHOT = """
INSERT INTO pricing_snapshots (snapshot_id, provider, region, purchase_option, captured_at,
    effective_at, source_urls, checksum, parser_version, service, currency, operating_system,
    tenancy, billing_unit, publication_date, offer_version, raw_sha256, content_sha256,
    map_version, catalog_version, imported_at, document)
VALUES (%(snapshot_id)s, %(provider)s, %(region)s, %(purchase_option)s, %(captured_at)s,
    %(publication_date)s, %(source_urls)s, %(content_sha256)s, %(parser_version)s, %(service)s,
    %(currency)s, %(operating_system)s, %(tenancy)s, %(billing_unit)s, %(publication_date)s,
    %(offer_version)s, %(raw_sha256)s, %(content_sha256)s, %(map_version)s, %(catalog_version)s,
    %(imported_at)s, %(document)s)
"""
_INSERT_RATE = """
INSERT INTO pricing_rates (snapshot_id, gpu_model, instance_type, instance_hourly_usd,
    instance_gpus, per_gpu_hour_usd, mapping, confidence, sku, rate_code, reason)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_SNAPSHOT_ROWS = """
SELECT snapshot_id, region, publication_date, captured_at, content_sha256, imported_at,
       (SELECT count(*) FROM pricing_rates r
         WHERE r.snapshot_id = s.snapshot_id AND r.per_gpu_hour_usd IS NOT NULL) AS priced,
       (SELECT count(*) FROM pricing_rates r
         WHERE r.snapshot_id = s.snapshot_id AND r.per_gpu_hour_usd IS NULL) AS unpriced
FROM pricing_snapshots s
ORDER BY publication_date NULLS FIRST, captured_at, snapshot_id
"""
_SELECT = """
INSERT INTO pricing_selection (singleton, snapshot_id, selected_at, source)
VALUES (true, %s, %s, %s)
ON CONFLICT (singleton) DO UPDATE SET
    snapshot_id = EXCLUDED.snapshot_id, selected_at = EXCLUDED.selected_at, source = EXCLUDED.source
WHERE pricing_selection.snapshot_id IS DISTINCT FROM EXCLUDED.snapshot_id
   OR pricing_selection.source IS DISTINCT FROM EXCLUDED.source
"""

_SNAPSHOT_FIELDS = (
    "snapshot_id",
    "provider",
    "region",
    "purchase_option",
    "parser_version",
    "service",
    "currency",
    "operating_system",
    "tenancy",
    "billing_unit",
    "offer_version",
    "raw_sha256",
    "content_sha256",
    "map_version",
    "catalog_version",
)

type Conn = psycopg.AsyncConnection[Any]  # dict rows (PostgresStore.connect)


class SnapshotConflictError(SnapshotError):
    """A different snapshot document already holds this immutable snapshot id."""


@dataclass(frozen=True)
class PresentPricing:
    """
    The present-day snapshot and the canonical models it prices.

    Attributes:
        snapshot_id (str): Selected snapshot id.
        publication_date (datetime): Its AWS publication date.
        priced (frozenset[str]): Canonical models with a rate.
        source (str): `TTV_PRICING_SNAPSHOT` or `latest`.
    """

    snapshot_id: str
    publication_date: datetime
    priced: frozenset[str]
    source: str


async def import_snapshot(conn: Conn, snap: Snapshot, now: datetime) -> bool:
    """
    Store a verified snapshot and its rates; re-importing the same document does nothing.

    Args:
        conn (Conn): Connection (autocommit; a transaction is opened here).
        snap (Snapshot): A verified snapshot.
        now (datetime): Import time.

    Returns:
        bool: True if imported now, False if this exact document was already present.

    Raises:
        SnapshotConflictError: If the id exists with a different content checksum.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_IMPORT_LOCK_KEY,))
        cur = await conn.execute(
            "SELECT content_sha256 FROM pricing_snapshots WHERE snapshot_id = %s",
            (snap.snapshot_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            existing = row["content_sha256"]
            if existing == snap.content_sha256:
                return False
            raise SnapshotConflictError(
                f"{snap.snapshot_id} is already imported with content {existing}, this file has "
                f"{snap.content_sha256}; snapshot ids are immutable"
            )
        document = snap.document()
        await conn.execute(
            _INSERT_SNAPSHOT,
            {
                **{k: document[k] for k in _SNAPSHOT_FIELDS},
                "captured_at": snap.captured_at,
                "publication_date": snap.publication_date,
                "source_urls": Jsonb(snap.source_urls),
                "imported_at": now,
                "document": Jsonb(document),
            },
        )
        for r in snap.rates:
            await conn.execute(
                _INSERT_RATE,
                (
                    snap.snapshot_id,
                    r.gpu_model,
                    r.instance_type,
                    r.instance_hourly_usd,
                    r.instance_gpus,
                    r.per_gpu_hour_usd,
                    r.kind,
                    str(r.confidence),
                    r.sku,
                    r.rate_code,
                    r.reason,
                ),
            )
        for u in snap.unpriced:
            await conn.execute(
                _INSERT_RATE,
                (
                    snap.snapshot_id,
                    u.gpu_model,
                    u.instance_type,
                    None,
                    None,
                    None,
                    "unpriced",
                    None,
                    None,
                    None,
                    u.reason,
                ),
            )
    return True


async def import_all(conn: Conn, snapshots: Sequence[Snapshot], now: datetime) -> list[str]:
    """
    Import several snapshots; each is idempotent.

    Args:
        conn (Conn): Connection.
        snapshots (Sequence[Snapshot]): Verified snapshots.
        now (datetime): Import time.

    Returns:
        list[str]: Ids imported now (already-present ones are skipped).
    """
    return [s.snapshot_id for s in snapshots if await import_snapshot(conn, s, now)]


async def list_imported(conn: Conn) -> list[dict[str, Any]]:
    """
    Every imported snapshot with its priced and unpriced model counts, oldest first.

    Args:
        conn (Conn): Connection.

    Returns:
        list[dict[str, Any]]: One row per snapshot.
    """
    cur = await conn.execute(_SNAPSHOT_ROWS)
    return [dict(r) for r in await cur.fetchall()]


async def select_present(conn: Conn, wanted: str | None, now: datetime) -> PresentPricing | None:
    """
    Choose the present-day snapshot (`wanted`, else the latest published) and record it.

    Args:
        conn (Conn): Connection.
        wanted (str | None): `TTV_PRICING_SNAPSHOT`, or None for the latest.
        now (datetime): Selection time.

    Returns:
        PresentPricing | None: The selection, or None when no snapshot is imported.

    Raises:
        SnapshotError: If `wanted` is not imported.
    """
    rows = await list_imported(conn)
    if wanted:
        chosen = next((r for r in rows if r["snapshot_id"] == wanted), None)
        if chosen is None:
            raise SnapshotError(f"TTV_PRICING_SNAPSHOT={wanted} is not imported")
    elif rows:
        chosen = rows[-1]
    else:
        return None
    source = "TTV_PRICING_SNAPSHOT" if wanted else "latest"
    await conn.execute(_SELECT, (chosen["snapshot_id"], now, source))
    cur = await conn.execute(
        "SELECT gpu_model FROM pricing_rates WHERE snapshot_id = %s"
        " AND per_gpu_hour_usd IS NOT NULL",
        (chosen["snapshot_id"],),
    )
    priced = frozenset(r["gpu_model"] for r in await cur.fetchall())
    return PresentPricing(chosen["snapshot_id"], chosen["publication_date"], priced, source)


def select_from_files(snapshots: Sequence[Snapshot], wanted: str | None) -> PresentPricing | None:
    """
    Choose the present-day snapshot among files, for an engine without a database.

    Args:
        snapshots (Sequence[Snapshot]): Verified snapshots.
        wanted (str | None): `TTV_PRICING_SNAPSHOT`, or None for the latest.

    Returns:
        PresentPricing | None: The selection, or None when there are no snapshots.

    Raises:
        SnapshotError: If `wanted` is not among them.
    """
    if wanted:
        snap = next((s for s in snapshots if s.snapshot_id == wanted), None)
        if snap is None:
            raise SnapshotError(f"TTV_PRICING_SNAPSHOT={wanted} is not in the pricing directory")
    else:
        snap = latest(list(snapshots))
    if snap is None:
        return None
    source = "TTV_PRICING_SNAPSHOT" if wanted else "latest"
    return PresentPricing(snap.snapshot_id, snap.publication_date, snap.priced_models(), source)


async def prepare_pricing(
    conn: Conn | None, directory: Path, wanted: str | None, now: datetime
) -> tuple[PresentPricing | None, list[str]]:
    """
    Engine startup: verify every snapshot file, import them, and select the present-day one.

    Args:
        conn (Conn | None): Ledger connection; None selects from the files only.
        directory (Path): Snapshot directory (`TTV_PRICING_DIR`).
        wanted (str | None): `TTV_PRICING_SNAPSHOT`.
        now (datetime): Startup time.

    Returns:
        tuple[PresentPricing | None, list[str]]: The selection and the ids imported now.

    Raises:
        SnapshotError: On a tampered or conflicting file, or an unknown `wanted` id.
    """
    snapshots = load_snapshot_dir(directory)
    if conn is None:
        return select_from_files(snapshots, wanted), []
    imported = await import_all(conn, snapshots, now)
    return await select_present(conn, wanted, now), imported
