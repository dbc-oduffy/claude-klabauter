"""
coordinator_core.ops.ceremony.tests.test_commit_gates_import_set -- importing the commit gates, and
running them over a terminal-commit-shaped path set, does not load `git_native`.

Purpose: `git_native` is reached only by the deletion-block fallback and the CLI `main()`; every
other gate on the commit path is spawn-free. Measured against the other module-level imports, each
one is executed by a gate for this path set (`.py` product, plan, handoff), so only `git_native`
is pinned absent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

_REPO_ROOT = Path(__file__).resolve().parents[4]

_PROBE = """
import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
def git(*a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
git("init", "-q")
git("config", "user.email", "t@example.invalid")
git("config", "user.name", "t")
(root / "state/handoffs").mkdir(parents=True)
(root / "docs/plans").mkdir(parents=True)
(root / "state/handoffs/b.md").write_text("---\\ntitle: b\\n---\\n", encoding="utf-8")
(root / "docs/plans/p.md").write_text("# p\\n", encoding="utf-8")
(root / "m.py").write_text("x = 1\\n", encoding="utf-8")
git("add", "-A")
git("commit", "-q", "-m", "seed")
(root / "m.py").write_text("x = 2\\n", encoding="utf-8")
from coordinator_core.ops.ceremony import commit_v2
paths = ["m.py", "docs/plans/p.md", "state/handoffs/b.md"]
def read_source(rel):
    p = root / rel
    return p.read_bytes() if p.exists() else None
refusal = commit_v2._pre_commit_gates(root, paths, [], read_source)
print("RESULT=" + json.dumps({"refusal": refusal, "loaded": [m for m in json.loads(sys.argv[2]) if m in sys.modules]}))
"""

_ABSENT = ("coordinator_core.ops.ceremony.git_native",)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_terminal_commit_shaped_gate_run_does_not_load_git_native(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path), json.dumps(_ABSENT)],
        capture_output=True, text=True, timeout=120, cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr[-800:]
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
    got = json.loads(line[len("RESULT="):])
    assert got["refusal"] is None
    assert got["loaded"] == []


def test_public_names_stay_importable():
    from coordinator_core.ops.ceremony import commit_gates

    for name in ("GateOutcome", "deletion_block_gate", "carry_gate", "attribution_gate", "main"):
        assert hasattr(commit_gates, name)


def test_main_usage_error_still_loads_git_native_on_demand(capsys):
    from coordinator_core.ops.ceremony import commit_gates

    assert commit_gates.main([]) == 2
    assert "usage" in capsys.readouterr().err
