"""ADR-0002 rule table: each row, its precedence, and the bucket invariants."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from conftest import T0, server
from ttv.classify import SERVERSCAN_SOURCE, ClassifyContext, classify
from ttv.config import Exclusions, Policy
from ttv.models import AssetInput, Category, Decision, Flag, Reason, SourceStatus, Stage


def run(
    policy: Policy,
    conflict: str | None = None,
    ctx: ClassifyContext | None = None,
    **fields: object,
) -> Decision:
    """Classify one asset built from `server(**fields)`."""
    asset = AssetInput("cisco:cis0001", server(**fields), conflict=conflict)
    return classify(asset, ctx or ClassifyContext(policy=policy))


def only(decision: Decision) -> tuple[Category, Reason, str | None]:
    """Return the single bucket's (category, reason, owner)."""
    (bucket,) = decision.buckets
    return bucket.category, bucket.reason, bucket.stage_owner


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, (Category.IDLE, Reason.NOT_IN_CLUSTER, "Redbull")),
        (
            {"lifecycle_state": "INSTALLED_TO_INVENTORY"},
            (Category.IDLE, Reason.IN_MCE_INVENTORY, "Redbull"),
        ),
        (
            {"lifecycle_state": "INSTALLED", "cluster_name": "other-cluster"},
            (Category.NOT_CHARGED, Reason.OUTSIDE_TARGET_CLUSTERS, None),
        ),
        (
            {"lifecycle_state": "INSTALLED", "cluster_name": "ocp4-prod-gpu-site1"},
            (Category.UNKNOWN, Reason.CLUSTER_NOT_OBSERVED, None),
        ),
        ({"lifecycle_state": None}, (Category.UNKNOWN, Reason.MEMBERSHIP_UNKNOWN, None)),
        ({"maintenance_enabled": True}, (Category.IDLE, Reason.MAINTENANCE, "DC")),
        ({"health_overall": "CRITICAL"}, (Category.IDLE, Reason.HARDWARE_CRITICAL, "DC")),
        ({"reachable": False}, (Category.IDLE, Reason.BMC_UNREACHABLE, "DC")),
        ({"contested_with": "ocp4-prod-gpu-site2"}, (Category.UNKNOWN, Reason.CONTESTED, None)),
        ({"stale": True}, (Category.UNKNOWN, Reason.SOURCE_STALE, None)),
    ],
)
def test_each_server_level_row(
    policy: Policy, fields: dict[str, Any], expected: tuple[object, ...]
) -> None:
    decision = run(policy, **fields)
    assert only(decision) == expected
    assert decision.expected_gpus == sum(b.gpus for b in decision.buckets) == 8


def test_first_match_wins_in_rule_order(policy: Policy) -> None:
    # Stale beats maintenance beats critical health beats AVAILABLE.
    assert (
        only(run(policy, stale=True, maintenance_enabled=True, health_overall="CRITICAL"))[1]
        == Reason.SOURCE_STALE
    )
    assert (
        only(run(policy, maintenance_enabled=True, health_overall="CRITICAL"))[1]
        == Reason.MAINTENANCE
    )
    assert (
        only(run(policy, health_overall="CRITICAL", reachable=False))[1] == Reason.HARDWARE_CRITICAL
    )


def test_exclusion_beats_everything(policy: Policy) -> None:
    excluded = policy.model_copy(
        update={"exclusions": Exclusions(asset_ids=frozenset({"cisco:cis0001"}))}
    )
    assert only(run(excluded, stale=True, maintenance_enabled=True)) == (
        Category.NOT_CHARGED,
        Reason.EXCLUDED,
        None,
    )


def test_running_work_wins_over_critical_health(policy: Policy) -> None:
    # C3: an INSTALLED server keeps its membership classification; critical health becomes a flag.
    decision = run(
        policy,
        lifecycle_state="INSTALLED",
        cluster_name="other-cluster",
        health_overall="CRITICAL",
        reachable=False,
    )
    (bucket,) = decision.buckets
    assert (bucket.category, bucket.reason) == (
        Category.NOT_CHARGED,
        Reason.OUTSIDE_TARGET_CLUSTERS,
    )
    assert set(bucket.flags) == {Flag.HARDWARE_CRITICAL, Flag.BMC_UNREACHABLE}


def test_identity_conflict_is_unknown(policy: Policy) -> None:
    assert only(run(policy, conflict="2 records share this serial")) == (
        Category.UNKNOWN,
        Reason.IDENTITY_CONFLICT,
        None,
    )


def test_failing_source_turns_unknown_only_after_grace(policy: Policy) -> None:
    def ctx(failed: int) -> ClassifyContext:
        return ClassifyContext(
            policy=policy, sources={SERVERSCAN_SOURCE: SourceStatus(SERVERSCAN_SOURCE, failed, T0)}
        )

    assert only(run(policy, ctx=ctx(1)))[1] == Reason.NOT_IN_CLUSTER
    assert only(run(policy, ctx=ctx(2)))[1] == Reason.SOURCE_STALE


def test_mixed_gpu_models_split_into_buckets_that_sum_to_e(policy: Policy) -> None:
    decision = run(policy, gpu_models=("NVIDIA H100 80GB",) * 6 + ("NVIDIA L40S 48GB",) * 2)
    assert {(b.gpu_model, b.gpus) for b in decision.buckets} == {
        ("NVIDIA H100 80GB", 6),
        ("NVIDIA L40S 48GB", 2),
    }
    assert sum(b.gpus for b in decision.buckets) == decision.expected_gpus == 8
    assert {b.category for b in decision.buckets} == {Category.IDLE}


def test_redbull_stage_clock_prefers_claim_change_then_lower_bound_creation(policy: Policy) -> None:
    changed = T0 - timedelta(hours=5)
    clock = run(policy, claim_changed_at=changed).stage_clocks["PROVISIONING"]
    assert (clock.since, clock.lower_bound, clock.source) == (
        changed,
        False,
        "openshift.claim_changed_at",
    )
    clock = run(policy).stage_clocks["PROVISIONING"]
    assert (clock.since, clock.lower_bound) == (T0 - timedelta(days=10), True)


def test_unknown_and_not_charged_have_no_stage(policy: Policy) -> None:
    cases: tuple[dict[str, Any], ...] = (
        {"stale": True},
        {"lifecycle_state": "INSTALLED", "cluster_name": "other-cluster"},
    )
    for fields in cases:
        decision = run(policy, **fields)
        assert all(b.stage is None and b.stage_owner is None for b in decision.buckets)
        assert decision.stage_clocks == {}


def test_trace_lists_rules_until_the_match(policy: Policy) -> None:
    trace = run(policy, maintenance_enabled=True).trace
    assert trace == (
        "1 EXCLUDED: no",
        "2 SOURCE_STALE: no",
        "2b IDENTITY_CONFLICT: no",
        "6a MAINTENANCE: MATCH",
    )


def test_assets_without_gpus_are_rejected(policy: Policy) -> None:
    with pytest.raises(ValueError, match="needs a server-scan record with GPUs"):
        run(policy, gpu_models=())
    assert Stage.PROVISIONING in policy.teams
