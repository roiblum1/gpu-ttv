"""Tests for the read-only server-scan source (fixture-tested; no network)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from ttv.sources.serverscan import (
    ROWS_PATH,
    InMemoryDetailCache,
    ServerScanError,
    ServerScanSource,
    UnsafeRequestError,
    row_hash,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://server-scan.test"
NOW = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
DELL = "srv_5532a086f2514abaa7643daf0881c603"
CISCO = "srv_beec9a7ab8aa4a2a8107625e1ab6bc82"
STANDALONE = "srv_de2beaa28a3b40a3a2334027303c9170"


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    data.pop("_meta", None)
    return data


def sandbox_rows() -> list[dict[str, Any]]:
    """Rows for DELL, CISCO, STANDALONE in that order (sandbox capture)."""
    return _load(FIXTURES / "serverscan-adapter" / "rows-sandbox.json")["items"]


def sandbox_details() -> dict[str, dict[str, Any]]:
    paths = (FIXTURES / "serverscan-sandbox").glob("serverscan-*.json")
    details = [_load(p) for p in paths if "openapi" not in p.name]
    return {d["id"]: d for d in details}


class FakeServerScan:
    """A MockTransport handler that serves rows (with a weak ETag) and details."""

    def __init__(self, rows: list[dict[str, Any]], details: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.details = details
        self.requests: list[httpx.Request] = []
        self.rows_override: Callable[[httpx.Request], httpx.Response] | None = None
        self.detail_errors: dict[str, int | Exception] = {}
        self.delay = 0.0
        self.in_flight = 0
        self.max_in_flight = 0

    def detail_ids(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[1] for r in self.requests if r.url.path != ROWS_PATH]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == ROWS_PATH:
            return self._rows(request)
        server_id = request.url.path.rsplit("/", 1)[1]
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        error = self.detail_errors.get(server_id)
        if isinstance(error, Exception):
            raise error
        if error is not None:
            return httpx.Response(error)
        if server_id not in self.details:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=self.details[server_id])

    def _rows(self, request: httpx.Request) -> httpx.Response:
        if self.rows_override is not None:
            return self.rows_override(request)
        body = json.dumps({"generated_at": "2026-10-30T19:00:10.741316Z", "items": self.rows})
        etag = f'W/"{hashlib.sha256(body.encode()).hexdigest()[:32]}"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        headers = {"ETag": etag, "Content-Type": "application/json"}
        return httpx.Response(200, content=body.encode(), headers=headers)


def sandbox_fake() -> FakeServerScan:
    return FakeServerScan(sandbox_rows(), sandbox_details())


def synthetic_fleet(count: int) -> FakeServerScan:
    """`count` copies of the Cisco sample with distinct ids and serials."""
    rows, details = [], {}
    for n in range(count):
        server_id, serial = f"srv_syn{n:03d}", f"SYN{n:03d}"
        row = {**copy.deepcopy(sandbox_rows()[1]), "id": server_id, "serial": serial}
        detail = {**copy.deepcopy(sandbox_details()[CISCO]), "id": server_id}
        detail["identity"] = {
            **detail["identity"],
            "serial": serial,
            "serial_normalized": serial.lower(),
        }
        rows.append(row)
        details[server_id] = detail
    return FakeServerScan(rows, details)


def make_source(
    fake: FakeServerScan,
    *,
    cache: InMemoryDetailCache | None = None,
    token: str | None = None,
    ttl: timedelta = timedelta(hours=6),
    budget: int = 100,
    concurrency: int = 4,
) -> ServerScanSource:
    return ServerScanSource(
        BASE_URL,
        token=token,
        ca_bundle=None,
        timeout_seconds=5.0,
        detail_ttl=ttl,
        detail_max_per_scan=budget,
        detail_concurrency=concurrency,
        cache=cache or InMemoryDetailCache(),
        transport=httpx.MockTransport(fake.handler),
    )


async def test_builds_servers_from_sandbox_samples() -> None:
    fake = sandbox_fake()
    async with make_source(fake) as source:
        result = await source.fetch(NOW)

    assert result.listing_complete and not result.not_modified
    assert (result.details_fetched, result.details_failed, result.pending_detail_ids) == (3, 0, ())
    assert result.generated_at == datetime(2026, 10, 30, 19, 0, 10, 741316, tzinfo=UTC)
    by_id = {s.id: s for s in result.servers}
    dell, cisco, standalone = by_id[DELL], by_id[CISCO], by_id[STANDALONE]

    assert (dell.vendor, dell.serial_normalized, dell.source_provider) == (
        "dell",
        "del0000818",
        "OPENMANAGE",
    )
    assert dell.system_uuid == "706017d2-6df6-4277-b893-85d0ea77c46c"
    assert dell.gpu_models == ("NVIDIA T4 16GB",) * 2
    assert (dell.lifecycle_state, dell.stale, dell.health_overall) == (
        "AVAILABLE",
        True,
        "CRITICAL",
    )
    assert dell.created_at == datetime(2026, 9, 17, 13, 25, 36, 403377, tzinfo=UTC)
    assert cisco.gpu_models == ("NVIDIA L40S 48GB",) * 8
    assert (cisco.lifecycle_state, cisco.cluster_name, cisco.mce_name) == (
        "INSTALLED",
        "hc-bat-yam-01",
        "mce-bat-yam",
    )
    # last_seen_at comes from the 10-30 row, not the 10-28 detail sample.
    assert cisco.last_seen_at == datetime(2026, 10, 30, 19, 0, 3, 19857, tzinfo=UTC)
    assert (standalone.vendor, standalone.serial_normalized) == ("standalone", "sta0000686")
    assert standalone.profile_template_name is None
    for server in result.servers:
        assert server.maintenance_enabled is False and server.maintenance_since is None
        for stamp in (server.last_seen_at, server.created_at):
            assert stamp is not None and stamp.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("gpus", "unread_fields", "expected"),
    [
        pytest.param("absent", [], None, id="absent-is-unread"),
        pytest.param(None, [], None, id="null-is-unread"),
        pytest.param([], [], (), id="empty-is-no-gpus"),
        pytest.param([], ["hardware.gpus"], None, id="empty-but-flagged-unread"),
        pytest.param(
            [{"model": "NVIDIA H100 80GB"}, {"model": None}], [], ("NVIDIA H100 80GB", "")
        ),
    ],
)
async def test_gpu_inventory_unread_is_not_zero(
    gpus: Any, unread_fields: list[str], expected: tuple[str, ...] | None
) -> None:
    fake = sandbox_fake()
    detail = fake.details[CISCO]
    detail["unread_fields"] = unread_fields
    if gpus == "absent":
        del detail["hardware"]["gpus"]
    else:
        detail["hardware"]["gpus"] = gpus
    async with make_source(fake) as source:
        result = await source.fetch(NOW)
    assert {s.id: s for s in result.servers}[CISCO].gpu_models == expected


async def test_row_and_detail_merge_rules() -> None:
    fake = sandbox_fake()
    cisco_row = fake.rows[1]
    del cisco_row["vendor"]
    cisco_row["maintenance"] = {"enabled": True, "reason": "RMA 4411", "created_by": "dc"}
    cisco_row["reachable"] = False
    cisco = fake.details[CISCO]
    cisco["maintenance"] = {"enabled": True, "created_at": "2026-10-29T08:00:00+02:00"}
    cisco["reachable"] = False
    cisco["unreachable_since"] = "2026-10-29T05:00:00"
    # A detail left over from an earlier maintenance episode while the row says none.
    fake.details[DELL]["maintenance"] = {"enabled": False, "created_at": "2026-01-01T00:00:00Z"}
    async with make_source(fake) as source:
        result = await source.fetch(NOW)

    by_id = {s.id: s for s in result.servers}
    server = by_id[CISCO]
    assert server.vendor == "cisco"
    assert (server.maintenance_enabled, server.maintenance_reason) == (True, "RMA 4411")
    assert server.maintenance_since == datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
    assert server.reachable is False
    assert server.unreachable_since == datetime(2026, 10, 29, 5, 0, tzinfo=UTC)
    assert by_id[DELL].maintenance_since is None


async def test_not_modified_reuses_listing_and_later_errors_still_raise() -> None:
    fake = sandbox_fake()
    async with make_source(fake) as source:
        first = await source.fetch(NOW)
        etag = fake.requests[0].headers.get("if-none-match")
        fake.requests.clear()
        second = await source.fetch(NOW + timedelta(minutes=5))
        assert etag is None and fake.requests[0].headers["if-none-match"].startswith('W/"')
        assert second.not_modified and fake.detail_ids() == []
        assert (second.servers, second.generated_at) == (first.servers, first.generated_at)

        fake.rows_override = lambda _: httpx.Response(503)
        with pytest.raises(ServerScanError):
            await source.fetch(NOW + timedelta(minutes=10))


async def test_row_hash_ignores_only_last_seen_at_and_stale() -> None:
    row = sandbox_rows()[1]
    base = row_hash(row)
    assert row_hash({**row, "last_seen_at": "2026-10-30T21:00:00Z", "stale": True}) == base
    assert row_hash(dict(reversed(row.items()))) == base
    assert row_hash({**row, "health": "WARNING"}) != base
    assert row_hash({**row, "maintenance": {**row["maintenance"], "reason": "RMA"}}) != base

    fake = sandbox_fake()
    async with make_source(fake) as source:
        await source.fetch(NOW)
        fake.requests.clear()
        fake.rows[1]["last_seen_at"] = "2026-10-30T21:45:00Z"
        volatile = await source.fetch(NOW + timedelta(minutes=5))
        assert not volatile.not_modified and fake.detail_ids() == []
        seen = {s.id: s for s in volatile.servers}[CISCO].last_seen_at
        assert seen == datetime(2026, 10, 30, 21, 45, tzinfo=UTC)

        fake.rows[1]["health"] = "WARNING"
        await source.fetch(NOW + timedelta(minutes=10))
        assert fake.detail_ids() == [CISCO]


async def test_detail_budget_and_concurrency_bound() -> None:
    fake = synthetic_fleet(10)
    fake.delay = 0.01
    async with make_source(fake, budget=6, concurrency=3) as source:
        result = await source.fetch(NOW)
    assert len(fake.detail_ids()) == 6 and fake.max_in_flight == 3
    assert (result.details_fetched, len(result.servers)) == (6, 6)
    assert result.pending_detail_ids == tuple(row["id"] for row in fake.rows[6:])


async def test_detail_priority_missing_then_changed_then_oldest_expired() -> None:
    fake = synthetic_fleet(6)
    a, b, c, d, e, f = (row["id"] for row in fake.rows)
    every_row = list(fake.rows)
    fake.rows = every_row[:4]
    cache = InMemoryDetailCache()
    async with make_source(fake, cache=cache) as warm:
        await warm.fetch(NOW - timedelta(hours=1))
    entries = await cache.get_many([b, c, d])
    await cache.put_many(
        [
            replace(entries[b], row_hash="hash-of-an-older-row"),
            replace(entries[c], fetched_at=NOW - timedelta(hours=10)),
            replace(entries[d], fetched_at=NOW - timedelta(hours=20)),
        ]
    )
    fake.rows = every_row
    fake.requests.clear()
    async with make_source(fake, cache=cache, budget=4, concurrency=1) as source:
        result = await source.fetch(NOW)
    assert fake.detail_ids() == [e, f, b, d]
    # c's detail is past its TTL but still usable; a's is fresh.
    assert (len(result.servers), result.pending_detail_ids) == (6, ())
    assert a in {s.id for s in result.servers}


async def test_ttl_expiry_refreshes_unchanged_rows() -> None:
    fake = sandbox_fake()
    async with make_source(fake, ttl=timedelta(hours=6)) as source:
        await source.fetch(NOW)
        fake.requests.clear()
        within = await source.fetch(NOW + timedelta(hours=5))
        assert within.not_modified and fake.detail_ids() == []
        # A GPU change shows in the detail only, so the TTL is what picks it up.
        fake.details[CISCO]["hardware"]["gpus"] = fake.details[CISCO]["hardware"]["gpus"][:4]
        after = await source.fetch(NOW + timedelta(hours=7))
    assert sorted(fake.detail_ids()) == sorted([DELL, CISCO, STANDALONE])
    assert {s.id: s for s in after.servers}[CISCO].gpu_models == ("NVIDIA L40S 48GB",) * 4


async def test_detail_failures_leave_ids_pending_without_raising() -> None:
    fake = sandbox_fake()
    fake.detail_errors = {CISCO: 500, STANDALONE: httpx.ReadTimeout("slow")}
    async with make_source(fake) as source:
        first = await source.fetch(NOW)
        assert [s.id for s in first.servers] == [DELL]
        assert first.pending_detail_ids == (CISCO, STANDALONE)
        assert (first.details_fetched, first.details_failed) == (1, 2)

        fake.detail_errors = {}
        second = await source.fetch(NOW + timedelta(minutes=5))
        assert (len(second.servers), second.pending_detail_ids) == (3, ())

        # Refreshes fail: a changed serial must not keep the old identity; other changes may.
        fake.detail_errors = {DELL: 503, CISCO: 503}
        fake.rows[0]["serial"] = "DEL0009999"
        fake.rows[1]["health"] = "WARNING"
        third = await source.fetch(NOW + timedelta(minutes=10))
    assert third.pending_detail_ids == (DELL,)
    assert {s.id: s for s in third.servers}[CISCO].health_overall == "WARNING"


def _raise_connect_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("down", request=request)


def _set_override(response: httpx.Response) -> Callable[[FakeServerScan], None]:
    def apply(fake: FakeServerScan) -> None:
        fake.rows_override = lambda _: response

    return apply


def _timeout(fake: FakeServerScan) -> None:
    fake.rows_override = _raise_connect_timeout


def _wrong_type(fake: FakeServerScan) -> None:
    fake.rows[0]["stale"] = "yes"


def _unsafe_id(fake: FakeServerScan) -> None:
    fake.rows[0]["id"] = "available"


def _duplicate_id(fake: FakeServerScan) -> None:
    fake.rows.append(copy.deepcopy(fake.rows[0]))


@pytest.mark.parametrize(
    "break_rows",
    [
        pytest.param(_set_override(httpx.Response(503)), id="http-503"),
        pytest.param(_timeout, id="timeout"),
        pytest.param(_set_override(httpx.Response(200, content=b"<html>")), id="not-json"),
        pytest.param(
            _set_override(httpx.Response(200, json={"generated_at": None})), id="no-items"
        ),
        pytest.param(_set_override(httpx.Response(304)), id="304-without-listing"),
        pytest.param(_wrong_type, id="wrong-field-type"),
        pytest.param(_unsafe_id, id="id-available"),
        pytest.param(_duplicate_id, id="duplicate-id"),
    ],
)
async def test_rows_failure_raises_and_fetches_no_detail(
    break_rows: Callable[[FakeServerScan], None],
) -> None:
    fake = sandbox_fake()
    break_rows(fake)
    async with make_source(fake) as source:
        with pytest.raises(ServerScanError):
            await source.fetch(NOW)
    assert {r.url.path for r in fake.requests} == {ROWS_PATH}


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/api/v1/servers/available"),
        ("GET", "/api/v1/servers/Available"),
        ("GET", "/api/v1/servers/available?name=ocp-x"),
        ("GET", "/api/v1/servers/rows?page_size=1"),
        ("GET", "/api/v1/servers/srv_1/events"),
        ("GET", "/api/v1/servers"),
        ("GET", "https://elsewhere.test/api/v1/servers/rows"),
        ("HEAD", "/api/v1/servers/rows"),
        ("POST", "/api/v1/servers/rows"),
        ("PUT", "/api/v1/servers/srv_1"),
        ("DELETE", "/api/v1/servers/srv_1"),
    ],
)
async def test_guard_refuses_all_but_get_rows_and_detail(method: str, url: str) -> None:
    fake = sandbox_fake()
    async with make_source(fake) as source:
        with pytest.raises(UnsafeRequestError):
            await source._client.request(method, url)
    assert fake.requests == []


async def test_bearer_token_sent_and_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    token = "tok-3f9c-never-log-me"
    fake = sandbox_fake()
    with capture_logs() as events:
        async with make_source(fake, token=token) as source:
            await source.fetch(NOW)
            fake.rows_override = lambda _: httpx.Response(401)
            with pytest.raises(ServerScanError) as excinfo:
                await source.fetch(NOW)
    assert fake.requests
    assert all(r.headers["authorization"] == f"Bearer {token}" for r in fake.requests)
    assert {e["event"] for e in events} >= {"serverscan_fetch", "serverscan_rows_failed"}
    assert token not in repr(events) + caplog.text + str(excinfo.value)

    anonymous = sandbox_fake()
    async with make_source(anonymous) as source:
        await source.fetch(NOW)
    assert all("authorization" not in r.headers for r in anonymous.requests)
