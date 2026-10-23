"""Tests for diff_test_select: pure `select` cases plus tmp_path git repos through `main()`."""
from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import diff_test_select as d
from coordinator_core.win_portability import no_console_creationflags

SURFACE = ["pkg"]
GREP = "pkg/tests/test_a.py:from pkg.a import x\npkg/tests/test_b.py:import pkg.b\n"


def _sel(edited, passed_at=None, unchanged=lambda s: False):
    return d.select(edited, SURFACE, GREP, passed_at or {}, unchanged)


def test_test_file_maps_to_itself():
    assert _sel(["pkg/tests/test_z.py"]).tests == ("pkg/tests/test_z.py",)


def test_source_maps_to_referencers():
    assert _sel(["pkg/a.py"]).tests == ("pkg/tests/test_a.py",)


def test_out_of_surface_dropped():
    s = _sel(["other/a.py", "other/tests/test_q.py"])
    assert s.tests == () and s.skipped == ()


def test_passed_and_unchanged_skipped():
    s = _sel(["pkg/a.py", "pkg/b.py"], {"pkg/tests/test_a.py": "abc"}, lambda sha: sha == "abc")
    assert s.tests == ("pkg/tests/test_b.py",)
    assert s.skipped == ("pkg/tests/test_a.py",)


def test_passed_but_changed_still_runs():
    s = _sel(["pkg/a.py"], {"pkg/tests/test_a.py": "abc"}, lambda sha: False)
    assert s.tests == ("pkg/tests/test_a.py",) and s.skipped == ()


def _git(root, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                   cwd=root, check=True,
                   capture_output=True, **no_console_creationflags())


@pytest.mark.parametrize("bad,reason", [("not json", "not valid JSON"), ("[1]", "must be a JSON object")])
def test_main_refuses_malformed_passed_at(tmp_path, capsys, bad, reason):
    rc = d.main(["--base", "HEAD", "--surface", "pkg", "--passed-at", bad, "--repo-root", str(tmp_path)])
    cap = capsys.readouterr()
    assert rc == 2
    assert cap.out == ""
    assert reason in cap.err


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_main_refuses_when_git_diff_fails(tmp_path, capsys):
    _git(tmp_path, "init", "-q")
    rc = d.main(["--base", "no-such-rev", "--surface", "pkg", "--repo-root", str(tmp_path)])
    cap = capsys.readouterr()
    assert rc == 2
    assert cap.out == ""
    assert "selection refused" in cap.err


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_main_end_to_end(tmp_path, capsys):
    (tmp_path / "pkg/tests").mkdir(parents=True)
    (tmp_path / "pkg/a.py").write_text("x = 1\n")
    (tmp_path / "pkg/tests/test_a.py").write_text("from pkg.a import x\n")
    (tmp_path / "pkg/tests/test_other.py").write_text("import os\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True, **no_console_creationflags()).stdout.strip()
    (tmp_path / "pkg/a.py").write_text("x = 2\n")
    (tmp_path / "pkg/tests/test_other.py").write_text("import sys\n")
    _git(tmp_path, "commit", "-qam", "edit")
    rc = d.main(["--base", base, "--surface", "pkg", "--repo-root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["tests"] == ["pkg/tests/test_a.py", "pkg/tests/test_other.py"]
    assert out["skipped"] == []
    assert out["edited"] == ["pkg/a.py", "pkg/tests/test_other.py"]
