"""Serving requests from pod counter readings (ADR-0009); synthetic readings, fixture-tested."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import T0
from ttv.demand import CounterHistory, holder_demand, pod_window
from ttv.models import CounterSample

H = timedelta(hours=1)
DAY = timedelta(hours=24)
REF = "team-a/LLMInferenceService/qwen"


def reading(
    at: datetime,
    value: float,
    *,
    pod: str = "qwen-a",
    started: datetime | None = T0 - timedelta(days=3),
    restarts: int = 0,
) -> CounterSample:
    """One counter reading of a pod of `REF`."""
    return CounterSample("site1", "team-a", pod, REF, at, value, started, restarts)


def every_5_minutes(
    start: datetime, end: datetime, value_at: dict[int, float]
) -> list[CounterSample]:
    """Readings every 5 min; `value_at` maps minutes after `start` to the counter value."""
    out: list[CounterSample] = []
    value = 0.0
    minute = 0
    while start + timedelta(minutes=minute) <= end:
        value = value_at.get(minute, value)
        out.append(reading(start + timedelta(minutes=minute), value))
        minute += 5
    return out


def test_requests_are_the_rise_since_the_baseline_before_the_window() -> None:
    samples = [reading(T0 - DAY - H, 100.0), reading(T0 - H, 150.0), reading(T0, 180.0)]
    day = pod_window(samples, T0 - DAY)
    assert day is not None
    assert (day.requests, day.complete, day.covered_from) == (80.0, True, T0 - DAY)
    hour = pod_window(samples, T0 - H)
    assert hour is not None and (hour.requests, hour.complete) == (30.0, True)


def test_a_pod_started_inside_the_window_counts_from_zero() -> None:
    started = T0 - timedelta(hours=2)
    result = pod_window([reading(T0, 42.0, started=started)], T0 - DAY)
    assert result is not None
    assert (result.requests, result.complete, result.covered_from) == (42.0, True, started)


def test_a_counter_at_zero_proves_no_requests_without_any_history() -> None:
    result = pod_window([reading(T0, 0.0)], T0 - DAY)
    assert result is not None and (result.requests, result.complete) == (0.0, True)


def test_an_old_pod_without_a_baseline_is_a_lower_bound() -> None:
    result = pod_window([reading(T0 - timedelta(minutes=10), 500.0), reading(T0, 510.0)], T0 - DAY)
    assert result is not None
    assert (result.requests, result.complete) == (10.0, False)
    assert result.covered_from == T0 - timedelta(minutes=10)


@pytest.mark.parametrize(
    "second",
    [reading(T0, 7.0), reading(T0, 300.0, restarts=1)],
    ids=["value-dropped", "restart-counted"],
)
def test_a_reset_counts_the_new_value_and_is_a_lower_bound(second: CounterSample) -> None:
    result = pod_window([reading(T0 - DAY - H, 0.0), reading(T0 - H, 200.0), second], T0 - DAY)
    assert result is not None
    assert result.requests == 200.0 + second.value and result.complete is False


def test_no_reading_inside_the_window_is_nothing_known() -> None:
    assert pod_window([reading(T0 - DAY - H, 5.0)], T0 - DAY) is None


def test_deployment_sums_pods_including_one_that_is_gone() -> None:
    a = [reading(T0 - DAY - H, 10.0), reading(T0, 70.0)]
    gone = [
        reading(T0 - DAY - H, 0.0, pod="qwen-old"),
        reading(T0 - timedelta(hours=3), 25.0, pod="qwen-old"),
    ]
    d = holder_demand(
        {("team-a", "qwen-a"): a, ("team-a", "qwen-old"): gone},
        now=T0,
        window=DAY,
        recent=H,
        read=[("team-a", "qwen-a")],
        unreadable=[],
    )
    assert d is not None
    assert (d.requests, d.complete, d.covered_hours) == (85.0, True, 24.0)
    assert d.pods == ("team-a/qwen-a",) and d.unreadable == ()


def test_recent_window_is_counted_separately() -> None:
    samples = every_5_minutes(T0 - DAY - H, T0, {0: 0.0, 60: 100.0, 1440: 140.0, 1500: 150.0})
    d = holder_demand(
        {("team-a", "qwen-a"): samples},
        now=T0,
        window=DAY,
        recent=H,
        read=[("team-a", "qwen-a")],
        unreadable=[],
    )
    assert d is not None
    assert (d.requests, d.recent_requests, d.recent_complete) == (50.0, 10.0, True)


def test_an_unreadable_current_pod_makes_the_count_incomplete() -> None:
    d = holder_demand(
        {("team-a", "qwen-a"): [reading(T0 - DAY - H, 0.0), reading(T0, 0.0)]},
        now=T0,
        window=DAY,
        recent=H,
        read=[("team-a", "qwen-a")],
        unreadable=[("team-a", "qwen-b")],
    )
    assert d is not None and (d.requests, d.complete, d.recent_complete) == (0.0, False, False)
    assert d.unreadable == ("team-a/qwen-b",)


def test_nothing_read_and_nothing_failed_is_no_demand() -> None:
    assert holder_demand({}, now=T0, window=DAY, recent=H, read=[], unreadable=[]) is None
    failed = holder_demand({}, now=T0, window=DAY, recent=H, read=[], unreadable=[("ns", "p")])
    assert failed is not None and failed.complete is False and failed.requests == 0.0


def test_a_young_deployment_covers_only_its_lifetime() -> None:
    started = T0 - timedelta(hours=2)
    d = holder_demand(
        {("team-a", "qwen-a"): [reading(T0, 6.0, started=started)]},
        now=T0,
        window=DAY,
        recent=H,
        read=[("team-a", "qwen-a")],
        unreadable=[],
    )
    assert d is not None and (d.requests, d.complete, d.covered_hours) == (6.0, True, 2.0)


def test_history_keeps_a_baseline_and_replaces_a_repeated_reading() -> None:
    history = CounterHistory(DAY + H)
    history.add([reading(T0 - timedelta(hours=30), 1.0), reading(T0 - timedelta(hours=26), 2.0)])
    history.add([reading(T0, 5.0), reading(T0, 6.0)])
    history.prune(T0)
    (series,) = history.for_holder("site1", REF).values()
    assert [(s.observed_at, s.value) for s in series] == [
        (T0 - timedelta(hours=26), 2.0),
        (T0, 6.0),
    ]
    assert history.holders("site1") == [REF]
    history.prune(T0 + timedelta(days=3))
    assert history.holders("site1") == []
