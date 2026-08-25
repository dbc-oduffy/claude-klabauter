"""
coordinator_core.review_assemble.tests.test_review_assemble_seam_exercise --
end-to-end seam test for the `roadmap` subject class added to `residue.py`'s
`--surface` ladder (C2).

Invokes the REAL `coordinator/bin/review-assemble.py` trampoline as a
subprocess (never an in-process call, never an inspection assertion over
`residue.py`'s internals) and asserts on the live decision object it prints
to stdout -- the same shape `test_residue.py`'s per-field rows exercise
in-process, but exercised here through the actual CLI seam a caller uses.

`COORDINATOR_ROOT` (a real dev/passthrough-mode rung on
`resolve_content_root`'s ladder -- see `coordinator_core.resolve_coordinator_
clone.resolve_content_root`) points the trampoline's content-root resolution
at a per-test fixture directory carrying `plan`/`diff`/`roadmap` segments
with distinct `segment_id`s, so the acceptance bar (a genuinely different
SEGMENT SET for `--surface roadmap`, proven against the live decision
object) is checked without touching any real installed content root.

Spec backlink: state/dispatch-briefs/2026-08-21-engine-half-of-the-roadmap-
sprint-spine-split/C2.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TRAMPOLINE = _REPO_ROOT / "coordinator" / "bin" / "review-assemble.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _segment_text(segment_id: str, surface: str, cls: str, order: int, body: str) -> str:
    return (
        "---\n"
        f"segment_id: {segment_id}\n"
        f"surface: {surface}\n"
        f"class: {cls}\n"
        f"order: {order}\n"
        "---\n"
        f"{body}\n"
    )


def _make_content_root(tmp_path: Path) -> Path:
    content_root = tmp_path / "content-root"
    residue_dir = content_root / "skills" / "review" / "residue"
    residue_dir.mkdir(parents=True)
    (residue_dir / "010-shared.md").write_text(
        _segment_text("shared-reminder", "shared", "protected", 0, "Shared body."),
        encoding="utf-8",
    )
    (residue_dir / "020-plan.md").write_text(
        _segment_text("plan-reminder", "plan", "droppable", 1, "Plan body."),
        encoding="utf-8",
    )
    (residue_dir / "030-diff.md").write_text(
        _segment_text("diff-reminder", "diff", "droppable", 2, "Diff body."),
        encoding="utf-8",
    )
    (residue_dir / "040-roadmap.md").write_text(
        _segment_text("roadmap-reminder", "roadmap", "droppable", 3, "Roadmap body."),
        encoding="utf-8",
    )
    return content_root


def _run_trampoline(repo: Path, content_root: Path, *extra_argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_ROOT"] = str(content_root)
    env["COORDINATOR_ENGINE_ROOT"] = str(_REPO_ROOT)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        [sys.executable, str(_TRAMPOLINE), "brief", *extra_argv],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_explicit_surface_roadmap_selects_a_genuinely_different_segment_set(
    tmp_path: Path,
) -> None:
    """Live-decision-object acceptance bar: `--surface roadmap` against the
    real trampoline selects a distinct segment set from `plan`/`diff` --
    proof this is a real subject class, not an alias of an existing one."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_content_root(tmp_path)

    result = _run_trampoline(repo, content_root, "--surface", "roadmap")
    assert result.returncode == 0, (result.stdout, result.stderr)

    decision_object = json.loads(result.stdout)
    assert decision_object["artifact"]["surface"] == "roadmap"
    assert decision_object["judgment_points"] == []

    segment_ids = {s["segment_id"] for s in decision_object["segments"]}
    assert segment_ids == {"shared-reminder", "roadmap-reminder"}
    assert "plan-reminder" not in segment_ids
    assert "diff-reminder" not in segment_ids


def test_explicit_surface_roadmap_differs_from_plan_and_diff_segment_sets(
    tmp_path: Path,
) -> None:
    """`roadmap`'s selected segment set is disjoint from both `plan`'s and
    `diff`'s droppable segments -- the SEGMENT SET is genuinely different,
    not merely a differently-labeled copy of an existing one."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_content_root(tmp_path)

    roadmap = json.loads(
        _run_trampoline(repo, content_root, "--surface", "roadmap").stdout
    )
    plan = json.loads(_run_trampoline(repo, content_root, "--surface", "plan").stdout)
    diff = json.loads(_run_trampoline(repo, content_root, "--surface", "diff").stdout)

    roadmap_ids = {s["segment_id"] for s in roadmap["segments"]}
    plan_ids = {s["segment_id"] for s in plan["segments"]}
    diff_ids = {s["segment_id"] for s in diff["segments"]}

    assert roadmap_ids != plan_ids
    assert roadmap_ids != diff_ids
    assert "roadmap-reminder" in roadmap_ids
    assert "roadmap-reminder" not in plan_ids
    assert "roadmap-reminder" not in diff_ids


def test_explicit_surface_roadmap_never_inferred_from_artifact_or_diff(
    tmp_path: Path,
) -> None:
    """`roadmap` is reachable ONLY through an explicit `--surface roadmap` --
    a dirty tree (would otherwise infer `diff`) and no `--surface` flag must
    NOT resolve to `roadmap`; it stays `diff`, matching the pre-C2 ladder."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_content_root(tmp_path)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    result = _run_trampoline(repo, content_root)
    assert result.returncode == 0, (result.stdout, result.stderr)
    decision_object = json.loads(result.stdout)
    assert decision_object["artifact"]["surface"] == "diff"


def test_unrecognized_explicit_surface_still_rejects_roadmap_lookalikes(
    tmp_path: Path,
) -> None:
    """A near-miss `--surface` value (not `roadmap`) is still a usage error --
    guards against a change that accidentally widened acceptance beyond the
    four legal `EXPLICIT_SURFACES` values."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_content_root(tmp_path)

    result = _run_trampoline(repo, content_root, "--surface", "roadmaps")
    assert result.returncode == 2, (result.stdout, result.stderr)
