"""
UCS Central reader of the profile-less GPU collector, read-only (ADR-0004, "Cisco (UCS Central)").

One UCS Central session per read lists every equipped server with its bound profile and GPUs; a
domain whose `graphicsCard` replica is empty is read through UCS Manager when that is configured.
"""

from __future__ import annotations

import asyncio
import functools
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any, NoReturn
from urllib.parse import urlparse

import structlog
from ucscsdk.ucscexception import UcscConnectionError, UcscException, UcscLoginError
from ucscsdk.ucschandle import UcscHandle
from ucsmsdk.ucsexception import UcsConnectionError, UcsException, UcsLoginError
from ucsmsdk.ucshandle import UcsHandle

from ttv.collector.blocking import run_abandonable
from ttv.config import UcsCentralConnection, read_secret
from ttv.identity import normalize_serial
from ttv.models import CollectorRead, ManagerServer
from ttv.sources.http import SourceError, SourceErrorKind, UnsafeRequestError, kind_for_status

log = structlog.get_logger(__name__)

VENDOR = "cisco"
KIND = "ucs_central"
CENTRAL_CLASSES = ("computeSystem", "computeBlade", "computeRackUnit", "lsServer", "graphicsCard")
UCSM_CLASSES = ("computeBlade", "computeRackUnit", "graphicsCard")
# Every XML API method a read sends: login, logout, class and DN queries. All else is refused.
READ_METHODS = frozenset({"aaaLogin", "aaaLogout", "configResolveClass", "configResolveDn"})
_LS_RN = re.compile(r"(?:^|/)ls-([^/]+)$")

# Builds an SDK handle, not logged in, from (host, username, password).
HandleFactory = Callable[[str, str, str], Any]

# Copied from server-scan `backend/app/infrastructure/providers/ucs_common.py@defe172`.
_EQUIPPED_PREFIX = "equipped"
_NON_PRIMARY_PRESENCE = frozenset({"equipped-slave", "equipped-not-primary"})
TEMPLATE_TYPES = frozenset({"initial-template", "updating-template"})

# Copied from server-scan `backend/app/infrastructure/providers/ucs_central/provider.py@defe172`.
_UCSM_ROOT = "sys/"
_CENTRAL_ROOT = "compute/sys-{domain_id}/"


# Copied from server-scan `backend/app/infrastructure/providers/ucs_common.py@defe172`.
def is_equipped(server_mo: Any) -> bool:
    """
    Report whether a compute MO is a physically-present, independently addressable server.

    Args:
        server_mo (Any): A `computeBlade`, `computeRackUnit` or `graphicsCard` managed object
            from either SDK.

    Returns:
        bool: True if it is equipped and is not the secondary half of a multi-node server.
    """
    raw = getattr(server_mo, "presence", None)
    if not raw:
        return False
    presence = str(raw)
    if presence in _NON_PRIMARY_PRESENCE:
        return False
    return presence.startswith(_EQUIPPED_PREFIX)


# Copied from server-scan `backend/app/infrastructure/providers/ucs_common.py@defe172`.
def group_by_owning_server_dn(
    mos: Iterable[Any], *, server_dns: Iterable[str]
) -> dict[str, list[Any]]:
    """
    Bucket descendant managed objects under the compute unit each lives below.

    Args:
        mos (Iterable[Any]): Descendant managed objects, e.g. one whole class query.
        server_dns (Iterable[str]): The compute-unit DNs to group under (a list: read twice).

    Returns:
        dict[str, list[Any]]: One entry per DN in `server_dns`, each holding the MOs beneath it.
    """
    known = set(server_dns)
    grouped: dict[str, list[Any]] = {dn: [] for dn in server_dns}
    for mo in mos:
        dn = getattr(mo, "dn", None)
        if not dn:
            continue
        ancestor, _, _ = str(dn).rpartition("/")
        while ancestor:
            if ancestor in known:
                grouped[ancestor].append(mo)
                break
            ancestor, _, _ = ancestor.rpartition("/")
    return grouped


# Copied from server-scan `backend/app/infrastructure/providers/ucs_central/provider.py@defe172`.
def domain_id_from_dn(dn: str) -> str | None:
    """
    Extract the registered domain from a UCS Central distinguished name.

    Args:
        dn (str): A UCS Central DN, e.g. `compute/sys-1009/chassis-1/blade-1`.

    Returns:
        str | None: The domain id, or None for anything not under a `computeSystem`.
    """
    parts = str(dn).split("/")
    if len(parts) < 2 or not parts[1].startswith("sys-"):
        return None
    return parts[1][len("sys-") :] or None


# Copied from server-scan `backend/app/infrastructure/providers/ucs_central/provider.py@defe172`.
def central_external_id(external_id: str, *, domain_id: str) -> str:
    """
    Re-root a UCS Manager DN into the UCS Central DN for the same object.

    Args:
        external_id (str): The DN as UCS Manager reported it (`sys/...`).
        domain_id (str): The owning domain's Central id.

    Returns:
        str: The domain-qualified DN, or `external_id` unchanged if it is not rooted at `sys/`.
    """
    if not external_id.startswith(_UCSM_ROOT):
        return external_id
    return _CENTRAL_ROOT.format(domain_id=domain_id) + external_id[len(_UCSM_ROOT) :]


# Adapted from server-scan `backend/app/infrastructure/providers/ucs_manager/client.py@defe172`.
def validate_endpoint(endpoint: str) -> str:
    """
    Check that an endpoint is the bare host or IP both SDKs require (they build the URL raw).

    Args:
        endpoint (str): The configured or Central-reported address.

    Returns:
        str: The endpoint, stripped of surrounding whitespace.

    Raises:
        ValueError: If the endpoint is empty, carries a URL scheme, or includes a port.
    """
    candidate = endpoint.strip()
    if not candidate:
        raise ValueError("UCS endpoint is empty.")
    if "://" in candidate:
        host = urlparse(candidate).hostname or ""
        raise ValueError(
            f"UCS endpoint {endpoint!r} must be a bare host, not a URL (use {host!r})."
        )
    if ":" in candidate and not candidate.startswith("["):  # not a bare IPv6 literal
        raise ValueError(f"UCS endpoint {endpoint!r} must not include a port.")
    return candidate


def gpu_models(cards: Iterable[Any]) -> tuple[str, ...]:
    """
    One model per equipped `graphicsCard`, as server-scan's `ucs_manager/mapping.py` `_gpus`.

    Args:
        cards (Iterable[Any]): `graphicsCard` MOs under one server.

    Returns:
        tuple[str, ...]: `model` verbatim per equipped card ("" when unset); empty slots skipped.
    """
    return tuple(str(getattr(mo, "model", None) or "") for mo in cards if is_equipped(mo))


def _attr(mo: Any, name: str) -> str:
    """
    Read an MO property as text.

    Args:
        mo (Any): Managed object.
        name (str): Python property name, e.g. `assigned_to_dn`.

    Returns:
        str: The value stripped, or "" when unset.
    """
    return str(getattr(mo, name, None) or "").strip()


# ---- read-only and TLS enforcement ----------------------------------------------------------


def guard_wire(handle: Any) -> None:
    """
    Make an SDK handle refuse to send any XML API method outside `READ_METHODS`.

    Both SDKs send every request, writes included (`configConfMos`), through `post_elem`.

    Args:
        handle (Any): A `UcscHandle` or `UcsHandle`.
    """
    send = handle.post_elem

    def post_elem(elem: Any, *args: Any, **kwargs: Any) -> Any:
        """
        Send `elem` only if it is a read method.

        Args:
            elem (Any): The request element.
            *args (Any): Forwarded to the SDK.
            **kwargs (Any): Forwarded to the SDK.

        Returns:
            Any: The SDK's parsed response.

        Raises:
            UnsafeRequestError: The method is not a read.
        """
        if elem.tag not in READ_METHODS:
            raise UnsafeRequestError(
                f"refused XML API method {elem.tag!r}: UCS reads are read-only"
            )
        return send(elem, *args, **kwargs)

    handle.post_elem = post_elem


class ReadOnlyHandle:
    """
    An SDK handle reduced to login, logout and queries; every other attribute is refused.

    `add_mo`, `set_mo`, `remove_mo`, `commit`, `process_xml_elem`, ... raise `UnsafeRequestError`,
    and the wrapped handle itself refuses non-read XML methods (`guard_wire`).
    """

    __slots__ = ("_handle",)

    def __init__(self, handle: Any) -> None:
        """
        Wrap a handle and guard its wire.

        Args:
            handle (Any): A `UcscHandle` or `UcsHandle`, not logged in.
        """
        guard_wire(handle)
        self._handle = handle

    def login(self) -> None:
        """Open the session (the SDK raises on bad credentials)."""
        self._handle.login()

    def logout(self) -> None:
        """Close the session; the SDKs make this a no-op before a successful login."""
        self._handle.logout()

    def query_classid(self, class_id: str) -> list[Any]:
        """
        Every instance of one class.

        Args:
            class_id (str): XML class id, e.g. `computeBlade`.

        Returns:
            list[Any]: The managed objects.
        """
        result = self._handle.query_classid(class_id)
        return list(result) if result else []

    def query_dn(self, dn: str) -> Any:
        """
        One object by DN.

        Args:
            dn (str): Distinguished name.

        Returns:
            Any: The managed object, or None.
        """
        return self._handle.query_dn(dn)

    def __getattr__(self, name: str) -> NoReturn:
        """
        Refuse everything not defined above.

        Args:
            name (str): Attribute name.

        Raises:
            AttributeError: For dunder lookups, so Python protocols behave.
            UnsafeRequestError: For anything else.
        """
        if name.startswith("__"):
            raise AttributeError(name)
        raise UnsafeRequestError(f"refused {name!r}: the UCS handle is read-only")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the XML API answers in place, and a redirect could leave TLS."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> NoReturn:
        """
        Refuse the redirect.

        Args:
            req (urllib.request.Request): The request.
            fp (IO[bytes]): The response body.
            code (int): HTTP status.
            msg (str): HTTP reason.
            headers (HTTPMessage): Response headers.
            newurl (str): Redirect target.

        Raises:
            urllib.error.URLError: Always.
        """
        raise urllib.error.URLError(f"refused an HTTP {code} redirect")


def _keep_handlers(tls_proto: str | None = None) -> None:
    """
    Replace the SDK driver's `update_handlers`, which swaps in unverified TLSv1 on any SSL error.

    Args:
        tls_proto (str | None): Ignored.
    """


def sdk_driver(handle: Any) -> Any:
    """
    The urllib driver inside an SDK handle (`UcscSession.__driver` / `UcsSession.__driver`).

    Args:
        handle (Any): A `UcscHandle` or `UcsHandle`.

    Returns:
        Any: The driver.

    Raises:
        RuntimeError: The SDK changed shape, so TLS verification cannot be enforced.
    """
    for key, value in vars(handle).items():
        if key.endswith("Session__driver"):
            return value
    raise RuntimeError("UCS SDK layout changed: cannot enforce TLS verification")


def verify_tls(driver: Any, ca_bundle: Path | None) -> None:
    """
    Make an SDK driver verify certificates and host names, and refuse redirects.

    Both SDKs wrap sockets in an SSL context that verifies nothing and retry failed TLS as TLSv1
    (ADR-0004); server-scan uses them as shipped. This replaces their handlers and the retry.

    Args:
        driver (Any): A `UcscDriver` or `UcsDriver`.
        ca_bundle (Path | None): PEM bundle to trust; None trusts the system store.

    Raises:
        RuntimeError: The SDK changed shape, so TLS verification cannot be enforced.
    """
    state = vars(driver)
    key = next((k for k in state if k.endswith("Driver__handlers")), None)
    if key is None or not callable(getattr(driver, "update_handlers", None)):
        raise RuntimeError("UCS SDK layout changed: cannot enforce TLS verification")
    context = ssl.create_default_context(cafile=str(ca_bundle) if ca_bundle else None)
    state[key] = [_NoRedirects(), urllib.request.HTTPSHandler(context=context)]
    driver.update_handlers = _keep_handlers


def central_handle(host: str, username: str, password: str, *, ca_bundle: Path | None) -> Any:
    """
    Build a UCS Central `UcscHandle` (port 443) whose transport verifies TLS.

    Args:
        host (str): Bare host name.
        username (str): Account.
        password (str): Its password.
        ca_bundle (Path | None): PEM bundle to trust.

    Returns:
        Any: The handle, not logged in.
    """
    handle = UcscHandle(validate_endpoint(host), username, password)
    verify_tls(sdk_driver(handle), ca_bundle)
    return handle


def ucsm_handle(
    host: str, username: str, password: str, *, ca_bundle: Path | None, timeout_seconds: float
) -> Any:
    """
    Build a UCS Manager `UcsHandle` (HTTPS, port 443) whose transport verifies TLS.

    Args:
        host (str): Bare host or IP of the domain, as UCS Central reports it.
        username (str): Account valid on every domain.
        password (str): Its password.
        ca_bundle (Path | None): PEM bundle to trust.
        timeout_seconds (float): Per-socket-operation timeout (the call deadline is separate).

    Returns:
        Any: The handle, not logged in.
    """
    handle = UcsHandle(
        validate_endpoint(host), username, password, port=443, secure=True, timeout=timeout_seconds
    )
    verify_tls(sdk_driver(handle), ca_bundle)
    return handle


# ---- sessions --------------------------------------------------------------------------------


class _CallError(Exception):
    """An SDK call failed; `kind` classifies it and the message carries no secrets."""

    def __init__(self, kind: SourceErrorKind, message: str) -> None:
        """
        Build the error.

        Args:
            kind (SourceErrorKind): Failure kind.
            message (str): Summary without secrets.
        """
        super().__init__(message)
        self.kind = kind


def _os_failure(what: str, exc: OSError) -> _CallError:
    """
    Classify a network-level failure (urllib re-raises socket and TLS errors untouched).

    Args:
        what (str): The call.
        exc (OSError): The failure.

    Returns:
        _CallError: The HTTP status's kind; INVALID for TLS; else TEMPORARILY_UNAVAILABLE.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return _CallError(kind_for_status(exc.code), f"{what}: HTTP {exc.code} {exc.reason}")
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLError):
        return _CallError(SourceErrorKind.INVALID, f"{what}: TLS failed: {reason}")
    return _CallError(SourceErrorKind.TEMPORARILY_UNAVAILABLE, f"{what}: {reason}")


class _Session:
    """
    One read-only SDK session with a whole-call deadline, as server-scan's UCS clients run them.

    A call that misses its deadline poisons the session: its thread may still use the handle, so
    later calls, logout included, are refused (server-scan `docs/cisco-collectors.md@defe172`).
    """

    def __init__(self, handle: Any, *, label: str, timeout_seconds: float) -> None:
        """
        Wrap a handle read-only.

        Args:
            handle (Any): A `UcscHandle` or `UcsHandle`, not logged in.
            label (str): Session name for thread names and logs.
            timeout_seconds (float): Deadline for each SDK call.
        """
        self._handle = ReadOnlyHandle(handle)
        self._label = label
        self._timeout = timeout_seconds
        self._poisoned_by: str | None = None

    async def _call(
        self,
        what: str,
        func: Callable[..., Any],
        *args: Any,
        api_error: SourceErrorKind = SourceErrorKind.INVALID,
    ) -> Any:
        """
        Run one blocking SDK call on an abandonable thread under the deadline.

        Args:
            what (str): The call, for errors.
            func (Callable[..., Any]): The bound SDK method.
            *args (Any): Its arguments.
            api_error (SourceErrorKind): Kind for an XML API error response.

        Returns:
            Any: Whatever `func` returned.

        Raises:
            _CallError: The call failed, timed out, or the session is poisoned.
            UnsafeRequestError: The call tried to write (a bug; never classified or retried).
        """
        if self._poisoned_by is not None:
            raise _CallError(
                SourceErrorKind.TEMPORARILY_UNAVAILABLE,
                f"{what} refused: {self._poisoned_by} never returned and may still hold the handle",
            )
        deadline = asyncio.timeout(self._timeout)
        try:
            async with deadline:
                return await run_abandonable(func, *args, name=f"{self._label}-{what}")
        except UnsafeRequestError:
            raise
        except TimeoutError as exc:
            if deadline.expired():
                self._poisoned_by = what
                raise _CallError(
                    SourceErrorKind.TEMPORARILY_UNAVAILABLE,
                    f"{what} timed out after {self._timeout:g}s",
                ) from exc
            raise _CallError(SourceErrorKind.TEMPORARILY_UNAVAILABLE, f"{what}: {exc}") from exc
        except (UcscException, UcsException) as exc:
            raise _CallError(
                api_error, f"{what}: XML API error {exc.error_code}: {exc.error_descr}"
            ) from exc
        except (UcscLoginError, UcsLoginError) as exc:
            raise _CallError(SourceErrorKind.PERMISSION_DENIED, f"{what}: {exc}") from exc
        except (UcscConnectionError, UcsConnectionError) as exc:
            raise _CallError(SourceErrorKind.TEMPORARILY_UNAVAILABLE, f"{what}: {exc}") from exc
        except OSError as exc:
            raise _os_failure(what, exc) from exc
        except Exception as exc:  # the SDKs' other errors and unparseable replies
            raise _CallError(
                SourceErrorKind.INVALID, f"{what}: {type(exc).__name__}: {exc}"
            ) from exc

    async def login(self) -> None:
        """
        Open the session; an XML API error here is a rejected login.

        Raises:
            _CallError: The login failed.
        """
        await self._call("login", self._handle.login, api_error=SourceErrorKind.PERMISSION_DENIED)

    async def logout(self) -> None:
        """Close the session, best-effort; never raises, so it is safe in a `finally`."""
        try:
            await self._call("logout", self._handle.logout)
        except _CallError as exc:
            log.warning("collector.ucs.logout_failed", session=self._label, error=str(exc))

    async def query_classid(self, class_id: str) -> list[Any]:
        """
        Every instance of one class.

        Args:
            class_id (str): XML class id.

        Returns:
            list[Any]: The managed objects.

        Raises:
            _CallError: The query failed.
        """
        return await self._call(f"query_classid({class_id})", self._handle.query_classid, class_id)


# ---- mapping (pure) --------------------------------------------------------------------------


@dataclass(frozen=True)
class CentralListing:
    """
    The UCS Central class listings one read takes (`CENTRAL_CLASSES`, in order).

    Attributes:
        systems (list[Any]): `computeSystem`, one per registered domain.
        blades (list[Any]): `computeBlade`.
        rack_units (list[Any]): `computeRackUnit`.
        profiles (list[Any]): `lsServer`, profiles and templates.
        cards (list[Any]): `graphicsCard`.
    """

    systems: list[Any]
    blades: list[Any]
    rack_units: list[Any]
    profiles: list[Any]
    cards: list[Any]


@dataclass(frozen=True)
class Domain:
    """
    One UCS Manager domain registered with UCS Central (`computeSystem`).

    Attributes:
        id (str): Central's domain id (`compute/sys-<id>`).
        name (str): Registered name.
        endpoint (str): Address for a UCS Manager session: `address`, else `name` (server-scan).
        last_refreshed_ts (str | None): When Central last refreshed its replica of the domain.
        inventory_status (str | None): Central's replica status (`ok`, `throttled`, ...).
    """

    id: str
    name: str
    endpoint: str
    last_refreshed_ts: str | None
    inventory_status: str | None

    @property
    def label(self) -> str:
        """The domain's name, else its id."""
        return self.name or self.id


@dataclass(frozen=True)
class Binding:
    """
    The profile bound to one server, in any state, and how it was found.

    Attributes:
        join (str): The field that bound it: `assignedToDn`, `lsDn` or `pnDn`.
        dn (str): Profile DN.
        profile (Any | None): Its `lsServer`, when Central listed it.
        others (tuple[str, ...]): Different profile DNs named by the other joins.
    """

    join: str
    dn: str
    profile: Any | None
    others: tuple[str, ...] = ()


@dataclass(frozen=True)
class CentralMap:
    """
    UCS Central's listing as collector records, before any UCS Manager fallback.

    Attributes:
        servers (tuple[ManagerServer, ...]): One per equipped server; `gpu_models` is None where
            the domain has no `graphicsCard` row in Central.
        domains (Mapping[str, Domain]): Registered domains by id.
        central_gpu_domains (frozenset[str]): Domain ids whose GPUs came from Central.
        fallback (tuple[Domain, ...]): Registered domains with servers but no `graphicsCard` row.
        unequipped (int): Compute rows dropped as not equipped.
    """

    servers: tuple[ManagerServer, ...]
    domains: Mapping[str, Domain]
    central_gpu_domains: frozenset[str]
    fallback: tuple[Domain, ...]
    unequipped: int


def domains_from(systems: Iterable[Any]) -> dict[str, Domain]:
    """
    Registered domains from `computeSystem`.

    Args:
        systems (Iterable[Any]): `computeSystem` MOs.

    Returns:
        dict[str, Domain]: By domain id; rows without an id are skipped.
    """
    domains: dict[str, Domain] = {}
    for mo in systems:
        domain_id = _attr(mo, "id")
        if not domain_id:
            continue
        name = _attr(mo, "name")
        domains[domain_id] = Domain(
            id=domain_id,
            name=name,
            endpoint=_attr(mo, "address") or name,
            last_refreshed_ts=_attr(mo, "last_refreshed_ts") or None,
            inventory_status=_attr(mo, "inventory_status") or None,
        )
    return domains


def _domain_by_key(key: str, domains: Mapping[str, Domain]) -> Domain | None:
    """
    Resolve `lsServer.domain` by name, then address, then id (server-scan's lookup order).

    Args:
        key (str): The profile's `domain` value.
        domains (Mapping[str, Domain]): Registered domains.

    Returns:
        Domain | None: The domain, if one matches.
    """
    if not key:
        return None
    for field in ("name", "endpoint", "id"):
        found = next((d for d in domains.values() if getattr(d, field) == key), None)
        if found is not None:
            return found
    return None


def index_profiles(
    ls_servers: Iterable[Any], domains: Mapping[str, Domain]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Index service profiles (not templates, as server-scan's `partition_profiles`) both ways.

    Args:
        ls_servers (Iterable[Any]): `lsServer` MOs.
        domains (Mapping[str, Domain]): Registered domains, to re-root a domain-local `pnDn`.

    Returns:
        tuple[dict[str, Any], dict[str, Any]]: Profiles by DN, and by the Central DN of the
        server their `pnDn` names.
    """
    by_dn: dict[str, Any] = {}
    by_pn_dn: dict[str, Any] = {}
    for mo in ls_servers:
        if _attr(mo, "type") in TEMPLATE_TYPES:
            continue
        by_dn[str(mo.dn)] = mo
        pn_dn = _attr(mo, "pn_dn")
        if pn_dn.startswith(_UCSM_ROOT):
            domain = _domain_by_key(_attr(mo, "domain"), domains)
            pn_dn = central_external_id(pn_dn, domain_id=domain.id) if domain else ""
        if pn_dn:
            by_pn_dn.setdefault(pn_dn, mo)
    return by_dn, by_pn_dn


def bound_profile(
    server_mo: Any, by_dn: Mapping[str, Any], by_pn_dn: Mapping[str, Any]
) -> Binding | None:
    """
    Find the profile bound to a server, in any state (user decision of 2026-10-30).

    Joins in order: `assignedToDn` (server-scan's only join), Central's `lsDn`, then `pnDn`.

    Args:
        server_mo (Any): `computeBlade` or `computeRackUnit` from Central.
        by_dn (Mapping[str, Any]): Profiles by DN.
        by_pn_dn (Mapping[str, Any]): Profiles by the server DN their `pnDn` names.

    Returns:
        Binding | None: The first join that names a profile, or None when none does.
    """
    found = [
        (join, dn)
        for join, dn in (
            ("assignedToDn", _attr(server_mo, "assigned_to_dn")),
            ("lsDn", _attr(server_mo, "ls_dn")),
        )
        if dn
    ]
    by_pn = by_pn_dn.get(str(server_mo.dn))
    if by_pn is not None:
        found.append(("pnDn", str(by_pn.dn)))
    if not found:
        return None
    join, dn = found[0]
    others = tuple(sorted({d for _, d in found} - {dn}))
    return Binding(join=join, dn=dn, profile=by_dn.get(dn), others=others)


def profile_name(binding: Binding) -> tuple[str, str]:
    """
    Name of a bound profile: `lsServer.name`, else the `ls-<name>` RN of its DN, else the DN.

    Args:
        binding (Binding): The binding.

    Returns:
        tuple[str, str]: The name, and its source (`lsServer` or `dn`).
    """
    if binding.profile is not None and (name := _attr(binding.profile, "name")):
        return name, "lsServer"
    match = _LS_RN.search(binding.dn)
    return (match.group(1) if match else binding.dn), "dn"


def _manager_server(
    mo: Any,
    *,
    manager: str,
    domain_id: str | None,
    domain: Domain | None,
    binding: Binding | None,
    gpus: tuple[str, ...] | None,
) -> ManagerServer:
    """
    One collector record for one equipped Central compute MO.

    Args:
        mo (Any): `computeBlade` or `computeRackUnit`.
        manager (str): `ucs-central:<name>`.
        domain_id (str | None): Domain id from the DN.
        domain (Domain | None): The registered domain, if listed.
        binding (Binding | None): Bound profile.
        gpus (tuple[str, ...] | None): GPU models from Central, None if unread.

    Returns:
        ManagerServer: The record.
    """
    dn = str(mo.dn)
    evidence: dict[str, Any] = {
        "domain_id": domain_id,
        "domain_last_refreshed_ts": domain.last_refreshed_ts if domain else None,
        "domain_inventory_status": domain.inventory_status if domain else None,
        "association": _attr(mo, "association") or None,
        "gpu_source": "central" if gpus is not None else None,
    }
    if gpus is None:
        evidence["gpu_unread_reason"] = (
            "UCS Central lists no graphicsCard in this domain"
            if domain
            else "domain not registered in UCS Central"
        )
    name: str | None = None
    if binding is not None:
        name, source = profile_name(binding)
        evidence |= {"profile_dn": binding.dn, "profile_join": binding.join}
        evidence["profile_name_source"] = source
        if binding.profile is not None:
            evidence["profile_assign_state"] = _attr(binding.profile, "assign_state") or None
            evidence["profile_assoc_state"] = _attr(binding.profile, "assoc_state") or None
            evidence["profile_config_state"] = _attr(binding.profile, "config_state") or None
        if binding.others:
            evidence["profile_other_dns"] = list(binding.others)
    return ManagerServer(
        vendor=VENDOR,
        serial_normalized=normalize_serial(getattr(mo, "serial", None)),
        manager=manager,
        source_id=dn,
        name=_attr(mo, "name") or dn,
        model=_attr(mo, "model") or None,
        profile_name=name,
        gpu_models=gpus,
        domain=domain.label if domain else domain_id,
        evidence=evidence,
    )


def map_central(listing: CentralListing, *, manager: str) -> CentralMap:
    """
    Map UCS Central's listing to one record per equipped server (pure, no I/O).

    Args:
        listing (CentralListing): One read's class listings.
        manager (str): `ucs-central:<name>`.

    Returns:
        CentralMap: Records, domains, and the domains needing the UCS Manager GPU fallback.
    """
    domains = domains_from(listing.systems)
    by_dn, by_pn_dn = index_profiles(listing.profiles, domains)
    computes = [*listing.blades, *listing.rack_units]
    equipped = [mo for mo in computes if is_equipped(mo)]
    cards = group_by_owning_server_dn(listing.cards, server_dns=[str(mo.dn) for mo in equipped])
    gpu_domains = frozenset(
        d for mo in listing.cards if (d := domain_id_from_dn(str(mo.dn))) is not None
    )
    servers = []
    for mo in equipped:
        domain_id = domain_id_from_dn(str(mo.dn))
        servers.append(
            _manager_server(
                mo,
                manager=manager,
                domain_id=domain_id,
                domain=domains.get(domain_id or ""),
                binding=bound_profile(mo, by_dn, by_pn_dn),
                gpus=gpu_models(cards[str(mo.dn)]) if domain_id in gpu_domains else None,
            )
        )
    with_servers = {domain_id_from_dn(str(mo.dn)) for mo in equipped}
    fallback = tuple(
        domains[d] for d in sorted(domains) if d in with_servers and d not in gpu_domains
    )
    return CentralMap(
        servers=tuple(servers),
        domains=domains,
        central_gpu_domains=gpu_domains & set(domains),
        fallback=fallback,
        unequipped=len(computes) - len(equipped),
    )


def ucsm_gpu_index(
    blades: Iterable[Any], rack_units: Iterable[Any], cards: Iterable[Any], *, domain_id: str
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """
    One domain's equipped servers as UCS Manager reports them, keyed by their Central DN.

    Args:
        blades (Iterable[Any]): UCS Manager `computeBlade`.
        rack_units (Iterable[Any]): UCS Manager `computeRackUnit`.
        cards (Iterable[Any]): UCS Manager `graphicsCard`.
        domain_id (str): Central's id for the domain.

    Returns:
        dict[str, tuple[str, tuple[str, ...]]]: Central DN -> (normalised serial, GPU models).
    """
    servers = [mo for mo in (*blades, *rack_units) if is_equipped(mo)]
    by_server = group_by_owning_server_dn(cards, server_dns=[str(mo.dn) for mo in servers])
    return {
        central_external_id(str(mo.dn), domain_id=domain_id): (
            normalize_serial(getattr(mo, "serial", None)),
            gpu_models(by_server[str(mo.dn)]),
        )
        for mo in servers
    }


def _apply_ucsm(
    server: ManagerServer, index: Mapping[str, tuple[str, tuple[str, ...]]] | None, failure: str
) -> ManagerServer:
    """
    Fill one server's GPUs from its domain's UCS Manager read, if the read and identity agree.

    Args:
        server (ManagerServer): Record from Central with `gpu_models` None.
        index (Mapping[str, tuple[str, tuple[str, ...]]] | None): The domain's read, or None.
        failure (str): Why the domain read failed, when `index` is None.

    Returns:
        ManagerServer: GPUs from UCS Manager, or still None with the reason in evidence.
    """
    entry = index.get(server.source_id) if index is not None else None
    if index is None:
        reason = f"UCS Manager read failed: {failure}"
    elif entry is None:
        reason = "UCS Manager does not list this server"
    elif entry[0] != server.serial_normalized:
        reason = "UCS Manager reports a different serial at this DN"
    else:
        evidence = {**server.evidence, "gpu_source": "ucsm"}
        evidence.pop("gpu_unread_reason", None)
        return replace(server, gpu_models=entry[1], evidence=evidence)
    return replace(server, evidence={**server.evidence, "gpu_unread_reason": reason})


# ---- the source ------------------------------------------------------------------------------


class UcsCentralSource:
    """
    One UCS Central and the UCS Manager domains registered with it, read-only (ADR-0004).

    Implements `ttv.collector.CollectorSource`. Every read opens and closes its own sessions.
    """

    def __init__(
        self,
        conn: UcsCentralConnection,
        *,
        timeout_seconds: float,
        domain_concurrency: int = 4,
        central_factory: HandleFactory | None = None,
        ucsm_factory: HandleFactory | None = None,
    ) -> None:
        """
        Configure the source; nothing is contacted until `read`.

        Args:
            conn (UcsCentralConnection): Connection settings.
            timeout_seconds (float): Deadline for each SDK call (`collector_timeout_seconds`).
            domain_concurrency (int): UCS Manager domains read at once (server-scan's default 4).
            central_factory (HandleFactory | None): Test seam; defaults to `central_handle`.
            ucsm_factory (HandleFactory | None): Test seam; defaults to `ucsm_handle`.
        """
        self.name = f"ucs-central:{conn.name}"
        self._conn = conn
        self._timeout = timeout_seconds
        self._concurrency = max(1, domain_concurrency)
        self._central_factory: HandleFactory = central_factory or functools.partial(
            central_handle, ca_bundle=conn.ca_bundle
        )
        self._ucsm_factory: HandleFactory = ucsm_factory or functools.partial(
            ucsm_handle, ca_bundle=conn.ca_bundle, timeout_seconds=timeout_seconds
        )

    async def read(self, now: datetime) -> CollectorRead:
        """
        Read every equipped server with its bound profile and GPU evidence.

        Args:
            now (datetime): Read time.

        Returns:
            CollectorRead: Always `complete=True` (Central's listing is whole); a failed
                UCS Manager domain leaves its servers' `gpu_models=None` and adds an error.

        Raises:
            SourceError: UCS Central could not be read (PERMISSION_DENIED for a rejected login).
        """
        started = time.monotonic()
        mapped = map_central(await self._read_central(), manager=self.name)
        servers = list(mapped.servers)
        outcome = dict.fromkeys(mapped.central_gpu_domains, "central")
        errors: list[str] = []
        username = self._conn.ucsm_username
        if mapped.fallback and username is None:
            outcome |= dict.fromkeys((d.id for d in mapped.fallback), "unread")
        elif mapped.fallback and username is not None:
            servers, failures = await self._ucsm_fallback(servers, mapped.fallback, username)
            for domain in mapped.fallback:
                outcome[domain.id] = "failed" if domain.id in failures else "ucsm"
                if domain.id in failures:
                    errors.append(f"domain {domain.label} (id {domain.id}): {failures[domain.id]}")
        result = CollectorRead(
            manager=self.name,
            kind=KIND,
            started_at=now,
            finished_at=now + timedelta(seconds=time.monotonic() - started),
            complete=True,
            servers=tuple(servers),
            errors=tuple(errors),
            coverage=_coverage(servers, mapped, outcome),
        )
        log.info(
            "collector.ucs_central.read",
            manager=self.name,
            complete=result.complete,
            **result.coverage,
        )
        return result

    async def aclose(self) -> None:
        """Nothing to release: every read logs out of its own sessions."""

    async def _read_central(self) -> CentralListing:
        """
        One UCS Central session: log in, list `CENTRAL_CLASSES`, log out.

        Returns:
            CentralListing: The listings.

        Raises:
            SourceError: The password file, login, a query or the deadline failed, or Central
                lists no domain.
        """
        host = self._conn.host
        try:
            password = read_secret(self._conn.password_file)
            handle = self._central_factory(host, self._conn.username, password)
        except (OSError, ValueError) as exc:
            raise SourceError(
                self.name, SourceErrorKind.INVALID, f"UCS Central setup: {exc}"
            ) from exc
        session = _Session(handle, label="ucs-central", timeout_seconds=self._timeout)
        try:
            await session.login()
            rows = [await session.query_classid(c) for c in CENTRAL_CLASSES]
        except _CallError as exc:
            raise SourceError(self.name, exc.kind, f"UCS Central {host}: {exc}") from exc
        finally:
            await session.logout()
        listing = CentralListing(*rows)
        if not domains_from(listing.systems):
            raise SourceError(
                self.name,
                SourceErrorKind.INVALID,
                f"UCS Central {host} lists no registered UCS Manager domain",
            )
        return listing

    async def _ucsm_fallback(
        self, servers: list[ManagerServer], domains: Sequence[Domain], username: str
    ) -> tuple[list[ManagerServer], dict[str, str]]:
        """
        Read GPUs through UCS Manager for domains whose Central `graphicsCard` replica is empty.

        Args:
            servers (list[ManagerServer]): Records from Central.
            domains (Sequence[Domain]): Domains to read.
            username (str): UCS Manager account.

        Returns:
            tuple[list[ManagerServer], dict[str, str]]: Updated records, and failures by domain id.
        """
        indexes: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
        failures: dict[str, str] = {}
        try:
            if self._conn.ucsm_password_file is None:
                raise ValueError("ucsm_password_file is not set")
            password = read_secret(self._conn.ucsm_password_file)
        except (OSError, ValueError) as exc:
            failures = dict.fromkeys((d.id for d in domains), f"invalid: UCS Manager setup: {exc}")
        else:
            sem = asyncio.Semaphore(self._concurrency)

            async def one(domain: Domain) -> None:
                """
                Read one domain, recording its index or its failure.

                Args:
                    domain (Domain): The domain.
                """
                async with sem:
                    try:
                        indexes[domain.id] = await self._domain_gpus(domain, username, password)
                    except _CallError as exc:
                        failures[domain.id] = f"{exc.kind}: {exc}"
                        log.warning(
                            "collector.ucs_central.domain_failed",
                            manager=self.name,
                            domain=domain.label,
                            domain_id=domain.id,
                            kind=str(exc.kind),
                            error=str(exc),
                        )

            async with asyncio.TaskGroup() as group:
                for domain in domains:
                    group.create_task(one(domain))
        fallback_ids = {d.id for d in domains}
        updated = []
        for server in servers:
            domain_id = domain_id_from_dn(server.source_id)
            if domain_id not in fallback_ids:
                updated.append(server)
                continue
            failure = failures.get(domain_id or "", "")
            updated.append(_apply_ucsm(server, indexes.get(domain_id or ""), failure))
        return updated, failures

    async def _domain_gpus(
        self, domain: Domain, username: str, password: str
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        """
        One read-only UCS Manager session for one domain: log in, list `UCSM_CLASSES`, log out.

        Args:
            domain (Domain): The domain.
            username (str): UCS Manager account.
            password (str): Its password.

        Returns:
            dict[str, tuple[str, tuple[str, ...]]]: `ucsm_gpu_index` of the domain.

        Raises:
            _CallError: The session could not be built, or login, a query or the deadline failed.
        """
        try:
            handle = self._ucsm_factory(domain.endpoint, username, password)
        except (OSError, ValueError) as exc:
            raise _CallError(SourceErrorKind.INVALID, f"UCS Manager setup: {exc}") from exc
        session = _Session(handle, label=f"ucsm-{domain.id}", timeout_seconds=self._timeout)
        try:
            await session.login()
            blades, rack_units, cards = [await session.query_classid(c) for c in UCSM_CLASSES]
        finally:
            await session.logout()
        return ucsm_gpu_index(blades, rack_units, cards, domain_id=domain.id)


def _coverage(
    servers: Sequence[ManagerServer], mapped: CentralMap, outcome: Mapping[str, str]
) -> dict[str, int]:
    """
    Counts for `collector_reads.coverage`.

    Args:
        servers (Sequence[ManagerServer]): Final records.
        mapped (CentralMap): Central's mapping.
        outcome (Mapping[str, str]): GPU source per domain id: central, ucsm, unread or failed.

    Returns:
        dict[str, int]: Domain and server counts.
    """
    by_outcome = list(outcome.values())
    return {
        "domains": len(mapped.domains),
        "domains_gpu_central": by_outcome.count("central"),
        "domains_gpu_ucsm": by_outcome.count("ucsm"),
        "domains_gpu_unread": by_outcome.count("unread"),
        "domains_failed": by_outcome.count("failed"),
        "servers": len(servers),
        "servers_unequipped_dropped": mapped.unequipped,
        "servers_without_serial": sum(not s.serial_normalized for s in servers),
        "servers_with_profile": sum(s.profile_name is not None for s in servers),
        "servers_profile_name_from_dn": sum(
            s.evidence.get("profile_name_source") == "dn" for s in servers
        ),
        "servers_with_gpus": sum(bool(s.gpu_models) for s in servers),
        "servers_gpu_unread": sum(s.gpu_models is None for s in servers),
    }
