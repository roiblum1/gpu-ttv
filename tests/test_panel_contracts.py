"""
Check every dashboard against its panel contracts and the page conventions (no database).

Schema: config/panel-contracts/README.md. Conventions: grafana/README.md, "Design conventions".
Each check is parametrised per dashboard file so a failure names the page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from grafana_sql import iter_panels

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "grafana" / "dashboards"
CONTRACT_DIR = ROOT / "config" / "panel-contracts"
DASHBOARDS = sorted(DASHBOARD_DIR.glob("*.json"))
CONTRACTS = sorted(CONTRACT_DIR.glob("*.yaml"))

# The links bar every page carries, in order (grafana/README.md, "Pages and stable UIDs").
PAGES = (
    ("Home", "ttv-home"),
    ("Queues", "ttv-queues"),
    ("Delivery", "ttv-delivery"),
    ("GPU use", "ttv-gpu-use"),
    ("Asset", "ttv-asset"),
    ("Evidence", "ttv-evidence"),
    ("Actions", "ttv-actions"),
    ("Capacity", "ttv-capacity"),
    ("Service", "ttv-service"),
    ("Economics", "ttv-economics"),
)
PAGE_UIDS = {uid for _, uid in PAGES}
QUESTIONS = tuple(f"E{n:02d}" for n in range(1, 19))

REQUIRED = (
    "id", "title", "question", "status", "audience", "decision", "definition", "unit",
    "population", "window", "filters", "query", "lineage", "freshness", "direction",
    "missing_data", "value_kind", "gate",
)  # fmt: skip
OPTIONAL = (
    "numerator", "denominator", "exclusions", "baseline", "thresholds", "uncertainty",
    "privileges", "cardinality", "drilldown", "action", "fixture", "expected",
)  # fmt: skip
ENUMS = {
    "question": {*QUESTIONS, "support"},
    "status": {"built", "missing_input"},
    "direction": {"lower_is_better", "higher_is_better", "neutral"},
    "value_kind": {
        "observed",
        "derived",
        "forecast",
        "scenario",
        "benchmark",
        "business_input",
        "assumption",
    },
    "gate": {"core", "history", "business_input"},
}
_DRILLDOWN = re.compile(r"(ttv-[a-z-]+)(?:#(\d+))?")


def _load_json(path: Path) -> dict[str, Any]:
    """
    Load a dashboard.

    Args:
        path (Path): Dashboard JSON file.

    Returns:
        dict[str, Any]: The dashboard.
    """
    return json.loads(path.read_text())


def _contract_path(dashboard: Path) -> Path:
    """
    Return the contract file that belongs to a dashboard file.

    Args:
        dashboard (Path): Dashboard JSON file.

    Returns:
        Path: `config/panel-contracts/<stem>.yaml`.
    """
    return CONTRACT_DIR / f"{dashboard.stem}.yaml"


def _contracts(dashboard: Path) -> dict[str, Any]:
    """
    Load a dashboard's contract file, failing clearly when it is missing.

    Args:
        dashboard (Path): Dashboard JSON file.

    Returns:
        dict[str, Any]: The parsed contract file.
    """
    path = _contract_path(dashboard)
    if not path.exists():
        pytest.fail(f"{path.relative_to(ROOT)} is missing: every dashboard needs panel contracts")
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict) and isinstance(data.get("panels"), list), (
        f"{path.name}: bad shape"
    )
    return data


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return every non-row panel, including panels nested in collapsed rows.

    Args:
        dashboard (dict[str, Any]): Dashboard JSON.

    Returns:
        list[dict[str, Any]]: Panels.
    """
    return [p for p in iter_panels(dashboard) if p.get("type") != "row"]


def _variables(dashboard: dict[str, Any]) -> set[str]:
    """
    Return the dashboard's template variable names.

    Args:
        dashboard (dict[str, Any]): Dashboard JSON.

    Returns:
        set[str]: Variable names.
    """
    return {v["name"] for v in dashboard.get("templating", {}).get("list", [])}


def _path_exists(ref: str) -> bool:
    """
    Tell whether the file part of `path#anchor` or `path::test` exists in the repo.

    Args:
        ref (str): Reference.

    Returns:
        bool: True if the file exists.
    """
    return (ROOT / re.split(r"#|::", ref, maxsplit=1)[0]).is_file()


def _entry_errors(entry: dict[str, Any], uid: str, dashboard: dict[str, Any]) -> list[str]:
    """
    Validate one contract entry against the schema and its dashboard.

    Args:
        entry (dict[str, Any]): Contract entry.
        uid (str): Dashboard UID.
        dashboard (dict[str, Any]): Dashboard JSON.

    Returns:
        list[str]: Problems, empty when the entry is valid.
    """
    where = f"{uid}#{entry.get('id')}"
    errors = [f"{where}: missing key {k!r}" for k in (*REQUIRED, *OPTIONAL) if k not in entry]
    errors += [f"{where}: unknown key {k!r}" for k in entry if k not in (*REQUIRED, *OPTIONAL)]
    for key in REQUIRED:
        value = entry.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"{where}: required key {key!r} is empty")
    for key, allowed in ENUMS.items():
        if key in entry and entry[key] not in allowed:
            errors.append(f"{where}: {key}={entry[key]!r} not in {sorted(allowed)}")
    if not isinstance(entry.get("id"), int):
        errors.append(f"{where}: id must be an integer")
    for key in ("filters", "lineage"):
        if not isinstance(entry.get(key), list) or not all(isinstance(x, str) for x in entry[key]):
            errors.append(f"{where}: {key} must be a list of strings")
    unknown = set(entry.get("filters") or []) - _variables(dashboard)
    if unknown:
        errors.append(f"{where}: filters {sorted(unknown)} are not variables of {uid}")
    query = entry.get("query")
    if isinstance(query, str):
        if not _path_exists(query):
            errors.append(f"{where}: query {query!r} points to a missing file")
        elif query.startswith(f"grafana/dashboards/{uid}.json") and query != (
            f"grafana/dashboards/{uid}.json#{entry.get('id')}"
        ):
            errors.append(f"{where}: query {query!r} should name this panel's id")
    fixture = entry.get("fixture")
    if isinstance(fixture, str) and not _path_exists(fixture):
        errors.append(f"{where}: fixture {fixture!r} points to a missing file")
    errors += _drilldown_errors(where, entry.get("drilldown"))
    return errors


def _drilldown_errors(where: str, drilldown: Any) -> list[str]:
    """
    Check a drilldown names a known page, and an existing panel when that page exists.

    Args:
        where (str): Entry label for messages.
        drilldown (Any): `null`, `<uid>`, `<uid>#<panel id>` or another link (e.g. `/explain`).

    Returns:
        list[str]: Problems.
    """
    if drilldown is None:
        return []
    m = _DRILLDOWN.fullmatch(str(drilldown))
    if m is None:
        return [] if str(drilldown).startswith("/") else [f"{where}: drilldown {drilldown!r}?"]
    uid, panel_id = m.group(1), m.group(2)
    if uid not in PAGE_UIDS:
        return [f"{where}: drilldown page {uid!r} is not one of the conventions' UIDs"]
    target = DASHBOARD_DIR / f"{uid}.json"
    if panel_id is None or not target.exists():
        return []  # a page not merged yet cannot be checked here
    if int(panel_id) not in {p.get("id") for p in _panels(_load_json(target))}:
        return [f"{where}: drilldown {drilldown!r} names a missing panel"]
    return []


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_uid_matches_file_name(path: Path) -> None:
    """The dashboard UID equals its file name and is one of the conventions' page UIDs."""
    uid = _load_json(path).get("uid")
    assert uid == path.stem
    assert uid in PAGE_UIDS, f"{uid} is not in the grafana/README.md page table"


def test_uids_are_unique() -> None:
    """No two dashboard files share a UID."""
    uids = [_load_json(p).get("uid") for p in DASHBOARDS]
    assert len(uids) == len(set(uids)), uids


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_has_gpu_ttv_tag(path: Path) -> None:
    """Every page carries the `gpu-ttv` tag."""
    assert "gpu-ttv" in _load_json(path).get("tags", [])


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_links_bar(path: Path) -> None:
    """The standard links bar: explicit links in page order, keeping variables and time."""
    links = _load_json(path).get("links", [])
    actual = [
        (link.get("type"), link.get("title"), link.get("url"), link.get("includeVars"),
         link.get("keepTime"))
        for link in links
    ]  # fmt: skip
    expected = [("link", title, f"/d/{uid}", True, True) for title, uid in PAGES]
    assert actual == expected


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_panel_ids_are_unique(path: Path) -> None:
    """Panel ids (rows included) are unique within the dashboard."""
    ids = [str(p.get("id")) for p in iter_panels(_load_json(path))]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_panel_has_one_contract(path: Path) -> None:
    """Each non-row panel has exactly one entry with the same id and title."""
    dashboard, data = _load_json(path), _contracts(path)
    assert data.get("dashboard") == path.stem, "the file's `dashboard` must be the dashboard UID"
    entries = data["panels"]
    problems = []
    for panel in _panels(dashboard):
        matching = [e for e in entries if e.get("id") == panel.get("id")]
        if len(matching) != 1:
            problems.append(
                f"panel {panel.get('id')} {panel.get('title')!r}: {len(matching)} entries"
            )
        elif matching[0].get("title") != panel.get("title"):
            problems.append(
                f"panel {panel.get('id')}: title {panel.get('title')!r} != contract "
                f"{matching[0].get('title')!r}"
            )
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_no_contract_without_panel(path: Path) -> None:
    """No contract entry refers to a panel the dashboard does not have (or to a row)."""
    ids = {p.get("id") for p in _panels(_load_json(path))}
    stray = [e.get("id") for e in _contracts(path)["panels"] if e.get("id") not in ids]
    assert not stray, f"contracts for missing panels: {stray}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_contract_entries_follow_the_schema(path: Path) -> None:
    """Keys, required values, enumerations, filters, query/fixture paths and drilldowns."""
    dashboard = _load_json(path)
    entries = _contracts(path)["panels"]
    errors = [e for entry in entries for e in _entry_errors(entry, path.stem, dashboard)]
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_missing_input_only_on_text_panels(path: Path) -> None:
    """missing_input panels are text cards titled `E0X · ...`, gated, with no queries."""
    panels = {p.get("id"): p for p in _panels(_load_json(path))}
    problems = []
    for entry in _contracts(path)["panels"]:
        panel = panels.get(entry.get("id"))
        if entry.get("status") != "missing_input" or panel is None:
            continue
        where = f"panel {entry.get('id')}"
        if panel.get("type") != "text":
            problems.append(f"{where}: missing_input on a {panel.get('type')} panel")
        if panel.get("targets"):
            problems.append(f"{where}: a missing-input card must not query anything")
        if not str(panel.get("title", "")).startswith(f"{entry.get('question')} · "):
            problems.append(f"{where}: title must start with '{entry.get('question')} · '")
        if entry.get("gate") not in ("history", "business_input"):
            problems.append(f"{where}: gate must be history or business_input")
    assert not problems, "\n".join(problems)


def test_no_contract_file_without_dashboard() -> None:
    """Every contract file belongs to a dashboard file."""
    stems = {p.stem for p in DASHBOARDS}
    assert not [c.name for c in CONTRACTS if c.stem not in stems]


def test_every_question_is_covered() -> None:
    """E01-E18 each have at least one contract across all pages (after every R1 page lands)."""
    if not (DASHBOARD_DIR / "ttv-home.json").exists():
        pytest.skip("ttv-home.json not merged yet: E01-E18 coverage is checked once Home exists")
    files = [yaml.safe_load(c.read_text()) for c in CONTRACTS]
    covered = {e.get("question") for f in files for e in f["panels"]}
    assert not [q for q in QUESTIONS if q not in covered]


def test_schema_matches_readme() -> None:
    """The keys this test enforces are exactly the keys of the README's example entry."""
    text = (CONTRACT_DIR / "README.md").read_text()
    block = re.search(r"```yaml\n(.*?)```", text, re.DOTALL)
    assert block is not None
    example = yaml.safe_load(block.group(1))["panels"][0]
    assert set(example) == {*REQUIRED, *OPTIONAL}


def test_question_map_lists_every_question() -> None:
    """docs/executive-question-map.md has a row for each of E01-E18."""
    text = (ROOT / "docs" / "executive-question-map.md").read_text()
    assert not [q for q in QUESTIONS if f"| {q} |" not in text]
