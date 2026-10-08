"""
coordinator_core.ops.fleet.tests.test_archive_plans_untracked_sidecar

Regression coverage for the defect commit 9cd2fc40cf introduced and
Example-store-repo-fb verified: a plan's `<stem>.workflow.mjs` and
`<stem>.workflow.mjs.emitted.json` fire-script sidecars are written beside
the plan by `emit-dispatch-workflow` but the emitter never commits them, so
they are untracked at HEAD. `archive_and_commit`'s `untracked-at-head`
refusal — a deliberate integrity refusal for a TRACKED primary, kept
unchanged here — was firing on these sidecars too, so the primary moved
into archive/ alone and the fire scripts were stranded as orphans, exactly
what 9cd2fc40cf was meant to stop.

Fix under test: `archive_plans._partition_untracked_sidecars` +
`archive_plans._apply_untracked_sidecar_moves` — an untracked sidecar is
routed around the commit entirely and moved with a plain filesystem rename
into the same archive destination, AFTER the tracked-commit call returns.

Real on-disk git repo throughout (mirrors test_common_tree_build.py's own
fixture pattern) — `_partition_untracked_sidecars` reads HEAD's tree spine
via `read_tree_spine`, which needs a real `.git` to answer honestly; a mock
cannot reproduce "genuinely untracked" vs "tracked" the way this defect
requires.

Negative-spec:
  - Does NOT re-test archive_and_commit's own untracked-at-head refusal for
    a TRACKED primary — that refusal is unchanged and out of this module's
    scope (see archive_plans.py's own module docstring negative-spec).
  - Does NOT assert on the single-flight lock or the standalone `_handler`
    dry_run path — both are covered by test_archive_plans.py already; this
    module is scoped to the act-path sidecar split only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import archive_plans as m
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn; see module
    # docstring for why a mock cannot stand in for the tracked/untracked
    # distinction this fix depends on.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)


def _write_and_commit_plan(root: Path, name: str, status: str) -> Path:
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / name
    path.write_text(f"---\ntitle: \"{name}\"\nstatus: {status}\n---\n\nbody\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", f"add {name}"], root)
    return path


def _write_fire_script(root: Path, stem: str) -> "tuple[Path, Path]":
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    script = plans_dir / f"{stem}.workflow.mjs"
    receipt = plans_dir / f"{stem}.workflow.mjs.emitted.json"
    script.write_text("export const meta = {}\n", encoding="utf-8")
    receipt.write_text("{}\n", encoding="utf-8")
    # Deliberately never `git add`/`git commit`'d — mirrors the emitter's
    # real behaviour (module docstring: "the emitter never commits them").
    return script, receipt


def test_partition_splits_untracked_sidecars_from_the_commit_batch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _write_and_commit_plan(root, "2026-08-20-fired.md", "implemented")
    _write_fire_script(root, "2026-08-20-fired")

    moves, _skipped = m.plan_sweep(root, root, cap=10)
    assert len(moves) == 3

    commit_moves, fs_moves = m._partition_untracked_sidecars(root, moves)

    assert {mv.candidate_id for mv in commit_moves} == {"docs/plans/2026-08-20-fired.md"}
    assert {mv.candidate_id for mv in fs_moves} == {
        "docs/plans/2026-08-20-fired.workflow.mjs",
        "docs/plans/2026-08-20-fired.workflow.mjs.emitted.json",
    }


def test_untracked_row_evidence_is_committed_with_its_plan(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _write_and_commit_plan(root, "2026-08-23-ev.md", "implemented")
    evidence = root / "docs" / "plans" / "2026-08-23-ev.evidence.yaml"
    evidence.write_text("C1: []\n", encoding="utf-8")
    _write_fire_script(root, "2026-08-23-ev")

    moves, _skipped = m.plan_sweep(root, root, cap=10)
    commit_moves, fs_moves = m._partition_untracked_sidecars(root, moves)

    by_id = {mv.candidate_id: mv for mv in commit_moves}
    assert set(by_id) == {"docs/plans/2026-08-23-ev.md", "docs/plans/2026-08-23-ev.evidence.yaml"}
    assert by_id["docs/plans/2026-08-23-ev.evidence.yaml"].restage_src is True
    assert {mv.candidate_id for mv in fs_moves} == {
        "docs/plans/2026-08-23-ev.workflow.mjs",
        "docs/plans/2026-08-23-ev.workflow.mjs.emitted.json",
    }


def test_partition_keeps_a_tracked_sidecar_in_the_commit_batch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _write_and_commit_plan(root, "2026-08-21-done.md", "implemented")
    _write_and_commit_plan(root, "2026-08-21-done.review.md", "implemented")

    moves, _skipped = m.plan_sweep(root, root, cap=10)
    assert len(moves) == 2

    commit_moves, fs_moves = m._partition_untracked_sidecars(root, moves)

    assert {mv.candidate_id for mv in commit_moves} == {
        "docs/plans/2026-08-21-done.md",
        "docs/plans/2026-08-21-done.review.md",
    }
    assert fs_moves == []


def test_untracked_sidecar_dest_exists_is_a_named_refusal_not_an_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    script, _receipt = _write_fire_script(root, "2026-08-22-collide")
    dst = root / "archive" / "specs" / "2026-08" / "2026-08-22-collide.workflow.mjs"
    dst.parent.mkdir(parents=True)
    dst.write_text("already here\n", encoding="utf-8")

    move = m.Move(src=script, dst=dst, candidate_id="docs/plans/2026-08-22-collide.workflow.mjs")
    acted, failed = m._apply_untracked_sidecar_moves([move])

    assert acted == []
    assert failed == [{"id": move.candidate_id, "reason": m._REASON_SIDECAR_DEST_EXISTS}]
    assert dst.read_text(encoding="utf-8") == "already here\n"
    assert script.is_file()


def test_handle_act_lands_primary_via_commit_and_fire_script_via_plain_rename(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _write_and_commit_plan(root, "2026-08-23-both.md", "implemented")
    _write_fire_script(root, "2026-08-23-both")

    result = m._handle_act(
        "already-terminal",
        root,
        root,
        [
            "docs/plans/2026-08-23-both.md",
            "docs/plans/2026-08-23-both.workflow.mjs",
            "docs/plans/2026-08-23-both.workflow.mjs.emitted.json",
        ],
        cap=10,
    )

    assert not result["failed"], result["failed"]
    acted_ids = {row["id"] for row in result["acted"]}
    assert acted_ids == {
        "docs/plans/2026-08-23-both.md",
        "docs/plans/2026-08-23-both.workflow.mjs",
        "docs/plans/2026-08-23-both.workflow.mjs.emitted.json",
    }

    dest_dir = root / "archive" / "specs" / "2026-08"
    assert (dest_dir / "2026-08-23-both.md").is_file()
    assert (dest_dir / "2026-08-23-both.workflow.mjs").is_file()
    assert (dest_dir / "2026-08-23-both.workflow.mjs.emitted.json").is_file()

    # The primary landed via a real commit; the fire scripts never did — the
    # commit log has exactly the two commits this test made itself (init
    # plan + this archival), not a third for the untracked-at-HEAD sidecars.
    log = _git(["log", "--oneline"], root).stdout.strip().splitlines()
    assert len(log) == 2

    ls_tree = _git(["ls-tree", "-r", "--name-only", "HEAD"], root).stdout
    assert "archive/specs/2026-08/2026-08-23-both.md" in ls_tree
    assert "archive/specs/2026-08/2026-08-23-both.workflow.mjs" not in ls_tree


def test_fire_script_alone_after_primary_already_archived_moves_with_zero_commits(tmp_path: Path) -> None:
    # Mirrors an earlier sweep that already archived the primary (committed)
    # while its fire-script sidecar was stranded (the defect's own shape).
    root = tmp_path / "repo"
    _init_repo(root)
    archived = root / "archive" / "specs" / "2026-08" / "2026-08-24-gone.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("---\nstatus: implemented\n---\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "archive primary"], root)
    _write_fire_script(root, "2026-08-24-gone")

    before_log = _git(["log", "--oneline"], root).stdout.strip().splitlines()

    result = m._handle_act(
        "already-terminal",
        root,
        root,
        [
            "docs/plans/2026-08-24-gone.workflow.mjs",
            "docs/plans/2026-08-24-gone.workflow.mjs.emitted.json",
        ],
        cap=10,
    )

    assert not result["failed"], result["failed"]
    assert {row["id"] for row in result["acted"]} == {
        "docs/plans/2026-08-24-gone.workflow.mjs",
        "docs/plans/2026-08-24-gone.workflow.mjs.emitted.json",
    }
    assert (archived.parent / "2026-08-24-gone.workflow.mjs").is_file()
    assert (archived.parent / "2026-08-24-gone.workflow.mjs.emitted.json").is_file()

    after_log = _git(["log", "--oneline"], root).stdout.strip().splitlines()
    assert after_log == before_log


def test_sibling_dest_exists_rolls_back_the_sidecar_already_moved(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    script, receipt = _write_fire_script(root, "2026-08-24-split")
    dest_dir = root / "archive" / "specs" / "2026-08"
    dest_dir.mkdir(parents=True)
    first = m.Move(
        src=script, dst=dest_dir / script.name, candidate_id="docs/plans/2026-08-24-split.workflow.mjs"
    )
    blocked_dst = dest_dir / receipt.name
    blocked_dst.write_text("already here\n", encoding="utf-8")
    second = m.Move(
        src=receipt, dst=blocked_dst, candidate_id="docs/plans/2026-08-24-split.workflow.mjs.emitted.json"
    )

    acted, failed = m._apply_untracked_sidecar_moves([first, second])

    assert acted == []
    assert {f["id"] for f in failed} == {first.candidate_id, second.candidate_id}
    assert script.is_file() and receipt.is_file()
    assert not first.dst.exists()
    assert blocked_dst.read_text(encoding="utf-8") == "already here\n"


def test_untracked_evidence_sidecar_follows_its_plan_into_the_archive(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _write_and_commit_plan(root, "2026-08-24-ev.md", "implemented")
    evidence = root / "docs" / "plans" / "2026-08-24-ev.evidence.yaml"
    evidence.write_text("rows:\n  C1:\n  - recorded_at: '2026-10-08T00:00:00Z'\n    text: ok\n", encoding="utf-8")

    result = m._handle_act(
        "already-terminal",
        root,
        root,
        ["docs/plans/2026-08-24-ev.md", "docs/plans/2026-08-24-ev.evidence.yaml"],
        cap=10,
    )

    assert not result["failed"], result["failed"]
    dest = root / "archive" / "specs" / "2026-08" / "2026-08-24-ev.evidence.yaml"
    assert dest.is_file()
    assert not evidence.exists()
    ls_tree = _git(["ls-tree", "-r", "--name-only", "HEAD"], root).stdout
    assert "archive/specs/2026-08/2026-08-24-ev.evidence.yaml" in ls_tree
    assert _git(["status", "--porcelain", "--", "archive", "docs"], root).stdout == ""


def test_tracked_evidence_sidecar_rides_the_archive_commit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _write_and_commit_plan(root, "2026-08-25-ev.md", "implemented")
    (root / "docs" / "plans" / "2026-08-25-ev.evidence.yaml").write_text("rows: {}\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "evidence"], root)

    result = m._handle_act(
        "already-terminal",
        root,
        root,
        ["docs/plans/2026-08-25-ev.md", "docs/plans/2026-08-25-ev.evidence.yaml"],
        cap=10,
    )

    assert not result["failed"], result["failed"]
    ls_tree = _git(["ls-tree", "-r", "--name-only", "HEAD"], root).stdout
    assert "archive/specs/2026-08/2026-08-25-ev.evidence.yaml" in ls_tree
    assert "docs/plans/2026-08-25-ev.evidence.yaml" not in ls_tree
