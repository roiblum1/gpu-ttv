"""Read API: health, readiness, per-asset explanation and metrics (AGENT.md §20.1, §20.6)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from ttv.engine import Engine


def create_app(engine: Engine, scan_interval: timedelta) -> FastAPI:
    """
    Build the API around a running engine.

    Args:
        engine (Engine): The engine whose last scan is served.
        scan_interval (timedelta): Expected time between scans, for readiness.

    Returns:
        FastAPI: The application.
    """
    app = FastAPI(title="ttv-engine", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/metrics", make_asgi_app(registry=engine.metrics.registry))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """
        Liveness: the process is serving.

        Returns:
            dict[str, str]: Always ok.
        """
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """
        Readiness: a scan finished within three intervals.

        Returns:
            JSONResponse: 200 when ready, 503 otherwise, with the last scan's summary.
        """
        report = engine.last_report
        if report is None:
            return JSONResponse({"ready": False, "reason": "no scan yet"}, status_code=503)
        age = datetime.now(UTC) - report.finished_at
        ready = report.status != "failed" and age <= 3 * scan_interval
        body = {
            "ready": ready,
            "last_scan": report.scan_id,
            "status": report.status,
            "age_seconds": int(age.total_seconds()),
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/explain/{asset_id:path}")
    async def explain(asset_id: str) -> dict[str, Any]:
        """
        Why an asset is classified as it is: rules evaluated, match, evidence, recent intervals.

        Args:
            asset_id (str): Asset id, e.g. `cisco:cis0001830`.

        Returns:
            dict[str, Any]: Explanation.

        Raises:
            HTTPException: 404 when the last scan did not classify this asset.
        """
        report = engine.last_report
        decision = report.decisions.get(asset_id) if report else None
        if report is None or decision is None:
            raise HTTPException(
                status_code=404, detail=f"{asset_id} was not classified in the last scan"
            )
        server = report.servers.get(asset_id)
        intervals = await engine.store.intervals_for(asset_id) if engine.store else []
        return {
            "asset_id": asset_id,
            "scan_id": report.scan_id,
            "scanned_at": report.started_at.isoformat(),
            "policy_version": decision.policy_version,
            "dry_run": engine.dry_run,
            "expected_gpus": decision.expected_gpus,
            "rules_evaluated": list(decision.trace),
            "buckets": [_bucket(b) for b in decision.buckets],
            "stage_clocks": {k: asdict(v) for k, v in decision.stage_clocks.items()},
            "milestones": [asdict(m) for m in decision.milestones],
            "serverscan": asdict(server) if server else None,
            "recent_intervals": intervals,
        }

    return app


def _bucket(bucket: Any) -> dict[str, Any]:
    """
    Serialise a bucket for JSON.

    Args:
        bucket (Any): A Bucket.

    Returns:
        dict[str, Any]: Plain fields.
    """
    data = asdict(bucket)
    data["evidence"] = dict(bucket.evidence)
    return data
