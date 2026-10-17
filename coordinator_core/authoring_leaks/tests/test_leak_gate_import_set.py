"""Pins the import set of `leak_gate` on a terminal-commit-shaped path set.

Probe result: with `commit_gates` already loaded (it owns `GateOutcome`), the three wired
detectors add only themselves and `coordinator_core.pyimports`; nothing else is thinnable.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks import import_closure, leak_gate
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PROBE = """
import json, sys
from pathlib import Path
import coordinator_core.ops.ceremony.commit_gates
before = set(sys.modules)
from coordinator_core.authoring_leaks import leak_gate
out = leak_gate(Path(sys.argv[1]), ["docs/plans/x.md", "state/handoffs/y.md"])
added = sorted(set(sys.modules) - before)
print(json.dumps({"passed": out.passed, "added": added}))
"""

_ABSENT = (
    "coordinator_core.authoring_leaks.ts_import_closure",
    "coordinator_core.percolate",
    "coordinator_core.percolate.engine",
    "coordinator_core.percolate.store",
    "tokenize",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


def test_terminal_commit_shaped_paths_load_only_wired_detectors(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / "docs/plans/x.md").write_text("plan\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path)],
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["passed"]
    added = set(result["added"])
    assert not [m for m in _ABSENT if m in added], sorted(added)


def test_import_closure_still_fires_on_matching_suffix(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg/b.py").write_text("def old_fn():\n    pass\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "pkg/b.py").write_text("def old_fn():\n    pass\n\ndef new_fn():\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg/a.py").write_text("from pkg.b import new_fn\n", encoding="utf-8")
    out = leak_gate(tmp_path, ["pkg/a.py"], detectors=[import_closure])
    assert not out.passed
