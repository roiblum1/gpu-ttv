"""Static checks of the Grafana alerting files: contract fields, links and inert notifications.

A malformed alerting file stops Grafana at startup, so these run without a database (ADR-0005).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ALERTING = ROOT / "grafana/provisioning/alerting"
RULES_DOC = yaml.safe_load((ALERTING / "ttv-rules.yaml").read_text())
NOTIFY_DOC = yaml.safe_load((ALERTING / "ttv-notifications.yaml").read_text())
RULES = [r for g in RULES_DOC["groups"] for r in g["rules"]]
RUNBOOKS = (ROOT / "docs/runbooks.md").read_text()
RUNBOOK_BASE = "https://github.com/roiblum1/gpu-ttv/blob/main/docs/runbooks.md#"
ANNOTATIONS = {
    "summary", "description", "impact", "confidence", "next_step", "runbook_url", "dashboard_url",
}  # fmt: skip
STAGE_BREACH_KIND = "{{ if .Labels.stage_owner }}stage_breach{{ else }}data_quality{{ end }}"
STAGE_BREACH_OWNER = (
    "{{ if .Labels.stage_owner }}{{ .Labels.stage_owner }}{{ else }}platform{{ end }}"
)

pytestmark = pytest.mark.unit
by_uid = pytest.mark.parametrize("rule", RULES, ids=[r["uid"] for r in RULES])


def slug(heading: str) -> str:
    """GitHub's anchor for a Markdown heading."""
    return re.sub(r"[^a-z0-9 -]", "", heading.strip().lower()).replace(" ", "-")


def refs(rule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A rule's queries and expressions by refId."""
    return {q["refId"]: q for q in rule["data"]}


def test_rule_groups_live_outside_the_dashboard_folder() -> None:
    # Alerting provisions before dashboards; sharing a title would create a second folder.
    folders = {g["folder"] for g in RULES_DOC["groups"]}
    assert folders == {"GPU Time-to-Value alerts"}
    assert {g["name"]: g["interval"] for g in RULES_DOC["groups"]} == {
        "ttv-data-quality": "1m",
        "ttv-stage-objectives": "5m",
    }
    assert len({r["uid"] for r in RULES}) == len(RULES)


@by_uid
def test_rule_has_contract_labels_and_annotations(rule: dict[str, Any]) -> None:
    assert rule["uid"] == slug(rule["title"])
    assert set(rule["annotations"]) >= ANNOTATIONS
    assert {"severity", "kind", "owner"} <= set(rule["labels"])
    assert rule["annotations"]["runbook_url"] == RUNBOOK_BASE + rule["uid"]
    assert rule["annotations"]["dashboard_url"].startswith("{{ externalURL }}d/ttv-")
    assert rule["for"] and not rule.get("isPaused")


@by_uid
def test_labels_never_use_dollar_templates(rule: dict[str, Any]) -> None:
    # Grafana expands environment variables in provisioned labels, so "$labels" becomes "".
    assert not any("$" in v for v in rule["labels"].values())


@by_uid
def test_owner_and_states_follow_the_kind(rule: dict[str, Any]) -> None:
    labels = rule["labels"]
    if rule["uid"] == "ttv-stage-objective-breached":
        assert (labels["kind"], labels["owner"]) == (STAGE_BREACH_KIND, STAGE_BREACH_OWNER)
        assert (rule["noDataState"], rule["execErrState"]) == ("KeepLast", "KeepLast")
    else:
        assert (labels["kind"], labels["owner"]) == ("data_quality", "platform")
        assert rule["execErrState"] == "Error"
        assert rule["noDataState"] in {"NoData", "KeepLast"}
    assert "OK" not in (rule["noDataState"], rule["execErrState"])  # no evidence is not healthy


@by_uid
def test_condition_is_a_threshold_over_one_ledger_query(rule: dict[str, Any]) -> None:
    by_ref = refs(rule)
    condition = by_ref[rule["condition"]]
    assert condition["datasourceUid"] == "__expr__"
    assert condition["model"]["type"] == "threshold"
    source = by_ref[condition["model"]["expression"]]
    assert source["datasourceUid"] == "ttv-postgres"
    for ref, q in by_ref.items():
        if q["datasourceUid"] == "ttv-postgres":
            assert q["model"]["format"] == "table" and q["model"]["rawQuery"], ref
            assert "$" not in q["model"]["rawSql"], ref  # no macros: rules run without a dashboard
    used = set(re.findall(r"\$values\.([A-Z])\b", json.dumps(rule["annotations"])))
    assert used <= set(by_ref), used - set(by_ref)


def test_datasource_uid_matches_the_provisioned_ledger() -> None:
    datasources = yaml.safe_load((ROOT / "grafana/provisioning/datasources/ttv.yaml").read_text())
    ledger = {d["uid"]: d["type"] for d in datasources["datasources"]}
    assert ledger["ttv-postgres"] == "grafana-postgresql-datasource"


@by_uid
def test_linked_panel_exists(rule: dict[str, Any]) -> None:
    board = json.loads((ROOT / f"grafana/dashboards/{rule['dashboardUid']}.json").read_text())
    assert board["uid"] == rule["dashboardUid"]
    assert rule["panelId"] in {p["id"] for p in board["panels"]}


@by_uid
def test_runbook_section_and_contract_row_exist(rule: dict[str, Any]) -> None:
    headings = {slug(h) for h in re.findall(r"^### (.+)$", RUNBOOKS, flags=re.MULTILINE)}
    assert rule["uid"] in headings
    assert f"`{rule['uid']}`" in RUNBOOKS.split("## Rule contracts", 1)[1]


def test_notifications_are_inert_and_grouped() -> None:
    [contact] = NOTIFY_DOC["contactPoints"]
    [receiver] = contact["receivers"]
    assert receiver["type"] == "webhook"
    assert receiver["settings"]["url"].startswith("http://127.0.0.1:1/")
    [mute] = NOTIFY_DOC["muteTimes"]
    assert mute["time_intervals"] == [{"times": [{"start_time": "00:00", "end_time": "24:00"}]}]
    [root] = NOTIFY_DOC["policies"]
    assert root["receiver"] == contact["name"]
    assert root["group_by"] == ["kind", "owner", "alertname"]
    kinds = set()
    for route in root["routes"]:
        assert route["receiver"] == contact["name"]
        assert route["group_by"] == ["kind", "owner", "alertname"]
        assert route["mute_time_intervals"] == [mute["name"]]
        [(label, op, value)] = route["object_matchers"]
        assert (label, op) == ("kind", "=")
        kinds.add(value)
    assert kinds == {"data_quality", "stage_breach"}
