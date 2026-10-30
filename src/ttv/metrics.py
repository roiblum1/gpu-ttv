"""Aggregated Prometheus gauges (AGENT.md §20.7); per-asset detail stays in Postgres."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

from prometheus_client import CollectorRegistry, Gauge
from prometheus_client import Counter as PromCounter

from ttv.models import Category, Decision, ServerScanServer


class EngineMetrics:
    """The engine's gauges, on their own registry so tests and multiple engines stay isolated."""

    def __init__(self) -> None:
        """Create the registry and every metric."""
        self.registry = CollectorRegistry()
        self.gpus = Gauge(
            "ttv_gpus",
            "Physical GPUs by current classification.",
            ["category", "reason_code", "stage_owner", "site", "cluster", "gpu_model"],
            registry=self.registry,
        )
        self.unknown_gpus = Gauge(
            "ttv_unknown_gpus",
            "GPUs whose state is unknown, by reason.",
            ["reason_code"],
            registry=self.registry,
        )
        self.last_success = Gauge(
            "ttv_scan_last_success_timestamp_seconds",
            "End of the last complete scan.",
            ["kind"],
            registry=self.registry,
        )
        self.duration = Gauge(
            "ttv_scan_duration_seconds",
            "Duration of the last scan.",
            ["kind"],
            registry=self.registry,
        )
        self.source_errors = PromCounter(
            "ttv_source_errors",
            "Failed or partial source fetches.",
            ["source", "target"],
            registry=self.registry,
        )
        self.conflicts = Gauge(
            "ttv_identity_conflicts",
            "Open identity conflicts seen in the last scan.",
            registry=self.registry,
        )
        self.unread = Gauge(
            "ttv_gpu_inventory_unread_servers",
            "Servers whose GPU inventory is unread (not zero GPUs).",
            registry=self.registry,
        )
        self.pending = Gauge(
            "ttv_serverscan_pending_details",
            "server-scan records still waiting for their detail fetch.",
            registry=self.registry,
        )
        self.dry_run = Gauge(
            "ttv_dry_run",
            "1 while the engine writes nothing to the ledger.",
            registry=self.registry,
        )

    def observe_decisions(
        self, decisions: Mapping[str, Decision], servers: Mapping[str, ServerScanServer]
    ) -> None:
        """
        Replace the classification gauges with this scan's totals.

        Args:
            decisions (Mapping[str, Decision]): Decisions by asset id.
            servers (Mapping[str, ServerScanServer]): The server-scan record behind each asset id.
        """
        totals: Counter[tuple[str, str, str, str, str, str]] = Counter()
        unknown: Counter[str] = Counter()
        for asset_id, decision in decisions.items():
            s = servers.get(asset_id)
            site = (s.site_id if s else None) or ""
            cluster = (s.cluster_name if s else None) or ""
            for b in decision.buckets:
                totals[
                    (
                        str(b.category),
                        str(b.reason),
                        b.stage_owner or "",
                        site,
                        cluster,
                        b.gpu_model or "",
                    )
                ] += b.gpus
                if b.category is Category.UNKNOWN:
                    unknown[str(b.reason)] += b.gpus
        self.gpus.clear()
        for labels, value in totals.items():
            self.gpus.labels(*labels).set(value)
        self.unknown_gpus.clear()
        for reason, value in unknown.items():
            self.unknown_gpus.labels(reason).set(value)

    def observe_scan(
        self,
        *,
        kind: str,
        complete: bool,
        finished_at: float,
        duration: float,
        conflicts: int,
        unread: int,
        pending: int,
        dry_run: bool,
        failed_sources: Iterable[tuple[str, str]] = (),
    ) -> None:
        """
        Record scan-level health.

        Args:
            kind (str): Scan kind.
            complete (bool): Whether every source fetch was complete.
            finished_at (float): End time, Unix seconds.
            duration (float): Duration in seconds.
            conflicts (int): Identity conflicts seen.
            unread (int): Servers with unread GPU inventory.
            pending (int): Records waiting for detail.
            dry_run (bool): Dry-run mode.
            failed_sources (Iterable[tuple[str, str]]): `(source, target)` pairs that failed.
        """
        if complete:
            self.last_success.labels(kind).set(finished_at)
        self.duration.labels(kind).set(duration)
        for source, target in failed_sources:
            self.source_errors.labels(source, target).inc()
        self.conflicts.set(conflicts)
        self.unread.set(unread)
        self.pending.set(pending)
        self.dry_run.set(1 if dry_run else 0)
