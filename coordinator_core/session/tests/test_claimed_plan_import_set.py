"""
coordinator_core.session.tests.test_claimed_plan_import_set

Purpose: `list_held_plan_claims` does not load `ops.fleet._common` (the heavy `coordinator_core.ops`
closure), and `_common.plan_claim_dir` is the same object as `session.claims.plan_claim_dir`.
Runs in a fresh interpreter so `sys.modules` is the call's own closure.
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

_REPO_ROOT = Path(__file__).resolve().parents[3]

_DRIVER = """
import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
claim = root / ".git" / "coordinator-sessions" / "plan-claims" / "some-plan"
claim.mkdir(parents=True)
(claim / "session_id").write_text("sid-import-set", encoding="utf-8")
(claim / "claimed_at").write_text("2026-10-17T00:00:00Z", encoding="utf-8")
from coordinator_core.session import claimed_plan
held = claimed_plan.list_held_plan_claims(root)
print(json.dumps({"held": held, "modules": sorted(sys.modules)}))
"""

_IDENTITY_DRIVER = """
import json
from coordinator_core.ops.fleet import _common
from coordinator_core.session import claims
print(json.dumps({"same": _common.plan_claim_dir is claims.plan_claim_dir}))
"""


def _run(driver: str, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", driver, *args],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT), "COORDINATOR_SESSION_ID": "sid-import-set"},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_list_held_plan_claims_does_not_load_fleet_common(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _run(_DRIVER, str(repo))
    assert [h[0] for h in out["held"]] == ["docs/plans/some-plan.md"]
    assert "coordinator_core.ops.fleet._common" not in out["modules"]


def test_common_plan_claim_dir_is_the_claims_object():
    assert _run(_IDENTITY_DRIVER)["same"] is True
