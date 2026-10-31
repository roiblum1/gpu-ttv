"""`GET /decisions`: the per-asset list the dry-run spot check samples (docs/dry-run-review.md)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from conftest import T0
from ttv.api import create_app
from ttv.engine import Engine, ScanReport
from ttv.metrics import EngineMetrics
from ttv.models import Bucket, Category, Decision, Reason, Stage

pytestmark = pytest.mark.unit


def decision(asset_id: str, *buckets: Bucket) -> Decision:
    """A decision whose expected GPUs are its buckets' sum."""
    return Decision(asset_id, sum(b.gpus for b in buckets), buckets, (), {}, "test.1")


async def get(report: ScanReport | None, **params: Any) -> httpx.Response:
    """GET /decisions from an API around a stand-in engine that only carries a last report."""
    engine = SimpleNamespace(last_report=report, dry_run=True, store=None, metrics=EngineMetrics())
    app = create_app(cast(Engine, engine), timedelta(minutes=5))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
        return await client.get("/decisions", params=params)


REPORT = ScanReport(
    scan_id=0,
    status="dry_run",
    started_at=T0,
    finished_at=T0,
    decisions={
        "cisco:b": decision(
            "cisco:b",
            Bucket(Category.PRODUCTIVE, Reason.RUNAI_WORKLOAD, 2, "NVIDIA H100 80GB",
                   holder_ref="team-a/TrainingWorkload/train", cluster="site1"),
            Bucket(Category.IDLE, Reason.NO_WORKLOAD, 6, "NVIDIA H100 80GB",
                   stage=Stage.SERVING, stage_owner="Serving", cluster="site1"),
        ),
        "cisco:a": decision(
            "cisco:a",
            Bucket(Category.IDLE, Reason.NOT_IN_CLUSTER, 8, "NVIDIA H100 80GB",
                   stage=Stage.PROVISIONING, stage_owner="Redbull"),
        ),
        "dell:c": decision(
            "dell:c", Bucket(Category.UNKNOWN, Reason.SOURCE_STALE, 4, "NVIDIA L40S 48GB")
        ),
    },
)  # fmt: skip


async def test_decisions_before_the_first_scan_is_unavailable() -> None:
    assert (await get(None)).status_code == 503


async def test_decisions_lists_every_asset_in_id_order_with_its_buckets() -> None:
    body = (await get(REPORT)).json()
    assert body["dry_run"] is True
    assert body["matched"] == 3
    assert [a["asset_id"] for a in body["assets"]] == ["cisco:a", "cisco:b", "dell:c"]
    mixed = body["assets"][1]
    assert mixed["expected_gpus"] == sum(b["gpus"] for b in mixed["buckets"]) == 8
    assert mixed["buckets"][0] == {
        "category": "PRODUCTIVE",
        "reason": "RUNAI_WORKLOAD",
        "gpus": 2,
        "stage_owner": None,
        "cluster": "site1",
        "holder_ref": "team-a/TrainingWorkload/train",
        "flags": [],
    }


async def test_decisions_filter_by_category_reason_and_limit() -> None:
    idle = (await get(REPORT, category="IDLE")).json()
    assert [a["asset_id"] for a in idle["assets"]] == ["cisco:a", "cisco:b"]
    stale = (await get(REPORT, reason="SOURCE_STALE")).json()
    assert [a["asset_id"] for a in stale["assets"]] == ["dell:c"]
    both = (await get(REPORT, category="IDLE", reason="RUNAI_WORKLOAD")).json()
    assert both["matched"] == 0
    first = (await get(REPORT, limit=1)).json()
    assert first["matched"] == 3
    assert len(first["assets"]) == 1
    assert (await get(REPORT, limit=0)).status_code == 422
