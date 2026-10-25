"""dispatch.terminal_commit with inline params on a run whose executor wrote nothing refuses
``no-change`` naming the paths, never ``unreviewed``; a written path lands; the ledger refuses a
``met`` item resting on unwritten paths unless the row adopts stranded dirt."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit, write_baseline
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    GatedRow,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _SID,
    DLV,
    PLAN,
    RUN,
    _git,
    _verdict,
    _write,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ZERO_SLICE = {
    "integration_stem": "rev-stem", "slices": 0, "fixes": 0,
    "wave_reviews": [{"agent_type": "coordinator:reviewer", "verdict": "PASS", "sidecar_path": "../wave-review.md"}],
}
PATHS = ("a.py", "b.py")


@pytest.fixture(autouse=True)
def _wave_sidecar(repo):
    (repo.parent / "wave-review.md").write_text("# review\n", encoding="utf-8")


def _stage(root, *, adopts=False):
    for p in PATHS:
        _write(root, p, "x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "v1")
    for p in PATHS:
        _write(root, p, "x = 2\n")
    baseline = {} if adopts else write_baseline.capture(root, PATHS)
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b.md", writes=PATHS, wave=0,
        register_rows=("R1",), adopts_stranded=adopts,
    )
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(row,), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
        gated=(GatedRow(id="C9", reason="external_gate", gate="other repo owns it", owner_repo="elsewhere"),),
        write_baseline=baseline or ({"unrelated.txt": None} if adopts else {}),
    )
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=PATHS),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")


def _call(root, review, verdict=None):
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": review,
        "session_id": _SID, "ledger_verdict": verdict or _verdict({"essential": False}),
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_a_run_that_wrote_nothing_refuses_no_change_naming_both_paths(repo):  # noqa: F811
    _stage(repo)
    out = _call(repo, None)
    assert out["committed"] is False
    assert out["refused"] == write_baseline.NO_CHANGE_REFUSAL
    assert out[write_baseline.MEANT_TO_LAND_KEY] == list(PATHS)
    assert out[write_baseline.DIRTY_BEFORE_RUN_KEY] == list(PATHS)
    assert "unreviewed" not in json.dumps(out)


def test_an_edited_path_lands(repo):  # noqa: F811
    _stage(repo)
    _write(repo, "a.py", "x = 3\n")
    out = _call(repo, ZERO_SLICE)
    assert out["committed"] is True, out
    assert "a.py" in _git(repo, "show", "--name-only", "--format=", out["sha"]).split()


def test_a_met_item_on_unwritten_paths_refuses_pre_existing_dirt(repo):  # noqa: F811
    _stage(repo)
    review = {**ZERO_SLICE, "tests": {"edited": ["a.py"]}}
    out = _call(repo, review)
    assert out["refused"] == "ledger-unmet"
    failure = out["ledger_failures"][0]
    assert failure["id"] == "R1" and failure["reason"].startswith("pre-existing-dirt")
    assert "a.py" in failure["reason"] and "b.py" in failure["reason"]


def test_an_adopting_row_is_accepted(repo):  # noqa: F811
    _stage(repo, adopts=True)
    out = _call(repo, ZERO_SLICE)
    assert out.get("refused") is None, out
    assert out["committed"] is True, out
