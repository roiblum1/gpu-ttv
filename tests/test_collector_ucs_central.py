"""
UCS Central reader of the profile-less collector (fixture-tested; ADR-0004 "Cisco (UCS Central)").

The real ucscsdk/ucsmsdk handles run end to end (login, XML encoding and parsing, logout); only the
SDK driver's `post` is replaced by `FakeXmlApi`, which answers from synthetic XML fixtures.
"""

from __future__ import annotations

import ssl
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from ucscsdk.mometa.ls.LsServer import LsServer
from ucscsdk.ucschandle import UcscHandle

from ttv.collector import CollectorSource
from ttv.collector.ucs_central import (
    READ_METHODS,
    HandleFactory,
    ReadOnlyHandle,
    UcsCentralSource,
    bound_profile,
    central_handle,
    guard_wire,
    sdk_driver,
    ucsm_handle,
)
from ttv.config import UcsCentralConnection
from ttv.models import CollectorRead, ManagerServer
from ttv.sources.http import SourceError, SourceErrorKind, UnsafeRequestError

FIXTURES = Path(__file__).parent / "fixtures" / "ucs_central"
T0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
CENTRAL_HOST = "ucsc.example.invalid"
DOM_B_ADDRESS = "192.0.2.22"
CENTRAL_PASSWORD = "synthetic-central-pw"
H100 = "UCSC-GPU-H100-80"

LOGIN_OK = (
    '<aaaLogin cookie="" response="yes" outCookie="c0" outRefreshPeriod="600" outPriv="read-only"'
    ' outDomains="" outChannel="noencssl" outEvtChannel="noencssl" outSessionId=""'
    ' outVersion="{version}" outName="synthetic"/>'
)
LOGIN_REJECTED = (
    '<aaaLogin cookie="" response="yes" errorCode="551" invocationResult="unidentified-fail"'
    ' errorDescr="Authentication failed"/>'
)
LOGOUT_OK = '<aaaLogout cookie="" response="yes" outStatus="success"/>'
TOP_SYSTEM = (
    '<configResolveDn dn="{dn}" cookie="c0" response="yes"><outConfig>'
    '<topSystem dn="sys" name="synthetic" address="192.0.2.1"/></outConfig></configResolveDn>'
)


class FakeXmlApi:
    """Stands in for an SDK driver: answers XML API requests from one synthetic fixture file."""

    redirect_uri = None

    def __init__(
        self,
        fixture: str | None = None,
        *,
        version: str = "2.0(1v)",
        reject_login: bool = False,
        unreachable: bool = False,
        hang_on: str | None = None,
    ) -> None:
        path = FIXTURES / fixture if fixture else None
        self.inventory = ET.parse(path).getroot() if path else ET.Element("x")  # noqa: S314 — own fixture
        self.version = version
        self.reject_login = reject_login
        self.unreachable = unreachable
        self.hang_on = hang_on
        self.release = threading.Event()
        self.methods: list[str] = []
        self.passwords: list[str] = []

    def post(
        self,
        uri: str,
        data: bytes | None = None,
        dump_xml: bool = False,
        read: bool = True,
        timeout: float | None = None,
    ) -> str:
        assert data is not None
        request = ET.fromstring(data)  # noqa: S314 — the SDK's own request
        self.methods.append(request.tag)
        if self.unreachable:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        if request.tag == "aaaLogin":
            self.passwords.append(request.get("inPassword", ""))
            return LOGIN_REJECTED if self.reject_login else LOGIN_OK.format(version=self.version)
        if request.tag == "aaaLogout":
            return LOGOUT_OK
        if request.tag == "configResolveDn":
            return TOP_SYSTEM.format(dn=request.get("dn"))
        assert request.tag == "configResolveClass", request.tag
        class_id = request.get("classId", "")
        if class_id == self.hang_on:
            self.release.wait()
        rows = "".join(
            ET.tostring(mo, encoding="unicode") for mo in self.inventory if mo.tag == class_id
        )
        return (
            f'<configResolveClass cookie="c0" response="yes" classId="{class_id}">'
            f"<outConfigs>{rows}</outConfigs></configResolveClass>"
        )


def central_factory(api: FakeXmlApi) -> HandleFactory:
    """A real UcscHandle (TLS-hardened as in production) whose driver is `api`."""

    def build(host: str, username: str, password: str) -> Any:
        handle = central_handle(host, username, password, ca_bundle=None)
        vars(handle)["_UcscSession__driver"] = api
        return handle

    return build


def ucsm_factory(apis: dict[str, FakeXmlApi]) -> HandleFactory:
    """Real UcsHandles (TLS-hardened as in production) whose drivers are `apis[host]`."""

    def build(host: str, username: str, password: str) -> Any:
        handle = ucsm_handle(host, username, password, ca_bundle=None, timeout_seconds=5)
        vars(handle)["_UcsSession__driver"] = apis[host]
        return handle

    return build


@pytest.fixture
def conn(tmp_path: Path) -> UcsCentralConnection:
    (tmp_path / "ucsc.pw").write_text(CENTRAL_PASSWORD + "\n")
    (tmp_path / "ucsm.pw").write_text("synthetic-ucsm-pw\n")
    return UcsCentralConnection(
        name="dc1",
        host=CENTRAL_HOST,
        username="svc-ttv-ro",
        password_file=tmp_path / "ucsc.pw",
        ucsm_username="svc-ttv-ro",
        ucsm_password_file=tmp_path / "ucsm.pw",
    )


def ucsm_apis(**kwargs: Any) -> dict[str, FakeXmlApi]:
    return {DOM_B_ADDRESS: FakeXmlApi("ucsm-dc1-dom-b.xml", version="4.2(3d)", **kwargs)}


async def read(
    conn: UcsCentralConnection,
    central: FakeXmlApi | None = None,
    ucsm: dict[str, FakeXmlApi] | None = None,
    *,
    deadline: float = 5.0,
) -> tuple[CollectorRead, dict[str, ManagerServer]]:
    source = UcsCentralSource(
        conn,
        timeout_seconds=deadline,
        central_factory=central_factory(central or FakeXmlApi("central.xml")),
        ucsm_factory=ucsm_factory(ucsm if ucsm is not None else ucsm_apis()),
    )
    result = await source.read(T0)
    await source.aclose()
    return result, {s.source_id: s for s in result.servers}


def test_source_satisfies_the_collector_protocol(conn: UcsCentralConnection) -> None:
    source: CollectorSource = UcsCentralSource(conn, timeout_seconds=5)
    assert source.name == "ucs-central:dc1"


async def test_blade_without_profile_reports_its_eight_gpus(conn: UcsCentralConnection) -> None:
    result, by_dn = await read(conn)
    blade = by_dn["compute/sys-1001/chassis-1/blade-1"]
    assert result.manager == "ucs-central:dc1"
    assert result.kind == "ucs_central"
    assert result.started_at == T0
    assert blade.vendor == "cisco"
    assert blade.manager == "ucs-central:dc1"
    assert blade.serial_normalized == "fch2650a0b1"
    assert blade.name == "compute/sys-1001/chassis-1/blade-1"
    assert blade.model == "UCSB-B200-M6"
    assert blade.profile_name is None
    assert blade.gpu_models == (H100,) * 8
    assert blade.domain == "dc1-dom-a"
    assert blade.evidence["gpu_source"] == "central"
    assert blade.evidence["domain_last_refreshed_ts"] == "2026-10-31T11:52:07.000"
    assert blade.evidence["association"] == "none"
    assert "profile_dn" not in blade.evidence


async def test_ocp_profile_rack_unit_counts_only_equipped_gpus(
    conn: UcsCentralConnection,
) -> None:
    _, by_dn = await read(conn)
    server = by_dn["compute/sys-1001/rack-unit-1"]
    assert server.profile_name == "ocp4-prod-gpu-site1-w01"
    assert server.gpu_models == ("UCSC-GPU-L40S",) * 4
    assert server.evidence["profile_dn"] == "org-root/org-ocp/ls-ocp4-prod-gpu-site1-w01"
    assert server.evidence["profile_join"] == "assignedToDn"
    assert server.evidence["profile_assoc_state"] == "associated"
    assert "profile_other_dns" not in server.evidence


async def test_non_ocp_profile_bound_in_a_failed_state_is_still_bound(
    conn: UcsCentralConnection,
) -> None:
    _, by_dn = await read(conn)
    server = by_dn["compute/sys-1001/rack-unit-2"]
    assert server.profile_name == "vdi-host-07"
    assert server.gpu_models == ("UCSC-GPU-A16",) * 2
    # Bound only through the profile's domain-local pnDn (`sys/rack-unit-2`), re-rooted.
    assert server.evidence["profile_join"] == "pnDn"
    assert server.evidence["profile_assoc_state"] == "failed"
    assert server.evidence["profile_config_state"] == "failed-to-apply"


async def test_unequipped_slot_is_dropped(conn: UcsCentralConnection) -> None:
    result, by_dn = await read(conn)
    assert "compute/sys-1001/chassis-1/blade-2" not in by_dn
    assert result.coverage["servers_unequipped_dropped"] == 1


async def test_placeholder_serial_is_kept_with_an_empty_serial(
    conn: UcsCentralConnection,
) -> None:
    _, by_dn = await read(conn)
    server = by_dn["compute/sys-1001/rack-unit-3"]
    assert server.serial_normalized == ""
    assert server.profile_name is None
    assert server.gpu_models == ()  # dc1-dom-a's replica is populated: zero is read, not unknown


async def test_bound_profile_missing_from_central_is_named_from_its_dn(
    conn: UcsCentralConnection,
) -> None:
    _, by_dn = await read(conn)
    server = by_dn["compute/sys-1001/rack-unit-4"]
    assert server.profile_name == "ocp-legacy-07"
    assert server.evidence["profile_name_source"] == "dn"
    assert "profile_assoc_state" not in server.evidence


async def test_domain_without_graphicscard_replica_reads_gpus_from_ucs_manager(
    conn: UcsCentralConnection,
) -> None:
    result, by_dn = await read(conn)
    ocp = by_dn["compute/sys-1002/rack-unit-1"]
    assert ocp.profile_name == "ocp4-prod-gpu-site1-w02"
    assert ocp.gpu_models == (H100,) * 8
    assert ocp.evidence["gpu_source"] == "ucsm"
    assert "gpu_unread_reason" not in ocp.evidence
    no_gpu = by_dn["compute/sys-1002/rack-unit-2"]
    assert no_gpu.gpu_models == ()
    assert no_gpu.evidence["gpu_source"] == "ucsm"
    # UCS Manager holds another serial at this DN: its GPUs belong to that serial, not this one.
    swapped = by_dn["compute/sys-1002/chassis-2/blade-1"]
    assert swapped.gpu_models is None
    assert swapped.evidence["gpu_unread_reason"] == (
        "UCS Manager reports a different serial at this DN"
    )
    assert result.complete
    assert result.errors == ()


async def test_without_ucsm_fallback_gpus_stay_unread_never_zero(
    conn: UcsCentralConnection,
) -> None:
    apis = ucsm_apis()
    result, by_dn = await read(conn.model_copy(update={"ucsm_username": None}), ucsm=apis)
    dom_b = [s for dn, s in by_dn.items() if dn.startswith("compute/sys-1002/")]
    assert len(dom_b) == 3
    assert all(s.gpu_models is None for s in dom_b)
    assert all(
        s.evidence["gpu_unread_reason"] == "UCS Central lists no graphicsCard in this domain"
        for s in dom_b
    )
    assert apis[DOM_B_ADDRESS].methods == []
    assert result.complete  # the listing is whole; only GPU evidence is missing
    assert result.coverage["domains_gpu_unread"] == 1
    assert result.coverage["servers_gpu_unread"] == 3


async def test_domain_timeout_is_a_partial_read(conn: UcsCentralConnection) -> None:
    apis = ucsm_apis(hang_on="graphicsCard")
    try:
        result, by_dn = await read(conn, ucsm=apis, deadline=0.3)
    finally:
        apis[DOM_B_ADDRESS].release.set()
    assert not result.complete
    assert len(result.errors) == 1
    assert result.errors[0].startswith("domain dc1-dom-b (id 1002): temporarily_unavailable:")
    assert "timed out after 0.3s" in result.errors[0]
    for dn in ("compute/sys-1002/rack-unit-1", "compute/sys-1002/rack-unit-2"):
        assert by_dn[dn].gpu_models is None
        assert "timed out" in by_dn[dn].evidence["gpu_unread_reason"]
    assert by_dn["compute/sys-1002/rack-unit-1"].profile_name == "ocp4-prod-gpu-site1-w02"
    assert by_dn["compute/sys-1001/chassis-1/blade-1"].gpu_models == (H100,) * 8
    assert result.coverage["domains_failed"] == 1
    # The wedged call may still hold the handle, so logout is refused, never sent concurrently.
    assert "aaaLogout" not in apis[DOM_B_ADDRESS].methods


async def test_central_login_rejected_raises_permission_denied(
    conn: UcsCentralConnection,
) -> None:
    with structlog.testing.capture_logs() as logs, pytest.raises(SourceError) as caught:
        await read(conn, central=FakeXmlApi("central.xml", reject_login=True))
    assert caught.value.kind is SourceErrorKind.PERMISSION_DENIED
    assert "Authentication failed" in str(caught.value)
    assert CENTRAL_PASSWORD not in str(caught.value)
    assert CENTRAL_PASSWORD not in repr(logs)


async def test_central_unreachable_raises_temporarily_unavailable(
    conn: UcsCentralConnection,
) -> None:
    with pytest.raises(SourceError) as caught:
        await read(conn, central=FakeXmlApi("central.xml", unreachable=True))
    assert caught.value.kind is SourceErrorKind.TEMPORARILY_UNAVAILABLE


async def test_central_with_no_registered_domain_is_not_an_empty_estate(
    conn: UcsCentralConnection,
) -> None:
    with pytest.raises(SourceError) as caught:
        await read(conn, central=FakeXmlApi())
    assert caught.value.kind is SourceErrorKind.INVALID


async def test_read_sends_only_read_methods_and_logs_out(conn: UcsCentralConnection) -> None:
    central = FakeXmlApi("central.xml")
    apis = ucsm_apis()
    await read(conn, central=central, ucsm=apis)
    for api in (central, apis[DOM_B_ADDRESS]):
        assert set(api.methods) <= READ_METHODS
        assert api.methods[0] == "aaaLogin"
        assert api.methods[-1] == "aaaLogout"


async def test_password_is_reread_on_every_read(conn: UcsCentralConnection) -> None:
    central = FakeXmlApi("central.xml")
    await read(conn, central=central)
    conn.password_file.write_text("rotated-synthetic-pw\n")
    await read(conn, central=central)
    assert central.passwords == [CENTRAL_PASSWORD, "rotated-synthetic-pw"]


async def test_coverage_counts_the_estate(conn: UcsCentralConnection) -> None:
    result, _ = await read(conn)
    assert result.coverage == {
        "domains": 2,
        "domains_gpu_central": 1,
        "domains_gpu_ucsm": 1,
        "domains_gpu_unread": 0,
        "domains_failed": 0,
        "servers": 8,
        "servers_unequipped_dropped": 1,
        "servers_without_serial": 1,
        "servers_with_profile": 4,
        "servers_profile_name_from_dn": 1,
        "servers_with_gpus": 5,
        "servers_gpu_unread": 1,
    }


@pytest.mark.parametrize(
    "method",
    ["add_mo", "set_mo", "remove_mo", "commit", "process_xml_elem", "post_elem", "file_upload"],
)
def test_read_only_handle_refuses_config_methods(method: str) -> None:
    raw = UcscHandle(CENTRAL_HOST, "u", "p")
    with pytest.raises(UnsafeRequestError):
        getattr(ReadOnlyHandle(raw), method)


@pytest.mark.parametrize("sdk", ["central", "ucsm"])
def test_guarded_sdk_handle_refuses_to_send_a_write(sdk: str) -> None:
    api = FakeXmlApi("central.xml")
    if sdk == "central":
        handle = central_handle(CENTRAL_HOST, "u", "p", ca_bundle=None)
        vars(handle)["_UcscSession__driver"] = api
    else:
        handle = ucsm_handle(DOM_B_ADDRESS, "u", "p", ca_bundle=None, timeout_seconds=5)
        vars(handle)["_UcsSession__driver"] = api
    guard_wire(handle)
    with pytest.raises(UnsafeRequestError):
        handle.post_elem(ET.Element("configConfMos"))
    if sdk == "central":
        handle.add_mo(LsServer(parent_mo_or_dn="org-root", name="synthetic"))
        with pytest.raises(UnsafeRequestError):
            handle.commit()
    assert api.methods == []


@pytest.mark.parametrize("sdk", ["central", "ucsm"])
def test_sdk_transport_verifies_tls_and_never_downgrades(sdk: str) -> None:
    ca = FIXTURES / "synthetic-ca.pem"
    handle = (
        central_handle(CENTRAL_HOST, "u", "p", ca_bundle=ca)
        if sdk == "central"
        else ucsm_handle(DOM_B_ADDRESS, "u", "p", ca_bundle=ca, timeout_seconds=5)
    )
    driver = sdk_driver(handle)

    def handlers() -> list[Any]:
        return next(v for k, v in vars(driver).items() if k.endswith("Driver__handlers"))

    https = [h for h in handlers() if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1
    context: ssl.SSLContext = https[0]._context
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname
    assert [c["subject"] for c in context.get_ca_certs()] == [
        ((("commonName", "ttv synthetic test CA"),),)
    ]
    before = handlers()
    driver.update_handlers(tls_proto="tlsv1")  # what the SDK calls after any SSL error
    assert handlers() is before
    redirects = [h for h in before if isinstance(h, urllib.request.HTTPRedirectHandler)]
    with pytest.raises(urllib.error.URLError, match="refused an HTTP 302 redirect"):
        redirects[0].redirect_request(None, None, 302, "Found", None, "http://192.0.2.9/")


def test_conflicting_joins_keep_assigned_to_dn_and_record_the_other() -> None:
    server = SimpleNamespace(
        dn="compute/sys-1/rack-unit-1",
        assigned_to_dn="org-root/ls-ocp-a",
        ls_dn="org-root/ls-ocp-b",
    )
    profiles = {
        "org-root/ls-ocp-a": SimpleNamespace(dn="org-root/ls-ocp-a", name="ocp-a"),
        "org-root/ls-ocp-b": SimpleNamespace(dn="org-root/ls-ocp-b", name="ocp-b"),
    }
    binding = bound_profile(server, profiles, {})
    assert binding is not None
    assert (binding.join, binding.dn, binding.others) == (
        "assignedToDn",
        "org-root/ls-ocp-a",
        ("org-root/ls-ocp-b",),
    )
