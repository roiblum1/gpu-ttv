"""Stable asset identity: one asset per server, keyed `<vendor>:<serial_normalized>` (§5)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ttv.models import AssetInput, ServerScanServer

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class IdentityConflict:
    """
    Two or more source records that claim the same asset.

    Attributes:
        kind (str): Conflict type, e.g. `DUPLICATE_SERIAL`.
        key (str): The contested asset id.
        candidates (tuple[str, ...]): server-scan ids involved, sorted.
    """

    kind: str
    key: str
    candidates: tuple[str, ...]


def asset_id_for(server: ServerScanServer) -> str:
    """
    Return the asset id for a server-scan record.

    A record without a serial falls back to its server-scan id, so it is still tracked
    but cannot be matched to other sources (docs/discovery.md, server-scan §5).

    Args:
        server (ServerScanServer): The record.

    Returns:
        str: `<vendor>:<serial_normalized>`, or `<vendor>:serverscan-id:<id>`.
    """
    vendor = (server.vendor or "unknown").strip().lower()
    serial = server.serial_normalized.strip()
    return f"{vendor}:{serial}" if serial else f"{vendor}:serverscan-id:{server.id}"


def build_assets(
    servers: Sequence[ServerScanServer],
) -> tuple[list[AssetInput], list[IdentityConflict]]:
    """
    Group server-scan records into assets and surface duplicates instead of merging them.

    Args:
        servers (Sequence[ServerScanServer]): All records from one fetch.

    Returns:
        tuple[list[AssetInput], list[IdentityConflict]]: One input per asset id (sorted), and
        the conflicts found. A duplicated asset keeps its most recently seen record and is
        marked conflicting, so the classifier reports it as UNKNOWN.
    """
    groups: dict[str, list[ServerScanServer]] = defaultdict(list)
    for server in servers:
        groups[asset_id_for(server)].append(server)

    assets: list[AssetInput] = []
    conflicts: list[IdentityConflict] = []
    for asset_id in sorted(groups):
        members = groups[asset_id]
        chosen = max(members, key=lambda s: (s.last_seen_at or _EPOCH, s.id))
        conflict = None
        if len(members) > 1:
            ids = tuple(sorted(s.id for s in members))
            conflicts.append(IdentityConflict("DUPLICATE_SERIAL", asset_id, ids))
            conflict = f"{len(members)} server-scan records share this serial: {', '.join(ids)}"
        assets.append(AssetInput(asset_id=asset_id, serverscan=chosen, conflict=conflict))
    return assets, conflicts
