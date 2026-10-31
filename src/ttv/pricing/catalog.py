"""
Canonical GPU model names, so raw inventory strings join to pricing rates (ADR-0003).

Ported from server-scan @defe172 `backend/app/domain/value_objects/gpu_models.py` (table) and
`gpu_catalog.py` (matching); github.com/team-redbull/server-scan. Local additions are marked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

# Bump the suffix whenever the table or the matching rules below change.
CATALOG_VERSION = "server-scan@defe172+ttv.2"

# (canonical name, VRAM in GB, identifiers this row matches), verbatim from server-scan.
GPU_MODELS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("NVIDIA Tesla P100 12GB", 12, ("UCSC-GPU-P100-12G", "Tesla P100-PCIE-12GB", "P100-12GB")),
    ("NVIDIA Tesla P100 16GB", 16, ("UCSC-GPU-P100-16G", "Tesla P100-PCIE-16GB", "P100-16GB")),
    (
        "NVIDIA Tesla V100 16GB",
        16,
        ("UCSC-GPU-V100", "Tesla V100-PCIE-16GB", "Tesla V100-SXM2-16GB", "V100-16GB"),
    ),
    (
        "NVIDIA Tesla V100 32GB",
        32,
        (
            "UCSC-GPU-V100-32",
            "Tesla V100-PCIE-32GB",
            "Tesla V100-SXM2-32GB",
            "Tesla V100S-PCIE-32GB",
            "V100-32GB",
        ),
    ),
    ("NVIDIA T4 16GB", 16, ("UCSC-GPU-T4-16", "UCSX-GPU-T4-16", "Tesla T4", "T4")),
    ("NVIDIA Quadro RTX 6000 24GB", 24, ("UCSC-GPU-RTX6000", "Quadro RTX 6000")),
    ("NVIDIA Quadro RTX 8000 48GB", 48, ("UCSC-GPU-RTX8000", "Quadro RTX 8000")),
    ("NVIDIA A2 16GB", 16, ("A2",)),
    ("NVIDIA A10 24GB", 24, ("UCSC-GPU-A10", "A10", "A10G")),
    (
        "NVIDIA A16 16GB",
        16,
        (
            "UCSC-GPU-A16",
            "UCSC-GPU-A16-D",
            "UCSC-GPU-A165",
            "UCSX-GPU-A16",
            "UCSX-GPU-A16-D",
            "A16",
        ),
    ),
    ("NVIDIA A30 24GB", 24, ("UCSC-GPU-A30", "UCSC-GPU-A30-D", "A30")),
    ("NVIDIA A40 48GB", 48, ("UCSC-GPU-A40", "UCSC-GPU-A40-D", "UCSX-GPU-A40", "A40")),
    (
        "NVIDIA A100 40GB",
        40,
        ("UCSC-GPU-A100", "A100-PCIE-40GB", "A100-SXM4-40GB", "A100 40GB PCIe", "A100 40GB"),
    ),
    (
        "NVIDIA A100 80GB",
        80,
        (
            "UCSC-GPU-A100-80",
            "UCSC-GPU-A100-805",
            "UCSC-GPUA100-80-D",
            "UCSX-GPU-A100-80",
            "UCSX-GPU-A100-80-D",
            "A100-PCIE-80GB",
            "A100-SXM4-80GB",
            "A100 80GB PCIe",
            "A100 80GB SXM",
            "A100 80GB",
        ),
    ),
    ("NVIDIA A800 40GB", 40, ("A800-SXM4-40GB", "A800 40GB")),
    ("NVIDIA A800 80GB", 80, ("A800-SXM4-80GB", "A800-PCIE-80GB", "A800 80GB")),
    ("NVIDIA H800 80GB", 80, ("H800-SXM5-80GB", "H800 PCIe", "H800 80GB", "H800")),
    ("NVIDIA L4 24GB", 24, ("UCSC-GPU-L4", "UCSC-GPU-L4M6", "UCSX-GPU-L4", "L4")),
    ("NVIDIA L40 48GB", 48, ("UCSC-GPU-L40", "UCSX-GPU-L40", "L40")),
    ("NVIDIA L40S 48GB", 48, ("UCSC-GPU-L40S", "UCSX-GPU-L40S", "L40S")),
    (
        "NVIDIA H100 80GB",
        80,
        (
            "UCSC-GPU-H100-80",
            "UCSX-GPU-H100-80",
            "H100-PCIE-80GB",
            "H100-SXM5-80GB",
            "H100 80GB HBM3",
            "H100 PCIe",
            "H100 80GB",
        ),
    ),
    (
        "NVIDIA H100 NVL 94GB",
        94,
        ("UCSC-GPU-H100-NVL", "UCSX-GPU-H100-NVL", "H100 NVL", "H100 NVL 94GB"),
    ),
    ("NVIDIA H200 141GB", 141, ("H200", "H200 NVL", "H200-SXM-141GB", "H200 141GB")),
    ("NVIDIA B200 180GB", 180, ("B200", "B200 180GB")),
    # Local additions, absent from server-scan (defe172 and main of 2026-10-29); user, 2026-10-31.
    (
        "NVIDIA B300 288GB",
        288,
        ("B300", "B300 288GB", "B300 268GB", "B300 SXM6 AC", "HGX B300"),
    ),
    ("NVIDIA H100 94GB", 94, ("H100 94GB", "H100 95GB")),
    (
        "NVIDIA RTX PRO 6000 Blackwell 96GB",
        96,
        ("RTX PRO 6000 Blackwell Server Edition", "RTX PRO 6000 Blackwell"),
    ),
    ("AMD Instinct MI100 32GB", 32, ("Instinct MI100", "MI100")),
    ("AMD Instinct MI210 64GB", 64, ("UCSX-GPU-MI210", "Instinct MI210", "MI210")),
    ("AMD Instinct MI250 128GB", 128, ("Instinct MI250", "MI250")),
    ("AMD Instinct MI250X 128GB", 128, ("Instinct MI250X", "MI250X")),
    ("AMD Instinct MI300A 128GB", 128, ("Instinct MI300A", "MI300A")),
    ("AMD Instinct MI300X 192GB", 192, ("Instinct MI300X", "MI300X")),
    ("AMD Instinct MI325X 256GB", 256, ("Instinct MI325X", "MI325X")),
    ("AMD Instinct MI355X 288GB", 288, ("Instinct MI355X", "MI355X")),
)

# Local addition: a bare family name shipped in several capacities (e.g. `NVIDIA A100`).
MEMORY_UNKNOWN_SUFFIX = " (memory unknown)"
# Local addition: Cisco PIDs whose trailing number is the capacity in GB (`UCSC-GPU-A100-40`).
_CISCO_PID_PREFIXES = frozenset({"UCSC", "UCSX", "UCSB"})

_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")
_VENDOR_PREFIXES = frozenset({"HPE", "HP", "NVIDIA", "AMD", "INTEL", "TESLA", "QUADRO"})
_TRAILING_NOISE = frozenset(
    {
        "ACCELERATOR",
        "ACCELERATORS",
        "COMPUTATIONAL",
        "GRAPHICS",
        "MODULE",
        "ADAPTER",
        "ADPTR",
        "CARD",
        "KIT",
        "GPU",
    }
)
_TRAILING_WATTAGE = re.compile(r"\d+W")
_FORM_FACTOR_WORDS = frozenset({"PCIE", "SXM", "SXM2", "SXM4", "SXM5", "SXM6", "OAM"})
_CAPACITY_WORD = re.compile(r"(\d+)GB")
_CAPACITY_SUFFIX = re.compile(r"\s+\d+GB$")


@dataclass(frozen=True, slots=True)
class GpuModel:
    """
    One catalogue row with its normalised match keys.

    Attributes:
        name (str): Canonical name, e.g. `NVIDIA A100 40GB`.
        memory_gb (int): VRAM in GB.
        keys (tuple[str, ...]): Normalised identifiers that match this row.
    """

    name: str
    memory_gb: int
    keys: tuple[str, ...]


def _words(identifier: str) -> list[str]:
    """
    Split an identifier into meaningful upper-case words (server-scan's rule).

    Args:
        identifier (str): A Cisco PID or a vendor model string.

    Returns:
        list[str]: Words without leading vendor words or trailing noise and wattage.
    """
    words = [word for word in _SEPARATORS.split(identifier.upper()) if word]
    while words and words[0] in _VENDOR_PREFIXES:
        words.pop(0)
    while words and (words[-1] in _TRAILING_NOISE or _TRAILING_WATTAGE.fullmatch(words[-1])):
        words.pop()
    return words


def _normalize(identifier: str) -> str:
    """
    Reduce an identifier to the key compared for equality (never as a substring).

    Args:
        identifier (str): A Cisco PID or a vendor model string.

    Returns:
        str: The key, or "" when nothing but vendor words and punctuation remain.
    """
    return "".join(_words(identifier))


@cache
def _rows() -> tuple[GpuModel, ...]:
    """
    Build the catalogue rows once.

    Returns:
        tuple[GpuModel, ...]: Rows in table order.
    """
    rows = []
    for name, memory_gb, identifiers in GPU_MODELS:
        keys: list[str] = []
        for identifier in identifiers:
            key = _normalize(identifier)
            if key and key not in keys:
                keys.append(key)
        rows.append(GpuModel(name, memory_gb, tuple(keys)))
    return tuple(rows)


@cache
def _ambiguous_families() -> dict[str, str]:
    """
    Families shipped in two or more capacities, keyed by normalised family name.

    Returns:
        dict[str, str]: e.g. `{"A100": "NVIDIA A100 (memory unknown)"}`.
    """
    names: dict[str, list[str]] = {}
    for name, _, _ in GPU_MODELS:
        family = _CAPACITY_SUFFIX.sub("", name)
        names.setdefault(family, []).append(name)
    return {
        _normalize(family): family + MEMORY_UNKNOWN_SUFFIX
        for family, members in names.items()
        if len(members) > 1
    }


def _by_key(key: str) -> GpuModel | None:
    """
    Find the row that lists a normalised key.

    Args:
        key (str): Normalised identifier.

    Returns:
        GpuModel | None: The row, or None.
    """
    return next((row for row in _rows() if key in row.keys), None)


def _by_capacity(base: str, memory_gb: int) -> GpuModel | None:
    """
    Find the row that lists `base` and has exactly `memory_gb` of VRAM.

    Args:
        base (str): Normalised identifier without its capacity word.
        memory_gb (int): Capacity named by the identifier.

    Returns:
        GpuModel | None: The row, or None when the capacity disagrees.
    """
    return next((r for r in _rows() if base in r.keys and r.memory_gb == memory_gb), None)


@cache
def canonical_gpu_model(raw: str | None) -> str | None:
    """
    Canonical name of a raw GPU model string, or None when it cannot be identified.

    Tried in order: exact key; minus form-factor words; `<model> <N>GB` against a row with
    N GB; a Cisco PID ending in N; a family shipped in several capacities.

    Args:
        raw (str | None): Model as server-scan reported it (canonical names map to themselves).

    Returns:
        str | None: Canonical name, `<family> (memory unknown)`, or None.
    """
    words = _words(raw or "")
    if not words:
        return None
    stripped = [w for w in words if w not in _FORM_FACTOR_WORDS]
    candidates = ["".join(words), "".join(stripped)]
    for candidate in candidates:
        if row := _by_key(candidate):
            return row.name
    capacity = _CAPACITY_WORD.fullmatch(stripped[-1]) if stripped else None
    if capacity and (row := _by_capacity("".join(stripped[:-1]), int(capacity[1]))):
        return row.name
    pid = words[0] in _CISCO_PID_PREFIXES and words[-1].isdigit()
    if pid and (row := _by_capacity("".join(words[:-1]), int(words[-1]))):
        return row.name
    families = _ambiguous_families()
    return next((families[c] for c in candidates if c in families), None)


def known_models() -> frozenset[str]:
    """
    Every canonical name the catalogue can produce, including memory-unknown families.

    Returns:
        frozenset[str]: Canonical names.
    """
    return frozenset(row.name for row in _rows()) | frozenset(_ambiguous_families().values())
