"""The versioned GPU model → AWS instance map (`config/pricing/gpu-instance-map.yaml`, ADR-0003)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ttv.pricing.catalog import known_models

DEFAULT_MAP_FILE = Path("config/pricing/gpu-instance-map.yaml")


class MappingKind(StrEnum):
    """How defensible a mapping is; only `exact` and `proxy` produce a rate."""

    EXACT = "exact"
    PROXY = "proxy"
    UNPRICED = "unpriced"


class Confidence(StrEnum):
    """Mapping confidence recorded on every rate."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MapEntry(BaseModel):
    """
    One canonical GPU model and the AWS instance it is compared with, or why there is none.

    Attributes:
        gpu_model (str): Canonical name from `ttv.pricing.catalog`.
        kind (MappingKind): `exact`, `proxy` (needs user approval) or `unpriced`.
        instance_type (str | None): AWS instance type; None when unpriced.
        instance_gpus (int | None): Physical GPUs in that instance per AWS docs (cross-checked
            against the price list's GPU column at capture).
        confidence (Confidence | None): Mapping confidence; None when unpriced.
        reason (str): Why this mapping, including hardware differences.
        sources (list[str]): Official URLs the mapping was read from.
    """

    model_config = ConfigDict(extra="forbid")

    gpu_model: str = Field(min_length=1)
    kind: MappingKind
    instance_type: str | None = None
    instance_gpus: int | None = Field(default=None, gt=0)
    confidence: Confidence | None = None
    reason: str = Field(min_length=1)
    sources: list[str] = []

    @model_validator(mode="after")
    def _consistent(self) -> MapEntry:
        """
        Require instance facts for priced kinds and none for unpriced ones.

        Returns:
            MapEntry: The validated entry.

        Raises:
            ValueError: On a missing or superfluous field.
        """
        priced = self.kind is not MappingKind.UNPRICED
        facts = (self.instance_type, self.instance_gpus, self.confidence)
        if priced and any(f is None for f in facts):
            raise ValueError(f"{self.gpu_model}: {self.kind} needs instance_type, gpus, confidence")
        if not priced and any(f is not None for f in facts):
            raise ValueError(f"{self.gpu_model}: unpriced entries name no instance")
        return self


class InstanceMap(BaseModel):
    """
    The whole map; its version is part of every snapshot id built from it.

    Attributes:
        map_version (str): Bumped on every change, e.g. `2026-10-31.1`.
        retrieved (str): Date the AWS documentation was read.
        size_rule (str): How the instance size was chosen.
        entries (list[MapEntry]): One per canonical GPU model.
    """

    model_config = ConfigDict(extra="forbid")

    map_version: str = Field(pattern=r"^[0-9A-Za-z.\-]+$")
    retrieved: str
    size_rule: str
    entries: list[MapEntry]

    @model_validator(mode="after")
    def _known_and_unique(self) -> InstanceMap:
        """
        Refuse unknown canonical names and duplicates.

        Returns:
            InstanceMap: The validated map.

        Raises:
            ValueError: On a name the catalogue cannot produce, or a repeated model.
        """
        names = [e.gpu_model for e in self.entries]
        unknown = sorted(set(names) - known_models())
        if unknown:
            raise ValueError(f"not canonical GPU models (see ttv.pricing.catalog): {unknown}")
        if len(names) != len(set(names)):
            raise ValueError("a GPU model is mapped twice")
        return self

    def priced(self) -> list[MapEntry]:
        """
        Entries that produce a rate.

        Returns:
            list[MapEntry]: `exact` and `proxy` entries.
        """
        return [e for e in self.entries if e.kind is not MappingKind.UNPRICED]

    def instance_types(self) -> frozenset[str]:
        """
        Instance types the capture must find in the price list.

        Returns:
            frozenset[str]: Instance types of priced entries.
        """
        return frozenset(e.instance_type for e in self.priced() if e.instance_type)


def load_instance_map(path: Path = DEFAULT_MAP_FILE) -> InstanceMap:
    """
    Load and validate the map file.

    Args:
        path (Path): YAML file.

    Returns:
        InstanceMap: The validated map.
    """
    return InstanceMap.model_validate(yaml.safe_load(path.read_text()))
