"""
AWS-equivalent value in USD against a real Postgres (AGENT.md §10, §17; ADR-0003).

Needs `TTV_TEST_DSN`; skipped otherwise. Every rate is unmistakably synthetic.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString

import pytest

from conftest import DSN, T0, server, synthetic_snapshot
from ttv.config import Policy
from ttv.engine import Engine
from ttv.metrics import EngineMetrics
from ttv.models import ServerScanServer
from ttv.pricing.catalog import CATALOG_VERSION
from ttv.pricing.importer import (
    SnapshotConflictError,
    import_snapshot,
    prepare_pricing,
    select_present,
)
from ttv.pricing.snapshot import SnapshotError, write_snapshot
from ttv.queries import IDLE_VALUE_BY_OWNER, IDLE_VALUE_PER_HOUR_NOW
from ttv.sources.serverscan import ServerScanFetch
from ttv.store import PostgresStore

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")

H100 = "NVIDIA H100 80GB"
A6000 = "NVIDIA RTX A6000"  # not in the catalogue: unidentified, so unpriced
PUBLISHED = T0 - timedelta(days=2)


async def rows(store: PostgresStore, sql: LiteralString, **params: Any) -> list[dict[str, Any]]:
    """Run a query and return dict rows."""
    return list(await (await store.conn.execute(sql, params)).fetchall())


async def idle_interval(
    store: PostgresStore,
    *,
    gpus: int,
    gpu_model: str | None,
    start: datetime,
    hours: float,
    owner: str = "Redbull",
    asset: str = "a",
) -> None:
    """Insert one closed IDLE interval in the PROVISIONING stage (objective 1 h in the policy)."""
    await store.conn.execute(
        "INSERT INTO assets (asset_id, first_observed_at, last_observed_at) VALUES (%s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (asset, start, start),
    )
    await store.conn.execute(
        "INSERT INTO capacity_intervals (asset_id, category, reason_code, stage, stage_owner,"
        " stage_entered_at, gpus, gpu_model, started_at, ended_at, policy_version)"
        " VALUES (%s, 'IDLE', 'NOT_IN_CLUSTER', 'PROVISIONING', %s, %s, %s, %s, %s, %s,"
        " 'test-1')",
        (asset, owner, start, gpus, gpu_model, start, start + timedelta(hours=hours)),
    )


async def alias(store: PostgresStore, raw: str, canonical: str | None) -> None:
    """Record one raw → canonical alias."""
    await store.conn.execute(
        "INSERT INTO gpu_model_aliases VALUES (%s, %s, 'test', %s) ON CONFLICT DO NOTHING",
        (raw, canonical, T0),
    )


async def value(
    store: PostgresStore, snapshot: str, start: datetime, end: datetime
) -> dict[str, dict[str, Any]]:
    """IDLE_VALUE_BY_OWNER for [start, end) as {owner: row}."""
    result = await rows(
        store, IDLE_VALUE_BY_OWNER, snapshot=snapshot, **{"from": start, "to": end, "now": end}
    )
    return {r["stage_owner"]: r for r in result}


async def test_import_is_idempotent_and_ids_are_immutable(store: PostgresStore) -> None:
    snap = synthetic_snapshot(PUBLISHED)
    assert await import_snapshot(store.conn, snap, T0) is True
    assert await import_snapshot(store.conn, snap, T0) is False
    (row,) = await rows(store, "SELECT * FROM pricing_snapshots")
    assert row["content_sha256"] == row["checksum"] == snap.content_sha256
    assert row["currency"] == "USD"
    assert row["publication_date"] == row["effective_at"] == PUBLISHED
    assert row["fx_usd_ils"] is None
    rates = await rows(
        store, "SELECT gpu_model, per_gpu_hour_usd, mapping, reason FROM pricing_rates ORDER BY 1"
    )
    assert rates == [
        {"gpu_model": "NVIDIA A16 16GB", "per_gpu_hour_usd": None, "mapping": "unpriced",
         "reason": "synthetic: no equivalent"},
        {"gpu_model": H100, "per_gpu_hour_usd": Decimal(10), "mapping": "exact",
         "reason": "synthetic"},
    ]  # fmt: skip
    tampered = synthetic_snapshot(PUBLISHED, {H100: ("96.00", 8)})
    assert tampered.snapshot_id == snap.snapshot_id
    with pytest.raises(SnapshotConflictError, match="immutable"):
        await import_snapshot(store.conn, tampered, T0)


async def test_eight_gpu_hours_are_one_instance_hour_not_eight(store: PostgresStore) -> None:
    # AGENT.md §17: $80.00/h instance with 8 GPUs; 8 GPUs idle for 1 h = 8 GPU-h = $80.00.
    snap = synthetic_snapshot(PUBLISHED)
    await import_snapshot(store.conn, snap, T0)
    await alias(store, H100, H100)
    await idle_interval(store, gpus=8, gpu_model=H100, start=T0, hours=1)
    (row,) = (await value(store, snap.snapshot_id, T0, T0 + timedelta(hours=1))).values()
    assert row["idle_gpu_hours"] == Decimal(8)
    assert row["idle_usd"] == Decimal(80)
    assert row["idle_usd"] != Decimal(640)
    assert row["idle_unpriced_gpu_hours"] == 0


async def test_overdue_value_and_unpriced_hours_stay_visible(
    store: PostgresStore, policy: Policy
) -> None:
    # 8 priced GPUs idle 3 h (1 h objective): 24 idle / 16 overdue GPU-h = $240 / $160.
    # 2 unidentified GPUs idle 3 h: 6 unpriced idle GPU-h, never $0.
    await store.sync_objectives(policy)
    snap = synthetic_snapshot(PUBLISHED)
    await import_snapshot(store.conn, snap, T0)
    await alias(store, H100, H100)
    await alias(store, A6000, None)
    await idle_interval(store, gpus=8, gpu_model=H100, start=T0, hours=3)
    await idle_interval(store, gpus=2, gpu_model=A6000, start=T0, hours=3, asset="b")
    row = (await value(store, snap.snapshot_id, T0, T0 + timedelta(hours=3)))["Redbull"]
    assert row["idle_gpu_hours"] == Decimal(30)
    assert row["idle_usd"] == Decimal(240)
    assert row["idle_unpriced_gpu_hours"] == Decimal(6)
    assert row["overdue_gpu_hours"] == Decimal(20)
    assert row["overdue_usd"] == Decimal(160)
    assert row["overdue_unpriced_gpu_hours"] == Decimal(4)


async def test_only_unpriced_gpus_give_no_value_not_zero(store: PostgresStore) -> None:
    snap = synthetic_snapshot(PUBLISHED)
    await import_snapshot(store.conn, snap, T0)
    await idle_interval(store, gpus=4, gpu_model="NVIDIA A16 16GB", start=T0, hours=2)
    await alias(store, "NVIDIA A16 16GB", "NVIDIA A16 16GB")
    (row,) = (await value(store, snap.snapshot_id, T0, T0 + timedelta(hours=2))).values()
    assert row["idle_usd"] is None
    assert row["idle_unpriced_gpu_hours"] == Decimal(8)


async def test_historical_value_survives_a_newer_snapshot(store: PostgresStore) -> None:
    old = synthetic_snapshot(PUBLISHED)
    await import_snapshot(store.conn, old, T0)
    await alias(store, H100, H100)
    await idle_interval(store, gpus=8, gpu_model=H100, start=T0, hours=1)
    # An interval that started before any snapshot was published is unpriced historically.
    await idle_interval(
        store, gpus=1, gpu_model=H100, start=PUBLISHED - timedelta(hours=2), hours=1, asset="b"
    )
    window = (PUBLISHED - timedelta(days=1), T0 + timedelta(hours=1))
    before = (await value(store, old.snapshot_id, *window))["Redbull"]
    assert before["idle_usd_historical"] == Decimal(80)
    assert before["idle_unpriced_gpu_hours_historical"] == Decimal(1)

    new = synthetic_snapshot(T0 + timedelta(days=1), {H100: ("160.00", 8)})
    await import_snapshot(store.conn, new, T0 + timedelta(days=1))
    after = (await value(store, new.snapshot_id, *window))["Redbull"]
    assert after["idle_usd_historical"] == before["idle_usd_historical"] == Decimal(80)
    assert after["idle_usd"] == Decimal(180)  # present-day: 9 GPU-h x $20
    assert after["idle_unpriced_gpu_hours"] == 0


async def test_value_per_hour_now(store: PostgresStore) -> None:
    snap = synthetic_snapshot(PUBLISHED)
    await import_snapshot(store.conn, snap, T0)
    await alias(store, H100, H100)
    await alias(store, A6000, None)
    await store.conn.execute(
        "INSERT INTO assets (asset_id, first_observed_at, last_observed_at) VALUES ('a', %s, %s)",
        (T0, T0),
    )
    for gpus, model in ((6, H100), (2, A6000)):
        await store.conn.execute(
            "INSERT INTO capacity_intervals (asset_id, category, reason_code, stage_owner, gpus,"
            " gpu_model, started_at, policy_version)"
            " VALUES ('a', 'IDLE', 'NO_WORKLOAD', 'Serving', %s, %s, %s, 'test-1')",
            (gpus, model, T0),
        )
    (row,) = await rows(store, IDLE_VALUE_PER_HOUR_NOW, snapshot=snap.snapshot_id)
    assert row == {"usd_per_hour": Decimal(60), "unpriced_gpus": 2, "idle_gpus": 8}


async def test_startup_imports_the_directory_and_selects(
    store: PostgresStore, tmp_path: Path
) -> None:
    old, new = synthetic_snapshot(PUBLISHED), synthetic_snapshot(T0)
    write_snapshot(old, tmp_path)
    write_snapshot(new, tmp_path)
    present, imported = await prepare_pricing(store.conn, tmp_path, None, T0)
    assert sorted(imported) == sorted([old.snapshot_id, new.snapshot_id])
    assert present is not None
    assert (present.snapshot_id, present.source) == (new.snapshot_id, "latest")
    assert present.priced == frozenset({H100})
    # Rollback: pin the older snapshot; importing again changes nothing.
    present, imported = await prepare_pricing(store.conn, tmp_path, old.snapshot_id, T0)
    assert imported == []
    assert present is not None
    assert present.snapshot_id == old.snapshot_id
    (sel,) = await rows(store, "SELECT snapshot_id, source FROM pricing_selection")
    assert sel == {"snapshot_id": old.snapshot_id, "source": "TTV_PRICING_SNAPSHOT"}
    with pytest.raises(SnapshotError, match="not imported"):
        await select_present(store.conn, "aws-ec2-ondemand-nowhere", T0)


class FakeSource:
    """A server-scan source returning the same servers every scan."""

    def __init__(self, servers: Sequence[ServerScanServer]) -> None:
        """Remember the servers."""
        self.servers = tuple(servers)

    async def fetch(self, now: datetime) -> ServerScanFetch:
        """Return the servers as a complete listing."""
        return ServerScanFetch(
            servers=self.servers, pending_detail_ids=(), listing_complete=True, generated_at=now,
            fetched_at=now, details_fetched=0, details_failed=0, not_modified=False,
        )  # fmt: skip


async def test_scan_records_aliases_and_publishes_unpriced_gpus(
    store: PostgresStore, policy: Policy, tmp_path: Path
) -> None:
    write_snapshot(synthetic_snapshot(PUBLISHED), tmp_path)
    present, _ = await prepare_pricing(store.conn, tmp_path, None, T0)
    servers = [
        server(),  # 8 x NVIDIA H100 80GB: priced
        server(id="srv_2", serial_normalized="cis0002", gpu_models=("UCSC-GPU-A100-40",) * 4),
        server(id="srv_3", serial_normalized="cis0003", gpu_models=(A6000,) * 2),
    ]
    metrics = EngineMetrics()
    engine = Engine(
        policy=policy,
        source=FakeSource(servers),
        store=store,
        metrics=metrics,
        dry_run=False,
        unknown_grace_scans=2,
        tombstone_after=timedelta(hours=24),
        pricing=present,
        clock=lambda: T0,
    )
    await engine.run_once()
    aliases = await rows(
        store, "SELECT raw_model, canonical, catalog_version FROM gpu_model_aliases ORDER BY 1"
    )
    assert aliases == [
        {"raw_model": H100, "canonical": H100, "catalog_version": CATALOG_VERSION},
        {"raw_model": A6000, "canonical": None, "catalog_version": CATALOG_VERSION},
        {"raw_model": "UCSC-GPU-A100-40", "canonical": "NVIDIA A100 40GB",
         "catalog_version": CATALOG_VERSION},
    ]  # fmt: skip
    unpriced = {
        s.labels["gpu_model"]: s.value
        for m in metrics.registry.collect()
        if m.name == "ttv_unpriced_gpus"
        for s in m.samples
    }
    assert unpriced == {"UCSC-GPU-A100-40": 4, A6000: 2}
    assert await store.refresh_gpu_aliases(lambda raw: None, T0) == 3
