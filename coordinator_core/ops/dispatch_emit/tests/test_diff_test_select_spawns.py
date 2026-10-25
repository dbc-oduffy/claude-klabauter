"""diff_test_select `main()` against real tmp_path git repos."""
from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import diff_test_select as d
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                   cwd=root, check=True,
                   capture_output=True, **no_console_creationflags())


def test_main_refuses_when_git_diff_fails(tmp_path, capsys):
    _git(tmp_path, "init", "-q")
    rc = d.main(["--base", "no-such-rev", "--surface", "pkg", "--repo-root", str(tmp_path)])
    cap = capsys.readouterr()
    assert rc == 2
    assert cap.out == ""
    assert "selection refused" in cap.err


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
