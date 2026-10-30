"""End-to-end scans against a real Postgres: AGENT.md §17 numerical checks and §20.12 scenarios.

Needs `TTV_TEST_DSN` (e.g. a throwaway `postgres:17` container); skipped otherwise.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, LiteralString

import pytest

from conftest import DSN, MIGRATIONS, T0, server
from ttv.config import Policy
from ttv.engine import Engine
from ttv.metrics import EngineMetrics
from ttv.models import ServerScanServer
from ttv.queries import (
    CURRENT_GPUS_BY_CATEGORY,
    IDLE_GPU_HOURS_BY_OWNER,
    OVERDUE_GPU_HOURS_BY_OWNER,
)
from ttv.sources.serverscan import ServerScanError, ServerScanFetch
from ttv.store import PostgresStore

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")


class FakeSource:
    """A server-scan source whose next answers the test scripts."""

    def __init__(self) -> None:
        """Start with no answers."""
        self.answers: list[Sequence[ServerScanServer] | Exception] = []

    async def fetch(self, now: datetime) -> ServerScanFetch:
        """Return (or raise) the next scripted answer."""
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ServerScanFetch(
            servers=tuple(answer), pending_detail_ids=(), listing_complete=True, generated_at=now,
            fetched_at=now, details_fetched=0, details_failed=0, not_modified=False,
        )  # fmt: skip


class Clock:
    """A settable clock."""

    def __init__(self) -> None:
        """Start at T0."""
        self.now = T0

    def __call__(self) -> datetime:
        """Return the current time."""
        return self.now


def make_engine(
    store: PostgresStore, policy: Policy, source: FakeSource, clock: Clock, *, dry_run: bool = False
) -> Engine:
    """An engine with grace 2 and a 24 h tombstone."""
    return Engine(
        policy=policy,
        source=source,
        store=store,
        metrics=EngineMetrics(),
        dry_run=dry_run,
        unknown_grace_scans=2,
        tombstone_after=timedelta(hours=24),
        clock=clock,
    )


async def rows(store: PostgresStore, sql: LiteralString, **params: Any) -> list[dict[str, Any]]:
    """Run a query and return dict rows."""
    return list(await (await store.conn.execute(sql, params)).fetchall())


async def hours(
    store: PostgresStore, sql: LiteralString, start: datetime, end: datetime, col: str
) -> dict[str, float]:
    """Run an idle/overdue query for [start, end) and return {owner: hours}."""
    result = await rows(store, sql, **{"from": start, "to": end, "now": end})
    return {r["stage_owner"]: round(float(r[col]), 6) for r in result}


async def test_eight_idle_gpus_for_three_hours_is_24_with_16_overdue(
    store: PostgresStore, policy: Policy
) -> None:
    # AGENT.md §17: 8 idle GPUs x 3 h = 24 GPU-h; with a 1 h stage allowance, 16 GPU-h overdue.
    await store.sync_objectives(policy)  # PROVISIONING objective is 1 h in the test policy
    source, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source, clock)
    available = server(claim_changed_at=T0, created_at=T0)
    source.answers = [[available], [available]]
    await engine.run_once()
    clock.now = T0 + timedelta(hours=3)
    await engine.run_once()
    end = T0 + timedelta(hours=3)
    assert await hours(store, IDLE_GPU_HOURS_BY_OWNER, T0, end, "idle_gpu_hours") == {
        "Redbull": 24.0
    }
    assert await hours(store, OVERDUE_GPU_HOURS_BY_OWNER, T0, end, "overdue_gpu_hours") == {
        "Redbull": 16.0
    }


async def test_two_productive_six_idle_for_two_hours_is_12_idle(
    store: PostgresStore, policy: Policy
) -> None:
    # AGENT.md §17: 2 of 8 GPUs productive and 6 idle for 2 h → 12 idle GPU-h, not 16 and not 0.
    await store.conn.execute(
        "INSERT INTO assets (asset_id, first_observed_at, last_observed_at)"
        " VALUES ('a', %(t)s, %(t)s)",
        {"t": T0},
    )
    for category, gpus in (("PRODUCTIVE", 2), ("IDLE", 6)):
        await store.conn.execute(
            "INSERT INTO capacity_intervals (asset_id, category, reason_code, stage, stage_owner,"
            " stage_entered_at, gpus, started_at, ended_at, policy_version)"
            " VALUES ('a', %s, 'X', 'SERVING', 'Serving', %s, %s, %s, %s, 'test-1')",
            (category, T0, gpus, T0, T0 + timedelta(hours=2)),
        )
    assert await hours(
        store, IDLE_GPU_HOURS_BY_OWNER, T0, T0 + timedelta(hours=2), "idle_gpu_hours"
    ) == {"Serving": 12.0}


async def test_rescan_is_idempotent_and_buckets_reconcile_to_e(
    store: PostgresStore, policy: Policy
) -> None:
    source, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source, clock)
    mixed = server(gpu_models=("NVIDIA H100 80GB",) * 6 + ("NVIDIA L40S 48GB",) * 2)
    source.answers = [[mixed], [mixed]]
    first = await engine.run_once()
    clock.now += timedelta(minutes=5)
    second = await engine.run_once()
    assert first.plan_counts == {"opens": 2, "closes": 0, "refreshes": 0}
    assert second.plan_counts == {"opens": 0, "closes": 0, "refreshes": 0}
    assert await rows(store, CURRENT_GPUS_BY_CATEGORY) == [{"category": "IDLE", "gpus": 8}]


async def test_source_failure_turns_unknown_only_after_grace(
    store: PostgresStore, policy: Policy
) -> None:
    source, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source, clock)
    source.answers = [[server()], ServerScanError("timeout"), ServerScanError("timeout")]
    await engine.run_once()
    for _ in range(2):
        clock.now += timedelta(minutes=5)
        report = await engine.run_once()
    open_ = await rows(
        store, "SELECT category, reason_code FROM capacity_intervals WHERE ended_at IS NULL"
    )
    assert open_ == [{"category": "UNKNOWN", "reason_code": "SOURCE_STALE"}]
    assert report.status == "partial"


async def test_one_failed_scan_changes_nothing(store: PostgresStore, policy: Policy) -> None:
    source, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source, clock)
    source.answers = [[server()], ServerScanError("timeout")]
    await engine.run_once()
    clock.now += timedelta(minutes=5)
    report = await engine.run_once()
    assert report.plan_counts == {"opens": 0, "closes": 0, "refreshes": 0}


async def test_partial_absence_is_not_deletion_until_tombstone(
    store: PostgresStore, policy: Policy
) -> None:
    source, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source, clock)
    source.answers = [[server()], [], []]
    await engine.run_once()
    clock.now = T0 + timedelta(hours=2)
    await engine.run_once()
    assert len(await rows(store, "SELECT id FROM capacity_intervals WHERE ended_at IS NULL")) == 1
    clock.now = T0 + timedelta(hours=25)
    await engine.run_once()
    (closed,) = await rows(store, "SELECT ended_at FROM capacity_intervals")
    assert closed["ended_at"] == T0  # retired at its last observation, not at the scan that noticed


async def test_next_action_annotation_never_moves_totals(
    store: PostgresStore, policy: Policy
) -> None:
    await store.sync_objectives(policy)
    source, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source, clock)
    source.answers = [[server(claim_changed_at=T0)]]
    await engine.run_once()
    end = T0 + timedelta(hours=3)
    before = await hours(store, IDLE_GPU_HOURS_BY_OWNER, T0, end, "idle_gpu_hours")
    await store.conn.execute(
        "INSERT INTO next_action_annotations"
        " (asset_id, next_action_owner, reason, created_by, expires_at)"
        " VALUES ('cisco:cis0001', 'Network', 'PXE VLAN missing', 'test', %s)",
        (end,),
    )
    assert (
        await hours(store, IDLE_GPU_HOURS_BY_OWNER, T0, end, "idle_gpu_hours")
        == before
        == {"Redbull": 24.0}
    )
    (owner,) = await rows(store, "SELECT DISTINCT stage_owner FROM capacity_intervals")
    assert owner["stage_owner"] == "Redbull"


async def test_dry_run_writes_no_ledger_rows(store: PostgresStore, policy: Policy) -> None:
    source, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source, clock, dry_run=True)
    source.answers = [[server()]]
    report = await engine.run_once()
    assert report.status == "dry_run" and report.plan_counts["opens"] == 1
    assert await rows(store, "SELECT count(*) AS n FROM capacity_intervals") == [{"n": 0}]
    assert await rows(store, "SELECT status FROM scan_runs") == [{"status": "dry_run"}]


async def test_only_one_engine_holds_the_leader_lock(store: PostgresStore) -> None:
    assert DSN
    other = await PostgresStore.connect(DSN)
    try:
        assert await store.try_acquire_leadership() is True
        assert await other.try_acquire_leadership() is False
    finally:
        await other.close()


async def test_duplicate_serials_become_one_unknown_asset_and_a_conflict(
    store: PostgresStore, policy: Policy
) -> None:
    source, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source, clock)
    twin = dataclasses.replace(server(), id="srv_2")
    source.answers = [[server(), twin]]
    await engine.run_once()
    assert await rows(store, "SELECT category, reason_code FROM capacity_intervals") == [
        {"category": "UNKNOWN", "reason_code": "IDENTITY_CONFLICT"}
    ]
    (conflict,) = await rows(
        store, "SELECT kind, candidates FROM identity_conflicts WHERE resolved_at IS NULL"
    )
    assert conflict == {"kind": "DUPLICATE_SERIAL", "candidates": ["srv_1", "srv_2"]}


async def test_migrations_are_idempotent(store: PostgresStore) -> None:
    assert await store.migrate(MIGRATIONS) == []
