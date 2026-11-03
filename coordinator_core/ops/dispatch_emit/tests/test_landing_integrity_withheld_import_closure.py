"""withheld_closure_findings: a withheld path reads at HEAD, so an importer of a name only its worktree bytes bind is a finding."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit.tests.test_landing_integrity import _w
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process  # repo setup only; the calls under test are spawn-guarded


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _w(tmp_path, "pkg/__init__.py", "")
    _w(tmp_path, "pkg/old.py", "def g():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


@pytest.fixture
def no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git/process spawn in withheld_closure_findings")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)


def test_importer_of_name_only_withheld_new_file_binds_is_a_finding(repo, no_spawn):
    _w(repo, "pkg/b.py", "def f():\n    return 1\n")
    _w(repo, "a.py", "from pkg.b import f\n")
    out = li.withheld_closure_findings(repo, ["a.py", "pkg/b.py"], [], ["pkg/b.py"])
    assert [(f.path, f.reason) for f in out] == [("a.py", li.IMPORT_CLOSURE_UNTRACKED)]


def test_same_file_landing_with_it_is_clean(repo, no_spawn):
    _w(repo, "pkg/b.py", "def f():\n    return 1\n")
    _w(repo, "a.py", "from pkg.b import f\n")
    assert li.withheld_closure_findings(repo, ["a.py", "pkg/b.py"], [], []) == []


def test_withheld_path_present_at_head_with_the_name_is_not_a_finding(repo, no_spawn):
    _w(repo, "pkg/old.py", "def g():\n    return 2\n")  # modified, withheld; g bound at HEAD
    _w(repo, "a.py", "from pkg.old import g\n")
    assert li.withheld_closure_findings(repo, ["a.py", "pkg/old.py"], [], ["pkg/old.py"]) == []


def test_name_added_only_by_withheld_modification_is_a_finding(repo, no_spawn):
    _w(repo, "pkg/old.py", "def g():\n    return 1\n\ndef h():\n    return 2\n")
    _w(repo, "a.py", "from pkg.old import h\n")
    out = li.withheld_closure_findings(repo, ["a.py", "pkg/old.py"], [], ["pkg/old.py"])
    assert len(out) == 1 and out[0].path == "a.py" and "h" in out[0].detail


def test_withheld_deletion_is_dropped_from_deleted(repo, no_spawn):
    (repo / "pkg/old.py").unlink()
    _w(repo, "a.py", "from pkg.old import g\n")
    assert li.withheld_closure_findings(repo, ["a.py"], ["pkg/old.py"], ["pkg/old.py"]) == []
    out = li.withheld_closure_findings(repo, ["a.py"], ["pkg/old.py"], [])
    assert len(out) == 1 and "deleted by this commit" in out[0].detail
