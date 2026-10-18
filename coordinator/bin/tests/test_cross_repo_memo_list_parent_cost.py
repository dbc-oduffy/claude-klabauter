"""Cost pin: the cross-repo-memo CLI's `list` path makes no git spawn and leaves
the modules only other verbs use unloaded."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "cross-repo-memo.py"

_PROBE = textwrap.dedent(
    """
    import runpy, subprocess, sys
    sys.path.insert(0, {bin_dir!r})
    spawns = []
    orig = subprocess.Popen.__init__
    def spy(self, *a, **k):
        spawns.append(a[0] if a else k.get("args"))
        return orig(self, *a, **k)
    subprocess.Popen.__init__ = spy
    ns = runpy.run_path({cli!r}, run_name="probe")
    import lib
    import cc_invoke
    cc_invoke.route_mutation = lambda *a, **k: {{"candidates": []}}
    rc = ns["main"](["list"])
    git = [s for s in spawns if "git" in str(s[0]).lower()]
    print("RC", rc, "GIT", len(git), "SPAWNS", len(spawns))
    print("LOADED", sorted(m for m in ("hashlib",) if m in sys.modules))
    """
)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_list_makes_no_git_spawn_and_defers_unused_imports(tmp_path):
    (tmp_path / ".git").mkdir()
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE.format(bin_dir=str(CLI.parent), cli=str(CLI))],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = proc.stdout
    assert "RC 0 GIT 0 SPAWNS 0" in out, (out, proc.stderr)
    assert "LOADED []" in out, out
