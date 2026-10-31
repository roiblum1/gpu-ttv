"""GPU model canonicalisation and the instance map (ADR-0003)."""

from __future__ import annotations

import pytest

from ttv.pricing.catalog import GPU_MODELS, canonical_gpu_model, known_models
from ttv.pricing.mapping import InstanceMap, MappingKind, load_instance_map

pytestmark = pytest.mark.unit

# Every distinct gpu_model in the lab ledger (ttv_lab_pricing, 2026-10-31) plus BMC spellings.
REAL_RAW_NAMES = {
    "NVIDIA H100 80GB": "NVIDIA H100 80GB",
    "NVIDIA A100 80GB": "NVIDIA A100 80GB",
    "NVIDIA A100 40GB": "NVIDIA A100 40GB",
    "UCSC-GPU-A100-40": "NVIDIA A100 40GB",
    "NVIDIA L40S 48GB": "NVIDIA L40S 48GB",
    "NVIDIA T4 16GB": "NVIDIA T4 16GB",
    "NVIDIA A16 16GB": "NVIDIA A16 16GB",
    "AMD Instinct MI300X 192GB": "AMD Instinct MI300X 192GB",
    "NVIDIA A100": "NVIDIA A100 (memory unknown)",
    "NVIDIA RTX A6000": None,
    "NVIDIA A100-PCIE-40GB": "NVIDIA A100 40GB",
    "NVIDIA H100 80GB HBM3": "NVIDIA H100 80GB",
    "Tesla T4": "NVIDIA T4 16GB",
    "UCSC-GPU-L40S": "NVIDIA L40S 48GB",
    "UCSX-GPU-A16": "NVIDIA A16 16GB",
    "": None,
    "   ": None,
    "unknown": None,
}


@pytest.mark.parametrize(("raw", "canonical"), sorted(REAL_RAW_NAMES.items()))
def test_canonicalises_the_raw_names_the_engine_sees(raw: str, canonical: str | None) -> None:
    assert canonical_gpu_model(raw) == canonical


def test_none_is_unidentified() -> None:
    assert canonical_gpu_model(None) is None


def test_every_catalogue_name_maps_to_itself() -> None:
    assert [n for n, _, _ in GPU_MODELS if canonical_gpu_model(n) != n] == []


def test_capacity_must_agree_and_a10_never_matches_a100() -> None:
    # A Cisco PID's trailing number must equal the row's VRAM; equality, never substring.
    assert canonical_gpu_model("UCSC-GPU-A100-41") is None
    assert canonical_gpu_model("NVIDIA A10") == "NVIDIA A10 24GB"
    assert canonical_gpu_model("NVIDIA H100") == "NVIDIA H100 (memory unknown)"  # 80 or 94 GB


def test_instance_map_covers_every_catalogue_model_once() -> None:
    m = load_instance_map()
    assert {e.gpu_model for e in m.entries} == known_models()
    for entry in m.priced():
        assert entry.sources, entry.gpu_model


def test_proxies_are_only_the_user_approved_ones() -> None:
    # Approved 2026-10-31 (docs/adr/0003-aws-pricing.md); a new proxy must be approved first.
    proxies = [e.gpu_model for e in load_instance_map().entries if e.kind is MappingKind.PROXY]
    assert sorted(proxies) == [
        "NVIDIA A10 24GB",
        "NVIDIA A100 (memory unknown)",
        "NVIDIA H100 (memory unknown)",
        "NVIDIA H100 94GB",
        "NVIDIA H100 NVL 94GB",
    ]


def test_b300_and_h100_95gb_are_recognised() -> None:
    assert canonical_gpu_model("NVIDIA B300 SXM6 AC") == "NVIDIA B300 288GB"
    assert canonical_gpu_model("NVIDIA B300") == "NVIDIA B300 288GB"
    assert canonical_gpu_model("NVIDIA H100 95GB") == "NVIDIA H100 94GB"
    assert canonical_gpu_model("NVIDIA H100 NVL") == "NVIDIA H100 NVL 94GB"


def test_map_refuses_unknown_models_and_inconsistent_entries() -> None:
    base = {"map_version": "t", "retrieved": "2026-10-31", "size_rule": "t"}
    with pytest.raises(ValueError, match="not canonical"):
        InstanceMap.model_validate(
            base | {"entries": [{"gpu_model": "NVIDIA X1", "kind": "unpriced", "reason": "r"}]}
        )
    with pytest.raises(ValueError, match="needs instance_type"):
        InstanceMap.model_validate(
            base | {"entries": [{"gpu_model": "NVIDIA T4 16GB", "kind": "exact", "reason": "r"}]}
        )
