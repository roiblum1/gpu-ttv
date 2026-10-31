"""
Immutable, checksummed pricing snapshot files: the only way prices reach the engine (ADR-0003).

A snapshot is captured on the connected side, committed or carried in, and imported offline.
`content_sha256` covers the canonical JSON of every other field, so a changed byte is refused.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ttv.pricing.mapping import Confidence

PARSER_VERSION = "ttv-aws-ec2-csv/1"
DecimalStr = Annotated[str, Field(pattern=r"^\d+(\.\d+)?$")]


class SnapshotError(ValueError):
    """A snapshot file is malformed, tampered with, or conflicts with an existing one."""


def decimal_str(value: Decimal) -> str:
    """
    Render a decimal in plain notation with no trailing zeros.

    Args:
        value (Decimal): The value.

    Returns:
        str: e.g. `10` for `Decimal("10.000")`, `2.74470525` for `Decimal("2.744705250")`.
    """
    return format(value.normalize(), "f")


def per_gpu_rate(instance_hourly_usd: Decimal, instance_gpus: int) -> Decimal:
    """
    Linear per-GPU comparison rate: the whole instance price split across its physical GPUs.

    Args:
        instance_hourly_usd (Decimal): Instance price per hour.
        instance_gpus (int): Physical GPUs in the instance.

    Returns:
        Decimal: Price per GPU-hour (AGENT.md §10); includes the host's share, not a GPU tariff.

    Raises:
        SnapshotError: If the GPU count is not positive.
    """
    if instance_gpus <= 0:
        raise SnapshotError(f"instance_gpus must be positive, got {instance_gpus}")
    return instance_hourly_usd / instance_gpus


def snapshot_id_for(region: str, publication_date: datetime, map_version: str) -> str:
    """
    The immutable id of a snapshot: one AWS publication priced with one map version.

    Args:
        region (str): AWS region code.
        publication_date (datetime): The price list's `Publication Date` (UTC).
        map_version (str): Instance map version.

    Returns:
        str: e.g. `aws-ec2-ondemand-us-east-1-20261029T175634Z-map-2026-10-31.1`.
    """
    stamp = publication_date.strftime("%Y%m%dT%H%M%SZ")
    return f"aws-ec2-ondemand-{region}-{stamp}-map-{map_version}"


class Rate(BaseModel):
    """
    One priced canonical GPU model.

    Attributes:
        gpu_model (str): Canonical GPU model.
        instance_type (str): AWS instance type.
        sku (str): Price list SKU.
        rate_code (str): Price list RateCode (SKU.OfferTermCode.PriceDimension).
        instance_hourly_usd (str): Instance On-Demand price per hour, decimal string.
        instance_gpus (int): Physical GPUs in the instance (price list GPU column).
        per_gpu_hour_usd (str): `instance_hourly_usd / instance_gpus`, decimal string.
        kind (Literal["exact", "proxy"]): Mapping kind; proxies need user approval.
        confidence (Confidence): Mapping confidence.
        reason (str): Why this instance, including hardware differences.
        effective_date (str): The price term's EffectiveDate from AWS.
        price_description (str): The price list's description of the term.
        gpu_memory (str): Price list `GPU Memory`, verbatim.
        vcpu (str): Price list `vCPU`, verbatim.
        memory (str): Price list `Memory` (host RAM), verbatim.
        map_sources (list[str]): Documentation URLs behind the mapping.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gpu_model: str
    instance_type: str
    sku: str
    rate_code: str
    instance_hourly_usd: DecimalStr
    instance_gpus: int = Field(gt=0)
    per_gpu_hour_usd: DecimalStr
    kind: Literal["exact", "proxy"]
    confidence: Confidence
    reason: str
    effective_date: str
    price_description: str
    gpu_memory: str
    vcpu: str
    memory: str
    map_sources: list[str]


class Unpriced(BaseModel):
    """
    A canonical GPU model this snapshot deliberately does not price.

    Attributes:
        gpu_model (str): Canonical GPU model.
        reason (str): Why there is no defensible comparison.
        instance_type (str | None): The mapped instance when its offer was missing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gpu_model: str
    reason: str
    instance_type: str | None = None


class Snapshot(BaseModel):
    """
    One dated, offline pricing snapshot (AGENT.md §10, §20.5).

    Attributes:
        snapshot_id (str): Immutable id (`snapshot_id_for`).
        provider (Literal["aws"]): Cloud provider.
        service (str): Price list offer code, `AmazonEC2`.
        region (str): AWS region code.
        currency (Literal["USD"]): Currency of every price.
        purchase_option (Literal["OnDemand"]): Purchase model.
        operating_system (Literal["Linux"]): Operating system of the priced offers.
        tenancy (Literal["Shared"]): Tenancy of the priced offers.
        billing_unit (Literal["Hrs"]): Billing unit of the priced offers.
        publication_date (datetime): AWS `Publication Date`; the snapshot is in force from here.
        offer_version (str): AWS `Version` of the offer file.
        captured_at (datetime): When the file was downloaded.
        source_urls (list[str]): Price list URL(s).
        raw_sha256 (str): sha256 of the raw downloaded bytes.
        raw_bytes (int): Size of the raw download.
        parser_version (str): Parser that produced the rates.
        map_version (str): Instance map version.
        catalog_version (str): GPU catalogue version.
        filters (dict[str, str]): Price list column values every priced row matched.
        rates (list[Rate]): Priced models.
        unpriced (list[Unpriced]): Models deliberately left unpriced.
        content_sha256 (str): sha256 of the canonical JSON of every other field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    provider: Literal["aws"] = "aws"
    service: str = "AmazonEC2"
    region: str
    currency: Literal["USD"] = "USD"
    purchase_option: Literal["OnDemand"] = "OnDemand"
    operating_system: Literal["Linux"] = "Linux"
    tenancy: Literal["Shared"] = "Shared"
    billing_unit: Literal["Hrs"] = "Hrs"
    publication_date: AwareDatetime
    offer_version: str
    captured_at: AwareDatetime
    source_urls: list[str]
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_bytes: int = Field(ge=0)
    parser_version: str
    map_version: str
    catalog_version: str
    filters: dict[str, str]
    rates: list[Rate]
    unpriced: list[Unpriced]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _consistent(self) -> Snapshot:
        """
        Check the id, the per-GPU arithmetic and that each model appears once.

        Returns:
            Snapshot: The validated snapshot.

        Raises:
            ValueError: On any inconsistency.
        """
        expected = snapshot_id_for(self.region, self.publication_date, self.map_version)
        if self.snapshot_id != expected:
            raise ValueError(f"snapshot_id {self.snapshot_id!r} should be {expected!r}")
        models = [r.gpu_model for r in self.rates] + [u.gpu_model for u in self.unpriced]
        if len(models) != len(set(models)):
            raise ValueError("a GPU model appears twice in rates/unpriced")
        for r in self.rates:
            want = per_gpu_rate(Decimal(r.instance_hourly_usd), r.instance_gpus)
            if Decimal(r.per_gpu_hour_usd) != want:
                raise ValueError(f"{r.gpu_model}: per_gpu_hour_usd is not hourly / gpus")
        return self

    @classmethod
    def create(cls, **fields: Any) -> Snapshot:
        """
        Build a snapshot, deriving its id and content checksum.

        Args:
            **fields (Any): Every field except `snapshot_id` and `content_sha256`.

        Returns:
            Snapshot: The sealed snapshot.
        """
        draft = cls.model_construct(**fields, snapshot_id="", content_sha256="0" * 64)
        body = draft.model_dump(mode="json", exclude={"content_sha256"})
        body["snapshot_id"] = snapshot_id_for(
            fields["region"], fields["publication_date"], fields["map_version"]
        )
        return cls.model_validate({**body, "content_sha256": content_sha256(body)})

    def document(self) -> dict[str, Any]:
        """
        The JSON document exactly as written to disk and stored in Postgres.

        Returns:
            dict[str, Any]: Plain JSON values.
        """
        return self.model_dump(mode="json")

    def priced_models(self) -> frozenset[str]:
        """
        Canonical models with a rate.

        Returns:
            frozenset[str]: Priced models.
        """
        return frozenset(r.gpu_model for r in self.rates)


def content_sha256(body: dict[str, Any]) -> str:
    """
    Checksum of a snapshot body: sorted keys, compact separators, UTF-8.

    Args:
        body (dict[str, Any]): The document without `content_sha256`.

    Returns:
        str: Hex sha256.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_snapshot(document: dict[str, Any]) -> Snapshot:
    """
    Verify a snapshot document's checksum, then validate it.

    Args:
        document (dict[str, Any]): Parsed JSON.

    Returns:
        Snapshot: The verified snapshot.

    Raises:
        SnapshotError: If the checksum does not match or the content is invalid.
    """
    body = {k: v for k, v in document.items() if k != "content_sha256"}
    claimed = document.get("content_sha256")
    actual = content_sha256(body)
    if claimed != actual:
        raise SnapshotError(f"content_sha256 mismatch: file says {claimed}, content is {actual}")
    try:
        return Snapshot.model_validate(document)
    except ValueError as exc:
        raise SnapshotError(f"invalid snapshot: {exc}") from exc


def load_snapshot(path: Path) -> Snapshot:
    """
    Read and verify one snapshot file.

    Args:
        path (Path): JSON file.

    Returns:
        Snapshot: The verified snapshot.

    Raises:
        SnapshotError: If the file is not JSON, is tampered with, or is invalid.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"{path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SnapshotError(f"{path}: not a JSON object")
    try:
        return parse_snapshot(document)
    except SnapshotError as exc:
        raise SnapshotError(f"{path}: {exc}") from exc


def load_snapshot_dir(directory: Path) -> list[Snapshot]:
    """
    Read and verify every `*.json` snapshot in a directory, oldest publication first.

    Args:
        directory (Path): Snapshot directory; a missing directory holds no snapshots.

    Returns:
        list[Snapshot]: Verified snapshots.

    Raises:
        SnapshotError: If any file is invalid, or two files claim one id with different content.
    """
    if not directory.is_dir():
        return []
    snapshots: dict[str, Snapshot] = {}
    for path in sorted(directory.glob("*.json")):
        snap = load_snapshot(path)
        other = snapshots.get(snap.snapshot_id)
        if other and other.content_sha256 != snap.content_sha256:
            raise SnapshotError(f"{path}: another file holds {snap.snapshot_id} with other content")
        snapshots[snap.snapshot_id] = snap
    return sorted(snapshots.values(), key=_recency)


def _recency(snap: Snapshot) -> tuple[datetime, datetime, str]:
    """
    Sort key: publication date, then capture time, then id.

    Args:
        snap (Snapshot): A snapshot.

    Returns:
        tuple[datetime, datetime, str]: The key.
    """
    return snap.publication_date, snap.captured_at, snap.snapshot_id


def latest(snapshots: list[Snapshot]) -> Snapshot | None:
    """
    The most recently published snapshot.

    Args:
        snapshots (list[Snapshot]): Candidates.

    Returns:
        Snapshot | None: The latest, or None when there are none.
    """
    return max(snapshots, key=_recency, default=None)


def write_snapshot(snap: Snapshot, directory: Path) -> tuple[Path, bool]:
    """
    Write `<snapshot_id>.json`; an existing file must hold identical content.

    Args:
        snap (Snapshot): The snapshot.
        directory (Path): Output directory (created if missing).

    Returns:
        tuple[Path, bool]: The file and whether it was written now.

    Raises:
        SnapshotError: If the file exists with different content (ids are immutable).
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snap.snapshot_id}.json"
    if path.exists():
        if load_snapshot(path).content_sha256 != snap.content_sha256:
            raise SnapshotError(f"{path} exists with different content; ids are immutable")
        return path, False
    text = json.dumps(snap.document(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return path, True
