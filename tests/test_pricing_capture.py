"""
Price list capture on a small synthetic CSV shaped like AWS's bulk file (ADR-0003).

The header is the real us-east-1 header of 2026-10-29; every price is unmistakably synthetic.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from ttv.pricing.capture import (
    CaptureError,
    build_snapshot,
    capture,
    parse_price_list,
    text_lines,
)
from ttv.pricing.mapping import InstanceMap

pytestmark = pytest.mark.unit

HEADER = [
    "SKU",
    "OfferTermCode",
    "RateCode",
    "TermType",
    "PriceDescription",
    "EffectiveDate",
    "StartingRange",
    "EndingRange",
    "Unit",
    "PricePerUnit",
    "Currency",
    "RelatedTo",
    "LeaseContractLength",
    "PurchaseOption",
    "OfferingClass",
    "Product Family",
    "serviceCode",
    "Location",
    "Location Type",
    "Instance Type",
    "Current Generation",
    "Instance Family",
    "vCPU",
    "Physical Processor",
    "Clock Speed",
    "Memory",
    "Storage",
    "Network Performance",
    "Processor Architecture",
    "Storage Media",
    "Volume Type",
    "Max Volume Size",
    "Max IOPS/volume",
    "Max IOPS Burst Performance",
    "Max throughput/volume",
    "Provisioned",
    "Tenancy",
    "EBS Optimized",
    "Operating System",
    "License Model",
    "Group",
    "Group Description",
    "Transfer Type",
    "From Location",
    "From Location Type",
    "To Location",
    "To Location Type",
    "usageType",
    "operation",
    "AvailabilityZone",
    "CapacityStatus",
    "ClassicNetworkingSupport",
    "Dedicated EBS Throughput",
    "Dedicated EBS Throughput Description",
    "ECU",
    "Elastic Graphics Type",
    "Enhanced Networking Supported",
    "From Region Code",
    "GPU",
    "GPU Memory",
    "Instance",
    "Instance Capacity - 10xlarge",
    "Instance Capacity - 12xlarge",
    "Instance Capacity - 16xlarge",
    "Instance Capacity - 18xlarge",
    "Instance Capacity - 24xlarge",
    "Instance Capacity - 2xlarge",
    "Instance Capacity - 32xlarge",
    "Instance Capacity - 4xlarge",
    "Instance Capacity - 8xlarge",
    "Instance Capacity - 9xlarge",
    "Instance Capacity - large",
    "Instance Capacity - medium",
    "Instance Capacity - metal",
    "Instance Capacity - xlarge",
    "Instance Family Category",
    "instanceSKU",
    "Intel AVX2 Available",
    "Intel AVX Available",
    "Intel Turbo Available",
    "MarketOption",
    "Normalization Size Factor",
    "Physical Cores",
    "Pre Installed S/W",
    "Processor Features",
    "Product Type",
    "Region Code",
    "Resource Type",
    "serviceName",
    "SnapshotArchiveFeeType",
    "To Region Code",
    "Volume API Name",
    "VPCNetworkingSupport",
]
METADATA = [
    ["FormatVersion", "v1.0"],
    ["Disclaimer", "This pricing list is for informational purposes only."],
    ["Publication Date", "2026-10-29T17:56:34Z"],
    ["Version", "20261029175634"],
    ["OfferCode", "AmazonEC2"],
]
GOOD = {
    "SKU": "SYNTH0001",
    "RateCode": "SYNTH0001.JRTCKXETXF.6YS6EN2CT7",
    "TermType": "OnDemand",
    "PriceDescription": "$80.00 per On Demand Linux p5.48xlarge Instance Hour",
    "EffectiveDate": "2026-10-01",
    "Unit": "Hrs",
    "PricePerUnit": "80.0000000000",
    "Currency": "USD",
    "Product Family": "Compute Instance",
    "Location Type": "AWS Region",
    "Instance Type": "p5.48xlarge",
    "vCPU": "192",
    "Memory": "2048 GiB",
    "Tenancy": "Shared",
    "Operating System": "Linux",
    "License Model": "No License required",
    "operation": "RunInstances",
    "CapacityStatus": "Used",
    "GPU": "8",
    "GPU Memory": "640 GB HBM3",
    "MarketOption": "OnDemand",
    "Pre Installed S/W": "NA",
    "Region Code": "us-east-1",
}
# Rows the filters must drop; each differs from GOOD in one column.
NOISE: list[dict[str, str]] = [
    {"Operating System": "Windows", "operation": "RunInstances:0002", "PricePerUnit": "99.0"},
    {"Tenancy": "Dedicated", "PricePerUnit": "88.0"},
    {"TermType": "Reserved", "PurchaseOption": "No Upfront", "PricePerUnit": "50.0"},
    {"Pre Installed S/W": "SQL Std", "operation": "RunInstances:0006", "PricePerUnit": "90.0"},
    {"CapacityStatus": "UnusedCapacityReservation", "PricePerUnit": "80.0"},
    {"MarketOption": "CapacityBlock", "operation": "RunInstances:CB", "PricePerUnit": "0.0"},
    {"Operating System": "RHEL", "operation": "RunInstances:0010", "PricePerUnit": "81.0"},
    {"Product Family": "Compute Instance (bare metal)", "PricePerUnit": "70.0"},
    {"Region Code": "us-west-2", "PricePerUnit": "70.0"},
    {"Instance Type": "m5.large", "GPU": "", "PricePerUnit": "0.096"},
]
MAP = InstanceMap.model_validate(
    {
        "map_version": "test-1",
        "retrieved": "2026-10-31",
        "size_rule": "test",
        "entries": [
            {
                "gpu_model": "NVIDIA H100 80GB",
                "kind": "exact",
                "instance_type": "p5.48xlarge",
                "instance_gpus": 8,
                "confidence": "high",
                "reason": "synthetic",
                "sources": ["https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html"],
            },
            {"gpu_model": "NVIDIA A16 16GB", "kind": "unpriced", "reason": "no equivalent"},
        ],
    }
)
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


def price_list(*rows: dict[str, str]) -> str:
    """Render metadata, header and rows (each a partial dict over GOOD) as AWS-style CSV."""
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerows(METADATA)
    writer.writerow(HEADER)
    for row in rows:
        full = GOOD | row
        writer.writerow([full.get(col, "") for col in HEADER])
    return out.getvalue()


def parse(text: str) -> Any:
    """Parse CSV text for the map's instance types."""
    return parse_price_list(
        io.StringIO(text), region="us-east-1", instance_types=MAP.instance_types()
    )


def build(text: str, **kwargs: Any) -> Any:
    """Parse and build a snapshot."""
    return build_snapshot(
        parse(text),
        MAP,
        region="us-east-1",
        source_url="https://pricing.example.invalid/index.csv",
        raw_sha256="0" * 64,
        raw_bytes=len(text),
        captured_at=NOW,
        **kwargs,
    )


def test_keeps_only_the_linux_shared_on_demand_row() -> None:
    parsed = parse(price_list({}, *NOISE))
    assert parsed.rows_read == 1 + len(NOISE)
    (row,) = parsed.rows["p5.48xlarge"]
    assert (row.sku, row.price_per_unit, row.gpu) == ("SYNTH0001", "80.0000000000", "8")
    assert parsed.metadata["Version"] == "20261029175634"


def test_snapshot_from_synthetic_list() -> None:
    snap = build(price_list({}, *NOISE))
    (rate,) = snap.rates
    assert rate.instance_hourly_usd == "80"
    assert Decimal(rate.per_gpu_hour_usd) == Decimal("10.00")
    assert rate.kind == "exact"
    assert snap.publication_date == datetime(2026, 10, 29, 17, 56, 34, tzinfo=UTC)
    assert snap.snapshot_id == "aws-ec2-ondemand-us-east-1-20261029T175634Z-map-test-1"
    assert [u.gpu_model for u in snap.unpriced] == ["NVIDIA A16 16GB"]
    assert snap.filters["MarketOption"] == "OnDemand"


def test_two_matching_rows_fail_loudly() -> None:
    with pytest.raises(CaptureError, match="2 matching rows"):
        build(price_list({}, {"SKU": "SYNTH0002"}))


def test_missing_row_fails_unless_allowed() -> None:
    with pytest.raises(CaptureError, match="0 matching rows"):
        build(price_list(*NOISE))
    snap = build(price_list(*NOISE), allow_missing=True)
    assert snap.rates == []
    missing = next(u for u in snap.unpriced if u.gpu_model == "NVIDIA H100 80GB")
    assert missing.instance_type == "p5.48xlarge"
    assert "no On-Demand" in missing.reason


def test_gpu_count_must_match_the_map() -> None:
    with pytest.raises(CaptureError, match="price list says '4' GPUs"):
        build(price_list({"GPU": "4"}))


def test_non_positive_price_fails() -> None:
    with pytest.raises(CaptureError, match="not positive"):
        build(price_list({"PricePerUnit": "0.0000000000"}))


def test_missing_column_fails() -> None:
    text = price_list({}).replace('"MarketOption"', '"Market"')
    with pytest.raises(CaptureError, match="MarketOption"):
        parse(text)


def test_text_lines_survive_split_multibyte_chunks() -> None:
    data = 'a,"café"\nb,c\n'.encode()
    chunks = [data[:6], data[6:]]  # splits the two bytes of é
    seen: list[bytes] = []
    assert list(text_lines(chunks, seen.append)) == ['a,"café"\n', "b,c\n"]
    assert b"".join(seen) == data


def test_capture_streams_hashes_and_keeps_raw(tmp_path: Path) -> None:
    body = price_list({}, *NOISE).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/us-east-1/index.csv")
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snap = capture(
            MAP, region="us-east-1", keep_raw=tmp_path / "raw.csv", client=client, clock=lambda: NOW
        )
    assert snap.raw_sha256 == hashlib.sha256(body).hexdigest()
    assert snap.raw_bytes == len(body)
    assert (tmp_path / "raw.csv").read_bytes() == body
    assert snap.source_urls == [
        "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.csv"
    ]
    assert snap.captured_at == NOW
    raw = tmp_path / "raw.csv"
    assert capture(MAP, region="us-east-1", from_file=raw, clock=lambda: NOW) == snap


def test_http_error_is_a_capture_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with httpx.Client(transport=transport) as client, pytest.raises(CaptureError, match="503"):
        capture(MAP, region="us-east-1", client=client)
