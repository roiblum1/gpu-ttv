"""End-to-end scans with profile-less collector reads in Postgres (ADR-0002 rows 3-5).

Needs `TTV_TEST_DSN`; skipped otherwise. Collector reads are synthetic (fixture-tested).
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from conftest import DSN, T0, server
from test_engine_pg import Clock, FakeSource, hours, rows
from ttv.config import Policy
from ttv.engine import Engine
from ttv.metrics import EngineMetrics
from ttv.models import CollectorRead, ManagerServer
from ttv.queries import IDLE_GPU_HOURS_BY_OWNER
from ttv.store import PostgresStore

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")

MANAGER = "openmanage:ome-1"


def dell(serial: str, **overrides: Any) -> ManagerServer:
    """A Dell server with 8 H100s and no profile."""
    base = ManagerServer(
        vendor="dell",
        serial_normalized=serial,
        manager=MANAGER,
        source_id=f"dev-{serial}",
        name=f"r760xa-{serial}",
        model="PowerEdge XE9680",
        profile_name=None,
        gpu_models=("NVIDIA H100 80GB",) * 8,
    )
    return dataclasses.replace(base, **overrides)


def read(at: Any, *servers: ManagerServer, complete: bool = True) -> CollectorRead:
    """One OME read finishing at `at`."""
    return CollectorRead(MANAGER, "openmanage", at - timedelta(minutes=2), at, complete, servers)


def engine(store: PostgresStore, policy: Policy, source: FakeSource, clock: Clock) -> Engine:
    """An engine with a 1 h collector staleness limit."""
    return Engine(
        policy=policy,
        source=source,
        store=store,
        metrics=EngineMetrics(),
        dry_run=False,
        unknown_grace_scans=2,
        tombstone_after=timedelta(hours=24),
        collector_stale_after=timedelta(hours=1),
        clock=clock,
    )


async def open_rows(store: PostgresStore) -> set[tuple[str, str, str, int]]:
    """(asset, category, reason, gpus) of open intervals."""
    result = await rows(
        store,
        "SELECT asset_id, category, reason_code, gpus FROM capacity_intervals "
        "WHERE ended_at IS NULL",
    )
    return {(r["asset_id"], r["category"], r["reason_code"], r["gpus"]) for r in result}


async def test_profile_less_gpu_server_is_charged_to_dc(
    store: PostgresStore, policy: Policy
) -> None:
    await store.sync_objectives(policy)
    await store.record_collector_read(
        read(
            T0,
            dell("svc0001"),
            dell("svc0002", profile_name="vdi-pool"),
            dell("svc0003", profile_name="ocp4-prod-gpu-03"),
            dell("svc0004", gpu_models=()),
            dell("svc0005", gpu_models=None),
        )
    )
    source, clock = FakeSource(), Clock()
    source.answers = [[server(lifecycle_state="AVAILABLE")]] * 2
    eng = engine(store, policy, source, clock)
    report = await eng.run_once()
    assert await open_rows(store) == {
        ("cisco:cis0001", "IDLE", "NOT_IN_CLUSTER", 8),
        ("dell:svc0001", "IDLE", "PROFILE_NONE", 8),
        ("dell:svc0002", "NOT_CHARGED", "OTHER_INTENDED_USE", 8),
        ("dell:svc0003", "UNKNOWN", "HANDOVER_PENDING", 8),
    }
    assert report.coverage["collector_gpu_assets"] == 3
    assert report.coverage["collector_gpu_inventory_unread"] == 1
    clock.now = T0 + timedelta(minutes=30)
    await eng.run_once()
    idle = await hours(store, IDLE_GPU_HOURS_BY_OWNER, T0, clock.now, "idle_gpu_hours")
    assert idle == {"DC": 4.0, "Redbull": 4.0}  # 8 GPUs x 0.5 h each


async def test_server_scan_wins_for_the_same_vendor_and_serial(
    store: PostgresStore, policy: Policy
) -> None:
    await store.record_collector_read(
        CollectorRead(
            "ucs-central:central-1",
            "ucs_central",
            T0 - timedelta(minutes=2),
            T0,
            True,
            (
                dataclasses.replace(
                    dell("cis0001"), vendor="cisco", manager="ucs-central:central-1"
                ),
            ),
        )
    )
    source, clock = FakeSource(), Clock()
    source.answers = [[server(lifecycle_state="AVAILABLE")]]
    await engine(store, policy, source, clock).run_once()
    assert await open_rows(store) == {("cisco:cis0001", "IDLE", "NOT_IN_CLUSTER", 8)}


async def test_stale_manager_turns_its_servers_unknown(
    store: PostgresStore, policy: Policy
) -> None:
    await store.record_collector_read(read(T0 - timedelta(hours=2), dell("svc0001")))
    source, clock = FakeSource(), Clock()
    source.answers = [[]]
    await engine(store, policy, source, clock).run_once()
    assert await open_rows(store) == {("dell:svc0001", "UNKNOWN", "SOURCE_STALE", 8)}


async def test_partial_collector_read_never_retires_a_server(
    store: PostgresStore, policy: Policy
) -> None:
    await store.record_collector_read(read(T0, dell("svc0001")))
    source, clock = FakeSource(), Clock()
    source.answers = [[]] * 3
    eng = engine(store, policy, source, clock)
    await eng.run_once()
    # Two days later svc0001 is missing from a complete read, but the latest read is partial.
    later = T0 + timedelta(days=2)
    await store.record_collector_read(read(later - timedelta(minutes=50), dell("svc0002")))
    await store.record_collector_read(read(later, complete=False))
    clock.now = later
    await eng.run_once()
    assert ("dell:svc0001", "IDLE", "PROFILE_NONE", 8) in await open_rows(store)
    # Once the latest read is complete again, the absence beyond the tombstone retires it.
    await store.record_collector_read(read(later + timedelta(minutes=5), dell("svc0002")))
    clock.now = later + timedelta(minutes=5)
    await eng.run_once()
    assert {r[0] for r in await open_rows(store)} == {"dell:svc0002"}


async def test_duplicate_serial_across_managers_is_a_conflict(
    store: PostgresStore, policy: Policy
) -> None:
    other = dataclasses.replace(dell("svc0001"), manager="openmanage:ome-2", source_id="d-9")
    await store.record_collector_read(read(T0, dell("svc0001")))
    await store.record_collector_read(
        CollectorRead(
            "openmanage:ome-2", "openmanage", T0 - timedelta(minutes=1), T0, True, (other,)
        )
    )
    source, clock = FakeSource(), Clock()
    source.answers = [[]]
    await engine(store, policy, source, clock).run_once()
    assert await open_rows(store) == {("dell:svc0001", "UNKNOWN", "IDENTITY_CONFLICT", 8)}
    kinds = await rows(store, "SELECT kind FROM identity_conflicts WHERE resolved_at IS NULL")
    assert kinds == [{"kind": "COLLECTOR_DUPLICATE"}]


async def test_unresolved_profile_is_an_identity_conflict(
    store: PostgresStore, policy: Policy
) -> None:
    ambiguous = dell("svc0001", profile_unresolved="TargetName shared by several devices")
    await store.record_collector_read(read(T0, ambiguous))
    source, clock = FakeSource(), Clock()
    source.answers = [[]]
    await engine(store, policy, source, clock).run_once()
    assert await open_rows(store) == {("dell:svc0001", "UNKNOWN", "IDENTITY_CONFLICT", 8)}
    kinds = await rows(store, "SELECT kind FROM identity_conflicts WHERE resolved_at IS NULL")
    assert kinds == [{"kind": "COLLECTOR_PROFILE_AMBIGUOUS"}]
