"""ADR-0002 rows 1-5 for GPU servers only the profile-less collector knows (fixture-tested)."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from conftest import T0
from ttv.classify import COLLECTOR_SOURCE_PREFIX, ClassifyContext, classify
from ttv.config import Exclusions, Policy
from ttv.models import AssetInput, Category, Decision, Flag, ManagerServer, Reason, SourceStatus

MANAGER = "ucs-central:central-1"


def record(**overrides: Any) -> ManagerServer:
    """A Cisco blade with 8 GPUs and no profile."""
    base = ManagerServer(
        vendor="cisco",
        serial_normalized="fch2345abcd",
        manager=MANAGER,
        source_id="compute/sys-1/chassis-1/blade-3",
        name="sys/chassis-1/blade-3",
        model="UCSX-210C-M7",
        profile_name=None,
        gpu_models=("NVIDIA H100 80GB",) * 8,
        domain="dc1-domain-a",
    )
    return dataclasses.replace(base, **overrides)


def decide(
    policy: Policy,
    m: ManagerServer | None = None,
    *,
    failed_scans: int = 0,
    first_seen: Any = T0 - timedelta(days=5),
    profile_since: Any = None,
    conflict: str | None = None,
) -> Decision:
    """Classify a collector-only asset with a healthy (or failing) manager."""
    m = m or record()
    status = SourceStatus(COLLECTOR_SOURCE_PREFIX + MANAGER, failed_scans, T0)
    ctx = ClassifyContext(policy=policy, sources={status.name: status}, now=T0)
    asset = AssetInput(
        f"cisco:{m.serial_normalized}",
        None,
        conflict=conflict,
        collector=m,
        collector_first_seen=first_seen,
        collector_profile_since=profile_since,
    )
    decision = classify(asset, ctx)
    assert (
        sum(b.gpus for b in decision.buckets) == decision.expected_gpus == len(m.gpu_models or ())
    )
    return decision


def only(decision: Decision) -> tuple[Category, Reason, str | None]:
    """The single (category, reason, owner) of a server-level decision."""
    (bucket,) = {(b.category, b.reason, b.stage_owner) for b in decision.buckets}
    return bucket


def test_row3_gpu_server_without_a_profile_is_dc_from_first_sighting(policy: Policy) -> None:
    decision = decide(policy)
    assert only(decision) == (Category.IDLE, Reason.PROFILE_NONE, "DC")
    clock = decision.stage_clocks["PRE_PROVISIONING"]
    assert (clock.since, clock.lower_bound) == (T0 - timedelta(days=5), True)


def test_row4_non_ocp_profile_is_not_charged(policy: Policy) -> None:
    for name in ("vdi-pool-07", "OCP-upper-case", "lab-ocp-01"):
        decision = decide(policy, record(profile_name=name))
        assert only(decision) == (Category.NOT_CHARGED, Reason.OTHER_INTENDED_USE, None), name


def test_row5_ocp_profile_missing_from_server_scan_is_unknown_then_overdue(policy: Policy) -> None:
    fresh = decide(policy, record(profile_name="ocp4-prod-gpu-01"), profile_since=T0)
    assert only(fresh) == (Category.UNKNOWN, Reason.HANDOVER_PENDING, None)
    assert Flag.HANDOVER_OVERDUE not in fresh.buckets[0].flags
    late = decide(
        policy, record(profile_name="ocp4-prod-gpu-01"), profile_since=T0 - timedelta(hours=7)
    )
    assert Flag.HANDOVER_OVERDUE in late.buckets[0].flags
    assert {m.name for m in late.milestones} == {"ocp_profile_seen"}


def test_handover_clock_starts_at_the_profile_not_the_first_sighting(policy: Policy) -> None:
    decision = decide(
        policy,
        record(profile_name="ocp4-prod-gpu-01"),
        first_seen=T0 - timedelta(days=30),
        profile_since=T0 - timedelta(hours=1),
    )
    assert Flag.HANDOVER_OVERDUE not in decision.buckets[0].flags


def test_row2_stale_manager_is_unknown_never_dc(policy: Policy) -> None:
    decision = decide(policy, failed_scans=2)
    assert only(decision) == (Category.UNKNOWN, Reason.SOURCE_STALE, None)


def test_row2_manager_without_status_is_unknown(policy: Policy) -> None:
    m = record()
    ctx = ClassifyContext(policy=policy, sources={}, now=T0)
    asset = AssetInput("cisco:x", None, collector=m, collector_first_seen=T0)
    assert only(classify(asset, ctx)) == (Category.UNKNOWN, Reason.SOURCE_STALE, None)


def test_row1_exclusion_and_row2b_conflict_win(policy: Policy) -> None:
    excluded = policy.model_copy(
        update={"exclusions": Exclusions(asset_ids=frozenset({"cisco:fch2345abcd"}))}
    )
    assert only(decide(excluded)) == (Category.NOT_CHARGED, Reason.EXCLUDED, None)
    conflicted = decide(policy, conflict="two managers")
    assert only(conflicted) == (Category.UNKNOWN, Reason.IDENTITY_CONFLICT, None)


def test_mixed_gpu_models_from_the_collector_sum_to_e(policy: Policy) -> None:
    models = ("NVIDIA L40S 48GB",) * 3 + ("NVIDIA T4 16GB",)
    decision = decide(policy, record(gpu_models=models))
    assert {(b.gpu_model, b.gpus) for b in decision.buckets} == {
        ("NVIDIA L40S 48GB", 3),
        ("NVIDIA T4 16GB", 1),
    }


def test_a_collector_record_without_gpus_is_rejected(policy: Policy) -> None:
    with pytest.raises(ValueError, match="GPUs"):
        decide(policy, record(gpu_models=()))


def test_row2b_unresolved_profile_is_unknown_never_dc(policy: Policy) -> None:
    unresolved = record(profile_unresolved="target name shared by several servers")
    decision = decide(policy, unresolved)
    assert only(decision) == (Category.UNKNOWN, Reason.IDENTITY_CONFLICT, None)
    assert "several servers" in decision.buckets[0].evidence["conflict"]
