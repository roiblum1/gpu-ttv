"""Postgres persistence for the ledger, engine state and the server-scan detail cache (ADR-0001)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ttv.config import Policy
from ttv.identity import IdentityConflict
from ttv.ledger import BucketKey, LastStage, LedgerPlan, OpenInterval
from ttv.models import SourceStatus
from ttv.sources.serverscan import CachedDetail

# One active engine: a session-level advisory lock on this key (AGENT.md §20.1).
LEADER_LOCK_KEY = 7_211_042_301
_DICT_ROWS: Any = dict_row  # Any: ty mis-resolves psycopg's row-factory overloads

_CREATE_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())
"""
_UPSERT_OBJECTIVE = """
INSERT INTO stage_objectives (policy_version, stage, objective) VALUES (%s, %s, %s)
ON CONFLICT (policy_version, stage) DO UPDATE SET objective = EXCLUDED.objective
"""
_LAST_STAGE = """
SELECT DISTINCT ON (asset_id) asset_id, stage, stage_entered_at, stage_entered_lower_bound
FROM capacity_intervals
WHERE stage IS NOT NULL AND stage_entered_at IS NOT NULL
ORDER BY asset_id, started_at DESC, id DESC
"""
_RECORD_SOURCE = """
INSERT INTO source_state (name, failed_scans, last_success_at, last_attempt_at, last_error)
VALUES (%(n)s, CASE WHEN %(ok)s THEN 0 ELSE 1 END,
        CASE WHEN %(ok)s THEN %(now)s END, %(now)s, %(err)s)
ON CONFLICT (name) DO UPDATE SET
    failed_scans = CASE WHEN %(ok)s THEN 0 ELSE source_state.failed_scans + 1 END,
    last_success_at = CASE WHEN %(ok)s THEN %(now)s ELSE source_state.last_success_at END,
    last_attempt_at = %(now)s,
    last_error = %(err)s
RETURNING failed_scans, last_success_at
"""
_FINISH_SCAN = """
UPDATE scan_runs SET finished_at = %s, status = %s, coverage = %s, errors = %s WHERE id = %s
"""
_UPSERT_ASSET = """
INSERT INTO assets (asset_id, aliases, vendor, serial, system_uuid, name, model, site, gpu_model,
                    gpu_count, source, first_observed_at, last_observed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (asset_id) DO UPDATE SET
    aliases = EXCLUDED.aliases, vendor = EXCLUDED.vendor, serial = EXCLUDED.serial,
    system_uuid = EXCLUDED.system_uuid, name = EXCLUDED.name, model = EXCLUDED.model,
    site = EXCLUDED.site, gpu_model = EXCLUDED.gpu_model, gpu_count = EXCLUDED.gpu_count,
    source = EXCLUDED.source, last_observed_at = EXCLUDED.last_observed_at
"""
_CLOSE_INTERVAL = """
UPDATE capacity_intervals SET ended_at = %s, closed_by_scan = %s WHERE id = %s AND ended_at IS NULL
"""
_REFRESH_INTERVAL = """
UPDATE capacity_intervals SET flags = %s, evidence = %s WHERE id = %s AND ended_at IS NULL
"""
_OPEN_INTERVAL = """
INSERT INTO capacity_intervals (asset_id, category, reason_code, stage, stage_owner,
    stage_entered_at, stage_entered_lower_bound, holder_ref, pool, team, gpu_model, gpus, flags,
    started_at, opened_by_scan, evidence, policy_version, cluster, node_uid)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_MILESTONE = """
INSERT INTO milestones (asset_id, milestone, observed_at, lower_bound, evidence)
VALUES (%s, %s, %s, %s, %s) ON CONFLICT (asset_id, milestone) DO NOTHING
"""
_UPSERT_CONFLICT = """
INSERT INTO identity_conflicts (kind, key, candidates, first_seen, last_seen)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (kind, key) WHERE resolved_at IS NULL
DO UPDATE SET candidates = EXCLUDED.candidates, last_seen = EXCLUDED.last_seen
"""
_RESOLVE_MISSING_CONFLICTS = """
UPDATE identity_conflicts SET resolved_at = %s, resolution = %s
WHERE resolved_at IS NULL AND NOT (kind || chr(31) || key = ANY(%s::text[]))
"""
_INTERVALS_FOR = """
SELECT * FROM capacity_intervals WHERE asset_id = %s ORDER BY started_at DESC, id DESC LIMIT %s
"""
_UPSERT_DETAIL = """
INSERT INTO serverscan_cache (id, row_hash, fetched_at, payload) VALUES (%s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET
    row_hash = EXCLUDED.row_hash, fetched_at = EXCLUDED.fetched_at, payload = EXCLUDED.payload
"""


@dataclass(frozen=True)
class AssetRow:
    """
    One asset's descriptive fields, upserted each scan it is observed.

    Attributes:
        asset_id (str): Asset id.
        vendor (str): Vendor.
        serial (str): Normalised serial.
        system_uuid (str | None): System UUID.
        name (str): server-scan name.
        model (str | None): Server model.
        site (str | None): Site.
        gpu_model (str | None): Most common GPU model.
        gpu_count (int | None): GPU count; None when unread.
        source (str): Source provider.
        aliases (dict[str, Any]): Source ids.
    """

    asset_id: str
    vendor: str
    serial: str
    system_uuid: str | None
    name: str
    model: str | None
    site: str | None
    gpu_model: str | None
    gpu_count: int | None
    source: str
    aliases: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Milestone:
    """
    A first-time fact about an asset; never overwritten once recorded.

    Attributes:
        asset_id (str): Asset id.
        milestone (str): Name, e.g. `first_seen`.
        observed_at (datetime): When it happened, or a lower bound.
        lower_bound (bool): Whether it may have happened earlier.
        evidence (dict[str, Any]): Source of the fact.
    """

    asset_id: str
    milestone: str
    observed_at: datetime
    lower_bound: bool
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerState:
    """
    What the planner needs from the ledger before a scan.

    Attributes:
        open_intervals (list[OpenInterval]): Open intervals.
        last_stage (dict[str, LastStage]): Each asset's most recent staged interval.
        last_observed (dict[str, datetime]): Known assets and when each was last observed.
    """

    open_intervals: list[OpenInterval]
    last_stage: dict[str, LastStage]
    last_observed: dict[str, datetime]


class PostgresStore:
    """The ledger in Postgres; one connection, which also holds the leader lock."""

    def __init__(self, conn: psycopg.AsyncConnection[dict[str, Any]]) -> None:
        """
        Wrap an open connection.

        Args:
            conn (psycopg.AsyncConnection[dict[str, Any]]): Connection with dict rows.
        """
        self.conn = conn

    @classmethod
    async def connect(cls, conninfo: str) -> PostgresStore:
        """
        Open a connection.

        Args:
            conninfo (str): libpq connection string or URL.

        Returns:
            PostgresStore: The store.
        """
        conn = await psycopg.AsyncConnection.connect(
            conninfo, row_factory=_DICT_ROWS, autocommit=True
        )
        return cls(cast("psycopg.AsyncConnection[dict[str, Any]]", conn))

    async def close(self) -> None:
        """Close the connection, releasing the leader lock."""
        await self.conn.close()

    async def _all(self, sql: LiteralString, params: Any = None) -> list[dict[str, Any]]:
        """
        Run a query and return every row.

        Args:
            sql (LiteralString): Query.
            params (Any): Parameters.

        Returns:
            list[dict[str, Any]]: Rows.
        """
        return list(await (await self.conn.execute(sql, params)).fetchall())

    async def _one(self, sql: LiteralString, params: Any = None) -> dict[str, Any]:
        """
        Run a query that returns exactly one row.

        Args:
            sql (LiteralString): Query.
            params (Any): Parameters.

        Returns:
            dict[str, Any]: The row.

        Raises:
            RuntimeError: If no row came back.
        """
        row = await (await self.conn.execute(sql, params)).fetchone()
        if row is None:
            raise RuntimeError("query returned no row")
        return row

    async def migrate(self, directory: Path) -> list[str]:
        """
        Apply `*.sql` files in name order that are not yet recorded.

        Args:
            directory (Path): Migrations directory.

        Returns:
            list[str]: Versions applied now.
        """
        applied: list[str] = []
        async with self.conn.transaction():
            await self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (LEADER_LOCK_KEY + 1,))
            await self.conn.execute(_CREATE_MIGRATIONS)
            done = {r["version"] for r in await self._all("SELECT version FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.stem in done:
                    continue
                await self.conn.execute(cast(LiteralString, path.read_text()))  # repo-owned SQL
                await self.conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,)
                )
                applied.append(path.stem)
        return applied

    async def try_acquire_leadership(self) -> bool:
        """
        Take the session-level leader lock if no other engine holds it.

        Returns:
            bool: True if this connection now holds the lock.
        """
        row = await self._one("SELECT pg_try_advisory_lock(%s) AS ok", (LEADER_LOCK_KEY,))
        return bool(row["ok"])

    async def sync_objectives(self, policy: Policy) -> None:
        """
        Record the policy's stage objectives under its version (idempotent).

        Args:
            policy (Policy): The active policy.
        """
        async with self.conn.transaction():
            for stage, objective in policy.stage_objectives.items():
                await self.conn.execute(
                    _UPSERT_OBJECTIVE, (policy.policy_version, str(stage), objective)
                )

    async def load_state(self) -> LedgerState:
        """
        Read open intervals, each asset's last staged interval, and known assets.

        Returns:
            LedgerState: The state.
        """
        open_rows = await self._all(
            "SELECT * FROM capacity_intervals WHERE ended_at IS NULL ORDER BY id"
        )
        last_stage = {
            r["asset_id"]: LastStage(
                r["stage"], r["stage_entered_at"], r["stage_entered_lower_bound"]
            )
            for r in await self._all(_LAST_STAGE)
        }
        assets = await self._all("SELECT asset_id, last_observed_at FROM assets")
        return LedgerState(
            [_open_interval(r) for r in open_rows],
            last_stage,
            {r["asset_id"]: r["last_observed_at"] for r in assets},
        )

    async def source_status(self, name: str) -> SourceStatus:
        """
        Read one source's health.

        Args:
            name (str): Source name.

        Returns:
            SourceStatus: Its health; a never-seen source has zero failures.
        """
        rows = await self._all("SELECT * FROM source_state WHERE name = %s", (name,))
        if not rows:
            return SourceStatus(name, 0, None)
        return SourceStatus(name, rows[0]["failed_scans"], rows[0]["last_success_at"])

    async def record_source(
        self, name: str, *, ok: bool, now: datetime, error: str | None = None
    ) -> SourceStatus:
        """
        Record one fetch attempt and return the updated health.

        Args:
            name (str): Source name.
            ok (bool): Whether the fetch was complete.
            now (datetime): Attempt time.
            error (str | None): Error summary for a failed fetch.

        Returns:
            SourceStatus: Health after this attempt.
        """
        row = await self._one(_RECORD_SOURCE, {"n": name, "ok": ok, "now": now, "err": error})
        return SourceStatus(name, row["failed_scans"], row["last_success_at"])

    async def begin_scan(self, kind: str, now: datetime, *, dry_run: bool) -> int:
        """
        Record the start of a scan.

        Args:
            kind (str): Scan kind, e.g. `engine`.
            now (datetime): Start time.
            dry_run (bool): Whether this scan writes nothing to the ledger.

        Returns:
            int: Scan id.
        """
        row = await self._one(
            "INSERT INTO scan_runs (kind, started_at, status) VALUES (%s, %s, %s) RETURNING id",
            (kind, now, "dry_run" if dry_run else "running"),
        )
        return int(row["id"])

    async def finish_scan(
        self,
        scan_id: int,
        *,
        status: str,
        now: datetime,
        coverage: dict[str, Any],
        errors: list[str],
    ) -> None:
        """
        Record the end of a scan.

        Args:
            scan_id (int): Scan id.
            status (str): `ok`, `partial`, `failed` or `dry_run`.
            now (datetime): End time.
            coverage (dict[str, Any]): Coverage counts.
            errors (list[str]): Error summaries.
        """
        await self.conn.execute(
            _FINISH_SCAN, (now, status, Jsonb(coverage), Jsonb(errors), scan_id)
        )

    async def apply(
        self,
        scan_id: int,
        ledger_plan: LedgerPlan,
        *,
        assets: Sequence[AssetRow],
        milestones: Sequence[Milestone],
        conflicts: Sequence[IdentityConflict],
        resolve_missing_conflicts: bool,
        now: datetime,
    ) -> None:
        """
        Write one scan's changes atomically, so a crash leaves the ledger as it was.

        Args:
            scan_id (int): Scan id.
            ledger_plan (LedgerPlan): Interval changes.
            assets (Sequence[AssetRow]): Assets observed this scan.
            milestones (Sequence[Milestone]): First-time facts; existing ones are kept.
            conflicts (Sequence[IdentityConflict]): Conflicts seen this scan.
            resolve_missing_conflicts (bool): Resolve open conflicts not seen (complete listings).
            now (datetime): Scan time.
        """
        async with self.conn.transaction():
            for a in assets:
                await self.conn.execute(_UPSERT_ASSET, _asset_params(a, now))
            for c in ledger_plan.closes:
                await self.conn.execute(_CLOSE_INTERVAL, (c.ended_at, scan_id, c.interval_id))
            for r in ledger_plan.refreshes:
                await self.conn.execute(
                    _REFRESH_INTERVAL, (list(r.flags), Jsonb(dict(r.evidence)), r.interval_id)
                )
            for o in ledger_plan.opens:
                b = o.bucket
                await self.conn.execute(
                    _OPEN_INTERVAL,
                    (
                        o.asset_id,
                        str(b.category),
                        str(b.reason),
                        str(b.stage) if b.stage else None,
                        b.stage_owner,
                        o.stage_entered_at,
                        o.stage_entered_lower_bound,
                        b.holder_ref,
                        b.pool,
                        b.team,
                        b.gpu_model,
                        b.gpus,
                        list(b.flags),
                        o.started_at,
                        scan_id,
                        Jsonb(dict(b.evidence)),
                        o.policy_version,
                        b.cluster,
                        b.node_uid,
                    ),
                )
            for m in milestones:
                await self.conn.execute(
                    _INSERT_MILESTONE,
                    (m.asset_id, m.milestone, m.observed_at, m.lower_bound, Jsonb(m.evidence)),
                )
            for c in conflicts:
                await self.conn.execute(
                    _UPSERT_CONFLICT, (c.kind, c.key, Jsonb(list(c.candidates)), now, now)
                )
            if resolve_missing_conflicts:
                seen = [f"{c.kind}\x1f{c.key}" for c in conflicts]
                await self.conn.execute(
                    _RESOLVE_MISSING_CONFLICTS, (now, Jsonb({"auto": "no longer observed"}), seen)
                )

    async def intervals_for(self, asset_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """
        Return an asset's most recent intervals, for `/explain`.

        Args:
            asset_id (str): Asset id.
            limit (int): Maximum rows.

        Returns:
            list[dict[str, Any]]: Interval rows, newest first.
        """
        return await self._all(_INTERVALS_FOR, (asset_id, limit))

    async def get_many(self, ids: Sequence[str]) -> dict[str, CachedDetail]:
        """
        Read cached server-scan details.

        Args:
            ids (Sequence[str]): server-scan ids.

        Returns:
            dict[str, CachedDetail]: Cached entries by id.
        """
        if not ids:
            return {}
        rows = await self._all("SELECT * FROM serverscan_cache WHERE id = ANY(%s)", (list(ids),))
        return {
            r["id"]: CachedDetail(r["id"], r["row_hash"], r["fetched_at"], r["payload"])
            for r in rows
        }

    async def put_many(self, entries: Sequence[CachedDetail]) -> None:
        """
        Store fetched server-scan details.

        Args:
            entries (Sequence[CachedDetail]): Entries to upsert.
        """
        async with self.conn.transaction():
            for e in entries:
                await self.conn.execute(
                    _UPSERT_DETAIL, (e.id, e.row_hash, e.fetched_at, Jsonb(e.payload))
                )


def _asset_params(a: AssetRow, now: datetime) -> tuple[Any, ...]:
    """
    Parameters for the asset upsert.

    Args:
        a (AssetRow): The asset.
        now (datetime): Scan time (first and last observation for a new asset).

    Returns:
        tuple[Any, ...]: Parameters in `_UPSERT_ASSET` order.
    """
    return (
        a.asset_id,
        Jsonb(a.aliases),
        a.vendor,
        a.serial,
        a.system_uuid,
        a.name,
        a.model,
        a.site,
        a.gpu_model,
        a.gpu_count,
        a.source,
        now,
        now,
    )


def _open_interval(r: dict[str, Any]) -> OpenInterval:
    """
    Convert an open `capacity_intervals` row into an OpenInterval.

    Args:
        r (dict[str, Any]): The row.

    Returns:
        OpenInterval: The interval.
    """
    key = BucketKey(
        category=r["category"],
        reason_code=r["reason_code"],
        stage_owner=r["stage_owner"] or "",
        holder_ref=r["holder_ref"] or "",
        pool=r["pool"] or "",
        team=r["team"] or "",
        gpu_model=r["gpu_model"] or "",
        gpus=r["gpus"],
    )
    return OpenInterval(
        id=r["id"],
        asset_id=r["asset_id"],
        key=key,
        stage=r["stage"],
        stage_entered_at=r["stage_entered_at"],
        stage_entered_lower_bound=r["stage_entered_lower_bound"],
        started_at=r["started_at"],
        flags=tuple(r["flags"] or ()),
        evidence=r["evidence"] or {},
    )
