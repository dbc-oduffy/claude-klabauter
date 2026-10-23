"""dispatch.land_run: the run's own record-named paths are re-read at commit, product drift still refuses."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import run_artifact_ownership
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import (  # noqa: F401
    PLAN, RUN, _author, _declare, _git, _land, _record_dir, _tree, _write, repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OWN = f"{RECORD_DIR_ROOT}/{RUN}/ask.md"


def test_own_record_path_rewritten_after_session_write_is_reread_and_lands(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py", OWN)
    _record_dir(repo)
    _author(repo, OWN, "ask v1\n")
    _write(repo, OWN, "ask v2, rewritten by the run's own stage\n")
    assert run_artifact_ownership.own_artifact_paths(repo, RUN).covers(OWN)

    out = _land(repo)

    assert out["committed"] is True, out
    assert OWN in out["run_artifacts"]["reread"]
    assert OWN in _tree(repo)
    assert "ask v2" in _git(["show", f"HEAD:{OWN}"], repo)


def test_product_path_drift_still_refuses_beside_a_reread_own_path(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py", OWN)
    _record_dir(repo)
    _author(repo, OWN, "ask v1\n")
    _write(repo, OWN, "ask v2\n")
    _write(repo, "src/a.py", "a = 2  # edited after the session's last recorded write\n")
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _land(repo)

    assert out["committed"] is False and out["refused"] == "blob-drift", out
    assert [d["path"] for d in out["blob_drift"]] == ["src/a.py"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
