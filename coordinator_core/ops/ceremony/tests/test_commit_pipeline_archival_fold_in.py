"""
coordinator_core.ops.ceremony.tests.test_commit_pipeline_archival_fold_in

Tests for the C4 fold-in seam (docs/plans/2026-08-25-the-terminal-handoff-sweep-stops-
being-an-op.md § C4): `commit_pipeline.run_commit_pipeline`'s `_run_in_plane_archive_sweep`
composes `archive_terminal_handoffs.plan_sweep` + `.apply_sweep` IN-PROCESS, immediately
before `commit_paths` is finalised, and unions the moved src/dst paths into it -- the
replacement for the killed `tail_ops.fire_archive_sweeps_detached`.

Coverage (discharges AC-3 and AC-5, per the C4 dispatch brief's own "Test surface"):
    (a) archival_contribution_adds_zero_git_processes -- a subprocess-spy test over
        `git_native._git`, the single choke point every native git call routes through
        (AC3 of the original C4 chunk of the wsc-tail rebuild): calling
        `_run_in_plane_archive_sweep` with `plan_sweep` stubbed to return a real move over
        a real fixture spawns ZERO git processes -- `apply_sweep` is `os.replace` only.
    (b) archival_move_lands_in_one_commit_with_both_halves -- the moved src (a deletion)
        and dst (an addition) both appear in `_committed_files_at_head`, in the SAME commit
        as the caller's own ordinary staged path -- never a second commit.
    (c) failed_sweep_leaves_commit_paths_untouched -- `plan_sweep` raising degrades to a
        clean `([], [])` contribution (module docstring's non-fatal contract): the
        ceremony's own commit still lands, scoped to exactly the caller's own
        `commit_paths`, never flipping `commit_failed`.
    (d) fire_archive_sweeps_detached_has_no_live_caller -- AC-5's repo-wide-grep contract,
        pinned as an importable-symbol assertion: the name no longer exists on
        `coordinator_core.ops.ceremony.tail_ops`.

Spec backlink: pln-the-terminal-handoff-sweep-sto-91fcc2 § C4.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.commit_pipeline as commit_pipeline_mod
from coordinator_core.ops.ceremony import tail_ops as tail_ops_mod
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline
from coordinator_core.ops.fleet._common import Move

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Windows-safe subprocess flag -- no-op on POSIX (getattr falls back to 0).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-intentional-last-resort


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=_NO_WINDOW,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _committed_files_at_head(repo: Path) -> list[str]:
    # `--no-renames`: git's default rename detection collapses a delete+add
    # pair with identical content into one `R100 old\tnew` record, which
    # `--name-only` alone then renders as ONLY the new path -- the old path
    # silently drops out of a plain name-only read. `--no-renames` forces
    # the underlying delete and add to surface as two separate entries, so
    # a test asserting "both halves of the archival move are in this commit"
    # sees both.
    result = subprocess.run(
        ["git", "show", "--no-renames", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        creationflags=_NO_WINDOW,
    )
    return [line for line in result.stdout.splitlines() if line]


def _log_count(repo: Path) -> int:
    result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        creationflags=_NO_WINDOW,
    )
    return len([line for line in result.stdout.splitlines() if line])


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def _spy_git(monkeypatch) -> dict:
    """Wrap `git_native._git` -- the single choke point every native git call in
    this module routes through -- with a call-counting shim, real behaviour
    preserved. Returns a dict the caller reads `["n"]` off."""
    calls = {"n": 0}
    orig = commit_pipeline_mod.git_native._git

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(commit_pipeline_mod.git_native, "_git", _counting)
    return calls


async def _fake_plan_sweep_one_move(worktree_root, common_dir, cap, *, candidate_ids=None):
    src = worktree_root / "state" / "handoffs" / "old.md"
    dst = worktree_root / "archive" / "handoffs" / "old.md"
    return [Move(src=src, dst=dst, candidate_id="state/handoffs/old.md")], []


async def _fake_plan_sweep_raises(worktree_root, common_dir, cap, *, candidate_ids=None):
    raise RuntimeError("boom -- scan failed")


# ---------------------------------------------------------------------------
# (a) zero additional git processes
# ---------------------------------------------------------------------------


def test_archival_contribution_adds_zero_git_processes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "state/handoffs/old.md", "terminal handoff content")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    common_dir = repo / ".git"
    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _fake_plan_sweep_one_move
    )
    calls = _spy_git(monkeypatch)

    srcs, dsts = commit_pipeline_mod._run_in_plane_archive_sweep(repo, common_dir)

    assert srcs == ["state/handoffs/old.md"]
    assert dsts == ["archive/handoffs/old.md"]
    assert calls["n"] == 0, "apply_sweep (os.replace only) must spawn zero git processes"
    assert not (repo / "state" / "handoffs" / "old.md").exists()
    assert (repo / "archive" / "handoffs" / "old.md").exists()


# ---------------------------------------------------------------------------
# (b) moved src + dst both land in ONE commit
# ---------------------------------------------------------------------------


def test_archival_move_lands_in_one_commit_with_both_halves(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "state/handoffs/old.md", "terminal handoff content")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _fake_plan_sweep_one_move
    )

    _seed_file(repo, "tasks/feature/todo.md", "content")
    log_count_before = _log_count(repo)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    # Exactly one new commit -- the archival move rides the caller's own
    # commit, never a second one of its own (AC-3/AC-10).
    assert _log_count(repo) == log_count_before + 1

    committed = set(_committed_files_at_head(repo))
    assert "tasks/feature/todo.md" in committed
    assert "state/handoffs/old.md" in committed  # deletion half
    assert "archive/handoffs/old.md" in committed  # addition half
    assert not (repo / "state" / "handoffs" / "old.md").exists()
    assert (repo / "archive" / "handoffs" / "old.md").exists()


# ---------------------------------------------------------------------------
# (c) a failed sweep leaves commit_paths untouched -- non-fatal contract
# ---------------------------------------------------------------------------


def test_failed_sweep_leaves_commit_paths_untouched(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _fake_plan_sweep_raises
    )

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    # A raising plan_sweep must never flip the ceremony's exit code -- the
    # commit lands exactly as it would have without this call.
    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert _committed_files_at_head(repo) == ["tasks/feature/todo.md"]


def test_apply_sweep_failure_also_leaves_commit_paths_untouched(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "state/handoffs/old.md", "terminal handoff content")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _fake_plan_sweep_one_move
    )

    def _raising_apply_sweep(moves):
        raise RuntimeError("boom -- replace failed")

    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "apply_sweep", _raising_apply_sweep
    )

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert _committed_files_at_head(repo) == ["tasks/feature/todo.md"]
    # The move was never applied -- src untouched.
    assert (repo / "state" / "handoffs" / "old.md").exists()


# ---------------------------------------------------------------------------
# (d) AC-5 -- no live caller of the deleted detached fire
# ---------------------------------------------------------------------------


def test_fire_archive_sweeps_detached_has_no_live_caller():
    assert not hasattr(tail_ops_mod, "fire_archive_sweeps_detached")
    assert not hasattr(tail_ops_mod, "_ARCHIVE_SWEEP_SCRIPTS")


# ---------------------------------------------------------------------------
# (f) the cadence gate — the OCCASION is every ceremony commit, the JOB is not
#
# plan_sweep costs a corpus-sized pass whether or not anything is archivable:
# frontmatter for every live handoff, the reverse-edge index over all of them,
# live-session resolution, a claim-dir probe per candidate. Measured 2026-08-26
# at p50 31.2ms CPU / 79.0ms wall over 237 records, archiving 0. AC-3 and AC-7
# both measure processes and spawns ADDED and this adds neither, so nothing in
# the shipped AC set could fail on it.
#
# The load-bearing assertion is the FIRST one in each test: that plan_sweep was
# not merely cheap but NOT CALLED. A gate asserted only on its return value
# passes just as well when the gate does nothing.
# ---------------------------------------------------------------------------


def _counting_plan_sweep(calls: dict):
    async def _fake(worktree_root, common_dir, cap, *, candidate_ids=None, **kwargs):
        calls["n"] += 1
        return [], []
    return _fake


def test_cadence_gate_skips_the_corpus_pass_inside_the_interval(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    common_dir = repo / ".git"
    calls = {"n": 0}
    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _counting_plan_sweep(calls)
    )
    monkeypatch.setattr(commit_pipeline_mod, "_ARCHIVE_SWEEP_INTERVAL_S", 900.0)
    commit_pipeline_mod._stamp_archive_sweep(common_dir)

    srcs, dsts = commit_pipeline_mod._run_in_plane_archive_sweep(repo, common_dir)

    assert calls["n"] == 0, (
        "a just-stamped sweep must do NO corpus work on the next ceremony "
        "commit -- an empty return proves nothing if plan_sweep still ran"
    )
    assert (srcs, dsts) == ([], [])


def test_cadence_gate_opens_once_the_interval_has_passed(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    common_dir = repo / ".git"
    calls = {"n": 0}
    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _counting_plan_sweep(calls)
    )
    monkeypatch.setattr(commit_pipeline_mod, "_ARCHIVE_SWEEP_INTERVAL_S", 0.0)
    commit_pipeline_mod._stamp_archive_sweep(common_dir)

    commit_pipeline_mod._run_in_plane_archive_sweep(repo, common_dir)

    assert calls["n"] == 1


def test_cadence_gate_open_on_a_never_stamped_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    common_dir = repo / ".git"
    calls = {"n": 0}
    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _counting_plan_sweep(calls)
    )
    monkeypatch.setattr(commit_pipeline_mod, "_ARCHIVE_SWEEP_INTERVAL_S", 900.0)

    commit_pipeline_mod._run_in_plane_archive_sweep(repo, common_dir)

    assert calls["n"] == 1, "a repo that has never swept must sweep"


def test_cadence_gate_stamps_even_when_the_sweep_finds_nothing(tmp_path, monkeypatch):
    """0 moves is the corpus this sweep currently produces. Stamping only on a
    non-empty result would reopen the gate on every commit for exactly that
    case -- the one the gate exists for."""
    repo = _init_repo(tmp_path)
    common_dir = repo / ".git"
    calls = {"n": 0}
    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _counting_plan_sweep(calls)
    )
    monkeypatch.setattr(commit_pipeline_mod, "_ARCHIVE_SWEEP_INTERVAL_S", 900.0)

    commit_pipeline_mod._run_in_plane_archive_sweep(repo, common_dir)
    commit_pipeline_mod._run_in_plane_archive_sweep(repo, common_dir)

    assert calls["n"] == 1, (
        "the second commit inside the interval must not re-run the pass, even "
        "though the first found nothing to archive"
    )


def test_cadence_gate_does_not_stamp_when_the_sweep_fails(tmp_path, monkeypatch):
    """A failed pass has not run the job, so it must not close the gate on the
    next occasion -- otherwise one exception silences archival for an interval."""
    repo = _init_repo(tmp_path)
    common_dir = repo / ".git"
    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _fake_plan_sweep_raises
    )
    monkeypatch.setattr(commit_pipeline_mod, "_ARCHIVE_SWEEP_INTERVAL_S", 900.0)

    assert commit_pipeline_mod._run_in_plane_archive_sweep(repo, common_dir) == ([], [])

    calls = {"n": 0}
    monkeypatch.setattr(
        commit_pipeline_mod.archive_terminal_handoffs, "plan_sweep", _counting_plan_sweep(calls)
    )
    commit_pipeline_mod._run_in_plane_archive_sweep(repo, common_dir)

    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# (g) the in-plane spawn count is ONE, and the count is taken at ONE layer
#
# AC-3 said 0 and was ticked met; AC-11 said 2 and was ticked partial. Both
# were wrong (plan AC table, corrected 2026-08-26). The 2 was a double-count --
# `git_native._git` delegates to `subprocess.run`, so a spy at each layer sees
# one process twice, which is the predecessor's own instrument-disagreement
# lesson arriving inverted. This test counts at the OS-call layer ONLY.
#
# 0 is not the target and must not be pursued: `known_dirty_relpaths` is a
# POSITIVE list, so feeding it commit_pipeline's `git diff --cached` answer --
# a different predicate over a different path set -- makes the worktree-dirty
# rail fail OPEN on every handoff outside commit_paths.
# ---------------------------------------------------------------------------


def test_in_plane_sweep_spawns_exactly_one_git_process(tmp_path, monkeypatch):
    import subprocess as _subprocess

    repo = _init_repo(tmp_path)
    _seed_file(repo, "state/handoffs/open-baton.md", "---\nstatus: open\n---\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(commit_pipeline_mod, "_ARCHIVE_SWEEP_INTERVAL_S", 0.0)

    spawned = []
    real_run = _subprocess.run

    def _counting_run(cmd, *a, **kw):
        spawned.append(cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(_subprocess, "run", _counting_run)

    commit_pipeline_mod._run_in_plane_archive_sweep(repo, repo / ".git")

    assert len(spawned) == 1, (
        f"in-plane sweep must spawn exactly one git process (Rail 1's "
        f"worktree-dirty status; Rail 2 is spawn-free since C10). Got "
        f"{len(spawned)}: {spawned}"
    )
    assert "status" in spawned[0] and "--porcelain" in spawned[0], spawned[0]


def test_cadence_gate_makes_that_spawn_per_interval_not_per_commit(tmp_path, monkeypatch):
    import subprocess as _subprocess

    repo = _init_repo(tmp_path)
    _seed_file(repo, "state/handoffs/open-baton.md", "---\nstatus: open\n---\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(commit_pipeline_mod, "_ARCHIVE_SWEEP_INTERVAL_S", 900.0)

    spawned = []
    real_run = _subprocess.run

    def _counting_run(cmd, *a, **kw):
        spawned.append(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(_subprocess, "run", _counting_run)

    for _ in range(5):
        commit_pipeline_mod._run_in_plane_archive_sweep(repo, repo / ".git")

    assert len(spawned) == 1, (
        f"five ceremony commits inside one interval must cost ONE git spawn "
        f"between them, not one each; got {len(spawned)}"
    )
