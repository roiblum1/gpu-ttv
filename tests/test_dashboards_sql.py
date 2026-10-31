"""
Run every dashboard query against a migrated, seeded Postgres, the way Grafana sends it.

Needs `TTV_TEST_DSN` (a throwaway database: the schema is dropped); skipped otherwise.
Macros and variables are substituted by `tests/grafana_sql.py`. The fixture is synthetic.
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

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = {
    p.stem: json.loads(p.read_text()) for p in sorted(ROOT.glob("grafana/dashboards/*.json"))
}
QUERIES = [q for d in DASHBOARDS.values() for q in panel_queries(d)]
VARIABLE_QUERIES = [q for d in DASHBOARDS.values() for q in variable_queries(d)]

POLICY = "fixture-1"
SNAPSHOT = "fixture-aws-us-east-1-synthetic"
H100_RAW, H100 = "NVIDIA H100 80GB HBM3", "NVIDIA H100 80GB"
A16_RAW, A16 = "NVIDIA A16", "NVIDIA A16 16GB"
CLUSTER = "ocp4-prod-gpu-site1"
COLLECTOR = "collector:ucs-central-fixture"
DC = "cisco:fxdc0001"  # DC episode that began before observation (lower bound)
RB = "cisco:fxrb0002"  # the §17 asset: 8 GPUs idle 3 h at Redbull, then productive
SV = "dell:fxsv0003"  # §17 partial idle: 6 idle beside 2 productive; now NO_REAL_DEMAND
DGX = "nvidia:fxdgx0004"  # standalone DGX at Redbull with a next-action annotation

Conn = psycopg.Connection[dict[str, Any]]
M = timedelta(minutes=1)


@dataclass(frozen=True)
class Seed:
    """
    Times the synthetic fixture was built around.

    Args:
        now (datetime): Seed time (open intervals are relative to it).
        p0 (datetime): Start of the §17 episode, 30 days earlier on a whole hour.
    """

    now: datetime
    p0: datetime


H = timedelta(hours=1)
D = timedelta(days=1)


def _asset(
    conn: Conn, asset_id: str, name: str, site: str | None, gpu: tuple[str | None, int | None],
    source: str, first: datetime,
) -> None:  # fmt: skip
    """
    Insert one asset, last observed 10 minutes ago.

    Args:
        conn (Conn): Connection.
        asset_id (str): `<vendor>:<serial>`.
        name (str): Server name.
        site (str | None): Site, or None.
        gpu (tuple[str | None, int | None]): Raw GPU model and count (None = unread).
        source (str): Registering source.
        first (datetime): First observation.
    """
    vendor, serial = asset_id.split(":")
    conn.execute(
        "INSERT INTO assets (asset_id, vendor, serial, name, model, site, gpu_model, gpu_count,"
        " source, first_observed_at, last_observed_at)"
        " VALUES (%s, %s, %s, %s, 'Synthetic GPU server', %s, %s, %s, %s, %s, now() - interval"
        " '10 minutes')",
        (asset_id, vendor, serial, name, site, *gpu, source, first),
    )


def _interval(
    conn: Conn, asset_id: str, category: str, reason: str, gpus: int, start: datetime,
    end: datetime | None = None, *, stage: str | None = None, owner: str | None = None,
    entered: datetime | None = None, lb: bool = False, model: str = H100_RAW,
    holder: str | None = None,
) -> None:  # fmt: skip
    """
    Insert one ledger interval under the fixture policy.

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
        model (str): Raw GPU model.
        holder (str | None): Workload holding the GPUs.
    """
    cluster = CLUSTER if stage in ("GPU_PLATFORM", "SERVING") or category == "PRODUCTIVE" else None
    conn.execute(
        "INSERT INTO capacity_intervals (asset_id, category, reason_code, stage, stage_owner,"
        " stage_entered_at, stage_entered_lower_bound, gpu_model, gpus, started_at, ended_at,"
        " holder_ref, cluster, policy_version)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (asset_id, category, reason, stage, owner, entered, lb, model, gpus, start, end, holder,
         cluster, POLICY),
    )  # fmt: skip


def _seed(conn: Conn) -> Seed:
    """
    Insert the synthetic fixture: assets in every category, episodes, pricing, milestones.

    Args:
        conn (Conn): Connection to a freshly migrated database.

    Returns:
        Seed: The times the fixture uses.
    """
    now = datetime.now(UTC).replace(microsecond=0)
    p0 = (now - 30 * D).replace(minute=0, second=0)
    ss = "serverscan"
    _asset(conn, DC, "fixture-dc-01", "site-a", (H100_RAW, 8), COLLECTOR, now - 5 * D)
    _asset(conn, RB, "ocp4-fixture-gpu-02", "site-a", (H100_RAW, 8), ss, p0 - 2 * D)
    _asset(conn, SV, "ocp4-fixture-gpu-03", "site-b", (H100_RAW, 8), ss, p0 - 2 * D)
    _asset(conn, DGX, "dgx-fixture-04", "site-b", (H100_RAW, 8), ss, now - 3 * D)
    _asset(conn, "dell:fxuk0005", "ocp4-fixture-gpu-05", "site-a", (H100_RAW, 4), ss, now - 10 * D)
    _asset(conn, "cisco:fxnc0006", "lab-fixture-06", None, (A16_RAW, 2), ss, now - 10 * D)
    _asset(conn, "dell:fxpp0007", "ocp4-fixture-gpu-07", "site-b", (A16_RAW, 2), ss, now - D)
    _asset(conn, "cisco:fxun0008", "fixture-unread-08", "site-a", (None, None), COLLECTOR, now - H)

    # §17: 8 GPUs idle 3 h with a 1 h objective; 6 of 8 idle for 2 h beside 2 productive.
    _interval(conn, RB, "IDLE", "NOT_IN_CLUSTER", 8, p0, p0 + 3 * H,
              stage="PROVISIONING", owner="Redbull", entered=p0)  # fmt: skip
    _interval(conn, RB, "PRODUCTIVE", "RUNAI_WORKLOAD", 8, p0 + 3 * H, holder="team-a/Training/pt")
    _interval(conn, SV, "IDLE", "NO_WORKLOAD", 6, p0 + H, p0 + 3 * H,
              stage="SERVING", owner="Serving", entered=p0 + H)  # fmt: skip
    _interval(conn, SV, "PRODUCTIVE", "RUNAI_WORKLOAD", 2, p0 + H, p0 + 3 * H, holder="team-b/I/c")
    # Now: one open episode per owner (DC's began before observation), plus other categories.
    _interval(conn, DC, "IDLE", "PROFILE_NONE", 8, now - 5 * D,
              stage="PRE_PROVISIONING", owner="DC", entered=now - 5 * D, lb=True)  # fmt: skip
    _interval(conn, DGX, "IDLE", "NOT_IN_CLUSTER", 8, now - 3 * D,
              stage="PROVISIONING", owner="Redbull", entered=now - 3 * D)  # fmt: skip
    _interval(conn, "dell:fxpp0007", "IDLE", "GPU_PLATFORM_NOT_READY", 2, now - D,
              stage="GPU_PLATFORM", owner="POPAI", entered=now - D, model=A16_RAW)  # fmt: skip
    _interval(conn, SV, "IDLE", "NO_REAL_DEMAND", 6, now - 2 * D,
              stage="SERVING", owner="Serving", entered=now - 2 * D, holder="llm/idle")  # fmt: skip
    _interval(conn, SV, "PRODUCTIVE", "SERVING_IN_USE", 2, now - 2 * D, holder="llm/LLMIS/chat")
    _interval(conn, "dell:fxuk0005", "UNKNOWN", "SOURCE_STALE", 4, now - 6 * H)
    _interval(conn, "cisco:fxnc0006", "NOT_CHARGED", "OTHER_INTENDED_USE", 2, now - 10 * D,
              model=A16_RAW)  # fmt: skip

    for stage, objective in (
        ("PRE_PROVISIONING", 3 * D), ("PROVISIONING", H), ("GPU_PLATFORM", D), ("SERVING", 7 * D),
    ):  # fmt: skip
        conn.execute(
            "INSERT INTO stage_objectives (policy_version, stage, objective) VALUES (%s, %s, %s)",
            (POLICY, stage, objective),
        )
    for kind, started, status, errors in (
        ("engine", now - 70 * M, "partial", [{"source": CLUSTER, "error": "timeout"}]),
        ("collector", now - 20 * M, "ok", []),
        ("engine", now - 10 * M, "ok", []),
    ):
        conn.execute(
            "INSERT INTO scan_runs (kind, started_at, finished_at, status, coverage, errors)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (kind, started, started + M, status, Jsonb({"serverscan": "complete"}), Jsonb(errors)),
        )
    _seed_milestones(conn, now, p0)
    conn.execute(
        "INSERT INTO next_action_annotations (asset_id, next_action_owner, reason, created_by,"
        " created_at, expires_at) VALUES (%s, 'Network', 'PXE VLAN missing (fixture)', 'fixture',"
        " %s, %s)",
        (DGX, now - D, now + 6 * D),
    )
    conn.execute(
        "INSERT INTO identity_conflicts (kind, key, candidates, first_seen, last_seen)"
        " VALUES ('serial', 'cisco:fxdup0009', %s, %s, %s)",
        (Jsonb([{"source": "ucs-central"}, {"source": "intersight"}]), now - 2 * D, now - H),
    )
    for name, failed, ok_at, err in (
        ("serverscan", 0, now - 9 * M, None),
        (f"cluster:{CLUSTER}", 1, now - H, "timeout (fixture)"),
        (COLLECTOR, 3, None, "permission denied (fixture)"),
    ):
        conn.execute(
            "INSERT INTO source_state (name, failed_scans, last_success_at, last_attempt_at,"
            " last_error) VALUES (%s, %s, %s, %s, %s)",
            (name, failed, ok_at, now - 9 * M, err),
        )
    _seed_pricing(conn, now, p0)
    _seed_collector(conn, now)
    return Seed(now=now, p0=p0)


def _seed_milestones(conn: Conn, now: datetime, p0: datetime) -> None:
    """
    Insert lifecycle milestones, including lower-bounded ones.

    Args:
        conn (Conn): Connection.
        now (datetime): Seed time.
        p0 (datetime): The §17 episode start.
    """
    milestones = [
        (DC, "first_seen", now - 5 * D, True),
        (RB, "first_seen", p0 - 2 * D, True),
        (RB, "in_serverscan", p0 - 2 * D, False),
        (RB, "assigned_cluster", p0 + 2 * H, False),
        (RB, "node_ready", p0 + 2 * H, False),
        (RB, "gpu_ready", p0 + 150 * M, False),
        (RB, "workload_running", p0 + 3 * H, False),
        (SV, "first_seen", p0 - 2 * D, True),
        (SV, "in_serverscan", p0 - 2 * D, False),
        (SV, "gpu_ready", p0 - D, False),
        (SV, "workload_running", p0 + H, False),
        (SV, "first_genuine_request", now - D, True),
        (DGX, "first_seen", now - 3 * D, True),
        (DGX, "in_serverscan", now - 3 * D, False),
        ("dell:fxpp0007", "first_seen", now - D, True),
        ("dell:fxpp0007", "node_ready", now - D, True),
    ]
    for m in milestones:
        conn.execute(
            "INSERT INTO milestones (asset_id, milestone, observed_at, lower_bound, evidence)"
            " VALUES (%s, %s, %s, %s, %s)",
            (*m, Jsonb({"source": "fixture"})),
        )


def _seed_pricing(conn: Conn, now: datetime, p0: datetime) -> None:
    """
    Insert one synthetic snapshot: $80/h for an 8-GPU H100 instance, A16 unpriced.

    Args:
        conn (Conn): Connection.
        now (datetime): Seed time.
        p0 (datetime): The §17 episode start (the snapshot is published a day before it).
    """
    published = p0 - D
    conn.execute(
        "INSERT INTO pricing_snapshots (snapshot_id, provider, region, purchase_option,"
        " captured_at, effective_at, checksum, fx_usd_ils, fx_date, parser_version, service,"
        " currency, operating_system, tenancy, billing_unit, publication_date, offer_version,"
        " map_version, catalog_version, imported_at, document)"
        " VALUES (%s, 'aws', 'us-east-1', 'OnDemand', %s, %s, %s, 2.0, %s, 'fixture', 'AmazonEC2',"
        " 'USD', 'Linux', 'Shared', 'Hrs', %s, 'fixture', 'fixture', 'fixture', %s, '{}')",
        (SNAPSHOT, published + H, published, "0" * 64, published.date(), published, now - D),
    )
    conn.execute(
        "INSERT INTO pricing_rates (snapshot_id, gpu_model, instance_type, instance_hourly_usd,"
        " instance_gpus, per_gpu_hour_usd, mapping, confidence, reason) VALUES"
        " (%s, %s, 'synthetic.48xlarge', 80.00, 8, 10.00, 'exact', 'high', 'synthetic'),"
        " (%s, %s, NULL, NULL, NULL, NULL, 'unpriced', NULL, 'synthetic: no equivalent')",
        (SNAPSHOT, H100, SNAPSHOT, A16),
    )
    for raw, canonical in ((H100_RAW, H100), (A16_RAW, A16)):
        conn.execute(
            "INSERT INTO gpu_model_aliases (raw_model, canonical, catalog_version, updated_at)"
            " VALUES (%s, %s, 'fixture', %s)",
            (raw, canonical, now - D),
        )
    conn.execute(
        "INSERT INTO pricing_selection (snapshot_id, selected_at, source)"
        " VALUES (%s, %s, 'latest')",
        (SNAPSHOT, now - D),
    )


def _seed_collector(conn: Conn, now: datetime) -> None:
    """
    Insert one complete profile-less collector read that sees the DC and the unread server.

    Args:
        conn (Conn): Connection.
        now (datetime): Seed time.
    """
    row = conn.execute(
        "INSERT INTO collector_reads (manager, kind, started_at, finished_at, complete)"
        " VALUES ('ucs-central-fixture', 'ucs_central', %s, %s, true) RETURNING id",
        (now - 20 * M, now - 19 * M),
    ).fetchone()
    assert row is not None
    for serial, gpus in (("fxdc0001", [H100_RAW] * 8), ("fxun0008", None)):
        gpu_json = None if gpus is None else Jsonb(gpus)  # SQL NULL = GPU inventory unread
        conn.execute(
            "INSERT INTO collector_servers (read_id, vendor, serial_normalized, source_id, name,"
            " gpu_models) VALUES (%s, 'cisco', %s, %s, %s, %s)",
            (row["id"], serial, f"sys/rack-unit-{serial}", f"fixture-{serial}", gpu_json),
        )
        conn.execute(
            "INSERT INTO collector_first_seen (vendor, serial_normalized, first_seen_at)"
            " VALUES ('cisco', %s, %s)",
            (serial, now - 5 * D),
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
    Run a variable query; return its `__value` column, else its first column, as text.

    Args:
        conn (Conn): Connection.
        sql (str): Rendered SQL.

    Returns:
        list[str]: Option values in result order.
    """
    rows = conn.execute(cast(LiteralString, sql)).fetchall()
    return [str(r["__value"] if "__value" in r else next(iter(r.values()))) for r in rows]


def _variables(
    conn: Conn,
    uid: str,
    ctx: QueryContext,
    values: Mapping[str, str | Sequence[str]] | None = None,
    *,
    selected: bool = False,
) -> QueryContext:
    """
    Add a dashboard's variables to a context, resolving defaults against the fixture.

    Args:
        conn (Conn): Connection.
        uid (str): Dashboard UID.
        ctx (QueryContext): Time range and interval.
        values (Mapping[str, str | Sequence[str]] | None): Explicit values by variable name.
        selected (bool): Pick each multi-value variable's first real option instead of All.

    Returns:
        QueryContext: The context with variables.
    """
    dashboard = DASHBOARDS[uid]
    resolver = partial(_first_column, conn)
    values = values or {}
    given: dict[str, str | Sequence[str]] = {"snapshot": SNAPSHOT, "asset_id": RB, **values}
    if selected:
        scope = dashboard_variables(dashboard, given, ctx=ctx, resolve=resolver)
        multi = {
            v["name"]
            for v in dashboard.get("templating", {}).get("list", [])
            if v.get("multi") or v.get("includeAll")
        }
        for q in variable_queries(dashboard):
            if q.ref_id in multi and q.ref_id not in values:
                options = resolver(render(q.sql, QueryContext(ctx.time_from, ctx.time_to, scope)))
                given[q.ref_id] = options[:1] or [""]
    variables = dashboard_variables(dashboard, given, ctx=ctx, resolve=resolver)
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
        assert "time" in columns, f"{query.label}: time_series format needs a 'time' column"
    return rows


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


@pytest.mark.parametrize("selected", [False, True], ids=["all", "selected"])
@pytest.mark.parametrize("query", QUERIES, ids=[q.label for q in QUERIES])
def test_panel_query_runs(db: tuple[Conn, Seed], query: PanelQuery, selected: bool) -> None:
    """Every Postgres panel query runs over the default range, with All and with one value."""
    conn, seed = db
    assert query.sql.strip(), f"{query.label}: Postgres target has no rawSql (use code mode)"
    ctx = _context(query, *time_range(DASHBOARDS[query.dashboard], seed.now))
    _execute(conn, query, _variables(conn, query.dashboard, ctx, selected=selected))


@pytest.mark.parametrize("query", VARIABLE_QUERIES, ids=[q.label for q in VARIABLE_QUERIES])
def test_variable_query_runs(db: tuple[Conn, Seed], query: PanelQuery) -> None:
    """Every Postgres variable query runs and returns at least one option on the fixture."""
    conn, seed = db
    ctx = QueryContext(*time_range(DASHBOARDS[query.dashboard], seed.now))
    rows = _execute(conn, query, _variables(conn, query.dashboard, ctx))
    assert rows, f"{query.label}: no options on the fixture"


def _panel(
    db: tuple[Conn, Seed],
    uid: str,
    panel_id: int,
    window: tuple[datetime, datetime] | None = None,
    **values: str | Sequence[str],
) -> list[dict[str, Any]]:
    """
    Run one panel's first query as Grafana would and return its rows.

    Args:
        db (tuple[Conn, Seed]): The seeded database.
        uid (str): Dashboard UID.
        panel_id (int): Panel id.
        window (tuple[datetime, datetime] | None): Time range; None = the dashboard default.
        **values (str | Sequence[str]): Variable values (default All, fixture snapshot).

    Returns:
        list[dict[str, Any]]: Rows.
    """
    conn, seed = db
    query = next(q for q in QUERIES if q.dashboard == uid and q.panel_id == panel_id)
    ctx = _context(query, *(window or time_range(DASHBOARDS[uid], seed.now)))
    return _execute(conn, query, _variables(conn, uid, ctx, values))


def _by(rows: Sequence[Mapping[str, Any]], key: str) -> dict[Any, Mapping[str, Any]]:
    """
    Index rows by one column.

    Args:
        rows (Sequence[Mapping[str, Any]]): Rows.
        key (str): Column name.

    Returns:
        dict[Any, Mapping[str, Any]]: Rows by that column's value.
    """
    return {r[key]: r for r in rows}


def _f(value: Any) -> float | None:
    """
    Convert a numeric cell (Decimal, int, float) to float, keeping NULL as None.

    Args:
        value (Any): Cell value.

    Returns:
        float | None: The number, or None.
    """
    return None if value is None else float(value)


def test_queues_idle_and_overdue_match_agent_17(db: tuple[Conn, Seed]) -> None:
    """§17: 8 GPUs idle 3 h with a 1 h objective = 24 idle, 16 overdue; 6 of 8 idle 2 h = 12."""
    p0 = db[1].p0
    rows = _by(_panel(db, "ttv-queues", 12, (p0 - H, p0 + 5 * H)), "Stage owner")
    assert _f(rows["Redbull"]["Idle GPU-h"]) == pytest.approx(24)
    assert _f(rows["Redbull"]["Overdue GPU-h"]) == pytest.approx(16)
    assert rows["Redbull"]["Stage clock"] == "observed"
    assert _f(rows["Serving"]["Idle GPU-h"]) == pytest.approx(12)
    assert _f(rows["Serving"]["Overdue GPU-h"]) == pytest.approx(0)
    assert _f(rows["DC"]["Idle GPU-h"]) == pytest.approx(0)
    # Synthetic $10 per GPU-hour ($80/h for an 8-GPU instance).
    assert _f(rows["Redbull"]["Idle value (USD)"]) == pytest.approx(240)
    assert _f(rows["Redbull"]["Overdue value (USD)"]) == pytest.approx(160)


def test_queues_clipped_range_counts_only_the_overlap(db: tuple[Conn, Seed]) -> None:
    """The same episode clipped to 00:30-02:30 gives 16 idle and 12 overdue GPU-hours."""
    p0 = db[1].p0
    rows = _by(_panel(db, "ttv-queues", 12, (p0 + H / 2, p0 + 5 * H / 2)), "Stage owner")
    assert _f(rows["Redbull"]["Idle GPU-h"]) == pytest.approx(16)
    assert _f(rows["Redbull"]["Overdue GPU-h"]) == pytest.approx(12)


def test_queues_historical_value_uses_the_snapshot_in_force(db: tuple[Conn, Seed]) -> None:
    """Present-day and historical USD agree when one snapshot was in force throughout."""
    p0 = db[1].p0
    rows = _by(_panel(db, "ttv-queues", 20, (p0 - H, p0 + 5 * H)), "Stage owner")
    assert _f(rows["Redbull"]["Idle USD, present-day"]) == pytest.approx(240)
    assert _f(rows["Redbull"]["Idle USD, historical"]) == pytest.approx(240)
    assert rows["Redbull"]["Historical snapshots"] == SNAPSHOT


def test_queues_idle_now_by_owner(db: tuple[Conn, Seed]) -> None:
    """Open IDLE intervals per stage owner, and the categories beside them."""
    rows = _by(_panel(db, "ttv-queues", 7), "stage_owner")
    assert {k: _f(v["idle_gpus"]) for k, v in rows.items()} == {
        "DC": 8, "Redbull": 8, "POPAI": 2, "Serving": 6,
    }  # fmt: skip
    assert _f(_panel(db, "ttv-queues", 8)[0]["gpus"]) == 10
    assert _f(_panel(db, "ttv-queues", 9)[0]["gpus"]) == 4
    assert _f(_panel(db, "ttv-queues", 10)[0]["gpus"]) == 2


def test_queues_value_is_linear_and_unpriced_is_not_zero(db: tuple[Conn, Seed]) -> None:
    """8 idle GPUs of an 8-GPU instance are worth one instance-hour; A16s stay unpriced."""
    redbull = _panel(db, "ttv-queues", 17, stage_owner=["Redbull"])[0]
    assert _f(redbull["usd_per_hour"]) == pytest.approx(80)
    assert _f(_panel(db, "ttv-queues", 17)[0]["usd_per_hour"]) == pytest.approx(220)
    assert _f(_panel(db, "ttv-queues", 18)[0]["gpus"]) == 2
    unpriced = _panel(db, "ttv-queues", 19)
    assert [(r["Canonical model"], _f(r["Idle GPUs"])) for r in unpriced] == [(A16, 2)]


def test_queues_unknown_is_never_zero(db: tuple[Conn, Seed]) -> None:
    """An empty scope gives no value (rendered as "No data"), never 0."""
    for panel_id in (8, 9, 10):
        assert _panel(db, "ttv-queues", panel_id, site=["no-such-site"])[0]["gpus"] is None
    empty = _panel(db, "ttv-queues", 5, site=["no-such-site"])[0]
    assert (empty["UNKNOWN GPUs"], empty["GPUs in scope"], empty["UNKNOWN share"]) == (None,) * 3
    coverage = _panel(db, "ttv-queues", 5)[0]
    assert _f(coverage["UNKNOWN GPUs"]) == 4
    assert _f(coverage["GPUs in scope"]) == 40
    assert coverage["Servers, GPU count unread"] == 1


def test_queues_longest_waits_mark_lower_bounds(db: tuple[Conn, Seed]) -> None:
    """A stage that began before observation shows "≥"; the annotation shows its owner."""
    rows = _by(_panel(db, "ttv-queues", 14), "Asset")
    assert rows["cisco:fxdc0001"]["Days in stage"].startswith("≥ ")
    assert rows["nvidia:fxdgx0004"]["Next-action owner"] == "Network"
    assert rows["nvidia:fxdgx0004"]["Stage owner"] == "Redbull"


def test_evidence_counts(db: tuple[Conn, Seed]) -> None:
    """Open identity conflicts, UNKNOWN GPUs by reason and the never-succeeded source."""
    conflicts = _panel(db, "ttv-evidence", 22)[0]
    assert (conflicts["Open conflicts"], conflicts["GPUs affected"]) == (1, 0)
    unknown = _by(_panel(db, "ttv-evidence", 25), "Reason")
    assert _f(unknown["SOURCE_STALE"]["UNKNOWN GPUs"]) == 4
    sources = _by(_panel(db, "ttv-evidence", 21), "Source")
    assert sources["collector:ucs-central-fixture"]["State"] == "never succeeded"
    assert sources["serverscan"]["State"] == "ok"
