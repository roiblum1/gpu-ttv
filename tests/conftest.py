"""Shared builders for tests."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ttv.config import Policy
from ttv.models import ServerScanServer, Stage
from ttv.store import PostgresStore

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DSN = os.environ.get("TTV_TEST_DSN")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    """A migrated, empty database per test (needs `TTV_TEST_DSN`)."""
    assert DSN
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    s = await PostgresStore.connect(DSN)
    await s.migrate(MIGRATIONS)
    yield s
    await s.close()


def server(**overrides: Any) -> ServerScanServer:
    """Build a healthy AVAILABLE 8-GPU server-scan record; override any field."""
    base = ServerScanServer(
        id="srv_1",
        name="ocp4-prod-site1-gpu-01",
        vendor="cisco",
        serial_normalized="cis0001",
        system_uuid="a087f1f7-362c-4741-adb7-20dfaedabda6",
        model="UCS C845A M8",
        site_id="site1",
        source_provider="UCS_CENTRAL",
        gpu_models=("NVIDIA H100 80GB",) * 8,
        profile_template_name="SPT-OCP",
        lifecycle_state="AVAILABLE",
        cluster_name=None,
        mce_name=None,
        reported_name=None,
        contested_with=None,
        claim_changed_at=None,
        maintenance_enabled=False,
        maintenance_reason=None,
        maintenance_since=None,
        health_overall="OK",
        reachable=True,
        unreachable_since=None,
        last_seen_at=T0,
        created_at=T0 - timedelta(days=10),
        stale=False,
    )
    return dataclasses.replace(base, **overrides)


@pytest.fixture
def policy() -> Policy:
    """The example policy with one target cluster and tight objectives for overdue tests."""
    return Policy(
        policy_version="test-1",
        teams={
            Stage.PRE_PROVISIONING: "DC",
            Stage.PROVISIONING: "Redbull",
            Stage.GPU_PLATFORM: "POPAI",
            Stage.SERVING: "Serving",
        },
        stage_objectives={
            Stage.PRE_PROVISIONING: timedelta(days=3),
            Stage.PROVISIONING: timedelta(hours=1),
            Stage.GPU_PLATFORM: timedelta(days=1),
            Stage.SERVING: timedelta(days=7),
        },
        target_clusters=frozenset({"ocp4-prod-gpu-site1"}),
    )
