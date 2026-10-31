"""Profile-less GPU collector: reads UCS Central and OpenManage, read-only (ADR-0004)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ttv.models import CollectorRead


class CollectorSource(Protocol):
    """One manager the collector reads."""

    name: str

    async def read(self, now: datetime) -> CollectorRead:
        """
        Read every server the manager knows, with its bound profile and GPU evidence.

        A listing read only in part returns `complete=False`; per-server gaps (GPUs unread,
        profile unresolved) stay on the server; a failure that leaves nothing raises
        `ttv.sources.http.SourceError`.

        Args:
            now (datetime): Read time.

        Returns:
            CollectorRead: The read.
        """
        ...

    async def aclose(self) -> None:
        """Log out and release connections."""
        ...
