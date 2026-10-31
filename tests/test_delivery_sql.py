"""The Delivery dashboard's SQL (grafana/dashboards/ttv-delivery.json) against a real Postgres.

Runs the panel queries exactly as stored, with Grafana's macros substituted as the browser would,
over a synthetic ledger (fixture-tested). Needs `TTV_TEST_DSN`; skipped otherwise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

import pytest

from conftest import DSN
from ttv.store import PostgresStore

pytestmark = pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")

DASHBOARD = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "ttv-delivery.json"
NOW = datetime.now(UTC).replace(microsecond=0)
DAY0 = NOW.replace(hour=0, minute=0, second=0)  # end of the last complete UTC day
LAST = DAY0 - timedelta(days=10)  # inside the last 28 complete days
PRIOR = DAY0 - timedelta(days=40)  # inside the 28 days before that
POLICY = "test-1"
OBJECTIVES = {"PRE_PROVISIONING": 3, "PROVISIONING": 2, "GPU_PLATFORM": 1, "SERVING": 7}
OWNERS = {
    "PRE_PROVISIONING": "DC",
    "PROVISIONING": "Redbull",
    "GPU_PLATFORM": "POPAI",
    "SERVING": "Serving",
}


def panel_sql(panel_id: int, **variables: str) -> LiteralString:
    """Return a panel's query with macros and variables substituted like Grafana does."""
    dashboard = json.loads(DASHBOARD.read_text())
    panel = next(p for p in dashboard["panels"] if p["id"] == panel_id)
    subs = {
        "$__timeFrom()": f"'{(NOW - timedelta(days=90)).isoformat()}'",
        "$__timeTo()": f"'{NOW.isoformat()}'",
        "$stage_owner": "'__all__'",
        "$site": "'__all__'",
        "$period": "28",
        "$clock": "rec_ready",
        "$min_n": "5",
    } | {f"${k}": v for k, v in variables.items()}
    sql = panel["targets"][0]["rawSql"]
    for key, value in subs.items():
        sql = sql.replace(key, value)
    return cast(LiteralString, sql)  # repo-owned SQL with test values substituted


async def run(store: PostgresStore, panel_id: int, **variables: str) -> list[dict[str, Any]]:
    """Run a panel query and return dict rows."""
    return list(await (await store.conn.execute(panel_sql(panel_id, **variables))).fetchall())


class Ledger:
    """Writes synthetic ledger rows with the engine's conventions."""

    def __init__(self, store: PostgresStore) -> None:
        """Bind to a migrated store."""
        self.store = store

    async def setup(self) -> None:
        """Insert the stage objectives of the test policy."""
        for stage, days in OBJECTIVES.items():
            await self.store.conn.execute(
                "INSERT INTO stage_objectives VALUES (%s, %s, %s)",
                (POLICY, stage, timedelta(days=days)),
            )

    async def asset(self, asset_id: str, site: str = "tlv") -> None:
        """Insert an asset."""
        await self.store.conn.execute(
            "INSERT INTO assets (asset_id, site, gpu_model, first_observed_at, last_observed_at)"
            " VALUES (%s, %s, 'NVIDIA H100 80GB', %s, %s)",
            (asset_id, site, NOW - timedelta(days=100), NOW),
        )

    async def interval(
        self,
        asset_id: str,
        start: datetime,
        end: datetime | None,
        *,
        stage: str | None = None,
        category: str = "IDLE",
        entered: datetime | None = None,
        lb: bool = False,
        gpus: int = 8,
    ) -> None:
        """Insert one interval; a staged interval defaults to an episode starting at `start`."""
        await self.store.conn.execute(
            "INSERT INTO capacity_intervals (asset_id, category, reason_code, stage, stage_owner,"
            " stage_entered_at, stage_entered_lower_bound, gpus, started_at, ended_at,"
            " policy_version) VALUES (%s, %s, 'X', %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                asset_id,
                category,
                stage,
                OWNERS.get(stage or ""),
                (entered or start) if stage else None,
                lb,
                gpus,
                start,
                end,
                POLICY,
            ),
        )

    async def milestone(self, asset_id: str, name: str, at: datetime, lb: bool = True) -> None:
        """Insert a milestone (the engine flags gpu_ready and later milestones lower_bound)."""
        await self.store.conn.execute(
            "INSERT INTO milestones (asset_id, milestone, observed_at, lower_bound)"
            " VALUES (%s, %s, %s, %s)",
            (asset_id, name, at, lb),
        )

    async def episode(
        self, asset_id: str, stage: str, end: datetime, days: float, *, lb: bool = False, to: str
    ) -> None:
        """A closed stage episode of `days` ending at `end`, then stage `to` (or PRODUCTIVE)."""
        await self.asset(asset_id)
        await self.interval(asset_id, end - timedelta(days=days), end, stage=stage, lb=lb)
        if to == "PRODUCTIVE":
            await self.interval(asset_id, end, None, category="PRODUCTIVE")
        else:
            await self.interval(asset_id, end, None, stage=to)

    async def ready_clock(self, asset_id: str, ready: datetime, days: float) -> None:
        """First record (server-scan, exact) `days` before a GPU-ready transition the ledger saw."""
        record = ready - timedelta(days=days)
        await self.asset(asset_id)
        await self.milestone(asset_id, "in_serverscan", record, lb=False)
        await self.milestone(asset_id, "first_seen", record + timedelta(minutes=5))
        await self.interval(asset_id, record + timedelta(minutes=5), ready, stage="PROVISIONING")
        await self.interval(asset_id, ready, None, category="PRODUCTIVE")
        await self.milestone(asset_id, "gpu_ready", ready)


@pytest.fixture
async def ledger(store: PostgresStore) -> Ledger:
    """An empty migrated ledger with the test objectives."""
    result = Ledger(store)
    await result.setup()
    return result


def by_clock(result: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index the Clocks table by its Clock column."""
    return {r["Clock"]: r for r in result}


async def test_stage_p50_12_to_8_days_reads_4_days_shorter_33_percent(ledger: Ledger) -> None:
    # AGENT.md §13 #8: p50 from 12 to 8 days is "4 days shorter / 33.3% shorter".
    for i in range(5):
        await ledger.episode(f"p{i}", "PROVISIONING", PRIOR, 12, to="GPU_PLATFORM")
        await ledger.episode(f"l{i}", "PROVISIONING", LAST, 8, to="SERVING")
    await ledger.episode("lb", "PROVISIONING", LAST, 30, lb=True, to="GPU_PLATFORM")
    await ledger.episode("rework", "PROVISIONING", LAST, 1, to="PRE_PROVISIONING")
    row = by_clock(await run(ledger.store, 14))["Redbull stage (provisioning)"]
    assert row["Owner"] == "Redbull"
    assert row["Target"] == "2.0 d"
    assert (row["p50 · last 28 d"], row["p90 · last 28 d"]) == ("8.0 d", "8.0 d")
    assert row["n · last 28 d"] == "5 · 2 excl."  # the lower bound and the rework are not used
    assert (row["p50 · prior 28 d"], row["n · prior 28 d"]) == ("12.0 d", "5")
    assert row["Change in p50"] == "4.0 d shorter / 33.3% shorter"


async def test_lower_bounds_give_a_ge_percentile_and_no_change(ledger: Ledger) -> None:
    # DC clocks start at first sighting (a lower bound): 2 exact + 4 "≥" samples give "≥ p50".
    for i, days in enumerate((1, 2)):
        await ledger.episode(f"x{i}", "PRE_PROVISIONING", LAST, days, to="PROVISIONING")
    for i, days in enumerate((3, 4, 5, 6)):
        await ledger.episode(f"b{i}", "PRE_PROVISIONING", LAST, days, lb=True, to="PROVISIONING")
    row = by_clock(await run(ledger.store, 14))["DC stage (pre-provisioning)"]
    assert row["n · last 28 d"] == "2 + 4 ≥"
    assert row["p50 · last 28 d"] == "≥ 3.5 d"  # median of 1..6 days, all minimums or exact
    assert row["n · prior 28 d"] == "0 · needs 5"
    assert row["Change in p50"] == "insufficient history (n = 2 vs 0)"


async def test_gpu_ready_clock_is_exact_only_when_the_ledger_saw_the_transition(
    ledger: Ledger,
) -> None:
    for i, days in enumerate((2, 3, 4, 5, 6)):
        await ledger.ready_clock(f"r{i}", LAST, days)
    # Already GPU-ready when first seen: no pre-ready interval ended then, so never used.
    await ledger.asset("seen-ready")
    await ledger.milestone("seen-ready", "in_serverscan", LAST - timedelta(days=50), lb=False)
    await ledger.milestone("seen-ready", "first_seen", LAST)
    await ledger.milestone("seen-ready", "gpu_ready", LAST)
    await ledger.interval("seen-ready", LAST, None, stage="SERVING")
    [p50] = await run(ledger.store, 10)
    [p90] = await run(ledger.store, 11)
    assert (p50["metric"], round(p50["value"], 6)) == ("n = 5 · last 28 d", 4.0)
    assert round(p90["value"], 6) == 5.6  # percentile_cont(0.9) of 2..6 days
    row = by_clock(await run(ledger.store, 14))["First record → GPU-ready"]
    assert row["n · last 28 d"] == "5 · 1 excl."


async def test_too_few_samples_say_insufficient_history_not_a_number(ledger: Ledger) -> None:
    await ledger.ready_clock("only", LAST, 3)
    [p50] = await run(ledger.store, 10)
    assert p50 == p50 | {"metric": "insufficient history · n = 1 of 5", "value": None}
    verdict = {r["Clock"]: r["Verdict"] for r in await run(ledger.store, 9)}
    assert verdict["Time to GPU-ready"].startswith(
        "Not enough history to judge: 1 exact completion in the last 28 days and 0"
    )


async def test_quicker_verdict_is_qualified_while_the_backlog_is_overdue(ledger: Ledger) -> None:
    for i, days in enumerate((4, 5, 6, 7, 8)):
        await ledger.ready_clock(f"p{i}", PRIOR, days)
    for i, days in enumerate((2, 3, 4, 5, 6)):
        await ledger.ready_clock(f"l{i}", LAST, days)
    label = "Time to GPU-ready"
    verdict = {r["Clock"]: r["Verdict"] for r in await run(ledger.store, 9)}
    assert verdict[label] == (
        "Quicker: p50 2.0 d shorter / 33.3% shorter, p90 2.0 d shorter / 26.3% shorter"
        " (n = 5 vs 5). Backlog now: 0 servers waiting in a stage, 0 past objective."
    )
    await ledger.asset("stuck")
    await ledger.interval("stuck", NOW - timedelta(days=5), None, stage="PROVISIONING")
    verdict = {r["Clock"]: r["Verdict"] for r in await run(ledger.store, 9)}
    assert verdict[label].startswith("Quicker on completed work only: p50 2.0 d shorter")
    assert verdict[label].endswith("1 server waiting in a stage, 1 past objective.")


async def test_backlog_keeps_lower_bounded_ages_apart(ledger: Ledger) -> None:
    await ledger.asset("redbull-old")  # began before observation, already past its 2 d objective
    await ledger.interval(
        "redbull-old", NOW - timedelta(days=10), None, stage="PROVISIONING", lb=True
    )
    await ledger.asset("dc-new")  # began before observation, 1 h old: might be overdue
    await ledger.interval(
        "dc-new", NOW - timedelta(hours=1), None, stage="PRE_PROVISIONING", lb=True
    )
    await ledger.asset("serving")  # exact clock, 2 days
    await ledger.interval("serving", NOW - timedelta(days=2), None, stage="SERVING", gpus=6)
    [overdue] = await run(ledger.store, 3)
    assert (overdue["metric"], overdue["value"]) == ("servers · +1 may be (start not observed)", 1)
    aging = {r["Owner"]: r for r in await run(ledger.store, 6)}
    assert aging["Redbull"]["Age unknown (began before observation)"] == 1
    assert aging["DC"]["Age unknown (began before observation)"] == 1
    assert aging["Serving"]["1\N{EN DASH}3 d"] == 1  # the column label uses an en dash
    backlog = {r["Owner"]: r for r in await run(ledger.store, 7)}
    assert backlog["Redbull"]["Overdue servers"] == 1
    assert backlog["Redbull"]["Oldest"] == "≥ 10.0 d"
    assert backlog["Serving"]["GPUs waiting"] == 6
    assert backlog["Serving"]["Oldest"] == "2.0 d"
    assert backlog["GPU_PLATFORM"]["Servers waiting"] == 0  # no owner seen yet: the stage code


async def test_cohort_cells_mark_unmatured_and_unobserved_ages(ledger: Ledger) -> None:
    record = DAY0 - timedelta(days=40)
    await ledger.asset("watched")  # observed from first record: every age is known
    await ledger.milestone("watched", "in_serverscan", record, lb=False)
    await ledger.milestone("watched", "first_seen", record)
    await ledger.interval("watched", record, record + timedelta(days=3), stage="PROVISIONING")
    await ledger.interval("watched", record + timedelta(days=3), record + timedelta(days=10),
                          stage="SERVING")  # fmt: skip
    await ledger.interval("watched", record + timedelta(days=10), None, category="PRODUCTIVE")
    await ledger.milestone("watched", "gpu_ready", record + timedelta(days=3))
    await ledger.milestone("watched", "first_genuine_request", record + timedelta(days=10))
    late = DAY0 - timedelta(days=60)  # registered 40 days before the engine first saw it
    await ledger.asset("late")
    await ledger.milestone("late", "in_serverscan", late, lb=False)
    await ledger.milestone("late", "first_seen", late + timedelta(days=40))
    await ledger.interval("late", late + timedelta(days=40), None, stage="PROVISIONING")
    young = NOW - timedelta(days=3)
    await ledger.asset("young")
    await ledger.milestone("young", "in_serverscan", young, lb=False)
    await ledger.milestone("young", "first_seen", young)
    await ledger.interval("young", young, None, stage="PROVISIONING")
    cohorts = {r["Intake week"]: r for r in await run(ledger.store, 18)}
    watched, unseen, fresh = (cohorts[week(t)] for t in (record, late, young))
    assert [watched[f"GPU-ready by {d} d"] for d in (1, 7, 14, 30)] == [
        "0% (0/1)", "100% (1/1)", "100% (1/1)", "100% (1/1)"
    ]  # fmt: skip
    assert [watched[f"First request by {d} d"] for d in (1, 7, 14, 30)] == [
        "0% (0/1)", "0% (0/1)", "100% (1/1)", "100% (1/1)"
    ]  # fmt: skip
    assert {unseen[f"GPU-ready by {d} d"] for d in (1, 7, 14, 30)} == {"unobserved"}
    assert fresh["GPU-ready by 1 d"] == "0% (0/1)"
    assert fresh["GPU-ready by 7 d"] == "not matured"
    assert (fresh["Eligible"], fresh["Out of scope"], fresh["Never classified"]) == (1, 0, 0)


def week(t: datetime) -> datetime:
    """Monday 00:00 UTC of `t`'s week (the cohort key)."""
    return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


async def test_empty_ledger_shows_no_value_not_zero(ledger: Ledger) -> None:
    [waiting] = await run(ledger.store, 2)
    assert waiting["value"] is None
    assert await run(ledger.store, 6) == []
    assert await run(ledger.store, 7) == []
