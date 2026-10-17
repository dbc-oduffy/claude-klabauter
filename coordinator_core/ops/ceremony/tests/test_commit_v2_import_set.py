"""
coordinator_core.ops.ceremony.tests.test_commit_v2_import_set

Purpose: `commit_v2` does not itself pull `ops.fleet._common` (a heavy closure) at import, and a
landing without a `repo_root` param does not reach `check_repo_root`. Runs in a fresh interpreter
so `sys.modules` is the module's own closure.

Negative-spec: does not assert `ops.fleet._common` is absent after a landing -- `git.commit_trailers`
reaches it through `session.claimed_plan.list_held_plan_claims`, outside `commit_v2`. Does not claim
`authoring_leaks.import_closure` (the leak gate must run), `baton_assemble` or
`bash_guards.block_subagent_commit` (loaded through modules outside this file) are absent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[4]

_IMPORT_DRIVER = """
import json, sys
from coordinator_core.ops.ceremony import commit_v2
print(json.dumps({"modules": sorted(sys.modules)}))
"""

_LANDING_DRIVER = """
import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
def g(*a):
    subprocess.run(["git", *a], cwd=str(root), check=True, capture_output=True)
g("init", "-q"); g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
(root / "seed.md").write_text("seed\\n", encoding="utf-8"); g("add", "."); g("commit", "-q", "-m", "seed")
from coordinator_core.ops.ceremony import commit_v2
import coordinator_core.ops.fleet._common as common
calls = []
orig = common.check_repo_root
common.check_repo_root = lambda *a, **k: calls.append(a) or orig(*a, **k)
results = []
for name in ("a.md", "b.md"):
    (root / name).write_text(name + "\\n", encoding="utf-8")
    results.append(commit_v2._handler({"paths": [name], "message": "add " + name}, repo_root=root / ".git"))
print(json.dumps({"results": results, "check_repo_root_calls": len(calls)}, default=str))
"""


def _run(driver: str, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", driver, *args],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_importing_commit_v2_does_not_load_fleet_common():
    assert "coordinator_core.ops.fleet._common" not in _run(_IMPORT_DRIVER)["modules"]


def test_landing_without_repo_root_param_skips_check_repo_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _run(_LANDING_DRIVER, str(repo))
    assert all(r.get("committed") for r in out["results"]), out["results"]
    assert out["check_repo_root_calls"] == 0
