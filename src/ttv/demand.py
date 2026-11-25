"""Requests of serving workloads from their pods' own counter readings (ADR-0009). Pure: no I/O.

A vLLM pod's request counter starts at 0 when its container starts. The engine reads it once per
scan; the requests in a window are the counter's rises between readings, counting a drop or a new
container restart as a reset. A window the readings don't fully cover gives a lower bound.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from ttv.models import CounterSample, Demand

PodKey = tuple[str, str]


@dataclass(frozen=True)
class PodWindow:
    """
    One pod's requests in one window.

    Attributes:
        requests (float): Requests counted after the window start.
        complete (bool): True when nothing in the window can be missing.
        covered_from (datetime): Earliest time the count is known to cover.
    """

    requests: float
    complete: bool
    covered_from: datetime


def pod_window(samples: Sequence[CounterSample], window_start: datetime) -> PodWindow | None:
    """
    Requests one pod served after `window_start`, from its readings.

    Args:
        samples (Sequence[CounterSample]): The pod's readings, oldest first.
        window_start (datetime): Start of the window.

    Returns:
        PodWindow | None: The count; None when no reading falls inside the window.
    """
    before = [s for s in samples if s.observed_at <= window_start]
    inside = [s for s in samples if s.observed_at > window_start]
    if not inside:
        return None
    seq = [before[-1], *inside] if before else inside
    requests = 0.0
    reset = False
    for a, b in pairwise(seq):
        if b.value < a.value or b.restarts > a.restarts:
            requests += b.value
            reset = True
        else:
            requests += b.value - a.value
    first = seq[0]
    started = first.pod_started_at
    if before:
        return PodWindow(requests, not reset, window_start)
    if first.restarts == 0 and started is not None and started >= window_start:
        return PodWindow(requests + first.value, not reset, started)
    if first.restarts == 0 and first.value == 0:
        return PodWindow(requests, not reset, window_start)
    return PodWindow(requests, False, first.observed_at)


def holder_demand(
    samples_by_pod: Mapping[PodKey, Sequence[CounterSample]],
    *,
    now: datetime,
    window: timedelta,
    recent: timedelta,
    read: Iterable[PodKey],
    unreadable: Iterable[PodKey],
) -> Demand | None:
    """
    Requests of one logical deployment over the demand and recent windows.

    Pods that are gone still count for the readings taken while they ran. A current pod
    whose `/metrics` could not be read makes both windows incomplete.

    Args:
        samples_by_pod (Mapping[PodKey, Sequence[CounterSample]]): Readings per pod, oldest
            first, including this scan's.
        now (datetime): Scan time.
        window (timedelta): Demand window (24 h by default).
        recent (timedelta): Recent window (1 h by default).
        read (Iterable[PodKey]): Current pods read this scan.
        unreadable (Iterable[PodKey]): Current pods whose read failed this scan.

    Returns:
        Demand | None: None when no pod has a reading inside the demand window and none
        failed (nothing is known about the deployment's requests).
    """
    missing = sorted(set(unreadable))
    counted: dict[timedelta, list[PodWindow]] = {window: [], recent: []}
    starts: list[datetime] = []
    for samples in samples_by_pod.values():
        for span, out in counted.items():
            result = pod_window(samples, now - span)
            if result is not None:
                out.append(result)
        if samples and samples[0].pod_started_at is not None:
            starts.append(samples[0].pod_started_at)
    if not counted[window] and not missing:
        return None

    def total(span: timedelta) -> tuple[float, bool, datetime]:
        """
        Sum the pods for one window.

        Args:
            span (timedelta): The window.

        Returns:
            tuple[float, bool, datetime]: Requests, completeness, start of coverage.
        """
        results = counted[span]
        start = now - span
        covered = max([start, *(r.covered_from for r in results if not r.complete)])
        if starts:
            covered = max(covered, min(starts))
        complete = bool(results) and all(r.complete for r in results) and not missing
        return sum(r.requests for r in results), complete, covered

    requests, complete, covered = total(window)
    recent_requests, recent_complete, _ = total(recent)
    return Demand(
        requests=requests,
        complete=complete,
        covered_hours=max((now - covered).total_seconds(), 0.0) / 3600,
        recent_requests=recent_requests,
        recent_complete=recent_complete,
        pods=tuple(f"{ns}/{pod}" for ns, pod in sorted(set(read))),
        unreadable=tuple(f"{ns}/{pod}" for ns, pod in missing),
    )


class CounterHistory:
    """In-memory readings per serving workload, kept for the demand window plus one hour."""

    def __init__(self, keep: timedelta) -> None:
        """
        Create an empty history.

        Args:
            keep (timedelta): How long to keep readings.
        """
        self.keep = keep
        self._by_holder: dict[tuple[str, str], dict[PodKey, list[CounterSample]]] = defaultdict(
            lambda: defaultdict(list)
        )

    def add(self, samples: Iterable[CounterSample]) -> None:
        """
        Add readings; a repeated `(pod, observed_at)` replaces the earlier one.

        Args:
            samples (Iterable[CounterSample]): New readings.
        """
        touched: set[tuple[tuple[str, str], PodKey]] = set()
        for s in samples:
            pod_key = (s.namespace, s.pod)
            series = self._by_holder[(s.cluster, s.holder_ref)][pod_key]
            series[:] = [x for x in series if x.observed_at != s.observed_at]
            series.append(s)
            touched.add(((s.cluster, s.holder_ref), pod_key))
        for holder, pod_key in touched:
            self._by_holder[holder][pod_key].sort(key=lambda x: x.observed_at)

    def prune(self, now: datetime) -> None:
        """
        Drop readings older than `keep`, keeping each pod's newest older one as a baseline.

        Args:
            now (datetime): Scan time.
        """
        cutoff = now - self.keep
        for holder in list(self._by_holder):
            pods = self._by_holder[holder]
            for pod_key in list(pods):
                series = pods[pod_key]
                old = [s for s in series if s.observed_at < cutoff]
                kept = [s for s in series if s.observed_at >= cutoff]
                if kept and old:
                    kept.insert(0, old[-1])
                if kept:
                    pods[pod_key] = kept
                else:
                    del pods[pod_key]
            if not pods:
                del self._by_holder[holder]

    def for_holder(self, cluster: str, holder_ref: str) -> Mapping[PodKey, Sequence[CounterSample]]:
        """
        Readings of one workload's pods.

        Args:
            cluster (str): Cluster.
            holder_ref (str): Workload.

        Returns:
            Mapping[PodKey, Sequence[CounterSample]]: Readings per pod, oldest first.
        """
        return {k: tuple(v) for k, v in self._by_holder.get((cluster, holder_ref), {}).items()}

    def holders(self, cluster: str) -> list[str]:
        """
        Workloads of a cluster that have readings.

        Args:
            cluster (str): Cluster.

        Returns:
            list[str]: Their `holder_ref`s.
        """
        return sorted(h for c, h in self._by_holder if c == cluster)
