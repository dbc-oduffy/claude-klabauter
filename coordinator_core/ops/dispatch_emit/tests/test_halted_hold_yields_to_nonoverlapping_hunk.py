"""dispatch.land_run: a halted holder withholds a path only for the landing's hunks that overlap the
holder's kept hunks; a non-overlapping hunk lands and the holder's hunks stay in the tree."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release, land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import (  # noqa: F401
    CODE, DLV, DOC, MET, PLAN, REPORT, RUN, SELF, SIZING, VERDICT, _author, _git, _write, repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HOLDER_SIZING = "state/sizings/20260101-halted-holder.yaml"
HEAD_TEXT = "a = 0\nb = 0\nc = 0\n"


def _halted_holder(repo, kept_text: str) -> str:
    """A halted run, minted minutes ago, whose kept blob for CODE is `kept_text` and which withholds CODE."""
    holder = time.strftime("ask-%Y%m%dT%H%M%S", time.gmtime(time.time() - 300)) + "-977c47"
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=kept_text.encode(), capture_output=True,
        check=True, **no_console_creationflags(),
    ).stdout.decode().strip()
    rec = f"state/warp/{holder}"
    _write(repo, f"{rec}/run.json", json.dumps({"sizing_rel": HOLDER_SIZING}))
    _write(repo, f"{rec}/halted.json", json.dumps({
        "status": "halted", "sizing_rel": HOLDER_SIZING, "next_action": {"kind": "refire"},
        "run_base_sha": _git(repo, "rev-parse", "HEAD").strip(),
        "run_artifacts": {"withheld": {CODE: "halt-record-only"}},
    }))
    _write(repo, f"{rec}/kept.json", json.dumps({"chunks": {"X1": {"paths": {CODE: blob}}}, "plan_gate": None}))
    return holder


def _land(repo, worktree_text: str) -> dict:
    _author(repo, DOC, "doc\n")
    _write(repo, REPORT, "DONE: X1\n")
    _author(repo, CODE, worktree_text)
    declared = (DOC, CODE)
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=declared, report=REPORT),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), review_code_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(repo, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(repo, RUN, all_of_pm_ask=False)
    rl.bind(repo, RUN, SIZING)
    token = run_token.mint(repo, RUN, f"{RUN}.mjs", session_id=SELF)
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": [],
            "inline_review": {**reviewed(), "integration_stem": "s", "criterion": dict(MET)},
            "session_id": SELF, "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=repo / ".git",
    )


def _head_text(repo) -> str:
    return _git(repo, "show", f"HEAD:{CODE}")


def test_a_hunk_clear_of_the_halted_holders_hunks_lands_and_the_holders_stay_in_the_tree(repo):
    _halted_holder(repo, "a = 0\nb = 0\nc = 7\n")

    out = _land(repo, "a = 1\nb = 0\nc = 7\n")

    assert out["committed"] is True, out
    assert _head_text(repo) == "a = 1\nb = 0\nc = 0\n"
    assert (repo / CODE).read_text(encoding="utf-8") == "a = 1\nb = 0\nc = 7\n"
    assert "ledger_withheld_product" not in out, out


def test_a_hunk_overlapping_the_halted_holders_hunk_stays_withheld_by_the_holder(repo):
    holder = _halted_holder(repo, "a = 5\nb = 0\nc = 0\n")

    out = _land(repo, "a = 1\nb = 0\nc = 0\n")

    assert (out.get("withheld") or {}).get(CODE) == f"withheld by {holder}", out
    assert _head_text(repo) == HEAD_TEXT
    assert (repo / CODE).read_text(encoding="utf-8") == "a = 1\nb = 0\nc = 0\n"


def test_the_holders_own_hunk_alone_in_the_tree_is_not_landed(repo):
    holder = _halted_holder(repo, "a = 0\nb = 0\nc = 7\n")

    out = _land(repo, "a = 0\nb = 0\nc = 7\n")

    assert _head_text(repo) == HEAD_TEXT
    assert (out.get("withheld") or {}).get(CODE) == f"withheld by {holder}", out


def test_a_holder_with_no_nameable_kept_hunks_holds_the_path_whole(repo):
    holder = _halted_holder(repo, "a = 0\nb = 0\nc = 7\n")
    (repo / "state/warp" / holder / "kept.json").unlink()

    out = _land(repo, "a = 1\nb = 0\nc = 7\n")

    assert _head_text(repo) == HEAD_TEXT
    assert (out.get("withheld") or {}).get(CODE) == f"withheld by {holder}", out


def test_clear_hunks_are_computed_without_a_git_spawn(repo, monkeypatch):
    holder = _halted_holder(repo, "a = 0\nb = 0\nc = 7\n")
    _write(repo, CODE, "a = 1\nb = 0\nc = 7\n")

    def _boom(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    got = halted_hold_release.hunks_clear_of_holders(repo, {CODE: [holder]})

    hunks, some_overlap = got[CODE]
    assert [h.old_start for h in hunks] == [1] and some_overlap is True
