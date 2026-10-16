"""dispatch.terminal_commit lands an ask run's whole candidate set and fails loud on residue."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
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
SESSION = "11111111-2222-3333-4444-555555555555"


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
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setattr(terminal_commit, "_ship_cited_sizing", lambda *a, **k: {})
    monkeypatch.setattr(terminal_commit, "_ledger_gate", lambda *a, **k: (None, None, []))
    return root


def _stage_graph(root: Path) -> None:
    """The run's record graph, all dirty: sizing naming baton and plan, sidecar, slice, predecessor."""
    _write(root, "a.py", "a\n")
    _write(root, SIZING, f"status: routed\nbaton: {BATON}\nplan: {PLAN}\n")
    _write(root, BATON, "---\ncarried_items: []\n---\nbaton\n")
    _write(root, EVIDENCE, "rows: []\n")
    _write(root, SLICE, "status: draft\n")
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask\n")
    _write(
        root, f"{RECORD_DIR_ROOT}/{RUN}/parent.json",
        json.dumps({"runs": [{"slice_sizing": SLICE}]}),
    )
    _write(root, f"{RECORD_DIR_ROOT}/{OLD_RUN}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, f"{RECORD_DIR_ROOT}/{OLD_RUN}/ask.md", "old ask\n")


def _fire(root: Path, **extra) -> dict:
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=PLAN
    )
    _write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker,
    )
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, "run.mjs", f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **extra},
        repo_root=root / ".git",
    )


def _dirty(root: Path) -> set:
    return {
        line[3:] for line in _git(["status", "--porcelain", "-uall"], root).splitlines()
    }


def test_an_ask_run_lands_sidecar_baton_slice_and_superseded_record(repo):
    _stage_graph(repo)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert out.get("ok") is not False, out
    assert "run_artifact_residue" not in out
    left = {
        p for p in _dirty(repo) - {"run.mjs", f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"}
        if not p.startswith(".coordinator-local/")
    }
    assert left == set(), left
    landed = set(out["run_artifacts"]["landed"])
    assert {EVIDENCE, BATON, SLICE, f"{RECORD_DIR_ROOT}/{OLD_RUN}/ask.md"} <= landed
    assert out["run_artifacts"]["withheld"] == {}


def test_a_peer_held_candidate_is_withheld_and_named(repo, monkeypatch):
    from coordinator_core.session import scope

    _stage_graph(repo)
    monkeypatch.setattr(
        scope, "contested_by_live_peers",
        lambda paths, sid, cwd=None: {BATON: ["peer"]} if BATON in paths else {},
    )

    out = _fire(repo, session_id=SESSION)

    assert out["committed"] is True, out
    assert out["run_artifacts"]["withheld"] == {BATON: "peer-held"}
    assert BATON in _dirty(repo)
    assert "run_artifact_residue" not in out
    assert _git(["show", "--name-only", "--format=", "HEAD"], repo).find(BATON) == -1


def test_a_gitignored_plan_stays_out(repo):
    _write(repo, ".gitignore", "scratch/\n")
    _git(["add", ".gitignore"], repo)
    _git(["commit", "-q", "-m", "ignore"], repo)
    _stage_graph(repo)
    ignored_plan = "scratch/plan.md"
    _write(repo, ignored_plan, f"---\nsizing_object: {SIZING}\n---\nbody\n")
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=ignored_plan
    )
    _write(repo, marker, render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker,
    )
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(repo, "run.mjs", f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")

    out = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert ignored_plan not in _git(["ls-files"], repo)
    assert "run_artifact_residue" not in out


def test_a_candidate_left_dirty_is_run_artifact_residue(repo, monkeypatch):
    _stage_graph(repo)
    monkeypatch.setattr(run_artifacts, "landable", lambda *a, **k: run_artifacts.Landable())

    out = _fire(repo)

    assert out["ok"] is False
    assert EVIDENCE in out["run_artifact_residue"]
    assert out["run_artifact_residue"][EVIDENCE] == "evidence-sidecar"
    assert EVIDENCE in out["error"]
