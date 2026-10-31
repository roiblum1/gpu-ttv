"""Runtime settings from `TTV_*` environment variables; policy from a validated YAML file."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ttv.models import Stage

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: Any) -> timedelta:
    """
    Parse `90s`, `15m`, `48h` or `3d` into a timedelta; other values go to pydantic's parser.

    Args:
        value (Any): A duration string, number of seconds, or timedelta.

    Returns:
        timedelta: The parsed duration.

    Raises:
        ValueError: If the value is negative or not a duration.
    """
    if isinstance(value, timedelta):
        result = value
    elif isinstance(value, int | float):
        result = timedelta(seconds=value)
    elif isinstance(value, str) and (match := _DURATION.match(value)):
        result = timedelta(seconds=float(match[1]) * _UNIT_SECONDS[match[2]])
    else:
        raise ValueError(f"not a duration: {value!r} (use e.g. 90s, 15m, 48h, 3d)")
    if result < timedelta(0):
        raise ValueError(f"duration must not be negative: {value!r}")
    return result


class Exclusions(BaseModel):
    """Assets deliberately left out of charging (AGENT.md I5)."""

    model_config = ConfigDict(extra="forbid")

    asset_ids: frozenset[str] = frozenset()
    serverscan_names: frozenset[str] = frozenset()


def _duration_field(value: Any) -> Any:
    """
    Parse a human duration string, leaving other values to pydantic.

    Args:
        value (Any): The raw value.

    Returns:
        Any: A timedelta for duration strings, else the value unchanged.
    """
    return parse_duration(value) if isinstance(value, str) else value


class CounterOfRecord(BaseModel):
    """
    One candidate request counter for a serving kind (§20.3, D8); the first with series wins.

    Attributes:
        metric (str): Metric name, e.g. `llm_d_epp_request_total`.
        pod_regex (str): Pods whose series count, with `{name}` replaced by the escaped name of
            the logical deployment, e.g. `^{name}-kserve-router-scheduler-[a-z0-9]+-[a-z0-9]+$`.
        matchers (str): Extra PromQL label matchers, e.g. `fairness_id!="synthetic"` (D9).
    """

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    pod_regex: str = Field(min_length=1)
    matchers: str = ""

    @property
    def selector(self) -> str:
        """
        The PromQL series selector, also the key of `ThanosSnapshot.counters`.

        Returns:
            str: `metric` or `metric{matchers}`.
        """
        return f"{self.metric}{{{self.matchers}}}" if self.matchers else self.metric

    @property
    def excludes_synthetic(self) -> bool:
        """
        Whether this counter filters out synthetic traffic (else attribution is unverified).

        Returns:
            bool: True when `matchers` is set.
        """
        return bool(self.matchers)


class ServingKind(BaseModel):
    """
    How to recognise a logical serving deployment and which counter measures its demand.

    Attributes:
        group (str): API group of the pod's top owner ("" for core).
        kind (str): Kind of the top owner.
        image_pattern (str | None): When set, a container image must match this regex.
        counters (list[CounterOfRecord]): Counter candidates in order; never summed together.
    """

    model_config = ConfigDict(extra="forbid")

    group: str
    kind: str
    image_pattern: str | None = None
    counters: list[CounterOfRecord] = Field(min_length=1)


_POD_HASH = "[a-z0-9]+-[a-z0-9]+$"
_EPP_PODS = "^{name}-kserve-router-scheduler-" + _POD_HASH
DEFAULT_SERVING_KINDS: tuple[ServingKind, ...] = (
    ServingKind(
        group="serving.kserve.io",
        kind="LLMInferenceService",
        counters=[
            CounterOfRecord(metric="llm_d_epp_request_total", pod_regex=_EPP_PODS),
            CounterOfRecord(metric="inference_objective_request_total", pod_regex=_EPP_PODS),
            CounterOfRecord(metric="inference_model_request_total", pod_regex=_EPP_PODS),
            CounterOfRecord(
                metric="vllm:request_success_total", pod_regex="^{name}-kserve-" + _POD_HASH
            ),
        ],
    ),
    ServingKind(
        group="serving.kserve.io",
        kind="InferenceService",
        counters=[
            CounterOfRecord(
                metric="vllm:request_success_total",
                pod_regex="^{name}-predictor-(?:[0-9]+-deployment-)?" + _POD_HASH,
            )
        ],
    ),
    ServingKind(
        group="apps",
        kind="Deployment",
        image_pattern="vllm",
        counters=[
            CounterOfRecord(metric="vllm:request_success_total", pod_regex="^{name}-" + _POD_HASH)
        ],
    ),
)


class ServingPolicy(BaseModel):
    """
    Serving demand rules (§20.2 rows 17-20, I2).

    Attributes:
        demand_window (timedelta): Window for genuine requests (row 19).
        warmup_grace (timedelta): A not-Ready serving pod younger than this is still loading.
        useful_requests_per_gpu_hour (float | None): Approved I2 threshold; None until approved
            (then `LOW_USE` is inactive and any genuine request is flagged `low_use_unverified`).
        kinds (list[ServingKind]): Recognised serving kinds, in order.
    """

    model_config = ConfigDict(extra="forbid")

    demand_window: timedelta = timedelta(hours=24)
    warmup_grace: timedelta = timedelta(minutes=30)
    useful_requests_per_gpu_hour: float | None = Field(default=None, ge=0)
    kinds: list[ServingKind] = Field(default_factory=lambda: list(DEFAULT_SERVING_KINDS))

    _durations = field_validator("demand_window", "warmup_grace", mode="before")(_duration_field)


class RunaiPolicy(BaseModel):
    """
    Which Run:ai owners are productive by presence (§0.2, C6); verified per cluster by probe R5.

    Attributes:
        group (str): API group of native Run:ai kinds.
        productive_kinds (frozenset[str]): Top-owner kinds that are productive.
        interactive_priority_classes (frozenset[str]): A `RunaiJob` pod with one of these
            priority classes is interactive, so idle (C6).
        priority_label (str): Pod label holding the priority class.
    """

    model_config = ConfigDict(extra="forbid")

    group: str = "run.ai"
    productive_kinds: frozenset[str] = frozenset(
        {
            "TrainingWorkload",
            "DistributedWorkload",
            "InferenceWorkload",
            "DistributedInferenceWorkload",
            "RunaiJob",
        }
    )
    interactive_priority_classes: frozenset[str] = frozenset({"build"})
    priority_label: str = "priorityClassName"


class AllocatedIdlePolicy(BaseModel):
    """
    The `allocated_idle` flag: a productive holder below `threshold` activity for `window`.

    Attributes:
        window (timedelta): How long activity must stay low.
        threshold (float): Highest activity, as a 0-1 ratio, that still counts as idle.
    """

    model_config = ConfigDict(extra="forbid")

    window: timedelta = timedelta(hours=6)
    threshold: float = Field(default=0.05, ge=0, le=1)

    _durations = field_validator("window", mode="before")(_duration_field)


class Pool(BaseModel):
    """
    A node pool: the first pool whose selector matches a node's labels names it.

    Attributes:
        name (str): Pool name recorded on intervals.
        node_selector (dict[str, str]): Labels the node must carry.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    node_selector: dict[str, str]


class TeamMap(BaseModel):
    """
    Consuming team of a productive bucket (§20.2 "Pools and teams"); unmapped is `unassigned`.

    Attributes:
        workloads (dict[str, str]): `namespace/Kind/name` of the logical workload → team.
        namespaces (dict[str, str]): Namespace → team.
    """

    model_config = ConfigDict(extra="forbid")

    workloads: dict[str, str] = {}
    namespaces: dict[str, str] = {}


class CollectorPolicy(BaseModel):
    """
    Profile-less collector rules (ADR-0002 rows 3-5; user decision of 2026-10-30).

    Attributes:
        ocp_profile_pattern (str): A bound profile whose name matches (`re.search`, case-
            sensitive) is an OCP profile. Same rule as server-scan's collectors (C2: verify the
            production value with probe P-SS-1).
        handover_grace (timedelta): How long an OCP-profile GPU server may be missing from
            server-scan before it is a data-quality problem (server-scan collects 3-hourly).
    """

    model_config = ConfigDict(extra="forbid")

    ocp_profile_pattern: str = "^ocp"
    handover_grace: timedelta = timedelta(hours=6)

    _durations = field_validator("handover_grace", mode="before")(_duration_field)

    @field_validator("ocp_profile_pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        """
        Refuse a pattern that is not a valid regular expression.

        Args:
            value (str): The pattern.

        Returns:
            str: The pattern unchanged.
        """
        re.compile(value)
        return value


class Policy(BaseModel):
    """
    Versioned business policy; every interval records the version it was decided under.

    Attributes:
        policy_version (str): Recorded on every interval; bump it when anything here changes.
        teams (dict[Stage, str]): Team that owns each stage.
        stage_objectives (dict[Stage, timedelta]): Time after which idle time in a stage is overdue.
        target_clusters (frozenset[str]): Names equal to server-scan `cluster_name`.
        critical_health_values (frozenset[str]): `health.overall` values meaning critical.
        exclusions (Exclusions): Intentional exclusions.
        serving (ServingPolicy): Serving recognition and demand rules.
        runai (RunaiPolicy): Run:ai productive kinds.
        allocated_idle (AllocatedIdlePolicy): The `allocated_idle` flag.
        pools (list[Pool]): Node pools, first match wins.
        team_map (TeamMap): Consuming teams.
        collector (CollectorPolicy): Profile-less collector rules.
    """

    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(min_length=1)
    teams: dict[Stage, str]
    stage_objectives: dict[Stage, timedelta]
    target_clusters: frozenset[str] = frozenset()
    critical_health_values: frozenset[str] = frozenset({"CRITICAL"})
    exclusions: Exclusions = Exclusions()
    serving: ServingPolicy = ServingPolicy()
    runai: RunaiPolicy = RunaiPolicy()
    allocated_idle: AllocatedIdlePolicy = AllocatedIdlePolicy()
    pools: list[Pool] = []
    team_map: TeamMap = TeamMap()
    collector: CollectorPolicy = CollectorPolicy()

    def counters(self) -> list[CounterOfRecord]:
        """
        Every distinct serving counter candidate, for the Thanos source to query.

        Returns:
            list[CounterOfRecord]: One per distinct selector, in policy order.
        """
        seen: dict[str, CounterOfRecord] = {}
        for kind in self.serving.kinds:
            for counter in kind.counters:
                seen.setdefault(counter.selector, counter)
        return list(seen.values())

    @field_validator("stage_objectives", mode="before")
    @classmethod
    def _parse_objectives(cls, value: Any) -> Any:
        """
        Accept human durations such as `3d` for each stage objective.

        Args:
            value (Any): The raw mapping from YAML.

        Returns:
            Any: The mapping with durations parsed.
        """
        if isinstance(value, dict):
            return {stage: parse_duration(duration) for stage, duration in value.items()}
        return value

    @model_validator(mode="after")
    def _every_stage_covered(self) -> Policy:
        """
        Require an owner and an objective for every stage.

        Returns:
            Policy: The validated policy.

        Raises:
            ValueError: If a stage has no team or no objective.
        """
        missing = [s for s in Stage if s not in self.teams or s not in self.stage_objectives]
        if missing:
            raise ValueError(f"teams and stage_objectives need every stage; missing {missing}")
        return self


def load_policy(path: Path) -> Policy:
    """
    Load and validate the policy file.

    Args:
        path (Path): YAML policy file.

    Returns:
        Policy: The validated policy.
    """
    return Policy.model_validate(yaml.safe_load(path.read_text()))


class ClusterConnection(BaseModel):
    """
    Read-only endpoints and label names of one target cluster (§20.3, D1-D3, D11).

    Attributes:
        name (str): Equals server-scan `cluster_name` and an entry of `target_clusters`.
        api_url (str): Kube API, e.g. `https://api.<cluster>:6443`.
        thanos_url (str): Thanos querier route, e.g. `https://thanos-querier-...` (`/api` only).
        token_file (Path): Service-account token (cluster reader + `cluster-monitoring-view`).
        ca_bundle (Path | None): CA bundle for the kube API; None uses the default trust store.
        thanos_ca_bundle (Path | None): CA bundle for the Thanos route.
        cluster_id (str | None): Expected `clusterversion` `spec.clusterID`; a mismatch fails
            the read, so a wrong URL never charges another cluster's nodes.
        gpu_resource (str): Extended resource of whole GPUs.
        reservation_namespaces (frozenset[str]): Namespaces of Run:ai reservation pods (D7).
        dcgm_node_label (str): DCGM label holding the node name (D1).
        dcgm_uuid_label (str): DCGM label holding the GPU UUID (D2).
        dcgm_pod_label (str): DCGM label holding the workload pod (D3).
        dcgm_namespace_label (str): DCGM label holding the workload namespace (D3).
        serving_pod_label (str): Pod label on serving counter series.
        serving_namespace_label (str): Namespace label on serving counter series.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    api_url: str = Field(pattern=r"^https://")
    thanos_url: str = Field(pattern=r"^https://")
    token_file: Path
    ca_bundle: Path | None = None
    thanos_ca_bundle: Path | None = None
    cluster_id: str | None = None
    gpu_resource: str = "nvidia.com/gpu"
    reservation_namespaces: frozenset[str] = frozenset({"runai-reservation"})
    dcgm_node_label: str = "Hostname"
    dcgm_uuid_label: str = "UUID"
    dcgm_pod_label: str = "exported_pod"
    dcgm_namespace_label: str = "exported_namespace"
    serving_pod_label: str = "pod"
    serving_namespace_label: str = "namespace"

    def token(self) -> str:
        """
        Read the bearer token from its mounted file (re-read every scan, so rotation works).

        Returns:
            str: The token, stripped.

        Raises:
            ValueError: If the file is empty.
        """
        token = self.token_file.read_text().strip()
        if not token:
            raise ValueError(f"{self.name}: token file {self.token_file} is empty")
        return token


class ClustersFile(BaseModel):
    """The clusters file: one connection per observed target cluster."""

    model_config = ConfigDict(extra="forbid")

    clusters: list[ClusterConnection] = []

    @model_validator(mode="after")
    def _unique_names(self) -> ClustersFile:
        """
        Refuse two connections with the same cluster name.

        Returns:
            ClustersFile: The validated file.

        Raises:
            ValueError: On a duplicate name.
        """
        names = [c.name for c in self.clusters]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate cluster names in clusters file: {names}")
        return self


class UcsCentralConnection(BaseModel):
    """
    One UCS Central (and the UCS Manager domains registered with it), read-only (ADR-0004).

    Attributes:
        name (str): Manager name; records carry `ucs-central:<name>`.
        host (str): Bare host name of UCS Central (port 443 only; no scheme, no path).
        username (str): UCS Central account (server-scan's, C4; read-only is enforced in code).
        password_file (Path): File holding its password.
        ucsm_username (str | None): The UCS Manager account valid on every domain, for the
            per-domain GPU fallback; None disables the fallback.
        ucsm_password_file (Path | None): Its password file.
        ca_bundle (Path | None): CA bundle for UCS Central and UCS Manager TLS.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    host: str = Field(pattern=r"^[A-Za-z0-9.-]+$")
    username: str
    password_file: Path
    ucsm_username: str | None = None
    ucsm_password_file: Path | None = None
    ca_bundle: Path | None = None


class OpenManageConnection(BaseModel):
    """
    One Dell OpenManage Enterprise, read-only (ADR-0004).

    Attributes:
        name (str): Manager name; records carry `openmanage:<name>`.
        url (str): OME base URL, `https://<host>`.
        username (str): OME account with the Viewer role (never Device Manager, which is scoped).
        password_file (Path): File holding its password.
        ca_bundle (Path | None): CA bundle for OME TLS.
        idrac_username (str | None): Read-only iDRAC account for the Redfish GPU fallback.
        idrac_password_file (Path | None): Its password file.
        idrac_ca_bundle (Path | None): CA bundle for iDRAC TLS.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")
    username: str
    password_file: Path
    ca_bundle: Path | None = None
    idrac_username: str | None = None
    idrac_password_file: Path | None = None
    idrac_ca_bundle: Path | None = None


class ManagersFile(BaseModel):
    """The managers file: the UCS Centrals and OpenManage instances the collector reads."""

    model_config = ConfigDict(extra="forbid")

    ucs_central: list[UcsCentralConnection] = []
    openmanage: list[OpenManageConnection] = []


def load_managers(path: Path | None) -> ManagersFile:
    """
    Load the managers file.

    Args:
        path (Path | None): YAML managers file; None reads no manager.

    Returns:
        ManagersFile: The connections.
    """
    if path is None:
        return ManagersFile()
    return ManagersFile.model_validate(yaml.safe_load(path.read_text()) or {})


def read_secret(path: Path) -> str:
    """
    Read a password or token from its mounted file (re-read on every use, so rotation works).

    Args:
        path (Path): The file.

    Returns:
        str: The value, stripped.

    Raises:
        ValueError: If the file is empty.
    """
    value = path.read_text().strip()
    if not value:
        raise ValueError(f"secret file {path} is empty")
    return value


def load_clusters(path: Path | None, policy: Policy) -> list[ClusterConnection]:
    """
    Load the clusters file and check every cluster is a policy target.

    Args:
        path (Path | None): YAML clusters file; None observes no cluster.
        policy (Policy): Policy whose `target_clusters` must include every entry.

    Returns:
        list[ClusterConnection]: The connections.

    Raises:
        ValueError: If a connection names a cluster that is not a target.
    """
    if path is None:
        return []
    clusters = ClustersFile.model_validate(yaml.safe_load(path.read_text()) or {}).clusters
    stray = sorted(c.name for c in clusters if c.name not in policy.target_clusters)
    if stray:
        raise ValueError(f"clusters file names non-target clusters: {stray}")
    return clusters


class Settings(BaseSettings):
    """
    Scalar runtime settings, read from `TTV_*` environment variables or a `.env` file.

    `dry_run` defaults to True: the engine classifies and logs but writes nothing until
    `TTV_DRY_RUN=false` is set after the dry-run review (AGENT.md §20.6).
    `pricing_dir` holds snapshot files imported at startup; `pricing_snapshot` pins the
    present-day snapshot id (unset: the latest published), which is also the rollback (ADR-0003).
    """

    model_config = SettingsConfigDict(env_prefix="TTV_", env_file=".env", extra="ignore")

    serverscan_url: str
    serverscan_token_file: Path | None = None
    ca_bundle: Path | None = None
    database_url: str | None = None
    policy_file: Path = Path("config/policy.yaml")
    clusters_file: Path | None = None
    cluster_timeout_seconds: float = Field(default=120.0, gt=0)
    owner_lookup_concurrency: int = Field(default=8, ge=1, le=64)
    dry_run: bool = True
    http_timeout_seconds: float = 30.0
    scan_interval_seconds: int = Field(default=300, ge=30)
    detail_ttl: timedelta = timedelta(hours=6)
    detail_max_per_scan: int = Field(default=1000, ge=1)
    detail_concurrency: int = Field(default=8, ge=1, le=64)
    unknown_grace_scans: int = Field(default=2, ge=1)
    tombstone_after: timedelta = timedelta(hours=24)
    pricing_dir: Path = Path("config/pricing/snapshots")
    pricing_snapshot: str | None = None
    managers_file: Path | None = None
    collector_timeout_seconds: float = Field(default=600.0, gt=0)
    collector_stale_after: timedelta = timedelta(hours=1)
    api_host: str = "0.0.0.0"  # noqa: S104 — serves inside a container; exposure is decided by the Service
    api_port: int = 8080

    @field_validator("pricing_snapshot", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        """
        Treat an empty `TTV_PRICING_SNAPSHOT` as unset.

        Args:
            value (Any): The raw value.

        Returns:
            Any: None for a blank string, else the value unchanged.
        """
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("detail_ttl", "tombstone_after", "collector_stale_after", mode="before")
    @classmethod
    def _parse_durations(cls, value: Any) -> Any:
        """
        Accept human durations such as `6h` from the environment.

        Args:
            value (Any): The raw value.

        Returns:
            Any: A timedelta when the value looked like a duration, else the value unchanged.
        """
        return parse_duration(value) if isinstance(value, str) and _DURATION.match(value) else value

    def serverscan_token(self) -> str | None:
        """
        Read the server-scan AUDITOR token from its mounted file, if configured.

        Returns:
            str | None: The token, stripped, or None.
        """
        if self.serverscan_token_file is None:
            return None
        return self.serverscan_token_file.read_text().strip() or None

    def redacted(self) -> dict[str, Any]:
        """
        Return the effective settings with secrets masked, for logs and `--print-config`.

        Returns:
            dict[str, Any]: Settings as plain values.
        """
        data = self.model_dump(mode="json")
        if data.get("database_url"):
            data["database_url"] = re.sub(
                r"://([^:/@]+):[^@]*@", r"://\1:***@", data["database_url"]
            )
        return data
