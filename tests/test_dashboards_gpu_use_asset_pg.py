"""The GPU use (E12) and Asset dashboards' SQL against a synthetic ledger in a real Postgres.

Every panel and variable query runs as Grafana would send it (macros and variables substituted);
key panels are checked against hand-computed numbers. Needs `TTV_TEST_DSN`; skipped otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, LiteralString, cast

import pytest

from conftest import DSN
from ttv.store import PostgresStore

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")

DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
FROM, TO = "2026-10-01T00:00:00Z", "2026-10-01T12:00:00Z"

# Synthetic ledger (fixture-tested only). Measured cluster c1; 32 GPUs in all:
#   syn-a  8 GPUs on a GPU-ready node: 2 SERVING_IN_USE, 2 RUNAI allocated_idle, 1 NO_REAL_DEMAND,
#          1 NO_WORKLOAD, 1 GPU_MISSING, 1 TRAFFIC_UNOBSERVED; before 06:00 all 8 platform-not-ready
#   syn-b  4 NODE_NOT_FOUND in c1; syn-f 4 IDENTITY_CONFLICT (NODE_CLAIM on c1)  -> use unknown
#   syn-c  8 NOT_IN_CLUSTER; syn-d 4 OUTSIDE_TARGET_CLUSTERS; syn-e 4 PROFILE_NONE -> not measured
FIXTURE = """
INSERT INTO scan_runs (kind, started_at, finished_at, status)
VALUES ('engine', '2026-10-01 11:55+00', '2026-10-01 11:56+00', 'ok');
INSERT INTO assets (asset_id, aliases, vendor, serial, name, model, site, gpu_model, gpu_count,
                    source, first_observed_at, last_observed_at) VALUES
 ('cisco:syn-a', '{"serverscan_id": "srv-a"}', 'cisco', 'syn-a', 'gpu-node-a', 'SYN-8GPU', 's1',
  'SYN-GPU', 8, 'UCS_CENTRAL', '2026-10-01 00:00+00', '2026-10-01 12:00+00'),
 ('cisco:syn-b', '{}', 'cisco', 'syn-b', 'gpu-node-b', 'SYN-4GPU', 's1', 'SYN-GPU', 4,
  'UCS_CENTRAL', '2026-10-01 00:00+00', '2026-10-01 12:00+00'),
 ('dell:syn-c', '{}', 'dell', 'syn-c', 'spare-c', 'SYN-8GPU', 's2', 'SYN-GPU', 8, 'OPENMANAGE',
  '2026-10-01 00:00+00', '2026-10-01 12:00+00'),
 ('dell:syn-d', '{}', 'dell', 'syn-d', 'other-d', 'SYN-4GPU', 's2', 'SYN-GPU', 4, 'OPENMANAGE',
  '2026-10-01 00:00+00', '2026-10-01 12:00+00'),
 ('hp:syn-e', '{"manager": "ucs-central-syn", "collector_source_id": "ls-e"}', 'hp', 'syn-e',
  'blade-e', 'SYN-4GPU', NULL, 'SYN-GPU', 4, 'ucs-central-syn', '2026-10-01 00:00+00',
  '2026-10-01 12:00+00'),
 ('dell:syn-f', '{}', 'dell', 'syn-f', 'gpu-node-f', 'SYN-4GPU', 's1', 'SYN-GPU', 4, 'OPENMANAGE',
  '2026-10-01 00:00+00', '2026-10-01 12:00+00');
INSERT INTO capacity_intervals (asset_id, category, reason_code, stage, stage_owner,
  stage_entered_at, stage_entered_lower_bound, cluster, holder_ref, team, gpu_model, gpus, flags,
  started_at, ended_at, evidence, policy_version) VALUES
 ('cisco:syn-a', 'IDLE', 'GPU_PLATFORM_NOT_READY', 'GPU_PLATFORM', 'POPAI', '2026-10-01 00:00+00',
  true, 'c1', NULL, NULL, 'SYN-GPU', 8, '{}', '2026-10-01 00:00+00', '2026-10-01 06:00+00',
  '{"node": "gpu-node-a", "A": 8, "R": 0}', 'syn-1'),
 ('cisco:syn-a', 'PRODUCTIVE', 'SERVING_IN_USE', NULL, NULL, NULL, false, 'c1',
  'ns1/Deployment/chat', 'Applai', 'SYN-GPU', 2,
  '{low_use_unverified,user_attribution_unverified}', '2026-10-01 06:00+00', NULL,
  '{"counter": "vllm:request_success_total", "requests": 36.0, "window_hours": 1.0,
    "requests_per_gpu_hour": 18.0, "deployment_gpus": 2}', 'syn-1'),
 ('cisco:syn-a', 'PRODUCTIVE', 'RUNAI_WORKLOAD', NULL, NULL, NULL, false, 'c1',
  'ns2/TrainingWorkload/t1', 'research', 'SYN-GPU', 2, '{allocated_idle}', '2026-10-01 06:00+00',
  NULL, '{"runai_kind": "TrainingWorkload"}', 'syn-1'),
 ('cisco:syn-a', 'IDLE', 'NO_REAL_DEMAND', 'SERVING', 'Serving', '2026-10-01 06:00+00', false,
  'c1', 'ns1/Deployment/idle-chat', NULL, 'SYN-GPU', 1, '{user_attribution_unverified}',
  '2026-10-01 06:00+00', NULL, '{"counter": "vllm:request_success_total", "requests": 0.0,
  "window_hours": 24.0, "requests_per_gpu_hour": 0.0}', 'syn-1'),
 ('cisco:syn-a', 'IDLE', 'NO_WORKLOAD', 'SERVING', 'Serving', '2026-10-01 06:00+00', false, 'c1',
  NULL, NULL, 'SYN-GPU', 1, '{}', '2026-10-01 06:00+00', NULL, '{"free_gpus": 1}', 'syn-1'),
 ('cisco:syn-a', 'IDLE', 'GPU_MISSING', 'GPU_PLATFORM', 'POPAI', '2026-10-01 00:00+00', true,
  'c1', NULL, NULL, 'SYN-GPU', 1, '{}', '2026-10-01 06:00+00', NULL,
  '{"E": 8, "A": 8, "R": 7, "last_xid": {"GPU-syn-7": 79.0}}', 'syn-1'),
 ('cisco:syn-a', 'UNKNOWN', 'TRAFFIC_UNOBSERVED', NULL, NULL, NULL, false, 'c1',
  'ns3/Deployment/unmonitored', NULL, 'SYN-GPU', 1, '{}', '2026-10-01 06:00+00', NULL,
  '{"tried": ["vllm:request_success_total"]}', 'syn-1'),
 ('cisco:syn-b', 'UNKNOWN', 'NODE_NOT_FOUND', NULL, NULL, NULL, false, NULL, NULL, NULL,
  'SYN-GPU', 4, '{}', '2026-10-01 00:00+00', NULL,
  '{"cluster": "c1", "node_name": "gpu-node-b"}', 'syn-1'),
 ('dell:syn-c', 'IDLE', 'NOT_IN_CLUSTER', 'PROVISIONING', 'Redbull', '2026-10-01 00:00+00', true,
  NULL, NULL, NULL, 'SYN-GPU', 8, '{}', '2026-10-01 00:00+00', NULL,
  '{"lifecycle_state": "AVAILABLE"}', 'syn-1'),
 ('dell:syn-d', 'NOT_CHARGED', 'OUTSIDE_TARGET_CLUSTERS', NULL, NULL, NULL, false, NULL, NULL,
  NULL, 'SYN-GPU', 4, '{}', '2026-10-01 00:00+00', NULL,
  '{"lifecycle_state": "INSTALLED", "cluster_name": "x9"}', 'syn-1'),
 ('hp:syn-e', 'IDLE', 'PROFILE_NONE', 'PRE_PROVISIONING', 'DC', '2026-10-01 00:00+00', true, NULL,
  NULL, NULL, 'SYN-GPU', 4, '{}', '2026-10-01 00:00+00', NULL,
  '{"manager": "ucs-central-syn", "source_id": "ls-e"}', 'syn-1'),
 ('dell:syn-f', 'UNKNOWN', 'IDENTITY_CONFLICT', NULL, NULL, NULL, false, NULL, NULL, NULL,
  'SYN-GPU', 4, '{}', '2026-10-01 00:00+00', NULL,
  '{"conflict": "node c1/node-7 also claimed by dell:syn-g"}', 'syn-1');
INSERT INTO milestones (asset_id, milestone, observed_at, lower_bound, evidence) VALUES
 ('cisco:syn-a', 'first_seen', '2026-10-01 00:00+00', true, '{"source": "ttv-engine"}'),
 ('cisco:syn-a', 'in_serverscan', '2026-09-20 08:00+00', false,
  '{"source": "serverscan.created_at"}'),
 ('cisco:syn-a', 'gpu_ready', '2026-10-01 06:00+00', true, '{"A": 8}');
INSERT INTO identity_conflicts (kind, key, candidates, first_seen, last_seen) VALUES
 ('NODE_CLAIM', 'c1/node-7', '["dell:syn-f", "dell:syn-g"]', '2026-10-01 00:00+00',
  '2026-10-01 12:00+00');
INSERT INTO next_action_annotations (asset_id, next_action_owner, reason, created_by, created_at,
                                     expires_at) VALUES
 ('cisco:syn-a', 'Network', 'synthetic: VLAN for node b', 'tester', '2026-10-01 07:00+00',
  '2099-01-01 00:00+00');
INSERT INTO collector_reads (id, manager, kind, started_at, finished_at, complete) VALUES
 (1, 'ucs-central-syn', 'ucs_central', '2026-10-01 10:00+00', '2026-10-01 10:01+00', true),
 (2, 'ucs-central-syn', 'ucs_central', '2026-10-01 11:00+00', '2026-10-01 11:01+00', true);
INSERT INTO collector_servers (read_id, vendor, serial_normalized, source_id, name, model,
                               profile_name, gpu_models) VALUES
 (1, 'hp', 'syn-e', 'ls-e', 'blade-e', 'SYN-4GPU', NULL, '["SYN-GPU", "SYN-GPU"]'),
 (2, 'hp', 'syn-e', 'ls-e', 'blade-e', 'SYN-4GPU', NULL,
  '["SYN-GPU", "SYN-GPU", "SYN-GPU", "SYN-GPU"]');
"""


def dashboard(uid: str) -> dict[str, Any]:
    """Load a dashboard JSON by UID."""
    return json.loads((DASHBOARDS / f"{uid}.json").read_text())


def panel_sql(uid: str, panel_id: int) -> str:
    """The raw SQL of one panel's only target."""
    panel = next(p for p in dashboard(uid)["panels"] if p["id"] == panel_id)
    return panel["targets"][0]["rawSql"]


def render(
    sql: str, *, site: str = "'__all__'", cluster: str = "'__all__'", asset: str = ""
) -> str:
    """Substitute macros and variables the way Grafana does for this datasource."""
    return (
        sql.replace("$__timeFrom()", f"'{FROM}'")
        .replace("$__timeTo()", f"'{TO}'")
        .replace("$__interval_ms", "3600000")
        .replace("$site", site)
        .replace("$cluster", cluster)
        .replace("$asset_id", asset.replace("'", "''"))
    )


async def query(store: PostgresStore, sql: str, **variables: str) -> list[dict[str, Any]]:
    """Run one rendered dashboard query and return dict rows."""
    cur = await store.conn.execute(cast(LiteralString, render(sql, **variables)))
    return list(await cur.fetchall())


@pytest.fixture
async def ledger(store: PostgresStore) -> PostgresStore:
    """The migrated store with the synthetic ledger loaded."""
    await store.conn.execute(FIXTURE)
    return store


async def test_every_query_runs_with_all_and_with_filters(ledger: PostgresStore) -> None:
    for uid in ("ttv-gpu-use", "ttv-asset"):
        dash = dashboard(uid)
        sqls = [v["query"] for v in dash["templating"]["list"] if v["type"] == "query"]
        sqls += [t["rawSql"] for p in dash["panels"] for t in p.get("targets", [])]
        for variables in (
            {},
            {"site": "'s1'", "cluster": "'c1'", "asset": "cisco:syn-a"},
            {"site": "'(none)'", "cluster": "'(none)','x9'", "asset": "hp:syn-e"},
            {"asset": "no:such-asset"},
        ):
            for sql in sqls:
                await query(ledger, sql, **variables)


async def test_use_classes_partition_the_measured_gpus(ledger: PostgresStore) -> None:
    [classes] = await query(ledger, panel_sql("ttv-gpu-use", 8))
    assert classes == {
        "Useful work": 2,
        "Allocated, inactive": 2,
        "Waste candidate": 2,
        "Constrained or failed": 1,
        "Use unknown": 9,
    }
    [share] = await query(ledger, panel_sql("ttv-gpu-use", 2))
    assert share["useful_share"] == pytest.approx(2 / 16)
    [waste] = await query(ledger, panel_sql("ttv-gpu-use", 3))
    assert waste["waste_gpus"] == 4
    [nm] = await query(ledger, panel_sql("ttv-gpu-use", 6))
    assert nm["not_measured_share"] == pytest.approx(16 / 32)


async def test_a_loaded_model_is_not_useful_work(ledger: PostgresStore) -> None:
    [ladder] = await query(ledger, panel_sql("ttv-gpu-use", 9))
    assert list(ladder.values()) == [8, 7, 6, 6, 4, 2]


async def test_unknown_is_never_zero_when_nothing_is_measured(ledger: PostgresStore) -> None:
    only_unmeasured = {"cluster": "'x9'"}
    [share] = await query(ledger, panel_sql("ttv-gpu-use", 2), **only_unmeasured)
    assert share["useful_share"] is None
    assert await query(ledger, panel_sql("ttv-gpu-use", 8), **only_unmeasured) == []
    why = await query(ledger, panel_sql("ttv-gpu-use", 10))
    assert {r["Why not measured"]: r["GPUs"] for r in why} == {
        "Not in a cluster yet (pipeline queue)": 12,
        "In a cluster outside the target list (I7)": 4,
    }


async def test_clusters_and_holders(ledger: PostgresStore) -> None:
    clusters = await query(ledger, panel_sql("ttv-gpu-use", 13))
    assert [(r["Cluster"], r["Use measured?"], r["GPUs"]) for r in clusters] == [
        ("c1", "Measured", 16),
        ("1 cluster outside the target list", "Outside target list (I7)", 4),
    ]
    assert clusters[1]["Useful work"] is None
    holders = await query(ledger, panel_sql("ttv-gpu-use", 15))
    first = holders[0]
    assert (first["Workload"], first["Use class"], first["Requests / GPU-h"]) == (
        "ns1/Deployment/idle-chat",
        "Waste candidate",
        0.0,
    )
    assert "0 genuine requests in 24 h" in first["Demand and readiness evidence"]
    assert holders[-1]["Use class"] == "Useful work"


async def test_trend_samples_open_intervals_on_a_grid(ledger: PostgresStore) -> None:
    trend = await query(ledger, panel_sql("ttv-gpu-use", 17))
    by_hour = {r["time"].hour: r for r in trend}
    assert by_hour[3]["Constrained or failed"] == 8
    assert by_hour[3]["Use unknown"] == 8
    assert by_hour[9]["Useful work"] == 2
    assert by_hour[9]["Constrained or failed"] == 1
    hours = await query(ledger, panel_sql("ttv-gpu-use", 19))
    got = {(r["Use class"], r["Reason"]): round(float(r["GPU-hours"]), 6) for r in hours}
    assert got[("Constrained or failed", "GPU_PLATFORM_NOT_READY")] == 48.0
    assert got[("Waste candidate", "NO_REAL_DEMAND")] == 6.0
    assert got[("Not measured", "(all reasons)")] == 16 * 12.0


async def test_asset_panels(ledger: PostgresStore) -> None:
    a = {"asset": "cisco:syn-a"}
    buckets = await query(ledger, panel_sql("ttv-asset", 9), **a)
    assert sum(r["GPUs"] for r in buckets) == 8
    assert [r["Category"] for r in buckets][:2] == ["PRODUCTIVE", "PRODUCTIVE"]
    missing = next(r for r in buckets if r["Reason"] == "GPU_MISSING")
    assert missing["Stage clock since (UTC)"] == "≥ 2026-10-01 00:00"
    assert "last XID GPU-syn-7: 79" in missing["Evidence"]
    milestones = {r["Milestone"]: r for r in await query(ledger, panel_sql("ttv-asset", 11), **a)}
    assert milestones["First seen by the engine"]["Observed at (UTC)"] == "≥ 2026-10-01 00:00"
    assert milestones["In server-scan"]["Precision"] == "exact"
    assert milestones["Node Ready"]["Observed at (UTC)"] == "not observed yet"
    [note] = await query(ledger, panel_sql("ttv-asset", 13), **a)
    assert (note["Status"], note["Next-action owner"]) == ("active", "Network")
    [conflict] = await query(ledger, panel_sql("ttv-asset", 12), asset="dell:syn-f")
    assert (conflict["Kind"], conflict["Other parties"]) == ("NODE_CLAIM", "dell:syn-g")
    timeline = await query(ledger, panel_sql("ttv-asset", 16), **a)
    at3 = [r for r in timeline if r["time"].hour == 3]
    assert [(r["metric"], r["value"]) for r in at3] == [("IDLE · GPU_PLATFORM_NOT_READY", 8)]


async def test_collector_record_and_no_access_state(ledger: PostgresStore) -> None:
    [rec] = await query(ledger, panel_sql("ttv-asset", 14), asset="hp:syn-e")
    assert (rec["Manager"], rec["Profile"], rec["GPUs"]) == ("ucs-central-syn", "none bound", 4)
    [other] = await query(ledger, panel_sql("ttv-asset", 14), asset="cisco:syn-a")
    assert other["Manager"] == "Not collector-only"
    await ledger.conn.execute("DROP TABLE collector_servers, next_action_annotations")
    for panel_id, column in ((14, "Manager"), (13, "Status")):
        [row] = await query(ledger, panel_sql("ttv-asset", panel_id), asset="hp:syn-e")
        assert row[column] == "Not available"
