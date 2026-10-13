"""unarchive_on_resume: a resumed session gets its archived state dir back."""

from __future__ import annotations

import asyncio
import os

import pytest

from coordinator_core.hooks import sessionstart_dispatch as sd
from coordinator_core.session import core, scope

SID = "sess-abc"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _base(repo):
    return core.sessions_dir(str(repo))


def _mkarchive(repo, date, content="x"):
    d = os.path.join(_base(repo), ".archive", f"{SID}-{date}")
    os.makedirs(d)
    with open(os.path.join(d, "marker.txt"), "w") as fh:
        fh.write(content)
    return d


def _live(repo):
    return os.path.join(_base(repo), SID)


def test_restores_on_resume(repo):
    _mkarchive(repo, "2026-10-01")
    assert scope.unarchive_on_resume(SID, str(repo)) is True
    assert os.path.isfile(os.path.join(_live(repo), "marker.txt"))
    assert not os.listdir(os.path.join(_base(repo), ".archive"))


def test_picks_latest_of_multiple(repo):
    _mkarchive(repo, "2026-09-30", "old")
    _mkarchive(repo, "2026-10-02", "new")
    assert scope.unarchive_on_resume(SID, str(repo))
    with open(os.path.join(_live(repo), "marker.txt")) as fh:
        assert fh.read() == "new"
    assert os.listdir(os.path.join(_base(repo), ".archive")) == [f"{SID}-2026-09-30"]


def test_merges_into_live_dir_live_wins(repo):
    arch = _mkarchive(repo, "2026-10-01", "archived")
    with open(os.path.join(arch, "dispatched-agents.txt"), "w") as fh:
        fh.write("ledger")
    os.makedirs(_live(repo))
    with open(os.path.join(_live(repo), "marker.txt"), "w") as fh:
        fh.write("live")
    assert scope.unarchive_on_resume(SID, str(repo)) is True
    with open(os.path.join(_live(repo), "marker.txt")) as fh:
        assert fh.read() == "live"
    assert os.path.isfile(os.path.join(_live(repo), "dispatched-agents.txt"))
    assert os.listdir(arch) == ["marker.txt"]


def test_other_sid_prefix_not_claimed(repo):
    os.makedirs(os.path.join(_base(repo), ".archive", f"{SID}-extra-2026-10-01"))
    assert scope.unarchive_on_resume(SID, str(repo)) is False


def test_no_archive_is_noop(repo):
    assert scope.unarchive_on_resume(SID, str(repo)) is False


def test_fail_open_on_rename_error(repo, monkeypatch):
    arch = _mkarchive(repo, "2026-10-01")

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(scope.os, "rename", boom)
    assert scope.unarchive_on_resume(SID, str(repo)) is False
    assert os.path.isdir(arch)


@pytest.mark.parametrize("source,restored", [
    ("resume", True), ("startup", False), ("clear", False), ("compact", False), ("fork", False),
])
def test_dispatch_leg_gates_on_resume(repo, source, restored):
    # The real session-start record writer runs first and recreates the live dir;
    # the restore must still land (stubbing it out hid exactly that ordering).
    _mkarchive(repo, "2026-10-01")
    asyncio.run(sd._handler({"payload": {
        "source": source, "session_id": SID, "cwd": str(repo)}}))
    assert os.path.isfile(os.path.join(_live(repo), "marker.txt")) is restored


def test_end_to_end_guard_rearms(repo, monkeypatch):
    from coordinator_core.bash_guards import guard_post_executor_verification as g

    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_session_start.delete_settings_home_session_record",
        lambda sid: None,
    )
    os.makedirs(_live(repo))
    with open(os.path.join(_live(repo), "dispatched-agents.txt"), "w") as fh:
        fh.write("a1\tt\tcoordinator:executor\n")
    assert g.executor_reported({}, SID, str(repo)) is True
    assert scope.archive(SID, str(repo)) is True
    assert g.executor_reported({}, SID, str(repo)) is False
    assert scope.unarchive_on_resume(SID, str(repo)) is True
    assert g.executor_reported({}, SID, str(repo)) is True
