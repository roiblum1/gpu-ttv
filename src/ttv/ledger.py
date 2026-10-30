"""Pure ledger planning: a scan's decisions into interval opens, closes, refreshes (§20.5-20.6)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ttv.models import Bucket, Decision


@dataclass(frozen=True, order=True)
class BucketKey:
    """
    The identity of an open interval: any change in these fields closes it and opens a new one.

    Attributes:
        category (str): Capacity category.
        reason_code (str): Matching rule.
        stage_owner (str): Owning team, or "".
        holder_ref (str): Holding workload, or "".
        pool (str): Node pool, or "".
        team (str): Consuming team, or "".
        gpu_model (str): GPU model, or "".
        gpus (int): Physical GPUs.
    """

    category: str
    reason_code: str
    stage_owner: str
    holder_ref: str
    pool: str
    team: str
    gpu_model: str
    gpus: int


def key_of(bucket: Bucket) -> BucketKey:
    """
    Return the interval key of a bucket.

    Args:
        bucket (Bucket): The bucket.

    Returns:
        BucketKey: Its key; None fields become "".
    """
    return BucketKey(
        category=str(bucket.category),
        reason_code=str(bucket.reason),
        stage_owner=bucket.stage_owner or "",
        holder_ref=bucket.holder_ref or "",
        pool=bucket.pool or "",
        team=bucket.team or "",
        gpu_model=bucket.gpu_model or "",
        gpus=bucket.gpus,
    )


@dataclass(frozen=True)
class OpenInterval:
    """
    An interval currently open in the ledger.

    Attributes:
        id (int): Row id.
        asset_id (str): Asset.
        key (BucketKey): Its key.
        stage (str | None): Stage, for IDLE intervals.
        stage_entered_at (datetime | None): Start of the stage episode.
        stage_entered_lower_bound (bool): The episode may have begun earlier.
        started_at (datetime): When this interval opened.
        flags (tuple[str, ...]): Current flags.
        evidence (Mapping[str, Any]): Current evidence.
    """

    id: int
    asset_id: str
    key: BucketKey
    stage: str | None
    stage_entered_at: datetime | None
    stage_entered_lower_bound: bool
    started_at: datetime
    flags: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LastStage:
    """
    The asset's most recent interval that had a stage (open or closed).

    Attributes:
        stage (str): That stage.
        stage_entered_at (datetime): Its episode start.
        lower_bound (bool): Whether that start was a lower bound.
    """

    stage: str
    stage_entered_at: datetime
    lower_bound: bool


@dataclass(frozen=True)
class NewInterval:
    """
    An interval to open.

    Attributes:
        asset_id (str): Asset.
        bucket (Bucket): Its classification.
        started_at (datetime): Open time (the scan time).
        stage_entered_at (datetime | None): Start of the stage episode.
        stage_entered_lower_bound (bool): Whether that start is a lower bound.
        policy_version (str): Policy the decision used.
    """

    asset_id: str
    bucket: Bucket
    started_at: datetime
    stage_entered_at: datetime | None
    stage_entered_lower_bound: bool
    policy_version: str


@dataclass(frozen=True)
class Close:
    """
    An interval to close.

    Attributes:
        interval_id (int): Row id.
        ended_at (datetime): Close time.
    """

    interval_id: int
    ended_at: datetime


@dataclass(frozen=True)
class Refresh:
    """
    New flags and evidence for an interval that stays open.

    Attributes:
        interval_id (int): Row id.
        flags (tuple[str, ...]): Flags now.
        evidence (Mapping[str, Any]): Evidence now.
    """

    interval_id: int
    flags: tuple[str, ...]
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class LedgerPlan:
    """
    Everything one scan changes in the ledger.

    Attributes:
        opens (tuple[NewInterval, ...]): Intervals to open.
        closes (tuple[Close, ...]): Intervals to close.
        refreshes (tuple[Refresh, ...]): Open intervals whose flags or evidence changed.
    """

    opens: tuple[NewInterval, ...] = ()
    closes: tuple[Close, ...] = ()
    refreshes: tuple[Refresh, ...] = ()


def plan(
    *,
    now: datetime,
    decisions: Mapping[str, Decision],
    open_intervals: Sequence[OpenInterval],
    last_stage: Mapping[str, LastStage],
    known_assets: Set[str],
    vanished: Mapping[str, datetime],
) -> LedgerPlan:
    """
    Diff this scan's decisions against the open intervals.

    Assets with a decision are reconciled bucket by bucket. Assets in `vanished` (absent from
    complete listings beyond the tombstone period) have every interval closed at their last
    observation. Every other asset keeps its open intervals untouched, so a partial or failed
    listing never reads as deletion (AGENT.md §4, §20.6).

    Args:
        now (datetime): Scan time.
        decisions (Mapping[str, Decision]): Decisions by asset id; an empty bucket tuple closes all.
        open_intervals (Sequence[OpenInterval]): Currently open intervals.
        last_stage (Mapping[str, LastStage]): Each asset's most recent staged interval.
        known_assets (Set[str]): Assets the ledger has seen before.
        vanished (Mapping[str, datetime]): Assets to retire, with their last observation time.

    Returns:
        LedgerPlan: The changes.
    """
    by_asset: dict[str, list[OpenInterval]] = defaultdict(list)
    for interval in open_intervals:
        by_asset[interval.asset_id].append(interval)

    opens: list[NewInterval] = []
    closes: list[Close] = []
    refreshes: list[Refresh] = []

    for asset_id, ended_at in vanished.items():
        if asset_id not in decisions:
            closes.extend(
                Close(i.id, max(ended_at, i.started_at)) for i in by_asset.get(asset_id, ())
            )

    for asset_id, decision in decisions.items():
        current = by_asset.get(asset_id, [])
        kept: dict[BucketKey, OpenInterval] = {}
        for interval in sorted(current, key=lambda i: i.id):
            if interval.key in kept:
                closes.append(Close(interval.id, now))
            else:
                kept[interval.key] = interval
        wanted = {key_of(b): b for b in decision.buckets}

        for key, interval in kept.items():
            bucket = wanted.get(key)
            if bucket is None:
                closes.append(Close(interval.id, now))
            elif tuple(bucket.flags) != tuple(interval.flags) or dict(bucket.evidence) != dict(
                interval.evidence
            ):
                refreshes.append(Refresh(interval.id, tuple(bucket.flags), dict(bucket.evidence)))

        for key, bucket in wanted.items():
            if key in kept:
                continue
            entered, lower = _stage_start(
                bucket, decision, current, last_stage.get(asset_id), asset_id in known_assets, now
            )
            opens.append(
                NewInterval(asset_id, bucket, now, entered, lower, decision.policy_version)
            )

    return LedgerPlan(tuple(opens), tuple(closes), tuple(refreshes))


def _stage_start(
    bucket: Bucket,
    decision: Decision,
    current: Sequence[OpenInterval],
    last: LastStage | None,
    known: bool,
    now: datetime,
) -> tuple[datetime | None, bool]:
    """
    Decide when the stage episode of a newly opened interval began.

    The episode carries over while the asset stays in the same stage, even across reason
    changes or gaps with no stage (UNKNOWN). A new asset uses the classifier's evidence;
    otherwise the transition is dated to this scan.

    Args:
        bucket (Bucket): The new bucket.
        decision (Decision): Its decision (for the stage clock evidence).
        current (Sequence[OpenInterval]): The asset's intervals open before this scan.
        last (LastStage | None): The asset's most recent staged interval.
        known (bool): Whether the ledger has seen the asset before.
        now (datetime): Scan time.

    Returns:
        tuple[datetime | None, bool]: `(stage_entered_at, lower_bound)`; None if no stage.
    """
    if bucket.stage is None:
        return None, False
    stage = str(bucket.stage)
    for interval in current:
        if interval.stage == stage and interval.stage_entered_at is not None:
            return interval.stage_entered_at, interval.stage_entered_lower_bound
    if last is not None and last.stage == stage:
        return last.stage_entered_at, last.lower_bound
    if not known:
        clock = decision.stage_clocks.get(stage)
        if clock is not None and clock.since is not None and clock.since <= now:
            return clock.since, clock.lower_bound
        return now, True
    came_from_staged = any(i.stage is not None for i in current)
    return now, not came_from_staged
