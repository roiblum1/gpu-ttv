"""
Run every query of the six managers' pages against a migrated, seeded Postgres, as Grafana sends it.

Needs `TTV_TEST_DSN` (a throwaway database: the schema is dropped); skipped otherwise. Macros and
variables are substituted by `tests/grafana_sql.py`. The fixture is synthetic: rows for every team,
in repair, no data, a stage that began before observation, closed intervals inside the period, a
renamed team, a reason from before ADR-0009, and `workloads_now` rows with complete and incomplete
windows. Every number asserted below is worked out in the fixture's comments.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier, Placeholder
from psycopg.types.json import Jsonb

from conftest import DSN, MIGRATIONS
from grafana_sql import (
    PanelQuery,
    QueryContext,
    dashboard_variables,
    leftover_placeholders,
    panel_queries,
    render,
    time_range,
    variable_queries,
)
from test_dashboards_static import RAW_CODE

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = {
    p.stem: json.loads(p.read_text()) for p in sorted(ROOT.glob("grafana/dashboards/*.json"))
}
QUERIES = [q for d in DASHBOARDS.values() for q in panel_queries(d)]
VARIABLE_QUERIES = [q for d in DASHBOARDS.values() for q in variable_queries(d)]

C1, C2 = "ocp4-prod-gpu-site1", "ocp4-prod-gpu-site2"
# Raw model -> canonical name and synthetic AWS rate in the selected snapshot (None = unpriced).
H100, H200, B200, B300 = (
    "NVIDIA H100 80GB HBM3", "NVIDIA H200 141GB HBM3e", "NVIDIA B200", "NVIDIA B300 SXM6 AC",
)  # fmt: skip
L4, A16, X9 = "NVIDIA L4", "NVIDIA A16", "NVIDIA RTX X9"  # X9: no catalogue name at all
CATALOGUE: dict[str, tuple[str, str | None]] = {
    H100: ("NVIDIA H100 80GB", "10.00"), H200: ("NVIDIA H200 141GB", "10.00"),
    B200: ("NVIDIA B200 180GB", "10.00"), B300: ("NVIDIA B300 288GB", "10.00"),
    L4: ("NVIDIA L4 24GB", "1.00"), A16: ("NVIDIA A16 16GB", None),
}  # fmt: skip
SELECTED, NEWER = "aws-synthetic-selected", "aws-synthetic-newer-not-selected"
H, D, M = timedelta(hours=1), timedelta(days=1), timedelta(minutes=1)
OLD_NAME = "Empty-GPU team (old name)"
TEAM_PAGES = ("ttv-samurai", "ttv-redbull", "ttv-popai", "ttv-apply", "ttv-ml")

Conn = psycopg.Connection[dict[str, Any]]


@dataclass(frozen=True)
class Seed:
    """
    Times the synthetic fixture was built around.

    Args:
        now (datetime): Seed time; every interval is placed relative to it.
        first_scan (datetime): Start of the first ok or partial engine scan (200 h before now).
        price_list (str): The selected snapshot as the money labels name it.
    """

    now: datetime
    first_scan: datetime
    price_list: str


def _asset(
    conn: Conn, asset_id: str, name: str, gpus: int, model: str | None, now: datetime
) -> None:
    """
    Insert one asset.

    Args:
        conn (Conn): Connection.
        asset_id (str): `<vendor>:<serial>`.
        name (str): Server name.
        gpus (int): GPU count.
        model (str | None): Raw GPU model as server-scan reports it (None = not reported).
        now (datetime): Seed time.
    """
    vendor, serial = asset_id.split(":")
    conn.execute(
        "INSERT INTO assets (asset_id, vendor, serial, name, model, gpu_model, gpu_count, source,"
        " first_observed_at, last_observed_at) VALUES (%s, %s, %s, %s, 'Synthetic GPU server',"
        " %s, %s, 'serverscan', %s, %s)",
        (asset_id, vendor, serial, name, model, gpus, now - 200 * H, now - 9 * M),
    )


def _iv(
    conn: Conn, asset_id: str, category: str, reason: str, gpus: int, start: datetime,
    end: datetime | None = None, *, stage: str | None = None, owner: str | None = None,
    entered: datetime | None = None, lb: bool = False, cluster: str | None = None,
    holder: str | None = None, evidence: dict[str, Any] | None = None,
) -> None:  # fmt: skip
    """
    Insert one ledger interval.

    Args:
        conn (Conn): Connection.
        asset_id (str): Asset id.
        category (str): PRODUCTIVE, IDLE, UNKNOWN or NOT_CHARGED.
        reason (str): Reason code.
        gpus (int): Physical GPUs.
        start (datetime): Interval start.
        end (datetime | None): Interval end, None while open.
        stage (str | None): Stage (IDLE only).
        owner (str | None): Stage owner (IDLE only).
        entered (datetime | None): Stage entry time.
        lb (bool): Stage entry is a lower bound (began before observation).
        cluster (str | None): Matched cluster.
        holder (str | None): Workload holding the GPUs.
        evidence (dict[str, Any] | None): Evidence JSON.
    """
    conn.execute(
        "INSERT INTO capacity_intervals (asset_id, category, reason_code, stage, stage_owner,"
        " stage_entered_at, stage_entered_lower_bound, cluster, holder_ref, gpu_model, gpus,"
        " started_at, ended_at, evidence, policy_version)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'fixture-1')",
        (asset_id, category, reason, stage, owner, entered, lb, cluster, holder, None, gpus,
         start, end, Jsonb(evidence or {})),
    )  # fmt: skip


def _workload(conn: Conn, now: datetime, cluster: str, holder: str, reason: str, category: str,
              kind: str, gpus: int, **extra: Any) -> None:  # fmt: skip
    """
    Insert one `workloads_now` row as the engine's last writing scan would.

    Args:
        conn (Conn): Connection.
        now (datetime): Seed time.
        cluster (str): Cluster.
        holder (str): `namespace/Kind/name`.
        reason (str): Reason code.
        category (str): Category.
        kind (str): serving, ml or unresolved.
        gpus (int): GPUs held.
        **extra (Any): Any other column (requests, windows, busy peak, stage, runai).
    """
    row = {"cluster": cluster, "holder_ref": holder, "reason_code": reason, "category": category,
           "kind": kind, "gpus": gpus, "updated_at": now - 9 * M, "since": now - D,
           **extra}  # fmt: skip
    insert = SQL("INSERT INTO workloads_now ({}) VALUES ({})").format(
        SQL(", ").join(map(Identifier, row)), SQL(", ").join(map(Placeholder, row))
    )
    conn.execute(insert, row)


def _seed(conn: Conn) -> Seed:
    """
    Insert the synthetic fixture.

    Open GPUs: in use 28, wasted 48 (Samurai 12, Redbull 16, POPAI 8, Apply 8, Machine Learning
    4), in repair 8, no data 20, not measured 8, excluded 2: 114 in all. Wasted GPU-hours over
    the default 7 days: Samurai 420, Redbull 984, POPAI 96, Apply 212, Machine Learning 96. At
    the synthetic rates ($10 per GPU-hour, $1 for the L4, A16 unpriced): $4,200, $9,624, no
    price, $2,120 and $960. Each interval carries its server's GPU model.

    Args:
        conn (Conn): Connection to a freshly migrated database.

    Returns:
        Seed: The times the fixture uses.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    assets = (
        ("cisco:fxem0001", "gpu-empty-01", 8, H200), ("cisco:fxem0002", "gpu-empty-02", 8, H100),
        ("dell:fxrb0003", "gpu-new-03", 8, B300), ("dell:fxrb0004", "gpu-hw-04", 8, H100),
        ("cisco:fxpp0005", "gpu-sw-05", 8, A16), ("cisco:fxem0006", "gpu-part-06", 8, H100),
        ("dell:fxrb0007", "gpu-done-07", 8, L4), ("nvidia:fxsv0008", "gpu-serve-08", 8, B200),
        ("nvidia:fxsv0009", "gpu-serve-09", 8, H100), ("cisco:fxml0010", "gpu-ml-10", 8, H100),
        ("cisco:fxml0011", "gpu-ml-11", 8, H200), ("dell:fxmt0012", "gpu-repair-12", 8, B300),
        ("dell:fxst0013", "gpu-stale-13", 4, X9), ("cisco:fxlb0014", "lab-14", 2, None),
        ("dell:fxot0015", "gpu-other-15", 8, H100), ("dell:fxpw0016", "gpu-power-16", 4, H100),
    )  # fmt: skip
    for asset_id, name, gpus, model in assets:
        _asset(conn, asset_id, name, gpus, model, now)
    a = {name: asset_id for asset_id, name, _, _ in assets}
    empty: dict[str, Any] = {"stage": "EMPTY", "owner": "Samurai"}
    red: dict[str, Any] = {"stage": "PROVISIONING", "owner": "Redbull"}
    apply_: dict[str, Any] = {"stage": "SERVING", "owner": "Apply"}
    ml: dict[str, Any] = {"stage": "MACHINE_LEARNING", "owner": "Machine Learning"}

    # Samurai: 8 empty for 48 h (384) + 4 empty for 6 h (24) + §17's 6 of 8 idle for 2 h (12).
    _iv(conn, a["gpu-empty-01"], "IDLE", "NO_WORKLOAD", 8, now - 48 * H, entered=now - 48 * H,
        cluster=C1, **empty)  # fmt: skip
    _iv(conn, a["gpu-empty-02"], "IDLE", "NO_WORKLOAD", 4, now - 6 * H, entered=now - 6 * H,
        cluster=C2, **empty)  # fmt: skip
    _iv(conn, a["gpu-empty-02"], "PRODUCTIVE", "RUNAI_WORKLOAD", 4, now - 6 * H, cluster=C2,
        holder="team-a/TrainingWorkload/llama-ft")  # fmt: skip
    _iv(conn, a["gpu-part-06"], "IDLE", "NO_WORKLOAD", 6, now - 48 * H, now - 46 * H,
        stage="EMPTY", owner=OLD_NAME, entered=now - 48 * H, cluster=C2)  # fmt: skip
    for gpus, start, end in ((2, now - 48 * H, now - 46 * H), (8, now - 46 * H, None)):
        _iv(conn, a["gpu-part-06"], "PRODUCTIVE", "RUNAI_WORKLOAD", gpus, start, end, cluster=C2,
            holder="team-b/TrainingWorkload/vision-train")  # fmt: skip
    # Redbull: 8 not in a cluster for 72 h (576; waiting ≥ 10 d), 8 hardware for 24 h (192),
    # §17's 8 idle for 3 h (24), and 8 in maintenance counted as waste before ADR-0009 (192).
    _iv(conn, a["gpu-new-03"], "IDLE", "NOT_IN_CLUSTER", 8, now - 72 * H, entered=now - 240 * H,
        lb=True, **red)  # fmt: skip
    _iv(conn, a["gpu-hw-04"], "IDLE", "HARDWARE_CRITICAL", 8, now - 24 * H, entered=now - 24 * H,
        evidence={"cluster_name": C1}, **red)  # fmt: skip
    _iv(conn, a["gpu-done-07"], "IDLE", "NODE_NOT_READY", 8, now - 72 * H, now - 69 * H,
        entered=now - 72 * H, cluster=C2, **red)  # fmt: skip
    _iv(conn, a["gpu-done-07"], "PRODUCTIVE", "ML_WORKLOAD", 8, now - 69 * H, cluster=C2,
        holder="batch/Deployment/batch-infer")  # fmt: skip
    _iv(conn, a["gpu-repair-12"], "IDLE", "MAINTENANCE", 8, now - 120 * H, now - 96 * H,
        entered=now - 120 * H, **red)  # fmt: skip
    _iv(conn, a["gpu-repair-12"], "NOT_CHARGED", "MAINTENANCE", 8, now - 96 * H)
    # POPAI: 8 GPUs whose GPU software is not ready, for 12 h (96).
    _iv(conn, a["gpu-sw-05"], "IDLE", "GPU_PLATFORM_NOT_READY", 8, now - 12 * H,
        stage="GPU_PLATFORM", owner="POPAI", entered=now - 12 * H, cluster=C1)  # fmt: skip
    # Apply: two models wasting 2 GPUs each for 48 h (192), one not starting on 4 for 5 h (20).
    serving: tuple[tuple[str, str, int, datetime, str, dict[str, Any]], ...] = (
        ("PRODUCTIVE", "SERVING_IN_USE", 2, now - 48 * H, "llm/LLMInferenceService/chat", {}),
        ("PRODUCTIVE", "SERVING_IN_USE", 2, now - 6 * H, "llm/InferenceService/summarise", {}),
        ("IDLE", "NO_REAL_DEMAND", 2, now - 48 * H, "llm/LLMInferenceService/translate", apply_),
        ("IDLE", "LOW_USE", 2, now - 48 * H, "llm/Deployment/legal", apply_),
    )
    for category, reason, gpus, start, holder, stage in serving:
        entered = start if stage else None
        _iv(conn, a["gpu-serve-08"], category, reason, gpus, start, entered=entered, cluster=C1,
            holder=holder, **stage)  # fmt: skip
    _iv(conn, a["gpu-serve-09"], "IDLE", "MODEL_NOT_READY", 4, now - 5 * H, entered=now - 5 * H,
        cluster=C2, holder="vision-team/LLMInferenceService/vision", **apply_)  # fmt: skip
    _iv(conn, a["gpu-serve-09"], "UNKNOWN", "TRAFFIC_UNOBSERVED", 4, now - H, cluster=C2,
        holder="search/InferenceService/search")  # fmt: skip
    # Machine Learning: 2 barely busy and 2 not starting, both for 24 h (96).
    _iv(conn, a["gpu-ml-10"], "PRODUCTIVE", "RUNAI_WORKLOAD", 4, now - 24 * H, cluster=C1,
        holder="research/PyTorchJob/pretrain")  # fmt: skip
    for reason, holder in (("LOW_GPU_UTILIZATION", "research/Notebook/notebook-ana"),
                           ("WORKLOAD_NOT_READY", "research/Job/eval")):  # fmt: skip
        _iv(conn, a["gpu-ml-10"], "IDLE", reason, 2, now - 24 * H, entered=now - 24 * H,
            cluster=C1, holder=holder, **ml)  # fmt: skip
    _iv(conn, a["gpu-ml-11"], "UNKNOWN", "ACTIVITY_UNOBSERVED", 8, now - 2 * H, cluster=C2,
        holder="research/PyTorchJob/sweep")  # fmt: skip
    # No data (4 + 8 above, 4 here), excluded, and a cluster outside the target list.
    _iv(conn, a["gpu-stale-13"], "UNKNOWN", "SOURCE_STALE", 4, now - 6 * H)
    _iv(conn, a["lab-14"], "NOT_CHARGED", "EXCLUDED", 2, now - 200 * H)
    _iv(conn, a["gpu-other-15"], "NOT_CHARGED", "OUTSIDE_TARGET_CLUSTERS", 8, now - 200 * H,
        evidence={"cluster_name": "ocp4-dev"})  # fmt: skip
    # 4 GPUs no pod holds, drawing power for 3 h: no data (ADR-0009 row 22b), not Samurai's waste.
    _iv(conn, a["gpu-power-16"], "UNKNOWN", "POWER_WITHOUT_POD", 4, now - 3 * H, cluster=C1)

    conn.execute(
        "UPDATE capacity_intervals i SET gpu_model = a.gpu_model FROM assets a"
        " WHERE a.asset_id = i.asset_id"
    )
    _seed_workloads(conn, now)
    _seed_pricing(conn, now)
    for started, status in ((now - 200 * H, "partial"), (now - 10 * M, "ok"),
                            (now - 2 * M, "dry_run")):  # fmt: skip
        conn.execute(
            "INSERT INTO scan_runs (kind, started_at, finished_at, status)"
            " VALUES ('engine', %s, %s, %s)",
            (started, started + M, status),
        )
    published = (now - 30 * D).strftime("%d %b %Y").lstrip("0")
    return Seed(now=now, first_scan=now - 200 * H,
                price_list=f"AWS price list of {published} (us-east-1)")  # fmt: skip


def _seed_pricing(conn: Conn, now: datetime) -> None:
    """
    Insert two synthetic AWS snapshots: the selected one ($10 and $1) and a newer, unselected one.

    The newer one prices only the H100, at $20, so a query that ignored pricing_selection would
    show different numbers.

    Args:
        conn (Conn): Connection.
        now (datetime): Seed time.
    """
    for snapshot, published in ((SELECTED, now - 30 * D), (NEWER, now - 2 * D)):
        conn.execute(
            "INSERT INTO pricing_snapshots (snapshot_id, provider, region, purchase_option,"
            " captured_at, effective_at, checksum, parser_version, currency, publication_date)"
            " VALUES (%s, 'aws', 'us-east-1', 'OnDemand', %s, %s, %s, 'fixture', 'USD', %s)",
            (snapshot, published + H, published, "0" * 64, published),
        )
    rates = [(SELECTED, canonical, rate) for canonical, rate in CATALOGUE.values()]
    rates.append((NEWER, CATALOGUE[H100][0], "20.00"))
    for snapshot, canonical, rate in rates:
        conn.execute(
            "INSERT INTO pricing_rates (snapshot_id, gpu_model, per_gpu_hour_usd, mapping, reason)"
            " VALUES (%s, %s, %s, %s, 'synthetic')",
            (snapshot, canonical, rate, "exact" if rate else "unpriced"),
        )
    for raw, (canonical, _rate) in CATALOGUE.items():
        conn.execute(
            "INSERT INTO gpu_model_aliases (raw_model, canonical, catalog_version, updated_at)"
            " VALUES (%s, %s, 'fixture', %s)",
            (raw, canonical, now - D),
        )
    conn.execute(
        "INSERT INTO pricing_selection (snapshot_id, selected_at, source)"
        " VALUES (%s, %s, 'TTV_PRICING_SNAPSHOT')",
        (SELECTED, now - D),
    )


def _seed_workloads(conn: Conn, now: datetime) -> None:
    """
    Insert `workloads_now` rows matching the ledger's open intervals, plus one unresolved row.

    Args:
        conn (Conn): Connection.
        now (datetime): Seed time.
    """
    w = partial(_workload, conn, now)
    apply_ = {"stage": "SERVING", "stage_owner": "Apply"}
    ml = {"stage": "MACHINE_LEARNING", "stage_owner": "Machine Learning"}
    full = {"window_hours": 24.0, "window_complete": True, "recent_hours": 1.0,
            "recent_complete": True}  # fmt: skip
    w(
        C1,
        "llm/LLMInferenceService/chat",
        "SERVING_IN_USE",
        "PRODUCTIVE",
        "serving",
        2,
        requests_window=2400.0,
        requests_recent=120.0,
        requests_per_gpu_hour=50.0,
        **full,
    )
    w(
        C1,
        "llm/InferenceService/summarise",
        "SERVING_IN_USE",
        "PRODUCTIVE",
        "serving",
        2,
        requests_window=24.0,
        requests_recent=0.0,
        requests_per_gpu_hour=2.0,
        **{**full, "window_hours": 6.0},
    )  # a model started 6 h ago: its window is its age
    w(
        C1,
        "llm/LLMInferenceService/translate",
        "NO_REAL_DEMAND",
        "IDLE",
        "serving",
        2,
        requests_window=0.0,
        requests_recent=0.0,
        requests_per_gpu_hour=0.0,
        **full,
        **apply_,
    )
    w(
        C1,
        "llm/Deployment/legal",
        "LOW_USE",
        "IDLE",
        "serving",
        2,
        requests_window=12.0,
        requests_recent=1.0,
        requests_per_gpu_hour=0.25,
        window_hours=24.0,
        window_complete=False,
        recent_hours=1.0,
        recent_complete=False,
        **apply_,
    )
    w(
        C2,
        "vision-team/LLMInferenceService/vision",
        "MODEL_NOT_READY",
        "IDLE",
        "serving",
        4,
        **apply_,
    )
    w(C2, "search/InferenceService/search", "TRAFFIC_UNOBSERVED", "UNKNOWN", "serving", 4)
    busy = {"busy_window_hours": 6.0}
    w(
        C1,
        "research/PyTorchJob/pretrain",
        "RUNAI_WORKLOAD",
        "PRODUCTIVE",
        "ml",
        4,
        runai=True,
        gpu_busy_peak=0.92,
        **busy,
    )
    w(
        C1,
        "research/Notebook/notebook-ana",
        "LOW_GPU_UTILIZATION",
        "IDLE",
        "ml",
        2,
        runai=False,
        gpu_busy_peak=0.03,
        **busy,
        **ml,
    )
    w(C1, "research/Job/eval", "WORKLOAD_NOT_READY", "IDLE", "ml", 2, runai=True, **busy, **ml)
    w(C2, "research/PyTorchJob/sweep", "ACTIVITY_UNOBSERVED", "UNKNOWN", "ml", 8, runai=True)
    w(
        C2,
        "batch/Deployment/batch-infer",
        "ML_WORKLOAD",
        "PRODUCTIVE",
        "ml",
        8,
        runai=False,
        gpu_busy_peak=0.6,
        **busy,
    )
    w(
        C2,
        "team-a/TrainingWorkload/llama-ft",
        "RUNAI_WORKLOAD",
        "PRODUCTIVE",
        "ml",
        4,
        runai=True,
        gpu_busy_peak=0.75,
        **busy,
    )
    w(
        C2,
        "team-b/TrainingWorkload/vision-train",
        "RUNAI_WORKLOAD",
        "PRODUCTIVE",
        "ml",
        8,
        runai=True,
        gpu_busy_peak=0.81,
        **busy,
    )
    w(C1, "misc/Pod/orphan", "HOLDER_UNRESOLVED", "UNKNOWN", "unresolved", 1)
    # GPU power as a share of rated power: 24 h average for served models, 6 h peak for jobs.
    for holder, column, share in (
        ("llm/LLMInferenceService/chat", "gpu_power_avg", 0.62),
        ("llm/InferenceService/summarise", "gpu_power_avg", 0.48),
        ("llm/LLMInferenceService/translate", "gpu_power_avg", 0.10),
        ("llm/Deployment/legal", "gpu_power_avg", 0.18),  # requests or not, the GPUs sit idle
        ("research/PyTorchJob/pretrain", "gpu_power_peak", 0.85),
        ("research/Notebook/notebook-ana", "gpu_power_peak", 0.08),
        ("batch/Deployment/batch-infer", "gpu_power_peak", 0.55),
        ("team-a/TrainingWorkload/llama-ft", "gpu_power_peak", 0.70),
        ("team-b/TrainingWorkload/vision-train", "gpu_power_peak", 0.661),
    ):
        conn.execute(
            SQL("UPDATE workloads_now SET {} = %s WHERE holder_ref = %s").format(
                Identifier(column)
            ),
            (share, holder),
        )
    # GPU models as the engine records them: the distinct raw models of the workload's buckets.
    for prefix, raws in (("research/PyTorchJob/sweep", [H200, X9]), ("llm/", [B200]),
                         ("batch/", [L4]), ("vision-team/", [H100]), ("search/", [H100]),
                         ("research/", [H100]), ("team-", [H100])):  # fmt: skip
        conn.execute(
            "UPDATE workloads_now SET gpu_models = %s"
            " WHERE holder_ref LIKE %s AND gpu_models = '{}'",
            (raws, prefix + "%"),
        )


@pytest.fixture(scope="module")
def db() -> Iterator[tuple[Conn, Seed]]:
    """
    A migrated database holding the synthetic fixture, shared by this module's tests.

    Yields:
        tuple[Conn, Seed]: An open connection (each query runs in its own transaction) and times.
    """
    assert DSN
    with psycopg.Connection[dict[str, Any]].connect(
        DSN, autocommit=True, row_factory=dict_row
    ) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(cast(LiteralString, path.read_text()))  # repo-owned SQL
        with conn.transaction():
            seed = _seed(conn)
        conn.execute("SET statement_timeout = '20s'")
        yield conn, seed


def _first_column(conn: Conn, sql: str) -> list[str]:
    """
    Run a variable query and return its first column as text.

    Args:
        conn (Conn): Connection.
        sql (str): Rendered SQL.

    Returns:
        list[str]: Option values in result order.
    """
    return [str(next(iter(r.values()))) for r in conn.execute(cast(LiteralString, sql))]


def _context(query: PanelQuery, time_from: datetime, time_to: datetime) -> QueryContext:
    """
    Build a context for one panel query (its own min interval and max data points).

    Args:
        query (PanelQuery): The query.
        time_from (datetime): Range start.
        time_to (datetime): Range end.

    Returns:
        QueryContext: Context without variables.
    """
    return QueryContext(
        time_from, time_to, max_data_points=query.max_data_points or 1000,
        min_interval=query.min_interval,
    )  # fmt: skip


def _with_variables(
    conn: Conn, uid: str, ctx: QueryContext, values: Mapping[str, str | Sequence[str]]
) -> QueryContext:
    """
    Add a dashboard's variables to a context (All unless given), resolved on the fixture.

    Args:
        conn (Conn): Connection.
        uid (str): Dashboard UID.
        ctx (QueryContext): Time range and interval.
        values (Mapping[str, str | Sequence[str]]): Explicit values by variable name.

    Returns:
        QueryContext: The context with variables.
    """
    resolver = partial(_first_column, conn)
    variables = dashboard_variables(DASHBOARDS[uid], values, ctx=ctx, resolve=resolver)
    return QueryContext(
        ctx.time_from, ctx.time_to, variables, ctx.interval, ctx.max_data_points, ctx.min_interval
    )


def _execute(conn: Conn, query: PanelQuery, ctx: QueryContext) -> list[dict[str, Any]]:
    """
    Render one query, check nothing is left unsubstituted, run it read-only and return rows.

    Args:
        conn (Conn): Connection.
        query (PanelQuery): The query.
        ctx (QueryContext): Time range, interval and variables.

    Returns:
        list[dict[str, Any]]: Result rows.
    """
    sql = render(query.sql, ctx)
    leftover = leftover_placeholders(sql)
    assert not leftover, f"{query.label}: unsubstituted {leftover}"
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        try:
            cur = conn.execute(cast(LiteralString, sql))
        except psycopg.Error as exc:
            pytest.fail(f"{query.label} ({query.title}) failed: {exc}\n--- SQL ---\n{sql}")
        rows = cur.fetchall()
        columns = [c.name for c in cur.description or []]
    if query.format == "time_series":
        assert columns == ["time", "metric", "value"], f"{query.label}: long time series"
    return rows


def _panel(
    db: tuple[Conn, Seed],
    uid: str,
    panel_id: int,
    window: tuple[datetime, datetime] | None = None,
    **values: str | Sequence[str],
) -> list[dict[str, Any]]:
    """
    Run one panel's query as Grafana would and return its rows.

    Args:
        db (tuple[Conn, Seed]): The seeded database.
        uid (str): Dashboard UID.
        panel_id (int): Panel id.
        window (tuple[datetime, datetime] | None): Time range; None = the dashboard default.
        **values (str | Sequence[str]): Variable values (default All).

    Returns:
        list[dict[str, Any]]: Rows.
    """
    conn, seed = db
    query = next(q for q in QUERIES if q.dashboard == uid and q.panel_id == panel_id)
    ctx = _context(query, *(window or time_range(DASHBOARDS[uid], seed.now)))
    return _execute(conn, query, _with_variables(conn, uid, ctx, values))


def _one(rows: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    """
    Return a stat's numeric value and its label.

    Args:
        rows (list[dict[str, Any]]): The stat query's rows (exactly one).

    Returns:
        tuple[float | None, str | None]: (value, label); None value means "no data".
    """
    [row] = rows
    value = next(v for k, v in row.items() if k != "label")
    return (None if value is None else float(value)), row.get("label")


def _latest(rows: list[dict[str, Any]]) -> dict[str, float]:
    """
    Return the last sample of a long time series as {series: value}, in series order.

    Args:
        rows (list[dict[str, Any]]): Rows with time, metric, value.

    Returns:
        dict[str, float]: Values at the last time.
    """
    last = max(r["time"] for r in rows)
    return {r["metric"]: float(r["value"]) for r in rows if r["time"] == last}


# --- every query --------------------------------------------------------------------------------


@pytest.mark.parametrize("selected", [False, True], ids=["all", "one-cluster"])
@pytest.mark.parametrize("query", QUERIES, ids=[q.label for q in QUERIES])
def test_panel_query_runs_and_shows_no_raw_codes(
    db: tuple[Conn, Seed], query: PanelQuery, selected: bool
) -> None:
    """Every query runs over the default range, with All and one cluster; no cell shows a code."""
    assert query.sql.strip(), f"{query.label}: Postgres target has no rawSql (use code mode)"
    values = {"cluster": [C1]} if selected else {}
    rows = _panel(db, query.dashboard, cast(int, query.panel_id), **values)
    codes = {
        (k, v) for r in rows for k, v in r.items() if isinstance(v, str) and RAW_CODE.search(v)
    }
    assert not codes, f"{query.label}: raw codes {codes}"


@pytest.mark.parametrize("query", VARIABLE_QUERIES, ids=[q.label for q in VARIABLE_QUERIES])
def test_cluster_options(db: tuple[Conn, Seed], query: PanelQuery) -> None:
    """Clusters come from the ledger (matched, else named in evidence) and workloads_now."""
    conn, seed = db
    ctx = QueryContext(*time_range(DASHBOARDS[query.dashboard], seed.now))
    rows = _execute(conn, query, _with_variables(conn, query.dashboard, ctx, {}))
    assert [next(iter(r.values())) for r in rows] == ["(no cluster)", "ocp4-dev", C1, C2]


# --- Home -------------------------------------------------------------------------------------


def test_home_headline(db: tuple[Conn, Seed]) -> None:
    """48 GPUs wasted now, of 110; the 16 with no data are named, never counted as waste."""
    assert _one(_panel(db, "ttv-home", 1)) == (48, "of 114 GPUs · 20 have no data")
    hours, label = _one(_panel(db, "ttv-home", 2))
    assert hours == pytest.approx(420 + 984 + 96 + 212 + 96, abs=0.5)
    assert label == "in the selected period · 56 GPU-hours have no data"  # 4*1 + 8*2 + 4*6 + 4*3


def test_home_where_every_gpu_is(db: tuple[Conn, Seed]) -> None:
    """Each GPU in one group; maintenance is in repair, not waste (ADR-0009)."""
    [row] = _panel(db, "ttv-home", 3)
    assert {k: float(v) for k, v in row.items()} == {
        "In use": 28, "Wasted": 48, "In repair": 8, "No data": 20, "Not measured": 8,
        "Excluded": 2,
    }  # fmt: skip


def test_data_as_of(db: tuple[Conn, Seed]) -> None:
    """The newest ok or partial engine scan; a newer dry run does not count."""
    value, _ = _one(_panel(db, "ttv-home", 4))
    assert value == pytest.approx((db[1].now - 9 * M).timestamp() * 1000, abs=1000)


def test_wasted_now_per_team(db: tuple[Conn, Seed]) -> None:
    """Home tiles (Samurai first) agree with each team page's headline and its label."""
    tiles = [_one(_panel(db, "ttv-home", pid)) for pid in range(5, 10)]
    assert tiles == [
        (12, "nothing on them · $4,200 in the period"),
        (16, "servers not ready · $9,624 in the period"),
        (8, "GPU software not ready · no AWS price"),  # A16s: nothing priced, never $0
        (8, "models barely used · $2,120 in the period"),
        (4, "jobs barely busy · $960 in the period"),
    ]  # fmt: skip
    heads = {uid: _one(_panel(db, uid, 1)) for uid in TEAM_PAGES}
    assert heads == {
        "ttv-samurai": (12, "Samurai · 20 GPUs have no data"),
        "ttv-redbull": (16, "Redbull · 20 GPUs have no data"),
        "ttv-popai": (8, "POPAI · 20 GPUs have no data"),
        "ttv-apply": (8, "Apply · 4 GPUs have no data"),  # the model whose requests are unread
        "ttv-ml": (4, "Machine Learning · 8 GPUs have no data"),  # the job with no activity
    }


def test_home_trend_by_team(db: tuple[Conn, Seed]) -> None:
    """One series per team in tile order, named by the stage's latest owner, plus No data."""
    latest = _latest(_panel(db, "ttv-home", 10))
    assert latest == {"Samurai": 12, "Redbull": 16, "POPAI": 8, "Apply": 8,
                      "Machine Learning": 4, "No data": 20}  # fmt: skip
    assert list(latest) == ["Samurai", "Redbull", "POPAI", "Apply", "Machine Learning", "No data"]
    assert OLD_NAME not in {r["metric"] for r in _panel(db, "ttv-home", 10)}


# --- period numbers ---------------------------------------------------------------------------


def test_gpu_hours_per_team_over_the_default_week(db: tuple[Conn, Seed]) -> None:
    """GPU-hours wasted in the default 7 days, per team page."""
    got = {uid: _one(_panel(db, uid, 2)) for uid in TEAM_PAGES}
    expected = {"ttv-samurai": 420, "ttv-redbull": 984, "ttv-popai": 96, "ttv-apply": 212,
                "ttv-ml": 96}  # fmt: skip
    for uid, (hours, label) in got.items():
        assert hours == pytest.approx(expected[uid], abs=0.5), uid
        assert label == "in the selected period", uid


def test_gpu_hours_match_agent_17(db: tuple[Conn, Seed]) -> None:
    """§17: 8 GPUs idle 3 h = 24 GPU-h, 16 when clipped to 2 h; 6 of 8 idle for 2 h = 12."""
    now = db[1].now
    episode = (now - 73 * H, now - 67 * H)
    assert _one(_panel(db, "ttv-redbull", 2, episode, cluster=[C2]))[0] == pytest.approx(24)
    clipped = (now - 72 * H + 30 * M, now - 72 * H + 150 * M)
    assert _one(_panel(db, "ttv-redbull", 2, clipped, cluster=[C2]))[0] == pytest.approx(16)
    partial_idle = (now - 49 * H, now - 45 * H)
    assert _one(_panel(db, "ttv-samurai", 2, partial_idle, cluster=[C2]))[0] == pytest.approx(12)


def test_period_before_the_first_scan_is_a_lower_bound(db: tuple[Conn, Seed]) -> None:
    """A range that starts before the first scan says 'at least' (time before it is unknown)."""
    now, first = db[1].now, db[1].first_scan
    day = first.strftime("%d %b %Y").lstrip("0")
    for uid in ("ttv-home", "ttv-redbull"):
        hours, label = _one(_panel(db, uid, 2, (now - 30 * D, now)))
        assert label is not None and label.startswith(f"at least: no data before {day}"), uid
    hours, _ = _one(_panel(db, "ttv-redbull", 2, (now - 30 * D, now)))
    assert hours == pytest.approx(984, abs=0.5)  # nothing recorded before the first scan


# --- team pages -------------------------------------------------------------------------------


def test_reasons_in_plain_words(db: tuple[Conn, Seed]) -> None:
    """Every reason of the team is listed in plain words, 0 when nothing waits for it."""
    got = {uid: [(r["Reason"], float(r["GPUs"])) for r in _panel(db, uid, 3)]
           for uid in TEAM_PAGES}  # fmt: skip
    assert got == {
        "ttv-samurai": [("Empty: nothing running on it", 12)],
        "ttv-redbull": [
            ("Not in a cluster yet", 8), ("Waiting in cluster inventory", 0),
            ("Node not ready", 0), ("Node switched off for scheduling", 0),
            ("Hardware problem", 8), ("Management controller unreachable", 0),
        ],
        "ttv-popai": [("GPU software not ready", 8), ("GPUs missing", 0)],
        "ttv-apply": [("Barely used", 2), ("No requests", 2), ("Model not starting", 4)],
        "ttv-ml": [("GPUs barely busy", 2), ("Job not starting", 2)],
    }  # fmt: skip


def test_team_trend_by_reason_keeps_history_as_recorded(db: tuple[Conn, Seed]) -> None:
    """Maintenance counted as Redbull's waste before ADR-0009 stays visible, in plain words."""
    rows = _panel(db, "ttv-redbull", 4)
    assert "In repair (before 25 Nov 2026)" in {r["metric"] for r in rows}
    latest = _latest(rows)
    assert latest["Not in a cluster yet"] == 8 and latest["Hardware problem"] == 8
    assert latest["In repair (before 25 Nov 2026)"] == 0
    assert _latest(_panel(db, "ttv-apply", 4)) == {
        "Barely used": 2, "No requests": 2, "Model not starting": 4,
    }  # fmt: skip


def test_team_tables_mark_lower_bounds(db: tuple[Conn, Seed]) -> None:
    """Servers by GPUs times time waiting; a stage that began before observation reads '≥'."""
    cols = ("Server", "GPU model", "Cluster", "GPUs", "Reason", "Waiting since",
            "GPU-hours so far", "Cost so far at AWS prices")  # fmt: skip
    rows = {uid: [tuple(r[c] for c in cols) for r in _panel(db, uid, 5)]
            for uid in ("ttv-redbull", "ttv-samurai", "ttv-popai")}  # fmt: skip
    assert rows == {
        "ttv-redbull": [  # 8 GPUs x $10 x 240 h, a lower bound like the wait itself
            ("gpu-new-03", "B300 288GB", "(no cluster)", 8, "Not in a cluster yet",
             "≥ 10 days ago", "≥ 1,920", "≥ $19,200"),
            ("gpu-hw-04", "H100 80GB", C1, 8, "Hardware problem", "1 day ago", "192",
             "$1,920"),  # cluster from evidence
        ],
        "ttv-samurai": [
            ("gpu-empty-01", "H200 141GB", C1, 8, "Empty: nothing running on it", "2 days ago",
             "384", "$3,840"),
            ("gpu-empty-02", "H100 80GB", C2, 4, "Empty: nothing running on it", "6 hours ago",
             "24", "$240"),
        ],
        "ttv-popai": [("gpu-sw-05", "A16 16GB", C1, 8, "GPU software not ready", "12 hours ago",
                       "96", "no AWS price")],
    }  # fmt: skip


def test_apply_lists_every_model_with_its_status(db: tuple[Conn, Seed]) -> None:
    """Waste first, then no data, then in use; incomplete counts read '≥', their rate 'about'."""
    cols = ("Model", "Namespace", "Cluster", "GPUs", "Requests, last hour",
            "Requests, last 24 h", "Requests per GPU per hour", "Status")  # fmt: skip
    table = _panel(db, "ttv-apply", 5)
    rows = [tuple(r[c] for c in cols) for r in table]
    # GPUs x $10 x 24 on wasted rows only; blank on models in use or with no data.
    assert [(r["Model"], r["GPU model"], r["Waste per day at AWS prices"]) for r in table] == [
        ("vision", "H100 80GB", "$960"), ("legal", "B200 180GB", "$480"),
        ("translate", "B200 180GB", "$480"), ("search", "H100 80GB", None),
        ("chat", "B200 180GB", None), ("summarise", "B200 180GB", None),
    ]  # fmt: skip
    assert rows == [
        ("vision", "vision-team", C2, 4, None, None, None, "Not starting"),
        ("legal", "llm", C1, 2, "≥ 1", "≥ 12", "about 0.25", "Barely used"),
        ("translate", "llm", C1, 2, "0", "0", "0.00", "No requests"),
        ("search", "search", C2, 4, None, None, None, "No data"),
        ("chat", "llm", C1, 2, "120", "2,400", "50", "Busy"),
        ("summarise", "llm", C1, 2, "0", "24 in 6 h", "2.00", "Quiet this hour"),
    ]
    wasted = sum(r[3] for r in rows if r[7] in ("Not starting", "Barely used", "No requests"))
    assert wasted == _one(_panel(db, "ttv-apply", 1))[0] == 8  # the table agrees with the ledger


def test_ml_lists_every_job_with_its_status(db: tuple[Conn, Seed]) -> None:
    """Every machine learning job, Run:ai or other; the unresolved workload is not a job."""
    cols = ("Job", "Namespace", "Cluster", "Runs on", "GPUs", "GPU busy (peak, 6 h)", "Status")
    table = _panel(db, "ttv-ml", 5)
    rows = [tuple(r[c] for c in cols) for r in table]
    assert [(r["GPU model"], r["Waste per day at AWS prices"]) for r in table] == [
        ("H100 80GB", "$480"), ("H100 80GB", "$480"),
        ("H200 141GB, RTX X9", None),  # two models; one with no catalogue name, kept as reported
        ("L4 24GB", None), ("H100 80GB", None), ("H100 80GB", None), ("H100 80GB", None),
    ]  # fmt: skip
    assert rows == [
        ("eval", "research", C1, "Run:ai", 2, None, "Not starting"),
        ("notebook-ana", "research", C1, "Other", 2, "3%", "Barely busy"),
        ("sweep", "research", C2, "Run:ai", 8, None, "No data"),
        ("batch-infer", "batch", C2, "Other", 8, "60%", "Busy"),
        ("vision-train", "team-b", C2, "Run:ai", 8, "81%", "Busy"),
        ("llama-ft", "team-a", C2, "Run:ai", 4, "75%", "Busy"),
        ("pretrain", "research", C1, "Run:ai", 4, "92%", "Busy"),
    ]
    wasted = sum(r[4] for r in rows if r[6] in ("Not starting", "Barely busy"))
    assert wasted == _one(_panel(db, "ttv-ml", 1))[0] == 4


# --- filters and unknown ----------------------------------------------------------------------


def test_cluster_filter(db: tuple[Conn, Seed]) -> None:
    """One cluster: its GPUs only; a team with nothing there is a real 0, not 'no data'."""
    assert _one(_panel(db, "ttv-home", 1, cluster=[C2])) == (8, "of 40 GPUs · 12 have no data")
    assert _one(_panel(db, "ttv-popai", 1, cluster=[C2]))[0] == 0
    assert _one(_panel(db, "ttv-apply", 1, cluster=[C2])) == (4, "Apply · 4 GPUs have no data")
    assert [r["Model"] for r in _panel(db, "ttv-apply", 5, cluster=[C1])] == [
        "legal", "translate", "chat", "summarise",
    ]  # fmt: skip
    assert _one(_panel(db, "ttv-redbull", 1, cluster=["(no cluster)"]))[0] == 8


def test_unknown_is_never_zero(db: tuple[Conn, Seed]) -> None:
    """A cluster with no GPUs gives no value or no rows ("no data yet"), never 0."""
    nowhere = ["no-such-cluster"]
    for uid, pid in (("ttv-home", 1), ("ttv-home", 2), ("ttv-home", 11), ("ttv-home", 12),
                     *(("ttv-home", p) for p in range(5, 10)),
                     *((u, p) for u in TEAM_PAGES for p in (1, 2, 6))):  # fmt: skip
        assert _one(_panel(db, uid, pid, cluster=nowhere))[0] is None, (uid, pid)
    for uid, pid in (("ttv-home", 3), ("ttv-home", 10), ("ttv-home", 13),
                     *((u, p) for u in TEAM_PAGES for p in (3, 4, 5, 7))):  # fmt: skip
        assert _panel(db, uid, pid, cluster=nowhere) == [], (uid, pid)


def test_before_the_first_scan_nothing_reads_zero(db: tuple[Conn, Seed]) -> None:
    """An empty ledger (a new install) shows 'no data yet' everywhere, never 0 waste."""
    conn, seed = db
    with conn.transaction(force_rollback=True):
        conn.execute("DELETE FROM workloads_now; DELETE FROM capacity_intervals;"
                     " DELETE FROM scan_runs")  # fmt: skip
        for query in QUERIES:
            ctx = _context(query, *time_range(DASHBOARDS[query.dashboard], seed.now))
            sql = render(query.sql, _with_variables(conn, query.dashboard, ctx, {}))
            rows = conn.execute(cast(LiteralString, sql)).fetchall()
            numbers = [v for r in rows for k, v in r.items()
                       if k not in ("label", "time") and not isinstance(v, str)]  # fmt: skip
            assert all(v is None for v in numbers), (query.label, rows)
            if query.dashboard == "ttv-home" and query.panel_id in (1, 2):
                assert [r["label"] for r in rows] == ["nothing measured yet"], query.label


# --- AWS price (present-day valuation, ADR-0003; synthetic rates) -----------------------------


def test_cost_of_waste_per_team_and_total(db: tuple[Conn, Seed]) -> None:
    """GPU-hours x the selected snapshot's rate; unpriced GPU-hours make the total 'at least'."""
    price_list = db[1].price_list
    got = {uid: _one(_panel(db, uid, 6)) for uid in TEAM_PAGES}
    assert got == {
        "ttv-samurai": (pytest.approx(4200, abs=1), price_list),  # 420 GPU-h at $10
        "ttv-redbull": (pytest.approx(9624, abs=1), price_list),  # 960 at $10, 24 at $1
        "ttv-popai": (None, f"96 GPU-hours have no AWS price · {price_list}"),  # "No price"
        "ttv-apply": (pytest.approx(2120, abs=1), price_list),
        "ttv-ml": (pytest.approx(960, abs=1), price_list),
    }
    total, label = _one(_panel(db, "ttv-home", 11))
    assert total == pytest.approx(4200 + 9624 + 2120 + 960, abs=1)  # 16,904: POPAI unpriced
    assert label == f"at least · 96 GPU-hours have no AWS price · {price_list}"


def test_linear_pricing_matches_agent_17(db: tuple[Conn, Seed]) -> None:
    """§17 at a synthetic $1 per GPU-hour: 24 GPU-hours cost $24, and $16 clipped to 2 h."""
    now = db[1].now
    episode = (now - 73 * H, now - 67 * H)
    assert _one(_panel(db, "ttv-redbull", 6, episode, cluster=[C2]))[0] == pytest.approx(24)
    clipped = (now - 72 * H + 30 * M, now - 72 * H + 150 * M)
    assert _one(_panel(db, "ttv-redbull", 6, clipped, cluster=[C2]))[0] == pytest.approx(16)
    # The same 6 GPUs idle for 2 h on H100s at $10 (§17's partial idle) cost $120.
    partial_idle = (now - 49 * H, now - 45 * H)
    assert _one(_panel(db, "ttv-samurai", 6, partial_idle, cluster=[C2]))[0] == pytest.approx(120)


def test_costing_now(db: tuple[Conn, Seed]) -> None:
    """40 priced wasted GPUs at $10 an hour; the 8 unpriced A16s make the day 'at least'."""
    value, label = _one(_panel(db, "ttv-home", 12))
    assert value == pytest.approx(400)
    assert label == f"at least $9,600 a day · 8 wasted GPUs have no AWS price · {db[1].price_list}"
    assert _one(_panel(db, "ttv-home", 12, cluster=[C2])) == (
        pytest.approx(80),
        f"about $1,920 a day · {db[1].price_list}",
    )  # 4 empty H100s and 4 not-starting H100s


def test_period_cost_before_the_first_scan_is_at_least(db: tuple[Conn, Seed]) -> None:
    """A range that starts before the first scan says 'at least', like its GPU-hours."""
    now, first = db[1].now, db[1].first_scan
    value, label = _one(_panel(db, "ttv-samurai", 6, (now - 30 * D, now)))
    assert value == pytest.approx(4200, abs=1)
    day = first.strftime("%d %b %Y").lstrip("0")
    assert label == f"at least · no data before {day} · {db[1].price_list}"
    assert _one(_panel(db, "ttv-home", 6, (now - 30 * D, now)))[1] == (
        "servers not ready · ≥ $9,624 in the period"
    )


def test_selected_snapshot_wins_else_the_latest(db: tuple[Conn, Seed]) -> None:
    """pricing_selection names the snapshot; without it the latest published one is used."""
    conn, seed = db
    query = next(q for q in QUERIES if q.dashboard == "ttv-samurai" and q.panel_id == 6)
    ctx = _with_variables(conn, "ttv-samurai", _context(query, seed.now - 7 * D, seed.now), {})
    with conn.transaction(force_rollback=True):
        conn.execute("DELETE FROM pricing_selection")
        [row] = conn.execute(cast(LiteralString, render(query.sql, ctx))).fetchall()
    # The newer snapshot prices only the H100 ($20): 36 GPU-h of H100s, 384 of H200s unpriced.
    assert float(row["Cost of waste at AWS prices"]) == pytest.approx(720, abs=1)
    published = (seed.now - 2 * D).strftime("%d %b %Y").lstrip("0")
    assert row["label"] == (
        f"at least · 384 GPU-hours have no AWS price · AWS price list of {published} (us-east-1)"
    )


def test_no_price_list_says_so(db: tuple[Conn, Seed]) -> None:
    """With no snapshot imported, money panels say so and show no number, never $0."""
    conn, seed = db
    expected = {
        ("ttv-home", 11): (None, "no AWS price list loaded"),
        ("ttv-home", 12): (None, "no AWS price list loaded"),
        ("ttv-home", 5): (12, "nothing on them · no AWS price list"),
        ("ttv-samurai", 6): (None, "no AWS price list loaded"),
    }
    with conn.transaction(force_rollback=True):
        conn.execute("DELETE FROM pricing_selection; DELETE FROM pricing_rates;"
                     " DELETE FROM pricing_snapshots")  # fmt: skip
        got = {}
        for uid, pid in [*expected, ("ttv-samurai", 5), ("ttv-apply", 5), ("ttv-home", 13)]:
            query = next(q for q in QUERIES if q.dashboard == uid and q.panel_id == pid)
            ctx = _context(query, *time_range(DASHBOARDS[uid], seed.now))
            sql = render(query.sql, _with_variables(conn, uid, ctx, {}))
            got[uid, pid] = conn.execute(cast(LiteralString, sql)).fetchall()
    assert {k: _one(got[k]) for k in expected} == expected
    assert {r["Cost so far at AWS prices"] for r in got["ttv-samurai", 5]} == {"no price list"}
    assert [r["Waste per day at AWS prices"] for r in got["ttv-apply", 5]][:3] == [
        "no price list"] * 3  # fmt: skip
    models = got["ttv-home", 13]
    assert {r["AWS price per GPU-hour"] for r in models} == {"no price list", None}
    assert {r["Wasted, costing per hour at AWS prices"] for r in models} == {"no price list"}


# --- GPU models -------------------------------------------------------------------------------


MODEL_COLS = ("Model", "GPUs", "In use", "Wasted", "In repair", "No data",
              "AWS price per GPU-hour", "Wasted, costing per hour at AWS prices")  # fmt: skip


def test_home_gpus_by_model(db: tuple[Conn, Seed]) -> None:
    """One row per model, biggest fleet first, no GPU dropped, a total row last."""
    rows = _panel(db, "ttv-home", 13)
    assert [tuple(r[c] for c in MODEL_COLS) for r in rows] == [
        ("H100 80GB", 52, 16, 20, 0, 8, "$10.00", "$200"),  # whole dollars from $100
        ("B300 288GB", 16, 0, 8, 8, 0, "$10.00", "$80.00"),
        ("H200 141GB", 16, 0, 8, 0, 8, "$10.00", "$80.00"),
        ("A16 16GB", 8, 0, 8, 0, 0, "no AWS price", "no AWS price"),
        ("B200 180GB", 8, 4, 4, 0, 0, "$10.00", "$40.00"),
        ("L4 24GB", 8, 8, 0, 0, 0, "$1.00", "$0.00"),
        ("RTX X9", 4, 0, 0, 0, 4, "no AWS price", "no AWS price"),  # no catalogue name
        ("Unknown model", 2, 0, 0, 0, 0, "no AWS price", "no AWS price"),  # no model at all
        ("All models", 114, 28, 48, 8, 20, None, "≥ $400"),
    ]  # fmt: skip
    shares = {r["Model"]: float(r["Wasted share"]) for r in rows}
    assert shares["H100 80GB"] == pytest.approx(100 * 20 / 52)
    assert shares["A16 16GB"] == 100 and shares["All models"] == pytest.approx(100 * 48 / 114)


def test_model_rows_add_up_to_where_every_gpu_is(db: tuple[Conn, Seed]) -> None:
    """The per-model counts sum to the summary's groups and the total row, for any cluster."""
    for cluster in ([], [C1], [C2], ["(no cluster)"]):
        values = {"cluster": cluster} if cluster else {}
        rows = _panel(db, "ttv-home", 13, **values)
        [where] = _panel(db, "ttv-home", 3, **values)
        models, [total] = rows[:-1], rows[-1:]
        for column in ("In use", "Wasted", "In repair", "No data"):
            assert sum(r[column] for r in models) == total[column] == where[column], column
        assert sum(r["GPUs"] for r in models) == total["GPUs"] == sum(where.values())


def test_team_pages_show_wasted_gpus_by_model(db: tuple[Conn, Seed]) -> None:
    """Each team's wasted GPUs by model; a team with none in a cluster shows one zero bar."""
    got = {uid: [(r["GPU model"], r["Wasted GPUs"]) for r in _panel(db, uid, 7)]
           for uid in TEAM_PAGES}  # fmt: skip
    assert got == {
        "ttv-samurai": [("H200 141GB", 8), ("H100 80GB", 4)],
        "ttv-redbull": [("B300 288GB", 8), ("H100 80GB", 8)],
        "ttv-popai": [("A16 16GB", 8)],
        "ttv-apply": [("B200 180GB", 4), ("H100 80GB", 4)],
        "ttv-ml": [("H100 80GB", 4)],
    }
    assert [(r["GPU model"], r["Wasted GPUs"]) for r in _panel(db, "ttv-popai", 7, cluster=[C2])
            ] == [("No wasted GPUs", 0)]  # fmt: skip


# --- GPU power (ADR-0009) ---------------------------------------------------------------------


def test_gpus_drawing_power_with_no_pod_are_no_data_not_waste(db: tuple[Conn, Seed]) -> None:
    """GPUs no pod holds that draw power: on Samurai's page, in Home's 'No data', not waste."""
    assert _one(_panel(db, "ttv-samurai", 8)) == (4, "GPUs with no pod: ask the platform team")
    assert _one(_panel(db, "ttv-samurai", 1))[0] == 12  # not Samurai's waste
    assert _panel(db, "ttv-home", 3)[0]["No data"] == 20  # 16 + the 4 drawing power
    assert _one(_panel(db, "ttv-samurai", 8, cluster=[C2]))[0] == 0  # a real 0 there
    assert _one(_panel(db, "ttv-samurai", 8, cluster=["no-such-cluster"]))[0] is None


def test_power_columns(db: tuple[Conn, Seed]) -> None:
    """Share of rated power: 24 h average per served model, 6 h peak per job; blank when unread."""
    apply_ = {r["Model"]: (r["GPU power (24 h average)"], r["Status"])
              for r in _panel(db, "ttv-apply", 5)}  # fmt: skip
    assert apply_ == {
        "vision": (None, "Not starting"), "legal": ("18%", "Barely used"),
        "translate": ("10%", "No requests"), "search": (None, "No data"),
        "chat": ("62%", "Busy"), "summarise": ("48%", "Quiet this hour"),
    }  # fmt: skip
    jobs = {r["Job"]: (r["GPU power (peak, 6 h)"], r["Status"]) for r in _panel(db, "ttv-ml", 5)}
    assert jobs == {
        "eval": (None, "Not starting"), "notebook-ana": ("8%", "Barely busy"),
        "sweep": (None, "No data"), "batch-infer": ("55%", "Busy"),
        "vision-train": ("66%", "Busy"), "llama-ft": ("70%", "Busy"), "pretrain": ("85%", "Busy"),
    }  # fmt: skip
