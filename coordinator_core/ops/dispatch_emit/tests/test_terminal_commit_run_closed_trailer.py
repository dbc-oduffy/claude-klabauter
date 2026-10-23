"""The terminal commit carries Checkpoint-Run-Closed for the run base (and a differing review base)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REVIEW_BASE = "b" * 40


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root, _git(["rev-parse", "HEAD"], root).strip()


def _close(root: Path, extra_params: dict, **req_kwargs) -> dict:
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    req = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),),
        deliverable_id="dlv-plan111",
        **req_kwargs,
    )
    (root / "run.mjs").write_text(render_marker(req) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [],
         "inline_review": {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}, **extra_params},
        repo_root=root / ".git",
    )


def _closed(root: Path) -> list:
    return _git(["log", "-1", "--format=%(trailers:key=Checkpoint-Run-Closed,valueonly)"], root).split()


def test_known_base_is_named(tmp_path):
    root, base = _repo(tmp_path)
    out = _close(root, {"run_base_sha": base})
    assert out.get("committed"), out
    assert _closed(root) == [base]


def test_unknown_base_omits_the_line(tmp_path):
    root, _ = _repo(tmp_path)
    out = _close(root, {})
    assert out.get("committed"), out
    assert _closed(root) == []


def test_differing_review_base_adds_a_line(tmp_path):
    root, base = _repo(tmp_path)
    (root / "docs").mkdir()
    (root / "docs/plan.md").write_text("---\ndeliverable_id: dlv-plan111\ntitle: t\n---\nbody\n", encoding="utf-8")
    out = _close(
        root, {"run_base_sha": base},
        plan_path="docs/plan.md", review_only=True, review_open_rows=("C1",), review_run_base=REVIEW_BASE,
    )
    assert out.get("committed"), out
    assert _closed(root) == [base, REVIEW_BASE]


def test_manifest_head_sha_is_named_first_and_differs_from_submission_base(tmp_path):
    root, base = _repo(tmp_path)
    manifest_head = "c" * 40
    rec = root / "state" / "warp" / "run-x"
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(f'{{"head_sha": "{manifest_head}"}}', encoding="utf-8")
    out = _close(root, {"run_base_sha": base, "run_id": "run-x"})
    assert out.get("committed"), out
    assert _closed(root) == [manifest_head, base]


def test_halt_commit_path_never_emits_it():
    assert "Checkpoint-Run-Closed" not in Path(halted_record_commit.__file__).read_text(encoding="utf-8")
