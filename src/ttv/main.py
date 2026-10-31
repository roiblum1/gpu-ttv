"""Entry point: `python -m ttv.main` runs the scan loop and the read API (ADR-0001)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

import structlog
import uvicorn

from ttv.api import create_app
from ttv.config import ClusterConnection, Policy, Settings, load_clusters, load_policy
from ttv.engine import ClusterSources, Engine
from ttv.metrics import EngineMetrics
from ttv.pricing.catalog import canonical_gpu_model
from ttv.pricing.importer import prepare_pricing
from ttv.sources.kube import KubeSource
from ttv.sources.serverscan import DetailCache, InMemoryDetailCache, ServerScanSource
from ttv.sources.thanos import ThanosSource
from ttv.store import MIGRATIONS, PostgresStore

log = structlog.get_logger(__name__)


def _configure_logging() -> None:
    """Emit JSON log lines on stdout."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """
    Parse command-line flags; everything else comes from `TTV_*` settings.

    Args:
        argv (list[str] | None): Arguments, or None for sys.argv.

    Returns:
        argparse.Namespace: Parsed flags.
    """
    parser = argparse.ArgumentParser(description="GPU Time-to-Value engine")
    parser.add_argument(
        "--once", action="store_true", help="Run one scan, print its summary and exit."
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print effective settings (secrets masked) and policy, then exit.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """
    Wire the engine from settings and run it.

    Args:
        args (argparse.Namespace): Parsed flags.

    Returns:
        int: Process exit code.
    """
    settings = Settings()
    policy = load_policy(settings.policy_file)
    clusters = load_clusters(settings.clusters_file, policy)
    if args.print_config:
        print(
            json.dumps(
                {
                    "settings": settings.redacted(),
                    "policy": policy.model_dump(mode="json"),
                    "clusters": [c.model_dump(mode="json") for c in clusters],
                },
                indent=2,
            )
        )
        return 0

    store = await PostgresStore.connect(settings.database_url) if settings.database_url else None
    cache: DetailCache = store if store else InMemoryDetailCache()
    if store:
        applied = await store.migrate(MIGRATIONS)
        log.info("db.migrated", applied=applied)
        while not await store.try_acquire_leadership():
            log.info("engine.standby", reason="another engine holds the leader lock")
            await asyncio.sleep(settings.scan_interval_seconds)
        await store.sync_objectives(policy)
    now = datetime.now(UTC)
    pricing, imported = await prepare_pricing(
        store.conn if store else None, settings.pricing_dir, settings.pricing_snapshot, now
    )
    log.info(
        "pricing.ready",
        directory=str(settings.pricing_dir),
        imported=imported,
        snapshot=pricing.snapshot_id if pricing else None,
        published=pricing.publication_date.isoformat() if pricing else None,
        selected_by=pricing.source if pricing else None,
        priced_models=sorted(pricing.priced) if pricing else [],
    )
    if pricing is None:
        log.warning("pricing.none", effect="every GPU is reported unpriced; no value is shown")
    if store and not settings.dry_run:
        seen = await store.refresh_gpu_aliases(canonical_gpu_model, now)
        log.info("pricing.aliases_refreshed", raw_models=seen)

    source = ServerScanSource(
        settings.serverscan_url,
        token=settings.serverscan_token(),
        ca_bundle=str(settings.ca_bundle) if settings.ca_bundle else None,
        timeout_seconds=settings.http_timeout_seconds,
        detail_ttl=settings.detail_ttl,
        detail_max_per_scan=settings.detail_max_per_scan,
        detail_concurrency=settings.detail_concurrency,
        cache=cache,
    )
    cluster_sources = _cluster_sources(settings, policy, clusters)
    engine = Engine(
        policy=policy,
        source=source,
        store=store,
        metrics=EngineMetrics(),
        dry_run=settings.dry_run,
        unknown_grace_scans=settings.unknown_grace_scans,
        tombstone_after=settings.tombstone_after,
        clusters=cluster_sources,
        cluster_timeout=settings.cluster_timeout_seconds,
        pricing=pricing,
    )
    log.info(
        "engine.start",
        dry_run=engine.dry_run,
        policy_version=policy.policy_version,
        database=bool(store),
        clusters=[c.conn.name for c in cluster_sources],
        unobserved_targets=sorted(policy.target_clusters - {c.name for c in clusters}),
    )
    try:
        if args.once:
            report = await engine.run_once()
            print(
                json.dumps(
                    {
                        "status": report.status,
                        "plan": report.plan_counts,
                        "coverage": report.coverage,
                        "errors": report.errors,
                    }
                )
            )
            return 0 if report.status != "failed" else 1
        interval = settings.scan_interval_seconds
        app = create_app(engine, timedelta(seconds=interval))
        config = uvicorn.Config(
            app, host=settings.api_host, port=settings.api_port, log_config=None
        )
        server = uvicorn.Server(config)
        await asyncio.gather(server.serve(), _loop(engine, interval))
        return 0
    finally:
        await source.aclose()
        for c in cluster_sources:
            await c.kube.aclose()
            await c.thanos.aclose()
        if store:
            await store.close()


def _cluster_sources(
    settings: Settings, policy: Policy, clusters: list[ClusterConnection]
) -> list[ClusterSources]:
    """
    Build the kube and Thanos sources of every configured target cluster.

    Args:
        settings (Settings): Runtime settings (deadlines, concurrency).
        policy (Policy): Policy (serving counters and windows to query).
        clusters (list[ClusterConnection]): Configured clusters.

    Returns:
        list[ClusterSources]: One entry per cluster.
    """
    return [
        ClusterSources(
            conn=c,
            kube=KubeSource(
                c,
                timeout_seconds=settings.cluster_timeout_seconds,
                owner_concurrency=settings.owner_lookup_concurrency,
            ),
            thanos=ThanosSource(
                c,
                counters=policy.counters(),
                demand_window=policy.serving.demand_window,
                activity_window=policy.allocated_idle.window,
                timeout_seconds=settings.cluster_timeout_seconds,
            ),
        )
        for c in clusters
    ]


async def _loop(engine: Engine, interval: int) -> None:
    """
    Scan forever; one failed scan never stops the loop.

    Args:
        engine (Engine): The engine.
        interval (int): Seconds between scan starts.
    """
    while True:
        loop_start = asyncio.get_running_loop().time()
        try:
            await engine.run_once()
        except Exception:
            log.exception("scan.crashed")
        elapsed = asyncio.get_running_loop().time() - loop_start
        await asyncio.sleep(max(1.0, interval - elapsed))


def main(argv: list[str] | None = None) -> int:
    """
    Run the engine.

    Args:
        argv (list[str] | None): Arguments, or None for sys.argv.

    Returns:
        int: Exit code.
    """
    _configure_logging()
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
