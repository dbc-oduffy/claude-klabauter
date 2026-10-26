"""After a completed, halted, refired or abandoned run, no run-authored path is left uncommitted."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

import pytest

from coordinator_core.ops.dispatch_emit import abandoned_runs, run_artifacts, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    HALT_PLAN_FIELD,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()
RUN = "ask-20261016T120000-aaaaaa"
OLD_RUN = "ask-20261016T100000-bbbbbb"
SIZING = "state/sizings/2026-10-16-x.yaml"
BATON = "state/handoffs/2026-10-16-x.md"
PLAN = "docs/plans/2026-10-16-x.md"
EVIDENCE = "docs/plans/2026-10-16-x.evidence.yaml"
SLICE = "state/sizings/2026-10-16-x-slice-2.yaml"
DOC = "docs/reference/r.md"
SESSION = "11111111-2222-3333-4444-555555555555"
DS_PLAN = "docs/plans/2026-10-16-y.md"
DS_EVIDENCE = "docs/plans/2026-10-16-y.evidence.yaml"
DS_HANDOFF = "state/handoffs/2026-10-16-y.md"
DS_DELIVERY = f"docs/delivery/{RUN}.md"
MODES = ("completed", "halted", "refired", "abandoned", "halted-docs-stage")


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, SIZING, "status: draft\n")
    _write(root, PLAN, f"---\nsizing_object: {SIZING}\n---\nbody\n")
    _write(root, DOC, "seed\n")
    _write(root, "run.mjs", "// script\n")
    _write(root, ".gitignore", f".coordinator-local/\n{RUN_DIR_ROOT}/*/submission.json\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setattr(terminal_commit, "_ship_cited_sizing", lambda *a, **k: {})
    monkeypatch.setattr(terminal_commit, "_ledger_gate", lambda *a, **k: (None, None, []))
    from coordinator_core.session import scope

    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {})
    monkeypatch.setattr(scope, "own_path_claims_in_process", lambda sid, cwd=None: [DOC])
    from coordinator_core.session import core

    ledger = Path(core.sessions_dir(str(root))) / SESSION / "dispatched-agents.txt"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("", encoding="utf-8")
    return root


def _author_graph(root: Path, run_id: str, *, docs: bool, evidence: bool = True) -> None:
    """Everything one run writes: record files, sizing naming baton and plan, sidecar, slice, doc edit."""
    _write(root, SIZING, f"status: routed\nbaton: {BATON}\nplan: {PLAN}\n")
    _write(root, BATON, "---\ncarried_items: []\n---\nbaton\n")
    if evidence:
        _write(root, EVIDENCE, "rows: []\n")
    _write(root, SLICE, "status: draft\n")
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/ask.md", "ask\n")
    _write(root, f"{RUN_DIR_ROOT}/{run_id}/submission.json", json.dumps({"submitted_at": time.time() - 600}))
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/parent.json", json.dumps({"runs": [{"slice_sizing": SLICE}]}))
    if docs:
        _write(root, DOC, "edited by the docs stage\n")


def _author_docs_stage_graph(root: Path, run_id: str) -> None:
    """What a ledger-check halt sees: the sizing names a baton but no plan; the plan, its handoff,
    evidence sidecar and delivery note were written by the docs stage and reach the halt only through
    ``halted["plan"]`` and ``doc_paths``."""
    _author_graph(root, run_id, docs=True, evidence=False)
    _write(root, SIZING, f"status: routed\nbaton: {BATON}\n")
    _write(root, DS_PLAN, f"---\nsizing_object: {SIZING}\n---\nplan\n")
    _write(root, DS_HANDOFF, "---\ncarried_items: []\n---\nhandoff\n")
    _write(root, DS_EVIDENCE, "rows: []\n")
    _write(root, DS_DELIVERY, "delivery\n")


def _write_predecessor(root: Path) -> None:
    _write(root, f"{RECORD_DIR_ROOT}/{OLD_RUN}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, f"{RECORD_DIR_ROOT}/{OLD_RUN}/ask.md", "old ask\n")


def _prepare_ask_commit(root: Path) -> None:
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    _write(root, "a.py", "a\n")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=PLAN)
    _write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker,
    )
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")


def _land_completed(root: Path) -> dict:
    _prepare_ask_commit(root)
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SESSION, "doc_paths": [DOC]},
        repo_root=root / ".git",
    )


def _land_halted(root: Path) -> dict:
    return terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, "session_id": SESSION, "doc_paths": [DOC],
         HALTED_RECORD_PARAM: {"halted": "refusal", "reason": "sizing refused"}},
        repo_root=root / ".git",
    )


def _land_halted_docs_stage(root: Path) -> dict:
    """The params ``_halt_record_commit_fn`` composes: script, run id, token, the ledger-check halt
    carrying the plan, and the docs stage's doc_paths."""
    token = run_token.mint(root, RUN, "run.mjs", session_id=SESSION)
    return terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, "run_token": token, "session_id": SESSION,
         HALTED_RECORD_PARAM: {"halted": "ledger-check", HALT_PLAN_FIELD: DS_PLAN, "items": []},
         "doc_paths": [DOC]},
        repo_root=root / ".git",
    )


def _land_abandoned(root: Path) -> dict:
    return abandoned_runs._handler({"session_id": SESSION}, repo_root=root / ".git")


def _age_record(root: Path, run_id: str) -> None:
    stamp = time.time() - run_token.TTL_SECONDS - 3600
    for f in (root / RECORD_DIR_ROOT / run_id).iterdir():
        os.utime(f, (stamp, stamp))


def _setup(mode: str, root: Path) -> tuple[Callable[[Path], dict], bool, tuple[str, ...]]:
    """Author the mode's graph; return its landing call, whether the docs stage ran, and the
    record dirs the run owns (own plus any superseded one)."""
    docs = mode != "abandoned"
    if mode == "halted-docs-stage":
        _author_docs_stage_graph(root, RUN)
    else:
        _author_graph(root, RUN, docs=docs)
    dirs = (f"{RECORD_DIR_ROOT}/{RUN}",)
    if mode == "refired":
        _write_predecessor(root)
        dirs += (f"{RECORD_DIR_ROOT}/{OLD_RUN}",)
    if mode == "abandoned":
        _age_record(root, RUN)
    return {
        "completed": _land_completed, "halted": _land_halted,
        "refired": _land_completed, "abandoned": _land_abandoned,
        "halted-docs-stage": _land_halted_docs_stage,
    }[mode], docs, dirs


def _expected(root: Path, mode: str, docs: bool, dirs: tuple[str, ...]) -> run_artifacts.RunArtifacts:
    """The run's candidate set read from the record graph before any landing runs."""
    if mode in ("completed", "refired"):
        _prepare_ask_commit(root)
    artifacts = run_artifacts.candidate_paths(
        root, RUN, plan_rel=DS_PLAN if mode == "halted-docs-stage" else None,
        doc_paths=[DOC] if docs else (),
    )
    if mode == "refired":
        artifacts = artifacts.with_superseded([OLD_RUN], RECORD_DIR_ROOT)
    return artifacts


def _leftovers(root: Path, artifacts: run_artifacts.RunArtifacts, dirs: tuple[str, ...]) -> list[str]:
    out = _git(
        ["status", "--porcelain", "--untracked-files=all", "--", *artifacts.paths(), *dirs], root
    )
    lines = [line for line in out.splitlines() if line.strip()]
    return [f"{line[3:]} [{artifacts.class_of(line[3:]) or 'record-dir'}]" for line in lines]


def _run_mode(mode: str, root: Path) -> tuple[dict, list[str], run_artifacts.RunArtifacts]:
    land, docs, dirs = _setup(mode, root)
    artifacts = _expected(root, mode, docs, dirs)
    assert artifacts.paths(), "the fixture graph must name candidate paths"
    reply = land(root)
    return reply, _leftovers(root, artifacts, dirs), artifacts


@pytest.mark.parametrize("mode", MODES)
def test_no_run_leaves_a_dirty_tree(repo, mode):
    reply, left, artifacts = _run_mode(mode, repo)

    assert reply.get("ok") is not False, reply
    assert "run_artifact_residue" not in reply, reply
    assert not left, f"{mode} run left run-authored paths uncommitted: {left}"
    expected_classes = {"sizing", "baton", "plan", "evidence-sidecar", "slice-sizing"}
    assert expected_classes <= {artifacts.class_of(p) for p in artifacts.paths()}
    if mode != "abandoned":
        assert artifacts.class_of(DOC) == "docs-stage"
    if mode == "refired":
        assert f"{RECORD_DIR_ROOT}/{OLD_RUN}/ask.md" in artifacts.by_class["superseded-record"]


@pytest.mark.parametrize("mode", MODES)
def test_the_oracle_goes_red_when_a_landing_site_ignores_run_artifacts(repo, monkeypatch, mode):
    land, docs, dirs = _setup(mode, repo)
    artifacts = _expected(repo, mode, docs, dirs)
    empty = run_artifacts.RunArtifacts(run_id=RUN, by_class={}, record_dirs=(), root=repo)
    monkeypatch.setattr(run_artifacts, "candidate_paths", lambda *a, **k: empty)

    land(repo)

    left = _leftovers(repo, artifacts, dirs)
    assert any(f"[{cls}]" in item for item in left for cls in ("evidence-sidecar", "baton")), left


def test_a_docs_stage_halt_lands_plan_handoff_and_delivery_note_and_leaves_the_tree_clean(repo):
    reply, left, artifacts = _run_mode("halted-docs-stage", repo)

    assert reply.get("committed") is True, reply
    assert "run_artifact_residue" not in reply, reply
    assert not left, left
    seed = _git(["rev-list", "--max-parents=0", "HEAD"], repo).strip()
    body = _git(["log", f"{seed}..HEAD", "--format=%B"], repo)
    assert "Lands-Artifacts:" in body, body
    landed = set(_git(["log", f"{seed}..HEAD", "--name-only", "--format="], repo).split())
    assert {DS_PLAN, DS_HANDOFF, DS_DELIVERY, DS_EVIDENCE, DOC} <= landed, landed
    assert artifacts.class_of(DS_HANDOFF) == "handoff" and artifacts.class_of(DS_DELIVERY) == "delivery-note"
    assert DS_HANDOFF != BATON
    assert _git(["status", "--porcelain", "--untracked-files=all"], repo).strip() == ""


REVIEW_FILE = "elsewhere/ask_roadmap.py"
REVIEW_MODES = tuple(m for m in MODES if m != "abandoned")
_REVIEW_AGENT = "agent-review-1"


def _author_review_edit(root: Path) -> None:
    """A reviewer edit in a directory no chunk declares, with the real per-agent touch record
    ``review_edit_source`` reads: the dispatch ledger row, owner file, and a write event after the
    run's ``submission.json`` ``submitted_at``."""
    from coordinator_core.session import core, touch_record

    _write(root, REVIEW_FILE, "# edited by review\n")
    hub = Path(core.sessions_dir(str(root)))
    agent_dir = hub / ".agents" / _REVIEW_AGENT
    agent_dir.mkdir(parents=True)
    (agent_dir / "em-session-id.txt").write_text(SESSION + "\n", encoding="utf-8")
    ledger = hub / SESSION / "dispatched-agents.txt"
    ledger.write_text(f"{_REVIEW_AGENT}\thost\tcoordinator:code-reviewer\t1\n", encoding="utf-8")
    with (agent_dir / touch_record.RECORD_FILENAME).open("ab") as fh:
        fh.write(
            touch_record.encode_line(
                session_id=SESSION, agent_id=_REVIEW_AGENT, verb=touch_record.VERB_TOUCH,
                path=REVIEW_FILE, timestamp=time.time(), kind=touch_record.KIND_WRITE,
            )
        )


@pytest.mark.parametrize("mode", REVIEW_MODES)
def test_a_review_written_file_outside_every_declared_path_is_committed_or_named(repo, mode):
    land, _docs, _dirs = _setup(mode, repo)
    if mode in ("completed", "refired"):
        _prepare_ask_commit(repo)
    _author_review_edit(repo)

    reply = land(repo)

    dirty = _git(["status", "--porcelain", "--untracked-files=all", "--", REVIEW_FILE], repo).strip()
    if mode in ("completed", "refired"):
        assert not dirty, (mode, dirty, reply)
        assert reply.get("review_residue") == [REVIEW_FILE], reply
        seed = _git(["rev-list", "--max-parents=0", "HEAD"], repo).strip()
        assert f"Review-Residue: {REVIEW_FILE}" in _git(["log", f"{seed}..HEAD", "--format=%B"], repo)
    else:
        assert reply["review_edits_unlanded"] == {REVIEW_FILE: "halted-run"}, reply
        seed = _git(["rev-list", "--max-parents=0", "HEAD"], repo).strip()
        assert REVIEW_FILE not in _git(["log", f"{seed}..HEAD", "--name-only", "--format="], repo).split(), reply
        tree_dirty = {
            line[3:] for line in _git(["status", "--porcelain", "--untracked-files=all"], repo).splitlines() if line
        }
        assert tree_dirty == {REVIEW_FILE}, (tree_dirty, reply)


def test_a_review_write_stays_unclaimed_while_another_run_holds_the_session(repo):
    _setup("completed", repo)
    _prepare_ask_commit(repo)
    _author_review_edit(repo)
    run_token.mint(repo, OLD_RUN, "other.mjs", session_id=SESSION)

    reply = _land_completed(repo)

    assert "review_residue" not in reply, reply
    assert _git(["status", "--porcelain", "--untracked-files=all", "--", REVIEW_FILE], repo).strip()


def test_the_review_edit_oracle_goes_red_when_the_source_cannot_see_the_touch_record(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_edits

    _setup("completed", repo)
    _prepare_ask_commit(repo)
    _author_review_edit(repo)
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {})

    _land_completed(repo)

    assert _git(["status", "--porcelain", "--untracked-files=all", "--", REVIEW_FILE], repo).strip()


def test_the_composed_halt_call_still_carries_the_plan_and_doc_paths(monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_compose
    from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

    blitz = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = ask_compose.compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (blitz, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **{**REVIEW_KW, "run_base_sha": "a" * 40},
    )

    assert f"{HALT_PLAN_FIELD}: _planRel" in script
    fn = script[script.index("async function _commitHaltRecord()"):].split("\n", 1)[0]
    assert f"{HALTED_RECORD_PARAM}: _halted" in fn and "doc_paths:" in fn
