"""
Static checks of the managers' pages (ADR-0009): six pages, plain words, AWS price only, links.

No database. The SQL itself runs in `tests/test_dashboards_sql.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from grafana_sql import iter_panels, panel_queries, variable_queries

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "grafana" / "dashboards"
DASHBOARDS = {p.stem: json.loads(p.read_text()) for p in sorted(DASHBOARD_DIR.glob("*.json"))}
UIDS = ("ttv-home", "ttv-samurai", "ttv-redbull", "ttv-popai", "ttv-apply", "ttv-ml")
TITLES = {
    "ttv-home": "GPU waste: summary",
    "ttv-samurai": "GPU waste: Samurai",
    "ttv-redbull": "GPU waste: Redbull",
    "ttv-popai": "GPU waste: POPAI",
    "ttv-apply": "GPU waste: Apply",
    "ttv-ml": "GPU waste: Machine Learning",
}
# Every code the ledger or workloads_now can hold; none may reach a viewer (ADR-0009).
CODES = (
    "PRODUCTIVE", "IDLE", "UNKNOWN", "NOT_CHARGED",
    "PROVISIONING", "GPU_PLATFORM", "EMPTY", "SERVING", "MACHINE_LEARNING",
    "NOT_IN_CLUSTER", "IN_MCE_INVENTORY", "NODE_NOT_READY", "NODE_UNSCHEDULABLE",
    "HARDWARE_CRITICAL", "BMC_UNREACHABLE", "GPU_PLATFORM_NOT_READY", "GPU_MISSING",
    "NO_WORKLOAD", "LOW_USE", "NO_REAL_DEMAND", "MODEL_NOT_READY", "LOW_GPU_UTILIZATION",
    "WORKLOAD_NOT_READY", "RUNAI_WORKLOAD", "ML_WORKLOAD", "SERVING_IN_USE", "MAINTENANCE",
    "EXCLUDED", "OUTSIDE_TARGET_CLUSTERS", "SOURCE_STALE", "IDENTITY_CONFLICT", "CONTESTED",
    "MEMBERSHIP_UNKNOWN", "CLUSTER_NOT_OBSERVED", "CLUSTER_UNREACHABLE", "NODE_NOT_FOUND",
    "HOLDER_UNRESOLVED", "TRAFFIC_UNOBSERVED", "ACTIVITY_UNOBSERVED", "OTHER_WORKLOAD",
    "POWER_WITHOUT_POD",
)  # fmt: skip
RAW_CODE = re.compile(r"\b(" + "|".join(CODES) + r")\b")
# Money is the AWS On-Demand equivalent only (the user, 2026-11-26; ADR-0003 present-day value).
MONEY_WORDS = re.compile(r"\$(?!\{)|\busd\b|dollar|\bprices?\b|\bcosts?\b|costing", re.IGNORECASE)
OTHER_MONEY = re.compile(
    r"€|£|₪|\bILS\b|\bEUR\b|\bGBP\b|kWh|\benergy\b|electric|tariff|internal cost|our cost|"
    r"budget|invoice|\bspend|historical|fx_|instance_hourly_usd",
    re.IGNORECASE,
)
MONEY_UNITS = re.compile(r"^(currency|prefix:|suffix:)", re.IGNORECASE)
SELECTED_SNAPSHOT = (
    "snap AS ( SELECT s.snapshot_id, s.region, coalesce(s.publication_date, s.captured_at) AS "
    "published FROM pricing_snapshots s LEFT JOIN pricing_selection sel ON sel.snapshot_id = "
    "s.snapshot_id ORDER BY (sel.snapshot_id IS NOT NULL) DESC, s.publication_date DESC NULLS "
    "LAST, s.captured_at DESC, s.snapshot_id DESC LIMIT 1 )"
)
RATE_JOIN = re.compile(
    r"LEFT JOIN gpu_model_aliases ga ON ga\.raw_model = \w+\.gpu_model LEFT JOIN pricing_rates r "
    r"ON r\.snapshot_id = \(SELECT snapshot_id FROM snap\) AND r\.gpu_model = ga\.canonical"
)
BUILT_IN = {"stat", "bargauge", "barchart", "table", "timeseries", "text", "row"}
VISIBLE_KEYS = {
    "title", "description", "noValue", "displayName", "tooltip", "axisLabel", "content",
    "text", "label",
}  # fmt: skip
PAGE_URL = re.compile(r"/d/([A-Za-z0-9_-]+)")

by_page = pytest.mark.parametrize("uid", UIDS)


def _strings(node: Any, key: str = "") -> Iterator[tuple[str, str]]:
    """
    Yield every string in a JSON tree with the key it sits under.

    Args:
        node (Any): JSON value.
        key (str): Key of `node` in its parent ("" for list items and the root).

    Yields:
        tuple[str, str]: `(key, string)`.
    """
    if isinstance(node, str):
        yield key, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(k, "")  # mapping keys are shown too (value mappings)
            yield from _strings(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v, key)


def _visible(dashboard: dict[str, Any]) -> list[str]:
    """
    Return the text a viewer can read: titles, descriptions, labels, mappings, link titles.

    Args:
        dashboard (dict[str, Any]): Dashboard JSON.

    Returns:
        list[str]: Visible strings (SQL and URLs excluded).
    """
    out = [dashboard["title"], dashboard.get("description", "")]
    for link in dashboard.get("links", []):
        out += [link.get("title", ""), link.get("tooltip", "")]
    for var in dashboard.get("templating", {}).get("list", []):
        out += [var.get("label", ""), var.get("description", "")]
    for panel in iter_panels(dashboard):
        panel = {k: v for k, v in panel.items() if k != "targets"}
        out += [s for k, s in _strings(panel) if k in VISIBLE_KEYS]
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            for prop in override.get("properties", []):
                if prop.get("id") == "mappings":
                    out += [s for _k, s in _strings(prop["value"])]
    return out


def _urls(dashboard: dict[str, Any]) -> list[str]:
    """
    Return every link URL on a page: the links bar, panel links and data links.

    Args:
        dashboard (dict[str, Any]): Dashboard JSON.

    Returns:
        list[str]: URLs.
    """
    urls = [link["url"] for link in dashboard.get("links", [])]
    for panel in iter_panels(dashboard):
        urls += [s for k, s in _strings({k: v for k, v in panel.items() if k != "targets"})
                 if k == "url"]  # fmt: skip
        urls += re.findall(r"\]\(([^)]+)\)", panel.get("options", {}).get("content", ""))
    return urls


def test_exactly_the_six_pages() -> None:
    """The six pages of ADR-0009 and nothing else; titles as the user asked."""
    assert sorted(DASHBOARDS) == sorted(UIDS)
    assert {uid: d["title"] for uid, d in DASHBOARDS.items()} == TITLES


@by_page
def test_every_panel_has_a_description(uid: str) -> None:
    """Each panel says in a sentence what it shows (the panel's info icon)."""
    panels = [p for p in iter_panels(DASHBOARDS[uid]) if p.get("type") != "row"]
    missing = [p["id"] for p in panels if len(p.get("description", "").strip()) < 20]
    assert not missing


@by_page
def test_no_raw_reason_codes_in_visible_text(uid: str) -> None:
    """Codes such as NO_WORKLOAD never reach a viewer: the SQL maps them to plain words."""
    found = {(m.group(1), s) for s in _visible(DASHBOARDS[uid]) for m in RAW_CODE.finditer(s)}
    assert not found


def _sql(panel: dict[str, Any]) -> str:
    """
    Return a panel's SQL with whitespace collapsed, so patterns ignore line breaks.

    Args:
        panel (dict[str, Any]): Panel JSON.

    Returns:
        str: The SQL of all its targets.
    """
    return " ".join(" ".join(t.get("rawSql", "") for t in panel.get("targets", [])).split())


def _money_panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return the panels that show money: those whose SQL reads an AWS rate.

    Args:
        dashboard (dict[str, Any]): Dashboard JSON.

    Returns:
        list[dict[str, Any]]: Panels.
    """
    return [p for p in iter_panels(dashboard) if "per_gpu_hour_usd" in _sql(p)]


@by_page
def test_money_comes_only_from_the_selected_aws_snapshot(uid: str) -> None:
    """Every rate is pricing_rates at pricing_selection, else the latest snapshot (ADR-0003)."""
    dashboard = DASHBOARDS[uid]
    money = _money_panels(dashboard)
    assert money, "every page shows the AWS price of its waste"
    for panel in iter_panels(dashboard):
        sql, where = _sql(panel), f"#{panel.get('id')}"
        if panel in money:
            assert SELECTED_SNAPSHOT in sql, where
            assert sql.count("pricing_rates") == len(RATE_JOIN.findall(sql)), where
            assert sql.count("per_gpu_hour_usd") == sql.count("r.per_gpu_hour_usd"), where
            assert sql.count("pricing_snapshots") == 1, where  # no historical lookup
        else:
            assert not re.search(r"pricing_|per_gpu_hour", sql), where
    variables = " ".join(q.sql for q in variable_queries(dashboard))
    assert "pricing" not in variables  # managers never pick a snapshot


@by_page
def test_money_panels_say_aws_price_and_not_our_bill(uid: str) -> None:
    """A money panel says it is the AWS price, a comparison and not our bill; others show none."""
    dashboard = DASHBOARDS[uid]
    money = _money_panels(dashboard)
    for panel in money:
        description = panel.get("description", "")
        assert "AWS price" in description and "not our bill" in description, panel["id"]
    for panel in iter_panels(dashboard):
        if panel in money:
            continue
        shown = _visible({"title": "", "panels": [panel]})
        assert not [s for s in shown if MONEY_WORDS.search(s)], panel["id"]


@by_page
def test_no_other_currency_or_money_view(uid: str) -> None:
    """Only US dollars at AWS On-Demand prices: no other currency, energy or internal cost."""
    dashboard = DASHBOARDS[uid]
    found = [s for s in _visible(dashboard) if OTHER_MONEY.search(s)]
    found += [q.label for q in panel_queries(dashboard) if OTHER_MONEY.search(q.sql)]
    assert not found
    units = {s for k, s in _strings(dashboard) if k in ("unit", "value") and MONEY_UNITS.match(s)}
    assert units <= {"prefix:$"}, units
    money = {id(p) for p in _money_panels(dashboard)}
    for panel in iter_panels(dashboard):
        if "prefix:$" in json.dumps(panel.get("fieldConfig", {})):
            assert id(panel) in money, panel["id"]


@by_page
def test_only_built_in_panels(uid: str) -> None:
    """Air-gapped: built-in panel types only, no plugins, no remote images or fonts."""
    types = {p["type"] for p in iter_panels(DASHBOARDS[uid])}
    assert types <= BUILT_IN
    assert "http" not in json.dumps(DASHBOARDS[uid])


@by_page
def test_links_resolve_to_existing_pages(uid: str) -> None:
    """Every `/d/<uid>` link (links bar, tiles, data links) opens one of the six pages."""
    dashboard = DASHBOARDS[uid]
    urls = _urls(dashboard)
    assert urls
    targets = {m.group(1) for u in urls for m in PAGE_URL.finditer(u)}
    assert targets <= set(UIDS), targets - set(UIDS)
    assert all(u.startswith("/d/") for u in urls), urls


def test_home_tiles_link_each_team_page_samurai_first() -> None:
    """The five team tiles sit in one row, Samurai first, each linking to its team's page."""
    tiles = [p for p in DASHBOARDS["ttv-home"]["panels"]
             if p["gridPos"]["y"] == 6 and p["type"] == "stat"]  # fmt: skip
    tiles.sort(key=lambda p: p["gridPos"]["x"])
    pages = [PAGE_URL.search(p["fieldConfig"]["defaults"]["links"][0]["url"]) for p in tiles]
    assert [m.group(1) if m else None for m in pages] == [
        "ttv-samurai", "ttv-redbull", "ttv-popai", "ttv-apply", "ttv-ml",
    ]  # fmt: skip
    assert [p["title"] for p in tiles] == ["Samurai", "Redbull", "POPAI", "Apply",
                                          "Machine Learning"]  # fmt: skip


@by_page
def test_only_time_range_and_cluster_to_choose(uid: str) -> None:
    """Nothing technical to set: one optional cluster filter and the time range (7 days)."""
    dashboard = DASHBOARDS[uid]
    variables = dashboard["templating"]["list"]
    assert [(v["name"], v["includeAll"], v["multi"]) for v in variables] == [
        ("cluster", True, True)
    ]
    assert dashboard["time"] == {"from": "now-7d", "to": "now"}
    assert dashboard["editable"] is False


@by_page
def test_team_pages_filter_by_stage_not_by_team_name(uid: str) -> None:
    """Pages select rows by stage, so renaming a team in the policy never empties a page."""
    for q in panel_queries(DASHBOARDS[uid]):
        assert not re.search(r"stage_owner\s*(=|IN)", q.sql), q.label
