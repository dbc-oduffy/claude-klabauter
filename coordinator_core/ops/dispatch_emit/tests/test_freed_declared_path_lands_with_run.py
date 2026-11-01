"""A halted spine-only sibling does not claim a path the landing run declares: the claim read that
freezes the commit's withheld set receives the run's declared writes, so the freed path lands with its test."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import sibling_claims, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_sibling_run_claims import (  # noqa: F401
    SELF,
    _committed,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SRC = "src/a.py"
TEST = "other/own.py"


def _halted_spine_only(repo: Path, path: str, withheld: tuple = ()) -> str:
    stamp = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_id = f"ask-{stamp}-f54544"
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/sp.yaml"}), encoding="utf-8")
    marker: dict = {"status": "halted"}
    if withheld:
        marker["run_artifacts"] = {"withheld": {p: "left" for p in withheld}}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    (repo / "state/sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state/sizings/sp.yaml").write_text('plan: "docs/plans/sp.md"\n', encoding="utf-8")
    (repo / "docs/plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs/plans/sp.md").write_text(
        "# P\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + path + "\n```\n", encoding="utf-8",
    )
    return run_id


def _land(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(SRC, TEST)),))
    (repo / "run-a.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run-a.mjs", "incomplete_chunks": [], "inline_review": reviewed(), "session_id": SELF},
        repo_root=repo / ".git",
    )


def test_a_freed_declared_path_lands_with_its_test(repo):
    _halted_spine_only(repo, SRC)
    out = _land(repo)

    assert out["committed"] is True, out
    assert {SRC, TEST} <= set(_committed(repo))
    assert _git(["status", "--porcelain", "--", SRC, TEST], repo).strip() == ""


def test_every_claim_read_of_the_commit_carries_the_runs_declared_writes(repo, monkeypatch):
    """The claim read that freezes the withheld set must see the declared writes: without them the
    spine-only halted sibling's dirty-declared claim on a run-declared path stands (and, unfreed,
    keeps the path out of the artifact/residue accounting)."""
    _halted_spine_only(repo, SRC)
    real = sibling_claims.sibling_run_claims
    seen: list = []

    def spy(root, own_run_id, candidates, **kw):
        seen.append(set(kw.get("own_declared") or ()))
        return real(root, own_run_id, candidates, **kw)

    monkeypatch.setattr(sibling_claims, "sibling_run_claims", spy)
    _land(repo)

    assert seen and all({SRC, TEST} <= s for s in seen), seen


def test_a_path_the_halted_sibling_still_withholds_is_claimed_for_it(repo):
    run_id = _halted_spine_only(repo, SRC, withheld=(SRC,))

    claims = sibling_claims.sibling_run_claims(repo, "run-r", [SRC], own_declared=[SRC])

    assert claims == {SRC: run_id}
