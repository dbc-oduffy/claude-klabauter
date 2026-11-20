"""dispatch.land_run: a land that commits only a subset of a subject-named row's declared hunks reports the
row's still-dirty declared paths in ``rows_unlanded``; the commit-stage halt reads that reply as
``rows-unlanded`` (never ``committed``) and writes ``halted: commit_stage`` naming both paths; the reply's ledger
verdict marks no item met from the worktree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import commit_stage_halt, halted_run, land_run, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_subject_rows_declared_writes_land import (
    DLV,
    MET,
    PLAN,
    RUN,
    SELF,
    SIZING,
    VERDICT,
    _author,
    _git,
    _write,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

MOD = "coordinator_core/mod.py"
MOD_TEST = "coordinator_core/tests/test_mod.py"
LINES = [f"x{i} = {i}\n" for i in range(30)]


def _edit(lines: list, index: int) -> str:
    return "".join(f"{line.strip()}  # edited\n" if i == index else line for i, line in enumerate(lines))


def _land_one_hunk_of_two(root: Path, monkeypatch) -> dict:
    """C1 declares MOD and MOD_TEST. The run edits MOD at line 2 and writes MOD_TEST; the commit carries those.
    The second MOD hunk (line 28) and a further MOD_TEST edit arrive after the commit, so the post-commit read finds
    both declared paths dirty."""
    _write(root, MOD, "".join(LINES))
    _write(root, MOD_TEST, "seed = 0\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed mod")
    head_before = _git(root, "rev-parse", "HEAD").strip()

    _write(root, "reports/C1.md", "DONE: C1\n")
    _author(root, MOD, _edit(LINES, 2))
    _author(root, MOD_TEST, "seed = 1\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=(MOD, MOD_TEST), report="reports/C1.md"),),
        deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), review_code_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    token = run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)

    real = land_run._dirty_among
    state = {"done": False}

    def after_commit(root_arg, paths):
        if not state["done"] and _git(root, "rev-parse", "HEAD").strip() != head_before:
            state["done"] = True
            lines = LINES[:]
            lines[2] = _edit(LINES, 2).splitlines(keepends=True)[2]
            lines[28] = f"{lines[28].strip()}  # second hunk\n"
            _write(root, MOD, "".join(lines))
            _write(root, MOD_TEST, "seed = 2\n")
        return real(root_arg, paths)

    monkeypatch.setattr(land_run, "_dirty_among", after_commit)
    monkeypatch.setattr(land_run, "_new_red_gate", lambda *a, **k: {})
    out = land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": [],
            "inline_review": {**reviewed(), "integration_stem": "s", "criterion": dict(MET)},
            "session_id": SELF, "run_id": RUN, "run_token": token,
            "ledger_verdict": {"items": [{**VERDICT["items"][0], "claimed_by": ["C1"]}]},
        },
        repo_root=root / ".git",
    )
    assert state["done"], out
    return out


def _commit_leg_journal(tmp_path: Path, reply: dict) -> str:
    run_dir = tmp_path / "runs" / "wf_subset"
    run_dir.mkdir(parents=True, exist_ok=True)
    journal = run_dir / "journal.jsonl"
    events = [
        {"type": "started", "agentId": "a1", "label": "commit", "key": "k1"},
        {"type": "result", "agentId": "a1", "key": "k1", "result": reply},
    ]
    journal.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return str(journal)


def test_a_subset_hunk_land_reports_both_dirty_paths_halts_the_run_and_credits_no_met_item(repo, monkeypatch, tmp_path):
    out = _land_one_hunk_of_two(repo, monkeypatch)

    assert out["committed"] is True, out
    assert MOD in _git(repo, "show", "--name-only", "--format=", "HEAD").split(), out
    assert "second hunk" not in _git(repo, "show", f"HEAD:{MOD}"), "the commit carries only the first hunk"
    assert out["rows_unlanded"] == {"C1": sorted([MOD, MOD_TEST])}, out
    assert out["ok"] is False, out

    # The ledger verdict marks no item met from the worktree.
    assert out.get("ledger_met") is None, out
    assert set(out["ledger_partial"]) == {"R1"}, out
    assert out["criterion_status"] == "not_met", out

    # The commit leg's reply is rows-unlanded, never committed.
    journal = _commit_leg_journal(tmp_path, out)
    label, state, reason = commit_stage_halt.commit_leg_outcome(Path(journal).read_text(encoding="utf-8"))
    assert state == "rows-unlanded" and state != "committed", (state, reason)
    assert MOD in reason and MOD_TEST in reason, reason

    # A completed task still halts at commit_stage, naming both paths.
    record_dir = repo / RUN_DIR_ROOT / RUN
    marker_path = commit_stage_halt.halt_run_on_commit_stage_end(
        record_dir, journal, terminal_status="completed", sizing_rel=SIZING, launched_at=1.0,
    )
    assert marker_path is not None
    marker = halted_run.read_strict(record_dir)
    assert marker["halted"] == commit_stage_halt.HALT_COMMIT_STAGE, marker
    assert MOD in marker["reason"] and MOD_TEST in marker["reason"], marker
