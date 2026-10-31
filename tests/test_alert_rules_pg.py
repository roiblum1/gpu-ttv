"""Grafana alert rules (grafana/provisioning/alerting/ttv-rules.yaml) against a real Postgres.

Each test loads a rule's SQL from the provisioning file, runs it on synthetic ledger rows, applies
the rule's own threshold, and checks which alert instances would fire (ADR-0005, docs/runbooks.md).
Needs `TTV_TEST_DSN`; skipped otherwise. All data is synthetic (fixture-tested, not lab evidence).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from psycopg import sql
from psycopg.types.json import Jsonb

from conftest import DSN, server
from test_engine_pg import Clock, FakeSource, make_engine
from ttv.config import Policy
from ttv.sources.serverscan import ServerScanError
from ttv.store import PostgresStore

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")

RULES_FILE = Path(__file__).resolve().parents[1] / "grafana/provisioning/alerting/ttv-rules.yaml"
RULES = {r["uid"]: r for g in yaml.safe_load(RULES_FILE.read_text())["groups"] for r in g["rules"]}
NUMERIC_OIDS = {20, 21, 23, 700, 701, 1700}  # int8, int2, int4, float4, float8, numeric
TEXT_OIDS = {19, 25, 1043}  # name, text, varchar
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Instance:
    """One alert instance as Grafana would build it from a SQL row."""

    labels: dict[str, str]
    value: float
    firing: bool


async def frame(store: PostgresStore, sql: str) -> list[tuple[dict[str, str], float]]:
    """Run one rule query and convert it as Grafana does: one numeric column, text labels."""
    async with store.conn.cursor() as cur:
        await cur.execute(sql.encode())
        assert cur.description is not None
        cols = [(d.name, d.type_code) for d in cur.description]
        data: list[Any] = await cur.fetchall()
    numeric = [name for name, oid in cols if oid in NUMERIC_OIDS]
    assert len(numeric) == 1, f"exactly one numeric column, got {cols}"
    assert all(oid in TEXT_OIDS for name, oid in cols if name not in numeric), cols
    out = []
    for row in data:
        labels = {k: v for k, v in row.items() if k != numeric[0]}
        assert None not in labels.values(), f"NULL label in {row}"
        assert row[numeric[0]] is not None, f"NULL value in {row}"
        out.append((labels, float(row[numeric[0]])))
    keys = [tuple(sorted(lbl.items())) for lbl, _ in out]
    assert len(keys) == len(set(keys)), "duplicate label sets"
    return out


def query(uid: str, ref: str) -> str:
    """The raw SQL of one query of a rule."""
    return next(q for q in RULES[uid]["data"] if q["refId"] == ref)["model"]["rawSql"]


def threshold(uid: str) -> tuple[str, str, float]:
    """The condition's input refId, evaluator type and parameter."""
    rule = RULES[uid]
    model = next(q for q in rule["data"] if q["refId"] == rule["condition"])["model"]
    assert model["type"] == "threshold"
    evaluator = model["conditions"][0]["evaluator"]
    return model["expression"], evaluator["type"], float(evaluator["params"][0])


async def evaluate(store: PostgresStore, uid: str) -> list[Instance]:
    """Evaluate a rule's condition on the current rows (empty list = Grafana's NoData)."""
    ref, kind, param = threshold(uid)
    compare = {"gt": lambda v: v > param, "lt": lambda v: v < param}[kind]
    frames = {
        q["refId"]: await frame(store, q["model"]["rawSql"])
        for q in RULES[uid]["data"]
        if "rawSql" in q["model"]
    }
    if not all(frames.values()):
        return []  # Grafana: one query without rows makes the whole rule NoData (lab-tested)
    for other, rows in frames.items():  # annotations find their row: labels contain the instance's
        for labels, _ in frames[ref]:
            assert any(labels.items() <= row.items() for row, _ in rows), (uid, other, labels)
    return [Instance(lbl, v, compare(v)) for lbl, v in frames[ref]]


async def by(store: PostgresStore, uid: str, label: str) -> dict[str, Instance]:
    """Evaluate a rule and index its instances by one label."""
    return {i.labels[label]: i for i in await evaluate(store, uid)}


async def detail(store: PostgresStore, uid: str, label: str | None = None) -> dict[str, Any]:
    """Run a rule's annotation query B, indexed by one label (or the single row)."""
    rows = await frame(store, query(uid, "B"))
    if label is None:
        assert len(rows) == 1
        return {"": {**rows[0][0], "value": rows[0][1]}}
    return {lbl[label]: {**lbl, "value": v} for lbl, v in rows}


async def db_now(store: PostgresStore) -> datetime:
    """The database clock: rule SQL measures ages with now()."""
    row: Any = await (await store.conn.execute("SELECT now() AS now")).fetchone()
    return row["now"]


async def scan(store: PostgresStore, finished: datetime, status: str = "ok") -> None:
    """Record an engine scan that finished at `finished`."""
    await store.conn.execute(
        "INSERT INTO scan_runs (kind, started_at, finished_at, status)"
        " VALUES ('engine', %s, %s, %s)",
        (finished - MIN, finished, status),
    )


async def source(
    store: PostgresStore, name: str, failed: int, attempt: datetime, error: str | None = None
) -> None:
    """Record a source's health as the engine would."""
    await store.conn.execute(
        "INSERT INTO source_state"
        " (name, failed_scans, last_success_at, last_attempt_at, last_error)"
        " VALUES (%s, %s, %s, %s, %s)",
        (name, failed, attempt - failed * 5 * MIN, attempt, error),
    )


async def interval(
    store: PostgresStore,
    asset_id: str,
    category: str,
    reason: str,
    gpus: int,
    started: datetime,
    **extra: Any,
) -> None:
    """Insert one capacity interval (and its asset); `extra` sets any other column."""
    await store.conn.execute(
        "INSERT INTO assets (asset_id, name, first_observed_at, last_observed_at)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (asset_id, f"host-{asset_id}", started, started),
    )
    row = {
        "asset_id": asset_id,
        "category": category,
        "reason_code": reason,
        "gpus": gpus,
        "started_at": started,
        "policy_version": "test-1",
        **extra,
    }
    if "evidence" in row:
        row["evidence"] = Jsonb(row["evidence"])
    insert = sql.SQL("INSERT INTO capacity_intervals ({}) VALUES ({})").format(
        sql.SQL(", ").join(map(sql.Identifier, row)), sql.SQL(", ").join(map(sql.Placeholder, row))
    )
    await store.conn.execute(insert, row)


async def collector_read(
    store: PostgresStore, manager: str, finished: datetime, *, complete: bool, error: str = ""
) -> None:
    """Record one collector read of a manager."""
    await store.conn.execute(
        "INSERT INTO collector_reads (manager, kind, started_at, finished_at, complete, errors)"
        " VALUES (%s, 'openmanage', %s, %s, %s, %s)",
        (manager, finished - 2 * MIN, finished, complete, Jsonb([error] if error else [])),
    )


async def age_migrations(store: PostgresStore, by_: timedelta) -> None:
    """Pretend the ledger schema was created `by_` ago."""
    await store.conn.execute("UPDATE schema_migrations SET applied_at = applied_at - %s", (by_,))


# --- Monitoring blindness -------------------------------------------------------------------


async def test_engine_scans_stale_fires_only_without_a_recent_ok_or_partial_scan(
    store: PostgresStore,
) -> None:
    uid = "ttv-engine-scans-stale"
    now = await db_now(store)
    await scan(store, now - 2 * MIN)
    [fresh] = await evaluate(store, uid)
    assert not fresh.firing
    assert fresh.value == pytest.approx(2, abs=0.2)

    # A failed and a dry-run scan are newer, but neither updated the ledger.
    await store.conn.execute("DELETE FROM scan_runs")
    await scan(store, now - 40 * MIN, "partial")
    await scan(store, now - 3 * MIN, "failed")
    await scan(store, now - MIN, "dry_run")
    [stale] = await evaluate(store, uid)
    assert stale.firing
    assert stale.value == pytest.approx(40, abs=0.2)
    b = (await detail(store, uid))[""]
    assert b["newest_scan"].startswith("dry_run, started ")
    assert b["value"] == 3  # engine scans started in the last hour


async def test_engine_never_scanned_counts_from_ledger_creation_not_zero(
    store: PostgresStore,
) -> None:
    uid = "ttv-engine-scans-stale"
    [young] = await evaluate(store, uid)
    assert not young.firing  # ledger created moments ago
    await age_migrations(store, 2 * HOUR)
    [never] = await evaluate(store, uid)
    assert never.firing
    assert never.value == pytest.approx(120, abs=1)
    b = (await detail(store, uid))[""]
    assert (b["last_good_scan"], b["newest_scan"]) == ("never", "no engine scan recorded")


async def test_source_failing_past_grace_fires_per_source_with_unknown_gpus(
    store: PostgresStore,
) -> None:
    uid = "ttv-source-failing-past-grace"
    now = await db_now(store)
    await source(store, "serverscan", 3, now - MIN, "serverscan: HTTP 503")
    await source(store, "kube:c1", 1, now - MIN, "kube:c1: timeout")
    await source(store, "thanos:c1", 0, now - MIN)
    await source(store, "kube:retired", 9, now - 2 * HOUR, "gone")  # no longer read
    stale = {"record_stale": False, "source_failed_scans": 3}
    await interval(store, "a1", "UNKNOWN", "SOURCE_STALE", 8, now - HOUR, evidence=stale)
    record = {"record_stale": True, "source_failed_scans": 0}  # stale record, not the source
    await interval(store, "a2", "UNKNOWN", "SOURCE_STALE", 4, now - HOUR, evidence=record)

    got = await by(store, uid, "source")
    assert {k: (v.value, v.firing) for k, v in got.items()} == {
        "serverscan": (3, True),
        "kube:c1": (1, False),  # within grace: its assets keep their last classification
        "thanos:c1": (0, False),
    }
    b = await detail(store, uid, "source")
    assert b["serverscan"]["value"] == 8
    assert b["serverscan"]["last_error"] == "serverscan: HTTP 503"


async def test_cluster_source_counts_its_unreachable_gpus(store: PostgresStore) -> None:
    uid = "ttv-source-failing-past-grace"
    now = await db_now(store)
    await source(store, "kube:c1", 2, now - MIN)
    await source(store, "kube:c2", 0, now - MIN)
    for i, cluster in enumerate(("c1", "c1", "c2")):
        evidence = {"cluster_name": cluster, "lifecycle_state": "INSTALLED"}
        await interval(
            store, f"n{i}", "UNKNOWN", "CLUSTER_UNREACHABLE", 4, now - HOUR, evidence=evidence
        )
    assert (await by(store, uid, "source"))["kube:c1"].firing
    b = await detail(store, uid, "source")
    assert (b["kube:c1"]["value"], b["kube:c2"]["value"]) == (8, 4)


async def test_collector_manager_stale_per_manager_and_never_read(store: PostgresStore) -> None:
    uid = "ttv-collector-manager-stale"
    [none] = await evaluate(store, uid)
    assert none.labels["manager"] == "(no collector reads)"
    assert not none.firing
    await age_migrations(store, 3 * HOUR)
    [none] = await evaluate(store, uid)
    assert none.firing  # no read for 3 h: DC's stage is unobserved, not empty

    now = await db_now(store)
    await collector_read(store, "ucs-a", now - 10 * MIN, complete=True)
    await collector_read(store, "ome-b", now - 2 * HOUR, complete=True)
    await collector_read(store, "ome-b", now - 5 * MIN, complete=False, error="HTTP 401")
    await collector_read(store, "ome-c", now - 90 * MIN, complete=False, error="timeout")
    evidence = {"manager": "ome-b", "source_failed_scans": 2}
    await interval(store, "d1", "UNKNOWN", "SOURCE_STALE", 8, now - HOUR, evidence=evidence)

    got = await by(store, uid, "manager")
    assert set(got) == {"ucs-a", "ome-b", "ome-c"}  # the "never read" row is gone
    assert not got["ucs-a"].firing
    assert got["ome-b"].firing and got["ome-b"].value == pytest.approx(120, abs=1)
    assert got["ome-c"].firing and got["ome-c"].value == pytest.approx(92, abs=1)
    b = await detail(store, uid, "manager")
    assert b["ome-b"]["last_attempt"].endswith("(incomplete)")
    assert "HTTP 401" in b["ome-b"]["last_errors"]
    assert b["ome-b"]["value"] == 8
    assert b["ome-c"]["last_complete_read"] == "never"


async def test_unknown_share_high(store: PostgresStore) -> None:
    uid = "ttv-unknown-share-high"
    assert await evaluate(store, uid) == []  # empty ledger: Grafana raises DatasourceNoData
    assert RULES[uid]["noDataState"] == "NoData"
    now = await db_now(store)
    await interval(store, "i", "IDLE", "NOT_IN_CLUSTER", 8, now - HOUR, stage_owner="Redbull")
    await interval(store, "n", "NOT_CHARGED", "EXCLUDED", 2, now - HOUR)
    await interval(store, "u", "UNKNOWN", "IDENTITY_CONFLICT", 1, now - HOUR)
    [low] = await evaluate(store, uid)
    assert not low.firing
    assert low.value == pytest.approx(100 / 11)  # every category is in scope, as on Queues panel 5
    await interval(store, "u2", "UNKNOWN", "SOURCE_STALE", 3, now - HOUR)
    [high] = await evaluate(store, uid)
    assert high.firing
    assert high.value == pytest.approx(100 * 4 / 14)
    b = (await detail(store, uid))[""]
    assert b["by_reason"] == "SOURCE_STALE 3, IDENTITY_CONFLICT 1"
    assert b["servers_gpu_count_unread"] == "unknown"  # no engine scan recorded: not zero


async def test_unknown_share_growing_compares_now_with_an_hour_ago(
    store: PostgresStore,
) -> None:
    uid = "ttv-unknown-share-growing"
    now = await db_now(store)
    await interval(store, "s", "IDLE", "NO_WORKLOAD", 12, now - 3 * HOUR, stage_owner="Serving")
    assert await evaluate(store, uid) != []
    start = now - 3 * HOUR
    await interval(
        store, "c", "IDLE", "NO_WORKLOAD", 8, start, stage_owner="Serving", ended_at=now - 20 * MIN
    )
    await interval(store, "c", "UNKNOWN", "CLUSTER_UNREACHABLE", 8, now - 20 * MIN)
    [grown] = await evaluate(store, uid)
    assert grown.firing
    assert grown.value == pytest.approx(40)  # 0 % an hour ago, 8 of 20 GPUs now
    b = (await detail(store, uid))[""]
    assert (b["value"], b["unknown_gpus_1h_ago"]) == (8, "0")
    assert b["change_by_reason"] == "CLUSTER_UNREACHABLE +8"


async def test_unknown_share_growing_needs_an_hour_of_history(store: PostgresStore) -> None:
    now = await db_now(store)
    await interval(store, "u", "UNKNOWN", "SOURCE_STALE", 8, now - 10 * MIN)
    assert await evaluate(store, "ttv-unknown-share-growing") == []  # NoData: state is held
    assert RULES["ttv-unknown-share-growing"]["noDataState"] == "KeepLast"


async def test_handover_overdue_per_manager_only_while_serverscan_is_read(
    store: PostgresStore,
) -> None:
    uid = "ttv-handover-overdue"
    now = await db_now(store)
    await source(store, "serverscan", 0, now - MIN)
    await collector_read(store, "ome-1", now - 5 * MIN, complete=True)
    await collector_read(store, "ucs-2", now - 5 * MIN, complete=True)
    since = (now - 9 * HOUR).isoformat()
    overdue = {"manager": "ome-1", "profile_name": "ocp-gpu", "name": "xe9680-1"}
    await interval(
        store, "h1", "UNKNOWN", "HANDOVER_PENDING", 8, now - 2 * HOUR,
        flags=["handover_overdue"], evidence=overdue | {"ocp_profile_since": since},
    )  # fmt: skip
    fresh = {"manager": "ucs-2", "profile_name": "ocp-gpu", "ocp_profile_since": since}
    await interval(store, "h2", "UNKNOWN", "HANDOVER_PENDING", 4, now - HOUR, evidence=fresh)

    got = await by(store, uid, "manager")
    assert {k: (v.value, v.firing) for k, v in got.items()} == {
        "ome-1": (8, True),
        "ucs-2": (0, False),  # within handover_grace: not flagged yet
    }
    b = await detail(store, uid, "manager")
    assert b["ome-1"]["first_asset"] == "h1"
    assert "h1 (xe9680-1, 8 GPUs, profile ocp-gpu since " in b["ome-1"]["oldest_assets"]

    # A server-scan read failure could make every collector server look unlisted: hold state.
    await store.conn.execute("UPDATE source_state SET failed_scans = 1")
    assert await evaluate(store, uid) == []
    assert RULES[uid]["noDataState"] == "KeepLast"


async def test_node_not_found_per_cluster(store: PostgresStore) -> None:
    uid = "ttv-node-not-found"
    now = await db_now(store)
    await source(store, "kube:c1", 0, now - MIN)
    await source(store, "kube:c2", 0, now - MIN)
    for asset, gpus in (("x1", 8), ("x2", 4)):
        evidence = {"cluster": "c1", "node_name": f"gpu-{asset}"}
        await interval(
            store, asset, "UNKNOWN", "NODE_NOT_FOUND", gpus, now - HOUR, evidence=evidence
        )
    got = await by(store, uid, "cluster")
    assert {k: (v.value, v.firing) for k, v in got.items()} == {"c1": (12, True), "c2": (0, False)}
    b = await detail(store, uid, "cluster")
    assert b["c1"]["value"] == 2
    assert "expected node gpu-x1" in b["c1"]["oldest_assets"]


# --- Stage-objective breaches ---------------------------------------------------------------


async def breach_ledger(store: PostgresStore, policy: Policy, now: datetime) -> None:
    """Redbull: 8 GPUs idle for 3 h against a 1 h objective (§17); DC within its 3 d objective."""
    await store.sync_objectives(policy)
    await scan(store, now - 2 * MIN)
    await source(store, "serverscan", 0, now - 2 * MIN)
    entered = now - 3 * HOUR
    redbull = {"stage": "PROVISIONING", "stage_owner": "Redbull", "stage_entered_at": entered}
    await interval(
        store, "rb", "IDLE", "NODE_NOT_READY", 8, entered, ended_at=now - 90 * MIN, **redbull
    )
    await interval(store, "rb", "IDLE", "NOT_IN_CLUSTER", 8, now - 90 * MIN, **redbull)
    dc = {"stage": "PRE_PROVISIONING", "stage_owner": "DC", "stage_entered_at": now - HOUR}
    await interval(store, "dc", "IDLE", "MAINTENANCE", 4, now - HOUR, **dc)
    await interval(store, "u", "UNKNOWN", "SOURCE_STALE", 8, now - 5 * HOUR)


async def test_stage_breach_one_instance_per_owner_with_overdue_gpu_hours(
    store: PostgresStore, policy: Policy
) -> None:
    uid = "ttv-stage-objective-breached"
    now = await db_now(store)
    await breach_ledger(store, policy, now)
    got = await by(store, uid, "stage_owner")
    assert {k: (v.value, v.firing) for k, v in got.items()} == {
        "Redbull": (8, True),
        "DC": (0, False),
    }
    b = await detail(store, uid, "stage_owner")
    # §17: 24 idle GPU-h, of which 16 overdue, summed across both intervals of the episode.
    assert b["Redbull"]["value"] == pytest.approx(16, abs=0.05)
    assert b["Redbull"]["lower_bound"] == ""
    assert b["Redbull"]["first_asset"] == "rb"
    assert (
        "rb (host-rb): 8 GPUs NOT_IN_CLUSTER, 0.1 d in stage vs 01:00:00 objective"
        in (b["Redbull"]["top_assets"])
    )
    assert (b["DC"]["value"], b["DC"]["top_assets"], b["DC"]["first_asset"]) == (0, "none", "")


async def test_next_action_annotation_never_moves_the_breach(
    store: PostgresStore, policy: Policy
) -> None:
    uid = "ttv-stage-objective-breached"
    now = await db_now(store)
    await breach_ledger(store, policy, now)
    await store.conn.execute(
        "INSERT INTO next_action_annotations (asset_id, next_action_owner, reason, created_by,"
        " expires_at) VALUES ('rb', 'Network', 'PXE VLAN missing', 'test', %s)",
        (now + HOUR,),
    )
    got = await by(store, uid, "stage_owner")
    assert (got["Redbull"].value, got["Redbull"].firing) == (8, True)
    assert "Network" not in got
    assert (
        "next action: Network" in (await detail(store, uid, "stage_owner"))["Redbull"]["top_assets"]
    )


async def test_lower_bound_stage_clock_marks_gpu_hours(
    store: PostgresStore, policy: Policy
) -> None:
    uid = "ttv-stage-objective-breached"
    now = await db_now(store)
    await breach_ledger(store, policy, now)
    await store.conn.execute("UPDATE capacity_intervals SET stage_entered_lower_bound = true")
    b = await detail(store, uid, "stage_owner")
    assert b["Redbull"]["lower_bound"] == "≥ "
    assert "≥ 0.1 d in stage" in b["Redbull"]["top_assets"]


async def test_no_idle_gpus_resolves_through_a_zero_row_not_nodata(
    store: PostgresStore, policy: Policy
) -> None:
    now = await db_now(store)
    await store.sync_objectives(policy)
    await scan(store, now - MIN)
    await interval(store, "p", "PRODUCTIVE", "RUNAI_WORKLOAD", 8, now - HOUR)
    [only] = await evaluate(store, "ttv-stage-objective-breached")
    assert (only.labels, only.value, only.firing) == ({"stage_owner": "(no idle GPUs)"}, 0, False)


async def test_stale_engine_or_serverscan_holds_stage_breach_state(
    store: PostgresStore, policy: Policy
) -> None:
    uid = "ttv-stage-objective-breached"
    now = await db_now(store)
    await breach_ledger(store, policy, now)
    assert (await by(store, uid, "stage_owner"))["Redbull"].firing

    # Blind: no ok/partial scan for 20 min. The overdue clock keeps running in SQL, so the
    # rule returns nothing (NoData) and Grafana holds every instance in its last state.
    await store.conn.execute(
        "UPDATE scan_runs SET finished_at = finished_at - interval '20 minutes'"
    )
    assert await evaluate(store, uid) == []
    await scan(store, now - MIN)
    await store.conn.execute("UPDATE source_state SET failed_scans = 2")
    assert await evaluate(store, uid) == []
    assert (RULES[uid]["noDataState"], RULES[uid]["execErrState"]) == ("KeepLast", "KeepLast")


async def test_source_stale_never_raises_a_stage_breach_end_to_end(
    store: PostgresStore, policy: Policy
) -> None:
    # The engine turns a failing source's GPUs UNKNOWN after grace, so they leave the stage
    # queue: no stage owner is accused, and the data-quality rule fires instead.
    uid = "ttv-stage-objective-breached"
    await store.sync_objectives(policy)  # PROVISIONING objective: 1 h
    now = await db_now(store)
    source_, clock = FakeSource(), Clock()
    engine = make_engine(store, policy, source_, clock)
    available = server(claim_changed_at=now - 3 * HOUR, created_at=now - 3 * HOUR)
    source_.answers = [[available], [available]]
    for at in (now - 3 * HOUR, now - 4 * MIN):
        clock.now = at
        await engine.run_once()
    assert (await by(store, uid, "stage_owner"))["Redbull"].firing  # 8 GPUs, 2 h overdue

    source_.answers = [ServerScanError("HTTP 503"), ServerScanError("HTTP 503")]
    for at in (now - 3 * MIN, now - 2 * MIN):
        clock.now = at
        await engine.run_once()
    reasons = await (
        await store.conn.execute(
            "SELECT category, reason_code FROM capacity_intervals WHERE ended_at IS NULL"
        )
    ).fetchall()
    assert [(r["category"], r["reason_code"]) for r in reasons] == [("UNKNOWN", "SOURCE_STALE")]
    assert await evaluate(store, uid) == []  # gate closed: state held, nothing new raised
    # Without the gate, no team would be charged either: no GPU is left in any stage queue.
    assert set(await detail(store, uid, "stage_owner")) == {"(no idle GPUs)"}
    failing = await by(store, "ttv-source-failing-past-grace", "source")
    assert failing["serverscan"].firing
    assert (await detail(store, "ttv-source-failing-past-grace", "source"))["serverscan"][
        "value"
    ] == 8
    stale = RULES["ttv-source-failing-past-grace"]["labels"]
    assert (stale["kind"], stale["owner"]) == ("data_quality", "platform")
