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
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
RUN = "ask-20261016T120000-aaaaaa"
OLD_RUN = "ask-20261016T100000-bbbbbb"
SIZING = "state/sizings/2026-10-16-x.yaml"
BATON = "state/handoffs/2026-10-16-x.md"
PLAN = "docs/plans/2026-10-16-x.md"
EVIDENCE = "docs/plans/2026-10-16-x.evidence.yaml"
SLICE = "state/sizings/2026-10-16-x-slice-2.yaml"
DOC = "docs/reference/r.md"
SESSION = "11111111-2222-3333-4444-555555555555"
MODES = ("completed", "halted", "refired", "abandoned")


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
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setattr(terminal_commit, "_ship_cited_sizing", lambda *a, **k: {})
    monkeypatch.setattr(terminal_commit, "_ledger_gate", lambda *a, **k: (None, None, []))
    from coordinator_core.session import scope

    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {})
    monkeypatch.setattr(scope, "own_path_claims_in_process", lambda sid, cwd=None: [DOC])
    return root


def _author_graph(root: Path, run_id: str, *, docs: bool) -> None:
    """Everything one run writes: record files, sizing naming baton and plan, sidecar, slice, doc edit."""
    _write(root, SIZING, f"status: routed\nbaton: {BATON}\nplan: {PLAN}\n")
    _write(root, BATON, "---\ncarried_items: []\n---\nbaton\n")
    _write(root, EVIDENCE, "rows: []\n")
    _write(root, SLICE, "status: draft\n")
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/ask.md", "ask\n")
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/parent.json", json.dumps({"runs": [{"slice_sizing": SLICE}]}))
    if docs:
        _write(root, DOC, "edited by the docs stage\n")


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
    }[mode], docs, dirs


def _expected(root: Path, mode: str, docs: bool, dirs: tuple[str, ...]) -> run_artifacts.RunArtifacts:
    """The run's candidate set read from the record graph before any landing runs."""
    if mode in ("completed", "refired"):
        _prepare_ask_commit(root)
    artifacts = run_artifacts.candidate_paths(root, RUN, doc_paths=[DOC] if docs else ())
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
