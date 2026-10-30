"""
Connected-side capture of the AWS EC2 Price List bulk CSV into a snapshot (ADR-0003).

The engine never runs this: it only imports snapshot files. Column names and values below were
checked against the us-east-1 file published 2026-10-29 (docs/discovery.md, "AWS pricing").
"""

from __future__ import annotations

import codecs
import csv
import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO

import httpx

from ttv.pricing.catalog import CATALOG_VERSION
from ttv.pricing.mapping import InstanceMap, MappingKind
from ttv.pricing.snapshot import (
    PARSER_VERSION,
    Rate,
    Snapshot,
    Unpriced,
    decimal_str,
    per_gpu_rate,
)

PRICE_LIST_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/{region}/index.csv"
)
# Every kept row must carry exactly these values ("Region Code" must equal the region too).
FILTERS: dict[str, str] = {
    "TermType": "OnDemand",
    "MarketOption": "OnDemand",
    "Operating System": "Linux",
    "Tenancy": "Shared",
    "Pre Installed S/W": "NA",
    "CapacityStatus": "Used",
    "License Model": "No License required",
    "Unit": "Hrs",
    "Product Family": "Compute Instance",
    "Location Type": "AWS Region",
    "Currency": "USD",
    "operation": "RunInstances",
}
_METADATA = ("FormatVersion", "Publication Date", "Version", "OfferCode")
_COLUMNS = (
    *FILTERS,
    "Region Code",
    "Instance Type",
    "SKU",
    "RateCode",
    "PricePerUnit",
    "EffectiveDate",
    "PriceDescription",
    "GPU",
    "GPU Memory",
    "vCPU",
    "Memory",
)
_CHUNK = 1 << 16


class CaptureError(RuntimeError):
    """The price list could not be read, or does not give one clean price per instance type."""


@dataclass(frozen=True)
class PriceRow:
    """
    One On-Demand price row kept by the filters.

    Attributes:
        instance_type (str): Instance type.
        sku (str): SKU.
        rate_code (str): RateCode.
        price_per_unit (str): PricePerUnit as published.
        effective_date (str): EffectiveDate of the term.
        description (str): PriceDescription.
        gpu (str): GPU count column, verbatim.
        gpu_memory (str): GPU Memory column, verbatim.
        vcpu (str): vCPU column.
        memory (str): Memory column (host RAM).
    """

    instance_type: str
    sku: str
    rate_code: str
    price_per_unit: str
    effective_date: str
    description: str
    gpu: str
    gpu_memory: str
    vcpu: str
    memory: str


@dataclass
class ParsedPriceList:
    """
    The metadata block and the kept rows of one price list file.

    Attributes:
        metadata (dict[str, str]): FormatVersion, Publication Date, Version, OfferCode, ...
        rows (dict[str, list[PriceRow]]): Kept rows by instance type.
        rows_read (int): Data rows read in total.
    """

    metadata: dict[str, str] = field(default_factory=dict)
    rows: dict[str, list[PriceRow]] = field(default_factory=lambda: defaultdict(list))
    rows_read: int = 0

    @property
    def publication_date(self) -> datetime:
        """
        The file's `Publication Date`.

        Returns:
            datetime: Publication time (UTC).

        Raises:
            CaptureError: If missing or malformed.
        """
        raw = self.metadata.get("Publication Date", "")
        try:
            return datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError as exc:
            raise CaptureError(f"bad Publication Date {raw!r}") from exc


def parse_price_list(
    lines: Iterable[str], *, region: str, instance_types: frozenset[str]
) -> ParsedPriceList:
    """
    Read the metadata lines, the header and every data row, keeping matching On-Demand rows.

    Args:
        lines (Iterable[str]): The CSV text, line by line (newlines kept).
        region (str): Region code every kept row must carry.
        instance_types (frozenset[str]): Instance types to keep.

    Returns:
        ParsedPriceList: Metadata and kept rows.

    Raises:
        CaptureError: If the metadata or header is missing or lacks a required column.
    """
    parsed = ParsedPriceList()
    reader = csv.reader(lines)
    header: list[str] | None = None
    for record in reader:
        if not record:
            continue
        if record[0] == "SKU":
            header = record
            break
        if len(record) == 2:
            parsed.metadata[record[0]] = record[1]
    if header is None:
        raise CaptureError("no header row starting with SKU")
    missing = [m for m in _METADATA if m not in parsed.metadata]
    missing += [c for c in _COLUMNS if c not in header]
    if missing:
        raise CaptureError(f"price list lacks metadata/columns: {missing}")
    col = {name: header.index(name) for name in _COLUMNS}
    itype, region_code = col["Instance Type"], col["Region Code"]
    checks = [(col[name], value) for name, value in FILTERS.items()]
    for record in reader:
        parsed.rows_read += 1
        if len(record) != len(header) or record[itype] not in instance_types:
            continue
        if record[region_code] != region or any(record[i] != v for i, v in checks):
            continue
        parsed.rows[record[itype]].append(
            PriceRow(
                instance_type=record[itype],
                sku=record[col["SKU"]],
                rate_code=record[col["RateCode"]],
                price_per_unit=record[col["PricePerUnit"]],
                effective_date=record[col["EffectiveDate"]],
                description=record[col["PriceDescription"]],
                gpu=record[col["GPU"]],
                gpu_memory=record[col["GPU Memory"]],
                vcpu=record[col["vCPU"]],
                memory=record[col["Memory"]],
            )
        )
    return parsed


def build_snapshot(
    parsed: ParsedPriceList,
    instance_map: InstanceMap,
    *,
    region: str,
    source_url: str,
    raw_sha256: str,
    raw_bytes: int,
    captured_at: datetime,
    allow_missing: bool = False,
) -> Snapshot:
    """
    Turn a parsed price list into a snapshot: exactly one positive price per mapped instance.

    Args:
        parsed (ParsedPriceList): Parsed file.
        instance_map (InstanceMap): GPU model → instance map.
        region (str): Region code.
        source_url (str): Where the file came from.
        raw_sha256 (str): sha256 of the raw file.
        raw_bytes (int): Size of the raw file.
        captured_at (datetime): Download time.
        allow_missing (bool): Record a mapped instance with no On-Demand row as unpriced (§10)
            instead of failing.

    Returns:
        Snapshot: The sealed snapshot.

    Raises:
        CaptureError: On a missing (unless allowed) or duplicate row, a GPU count that differs
            from the map, or a non-positive price; every problem is listed.
    """
    problems: list[str] = []
    rates: list[Rate] = []
    unpriced = [
        Unpriced(gpu_model=e.gpu_model, reason=e.reason)
        for e in instance_map.entries
        if e.kind is MappingKind.UNPRICED
    ]
    version = parsed.metadata.get("Version", "")
    for entry in instance_map.priced():
        assert entry.instance_type and entry.instance_gpus and entry.confidence
        rows = parsed.rows.get(entry.instance_type, [])
        if not rows and allow_missing:
            reason = f"no On-Demand Linux shared-tenancy row in the {region} price list {version}"
            unpriced.append(
                Unpriced(
                    gpu_model=entry.gpu_model, reason=reason, instance_type=entry.instance_type
                )
            )
            continue
        if len(rows) != 1:
            problems.append(f"{entry.instance_type}: {len(rows)} matching rows, expected 1")
            continue
        row = rows[0]
        try:
            hourly = Decimal(row.price_per_unit)
        except InvalidOperation:
            problems.append(f"{entry.instance_type}: price {row.price_per_unit!r} is not a number")
            continue
        if hourly <= 0:
            problems.append(f"{entry.instance_type}: price {row.price_per_unit} is not positive")
            continue
        if row.gpu != str(entry.instance_gpus):
            problems.append(
                f"{entry.instance_type}: price list says {row.gpu!r} GPUs, map says "
                f"{entry.instance_gpus}"
            )
            continue
        rates.append(
            Rate(
                gpu_model=entry.gpu_model,
                instance_type=entry.instance_type,
                sku=row.sku,
                rate_code=row.rate_code,
                instance_hourly_usd=decimal_str(hourly),
                instance_gpus=entry.instance_gpus,
                per_gpu_hour_usd=decimal_str(per_gpu_rate(hourly, entry.instance_gpus)),
                kind="proxy" if entry.kind is MappingKind.PROXY else "exact",
                confidence=entry.confidence,
                reason=entry.reason,
                effective_date=row.effective_date,
                price_description=row.description,
                gpu_memory=row.gpu_memory,
                vcpu=row.vcpu,
                memory=row.memory,
                map_sources=list(entry.sources),
            )
        )
    if problems:
        raise CaptureError(
            "price list does not give one clean price per instance:\n  - " + "\n  - ".join(problems)
        )
    return Snapshot.create(
        region=region,
        publication_date=parsed.publication_date,
        offer_version=version,
        captured_at=captured_at,
        source_urls=[source_url],
        raw_sha256=raw_sha256,
        raw_bytes=raw_bytes,
        parser_version=PARSER_VERSION,
        map_version=instance_map.map_version,
        catalog_version=CATALOG_VERSION,
        filters={**FILTERS, "Region Code": region},
        rates=sorted(rates, key=lambda r: r.gpu_model),
        unpriced=sorted(unpriced, key=lambda u: u.gpu_model),
    )


class _RawTap:
    """Hashes, counts and optionally copies raw bytes as they stream past."""

    def __init__(self, keep: BinaryIO | None) -> None:
        """
        Start empty.

        Args:
            keep (BinaryIO | None): File that receives a copy of the raw bytes.
        """
        self.sha = hashlib.sha256()
        self.size = 0
        self.keep = keep

    def __call__(self, chunk: bytes) -> None:
        """
        Account for one chunk.

        Args:
            chunk (bytes): Raw bytes.
        """
        self.sha.update(chunk)
        self.size += len(chunk)
        if self.keep:
            self.keep.write(chunk)


def text_lines(chunks: Iterable[bytes], tap: Callable[[bytes], None]) -> Iterator[str]:
    """
    Decode UTF-8 byte chunks into lines (newlines kept), passing every chunk to `tap` first.

    Args:
        chunks (Iterable[bytes]): Raw byte chunks.
        tap (Callable[[bytes], None]): Sees every chunk (hashing, copying).

    Yields:
        str: One line at a time.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    for chunk in chunks:
        tap(chunk)
        pending += decoder.decode(chunk)
        *lines, pending = pending.split("\n")
        for line in lines:
            yield line + "\n"
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


def _file_chunks(path: Path) -> Iterator[bytes]:
    """
    Read a file in chunks.

    Args:
        path (Path): File.

    Yields:
        bytes: Chunks.
    """
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            yield chunk


def capture(
    instance_map: InstanceMap,
    *,
    region: str,
    url: str | None = None,
    from_file: Path | None = None,
    keep_raw: Path | None = None,
    allow_missing: bool = False,
    client: httpx.Client | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Snapshot:
    """
    Stream the price list (or read a saved copy), hash it and build a snapshot.

    Args:
        instance_map (InstanceMap): GPU model → instance map.
        region (str): AWS region code.
        url (str | None): Price list URL; defaults to the official regional bulk CSV.
        from_file (Path | None): Parse a previously saved raw file instead of downloading.
        keep_raw (Path | None): Save the raw download here as evidence.
        allow_missing (bool): See `build_snapshot`.
        client (httpx.Client | None): HTTP client (tests inject one).
        clock (Callable[[], datetime]): Capture time source.

    Returns:
        Snapshot: The sealed snapshot.

    Raises:
        CaptureError: On HTTP failure or a price list that fails the checks.
    """
    source_url = url or PRICE_LIST_URL.format(region=region)
    captured_at = clock()
    keep = keep_raw.open("wb") if keep_raw else None
    tap = _RawTap(keep)
    types = instance_map.instance_types()
    try:
        if from_file:
            parsed = parse_price_list(
                text_lines(_file_chunks(from_file), tap), region=region, instance_types=types
            )
        else:
            http = client or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))
            try:
                with http.stream("GET", source_url) as response:
                    if response.status_code != 200:
                        raise CaptureError(f"GET {source_url}: HTTP {response.status_code}")
                    chunks = response.iter_bytes(_CHUNK)
                    parsed = parse_price_list(
                        text_lines(chunks, tap), region=region, instance_types=types
                    )
            except httpx.HTTPError as exc:
                raise CaptureError(f"GET {source_url}: {exc}") from exc
            finally:
                if client is None:
                    http.close()
    finally:
        if keep:
            keep.close()
    return build_snapshot(
        parsed,
        instance_map,
        region=region,
        source_url=source_url,
        raw_sha256=tap.sha.hexdigest(),
        raw_bytes=tap.size,
        captured_at=captured_at,
        allow_missing=allow_missing,
    )
