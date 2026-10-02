"""`session.safe_commit_offer` NAMES the files it declines, from the source that carries them.

`compute_offer`'s `orphans` is always empty by contract (it reads claims, never the worktree),
so it cannot name a file. The named set comes from the post-claim dirty read instead:
`reconciliation.unclaimed` filtered to paths written at or after the session's `started_at`,
surfaced as `mtime_orphans` and rendered by path. Naming never adopts: these paths stay out of
`safe_paths`.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core
from coordinator_core.tests.git_seed import seeded_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _backdate(path, seconds=3600):
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))


def test_orphans_stays_empty_and_mtime_orphans_names_the_recent_unclaimed_file(tmp_path):
    repo = seeded_repo(tmp_path, readme="x")
    (repo / "old.py").write_text("written before the session")
    _backdate(repo / "old.py")
    core.init("mine", cwd=str(repo))
    (repo / "new.py").write_text("written by a shell, no claim")

    out = safe_commit_offer._handler({"session_id": "mine", "cwd": str(repo), "dry_run": True})

    assert out["orphans"] == []
    assert out["safe_paths"] == []
    assert set(out["reconciliation"]["unclaimed"]) >= {"old.py", "new.py"}
    assert out["mtime_orphans"] == ["new.py"]
    assert "Written since this session started, claimed by no session: 1 path(s) — new.py" in out["rendered"]


def test_mtime_orphans_fails_closed_without_started_at(tmp_path):
    repo = seeded_repo(tmp_path, readme="x")
    core.init("mine", cwd=str(repo))
    (repo / "new.py").write_text("x")
    os.remove(os.path.join(core.session_dir("mine", str(repo)), "started_at"))

    assert safe_commit_offer._mtime_orphans("mine", ["new.py"], str(repo)) == []


def test_mtime_orphans_line_caps_with_a_more_tail():
    paths = ["f%02d.py" % i for i in range(safe_commit_offer._REPORT_PATH_PREVIEW_COUNT + 3)]

    line = safe_commit_offer._mtime_orphans_line(paths)

    assert line.count(".py") == safe_commit_offer._REPORT_PATH_PREVIEW_COUNT
    assert "(+3 more)" in line
    assert "NAMED, NOT ADOPTED" in line
