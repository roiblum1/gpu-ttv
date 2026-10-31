"""Tests for the read-only Dell OpenManage reader (fixture-tested: synthetic OME/iDRAC JSON)."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from ttv.collector import CollectorSource
from ttv.collector.openmanage import OpenManageSource, check_request, gpu_models_from_inventory
from ttv.collector.redfish_gpu import check_request as check_redfish_request
from ttv.collector.redfish_gpu import idrac_origin, is_gpu_pcie_device, pcie_gpu_model
from ttv.config import OpenManageConnection
from ttv.models import CollectorRead, ManagerServer
from ttv.sources.http import SourceError, SourceErrorKind, UnsafeRequestError

FIXTURES = Path(__file__).parent / "fixtures" / "openmanage"
OME = "https://ome.example.test"
OME_HOST = "ome.example.test"
NOW = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)
TOKEN = "synthetic-ome-token-never-log-me"
OME_PASSWORD = "synthetic-ome-password-never-log-me"
IDRAC_USER = "ttv-idrac-ro"
IDRAC_PASSWORD = "synthetic-idrac-password-never-log-me"
BASIC = "Basic " + base64.b64encode(f"{IDRAC_USER}:{IDRAC_PASSWORD}".encode()).decode()
SESSION_ID = "7c4f0e1a-5b2d-4c8e-9f31-0a6b2d9e4c11"
LOGIN = "/api/SessionService/Sessions"
LOGOUT = f"/api/SessionService/Sessions('{SESSION_ID}')"
DEVICES_1 = "/api/DeviceService/Devices?$filter=Type%20eq%201000"
DEVICES_2 = "/api/DeviceService/Devices?$skip=3&$top=3&$filter=Type%20eq%201000"
PROFILES = "/api/ProfileService/Profiles"
SYS = "/redfish/v1/Systems/System.Embedded.1"
PCIE = "/redfish/v1/Chassis/System.Embedded.1/PCIeDevices"
H100_80 = "GH100 [H100 SXM5 80GB] 3D controller"
H100_94 = "GH100 [H100 SXM5 94GB] 3D controller"

Reply = Callable[[], httpx.Response]
Key = tuple[str, str, str]


def load(rel: str) -> Any:
    return json.loads((FIXTURES / rel).read_text())


def ok(body: Any, code: int = 200, **headers: str) -> Reply:
    return lambda: httpx.Response(code, json=body, headers=headers)


def status(code: int) -> Reply:
    return lambda: httpx.Response(code, json={"error": {"code": "Base.1.0.GeneralError"}})


def inventory(device_id: int, kind: str = "serverDeviceCards") -> str:
    return f"/api/DeviceService/Devices({device_id})/InventoryDetails('{kind}')"


class FakeDell:
    """A MockTransport handler for one OME and its iDRACs, keyed by (method, host, path?query)."""

    def __init__(self) -> None:
        self.routes: dict[Key, list[Reply]] = {}
        self.delays: dict[Key, float] = {}
        self.requests: list[httpx.Request] = []
        self.unexpected: list[Key] = []

    def route(self, method: str, host: str, target: str, *replies: Reply) -> None:
        self.routes[(method, host, target)] = list(replies)

    def get(self, host: str, target: str, *replies: Reply) -> None:
        self.route("GET", host, target, *replies)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.host, request.url.raw_path.decode())
        if key in self.delays:
            await asyncio.sleep(self.delays[key])
        replies = self.routes.get(key)
        if not replies:
            self.unexpected.append(key)
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        return (replies.pop(0) if len(replies) > 1 else replies[0])()

    def sent(self, host: str | None = None) -> list[Key]:
        return [
            (r.method, r.url.host, r.url.raw_path.decode())
            for r in self.requests
            if host is None or r.url.host == host
        ]


def idrac(fake: FakeDell, host: str, processors: str, pcie: dict[str, str] | None = None) -> None:
    """Serve one iDRAC: one system, its processors, and its PCIe devices by id → fixture."""
    collection = load(f"redfish/{processors}")
    fake.get(host, "/redfish/v1/Systems", ok(load("redfish/systems.json")))
    fake.get(host, SYS, ok(load("redfish/system.json")))
    fake.get(host, f"{SYS}/Processors", ok(collection))
    for member in collection["Members"]:
        path = member["@odata.id"]
        kind = "gpu" if "/Video." in path else "cpu"
        fake.get(host, path, ok(load(f"redfish/processor-{kind}.json")))
    for device_id, fixture in (pcie or {}).items():
        fake.get(host, f"{PCIE}/{device_id}", ok(load(f"redfish/{fixture}.json")))


def dell_fleet() -> FakeDell:
    """Five server devices over two pages, plus a chassis; see tests/fixtures/openmanage."""
    fake = FakeDell()
    session = ok(load("ome/session.json"), 201, **{"X-Auth-Token": TOKEN})
    fake.route("POST", OME_HOST, LOGIN, session)
    fake.route("DELETE", OME_HOST, LOGOUT, lambda: httpx.Response(204))
    fake.get(OME_HOST, DEVICES_1, ok(load("ome/devices-page1.json")))
    fake.get(OME_HOST, DEVICES_2, ok(load("ome/devices-page2.json")))
    fake.get(OME_HOST, PROFILES, ok(load("ome/profiles.json")))
    fake.get(OME_HOST, inventory(10101), ok(load("ome/inventory-8gpu.json")))
    for device_id in (10102, 10103, 10104):
        fake.get(OME_HOST, inventory(device_id), ok(load("ome/inventory-no-gpu.json")))
    fake.get(OME_HOST, inventory(10105), ok(load("ome/inventory-4gpu.json")))
    idrac(fake, "192.0.2.12", "processors-gpu.json")
    pcie_gpus = {
        "59-0": "pcie-gpu-pci-id",
        "60-0": "pcie-gpu-named",
        "3-0": "pcie-nic",
        "1-0": "pcie-raid",
    }
    idrac(fake, "192.0.2.13", "processors-cpu.json", pcie_gpus)
    no_gpus = {"59-0": "pcie-nic", "60-0": "pcie-gpu-absent", "3-0": "pcie-nic", "1-0": "pcie-raid"}
    idrac(fake, "192.0.2.14", "processors-cpu.json", no_gpus)
    return fake


def make_source(
    tmp_path: Path, fake: FakeDell, *, redfish: bool = True, **kwargs: Any
) -> OpenManageSource:
    (tmp_path / "ome.pass").write_text(OME_PASSWORD + "\n")
    (tmp_path / "idrac.pass").write_text(IDRAC_PASSWORD + "\n")
    conn = OpenManageConnection(
        name="ome-site1",
        url=OME,
        username="ttv-viewer",
        password_file=tmp_path / "ome.pass",
        idrac_username=IDRAC_USER if redfish else None,
        idrac_password_file=tmp_path / "idrac.pass" if redfish else None,
    )
    kwargs.setdefault("timeout_seconds", 30.0)
    return OpenManageSource(
        conn, transport=httpx.MockTransport(fake.handler), retry_delay=0.0, **kwargs
    )


async def read_once(source: OpenManageSource, now: datetime = NOW) -> CollectorRead:
    async with source:
        return await source.read(now)


def by_id(read: CollectorRead) -> dict[str, ManagerServer]:
    return {server.source_id: server for server in read.servers}


async def test_read_reports_every_server_with_its_profile_and_gpus(tmp_path: Path) -> None:
    fake = dell_fleet()
    source = make_source(tmp_path, fake)
    implements: CollectorSource = source
    assert implements.name == "openmanage:ome-site1"
    read = await read_once(source)

    assert fake.unexpected == []
    assert (read.manager, read.kind, read.complete, read.errors) == (
        "openmanage:ome-site1",
        "openmanage",
        True,
        (),
    )
    assert read.started_at == NOW and read.finished_at >= NOW
    servers = by_id(read)
    assert list(servers) == ["10101", "10102", "10103", "10104", "10105"]
    assert {s.vendor for s in read.servers} == {"dell"}
    assert {s.manager for s in read.servers} == {"openmanage:ome-site1"}

    no_profile = servers["10101"]
    assert (no_profile.serial_normalized, no_profile.name, no_profile.model) == (
        "gpu8np1",
        "idrac-gpu8np1",
        "PowerEdge XE9680",
    )
    assert no_profile.profile_name is None
    assert no_profile.gpu_models == (H100_80,) * 8
    assert no_profile.domain is None
    assert no_profile.evidence == {
        "device_id": 10101,
        "last_inventory_time": "2026-10-30 04:00:11.123",
        "gpu_source": "ome-inventory",
        "gpu_rule": "serverDeviceCards",
        "gpu_read_at": NOW.isoformat(),
    }

    ocp = servers["10102"]
    assert ocp.profile_name == "ocp4-prod-site1-gpu-07"
    assert ocp.gpu_models == ("NVIDIA L40S",) * 4
    assert ocp.evidence["profile_id"] == 501
    assert ocp.evidence["profile_state"] == 4
    assert ocp.evidence["profile_join"] == "target_id"
    assert (ocp.evidence["gpu_source"], ocp.evidence["gpu_rule"]) == ("redfish", "processors")

    non_ocp = servers["10103"]
    assert non_ocp.profile_name == "vdi-farm-03"
    assert non_ocp.evidence["profile_join"] == "target_name"
    assert non_ocp.gpu_models == ("A10", "GA102GL [A10] 3D controller")
    assert (non_ocp.evidence["gpu_source"], non_ocp.evidence["gpu_rule"]) == ("redfish", "pcie")

    spare = servers["10104"]
    assert spare.profile_name is None
    assert spare.gpu_models == ()
    assert spare.evidence["gpu_rule"] == "pcie"

    staged = servers["10105"]
    assert staged.serial_normalized == ""
    assert staged.profile_name == "ocp4-staged-gpu-09"
    assert staged.evidence["profile_state"] == 0
    assert staged.gpu_models == (H100_94,) * 4

    assert read.coverage == {
        "devices_listed": 6,
        "servers": 5,
        "non_server_devices": 1,
        "devices_malformed": 0,
        "profiles": 5,
        "profiles_malformed": 0,
        "profiles_bound": 3,
        "profiles_untargeted": 1,
        "profiles_unmatched": 1,
        "profiles_ambiguous": 0,
        "profile_path": PROFILES,
        "servers_with_profile": 3,
        "servers_with_several_profiles": 0,
        "servers_without_serial": 1,
        "servers_with_gpus": 4,
        "gpu_ome_inventory": 2,
        "gpu_redfish": 3,
        "gpu_from_cache": 0,
        "gpu_unread": 0,
        "gpu_read_failed": 0,
    }
    sent = fake.sent()
    assert sent[0] == ("POST", OME_HOST, LOGIN)
    assert sent[-1] == ("DELETE", OME_HOST, LOGOUT)
    assert not any(host in {"192.0.2.11", "192.0.2.15"} for _, host, _ in sent)


async def test_credentials_go_only_where_they_belong(tmp_path: Path) -> None:
    fake = dell_fleet()
    await read_once(make_source(tmp_path, fake))
    for request in fake.requests:
        if request.url.host == OME_HOST:
            assert "authorization" not in request.headers
            expected = None if request.method == "POST" else TOKEN
            assert request.headers.get("x-auth-token") == expected
        else:
            assert request.headers["authorization"] == BASIC
            assert "x-auth-token" not in request.headers
    login = json.loads(fake.requests[0].content)
    assert login == {"UserName": "ttv-viewer", "Password": OME_PASSWORD, "SessionType": "API"}


async def test_secrets_are_never_logged_or_recorded(tmp_path: Path) -> None:
    fake = dell_fleet()
    fake.get(OME_HOST, inventory(10104), status(500))
    fake.get("192.0.2.14", "/redfish/v1/Systems", status(401))
    with capture_logs() as logs:
        read = await read_once(make_source(tmp_path, fake))
        fake.route("POST", OME_HOST, LOGIN, status(401))
        with pytest.raises(SourceError) as failed:
            await read_once(make_source(tmp_path, fake))
    recorded = json.dumps(
        {
            "logs": logs,
            "errors": read.errors,
            "coverage": read.coverage,
            "evidence": [s.evidence for s in read.servers],
            "error": str(failed.value),
        },
        default=str,
    )
    assert "openmanage_read" in recorded
    for secret in (OME_PASSWORD, IDRAC_PASSWORD, TOKEN, BASIC.split()[1]):
        assert secret not in recorded


async def test_without_redfish_unconfirmed_gpus_stay_unread(tmp_path: Path) -> None:
    fake = dell_fleet()
    read = await read_once(make_source(tmp_path, fake, redfish=False))
    servers = by_id(read)
    for device_id in ("10102", "10103", "10104"):
        assert servers[device_id].gpu_models is None
        assert servers[device_id].evidence["gpu_source"] is None
        assert "not configured" in servers[device_id].evidence["gpu_note"]
    assert servers["10101"].gpu_models == (H100_80,) * 8
    assert read.complete is True
    assert (read.coverage["gpu_unread"], read.coverage["gpu_read_failed"]) == (3, 0)
    assert {host for _, host, _ in fake.sent()} == {OME_HOST}


async def test_empty_inventory_is_trusted_only_when_configured(tmp_path: Path) -> None:
    fake = dell_fleet()
    source = make_source(tmp_path, fake, redfish=False, empty_inventory_means_no_gpu=True)
    servers = by_id(await read_once(source))
    for device_id in ("10102", "10103", "10104"):
        assert servers[device_id].gpu_models == ()
        assert servers[device_id].evidence["gpu_source"] == "ome-inventory"


async def test_pcie_fallback_off_trusts_processors(tmp_path: Path) -> None:
    fake = dell_fleet()
    servers = by_id(await read_once(make_source(tmp_path, fake, pcie_fallback=False)))
    assert servers["10103"].gpu_models == ()
    assert servers["10103"].evidence["gpu_rule"] == "processors"
    assert not any("/PCIeDevices/" in path for _, _, path in fake.sent())


async def test_too_many_redfish_members_leave_gpus_unread(tmp_path: Path) -> None:
    fake = dell_fleet()
    read = await read_once(make_source(tmp_path, fake, max_members=3))
    servers = by_id(read)
    assert servers["10103"].gpu_models is None
    assert servers["10103"].evidence["gpu_note"] == "more than 3 PCIe devices to screen"
    assert servers["10102"].gpu_models is None
    assert "more than 3 members" in servers["10102"].evidence["gpu_note"]
    assert read.complete is False


async def test_login_rejected_is_permission_denied(tmp_path: Path) -> None:
    fake = dell_fleet()
    fake.route("POST", OME_HOST, LOGIN, status(401))
    with pytest.raises(SourceError) as failed:
        await read_once(make_source(tmp_path, fake))
    assert failed.value.kind is SourceErrorKind.PERMISSION_DENIED
    assert fake.sent() == [("POST", OME_HOST, LOGIN)]


async def test_unreadable_password_file_is_permission_denied(tmp_path: Path) -> None:
    fake = dell_fleet()
    source = make_source(tmp_path, fake)
    (tmp_path / "ome.pass").write_text("\n")
    with pytest.raises(SourceError) as failed:
        await read_once(source)
    assert failed.value.kind is SourceErrorKind.PERMISSION_DENIED
    assert fake.requests == []


@pytest.mark.parametrize(
    ("target", "reply", "kind"),
    [
        (DEVICES_2, status(500), SourceErrorKind.TEMPORARILY_UNAVAILABLE),
        (DEVICES_1, ok({"no": "value"}), SourceErrorKind.INVALID),
        (PROFILES, status(403), SourceErrorKind.PERMISSION_DENIED),
    ],
)
async def test_a_failing_listing_page_fails_the_read(
    tmp_path: Path, target: str, reply: Reply, kind: SourceErrorKind
) -> None:
    fake = dell_fleet()
    fake.get(OME_HOST, target, reply)
    with pytest.raises(SourceError) as failed:
        await read_once(make_source(tmp_path, fake))
    assert failed.value.kind is kind
    assert fake.sent()[-1] == ("DELETE", OME_HOST, LOGOUT)
    assert not any("InventoryDetails" in path for _, _, path in fake.sent())


async def test_profiles_fall_back_to_the_ome_3_2_path(tmp_path: Path) -> None:
    fake = dell_fleet()
    fake.get(OME_HOST, PROFILES, status(404))
    fake.get(OME_HOST, "/api/TemplateService/Profiles", ok(load("ome/profiles.json")))
    read = await read_once(make_source(tmp_path, fake))
    assert read.coverage["profile_path"] == "/api/TemplateService/Profiles"
    assert by_id(read)["10102"].profile_name == "ocp4-prod-site1-gpu-07"


@pytest.mark.parametrize(
    "link",
    [
        "/api/JobService/Jobs?$skip=3",
        "/api/DeviceService/Devices?$skip=3&$top=3",
        "/api/DeviceService/Devices?$skip=3&$filter=Type%20eq%202000",
        DEVICES_1,
    ],
)
async def test_an_unusable_next_link_fails_the_read(tmp_path: Path, link: str) -> None:
    fake = dell_fleet()
    page = load("ome/devices-page1.json") | {"@odata.nextLink": link}
    fake.get(OME_HOST, DEVICES_1, ok(page))
    with pytest.raises(SourceError) as failed:
        await read_once(make_source(tmp_path, fake))
    assert failed.value.kind is SourceErrorKind.INVALID
    assert fake.unexpected == []


async def test_a_next_link_naming_another_host_pages_on_our_origin(tmp_path: Path) -> None:
    fake = dell_fleet()
    link = "https://ome-internal.example.test" + DEVICES_2
    fake.get(OME_HOST, DEVICES_1, ok(load("ome/devices-page1.json") | {"@odata.nextLink": link}))
    read = await read_once(make_source(tmp_path, fake))
    assert len(read.servers) == 5
    assert {host for _, host, _ in fake.sent()} <= {
        OME_HOST,
        "192.0.2.12",
        "192.0.2.13",
        "192.0.2.14",
    }


async def test_failed_gpu_reads_make_the_read_partial(tmp_path: Path) -> None:
    fake = dell_fleet()
    fake.get(OME_HOST, inventory(10101), status(500))
    fake.get("192.0.2.11", "/redfish/v1/Systems", status(503))
    fake.get(OME_HOST, inventory(10102), status(400))
    fake.get(OME_HOST, inventory(10105), status(500))
    idrac(fake, "192.0.2.15", "processors-gpu.json")
    read = await read_once(make_source(tmp_path, fake))

    servers = by_id(read)
    assert read.complete is False
    assert servers["10101"].gpu_models is None
    assert servers["10101"].evidence["gpu_source"] == "redfish"
    assert servers["10102"].gpu_models == ("NVIDIA L40S",) * 4
    assert servers["10105"].gpu_models == ("NVIDIA L40S",) * 4
    assert servers["10105"].evidence["gpu_source"] == "redfish"
    assert (read.coverage["gpu_unread"], read.coverage["gpu_read_failed"]) == (1, 1)
    assert any(e.startswith("device 10101: OME serverDeviceCards") for e in read.errors)
    assert any(e.startswith("device 10101: Redfish") for e in read.errors)
    assert not any(e.startswith("device 10102") for e in read.errors)
    assert fake.sent().count(("GET", OME_HOST, inventory(10101))) == 3
    assert fake.sent().count(("GET", "192.0.2.11", "/redfish/v1/Systems")) == 2


async def test_a_rejected_idrac_account_stops_redfish_after_three_hosts(tmp_path: Path) -> None:
    fake = dell_fleet()
    for device_id in range(10101, 10106):
        fake.get(OME_HOST, inventory(device_id), ok(load("ome/inventory-no-gpu.json")))
        fake.get(f"192.0.2.{device_id - 10090}", "/redfish/v1/Systems", status(401))
    read = await read_once(make_source(tmp_path, fake, gpu_concurrency=1))
    idrac_requests = [key for key in fake.sent() if key[1] != OME_HOST]
    assert len(idrac_requests) == 3
    assert all(s.gpu_models is None for s in read.servers)
    assert "refused by every iDRAC tried" in by_id(read)["10105"].evidence["gpu_note"]
    assert read.complete is False
    assert read.coverage["gpu_read_failed"] == 5


async def test_a_redfish_link_outside_the_allow_list_is_never_followed(tmp_path: Path) -> None:
    fake = dell_fleet()
    collection = load("redfish/processors-gpu.json")
    collection["Members"].append({"@odata.id": "/redfish/v1/Managers/iDRAC.Embedded.1"})
    fake.get("192.0.2.12", f"{SYS}/Processors", ok(collection))
    read = await read_once(make_source(tmp_path, fake))
    assert by_id(read)["10102"].gpu_models is None
    assert "outside the GPU reads" in by_id(read)["10102"].evidence["gpu_note"]
    assert not any("/Managers" in path for _, _, path in fake.sent())
    assert read.complete is False


async def test_gpu_answers_are_cached_until_the_inventory_changes(tmp_path: Path) -> None:
    fake = dell_fleet()
    async with make_source(tmp_path, fake) as source:
        first = await source.read(NOW)
        seen = len(fake.requests)
        second = await source.read(NOW + timedelta(minutes=15))
        cached = fake.sent()[seen:]
        assert not any("InventoryDetails" in path for _, _, path in cached)
        assert {host for _, host, _ in cached} == {OME_HOST}
        assert second.coverage["gpu_from_cache"] == 5
        assert [s.gpu_models for s in second.servers] == [s.gpu_models for s in first.servers]
        assert by_id(second)["10101"].evidence["gpu_cached"] is True
        assert by_id(second)["10101"].evidence["gpu_read_at"] == NOW.isoformat()

        page = load("ome/devices-page1.json")
        page["value"][0]["LastInventoryTime"] = "2026-10-31 04:00:00.000"
        fake.get(OME_HOST, DEVICES_1, ok(page))
        seen = len(fake.requests)
        third = await source.read(NOW + timedelta(minutes=30))
        reread = [path for _, _, path in fake.sent()[seen:] if "InventoryDetails" in path]
        assert reread == [inventory(10101)]
        assert third.coverage["gpu_from_cache"] == 4

        seen = len(fake.requests)
        await source.read(NOW + timedelta(hours=25))
        reread = [path for _, _, path in fake.sent()[seen:] if "InventoryDetails" in path]
        assert len(reread) == 5


async def test_an_ambiguous_target_name_binds_no_device(tmp_path: Path) -> None:
    fake = dell_fleet()
    page = load("ome/devices-page2.json")
    page["value"][0]["DeviceName"] = "idrac-vdigpu3"
    fake.get(OME_HOST, DEVICES_2, ok(page))
    read = await read_once(make_source(tmp_path, fake))
    servers = by_id(read)
    for device_id in ("10103", "10104"):
        assert servers[device_id].profile_name is None
        assert servers[device_id].evidence["profile_join"] == "ambiguous"
    assert read.coverage["profiles_ambiguous"] == 1
    assert read.complete is False
    assert any("TargetName shared by several devices" in e for e in read.errors)


async def test_several_profiles_on_one_device_prefer_target_id_then_lowest_id(
    tmp_path: Path,
) -> None:
    fake = dell_fleet()
    profiles = load("ome/profiles.json")
    extra = [
        copy.deepcopy(profiles["value"][0]) | {"Id": 506, "ProfileName": "zz-second"},
        copy.deepcopy(profiles["value"][0])
        | {"Id": 400, "ProfileName": "by-name", "TargetId": None},
    ]
    profiles["value"].extend(extra)
    fake.get(OME_HOST, PROFILES, ok(profiles))
    read = await read_once(make_source(tmp_path, fake))
    server = by_id(read)["10102"]
    assert server.profile_name == "ocp4-prod-site1-gpu-07"
    assert server.evidence["other_profile_ids"] == [506, 400]
    assert read.coverage["servers_with_several_profiles"] == 1
    assert read.complete is True


async def test_gpu_reads_that_miss_the_deadline_stay_unread(tmp_path: Path) -> None:
    fake = dell_fleet()
    fake.delays[("GET", OME_HOST, inventory(10101))] = 5.0
    read = await read_once(make_source(tmp_path, fake, timeout_seconds=1.0))
    servers = by_id(read)
    assert servers["10101"].gpu_models is None
    assert "deadline" in servers["10101"].evidence["gpu_note"]
    assert servers["10105"].gpu_models == (H100_94,) * 4
    assert read.complete is False
    assert fake.sent()[-1] == ("DELETE", OME_HOST, LOGOUT)


async def test_a_listing_that_misses_the_deadline_fails_the_read(tmp_path: Path) -> None:
    fake = dell_fleet()
    fake.delays[("GET", OME_HOST, DEVICES_2)] = 5.0
    with pytest.raises(SourceError) as failed:
        await read_once(make_source(tmp_path, fake, timeout_seconds=0.3))
    assert failed.value.kind is SourceErrorKind.TEMPORARILY_UNAVAILABLE
    assert fake.sent()[-1] == ("DELETE", OME_HOST, LOGOUT)


async def test_the_guards_run_before_the_transport(tmp_path: Path) -> None:
    fake = dell_fleet()
    source = make_source(tmp_path, fake)
    async with source:
        with pytest.raises(UnsafeRequestError):
            await source._ome.post("/api/JobService/Jobs", json={})
        with pytest.raises(UnsafeRequestError):
            await source._ome.delete(LOGOUT)
        with pytest.raises(UnsafeRequestError):
            await source._idrac.post(f"https://192.0.2.12{SYS}/Actions/ComputerSystem.Reset")
    assert fake.requests == []


def test_constructor_rejects_bad_settings(tmp_path: Path) -> None:
    conn = OpenManageConnection(
        name="ome", url=OME, username="u", password_file=tmp_path / "p", idrac_username="i"
    )
    with pytest.raises(ValueError, match="idrac_password_file"):
        OpenManageSource(conn, timeout_seconds=10)
    good = conn.model_copy(update={"idrac_username": None})
    with pytest.raises(ValueError, match="inventory types"):
        OpenManageSource(good, timeout_seconds=10, inventory_types=["serverDeviceCards')"])
    with pytest.raises(ValueError, match="positive"):
        OpenManageSource(good, timeout_seconds=0)


SESSION = "7c4f0e1a-5b2d-4c8e-9f31-0a6b2d9e4c11"
TYPES = ("serverDeviceCards",)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", f"{OME}{LOGIN}"),
        ("DELETE", f"{OME}{LOGOUT}"),
        ("GET", f"{OME}{DEVICES_1}"),
        ("GET", f"{OME}{DEVICES_2}"),
        ("GET", f"{OME}/api/DeviceService/Devices?$filter=Type+eq+1000&$top=500"),
        ("GET", f"{OME}{PROFILES}"),
        ("GET", f"{OME}{PROFILES}?$skip=100&$top=100"),
        ("GET", f"{OME}/api/TemplateService/Profiles"),
        ("GET", f"{OME}{inventory(10101)}"),
    ],
)
def test_ome_guard_allows_login_logout_and_the_reads(method: str, url: str) -> None:
    check_request(method, httpx.URL(url), httpx.URL(OME), session_id=SESSION, inventory_types=TYPES)


@pytest.mark.parametrize(
    ("method", "url", "session"),
    [
        ("POST", f"{OME}/api/JobService/Jobs", SESSION),
        ("POST", f"{OME}/api/TemplateService/Actions/TemplateService.Deploy", SESSION),
        ("POST", f"{OME}/api/ProfileService/Actions/ProfileService.AssignProfile", SESSION),
        ("POST", f"{OME}{LOGIN}?x=1", SESSION),
        ("DELETE", f"{OME}/api/SessionService/Sessions('someone-else')", SESSION),
        ("DELETE", f"{OME}{LOGOUT}", None),
        ("DELETE", f"{OME}/api/DeviceService/Devices(10101)", SESSION),
        ("PUT", f"{OME}/api/ProfileService/Profiles(501)", SESSION),
        ("PATCH", f"{OME}/api/ProfileService/Profiles(501)", SESSION),
        ("GET", f"{OME}/api/DeviceService/Devices", SESSION),
        ("GET", f"{OME}/api/DeviceService/Devices?$filter=Type%20eq%202000", SESSION),
        ("GET", f"{OME}{DEVICES_1}&$expand=InventoryDetails", SESSION),
        ("GET", f"{OME}{DEVICES_1}&$top=all", SESSION),
        ("GET", f"{OME}{DEVICES_1}&$top=5&$top=6", SESSION),
        ("GET", f"{OME}{PROFILES}?$filter=ProfileState%20eq%204", SESSION),
        ("GET", f"{OME}{inventory(10101, 'serverProcessors')}", SESSION),
        ("GET", f"{OME}{inventory(10101)}?$top=1", SESSION),
        ("GET", f"{OME}/api/JobService/Jobs", SESSION),
        ("GET", f"{OME}/api/SessionService/Sessions", SESSION),
        ("GET", f"{OME}/api/ProfileService/%50rofiles", SESSION),
        ("GET", f"https://user:pw@{OME_HOST}{PROFILES}", SESSION),
        ("GET", f"https://evil.example.test{PROFILES}", SESSION),
        ("GET", f"http://{OME_HOST}{PROFILES}", SESSION),
        ("GET", f"https://{OME_HOST}:8443{PROFILES}", SESSION),
    ],
)
def test_ome_guard_refuses_everything_else(method: str, url: str, session: str | None) -> None:
    with pytest.raises(UnsafeRequestError):
        check_request(
            method, httpx.URL(url), httpx.URL(OME), session_id=session, inventory_types=TYPES
        )


IDRAC = "https://192.0.2.12"
HOSTS = frozenset({"192.0.2.12"})


@pytest.mark.parametrize(
    "path",
    [
        "/redfish/v1/Systems",
        SYS,
        f"{SYS}/Processors",
        f"{SYS}/Processors/Video.Slot.31-1",
        f"{PCIE}/59-0",
        f"{SYS}/PCIeDevices/59-0",
    ],
)
def test_redfish_guard_allows_the_gpu_reads(path: str) -> None:
    check_redfish_request("GET", httpx.URL(IDRAC + path), HOSTS)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", f"{IDRAC}{SYS}/Actions/ComputerSystem.Reset"),
        ("POST", f"{IDRAC}/redfish/v1/Managers/iDRAC.Embedded.1/Actions/Manager.Reset"),
        ("POST", f"{IDRAC}/redfish/v1/SessionService/Sessions"),
        ("DELETE", f"{IDRAC}/redfish/v1/SessionService/Sessions/1"),
        ("PATCH", f"{IDRAC}{SYS}"),
        ("GET", f"{IDRAC}{SYS}/Actions/ComputerSystem.Reset"),
        ("GET", f"{IDRAC}/redfish/v1/Systems/Actions"),
        ("GET", f"{IDRAC}/redfish/v1/Managers/iDRAC.Embedded.1"),
        ("GET", f"{IDRAC}/redfish/v1/AccountService/Accounts"),
        ("GET", f"{IDRAC}/redfish/v1/Chassis/System.Embedded.1"),
        ("GET", f"{IDRAC}{SYS}/Processors?$expand=*"),
        ("GET", f"{IDRAC}/redfish/v1/Systems/%53ystem.Embedded.1"),
        ("GET", "https://192.0.2.99/redfish/v1/Systems"),
        ("GET", "http://192.0.2.12/redfish/v1/Systems"),
        ("GET", "https://192.0.2.12:8443/redfish/v1/Systems"),
        ("GET", "https://root:calvin@192.0.2.12/redfish/v1/Systems"),
    ],
)
def test_redfish_guard_refuses_everything_else(method: str, url: str) -> None:
    with pytest.raises(UnsafeRequestError):
        check_redfish_request(method, httpx.URL(url), HOSTS)


@pytest.mark.parametrize(
    ("device", "is_gpu"),
    [
        ({"Manufacturer": "10DE20B2", "Description": "10DE VGA"}, True),
        ({"Manufacturer": "NVIDIA Corporation", "Description": H100_80}, True),
        (
            {
                "Manufacturer": "Advanced Micro Devices, Inc. [AMD/ATI]",
                "Description": "Aldebaran/MI200 [Instinct MI210] Display controller",
            },
            True,
        ),
        (
            {"Manufacturer": "NVIDIA Corporation", "Description": "GH100 [H100 NVSwitch] Bridge"},
            False,
        ),
        ({"Manufacturer": "NVIDIA Corporation", "Description": "GA100 [A100 PCIe 80GB]"}, False),
        (
            {
                "Manufacturer": "Matrox Electronics Systems Ltd.",
                "Description": "Integrated Matrox G200eW3 Graphics Controller",
            },
            False,
        ),
        ({"Manufacturer": "Intel Corporation", "Description": "Ethernet Controller E810-C"}, False),
        ({"Manufacturer": "15B3", "Description": "ConnectX-7 VGA"}, False),
        (
            {"Manufacturer": "10DE20B5", "Description": "10DE VGA", "Status": {"State": "Absent"}},
            False,
        ),
    ],
)
def test_gpu_card_rule(device: dict[str, Any], is_gpu: bool) -> None:
    assert is_gpu_pcie_device(device) is is_gpu


def test_pcie_gpu_model_names_known_pci_ids() -> None:
    assert pcie_gpu_model({"Manufacturer": "10DE2330", "Description": "10DE VGA"}) == (
        "H100-SXM5-80GB"
    )
    assert pcie_gpu_model({"Manufacturer": "10DE26B9", "Description": "10DE 3D"}) == "10DE 3D"
    assert pcie_gpu_model({"Manufacturer": "NVIDIA", "Description": None}) == ""


def test_inventory_without_inventory_info_is_no_answer() -> None:
    assert gpu_models_from_inventory({"InventoryType": "serverDeviceCards"}) is None
    assert gpu_models_from_inventory([]) is None
    assert gpu_models_from_inventory({"InventoryInfo": []}) == ()


@pytest.mark.parametrize(
    ("address", "origin"),
    [
        ("192.0.2.12", "https://192.0.2.12"),
        ("idrac-ocpgpu7.example.test", "https://idrac-ocpgpu7.example.test"),
        ("2001:db8::12", "https://[2001:db8::12]"),
        ("idrac/../admin", None),
        ("https://192.0.2.12", None),
        ("192.0.2.12:8443", None),
    ],
)
def test_idrac_origin(address: str, origin: str | None) -> None:
    found = idrac_origin(address)
    assert (str(found).rstrip("/") if found else None) == origin
