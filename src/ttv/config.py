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


class ServingKind(BaseModel):
    """
    How to recognise a logical serving deployment by its pods' top owner.

    Attributes:
        group (str): API group of the pod's top owner ("" for core).
        kind (str): Kind of the top owner.
        image_pattern (str | None): When set, a container image must match this regex.
    """

    model_config = ConfigDict(extra="forbid")

    group: str
    kind: str
    image_pattern: str | None = None


DEFAULT_SERVING_KINDS: tuple[ServingKind, ...] = (
    ServingKind(group="serving.kserve.io", kind="LLMInferenceService"),
    ServingKind(group="serving.kserve.io", kind="InferenceService"),
    ServingKind(group="apps", kind="Deployment", image_pattern="vllm"),
)


class ServingPolicy(BaseModel):
    """
    Serving demand rules (ADR-0002 rows 16b-20, ADR-0009): requests read from each vLLM pod.

    Attributes:
        demand_window (timedelta): Window whose requests decide in use or waste (row 19).
        recent_window (timedelta): Shorter window shown next to it ("quiet" models).
        warmup_grace (timedelta): A not-Ready pod younger than this is still starting.
        useful_requests_per_gpu_hour (float | None): Below this many requests per GPU per hour
            over the demand window a model is `LOW_USE` (I2; chosen 2026-11-25, provisional).
            None turns `LOW_USE` off and flags genuine use `low_use_unverified`.
        kinds (list[ServingKind]): Recognised serving kinds, in order.
        metric (str): Request counter read from each pod's `/metrics`, summed over its labels.
        metrics_port (int | None): Port to read; None uses `ClusterPod.metrics_port`, else 8000.
        role_label (str): Pod label naming an llm-d role.
        skip_roles (frozenset[str]): Roles whose counters are not added (prefill pods count
            each request again; discovery fact 48).
    """

    model_config = ConfigDict(extra="forbid")

    demand_window: timedelta = timedelta(hours=24)
    recent_window: timedelta = timedelta(hours=1)
    warmup_grace: timedelta = timedelta(minutes=30)
    useful_requests_per_gpu_hour: float | None = Field(default=1.0, ge=0)
    kinds: list[ServingKind] = Field(default_factory=lambda: list(DEFAULT_SERVING_KINDS))
    metric: str = Field(default="vllm:request_success_total", pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    metrics_port: int | None = Field(default=None, ge=1, le=65535)
    role_label: str = "llm-d.ai/role"
    skip_roles: frozenset[str] = frozenset({"prefill"})

    _durations = field_validator("demand_window", "recent_window", "warmup_grace", mode="before")(
        _duration_field
    )

    @model_validator(mode="after")
    def _recent_inside_window(self) -> ServingPolicy:
        """
        Require the recent window to be positive and no longer than the demand window.

        Returns:
            ServingPolicy: This policy.

        Raises:
            ValueError: If `recent_window` is not positive or exceeds `demand_window`.
        """
        if not timedelta(0) < self.recent_window <= self.demand_window:
            raise ValueError("serving.recent_window must be positive and at most demand_window")
        return self


class RunaiPolicy(BaseModel):
    """
    Run:ai workloads: any pod whose top owner is in this API group (ADR-0009: all are ML).

    Attributes:
        group (str): API group of native Run:ai kinds.
    """

    model_config = ConfigDict(extra="forbid")

    group: str = "run.ai"


class AllocatedIdlePolicy(BaseModel):
    """
    GPU utilization rule: Machine Learning waste (row 21) and the serving `allocated_idle` flag.

    A pod older than `window` whose highest GPU activity over `window` stayed below `threshold`.

    Attributes:
        window (timedelta): How long activity must stay low.
        threshold (float): Highest activity, as a 0-1 ratio, that still counts as idle.
    """

    model_config = ConfigDict(extra="forbid")

    window: timedelta = timedelta(hours=6)
    threshold: float = Field(default=0.05, ge=0, le=1)

    _durations = field_validator("window", mode="before")(_duration_field)


class PowerRating(BaseModel):
    """
    Rated power of the GPUs whose DCGM `modelName` matches `pattern` (first match wins).

    Attributes:
        pattern (str): Case-insensitive regex searched in the model name, e.g. `H100.*PCIe`.
        watts (float): Rated board power in watts.
    """

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1)
    watts: float = Field(gt=0)


# Board power from NVIDIA's data sheets (documented, 2026-11-26). Where one name covers
# several boards the lower rating is used, which can only make a GPU look busier (ADR-0009).
DEFAULT_POWER_RATINGS: tuple[PowerRating, ...] = tuple(
    PowerRating(pattern=p, watts=w)
    for p, w in (
        ("B300", 1100),
        ("B200", 1000),
        ("H200 NVL", 600),
        ("H200", 700),
        ("H100 NVL", 350),
        ("H100 PCIe", 350),
        ("H100", 700),
        ("H800", 350),
        ("A100.*PCIe", 250),
        ("A100", 400),
        ("A800", 250),
        ("L40S", 350),
        ("L40", 300),
        (r"L4\b", 72),
        ("RTX PRO 6000", 400),
        (r"A10\b", 150),
        ("A16", 250),
        ("A30", 165),
        ("A40", 300),
        ("T4", 70),
        ("V100", 250),
    )
)


class PowerPolicy(BaseModel):
    """
    GPU power as a second sign of work (ADR-0009), as a share of each GPU's rated power.

    A GPU's rating is its DCGM power-management limit when exported, else the first matching
    `ratings` entry for its model name, else unknown (then power is not used for it).

    Attributes:
        threshold (float | None): Share of rated power that counts as working: a Machine
            Learning pod is busy when its peak reaches it, a served model is barely used when
            its 24 h average stays below it, and an empty GPU whose peak reaches it is in use
            by something unseen. None turns power off.
        ratings (list[PowerRating]): Rated power by model name, first match wins.
    """

    model_config = ConfigDict(extra="forbid")

    threshold: float | None = Field(default=0.25, gt=0, le=1)
    ratings: list[PowerRating] = Field(default_factory=lambda: list(DEFAULT_POWER_RATINGS))

    def rated_watts(self, model: str) -> float | None:
        """
        Rated power of a GPU model from the table.

        Args:
            model (str): DCGM `modelName`, e.g. `NVIDIA H100 80GB HBM3`.

        Returns:
            float | None: Watts, or None when no entry matches.
        """
        for rating in self.ratings:
            if re.search(rating.pattern, model, re.IGNORECASE):
                return rating.watts
        return None


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
        runai (RunaiPolicy): Run:ai recognition.
        allocated_idle (AllocatedIdlePolicy): GPU utilization rule.
        power (PowerPolicy): GPU power as a second sign of work.
        pools (list[Pool]): Node pools, first match wins.
        team_map (TeamMap): Consuming teams.
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
    power: PowerPolicy = PowerPolicy()
    pools: list[Pool] = []
    team_map: TeamMap = TeamMap()

    @model_validator(mode="before")
    @classmethod
    def _no_dc_stage(cls, value: Any) -> Any:
        """
        Refuse a policy written for the removed DC stage, with a message saying what to change.

        Args:
            value (Any): The raw policy mapping.

        Returns:
            Any: The value unchanged.

        Raises:
            ValueError: If `teams` or `stage_objectives` still names `PRE_PROVISIONING`.
        """
        if isinstance(value, dict) and any(
            isinstance(value.get(k), dict) and "PRE_PROVISIONING" in value[k]
            for k in ("teams", "stage_objectives")
        ):
            raise ValueError(
                "the DC stage (PRE_PROVISIONING) was removed (ADR-0008): delete it from"
                " teams and stage_objectives"
            )
        return value

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
            raise ValueError(
                f"teams and stage_objectives need every stage; missing {[str(m) for m in missing]}"
                " (ADR-0009 added EMPTY and MACHINE_LEARNING)"
            )
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
        thanos_insecure_skip_tls_verify (bool): Skip TLS verification for the Thanos route only,
            e.g. while its certificate is expired. Off by default; the engine logs a warning.
        cluster_id (str | None): Expected `clusterversion` `spec.clusterID`; a mismatch fails
            the read, so a wrong URL never charges another cluster's nodes.
        gpu_resource (str): Extended resource of whole GPUs.
        reservation_namespaces (frozenset[str]): Namespaces of Run:ai reservation pods (D7).
        dcgm_node_label (str): DCGM label holding the node name (D1).
        dcgm_uuid_label (str): DCGM label holding the GPU UUID (D2).
        dcgm_pod_label (str): DCGM label holding the workload pod (D3).
        dcgm_namespace_label (str): DCGM label holding the workload namespace (D3).
        dcgm_model_label (str): DCGM label holding the GPU model name (power ratings).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    api_url: str = Field(pattern=r"^https://")
    thanos_url: str = Field(pattern=r"^https://")
    token_file: Path
    ca_bundle: Path | None = None
    thanos_ca_bundle: Path | None = None
    thanos_insecure_skip_tls_verify: bool = False
    cluster_id: str | None = None
    gpu_resource: str = "nvidia.com/gpu"
    reservation_namespaces: frozenset[str] = frozenset({"runai-reservation"})
    dcgm_node_label: str = "Hostname"
    dcgm_uuid_label: str = "UUID"
    dcgm_pod_label: str = "exported_pod"
    dcgm_namespace_label: str = "exported_namespace"
    dcgm_model_label: str = "modelName"

    @model_validator(mode="after")
    def _one_thanos_trust(self) -> ClusterConnection:
        """
        Refuse a Thanos CA bundle together with skipping verification, which would ignore it.

        Returns:
            ClusterConnection: This connection.

        Raises:
            ValueError: If both are set.
        """
        if self.thanos_insecure_skip_tls_verify and self.thanos_ca_bundle:
            raise ValueError(
                f"{self.name}: set thanos_ca_bundle or thanos_insecure_skip_tls_verify, not both"
            )
        return self

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

    serverscan_url: str | None = None
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

    @field_validator("detail_ttl", "tombstone_after", mode="before")
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
