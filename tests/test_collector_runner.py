"""The `ttv-collector` job: building readers, failed reads, rounds and storage (fixture-tested)."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import DSN
from ttv.collector.main import Manager, build_managers, main, read_one, run
from ttv.collector.openmanage import OpenManageSource
from ttv.config import ManagersFile, OpenManageConnection, Settings, UcsCentralConnection
from ttv.models import CollectorRead, ManagerServer
from ttv.sources.http import SourceError, SourceErrorKind, UnsafeRequestError
from ttv.store import COLLECTOR_LOCK_KEY, PostgresStore


class FakeSource:
    """A reader that returns a fixed read, or raises."""

    def __init__(self, name: str, outcome: CollectorRead | Exception) -> None:
        """
        Build it.

        Args:
            name (str): Manager name.
            outcome (CollectorRead | Exception): What `read` returns or raises.
        """
        self.name = name
        self.outcome = outcome
        self.closed = False

    async def read(self, now: datetime) -> CollectorRead:
        """
        Return or raise the outcome.

        Args:
            now (datetime): Read time.

        Returns:
            CollectorRead: The outcome.

        Raises:
            Exception: The outcome, when it is one.
        """
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def aclose(self) -> None:
        """Record the close."""
        self.closed = True


def good_read(now: datetime) -> CollectorRead:
    """A complete OME read with one 8-GPU server and no profile."""
    server = ManagerServer(
        vendor="dell",
        serial_normalized="svc0001",
        manager="openmanage:ome-1",
        source_id="10101",
        name="r760xa-1",
        model="PowerEdge XE9680",
        profile_name=None,
        gpu_models=("NVIDIA H100 80GB",) * 8,
    )
    return CollectorRead(
        "openmanage:ome-1", "openmanage", now, now + timedelta(seconds=3), True, (server,)
    )


def connections(tmp_path: Path) -> ManagersFile:
    """One UCS Central and one OME, with password files that exist but are never read."""
    secret = tmp_path / "password"
    secret.write_text("not-a-real-password\n")
    return ManagersFile(
        ucs_central=[
            UcsCentralConnection(
                name="central-1", host="ucsc.example.test", username="u", password_file=secret
            )
        ],
        openmanage=[
            OpenManageConnection(
                name="ome-1",
                url="https://ome.example.test",
                username="u",
                password_file=secret,
                inventory_types=["serverDeviceCards", "serverPcieDevices"],
                empty_inventory_means_no_gpu=True,
            )
        ],
    )


async def test_build_managers_names_kinds_and_settings(tmp_path: Path) -> None:
    built = build_managers(connections(tmp_path), Settings(_env_file=None))
    assert [(m.kind, m.source.name) for m in built] == [
        ("ucs_central", "ucs-central:central-1"),
        ("openmanage", "openmanage:ome-1"),
    ]
    ome = built[1].source
    assert isinstance(ome, OpenManageSource)
    assert ome._inventory_types == ("serverDeviceCards", "serverPcieDevices")
    assert ome._empty_means_none is True
    for m in built:
        await m.source.aclose()


async def test_two_managers_with_one_name_are_refused(tmp_path: Path) -> None:
    managers = connections(tmp_path)
    managers.openmanage.append(managers.openmanage[0])
    with pytest.raises(ValueError, match="more than once"):
        build_managers(managers, Settings(_env_file=None))


@pytest.mark.parametrize(
    "error",
    [
        SourceError("openmanage:ome-1", SourceErrorKind.PERMISSION_DENIED, "login rejected"),
        UnsafeRequestError("refused PATCH"),
        RuntimeError("boom"),
    ],
)
async def test_a_failed_read_is_recorded_as_incomplete_with_no_servers(error: Exception) -> None:
    now = datetime.fromisoformat("2026-10-31T12:00:00+00:00")
    read = await read_one(Manager("openmanage", FakeSource("openmanage:ome-1", error)), now)
    assert (read.manager, read.kind, read.complete, read.servers) == (
        "openmanage:ome-1",
        "openmanage",
        False,
        (),
    )
    assert read.started_at == now
    (message,) = read.errors
    assert message.startswith("openmanage:ome-1: ")


async def test_once_dry_run_prints_every_manager_and_fails_on_an_incomplete_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.fromisoformat("2026-10-31T12:00:00+00:00")
    managers = [
        Manager("openmanage", FakeSource("openmanage:ome-1", good_read(now))),
        Manager(
            "ucs_central",
            FakeSource(
                "ucs-central:central-1",
                SourceError("ucs-central:central-1", SourceErrorKind.TEMPORARILY_UNAVAILABLE, "x"),
            ),
        ),
    ]
    code = await run(managers, None, interval_seconds=900, once=True, show_servers=True)
    assert code == 1
    out = capsys.readouterr().out
    printed = json.loads(out[out.index("[\n") :])
    assert [(r["manager"], r["complete"], r["servers"]) for r in printed] == [
        ("openmanage:ome-1", True, 1),
        ("ucs-central:central-1", False, 0),
    ]
    assert printed[0]["records"][0]["gpus"] == ["NVIDIA H100 80GB"] * 8
    assert printed[1]["errors"][0].startswith("ucs-central:central-1: temporarily_unavailable")


@pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")
async def test_a_round_stores_good_and_failed_reads(store: PostgresStore) -> None:
    now = datetime.fromisoformat("2026-10-31T12:00:00+00:00")
    managers = [
        Manager("openmanage", FakeSource("openmanage:ome-1", good_read(now))),
        Manager(
            "ucs_central",
            FakeSource(
                "ucs-central:central-1",
                SourceError("ucs-central:central-1", SourceErrorKind.PERMISSION_DENIED, "no"),
            ),
        ),
    ]
    assert await run(managers, store, interval_seconds=900, once=True) == 1
    state = await store.load_collector_state()
    assert [r.manager for r in state.reads] == ["openmanage:ome-1"]
    assert state.last_attempt["ucs-central:central-1"][1] is False
    assert len(state.reads[0].servers) == 1


@pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")
async def test_only_one_collector_holds_the_lock(store: PostgresStore) -> None:
    assert DSN
    other = await PostgresStore.connect(DSN)
    try:
        assert await store.try_acquire_leadership(COLLECTOR_LOCK_KEY)
        assert not await other.try_acquire_leadership(COLLECTOR_LOCK_KEY)
        assert await other.try_acquire_leadership()  # the engine's lock is separate
    finally:
        await other.close()


def test_print_config_and_missing_managers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("ttv.collector.main.configure_logging", lambda: None)  # global state
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TTV_MANAGERS_FILE", raising=False)
    assert main(["--once", "--dry-run"]) == 2
    assert "no manager configured" in capsys.readouterr().err
    managers = tmp_path / "managers.yaml"
    managers.write_text(
        "openmanage:\n  - {name: ome-1, url: 'https://ome.example.test', username: u,"
        " password_file: /nonexistent}\n"
    )
    monkeypatch.setenv("TTV_MANAGERS_FILE", str(managers))
    monkeypatch.setenv("TTV_DATABASE_URL", "postgresql://ttv:secret@db.example.test/ttv")
    assert main(["--print-config"]) == 0
    out = capsys.readouterr().out
    config = json.loads(out[out.index("{") :])
    assert config["managers"]["openmanage"][0]["inventory_types"] == ["serverDeviceCards"]
    assert "secret" not in out


@pytest.mark.skipif(not DSN, reason="TTV_TEST_DSN not set")
async def test_a_manager_removed_from_the_config_is_ignored(store: PostgresStore) -> None:
    now = datetime.fromisoformat("2026-10-31T12:00:00+00:00")
    old = dataclasses.replace(good_read(now - timedelta(days=3)), manager="openmanage:ome-old")
    await store.record_collector_read(old)
    managers = [Manager("openmanage", FakeSource("openmanage:ome-1", good_read(now)))]
    assert await run(managers, store, interval_seconds=900, once=True) == 0
    state = await store.load_collector_state()
    assert [r.manager for r in state.reads] == ["openmanage:ome-1"]
    assert set(state.last_attempt) == {"openmanage:ome-1"}
    # Configured again: its reads count again; removing it twice reports it once.
    assert await store.sync_collector_managers({"openmanage:ome-old": "openmanage"}, now) == [
        "openmanage:ome-1"
    ]
    assert await store.sync_collector_managers({"openmanage:ome-old": "openmanage"}, now) == []
    assert [r.manager for r in (await store.load_collector_state()).reads] == ["openmanage:ome-old"]
