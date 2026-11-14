"""Tests for _memo_owning_box.resolve_owning_box over a tmp_path sessions dir."""

import json
import os
import re
import time
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _memo_owning_box as mob

DEAD_PID = 999_999_999
REPO = Path("/somewhere/claude-klabauter")


@pytest.fixture(autouse=True)
def _local_box(monkeypatch):
    monkeypatch.setattr(mob._contract(), "local_box", lambda: "machine-a")


def _write(d, name, **fields):
    entry = {"sessionId": name, "name": name, "pid": os.getpid(), "cwd": "/x/claude-klabauter"}
    entry.update(fields)
    (d / f"{name}.json").write_text(json.dumps(entry), encoding="utf-8")


def _fresh():
    return int(time.time() * 1000)


def test_other_box(tmp_path):
    _write(tmp_path, "em-mini", unimatrixBox="mini")
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)) == ("other_box", "mini", "em-mini")


def test_this_box_wins_over_other(tmp_path):
    _write(tmp_path, "em-mini", unimatrixBox="mini")
    _write(tmp_path, "em-here")
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)) == ("this_box", "machine-a", "em-here")


def test_unknown_when_none_live_or_other_repo(tmp_path):
    _write(tmp_path, "dead", pid=DEAD_PID)
    _write(tmp_path, "elsewhere", cwd="/x/other-repo")
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)).state == "unknown"


def test_unreadable_dir_is_unknown(tmp_path):
    got = mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path / "missing"))
    assert got == ("unknown", None, None)


def test_bad_json_skipped(tmp_path):
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    _write(tmp_path, "em-mini", unimatrixBox="mini")
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)).state == "other_box"


def test_stale_mirror_ignored(tmp_path):
    _write(tmp_path, "m", unimatrixBox="mini", unimatrixRemote="mini", unimatrixFarSeen=_fresh() - 10_000_000)
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)).state == "unknown"


def test_fresh_mirror_counts(tmp_path):
    _write(tmp_path, "m", unimatrixBox="mini", unimatrixRemote="mini", unimatrixFarSeen=_fresh())
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)).state == "other_box"


def test_orphaned_mirror_ignored(tmp_path):
    _write(tmp_path, "m", unimatrixBox="mini", macSocketPath="/s", unimatrixFarSeen=_fresh(),
           unimatrixRosterPid=DEAD_PID)
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)).state == "unknown"


def test_unimatrix_stale_flag_ignored(tmp_path):
    _write(tmp_path, "m", unimatrixBox="mini", unimatrixStale=True)
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)).state == "unknown"


def test_codex_vendor_counts(tmp_path):
    _write(tmp_path, "cx", unimatrixBox="mini", unimatrixVendor="codex")
    assert mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path)) == ("other_box", "mini", "cx")


def test_windows_cwd_on_mirror_matches_posix_repo(tmp_path):
    _write(tmp_path, "m", unimatrixBox="machine_a2", unimatrixRemote="machine_a2", unimatrixFarSeen=_fresh(),
           cwd="\\".join(["X:", "x", "claude-klabauter", ""]))
    got = mob.resolve_owning_box(REPO, sessions_dir=str(tmp_path))
    assert got == ("other_box", "machine_a2", "m")


def test_far_seen_age_matches_roster_source():
    src = (mob._BIN / "unimatrix-roster.py").read_text(encoding="utf-8")
    poll = int(re.search(r"^POLL_SECONDS = (\d+)", src, re.M).group(1))
    mult = int(re.search(r"^FAR_SEEN_MAX_AGE_SECONDS = (\d+) \* POLL_SECONDS", src, re.M).group(1))
    assert mob.FAR_SEEN_MAX_AGE_SECONDS == mult * poll
