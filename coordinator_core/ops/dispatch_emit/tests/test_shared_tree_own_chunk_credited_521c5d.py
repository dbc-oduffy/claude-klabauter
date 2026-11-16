"""Replay of ask-20261116T150948-521c5d: a halted commit refused no-credited-chunk-landed for a chunk the run wrote.

recovery.json recorded C1 `blocked` and its `remnant` pass `blocked` with `paths` naming only the pass's own
sidecar note and scratch capture (`remnant wrote outside the footprint: ...`). A blocked remnant kept the
chunk stranded, so no chunk credited and the run's three written files stayed uncommitted. The fix reads a
write under a process-output prefix as the pass's own notes, not a breach; any other outside write, a
blocked pass with no recorded paths, or an open item still bars the credit, and a peer's dirt in the shared
tree stays out of the commit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import credit_gate, land_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c1"
RUN = "ask-20261116T150948-521c5d"
SCRIPT = "run-a.mjs"
PLAN = "docs/plans/p.md"
FILES = ("src/land_run.py", "src/terminal_commit.py", "src/ask_compose.py")
SIDECAR = f".coordinator-local/workflow-sidecars/ask/{RUN}/remnant-complete/verification.md"
SCRATCH = f"scratch/remnant-complete/{RUN}/scoped-tests.txt"
BLOCKED_BY_SIDECARS = {
    "field": "remnant", "status": "blocked", "still_open": [], "footprint": [*FILES, "docs/delivery/d.md"],
    "paths": [SIDECAR, SCRATCH],
    "last_reason": f"remnant wrote outside the footprint: {SIDECAR}, {SCRATCH}",
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _write(root, "peer.py", "peer = 0\n")
    _write(root, PLAN, "---\ntitle: p\n---\nbody\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))
    return root


def _author(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
    session_scope.touch(SELF, rel, cwd=str(root))
    sink = touch_record.sink_path(session_dir_for(root / ".git", SELF))
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


def _run(root: Path, remnant: dict | None) -> None:
    """C1 declares three files, recovery.json marks it blocked, beside ``remnant``."""
    report = ".coordinator-local/subagent-share/dispatch-reports/x/C1.md"
    _write(root, report, "status: DONE\n\nledger-fix landed.\n")
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=FILES, report=report),), plan_path=PLAN)
    _write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker)
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")
    chunk = {"rows": ["R1"], "attempts": 1, "status": "blocked", "paths": [SIDECAR]}
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/recovery.json", json.dumps({
        "cap": 2, "chunks": {"C1": chunk}, **({"remnant": remnant} if remnant is not None else {}),
    }))
    for rel in FILES:
        _author(root, rel, f"# {rel}\nx = 1\n")
    _write(root, "peer.py", "peer = 1\n")


def _land(root: Path) -> dict:
    return land_run._handler({
        "script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(reviewed()),
        "session_id": SELF, "run_id": RUN, "recovery_withheld": ["C1"],
    }, repo_root=root / ".git")


def _landed(root: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def _dirty(root: Path) -> set:
    return {ln[3:] for ln in _git(["status", "--porcelain", "-uall"], root).splitlines()}


def test_a_remnant_blocked_only_by_its_own_sidecar_and_scratch_writes_credits_the_chunk(repo):
    _run(repo, BLOCKED_BY_SIDECARS)

    out = _land(repo)

    assert out.get("refused") != credit_gate.REFUSED_NO_CREDITED_LANDING, out
    assert out["committed"] is True, out
    assert set(FILES) <= _landed(repo)


def test_a_peers_dirt_in_the_shared_tree_stays_out_of_the_credited_commit(repo):
    _run(repo, BLOCKED_BY_SIDECARS)

    out = _land(repo)

    assert out["committed"] is True, out
    assert "peer.py" not in _landed(repo)
    assert "peer.py" in _dirty(repo)


@pytest.mark.parametrize("remnant", [
    {**BLOCKED_BY_SIDECARS, "paths": [SIDECAR, "src/other.py"]},
    {**BLOCKED_BY_SIDECARS, "paths": []},
    {**BLOCKED_BY_SIDECARS, "last_reason": "remnant leg returned no result"},
    {**BLOCKED_BY_SIDECARS, "still_open": [{"id": "R1"}]},
    None,
])
def test_a_blocked_remnant_with_a_real_breach_or_no_pass_keeps_the_chunk_stranded(repo, remnant):
    _run(repo, remnant)

    out = _land(repo)

    assert out["committed"] is False and out["refused"] == credit_gate.REFUSED_NO_CREDITED_LANDING, out
    assert set(FILES) <= _dirty(repo)


def _review(delivery, integration="absent"):
    out = {"prep": {"slice_files": ["a.py"]}, "delivery": delivery}
    if integration != "absent":
        out["integration"] = integration
    return out


def test_a_clean_integration_leg_with_a_pass_delivery_credits_and_a_missing_one_credits_nothing():
    passed = {"verdict": "PASS"}
    assert land_run._reviewed_files(_review(passed), need_pass=True) is None
    assert land_run._reviewed_files(_review(passed, None), need_pass=True) is None
    assert land_run._reviewed_files(_review(passed, {}), need_pass=True) == {"a.py"}
    assert land_run._reviewed_files(_review({"verdict": "FAIL"}), need_pass=True) is None
    assert land_run._reviewed_files(_review(None), need_pass=True) is None
    assert land_run._reviewed_files(_review(passed, {"unresolved": ["f1"]}), need_pass=True) is None


def _grade_delivered(root: Path, review: dict) -> dict:
    (root / "r.md").write_text("DONE: C1\n", encoding="utf-8")
    request = CommitRequest(plan_path="p.md", chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",), report="r.md"),))
    return land_run._grade(
        root, request, {"C1"}, review, {}, {"items": [{"id": "C1", "status": "met"}]}, set(),
        None, None, None, None, {},
    )


def test_a_done_delivered_incomplete_chunk_is_regraded_by_a_passed_ledger_and_clean_review(tmp_path):
    graded = _grade_delivered(tmp_path, _review({"verdict": "PASS"}, {}))
    assert set(graded) == {"C1"} and graded["C1"].startswith("ledger-met")


def test_a_done_delivered_chunk_stays_incomplete_when_the_review_does_not_cover_its_files(tmp_path):
    review = _review({"verdict": "PASS"}, {})
    review["prep"]["slice_files"] = ["other.py"]
    assert _grade_delivered(tmp_path, review) == {}


def test_a_siblings_delivery_note_is_run_owned_and_a_stranger_notes_is_not(tmp_path):
    sibling = "ask-20261116T000000-aaaaaa"
    own = land_run._run_owned(tmp_path, RUN, sibling)
    assert own.covers(f"docs/delivery/{sibling}.md")
    assert own.covers(f"docs/delivery/2026-11/{sibling}.md")
    assert not own.covers("docs/delivery/ask-20261116T000000-bbbbbb.md")
    assert not land_run._run_owned(tmp_path, RUN).covers(f"docs/delivery/{sibling}.md")
