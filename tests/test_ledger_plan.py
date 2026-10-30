"""Ledger planning: interval diffs, stage episodes, and partial-listing safety."""

from __future__ import annotations

from datetime import timedelta

from conftest import T0, server
from ttv.classify import ClassifyContext, classify
from ttv.config import Policy
from ttv.ledger import LastStage, LedgerPlan, NewInterval, OpenInterval, key_of, plan
from ttv.models import AssetInput, Decision

A = "cisco:cis0001"


def decide(policy: Policy, **fields: object) -> Decision:
    """Classify asset A with the given server-scan fields."""
    return classify(AssetInput(A, server(**fields)), ClassifyContext(policy=policy))


def as_open(new: NewInterval, interval_id: int) -> OpenInterval:
    """Pretend a planned interval was written and is now open."""
    b = new.bucket
    return OpenInterval(
        interval_id,
        new.asset_id,
        key_of(b),
        str(b.stage) if b.stage else None,
        new.stage_entered_at,
        new.stage_entered_lower_bound,
        new.started_at,
        tuple(b.flags),
        dict(b.evidence),
    )


def run_plan(policy_decision: Decision, opened: list[OpenInterval], **kw: object) -> LedgerPlan:
    """Plan one scan for asset A with sensible defaults."""
    args: dict[str, object] = {
        "now": T0,
        "decisions": {A: policy_decision},
        "open_intervals": opened,
        "last_stage": {},
        "known_assets": {A} if opened else set(),
        "vanished": {},
    }
    args.update(kw)
    return plan(**args)  # type: ignore[arg-type]


def test_new_asset_opens_one_interval_per_bucket_with_evidence_clock(policy: Policy) -> None:
    result = run_plan(decide(policy), [])
    (new,) = result.opens
    assert (new.bucket.reason, new.bucket.gpus, new.started_at) == ("NOT_IN_CLUSTER", 8, T0)
    assert (new.stage_entered_at, new.stage_entered_lower_bound) == (T0 - timedelta(days=10), True)
    assert result.closes == () and result.refreshes == ()


def test_unchanged_scan_is_a_no_op(policy: Policy) -> None:
    first = run_plan(decide(policy), [])
    opened = [as_open(first.opens[0], 1)]
    assert run_plan(decide(policy), opened, now=T0 + timedelta(minutes=5)) == LedgerPlan()


def test_reason_change_within_a_stage_keeps_the_episode_start(policy: Policy) -> None:
    opened = [as_open(run_plan(decide(policy), []).opens[0], 1)]
    later = T0 + timedelta(hours=2)
    result = run_plan(decide(policy, lifecycle_state="INSTALLED_TO_INVENTORY"), opened, now=later)
    (close,) = result.closes
    (new,) = result.opens
    assert (close.interval_id, close.ended_at) == (1, later)
    assert new.bucket.reason == "IN_MCE_INVENTORY"
    assert new.stage_entered_at == T0 - timedelta(days=10)


def test_stage_change_starts_a_new_episode_at_scan_time(policy: Policy) -> None:
    opened = [as_open(run_plan(decide(policy), []).opens[0], 1)]
    later = T0 + timedelta(hours=3)
    (new,) = run_plan(decide(policy, maintenance_enabled=True), opened, now=later).opens
    assert (new.bucket.stage_owner, new.stage_entered_at, new.stage_entered_lower_bound) == (
        "DC",
        later,
        False,
    )


def test_episode_survives_an_unknown_gap(policy: Policy) -> None:
    gap_start = T0 + timedelta(hours=1)
    unknown = run_plan(decide(policy, stale=True), [], now=gap_start, known_assets={A}).opens[0]
    opened = [as_open(unknown, 7)]
    last = {A: LastStage("PROVISIONING", T0 - timedelta(days=10), True)}
    (new,) = run_plan(decide(policy), opened, now=T0 + timedelta(hours=4), last_stage=last).opens
    assert (new.stage_entered_at, new.stage_entered_lower_bound) == (T0 - timedelta(days=10), True)


def test_known_asset_entering_a_stage_after_unknown_is_a_lower_bound(policy: Policy) -> None:
    unknown = run_plan(decide(policy, stale=True), [], known_assets={A}).opens[0]
    later = T0 + timedelta(hours=1)
    (new,) = run_plan(
        decide(policy, maintenance_enabled=True), [as_open(unknown, 3)], now=later
    ).opens
    assert (new.stage_entered_at, new.stage_entered_lower_bound) == (later, True)


def test_absent_asset_is_kept_open_until_tombstoned(policy: Policy) -> None:
    opened = [as_open(run_plan(decide(policy), []).opens[0], 1)]
    later = T0 + timedelta(hours=30)
    kept = plan(
        now=later, decisions={}, open_intervals=opened, last_stage={}, known_assets={A}, vanished={}
    )
    assert kept == LedgerPlan()
    retired = plan(
        now=later,
        decisions={},
        open_intervals=opened,
        last_stage={},
        known_assets={A},
        vanished={A: T0 + timedelta(hours=2)},
    )
    assert retired.closes[0].ended_at == T0 + timedelta(hours=2)


def test_flag_change_refreshes_without_splitting(policy: Policy) -> None:
    base = decide(policy, lifecycle_state="INSTALLED", cluster_name="other")
    opened = [as_open(run_plan(base, []).opens[0], 1)]
    flagged = decide(
        policy, lifecycle_state="INSTALLED", cluster_name="other", health_overall="CRITICAL"
    )
    result = run_plan(flagged, opened)
    assert result.opens == () and result.closes == ()
    assert result.refreshes[0].flags == ("hardware_critical",)


def test_gpu_count_change_reopens_with_the_new_count(policy: Policy) -> None:
    opened = [as_open(run_plan(decide(policy), []).opens[0], 1)]
    result = run_plan(decide(policy, gpu_models=("NVIDIA H100 80GB",) * 6), opened)
    assert [c.interval_id for c in result.closes] == [1]
    assert [o.bucket.gpus for o in result.opens] == [6]


def test_asset_losing_all_gpus_closes_everything(policy: Policy) -> None:
    opened = [as_open(run_plan(decide(policy), []).opens[0], 1)]
    empty = Decision(A, 0, (), (), {}, "test-1")
    result = run_plan(empty, opened)
    assert [c.interval_id for c in result.closes] == [1] and result.opens == ()
