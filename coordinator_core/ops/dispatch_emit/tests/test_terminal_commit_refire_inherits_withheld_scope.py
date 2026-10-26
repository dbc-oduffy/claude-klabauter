"""A predecessor run's withheld path is not pre-existing dirt for a refire; the same dirt unwithheld still refuses."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, ManifestRow, StageManifest
from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE, capture, fingerprint

SIZING = "state/sizings/2026-10-25-inherit.yaml"
PLAN = "docs/plans/inherit.md"
DIRTY = "pkg/carried.py"


def _put(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _setup(root, withheld_by_predecessor):
    _put(root, SIZING, "status: routed\n")
    _put(root, PLAN, f"---\nsizing_object: {SIZING}\n---\n# plan\n")
    _put(root, DIRTY, "# carried\n")
    if withheld_by_predecessor:
        halted = {"status": "halted", "sizing_rel": SIZING, "run_artifacts": {"withheld": {DIRTY: "abc"}}}
        _put(root, f"{RECORD_DIR_ROOT}/ask-20260101T000000-bbbbbb/halted.json", json.dumps(halted))
        _put(
            root, f"{RECORD_DIR_ROOT}/ask-20260101T000000-bbbbbb/{WITHHELD_PRINTS_FILE}",
            json.dumps({DIRTY: fingerprint(root, DIRTY)}),
        )
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b.md", writes=(DIRTY,), wave=0, register_rows=("R1",)
    )
    manifest = StageManifest(
        run_dir="run", rows=(row,), review_declared_paths=(), marker_path="m",
        write_baseline=capture(root, [DIRTY]),
    )
    run_dir = root / RECORD_DIR_ROOT / "ask-20260101T000000-aaaaaa"
    run_dir.mkdir(parents=True, exist_ok=True)
    return manifest, run_dir


def _call(root, manifest, run_dir):
    verdict = {"items": [{"id": "R1", "status": "met"}]}
    ledger = SimpleNamespace(items=["R1"], all_of_pm_ask=False)
    return terminal_commit._pre_existing_dirt_refusal(root, manifest, verdict, ledger, PLAN, run_dir)


def test_path_withheld_by_predecessor_proceeds(tmp_path):
    manifest, run_dir = _setup(tmp_path, True)
    assert _call(tmp_path, manifest, run_dir) is None


def test_same_dirt_not_withheld_by_predecessor_refuses(tmp_path):
    manifest, run_dir = _setup(tmp_path, False)
    refusal = _call(tmp_path, manifest, run_dir)
    assert refusal is not None
    assert refusal["refused"] == "ledger-unmet"
    assert DIRTY in refusal["ledger_failures"][0]["reason"]


NOTE = "## What shipped\n\n- `pkg/carried.py`\n- `pkg/other.py`\n"


def _shipped(root, withheld):
    from coordinator_core.ops.dispatch_emit.shipped_claims import shipped_gate

    return shipped_gate(root, NOTE, landing_paths=["pkg/carried.py", "pkg/other.py"], withheld_paths=withheld)[0]


def test_shipped_gate_admits_inherited_withheld_path(tmp_path):
    withheld = {DIRTY: "withhold_paths: abc", "pkg/other.py": "withhold_paths: z"}
    inherited = {DIRTY: "predecessor-withheld"}
    assert _shipped(tmp_path, withheld) is not None
    kept = terminal_commit._drop_inherited_withheld(tmp_path, withheld, inherited, "run-a", PLAN)
    assert DIRTY not in kept and "pkg/other.py" in kept
    assert _shipped(tmp_path, kept) is not None  # a non-inherited withheld claim still refuses


def test_shipped_gate_admits_reviewer_applied_and_completed_row_path(tmp_path):
    withheld = {DIRTY: "withhold_paths: abc"}
    kept = terminal_commit._drop_inherited_withheld(
        tmp_path, withheld, {DIRTY: "reviewer-applied"}, "run-a", PLAN
    )
    assert kept == {}
    assert _shipped(tmp_path, kept) is None


def test_foreign_held_inherited_path_stays_withheld(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_commit, "foreign_halted_withheld", lambda *a, **k: {DIRTY: "other-run"})
    kept = terminal_commit._drop_inherited_withheld(
        tmp_path, {DIRTY: "w"}, {DIRTY: "predecessor-withheld"}, "run-a", PLAN
    )
    assert kept == {DIRTY: "w"}


# Through the real `terminal_commit._handler`: an inherited path rides in `all_paths`, so it lands.
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests import test_ledger_halt_commits_delivered_rows as _fx  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import repo  # noqa: E402,F401

_PRED = "ask-20260101T000000-bbbbbb"
_INHERITED = "m.py"
_SIB_SID = "22222222-3333-4444-5555-666666666666"


def _refire(root, withheld: dict, live_sibling: str = ""):
    _fx._write(root, _INHERITED, "def m():\n    return 1\n")
    _fx._git(root, "add", _INHERITED)
    _fx._git(root, "commit", "-q", "-m", "m at head")
    _fx._write(root, _INHERITED, "def m():\n    return 2\n")
    _fx._write(root, ".gitignore", ".coordinator-local/\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), deliverable_id=_fx.DLV, plan_path=_fx.PLAN,
    )
    marker = f"{RECORD_DIR_ROOT}/{_fx.RUN}/commit-request.txt"
    _fx._write(root, marker, render_marker(request) + "\n")
    _fx._write(root, f"{RECORD_DIR_ROOT}/{_PRED}/halted.json", json.dumps({
        "status": "halted", "sizing_rel": _fx.SIZING, "run_artifacts": {"withheld": withheld},
    }))
    if live_sibling:
        from coordinator_core.ops.dispatch_emit import run_token
        from coordinator_core.session import core as session_core

        session_core.init(_SIB_SID, cwd=str(root))
        _fx._write(root, f"{RECORD_DIR_ROOT}/{live_sibling}/manifest.json", json.dumps(
            {"rows": [{"id": "C1", "writes": [_INHERITED]}], "doc_writes": []}))
        run_token.mint(root, live_sibling, "b.mjs", session_id=_SIB_SID)
        own = run_token._record_path(root, _fx.RUN, claimed=True)  # the refire is senior: peer-claim skips the junior sibling
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text(json.dumps({"run_id": _fx.RUN, "minted_at": 1.0}), encoding="utf-8")
    params = {
        "script_path": "run.mjs", "incomplete_chunks": ["C9"], "inline_review": _fx._REVIEWED,
        "session_id": _fx._SID, "ledger_verdict": _fx._verdict({"essential": False}), "run_id": _fx.RUN,
        "resume_ledger": {"run_id": _PRED, "sizing": _fx.SIZING},
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_terminal_commit_lands_the_path_a_halted_predecessor_withheld(repo):  # noqa: F811
    out = _refire(repo, {_INHERITED: "halt-record-only"})

    assert out.get("committed") is True, out
    assert _INHERITED in _fx._git(repo, "show", "--name-only", "--format=", out["sha"]).split()


def test_terminal_commit_leaves_the_same_dirt_alone_without_a_predecessor_withhold(repo):  # noqa: F811
    out = _refire(repo, {})

    assert _INHERITED not in (_fx._git(repo, "show", "--name-only", "--format=", out["sha"]).split() if out.get("sha") else [])


def test_terminal_commit_withholds_an_inherited_path_a_live_sibling_claims(repo):  # noqa: F811
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y%m%dT%H%M%S")
    sibling = f"ask-{stamp}-cccccc"
    out = _refire(repo, {_INHERITED: "halt-record-only"}, live_sibling=sibling)

    landed = _fx._git(repo, "show", "--name-only", "--format=", out["sha"]).split() if out.get("sha") else []
    assert _INHERITED not in landed, out
    assert out.get("sibling_withheld", {}).get(_INHERITED) == sibling, out
