"""Rendered Helm charts: read-only RBAC, digests, dry run, no secret data, valid config (ADR-0007).

Renders `deploy/helm/gpu-ttv` and `deploy/gpu-cluster-rbac` with `helm template`; skipped when helm
is not on PATH (GitLab CI copies it in, GitHub runners have it).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from ttv.config import ClustersFile, ManagersFile, Policy, Settings

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/gpu-ttv"
RBAC_CHART = ROOT / "deploy/gpu-cluster-rbac"
FULL_VALUES = CHART / "ci/full-values.yaml"
GRAFANA = ROOT / "grafana"
HELM = shutil.which("helm")
READ_VERBS = {"get", "list", "watch"}
DIGEST_REF = re.compile(r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
# Set by the chart itself (or fixed by the image), so never accepted under `settings`.
CHART_MANAGED = {
    "TTV_DRY_RUN", "TTV_SERVERSCAN_URL", "TTV_SERVERSCAN_TOKEN_FILE", "TTV_CA_BUNDLE",
    "TTV_DATABASE_URL", "TTV_POLICY_FILE", "TTV_CLUSTERS_FILE", "TTV_MANAGERS_FILE",
    "TTV_PRICING_DIR", "TTV_API_HOST", "TTV_API_PORT",
}  # fmt: skip

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(HELM is None, reason="helm is not on PATH"),
]


def helm_template(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run `helm template` without raising, so refusals can be asserted."""
    assert HELM
    cmd = [HELM, "template", "ttv", str(chart), "--namespace", "gpu-ttv", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603 (repo charts)


def render(chart: Path, *args: str) -> list[dict[str, Any]]:
    """Rendered manifests of a chart."""
    result = helm_template(chart, *args)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


@pytest.fixture(scope="module")
def default_docs() -> list[dict[str, Any]]:
    return render(CHART)


@pytest.fixture(scope="module")
def full_docs() -> list[dict[str, Any]]:
    return render(CHART, "-f", str(FULL_VALUES))


@pytest.fixture(scope="module")
def rbac_docs(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    values = tmp_path_factory.mktemp("rbac") / "discovered.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "discoveredKinds": [
                    {"apiGroup": "run.ai", "resources": ["trainingworkloads", "runaijobs"]},
                    {"apiGroup": "serving.kserve.io", "resources": ["llminferenceservices"]},
                ]
            }
        )
    )
    return render(RBAC_CHART, "-f", str(values))


def by_kind(docs: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    """Documents of one kind, by name."""
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def containers(docs: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every container and init container of every workload, with its workload name."""
    for d in docs:
        spec = d.get("spec", {}).get("template", {}).get("spec")
        if spec:
            for c in spec.get("initContainers", []) + spec["containers"]:
                yield d["metadata"]["name"], c


def env(container: dict[str, Any]) -> dict[str, Any]:
    """A container's env: plain values as strings, secret references as their dicts."""
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container.get("env", [])}


def engine(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """The engine container."""
    return next(c for name, c in containers(docs) if name.endswith("-engine"))


def config_map_file(docs: list[dict[str, Any]], suffix: str, key: str) -> str:
    """One file of the ConfigMap whose name ends with `suffix`."""
    cm = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith(suffix)
    )
    return cm["data"][key]


def test_charts_render_and_lint() -> None:
    assert HELM
    for args in ([str(CHART)], [str(CHART), "-f", str(FULL_VALUES)], [str(RBAC_CHART)]):
        cmd = [HELM, "lint", *args]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        assert result.returncode == 0, result.stdout + result.stderr


def test_dry_run_is_on_by_default(
    default_docs: list[dict[str, Any]], full_docs: list[dict[str, Any]]
) -> None:
    assert yaml.safe_load((CHART / "values.yaml").read_text())["dryRun"] is True
    assert env(engine(default_docs))["TTV_DRY_RUN"] == "true"
    assert env(engine(full_docs))["TTV_DRY_RUN"] == "true"
    assert env(engine(render(CHART, "--set", "dryRun=false")))["TTV_DRY_RUN"] == "false"


def test_dry_run_cannot_be_overridden_through_settings_or_a_string() -> None:
    assert helm_template(CHART, "--set", "settings.TTV_DRY_RUN=false").returncode != 0
    assert helm_template(CHART, "--set-string", "dryRun=false").returncode != 0


def test_every_image_is_pinned_by_digest(
    default_docs: list[dict[str, Any]], full_docs: list[dict[str, Any]]
) -> None:
    images = [c["image"] for docs in (default_docs, full_docs) for _, c in containers(docs)]
    assert len(images) == 3  # engine (default) + engine and collector (full)
    assert all(DIGEST_REF.match(i) for i in images), images
    assert helm_template(CHART, "--set", "image.digest=latest").returncode != 0


@pytest.mark.parametrize("docs_name", ["default_docs", "full_docs", "rbac_docs"])
def test_no_rbac_rule_has_a_write_verb(docs_name: str, request: pytest.FixtureRequest) -> None:
    docs: list[dict[str, Any]] = request.getfixturevalue(docs_name)
    rules = [r for d in docs if d["kind"] in ("Role", "ClusterRole") for r in d["rules"]]
    for rule in rules:
        assert set(rule["verbs"]) <= READ_VERBS, rule
        assert "*" not in rule.get("resources", []) + rule.get("apiGroups", []), rule
    if docs_name != "rbac_docs":
        assert not rules  # the app chart binds no RBAC at all


def test_cluster_reader_has_read_access_monitoring_view_and_a_token(
    rbac_docs: list[dict[str, Any]],
) -> None:
    role = by_kind(rbac_docs, "ClusterRole")["gpu-ttv-reader"]
    granted = {(g, r) for rule in role["rules"] for g in rule.get("apiGroups", [])
               for r in rule.get("resources", [])}  # fmt: skip
    assert {("", "nodes"), ("", "pods"), ("run.ai", "trainingworkloads"),
            ("serving.kserve.io", "llminferenceservices")} <= granted  # fmt: skip
    assert not any(r in ("secrets", "configmaps") for _, r in granted)
    bindings = {b["roleRef"]["name"] for b in by_kind(rbac_docs, "ClusterRoleBinding").values()}
    assert bindings == {"gpu-ttv-reader", "cluster-monitoring-view"}
    token = by_kind(rbac_docs, "Secret")["gpu-ttv-reader-token"]
    assert token["type"] == "kubernetes.io/service-account-token"
    assert (
        token["metadata"]["annotations"]["kubernetes.io/service-account.name"] == "gpu-ttv-reader"
    )


@pytest.mark.parametrize("resource", ["secrets", "pods/exec", "*", "serviceaccounts"])
def test_cluster_reader_refuses_credentials_subresources_and_wildcards(resource: str) -> None:
    result = helm_template(
        RBAC_CHART,
        "--set", "discoveredKinds[0].apiGroup=run.ai",
        "--set", f"discoveredKinds[0].resources[0]={resource}",
    )  # fmt: skip
    assert result.returncode != 0
    assert "refusing resource" in result.stderr


@pytest.mark.parametrize("docs_name", ["default_docs", "full_docs", "rbac_docs"])
def test_no_secret_carries_data(docs_name: str, request: pytest.FixtureRequest) -> None:
    docs: list[dict[str, Any]] = request.getfixturevalue(docs_name)
    for secret in by_kind(docs, "Secret").values():
        assert not secret.get("data") and not secret.get("stringData"), secret["metadata"]["name"]


def test_secrets_are_referenced_by_name_only(full_docs: list[dict[str, Any]]) -> None:
    assert not by_kind(full_docs, "Secret")
    refs = {e["valueFrom"]["secretKeyRef"]["name"]
            for _, c in containers(full_docs) for e in c["env"] if "valueFrom" in e}  # fmt: skip
    assert refs == {"ttv-database"}


def test_grafana_config_maps_are_the_repo_files(full_docs: list[dict[str, Any]]) -> None:
    maps = by_kind(full_docs, "ConfigMap")
    dashboards = {k: v for cm in maps.values()
                  if cm["metadata"]["labels"].get("grafana_dashboard") == "1"
                  for k, v in cm["data"].items()}  # fmt: skip
    files = sorted((GRAFANA / "dashboards").glob("*.json"))
    assert sorted(dashboards) == [f.name for f in files]
    for f in files:
        assert dashboards[f.name] == f.read_text(), f.name
    provisioning = GRAFANA / "provisioning"
    assert (
        config_map_file(full_docs, "-grafana-datasource", "ttv.yaml")
        == (provisioning / "datasources/ttv.yaml").read_text()
    )
    for name in ("ttv-rules.yaml", "ttv-notifications.yaml"):
        assert (
            config_map_file(full_docs, "-grafana-alerting", name)
            == (provisioning / "alerting" / name).read_text()
        )


def test_grafana_provisioning_is_opt_in(default_docs: list[dict[str, Any]]) -> None:
    labels = [cm["metadata"]["labels"] for cm in by_kind(default_docs, "ConfigMap").values()]
    assert not any({"grafana_dashboard", "grafana_datasource", "grafana_alert"} & set(lab)
                   for lab in labels)  # fmt: skip


def test_every_ttv_variable_the_chart_sets_is_a_setting(full_docs: list[dict[str, Any]]) -> None:
    fields = {f"TTV_{name.upper()}" for name in Settings.model_fields}
    names = {n for _, c in containers(full_docs) for n in env(c) if n.startswith("TTV_")}
    assert names <= fields, names - fields
    assert {"TTV_DRY_RUN", "TTV_DATABASE_URL", "TTV_MANAGERS_FILE", "TTV_CLUSTERS_FILE"} <= names


def test_settings_schema_covers_exactly_the_settings_the_chart_leaves_open() -> None:
    schema = yaml.safe_load((CHART / "values.schema.json").read_text())
    open_names = set(schema["properties"]["settings"]["properties"])
    fields = {f"TTV_{name.upper()}" for name in Settings.model_fields}
    assert not open_names & CHART_MANAGED
    assert open_names | CHART_MANAGED == fields, fields ^ (open_names | CHART_MANAGED)
    assert helm_template(CHART, "--set", "settings.TTV_SCAN_INTERVL_SECONDS=60").returncode != 0


def test_generated_config_files_load_in_the_engine_and_collector(
    default_docs: list[dict[str, Any]], full_docs: list[dict[str, Any]]
) -> None:
    example = (ROOT / "config/policy.example.yaml").read_text()
    assert config_map_file(default_docs, "-policy", "policy.yaml") == example
    policy = Policy.model_validate(
        yaml.safe_load(config_map_file(full_docs, "-policy", "policy.yaml"))
    )
    clusters = ClustersFile.model_validate(
        yaml.safe_load(config_map_file(full_docs, "-clusters", "clusters.yaml"))
    ).clusters
    assert {c.name for c in clusters} <= policy.target_clusters
    managers = ManagersFile.model_validate(
        yaml.safe_load(config_map_file(full_docs, "-managers", "managers.yaml"))
    )
    assert [m.name for m in managers.ucs_central + managers.openmanage] == ["central-1", "ome-1"]


def test_every_configured_file_is_mounted(full_docs: list[dict[str, Any]]) -> None:
    clusters = yaml.safe_load(config_map_file(full_docs, "-clusters", "clusters.yaml"))["clusters"]
    eng = engine(full_docs)
    mounts = {m["mountPath"] for m in eng["volumeMounts"]}
    keys = ("token_file", "ca_bundle", "thanos_ca_bundle")
    paths = [Path(c[k]) for c in clusters for k in keys if c.get(k)]
    paths += [Path(v) for k, v in env(eng).items() if k.endswith(("_FILE", "_BUNDLE"))]
    for path in paths:
        assert str(path.parent) in mounts, path
    site1 = next(c for c in clusters if c["name"] == "ocp4-prod-gpu-site1")
    assert site1["token_file"] == "/var/run/secrets/ttv/clusters/ocp4-prod-gpu-site1/token"
    assert site1["ca_bundle"] == "/var/run/secrets/ttv/clusters/ocp4-prod-gpu-site1/ca.crt"


def test_collector_runs_only_with_managers_and_a_database(
    default_docs: list[dict[str, Any]], full_docs: list[dict[str, Any]]
) -> None:
    assert [n for n in by_kind(default_docs, "Deployment") if n.endswith("-collector")] == []
    assert [n for n in by_kind(full_docs, "Deployment") if n.endswith("-collector")]
    result = helm_template(CHART, "--set", "collector.managers.openmanage[0].name=ome-1")
    assert result.returncode != 0
    assert "database.existingSecret is required" in result.stderr


def test_a_cluster_outside_the_policy_targets_is_refused() -> None:
    result = helm_template(
        CHART,
        "--set", "clusters[0].name=not-a-target",
        "--set", "clusters[0].api_url=https://api.example.internal:6443",
        "--set", "clusters[0].thanos_url=https://thanos.example.internal",
        "--set", "clusters[0].tokenSecret=ttv-reader",
    )  # fmt: skip
    assert result.returncode != 0
    assert "not in the policy's target_clusters" in result.stderr


def test_pods_are_restricted(full_docs: list[dict[str, Any]]) -> None:
    for d in by_kind(full_docs, "Deployment").values():
        spec = d["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["automountServiceAccountToken"] is False
        assert spec["shareProcessNamespace"] is True
        assert d["spec"]["strategy"]["type"] == "Recreate"
        for c in spec["containers"]:
            sc = c["securityContext"]
            assert sc["readOnlyRootFilesystem"] is True
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"]["drop"] == ["ALL"]
            assert c["resources"]["requests"] and c["resources"]["limits"]


def test_engine_probes_and_metrics(full_docs: list[dict[str, Any]]) -> None:
    eng = engine(full_docs)
    assert eng["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert eng["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert eng["readinessProbe"]["httpGet"]["path"] == "/readyz"
    monitor = next(iter(by_kind(full_docs, "ServiceMonitor").values()))
    assert monitor["spec"]["endpoints"][0]["path"] == "/metrics/"
    policies = by_kind(full_docs, "NetworkPolicy")
    collector = next(p for n, p in policies.items() if n.endswith("-collector"))
    assert collector["spec"]["ingress"] == []
