"""dispatch.terminal_commit in a repo without coordinator state commits product paths only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
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

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.foreign_repo]

REVIEW_SIDECAR = ".coordinator-local/subagent-share/rev-s1.md"
_REVIEWED = {
    "integration_stem": "rev-stem",
    "slices": 1,
    "fixes": 0,
    "wave_reviews": [{"slice_id": "s1", "verdict": "PASS", "sidecar_path": REVIEW_SIDECAR}],
}
RUN = "ask-20261016T120000-aaaaaa"
SIZING = "state/sizings/2026-10-16-x.yaml"
BATON = "state/handoffs/2026-10-16-x.md"
PLAN = "docs/plans/2026-10-16-x.md"
EVIDENCE = "docs/plans/2026-10-16-x.evidence.yaml"
PRODUCT = "a.py"


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
    _write(root, "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setattr(terminal_commit, "_ship_cited_sizing", lambda *a, **k: {})
    monkeypatch.setattr(terminal_commit, "_ledger_gate", lambda *a, **k: (None, None, []))
    return root


def _stage_run(root: Path) -> None:
    _write(root, PRODUCT, "a\n")
    _write(root, PLAN, f"---\nsizing_object: {SIZING}\n---\nbody\n")
    _write(root, SIZING, f"status: routed\nbaton: {BATON}\nplan: {PLAN}\n")
    _write(root, BATON, "---\ncarried_items: []\n---\nbaton\n")
    _write(root, EVIDENCE, "rows: []\n")
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask\n")


def _fire(root: Path) -> dict:
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=(PRODUCT,)),), plan_path=PLAN
    )
    _write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker,
    )
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, REVIEW_SIDECAR, "---\nverdict: PASS\n---\nreview\n")
    _write(root, "run.mjs", f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED},
        repo_root=root / ".git",
    )


def _exclude_lines(root: Path) -> list[str]:
    path = root / ".git" / "info" / "exclude"
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]


def _dirty(root: Path) -> set:
    """Dirty paths other than the harness's own run script, marker and local scratch."""
    lines = _git(["status", "--porcelain", "-uall"], root).splitlines()
    return {
        ln[3:] for ln in lines
        if ln[3:] != "run.mjs"
        and not ln[3:].startswith((".coordinator-local/", "scratch/"))
    }


def _commits(root: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], root).strip())


def test_a_run_in_a_repo_without_coordinator_state_commits_product_paths_only(repo):
    _stage_run(repo)
    before = _commits(repo)

    out = _fire(repo)

    assert out["committed"] is True, out
    # Pins current behavior: the stage stamp lands as a second "(process files)" commit rather
    # than riding the product commit; a fix to that must update this count.
    assert _commits(repo) == before + 2
    stage = _git(["show", "--name-only", "--format=", "HEAD"], repo).split()
    assert stage == [f"{RECORD_DIR_ROOT}/{RUN}/run-stage.json"], stage
    paths = _git(["show", "--name-only", "--format=", "HEAD~1"], repo).split()
    assert paths == [PRODUCT], paths
    assert out["run_artifacts"]["landed"] == []
    withheld = out["run_artifacts"]["withheld"]
    assert {SIZING, BATON, EVIDENCE} <= set(withheld), withheld
    assert set(withheld.values()) == {"repo-not-opted-in"}
    assert _dirty(repo) == set(), _dirty(repo)
    excluded = set(_exclude_lines(repo))
    assert {"/" + p for p in withheld} <= excluded, excluded
    assert not (repo / "archive").exists()


def test_a_second_run_adds_no_duplicate_exclude_lines(repo):
    _stage_run(repo)
    _fire(repo)
    first = _exclude_lines(repo)

    _write(repo, PRODUCT, "a2\n")
    second = _fire(repo)

    assert second["committed"] is True, second
    stage_line = f"/{RECORD_DIR_ROOT}/{RUN}/run-stage.json"
    assert _exclude_lines(repo) == [*first, stage_line]
    assert stage_line not in first
    assert len(first) == len(set(first))


def test_the_same_run_in_an_onboarded_repo_still_lands_the_artifacts(repo):
    (repo / "archive").mkdir()
    _write(repo, "archive/.keep", "")
    _git(["add", "archive/.keep"], repo)
    _git(["commit", "-q", "-m", "onboard"], repo)
    _stage_run(repo)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert out["run_artifacts"]["withheld"] == {}
    assert EVIDENCE in out["run_artifacts"]["landed"]
