"""Snapshot arithmetic, checksums and immutability (AGENT.md §10, §17; ADR-0003)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import T0, synthetic_snapshot
from ttv.pricing.importer import select_from_files
from ttv.pricing.snapshot import (
    SnapshotError,
    content_sha256,
    latest,
    load_snapshot,
    load_snapshot_dir,
    parse_snapshot,
    per_gpu_rate,
    write_snapshot,
)

pytestmark = pytest.mark.unit


def test_linear_pricing_eight_gpu_hours_is_one_instance_hour() -> None:
    # AGENT.md §17: $80.00/h for 8 GPUs → $10.00 per GPU-hour; 8 GPU-hours → $80.00, never $640.
    rate = per_gpu_rate(Decimal("80.00"), 8)
    assert rate == Decimal(10)
    assert Decimal(8) * rate == Decimal(80)
    assert Decimal(8) * rate != Decimal(8) * Decimal("80.00")


def test_snapshot_stores_the_per_gpu_rate_and_a_stable_id() -> None:
    snap = synthetic_snapshot(T0)
    (rate,) = snap.rates
    assert (rate.instance_hourly_usd, rate.instance_gpus, rate.per_gpu_hour_usd) == (
        "80.00",
        8,
        "10",
    )
    assert snap.snapshot_id == "aws-ec2-ondemand-us-east-1-20261001T120000Z-map-test-1"
    assert snap.currency == "USD"
    assert snap.purchase_option == "OnDemand"


def test_round_trip_verifies(tmp_path: Path) -> None:
    snap = synthetic_snapshot(T0)
    path, written = write_snapshot(snap, tmp_path)
    assert written
    assert load_snapshot(path) == snap


def test_tampered_rate_is_refused(tmp_path: Path) -> None:
    path, _ = write_snapshot(synthetic_snapshot(T0), tmp_path)
    doc = json.loads(path.read_text())
    doc["rates"][0]["instance_hourly_usd"] = "8.00"
    doc["rates"][0]["per_gpu_hour_usd"] = "1"
    path.write_text(json.dumps(doc))
    with pytest.raises(SnapshotError, match="content_sha256 mismatch"):
        load_snapshot(path)


def test_resealed_but_inconsistent_rate_is_refused() -> None:
    # A per-GPU rate that is not hourly / GPUs fails validation even with a matching checksum.
    doc = synthetic_snapshot(T0).document()
    doc["rates"][0]["per_gpu_hour_usd"] = "80"
    doc["content_sha256"] = content_sha256({k: v for k, v in doc.items() if k != "content_sha256"})
    with pytest.raises(SnapshotError, match="not hourly / gpus"):
        parse_snapshot(doc)


def test_snapshot_ids_are_immutable(tmp_path: Path) -> None:
    write_snapshot(synthetic_snapshot(T0), tmp_path)
    assert write_snapshot(synthetic_snapshot(T0), tmp_path)[1] is False
    other = synthetic_snapshot(T0, {"NVIDIA H100 80GB": ("96.00", 8)})
    with pytest.raises(SnapshotError, match="immutable"):
        write_snapshot(other, tmp_path)


def test_directory_load_and_latest(tmp_path: Path) -> None:
    old, new = synthetic_snapshot(T0), synthetic_snapshot(T0 + timedelta(days=7))
    write_snapshot(new, tmp_path)
    write_snapshot(old, tmp_path)
    loaded = load_snapshot_dir(tmp_path)
    assert [s.snapshot_id for s in loaded] == [old.snapshot_id, new.snapshot_id]
    assert latest(loaded) == new
    assert load_snapshot_dir(tmp_path / "missing") == []


def test_committed_snapshots_verify() -> None:
    directory = Path(__file__).resolve().parents[1] / "config" / "pricing" / "snapshots"
    for snap in load_snapshot_dir(directory):
        assert snap.currency == "USD"
        for rate in snap.rates:
            hourly = Decimal(rate.instance_hourly_usd)
            assert Decimal(rate.per_gpu_hour_usd) == per_gpu_rate(hourly, rate.instance_gpus)


def test_without_a_database_the_latest_file_or_the_pinned_one_is_present_day() -> None:
    old, new = synthetic_snapshot(T0), synthetic_snapshot(T0 + timedelta(days=7))
    present = select_from_files([old, new], None)
    assert present is not None
    assert (present.snapshot_id, present.source) == (new.snapshot_id, "latest")
    pinned = select_from_files([old, new], old.snapshot_id)
    assert pinned is not None
    assert (pinned.snapshot_id, pinned.priced) == (old.snapshot_id, frozenset({"NVIDIA H100 80GB"}))
    assert select_from_files([], None) is None
    with pytest.raises(SnapshotError, match="not in the pricing directory"):
        select_from_files([old], "aws-ec2-ondemand-nowhere")
