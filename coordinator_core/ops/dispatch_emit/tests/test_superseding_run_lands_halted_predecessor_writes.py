"""Run B superseding halted run A of the same sizing: A's dirty writes are B's baseline, not
pre-existing dirt. A halts through ``dispatch.land_run``'s halted mode, B through the real terminal
commit; a path no run of the sizing wrote, or edited after the halt, is still dirt."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_check, land_run, terminal_commit, write_baseline
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
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
    SIZING,
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
PRED_RUN = "ask-20261028T110000-aaaaaa"
PATHS = ("a.py", "b.py")
FOREIGN = "foreign.py"


@pytest.fixture(autouse=True)
def _wave_sidecar(repo):  # noqa: F811
    (repo.parent / "wave-review.md").write_text("# review\n", encoding="utf-8")


def _halt_predecessor(root, monkeypatch):
    """Run A wrote ``PATHS`` and halted: the halt record lands, A's product stays dirty, withheld."""
    for p in PATHS:
        _write(root, p, "x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "v1")
    for p in PATHS:
        _write(root, p, "x = 2\n")
    _write(root, "reports/C1.md", "DONE: C1\n")
    pred = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=PATHS, report="reports/C1.md"),))
    _write(root, "pred.mjs", render_marker(pred) + "\n")
    _write(root, f"{RECORD_DIR_ROOT}/{PRED_RUN}/run.json", json.dumps({"sizing_rel": SIZING}))
    monkeypatch.setattr(halt_tree_check, "broken_paths", lambda *_a, **_k: {})
    out = land_run._handler(
        {"script_path": "pred.mjs", "run_id": PRED_RUN, HALTED_RECORD_PARAM: {"halted": "ledger-check", "reason": "R1 open"}},
        repo_root=root / ".git",
    )
    assert out.get("withheld", {}).keys() >= set(PATHS), out


def _stage_b(root, claimed):
    """Run B's stage: write baseline captured with the tree as it stands, ``claimed`` declared for R1."""
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b.md", writes=claimed, wave=0, register_rows=("R1",)
    )
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(row,), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
        gated=(GatedRow(id="C9", reason="external_gate", gate="other repo owns it", owner_repo="elsewhere"),),
        write_baseline=write_baseline.capture(root, claimed),
    )
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=claimed),), deliverable_id=DLV, plan_path=PLAN)
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")


def _foreign_dirt(root):
    _write(root, FOREIGN, "y = 1\n")
    _git(root, "add", FOREIGN)
    _git(root, "commit", "-q", "-m", "foreign v1")
    _write(root, FOREIGN, "y = 2\n")


def _dirt(out) -> str:
    assert out["refused"] == "ledger-unmet", out
    reason = out["ledger_failures"][0]["reason"]
    assert reason.startswith("pre-existing-dirt: "), reason
    return reason[len("pre-existing-dirt: "):]


def _call_b(root):
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": {**ZERO_SLICE, "tests": {"edited": ["a.py"]}},
        "session_id": _SID, "ledger_verdict": _verdict({"essential": False}), "run_id": RUN,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_predecessor_writes_pass_the_ledger_gate_and_foreign_dirt_beside_them_is_refused(repo, monkeypatch):  # noqa: F811
    _halt_predecessor(repo, monkeypatch)
    _foreign_dirt(repo)
    _stage_b(repo, (*PATHS, FOREIGN))

    assert _dirt(_call_b(repo)) == FOREIGN


def test_predecessor_writes_alone_are_no_pre_existing_dirt(repo, monkeypatch):  # noqa: F811
    _halt_predecessor(repo, monkeypatch)
    _stage_b(repo, PATHS)

    out = _call_b(repo)

    assert "pre-existing-dirt" not in json.dumps(out, default=str), out


def test_a_predecessor_path_edited_after_the_halt_is_dirt(repo, monkeypatch):  # noqa: F811
    _halt_predecessor(repo, monkeypatch)
    _write(repo, "a.py", "x = 3\n")
    _stage_b(repo, PATHS)

    assert _dirt(_call_b(repo)) == "a.py"


def test_dirt_with_no_halted_predecessor_of_the_sizing_is_refused(repo):  # noqa: F811
    for p in PATHS:
        _write(repo, p, "x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "v1")
    for p in PATHS:
        _write(repo, p, "x = 2\n")
    _stage_b(repo, PATHS)

    assert _dirt(_call_b(repo)) == "a.py, b.py"
