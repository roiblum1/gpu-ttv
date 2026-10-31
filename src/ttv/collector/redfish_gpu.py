"""
Read-only iDRAC Redfish GPU reader and server-scan's GPU rules (ADR-0004, "Dell").

The GPU rules are server-scan's (`redfish/mapping.py@defe172`, its ADR-0016/0020), trimmed to the
one thing the collector needs: a model string per GPU. Requests are GET-only with HTTP Basic auth,
so nothing on the BMC is created, changed or reset (no session, no action).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ttv.sources.http import UnsafeRequestError, get_json

REDFISH_ROOT = "/redfish/v1"
SYSTEMS_PATH = "/redfish/v1/Systems"
RULE_PROCESSORS = "processors"
RULE_PCIE = "pcie"

# Copied from server-scan backend/app/infrastructure/providers/redfish/mapping.py@defe172.
GPU_PCI_VENDOR_IDS: dict[str, str] = {"10DE": "NVIDIA", "1002": "AMD", "8086": "Intel"}

# Copied from server-scan backend/app/infrastructure/providers/redfish/mapping.py@defe172.
GPU_DESCRIPTION_HINTS = ("VGA", "GPU", "3D", "DISPLAY")

# Copied from server-scan backend/app/infrastructure/providers/redfish/mapping.py@defe172.
BUILTIN_PCI_DEVICE_MODELS: dict[tuple[str, str], str] = {
    ("10de", "15f7"): "Tesla P100-PCIE-12GB",
    ("10de", "15f8"): "Tesla P100-PCIE-16GB",
    ("10de", "1eb8"): "Tesla T4",
    ("10de", "20b0"): "A100-SXM4-40GB",
    ("10de", "20b1"): "A100-PCIE-40GB",
    ("10de", "20b2"): "A100-SXM4-80GB",
    ("10de", "20b5"): "A100-PCIE-80GB",
    ("10de", "20b7"): "A30",
    ("10de", "20bd"): "A800-SXM4-40GB",
    ("10de", "20f3"): "A800-SXM4-80GB",
    ("10de", "2235"): "A40",
    ("10de", "2236"): "A10",
    ("10de", "2237"): "A10G",
    ("10de", "2322"): "H800 PCIe",
    ("10de", "2324"): "H800",
    ("10de", "2330"): "H100-SXM5-80GB",
    ("10de", "2331"): "H100 PCIe",
}

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_ALLOWED_PATHS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"^{SYSTEMS_PATH}$",
        rf"^{SYSTEMS_PATH}/{_SEGMENT}$",
        rf"^{SYSTEMS_PATH}/{_SEGMENT}/Processors$",
        rf"^{SYSTEMS_PATH}/{_SEGMENT}/Processors/{_SEGMENT}$",
        rf"^{REDFISH_ROOT}/(?:Systems|Chassis)/{_SEGMENT}/PCIeDevices/{_SEGMENT}$",
    )
)
_REFUSED_SEGMENTS = frozenset({"Actions", "SessionService", "Sessions", "Oem"})
# OME device cards (and, unverified, iDRAC) name the vendor instead of a PCI ID (ADR-0004).
_GPU_VENDOR_NAME = re.compile(r"\b(?:NVIDIA|AMD|ADVANCED MICRO DEVICES|INTEL)\b")
_ADDRESS = re.compile(r"^(?:[A-Za-z0-9.-]+|\[?[0-9A-Fa-f:.]+\]?)$")


class RedfishPayloadError(ValueError):
    """A BMC answered with something the reader cannot use; that server's GPUs stay unread."""


@dataclass(frozen=True)
class RedfishGpus:
    """
    GPU evidence read from one iDRAC.

    Attributes:
        models (tuple[str, ...] | None): One model per GPU ("" if unnamed); () when the BMC
            reports none; None when the BMC's answer cannot rule GPUs in or out.
        rule (str): Which rule decided: `processors` or `pcie`.
        note (str | None): Why `models` is None, when it is.
    """

    models: tuple[str, ...] | None
    rule: str
    note: str | None = None


# Copied from server-scan backend/app/infrastructure/providers/redfish/mapping.py@defe172.
def is_absent(resource: Mapping[str, Any]) -> bool:
    """
    Report whether a component is physically not installed (`Status.State == "Absent"`).

    Args:
        resource (Mapping[str, Any]): Any Redfish resource.

    Returns:
        bool: True when the resource reports itself absent.
    """
    status = resource.get("Status")
    return isinstance(status, dict) and str(status.get("State", "")) == "Absent"


# Copied from server-scan backend/app/infrastructure/providers/redfish/mapping.py@defe172.
def is_gpu_processor(processor: Mapping[str, Any]) -> bool:
    """
    Report whether a `Processor` entry represents a GPU rather than a CPU.

    Args:
        processor (Mapping[str, Any]): A `Processor` resource.

    Returns:
        bool: True when `ProcessorType == "GPU"` and the slot is not reported absent.
    """
    return str(processor.get("ProcessorType", "")) == "GPU" and not is_absent(processor)


# Adapted from server-scan redfish/mapping.py@defe172 `gpus_from_processors` (model only).
def gpu_models_from_processors(processors: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """
    One model per GPU (`ProcessorType == "GPU"`) in a server's `Processors`.

    Args:
        processors (Sequence[Mapping[str, Any]]): `Processors` members.

    Returns:
        tuple[str, ...]: `Model` of each GPU, "" when it has none.
    """
    return tuple(str(p.get("Model") or "") for p in processors if is_gpu_processor(p))


# Adapted from server-scan redfish/mapping.py@defe172 `is_gpu_pcie_device`: vendor names count too.
def is_gpu_pcie_device(device: Mapping[str, Any]) -> bool:
    """
    Report whether a PCIe device (Redfish `PCIeDevice` or OME device card) looks like a GPU.

    Args:
        device (Mapping[str, Any]): The resource; `Manufacturer`, `Description`, `Status`.

    Returns:
        bool: True when `Manufacturer` starts with a GPU vendor's PCI-SIG ID or names that
            vendor, and `Description` names a display/GPU-shaped device; neither alone counts.
    """
    if is_absent(device):
        return False
    manufacturer = str(device.get("Manufacturer") or "").upper()
    if manufacturer[:4] not in GPU_PCI_VENDOR_IDS and not _GPU_VENDOR_NAME.search(manufacturer):
        return False
    description = str(device.get("Description") or "").upper()
    return any(hint in description for hint in GPU_DESCRIPTION_HINTS)


# Copied from server-scan backend/app/infrastructure/providers/redfish/mapping.py@defe172.
def pci_ids_from_manufacturer(manufacturer: str) -> tuple[str, str] | None:
    """
    Split a `Manufacturer` that packs a raw PCI vendor+device ID as 8 hex digits.

    Args:
        manufacturer (str): The raw `Manufacturer` string.

    Returns:
        tuple[str, str] | None: `(vendor_id, device_id)`, lowercase, or None when it is not
            8 hex digits.
    """
    candidate = manufacturer.strip()
    if len(candidate) != 8:
        return None
    try:
        int(candidate, 16)
    except ValueError:
        return None
    return candidate[:4].lower(), candidate[4:].lower()


# Adapted from server-scan redfish/mapping.py@defe172 `pcie_device_to_gpu` (model only).
def pcie_gpu_model(device: Mapping[str, Any]) -> str:
    """
    The model of a `PCIeDevice` already confirmed a GPU: a known PCI ID's name, else `Description`.

    Args:
        device (Mapping[str, Any]): The device.

    Returns:
        str: The model, "" when there is none.
    """
    model = str(device.get("Description") or "")
    pci_ids = pci_ids_from_manufacturer(str(device.get("Manufacturer") or ""))
    return BUILTIN_PCI_DEVICE_MODELS.get(pci_ids, model) if pci_ids else model


# Adapted from server-scan redfish/mapping.py@defe172 `gpus_from_pcie_devices` (model only).
def gpu_models_from_pcie_devices(devices: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """
    Screen `PCIeDevices` for GPUs, the fallback when `Processors` reports none.

    Args:
        devices (Sequence[Mapping[str, Any]]): `PCIeDevice` resources.

    Returns:
        tuple[str, ...]: One model per matched GPU, in encounter order.
    """
    return tuple(pcie_gpu_model(d) for d in devices if is_gpu_pcie_device(d))


# Copied from server-scan backend/app/infrastructure/providers/redfish/mapping.py@defe172.
def pcie_device_refs(system: Mapping[str, Any]) -> tuple[str, ...]:
    """
    A `ComputerSystem`'s own `PCIeDevices` links (a direct array, not a collection).

    Args:
        system (Mapping[str, Any]): The `ComputerSystem` resource.

    Returns:
        tuple[str, ...]: Every entry's `@odata.id`, or `()` when absent or empty.
    """
    entries = system.get("PCIeDevices")
    if not isinstance(entries, list):
        return ()
    return tuple(
        odata_id
        for entry in entries
        if isinstance(entry, dict) and isinstance(odata_id := entry.get("@odata.id"), str)
    )


# Copied from server-scan redfish/client.py@defe172 `validate_odata_id` (raises our error type).
def validate_odata_id(odata_id: object) -> str:
    """
    Check that a link the BMC handed us points back into its own tree.

    Args:
        odata_id (object): The `@odata.id` value as received.

    Returns:
        str: The validated path.

    Raises:
        RedfishPayloadError: Missing, not a string, not under `/redfish/v1`, or traversing.
    """
    if not isinstance(odata_id, str) or not odata_id:
        raise RedfishPayloadError(f"@odata.id is missing or not a string: {odata_id!r}")
    if not odata_id.startswith(REDFISH_ROOT):
        raise RedfishPayloadError(f"@odata.id {odata_id!r} is not a path under {REDFISH_ROOT}")
    if ".." in odata_id.split("/"):
        raise RedfishPayloadError(f"@odata.id {odata_id!r} contains a traversal segment")
    return odata_id


def is_allowed_path(path: str) -> bool:
    """
    Tell whether a path is one of the GPU reads: systems, processors, PCIe devices.

    Args:
        path (str): Request path.

    Returns:
        bool: True for the allow-listed resource shapes with no refused segment.
    """
    if _REFUSED_SEGMENTS & set(path.split("/")):
        return False
    return any(pattern.fullmatch(path) for pattern in _ALLOWED_PATHS)


def check_request(method: str, url: httpx.URL, allowed_hosts: Collection[str]) -> None:
    """
    Refuse every iDRAC request except an HTTPS GET of an allow-listed path on a listed iDRAC.

    Args:
        method (str): HTTP method.
        url (httpx.URL): Absolute request URL.
        allowed_hosts (Collection[str]): iDRAC hosts of the current OME listing.

    Raises:
        UnsafeRequestError: The request is not on the allow-list.
    """
    if method != "GET":
        raise UnsafeRequestError(f"refused {method}: iDRAC Redfish is read-only here")
    if url.scheme != "https" or url.port not in (None, 443):
        raise UnsafeRequestError("refused a non-HTTPS or non-443 iDRAC request")
    if url.host not in allowed_hosts:
        raise UnsafeRequestError("refused a request to a host OME did not list")
    if url.userinfo or url.fragment or url.query:
        raise UnsafeRequestError("refused userinfo, a fragment or a query string")
    if url.raw_path.decode("ascii") != url.path:
        raise UnsafeRequestError("refused a percent-encoded path")
    if not is_allowed_path(url.path):
        raise UnsafeRequestError("refused a path outside the GPU reads")


def idrac_origin(address: str) -> httpx.URL | None:
    """
    The HTTPS origin of an iDRAC from OME's `DeviceManagement[0].NetworkAddress`.

    Args:
        address (str): Host name, IPv4 or IPv6 address.

    Returns:
        httpx.URL | None: `https://<host>`, or None when the value is not a bare host.
    """
    address = address.strip()
    if not _ADDRESS.fullmatch(address):
        return None
    if ":" in address and not address.startswith("["):
        address = f"[{address}]"
    try:
        return httpx.URL(f"https://{address}")
    except httpx.InvalidURL:
        return None


def _checked(odata_id: object) -> str:
    """
    Validate a BMC-supplied link and require it to be one of the GPU reads.

    Args:
        odata_id (object): The `@odata.id`.

    Returns:
        str: The path.

    Raises:
        RedfishPayloadError: The link is malformed or outside the allow-list.
    """
    path = validate_odata_id(odata_id)
    if not is_allowed_path(path):
        raise RedfishPayloadError(f"link {path!r} is outside the GPU reads")
    return path


async def read_gpu_models(
    client: httpx.AsyncClient,
    origin: httpx.URL,
    *,
    source: str,
    headers: Mapping[str, str],
    pcie_fallback: bool,
    max_members: int,
    attempts: int,
    base_delay: float,
) -> RedfishGpus:
    """
    Read one iDRAC's GPUs with server-scan's rule: GPU `Processors`, else GPU-shaped PCIe devices.

    Args:
        client (httpx.AsyncClient): Client whose request hook applies `check_request`.
        origin (httpx.URL): `idrac_origin(...)` of the BMC.
        source (str): Source name for errors.
        headers (Mapping[str, str]): The Basic `Authorization` header.
        pcie_fallback (bool): Screen PCIe devices when `Processors` lists no GPU.
        max_members (int): Most members read from one collection or PCIe device list.
        attempts (int): Tries per request (`get_json`).
        base_delay (float): First retry backoff, in seconds.

    Returns:
        RedfishGpus: The GPUs and which rule found them.

    Raises:
        SourceError: A request failed.
        RedfishPayloadError: The BMC's answer is unusable.
    """

    async def get(path: str) -> dict[str, Any]:
        """
        GET one resource of this BMC.

        Args:
            path (str): Allow-listed path.

        Returns:
            dict[str, Any]: The resource.

        Raises:
            RedfishPayloadError: The body is not a JSON object.
        """
        body = await get_json(
            client,
            str(origin.copy_with(path=path)),
            source=source,
            headers=headers,
            attempts=attempts,
            base_delay=base_delay,
        )
        if not isinstance(body, dict):
            raise RedfishPayloadError(f"{path}: not a JSON object")
        return body

    systems = await _members(get, SYSTEMS_PATH, max_members)
    if not systems:
        raise RedfishPayloadError("the BMC lists no ComputerSystem")
    models: list[str] = []
    for system in systems:
        link = system.get("Processors")
        path = link.get("@odata.id") if isinstance(link, dict) else None
        if not path:
            raise RedfishPayloadError("a ComputerSystem has no Processors link")
        models.extend(gpu_models_from_processors(await _members(get, _checked(path), max_members)))
    if models or not pcie_fallback:
        return RedfishGpus(tuple(models), RULE_PROCESSORS)
    refs = [ref for system in systems for ref in pcie_device_refs(system)]
    if not refs:
        return RedfishGpus(None, RULE_PCIE, "no GPU in Processors and no PCIeDevices to screen")
    if len(refs) > max_members:
        return RedfishGpus(None, RULE_PCIE, f"more than {max_members} PCIe devices to screen")
    devices = [await get(_checked(ref)) for ref in dict.fromkeys(refs)]
    return RedfishGpus(gpu_models_from_pcie_devices(devices), RULE_PCIE)


async def _members(
    get: Callable[[str], Awaitable[dict[str, Any]]], path: str, max_members: int
) -> list[dict[str, Any]]:
    """
    Read a collection's members, following each member link that is not already expanded.

    Args:
        get (Callable[[str], Awaitable[dict[str, Any]]]): GET of one allow-listed path.
        path (str): The collection.
        max_members (int): Most members accepted.

    Returns:
        list[dict[str, Any]]: Every member resource.

    Raises:
        RedfishPayloadError: The collection is paged, too large or malformed.
    """
    page = await get(path)
    if page.get("Members@odata.nextLink"):
        raise RedfishPayloadError(f"{path}: paged collections are not read")
    raw = page.get("Members")
    if not isinstance(raw, list) or not all(isinstance(m, dict) for m in raw):
        raise RedfishPayloadError(f"{path}: Members is not a list of objects")
    if len(raw) > max_members:
        raise RedfishPayloadError(f"{path}: more than {max_members} members")
    return [m if "@odata.type" in m else await get(_checked(m.get("@odata.id"))) for m in raw]
