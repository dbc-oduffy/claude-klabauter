"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit

Tests for the `ceremony.scoped_git_commit` op (scoped_git_commit.py) — the
DEC-3 thin wrapper over `commit_pipeline.run_commit_pipeline` for the
fence-inventory `scoped-git-commit` sites
(docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § DEC-3).

Coverage:
  - a genuine commit lands, response reports the real sha and `pushed=None`
    (no remote configured in these throwaway fixtures).
  - a second, identical invocation is a safe no-op (AC7 idempotency) —
    `committed=False`, `sha=None`.
  - the explicit pathspec never absorbs a concurrent sibling's own staged
    file outside the caller's paths (mirrors commit_pipeline's own
    assertion (d), exercised through the op boundary this time).
  - required-param validation errors for each of the three params.

All git operations run against a throwaway repo created fresh under
`tmp_path` — never the working repo.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.ceremony import git_native, scoped_git_commit
from coordinator_core.ops.session.safe_commit_offer import compute_offer
from coordinator_core.ops.session import scope_report
from coordinator_core.ops.session.scope_report import (
    assert_paths_in_session_scope as _real_assert_paths_in_session_scope,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness
from coordinator_core.session import scope as session_scope

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


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
    result = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _porcelain(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _current_branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _call(params: dict) -> dict:
    # _handler is a plain sync function (2026-08-07 transport-hang fix — see
    # its own docstring); no asyncio.run wrapper needed or possible.
    return scoped_git_commit._handler(params, repo_root=None)


def test_commit_lands_and_reports_sha_and_no_remote(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["tasks/feature/todo.md"],
            "message": "add feature todo",
        }
    )

    assert result["committed"] is True
    assert result["sha"]
    assert result["pushed"] is None  # no remote configured
    assert result["push_state"] == scoped_git_commit.PUSH_STATE_NO_REMOTE
    assert "error" not in result
    assert _committed_files_at_head(repo) == ["tasks/feature/todo.md"]


def test_second_identical_invocation_is_safe_noop(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    params = {
        "worktree_root": str(repo),
        "paths": ["tasks/feature/todo.md"],
        "message": "add feature todo",
    }

    first = _call(params)
    assert first["committed"] is True
    first_sha = first["sha"]

    second = _call(params)
    assert second["committed"] is False
    assert second["sha"] is None
    assert second["pushed"] is False
    # `push_state` is a report about a commit that landed; a no-op has no push
    # to report, so the key is absent rather than carrying a misleading value.
    assert "push_state" not in second

    # HEAD did not move, no duplicate commit.
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_sha == first_sha


def test_does_not_absorb_concurrent_sibling_staged_file(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")
    _seed_file(repo, "tasks/sibling/other.md", "sibling content")
    _git(["add", "--", "tasks/sibling/other.md"], repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["tasks/feature/todo.md"],
            "message": "add feature todo only",
        }
    )

    assert result["committed"] is True
    committed_files = _committed_files_at_head(repo)
    assert committed_files == ["tasks/feature/todo.md"]
    assert "tasks/sibling/other.md" not in committed_files

    # Sibling's own staged file remains staged, neither committed nor lost.
    status = _porcelain(repo)
    assert any(line.endswith("tasks/sibling/other.md") for line in status)


def test_agree_branch_cas_refuses_when_peer_absorbs_the_stage_mid_call(tmp_path, monkeypatch):
    """Layer-1 CAS regression (state/audits/2026-08-14-scoped-commit-
    partial-stage-sweep.md, "S5" -- the live incident this fix closes).

    A peer commits this call's own staged content into HEAD in the window
    between `commit_scoped()`'s own `diverging_paths()` check (correctly
    finds "not diverged" -- the peer's commit made it so) and its
    agree-branch `git add`. Before the fix, that `git add` silently
    restaged the (now peer-authored-in-history) worktree content and the
    commit landed anyway, sweeping the race in. After the fix, the CAS
    snapshot taken before `diverging_paths()` ran no longer matches --
    the call refuses loud rather than committing over it.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "file.txt", "base\n")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "base"], repo)

    _seed_file(repo, "file.txt", "base\nEM edit\n")
    _git(["add", "--", "file.txt"], repo)

    real_diverging_paths = git_native.diverging_paths
    landed = {"done": False}

    def _racing_diverging_paths(paths, **kwargs):
        result = real_diverging_paths(paths, **kwargs)
        if not landed["done"]:
            landed["done"] = True
            # A real concurrent peer commits EXACTLY this call's own staged/
            # worktree content (they agree -- not diverged) into HEAD before
            # this call's own agree branch ever runs `git add`.
            _git(["commit", "-q", "-m", "peer race commit", "--", "file.txt"], repo)
            # The peer's OWN further worktree edit, still uncommitted -- the
            # real incident's shape (86bb14e47 swept 213 lines of the peer's
            # OWN uncommitted hunks, not just the absorbed content). Without
            # this, the pipeline's post-refusal rollback would leave the
            # path byte-identical to the new HEAD, and this call's own
            # idempotency reclassification (`_classify_uncommitted`) would
            # read that as a benign already-committed no-op rather than the
            # genuine refusal this test pins -- see that function's own
            # docstring for why "nothing left to commit" and "refused" are
            # otherwise indistinguishable once the absorbed content matches.
            (repo / "file.txt").write_text(
                "base\nEM edit\npeer's own further uncommitted edit\n", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(git_native, "diverging_paths", _racing_diverging_paths)

    head_before_call = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["file.txt"],
            "message": "EM's own commit attempt",
        }
    )

    assert result["committed"] is False
    assert result.get("commit_failed") is True
    assert any("compare-and-swap" in d.lower() for d in result.get("diagnostics", []))

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    # HEAD is the peer's race commit -- never silently re-committed under a
    # SECOND (EM) commit, and never rewritten/orphaned by this call.
    assert head_after != head_before_call
    log = subprocess.run(
        ["git", "log", "--format=%s", "-n", "3"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout
    assert log.count("peer race commit") == 1
    assert "EM's own commit attempt" not in log


def test_agree_branch_cas_check_does_not_false_refuse_a_staged_deletion(tmp_path):
    """Layer-1 CAS gap noted in review (code-reviewer, bf7bab8ce37c review,
    P3 "test gap"): a STAGED DELETION has no index entry at all --
    `_index_blobs` maps it to `None` on both the pre-snapshot and the
    re-observation immediately before `git add`/`git commit`. `None != None`
    is False, so `moved` must never fire for it, and `pre_index_blobs.get(p)
    is not None` excludes it from `absorbed_candidates` outright -- the CAS
    check must be a complete no-op for this path, and the deletion must
    still land through the ordinary agree branch.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "file.txt", "seed\n")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _git(["rm", "-q", "--", "file.txt"], repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["file.txt"],
            "message": "commit the staged deletion",
        }
    )

    assert result["committed"] is True
    assert not (repo / "file.txt").exists()
    ls_tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "file.txt" not in ls_tree.splitlines()


def test_agree_branch_cas_refuses_case_divergent_caller_path(tmp_path):
    """PM follow-up on the P1 path-key-mismatch review finding
    (bf7bab8ce37c): case divergence is the same missed-refusal shape as the
    C-quoting bug, narrowed -- a caller-supplied path differing only in
    case from the tracked path must refuse, not silently pass the CAS
    check as `None == None`.

    Portable ONLY on a case-INSENSITIVE filesystem (Windows/macOS default --
    this repo's primary box per CLAUDE.md). Empirically verified
    (2026-08-14, this repo's own git): git's pathspec matching itself is
    ALWAYS case-sensitive regardless of `core.ignorecase` -- the hazard is
    not "the primary ls-files/ls-tree query matches under the wrong case",
    it is that `git add -- file.txt` silently resolves the file THROUGH the
    case-insensitive FILESYSTEM and reuses the existing `File.txt` index
    entry (git's own collision-avoidance), while the primary CAS-snapshot
    query still (correctly, case-sensitively) sees nothing for `file.txt`
    and would read `None == None` without the `:(icase)` rescan this fix
    adds. On a case-sensitive filesystem, `file.txt` never resolves to
    `File.txt` on disk at all -- `git add -- file.txt` fails outright with
    "pathspec did not match any files", so the hazard cannot arise; skip
    there rather than false-failing.

    Exercises `git_native.commit_scoped()` DIRECTLY, not through the
    `scoped-git-commit` op boundary (`_call`, every sibling test's own
    helper): `scoped_git_commit.py::_classify_uncommitted` /
    `_commit_paths_are_clean` (commit_pipeline.py, outside this chunk's own
    file scope) reclassify a refused commit as a benign idempotent no-op by
    probing `git status --porcelain -- file.txt` -- a probe that is ITSELF
    case-sensitive and finds nothing dirty (the real dirty entity is
    `File.txt`, never touched), so it silently swallows this exact refusal
    at the op layer (verified empirically, 2026-08-14 -- reported upstream
    as a live escalation, not fixed here: out of this file's own scope).
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "File.txt", "seed\n")
    _git(["add", "--", "File.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    if not (repo / "file.txt").exists():
        pytest.skip(
            "filesystem here is case-sensitive -- 'file.txt' does not "
            "resolve to the tracked 'File.txt' on disk, so the case-"
            "divergent add hazard cannot arise"
        )

    msg_file = repo / "msg.txt"
    msg_file.write_text("case-divergent commit attempt\n", encoding="utf-8")
    result = git_native.commit_scoped(["file.txt"], str(msg_file), str(repo))

    assert result.ok is False
    assert "could not be matched" in result.stderr.lower()


def test_op_surfaces_case_divergent_cas_refusal_as_a_failure_not_a_noop(tmp_path):
    """The escalation this dispatch exists to close (state/subagent-share/
    ca848831-1b10-4515-8203-5a0bade9ff0d/coordinatorreview-integrator-
    1993e652.md, "A third, out-of-scope discovery"): `git_native.
    commit_scoped()`'s CAS refusal for a case-divergent caller path (see the
    sibling test above, which exercises `commit_scoped()` directly) was
    being reclassified as a benign already-committed no-op by THIS op's own
    `_classify_uncommitted()` — its `reclassifiable` check accepted ANY
    non-zero `commit` exit code, not just git's own `1` ("nothing to
    commit"), so `commit_scoped()`'s deliberately-distinct `returncode=-1`
    refusal sentinel was swept into the same reclassification branch and
    then rubber-stamped "clean" by `_commit_paths_are_clean()`'s own
    case-sensitive `git status --porcelain -- file.txt` probe (which never
    sees `File.txt`'s real divergence).

    Portable ONLY on a case-insensitive filesystem — same skip condition as
    the sibling `git_native`-level test.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "File.txt", "seed\n")
    _git(["add", "--", "File.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    if not (repo / "file.txt").exists():
        pytest.skip(
            "filesystem here is case-sensitive -- 'file.txt' does not "
            "resolve to the tracked 'File.txt' on disk, so the case-"
            "divergent add hazard cannot arise"
        )

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["file.txt"],
            "message": "case-divergent commit attempt",
        }
    )

    # Never rendered as the benign no-op shape (`reason ==
    # "empty-commit-set"`) -- a real, diagnostic-bearing refusal.
    assert result["committed"] is False
    assert result.get("reason") != "empty-commit-set"
    assert result.get("commit_failed") is True
    assert any(
        "could not be matched" in str(d).lower() or "compare-and-swap" in str(d).lower()
        for d in result.get("diagnostics", [])
    )

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_after == head_before


def test_diverged_path_reports_worktree_excluded_at_the_op_boundary(tmp_path):
    """P1 fix (state/bug-backlog/2026-08-10-scoped-git-commit-reports-
    success-while-334e90d707f9.yaml): the private-index branch's success
    threads `worktree_excluded` all the way out to the op's own response,
    naming the excluded path -- not just to `GitResult`/`CommitOutcome`."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "file.txt", "STAGED\n")
    _git(["add", "--", "file.txt"], repo)
    _seed_file(repo, "file.txt", "WORKTREE\n")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["file.txt"],
            "message": "diverged: commit staged",
        }
    )

    assert result["committed"] is True
    assert result["worktree_excluded"] == ["file.txt"]
    assert "file.txt" in result["worktree_excluded_warning"]
    assert _committed_files_at_head(repo) == ["file.txt"]


def test_agree_branch_reports_no_worktree_excluded_key(tmp_path):
    """Silence on the clean path: the response carries no
    `worktree_excluded` key at all when index and worktree agree."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["tasks/feature/todo.md"],
            "message": "add feature todo",
        }
    )

    assert result["committed"] is True
    assert "worktree_excluded" not in result
    assert "worktree_excluded_warning" not in result


def test_agree_branch_commit_carries_session_id_trailer_via_real_hook(tmp_path, monkeypatch):
    """Regression: the AGREE branch (a plain `git commit -F`, `commit_with_
    message_file` — the non-diverged path `commit_scoped()` selects when
    index and worktree already agree) relies on the REAL `prepare-commit-msg`
    git hook firing to stamp `Session-Id:`/`Deliverable-Id:` trailers (see
    `git_native.py`'s own module docstring, "the `prepare-commit-msg` hook
    that stamps Session-Id/Deliverable-Id on every ordinary `git commit`").
    Unlike the PRIVATE-INDEX branch (`_commit_scoped_private_index`, plumbing
    — `commit-tree`/`update-ref` — which computes trailers itself in Python
    via `compute_missing_trailer_args`; hooks never fire for plumbing), the
    agree branch has NO Python-side trailer fallback: if the script the
    installed `.git/hooks/prepare-commit-msg` shim execs is missing from
    disk, the shim silently warns on stderr and exits 0 — the commit lands,
    with no Session-Id trailer, and no error surfaces anywhere in this op's
    response.

    2026-08-14 live incident: `coordinator/bin/coordinator-prepare-commit-
    msg` (the extensionless script the installed shim execs) was deleted
    from the working tree as uncommitted WIP for an in-flight extensionless
    -> `.py` rename, leaving the installed shim (which only probed the
    extensionless name) unable to find it. Every AGREE-branch commit landed
    with no `Session-Id:` trailer; every PRIVATE-INDEX-branch commit (a
    caller with a partially-staged/diverged path) kept its trailer, because
    that branch never depends on the hook or the file the hook execs at all
    — explaining the non-monotonic (not a clean time-cutover) trailer-
    presence pattern observed across nearby commits on the shared branch.

    This installs a MINIMAL, self-contained stand-in hook (never the live
    repo's own `.git/hooks/prepare-commit-msg` or `coordinator/bin/` tree —
    this test must never depend on, or be broken by, either) that replays
    the identical `compute_missing_trailer_args` call the real script makes,
    so an agree-branch commit through this op is asserted, end to end, to
    carry a `Session-Id:` trailer — the exact assertion that had no test
    before this incident.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # `git commit`'s hook subprocess starts with a bare `sys.path` (no
    # inherited PYTHONPATH), so it cannot find `coordinator_core` on its own
    # the way this TEST process can (pytest's own rootdir insertion) —
    # inserted explicitly here, same shape the real
    # `coordinator-prepare-commit-msg` script's own `_ensure_claude_klabauter_on_
    # syspath()` bootstrap performs via `cc_invoke`, just derived from this
    # test module's own file location rather than a hardcoded path.
    claude_klabauter_root = Path(__file__).resolve().parents[4]
    hook_path = repo / ".git" / "hooks" / "prepare-commit-msg"
    hook_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(claude_klabauter_root)!r})\n"
        "from coordinator_core.git.commit_trailers import compute_missing_trailer_args\n"
        "from coordinator_core.ops.ceremony import git_native\n"
        "args = compute_missing_trailer_args(sys.argv[1], '.')\n"
        "if args:\n"
        "    git_native._git(['interpret-trailers', '--in-place', *args, sys.argv[1]], cwd='.')\n",
        encoding="utf-8",
    )
    hook_path.chmod(0o755)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    session_id = "50826754-75f7-40b2-a787-e59c70a43e90"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", session_id)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["tasks/feature/todo.md"],
            "message": "add feature todo",
        }
    )

    assert result["committed"] is True
    assert "worktree_excluded" not in result  # confirms the AGREE branch ran

    message = subprocess.run(
        ["git", "log", "-1", "--format=%B", result["sha"]],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert f"Session-Id: {session_id}" in message


def test_agree_branch_commit_carries_session_id_trailer_with_no_hook_installed(tmp_path, monkeypatch):
    """Regression, the actual defect the sibling `..._via_real_hook` test
    above cannot catch (2026-08-14 cross-repo memo,
    `cross-repo/inbox/2026-08-14-doe-claude-em-scoped-git-commit-drops-
    session-id-trailer.md`; fix landed in `git_native.py`'s agree branch,
    the `if not diverged:` block in `commit_scoped()`).

    The sibling test installs a stand-in `prepare-commit-msg` hook and
    therefore only proves the AGREE branch still works when a hook fires —
    it was already passing before this fix, because the live incident was
    never "the hook computes the wrong trailer", it was "the hook, or the
    script it execs, sometimes does not fire at all, and the branch had no
    fallback of its own". This test installs NO `prepare-commit-msg` hook
    whatsoever (no file under `.git/hooks/` for that name) — the exact
    shape of a hook non-fire, whatever its proximate cause (missing shim
    target, no python on PATH, `core.hooksPath` override, non-executable
    hook, a future rename) — and asserts the trailer still lands, because
    `commit_scoped()`'s agree branch now calls `compute_missing_trailer_args`
    itself before committing, mirroring `_commit_scoped_private_index`'s
    own hook-independent trailer computation rather than trusting a hook
    that may silently not run.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    hook_path = repo / ".git" / "hooks" / "prepare-commit-msg"
    assert not hook_path.exists()

    _seed_file(repo, "tasks/feature/todo.md", "content")

    session_id = "6f1c1e1a-2b3c-4d5e-8f9a-0b1c2d3e4f5a"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", session_id)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["tasks/feature/todo.md"],
            "message": "add feature todo",
        }
    )

    assert result["committed"] is True
    assert "worktree_excluded" not in result  # confirms the AGREE branch ran

    message = subprocess.run(
        ["git", "log", "-1", "--format=%B", result["sha"]],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert f"Session-Id: {session_id}" in message
    assert message.count("Session-Id:") == 1


def test_agree_branch_session_id_trailer_not_duplicated_when_hook_also_stamps(tmp_path, monkeypatch):
    """Idempotency contract, end to end (module docstring item 5,
    `coordinator_core.git.commit_trailers`: "Session-Id and Deliverable-Id
    have INDEPENDENT idempotency checks"). Now that the AGREE branch itself
    pre-composes `Session-Id:` into `msg_file` before `commit_with_message_
    file` runs (this fix), a subsequently-firing `prepare-commit-msg` hook
    must see the line already present via its own `need_session_id = not
    _has_trailer_line(commit_msg_file, "Session-Id:")` check and skip
    re-stamping — the same contract the pre-existing `deliverable_id`
    pre-compose already relied on (`git_native.py`'s own comment, "LOAD-
    BEARING, not incidental"). Reuses the sibling `..._via_real_hook`
    test's minimal self-contained stand-in hook (never the live repo's
    `.git/hooks/` or `coordinator/bin/` tree).
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    claude_klabauter_root = Path(__file__).resolve().parents[4]
    hook_path = repo / ".git" / "hooks" / "prepare-commit-msg"
    hook_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(claude_klabauter_root)!r})\n"
        "from coordinator_core.git.commit_trailers import compute_missing_trailer_args\n"
        "from coordinator_core.ops.ceremony import git_native\n"
        "args = compute_missing_trailer_args(sys.argv[1], '.')\n"
        "if args:\n"
        "    git_native._git(['interpret-trailers', '--in-place', *args, sys.argv[1]], cwd='.')\n",
        encoding="utf-8",
    )
    hook_path.chmod(0o755)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    session_id = "9a8b7c6d-5e4f-4a3b-9c8d-7e6f5a4b3c2d"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", session_id)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["tasks/feature/todo.md"],
            "message": "add feature todo",
        }
    )

    assert result["committed"] is True

    message = subprocess.run(
        ["git", "log", "-1", "--format=%B", result["sha"]],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert message.count(f"Session-Id: {session_id}") == 1
    assert message.count("Session-Id:") == 1


def test_handler_attributes_each_commit_to_its_own_invoker_session_id(tmp_path, monkeypatch):
    """End-to-end pin for the full `_handler -> run_commit_pipeline ->
    commit() -> commit_scoped()` chain (c425e181fa1a, `attributed_session_id`
    threading; code-reviewer warn on that landed diff -- the prior tests in
    this file and in `test_commit_scoped.py` pin `session_id_override` at the
    `commit_trailers` layer and `attributed_session_id` at the `git_native.
    commit_scoped()` layer directly, but nothing before this test drove the
    op HANDLER itself, which is exactly where the original defect
    manifested: `owner_session_id` was resolved correctly in `_handler` and
    then simply never threaded down. `attributed_session_id=owner_session_id`
    is currently a single keyword pass-through at `_handler`'s own
    `run_commit_pipeline(...)` call site (see that call, and `commit()`'s own
    forwarding into `commit_scoped()`) -- pinned by nothing until this test,
    so a rename or a dropped kwarg anywhere in that chain would regress
    silently.

    Two sessions, overlapping dirty paths (both `a.txt` and `b.txt` are
    dirty on disk for BOTH calls below -- the shape the incident needs: an
    ambient env fallback that leaked in for either call would pick up the
    SAME wrong identity for both, not two correctly-differing ones). The
    ambient env is deliberately set to a THIRD, wrong identity that must
    never appear in either trailer. Each call commits only its own explicit
    pathspec via `params["session_id"]` (never the ambient env), and each
    landed commit's `Session-Id:` trailer names its own invoker only.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setenv("CLAUDE_SESSION_ID", "00000000-0000-0000-0000-000000000000")

    session_a = "903044ef-72b9-4549-a3df-6300e10b6b84"
    session_b = "e77424be-b452-43bd-a995-e12d60168cb6"

    _seed_file(repo, "a.txt", "from A\n")
    _seed_file(repo, "b.txt", "from B\n")

    result_a = _call(
        {
            "worktree_root": str(repo),
            "paths": ["a.txt"],
            "message": "commit from session A",
            "session_id": session_a,
        }
    )
    assert result_a["committed"] is True
    message_a = subprocess.run(
        ["git", "log", "-1", "--format=%B", result_a["sha"]],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert f"Session-Id: {session_a}" in message_a
    assert session_b not in message_a
    assert "00000000-0000-0000-0000-000000000000" not in message_a

    result_b = _call(
        {
            "worktree_root": str(repo),
            "paths": ["b.txt"],
            "message": "commit from session B",
            "session_id": session_b,
        }
    )
    assert result_b["committed"] is True
    message_b = subprocess.run(
        ["git", "log", "-1", "--format=%B", result_b["sha"]],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert f"Session-Id: {session_b}" in message_b
    assert session_a not in message_b
    assert "00000000-0000-0000-0000-000000000000" not in message_b


def test_agree_branch_explicit_deliverable_id_wins_and_appears_once(tmp_path, monkeypatch):
    """C7a precedence, now sharing this fix's `trailer_args` list with the
    Session-Id computation (`_drop_trailer_arg`-then-append shape, mirroring
    `_commit_scoped_private_index`'s own use of the same pattern a few lines
    below in `git_native.py`). An explicit `deliverable_id` parameter must
    still win over anything `compute_missing_trailer_args` would infer from
    the committed artifact's own frontmatter, and must land exactly once —
    no duplicate `Deliverable-Id:` line from the two trailer sources
    (this fix's `compute_missing_trailer_args` call and the pre-existing
    explicit-parameter block) landing in the same message.
    """
    repo = _init_repo(tmp_path)
    _seed_deliverable_artifact(repo, "dlv-inferred00", slug="inferred-plan")
    _seed_deliverable_artifact(repo, "dlv-explicit01", slug="explicit-plan")
    _seed_file(repo, "a.md", "content")

    session_id = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d"
    monkeypatch.setenv("COORDINATOR_SESSION_ID", session_id)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["a.md", "docs/plans/inferred-plan.md"],
            "message": "subject",
            "deliverable_id": "dlv-explicit01",
        }
    )

    assert result["committed"] is True

    message = subprocess.run(
        ["git", "log", "-1", "--format=%B", result["sha"]],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert message.count("Deliverable-Id: dlv-explicit01") == 1
    assert message.count("Deliverable-Id:") == 1
    assert f"Session-Id: {session_id}" in message


def test_missing_worktree_root_param_errors():
    result = _call({"paths": ["a.md"], "message": "m"})
    assert result["committed"] is False
    assert result["sha"] is None
    assert "worktree_root" in result["error"]


def test_missing_paths_param_errors():
    result = _call({"worktree_root": "/tmp/nonexistent", "message": "m"})
    assert result["committed"] is False
    assert "paths" in result["error"]


def test_missing_message_param_errors():
    result = _call({"worktree_root": "/tmp/nonexistent", "paths": ["a.md"]})
    assert result["committed"] is False
    assert "message" in result["error"]


# ---------------------------------------------------------------------------
# `deliverable_id` -- C7a (AC14), docs/plans/2026-08-10-a-commit-trailer-
# that-names-the-session.md. The CLI/op-schema round-trip: `--deliverable-id
# <id>` on the CLI parses into `params["deliverable_id"]`, and the op's own
# schema validates (accepts a string, rejects a non-string) without erroring
# on an otherwise-valid call. Full end-to-end threading to `commit_scoped()`
# is C7b's job -- see `_handler`'s own docstring for the current boundary;
# not exercised here (out of this chunk's scope).
# ---------------------------------------------------------------------------


def _load_cli_module():
    """Import the extension-less `scoped-git-commit` CLI as a module, purely
    to test `_parse_args`'s own `--deliverable-id` grammar -- mirrors
    `coordinator/bin/tests/test_scoped_git_commit_cli.py::_load_cli_module`
    (that file is a peer's test surface, out of this chunk's scope to edit;
    this is a same-shaped, independent loader inside this file, not an edit
    to that one)."""
    import importlib.machinery
    import importlib.util

    cli_path = (
        Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "scoped-git-commit"
    )
    loader = importlib.machinery.SourceFileLoader("scoped_git_commit_cli_ac14", str(cli_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_cli_parses_deliverable_id_flag_into_params_shape():
    cli = _load_cli_module()
    (
        subject, repo, paths, as_json, include_orphans, deliverable_id, _stage_patch,
        mangled_cr_paths,
    ) = cli._parse_args(["-m", "subject", "--deliverable-id", "dlv-abc123", "--", "a.md"])
    assert deliverable_id == "dlv-abc123"
    assert subject == "subject"
    assert paths == ["a.md"]
    assert mangled_cr_paths == []


def test_cli_omits_deliverable_id_when_not_given():
    cli = _load_cli_module()
    (
        _subject, _repo, _paths, _as_json, _include_orphans, deliverable_id, _stage_patch,
        _mangled_cr_paths,
    ) = cli._parse_args(["-m", "subject", "--", "a.md"])
    assert deliverable_id is None


def _seed_deliverable_artifact(repo: Path, deliverable_id: str, *, slug: str = "seed-plan") -> Path:
    """Mirrors `test_commit_scoped.py::_seed_deliverable_artifact` locally --
    same shape, independent copy (not imported across test modules; see
    `_load_cli_module`'s docstring above for why this file keeps its own
    fixtures rather than reaching into a peer test module)."""
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{slug}.md"
    path.write_text(f"---\ndeliverable_id: {deliverable_id}\n---\n\n# seed plan\n", encoding="utf-8")
    return path


def test_op_schema_accepts_string_deliverable_id(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a.md", "content")
    _seed_deliverable_artifact(repo, "dlv-abc123")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["a.md"],
            "message": "subject",
            "deliverable_id": "dlv-abc123",
        }
    )

    assert result["committed"] is True
    assert "error" not in result


def test_deliverable_id_reaches_run_commit_pipeline(tmp_path, monkeypatch):
    """C7b -- the CLI/schema round-trip above stops at "accepted"; this closes
    the gap by asserting the value actually reaches `run_commit_pipeline`,
    not merely that the op doesn't reject it. A spy on `run_commit_pipeline`
    is used rather than a full end-to-end artifact-seeded commit (see
    `test_commit_scoped.py::_seed_deliverable_artifact`) because the point
    here is pinning THIS handler's forwarding, not re-testing
    `commit_scoped`'s own resolution/validation of the id, which belongs to
    `test_commit_scoped.py`.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a.md", "content")

    captured_kwargs = {}

    def _fake_pipeline(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return SimpleNamespace(
            committed_sha="deadbeef",
            pushed=None,
            push_status=scoped_git_commit.PUSH_STATUS_NO_REMOTE,
            commit_failed=False,
            integrity_breach=False,
            diagnostics=[],
        )

    monkeypatch.setattr(scoped_git_commit, "run_commit_pipeline", _fake_pipeline)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["a.md"],
            "message": "subject",
            "deliverable_id": "dlv-abc123",
        }
    )

    assert result["committed"] is True
    assert captured_kwargs.get("deliverable_id") == "dlv-abc123"


def test_op_schema_rejects_non_string_deliverable_id():
    result = _call(
        {
            "worktree_root": "/tmp/nonexistent",
            "paths": ["a.md"],
            "message": "m",
            "deliverable_id": 12345,
        }
    )
    assert result["committed"] is False
    assert "deliverable_id" in result["error"]


def test_message_file_path_is_rejected_not_committed_verbatim(tmp_path):
    """Regression for `fdbff578b7dc` / `40bf1064a124` (doe-claude-em FYI memo,
    2026-08-04): a caller composed the message in a scratchpad file and passed
    that file's PATH as `message`, expecting `-F` semantics. The op committed
    the path as the subject line and the body was lost — silently, and only
    legible once someone read `git log` for the rationale that no longer
    existed. Both an absolute path and an existing relative one are refused
    before any staging happens.
    """
    msg_file = tmp_path / "wave13-commit-msg.txt"
    msg_file.write_text("real subject\n\nreal body\n", encoding="utf-8")

    result = _call(
        {"worktree_root": str(tmp_path), "paths": ["a.md"], "message": str(msg_file)}
    )
    assert result["committed"] is False
    assert result["sha"] is None
    assert "file path" in result["error"]
    assert "prose" in result["error"]


@pytest.mark.parametrize(
    "subject",
    [
        "fix(ops/ceremony): reject a path-shaped commit subject",
        "review slice 2 integration: 'could not determine' stops masquerading",
        "docs/reference/test-tiers.md rewritten for the marker split",
    ],
)
def test_ordinary_subjects_are_not_mistaken_for_paths(subject):
    """The path-shaped guard must never fire on a real subject — including
    conventional-commit scopes and subjects that name a file inline."""
    assert scoped_git_commit._reject_path_shaped_message(subject) is None


def test_directory_pathspec_rejection_reaches_the_caller(tmp_path):
    """A directory pathspec is now refused BEFORE anything is ever staged
    (session fb5fa766, 2026-07-31 incident, closed 2026-07-31) —
    `run_commit_pipeline`'s pre-stage guard rejects it ahead of
    `explicit_stage()`, reusing `commit_scoped()`'s own predicate/wording
    (`git_native.directory_pathspecs()` / `directory_pathspec_diagnostic()`).

    Previously `explicit_stage()` ran `git add -- notes/` FIRST, and only
    `commit_scoped()`, further down the pipeline, refused the directory
    pathspec — by which point the directory was already staged. That staged
    residue then survived `run_commit_pipeline`'s post-stage rollback by
    design: `git_native.reset_paths()` deliberately DROPS any directory
    entry from its own pathspec rather than reset one (a directory pathspec
    matches whatever is CURRENTLY inside it at reset time, not just what
    this call staged — the same hazard the directory refusal itself exists
    for, one layer up). The fix closes that gap at the source: refusing
    before the `git add` ever runs means there is no staged residue for the
    rollback to have missed in the first place.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "content")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/"],  # directory, not a file
            "message": "add notes",
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    # The point of the test: the rejection is legible through the op boundary.
    assert result["commit_failed"] is True
    assert any("directory pathspec" in d for d in result["diagnostics"])
    # Not an empty-commit-set: this is a real failure, not a benign no-op.
    assert "reason" not in result
    # And nothing was ever staged for it — the pre-stage guard refuses before
    # `explicit_stage()` runs, so there is no residue to leave behind.
    assert _porcelain(repo) == ["?? notes/"]


def test_successful_commit_response_stays_thin(tmp_path):
    """The green path stays a thin fixed shape — the FAILURE-diagnostic keys
    (`commit_failed`/`diagnostics`/`reason`) are added only where a caller
    would otherwise be blind, and never leak onto the success path.

    `push_state` is part of that fixed shape rather than an exception to it:
    it is the answer to "did this commit publish", which every success-path
    caller already asks via `pushed` and could not previously get a truthful
    three-valued answer to.

    `ownership_gate` was part of that fixed shape until the 2026-08-08
    ownership-gate excision removed the gate it reported on; the success
    response is back to the thin four-key shape.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "content")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "add notes",
        }
    )

    assert result["committed"] is True
    assert set(result) == {"committed", "sha", "pushed", "push_state"}


def test_commit_spawn_stands_the_other_publisher_down(tmp_path, monkeypatch):
    """opro-01 C-01: the 2026-07-30 false negative cannot return, because the
    thing that caused it no longer runs.

    This test replaces `test_no_false_integrity_breach_when_something_else_
    pushed`, which pinned the SUPPRESSION of the symptom: it forced
    `pushed=False`/`integrity_breach=True` for a sha genuinely on the remote
    and asserted `_remote_sha_state` corrected the verdict. That probe is gone
    (C-02), and a test asserting a removed correction would just be deleted
    coverage. The invariant that replaces it is upstream of the symptom:
    `git commit` must carry the marker that stands the post-commit hook's own
    detached push down, so no second publisher exists to lose a race to.

    Asserted against the real `git commit` spawn's env rather than the flag on
    the way in -- the flag being threaded is not the same fact as it reaching
    git, and only the latter prevents the incident.
    """
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "notes/alpha.md", "content")

    commit_envs = []
    orig = git_native._git

    def _spy(args, **kw):
        if args and args[0] == "commit":
            commit_envs.append(kw.get("env"))
        return orig(args, **kw)

    monkeypatch.setattr(git_native, "_git", _spy)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "add notes",
            # EXPLICIT since the op's default became `deferred` (2026-08-19
            # op-tail-latency): the sole-publisher marker is correct ONLY when
            # this op is itself the publisher. Under `deferred` the hook is the
            # publisher and the marker must be ABSENT -- pinned by
            # `test_deferred_push_leaves_the_hook_as_publisher` below. This
            # test owns the sync arm of that pair.
            "push_mode": "sync",
        }
    )

    assert result["committed"] is True
    assert commit_envs, "no `git commit` spawn observed -- fixture did not commit"
    for env in commit_envs:
        assert env is not None and env.get(git_native._AUTO_PUSH_SUPPRESS_ENV) == "1", (
            "the commit spawn did not carry the stand-down marker: the "
            "post-commit hook will detach and push, racing this op's own "
            "push, and a landed commit can again render as PUSH FAILED"
        )


def test_landed_but_sha_unverified_reports_committed_true_not_false(tmp_path, monkeypatch):
    """W3 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md):
    a commit that LANDED but whose sha could not be resolved
    (`PipelineResult.sha_unverified=True`) must render `committed: True` --
    the previous `result.committed_sha is not None` predicate alone would
    report `committed: False` here (`committed_sha` is correctly `None`,
    only ITS PRESENCE was ever the signal), which is the exact "landed
    commit reported as failed" bug this plan closes.

    Mechanism check (not just the assertion): reverting the fix -- i.e.
    `"committed": result.committed_sha is not None` alone, without the `or
    result.sha_unverified` widening -- makes `result["committed"]` False
    here, failing this test's very first assertion. Verified by hand:
    temporarily restoring the old expression and re-running this test
    reproduces a fail (`assert False is True`) before the fix; restored
    immediately after.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "content")

    fake_result = SimpleNamespace(
        committed_sha=None,
        pushed=None,
        push_status=scoped_git_commit.PUSH_STATUS_NOT_ATTEMPTED,
        commit_failed=False,
        integrity_breach=False,
        sha_unverified=True,
        diagnostics=[
            "commit: landed but sha verification failed -- HEAD unresolvable"
        ],
    )
    monkeypatch.setattr(scoped_git_commit, "run_commit_pipeline", lambda *a, **k: fake_result)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "add notes",
        }
    )

    assert result["committed"] is True
    assert result["sha"] is None
    assert result.get("sha_unverified") is True
    assert result.get("diagnostics")
    # Never the failure shape -- this is not "nothing landed".
    assert "commit_failed" not in result
    assert result.get("reason") != "empty-commit-set"


def test_landed_via_commit_outcome_reports_committed_not_empty_commit_set(
    tmp_path, monkeypatch
):
    """W3b (2026-08-19): the sibling of the W3 case above, for the landed-but-
    no-sha shapes W3 did NOT cover.

    `CommitOutcome.landed` is True on every path where `commit_scoped()`
    succeeded -- including the post-success verification failures that are
    NOT `sha_unverified` (empty message subject; zero-or-ambiguous
    commit-token match). That flag is not mirrored onto `PipelineResult`, so
    before this fix the `committed` predicate could not see it and rendered
    `committed: False` over a commit that exists in history. The operator-
    visible damage is the SECOND-ORDER effect, which is why this test asserts
    on `reason`: a `committed: False` response falls through to
    `_classify_uncommitted`, which probes `git status`, finds the tree clean
    BECAUSE the commit landed, and returns the benign `"empty-commit-set"` --
    so the CLI tells the operator "no commit landed" about a commit that did.
    A caller trusting that either redoes the work or reports it lost.

    Mechanism check: dropping the `getattr(result.commit, "landed", ...)`
    disjunct from the predicate makes `committed` False here and flips
    `reason` to `"empty-commit-set"`, failing the first and last assertions.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # The shape the git layer returns when the commit landed but the token
    # match was ambiguous: landed=True, no resolvable sha, and -- crucially --
    # sha_unverified False, so W3's widening does not fire.
    fake_result = SimpleNamespace(
        committed_sha=None,
        pushed=None,
        push_status=scoped_git_commit.PUSH_STATUS_NOT_ATTEMPTED,
        commit_failed=False,
        integrity_breach=False,
        sha_unverified=False,
        commit=SimpleNamespace(landed=True),
        diagnostics=["commit: landed but commit-token match was ambiguous"],
    )
    monkeypatch.setattr(
        scoped_git_commit, "run_commit_pipeline", lambda *a, **k: fake_result
    )

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["README.md"],
            "message": "add notes",
        }
    )

    assert result["committed"] is True
    assert result["sha"] is None
    # The operator-facing failure this closes: never "nothing landed".
    assert result.get("reason") != "empty-commit-set"


def test_genuinely_uncommitted_still_reports_empty_commit_set(tmp_path, monkeypatch):
    """Negative half of W3b -- the widening must not swallow the real no-op.

    A pipeline result carrying `commit=None` (no commit was attempted or the
    commit genuinely failed) must still render `committed: False` and reach
    the ordinary `empty-commit-set` classification. Without this, the fix
    above would convert every benign already-committed no-op into a phantom
    success, which is a strictly worse defect than the one it repairs.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    fake_result = SimpleNamespace(
        committed_sha=None,
        pushed=None,
        push_status=scoped_git_commit.PUSH_STATUS_NOT_ATTEMPTED,
        commit_failed=False,
        integrity_breach=False,
        sha_unverified=False,
        commit=None,
        diagnostics=[],
    )
    monkeypatch.setattr(
        scoped_git_commit, "run_commit_pipeline", lambda *a, **k: fake_result
    )

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["README.md"],
            "message": "add notes",
        }
    )

    assert result["committed"] is False


def _commit_and_fake_pipeline(
    tmp_path, monkeypatch, *, with_remote: bool, push_status=None
):
    """Land a real local commit, then make the pipeline claim `pushed=False`
    for it.

    `push_status` defaults to `NOT_ATTEMPTED` (the unknown rung). Pass
    `PUSH_STATUS_FAILED` for the genuinely-failed-publish shape: since C-02
    removed the remote probe, that status is what distinguishes a real failure
    from an unknown one, where the probe's verdict used to.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    if with_remote:
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        _git(["remote", "add", "origin", str(origin)], repo)
        _git(["push", "-q", "-u", "origin", "HEAD"], repo)

    _seed_file(repo, "notes/alpha.md", "content")
    _git(["add", "--", "notes/alpha.md"], repo)
    _git(["commit", "-q", "-m", "add notes"], repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    monkeypatch.setattr(
        scoped_git_commit,
        "run_commit_pipeline",
        lambda *a, **k: SimpleNamespace(
            committed_sha=sha,
            pushed=False,
            push_status=push_status or scoped_git_commit.PUSH_STATUS_NOT_ATTEMPTED,
            commit_failed=False,
            integrity_breach=True,
            diagnostics=[],
        ),
    )
    return repo, sha


def test_genuinely_unpushed_commit_still_reports_failure_and_breach(tmp_path, monkeypatch):
    """The tri-state must not soften a REAL publish failure into "unconfirmed"
    — that would trade the memo's false negative for a false positive and
    destroy the only signal that says a push actually failed.
    """
    repo, sha = _commit_and_fake_pipeline(
        tmp_path, monkeypatch, with_remote=True,
        push_status=scoped_git_commit.PUSH_STATUS_FAILED,
    )

    result = _call(
        {"worktree_root": str(repo), "paths": ["notes/alpha.md"], "message": "add notes"}
    )

    assert result["pushed"] is False
    assert result["push_state"] == scoped_git_commit.PUSH_STATE_FAILED
    assert result["integrity_breach"] is True


def test_unreadable_remote_reports_unconfirmed_never_failure(tmp_path, monkeypatch):
    """The third state: the commit landed, the remote could not be read, and
    the honest answer is "unknown". Rendering this as a failed push is what
    invites the dangerous correction (re-push / amend / force-push) on an
    auto-push-armed shared branch — so it must be neither `pushed=False` nor
    an `integrity_breach`.
    """
    repo, _sha = _commit_and_fake_pipeline(tmp_path, monkeypatch, with_remote=False)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "add notes",
            # EXPLICIT `sync` (2026-08-19 op-tail-latency): this test's subject
            # is the UNKNOWN rung -- a push was attempted and its outcome could
            # not be read -- which only the sync path can produce. Under the
            # new `deferred` default no push is attempted at all, so the honest
            # report there is `deferred`/`no-remote`, not `unconfirmed`; that
            # arm is pinned by `test_deferred_push_reports_deferred_not_
            # unconfirmed`. Leaving this call on the default would have
            # silently retargeted the test at a different state.
            "push_mode": "sync",
        }
    )

    assert result["committed"] is True
    assert result["pushed"] is None
    assert result["push_state"] == scoped_git_commit.PUSH_STATE_UNCONFIRMED
    assert "integrity_breach" not in result


def test_deferred_push_leaves_the_hook_as_publisher(tmp_path, monkeypatch):
    """The invariant that makes deferral safe: SOMEBODY still publishes.

    `push_mode="deferred"` (this op's default since 2026-08-19) skips the
    inline push to buy back 1.3-4.9s of network round trip. That is only
    correct if the post-commit hook then does publish -- which it does by the
    ABSENCE of the sole-publisher suppression marker in the `git commit`
    child's env. If a future change ever set that marker unconditionally,
    deferral would silently become "nobody pushes", and the commit would sit
    local forever with the op reporting a cheerful "queued for background
    push". Asserted against the real spawn's env for the same reason the sync
    twin (`test_commit_spawn_stands_the_other_publisher_down`) is: the flag
    being threaded is not the same fact as it reaching git.
    """
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "notes/alpha.md", "content")

    commit_envs = []
    orig = git_native._git

    def _spy(args, **kw):
        if args and args[0] == "commit":
            commit_envs.append(kw.get("env"))
        return orig(args, **kw)

    monkeypatch.setattr(git_native, "_git", _spy)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/alpha.md"],
            "message": "add notes",
        }
    )

    assert result["committed"] is True
    assert commit_envs, "no `git commit` spawn observed -- fixture drift"
    for env in commit_envs:
        if env is None:
            continue
        assert git_native._AUTO_PUSH_SUPPRESS_ENV not in env, (
            "deferred push stood the post-commit publisher down -- with the "
            "inline push already skipped, nothing would ever publish this commit"
        )


def test_deferred_push_publishes_the_hookless_private_index_branch(tmp_path, monkeypatch):
    """The sibling of `test_deferred_push_leaves_the_hook_as_publisher`, for
    the branch that has no hook to leave.

    Review finding (BLOCKED, 2026-08-19): that test spies for a `git commit`
    argv, which only `commit_scoped`'s AGREE branch spawns. A path that has
    diverged since the caller's last read routes through
    `_commit_scoped_private_index` instead, which lands via `commit-tree`/
    `update-ref` and fires NO hooks at all -- so under the `deferred` default
    the pipeline skips its own push, no `post-commit` runs, and the commit is
    stranded local while the op cheerfully reports "queued for background
    push". Divergence is the ORDINARY case on a tree with 50-70 concurrent
    sessions, not an exotic one, which is what made this worth blocking on.

    Pins the replay, not the push: `_replay_post_commit_auto_push` invokes
    the hook exactly as git would and returns without waiting on the detached
    child, so asserting it was CALLED is the honest assertion here --
    asserting a push landed would require a remote and a network round trip
    this test has no business taking.
    """
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "diverge.py", "v1\n")
    _git(["add", "--", "diverge.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # Stage v2, then leave v3 in the worktree: staged content now differs from
    # worktree content, which is exactly what `diverging_paths` reports and
    # what routes `commit_scoped` down the private-index branch.
    _seed_file(repo, "diverge.py", "v2\n")
    _git(["add", "--", "diverge.py"], repo)
    _seed_file(repo, "diverge.py", "v3\n")

    replays = []
    monkeypatch.setattr(
        git_native,
        "_replay_post_commit_auto_push",
        lambda root: replays.append(str(root)),
    )
    commit_spawns = []
    orig = git_native._git

    def _spy(args, **kw):
        if args and args[0] == "commit":
            commit_spawns.append(list(args))
        return orig(args, **kw)

    monkeypatch.setattr(git_native, "_git", _spy)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["diverge.py"],
            "message": "commit a diverged path under the deferred default",
        }
    )

    assert result["committed"] is True, result
    assert not commit_spawns, (
        "fixture drift -- a `git commit` spawn means this took the AGREE branch, "
        "so it is re-testing the sibling case and proves nothing about the "
        "hookless path"
    )
    assert replays, (
        "a diverged-path commit landed under push_mode=deferred with no hook fired "
        "and no auto-push replay -- the commit is stranded local while the op "
        "reports push_state=deferred"
    )


def test_deferred_push_reports_deferred_not_unconfirmed(tmp_path, monkeypatch):
    """A deferred push is a KNOWN state, and must not borrow the unknown one's
    corrective note.

    `PUSH_STATE_UNCONFIRMED` renders as "re-check, do not re-push" -- correct
    when a push was attempted and its outcome could not be read, actively
    harmful as the standing note on every ordinary commit. The op therefore
    maps a non-sync `PUSH_STATUS_NOT_ATTEMPTED` to its own
    `PUSH_STATE_DEFERRED`. Guards the specific regression of collapsing the
    two states back together on the grounds that both carry `pushed=None`.
    """
    repo, _sha = _commit_and_fake_pipeline(tmp_path, monkeypatch, with_remote=True)

    result = _call(
        {"worktree_root": str(repo), "paths": ["notes/alpha.md"], "message": "add notes"}
    )

    assert result["committed"] is True
    assert result["pushed"] is None
    assert result["push_state"] == scoped_git_commit.PUSH_STATE_DEFERRED
    assert "integrity_breach" not in result


def test_deferred_push_still_reports_a_missing_remote(tmp_path, monkeypatch):
    """"Queued for background push" must not be said about a repo with no
    remote to push to -- the queue would never drain and the note would never
    come true. The deferred branch runs one LOCAL `git remote` probe to keep
    `no-remote` distinguishable; this pins that it is not lost to the cheaper
    unconditional "deferred".
    """
    repo, _sha = _commit_and_fake_pipeline(tmp_path, monkeypatch, with_remote=False)

    result = _call(
        {"worktree_root": str(repo), "paths": ["notes/alpha.md"], "message": "add notes"}
    )

    assert result["committed"] is True
    assert result["pushed"] is None
    assert result["push_state"] == scoped_git_commit.PUSH_STATE_NO_REMOTE


def test_push_subprocess_timeout_renders_unconfirmed_end_to_end(tmp_path, monkeypatch):
    """FIX-I (2026-08-19): a push subprocess TIMEOUT is a fourth, DISTINCT
    reason `push_status` can be the unknown rung -- the pipeline's own
    `PUSH_STATUS_UNCONFIRMED` (not the `NOT_ATTEMPTED` fallback the sibling
    test above exercises). Proves the whole chain end to end: the op maps
    it to `PUSH_STATE_UNCONFIRMED` here, and the CLI's `_render` (see
    `coordinator/bin/tests/test_scoped_git_commit_cli.py::
    test_unconfirmed_does_not_read_as_a_failure`) renders that value as
    prose that never reads as a failure -- never `PUSH_STATE_FAILED`,
    which is what this exact shape rendered as before this fix.
    """
    repo, _sha = _commit_and_fake_pipeline(
        tmp_path, monkeypatch, with_remote=True,
        push_status=scoped_git_commit.PUSH_STATUS_UNCONFIRMED,
    )

    result = _call(
        {"worktree_root": str(repo), "paths": ["notes/alpha.md"], "message": "add notes"}
    )

    assert result["committed"] is True
    assert result["pushed"] is None
    assert result["push_state"] == scoped_git_commit.PUSH_STATE_UNCONFIRMED
    assert "integrity_breach" not in result


def test_unstaged_deletion_named_in_pathspec_is_committed(tmp_path):
    """2026-08-04 fix, defect A -- THE primary live break: with a deletion
    NAMED in the pathspec (a plain `rm`, never staged), the op previously
    committed the OTHER paths, reported success, and silently dropped the
    deletion -- the file stayed in HEAD with no diagnostic naming why.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "notes/gone.md", "will be removed")
    _git(["add", "--", "notes/gone.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "notes" / "gone.md").unlink()

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/gone.md"],
            "message": "remove notes/gone.md",
        }
    )

    assert result["committed"] is True
    assert result["sha"]
    assert "error" not in result
    assert "declined_paths" not in result
    assert _committed_files_at_head(repo) == ["notes/gone.md"]
    head_files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "notes/gone.md" not in head_files


def test_deletion_and_modification_in_one_pathspec_both_land(tmp_path):
    """A deletion plus a modification named in the SAME pathspec -- both must
    land in one commit.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "notes/gone.md", "will be removed")
    _seed_file(repo, "notes/kept.md", "v1")
    _git(["add", "--", "notes/gone.md", "notes/kept.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "notes" / "gone.md").unlink()
    _seed_file(repo, "notes/kept.md", "v2")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/gone.md", "notes/kept.md"],
            "message": "remove gone, update kept",
        }
    )

    assert result["committed"] is True
    assert "declined_paths" not in result
    committed = sorted(_committed_files_at_head(repo))
    assert committed == ["notes/gone.md", "notes/kept.md"]
    head_files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "notes/gone.md" not in head_files
    assert "notes/kept.md" in head_files
    committed_blob = subprocess.run(
        ["git", "show", "HEAD:notes/kept.md"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert committed_blob == "v2"


def test_staged_deletion_named_in_pathspec_no_longer_reports_empty_commit_set(tmp_path):
    """2026-08-04 fix, defect B -- with the deletion staged FIRST (`git rm`),
    the op previously reported `empty-commit-set` (`reason` key present,
    `committed=False`) instead of committing the deletion the caller
    explicitly staged and named.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "notes/gone.md", "will be removed")
    _git(["add", "--", "notes/gone.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _git(["rm", "-q", "notes/gone.md"], repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/gone.md"],
            "message": "remove notes/gone.md (already staged)",
        }
    )

    assert result["committed"] is True
    assert "reason" not in result
    assert result["sha"]
    head_files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "notes/gone.md" not in head_files


def test_declined_path_is_named_with_reason_in_the_response(tmp_path):
    """Any path declined for any reason must be NAMED in the output, with a
    reason -- assert on the reported text, not just the verdict. Mixes one
    genuinely committable path with one that never existed at all (never
    tracked, never on disk, not a deletion of any kind) in the SAME
    pathspec, so the response must report BOTH: the commit succeeding for
    the real path, and the bogus one named with its own reason.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/real.md", "content")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["notes/real.md", "notes/never-existed.md"],
            "message": "add notes/real.md",
        }
    )

    assert result["committed"] is True
    assert _committed_files_at_head(repo) == ["notes/real.md"]
    assert "declined_paths" in result
    declined = result["declined_paths"]
    assert len(declined) == 1
    assert declined[0]["path"] == "notes/never-existed.md"
    assert declined[0]["reason"]






def test_post_commit_release_phantom_claims_clears_a_phantom_touch(tmp_path):
    """Wiring test for `session_scope.release_phantom_claims`, called
    alongside the pre-existing `release_committed_claims` on this op's
    post-commit path (state/bug-backlog/2026-08-06-a-phantom-touch-claim-
    from-an-interrupte-c21f5bbdd077.yaml).

    A session claims (`T`) a path it never actually creates on disk -- the
    residue of an interrupted `relocate_touched_path` call. That claim has
    no git-representable content anywhere, so nothing about a NORMAL commit
    of some other, unrelated path would ever retire it on its own; before
    this wiring it would re-surface in `compute_offer` for the rest of the
    session. This asserts the phantom is gone from `compute_offer`'s
    `safe_paths` once a commit lands for this session.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    sid = "phantom-claim-session"
    session_core.init(sid, cwd=str(repo))

    phantom_path = "phantom/never-created.md"
    session_scope.touch(sid, phantom_path, cwd=str(repo))
    assert not (repo / phantom_path).exists()

    _seed_file(repo, "real/work.md", "genuine content")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["real/work.md"],
            "message": "commit real work, alongside a stale phantom claim",
            "session_id": sid,
        }
    )
    assert result["committed"] is True

    offer = compute_offer(sid, cwd=str(repo))
    assert phantom_path not in offer["safe_paths"]


def test_directory_expands_to_dirty_tracked_files_and_commits(tmp_path):
    """A (2026-08-06 fix): a directory naming only in-scope, already-tracked
    dirty files just works -- expanded to its member files and committed,
    rather than tripping the hard directory-pathspec rejection.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "waivers/a.json", "v1")
    _seed_file(repo, "waivers/b.json", "v1")
    _git(["add", "--", "waivers/a.json", "waivers/b.json"], repo)
    _git(["commit", "-q", "-m", "seed waivers"], repo)

    _seed_file(repo, "waivers/a.json", "v2")
    _seed_file(repo, "waivers/b.json", "v2")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["waivers/"],
            "message": "rewrite waivers",
        }
    )

    assert result["committed"] is True
    assert "error" not in result
    assert sorted(_committed_files_at_head(repo)) == ["waivers/a.json", "waivers/b.json"]


def test_directory_expansion_never_sweeps_in_an_untracked_file(tmp_path):
    """Guard rail: a directory containing a dirty TRACKED file alongside an
    UNTRACKED one commits only the tracked file -- naming the parent
    directory must never launder the untracked file into the commit.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "waivers/tracked.json", "v1")
    _git(["add", "--", "waivers/tracked.json"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "waivers/tracked.json", "v2")
    _seed_file(repo, "waivers/untracked.json", "brand new")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["waivers/"],
            "message": "rewrite tracked waiver only",
        }
    )

    assert result["committed"] is True
    committed = _committed_files_at_head(repo)
    assert committed == ["waivers/tracked.json"]
    assert "waivers/untracked.json" not in committed
    # The untracked file is still sitting there, untouched, never staged.
    status = _porcelain(repo)
    assert any(line == "?? waivers/untracked.json" for line in status)














def test_deny_reason_names_a_holder_is_true_for_exactly_the_claimed_by_shapes():
    """The predicate itself, over every classification `_classify_denied_path`
    can return: true for the three claimed-by branch shapes, false for all
    five `_CLASSIFICATION_*` constants -- including the orphan one whose
    wording collides with the construction prefix."""
    holder_shapes = [
        "claimed by live session peer-1",
        (
            "claimed by session peer-1 (holder reads NOT live in this repo; "
            "NOT a licence to reap — re-verify the holder before any takeover)"
        ),
        "claimed by session unknown (claims unreadable: other) (liveness not checked)",
    ]
    non_holder_shapes = [
        scope_report._CLASSIFICATION_ORPHAN,
        scope_report._CLASSIFICATION_INCLUDE_ORPHANS_IGNORED,
        scope_report._CLASSIFICATION_INDETERMINATE,
        scope_report._CLASSIFICATION_UNCLASSIFIED,
        scope_report._CLASSIFICATION_ALREADY_CLEAN,
    ]

    for shape in holder_shapes:
        assert scope_report.deny_reason_names_a_holder(shape) is True, shape
    for shape in non_holder_shapes:
        assert scope_report.deny_reason_names_a_holder(shape) is False, shape

    # Fail-closed on a non-string: a caller deciding whether it may ASSERT a
    # holder must never get True from a degraded input.
    assert scope_report.deny_reason_names_a_holder(None) is False








def test_post_commit_release_phantom_claims_never_drops_a_pending_tracked_deletion(
    tmp_path,
):
    """Regression guard (the second, load-bearing half of this test pair):
    a claimed path that is absent from disk BECAUSE it is a genuine,
    tracked-at-HEAD deletion must stay claimed and visible in the offer as
    a pending deletion -- `release_phantom_claims` must not reclassify a
    real deletion as a phantom just because both are absent from disk.
    Reintroducing this from the opposite side (over-releasing) is the exact
    failure this workstream's original bug report is the mirror image of.
    """
    repo = _init_repo(tmp_path)
    tracked_path = "keep/tracked.md"
    _seed_file(repo, tracked_path, "v1")
    _git(["add", "--", tracked_path], repo)
    _git(["commit", "-q", "-m", "seed tracked file"], repo)

    sid = "pending-deletion-session"
    session_core.init(sid, cwd=str(repo))

    # Claim the tracked file, then genuinely delete it -- a real, git-
    # representable pending deletion (`git status` reports it as `D`), not
    # a phantom.
    session_scope.touch(sid, tracked_path, cwd=str(repo))
    (repo / tracked_path).unlink()
    assert not (repo / tracked_path).exists()

    _seed_file(repo, "real/other-work.md", "genuine content")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["real/other-work.md"],
            "message": "commit unrelated real work",
            "session_id": sid,
        }
    )
    assert result["committed"] is True

    offer = compute_offer(sid, cwd=str(repo))
    assert tracked_path in offer["safe_paths"]


def test_resolve_push_report_issues_no_git_at_all(monkeypatch):
    """C-02: `_resolve_push_report` is a pure mapping now.

    The strongest available statement of "the probe is gone": explode on any
    git spawn, then exercise every status. A reintroduced remote read -- under
    any name, from any layer -- fails here rather than quietly costing 4 spawns
    and 1.0s of sleep on the push-raced path again.
    """
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony import scoped_git_commit as sgc

    def _explode(*a, **k):
        raise AssertionError("_resolve_push_report spawned git: %r" % (a,))

    monkeypatch.setattr(git_native, "_git", _explode)

    for status in (
        sgc.PUSH_STATUS_DECLINED,
        sgc.PUSH_STATUS_PUSHED,
        sgc.PUSH_STATUS_NO_REMOTE,
        sgc.PUSH_STATUS_FAILED,
        sgc.PUSH_STATUS_NOT_ATTEMPTED,
    ):
        sgc._resolve_push_report(status)


def test_resolve_push_report_maps_every_status():
    """Each canonical status maps to exactly one report, including the two
    rungs the acceptance criteria name explicitly.

    `PUSH_STATUS_FAILED -> (False, PUSH_STATE_FAILED)` is the rung C-01 made
    trustworthy: with one publisher per commit, this op's own push status IS
    whether the commit is published, so a failure is a failure.

    `PUSH_STATUS_NOT_ATTEMPTED -> (None, PUSH_STATE_UNCONFIRMED)` is the rung
    that must NOT collapse into failure. A push that never ran is genuinely
    unknown, and rendering unknown as failure invites the dangerous correction
    (re-push, amend, force-push) on an auto-push-armed shared branch.
    """
    from coordinator_core.ops.ceremony import scoped_git_commit as sgc

    assert sgc._resolve_push_report(sgc.PUSH_STATUS_DECLINED) == (
        None, sgc.PUSH_STATE_DECLINED)
    assert sgc._resolve_push_report(sgc.PUSH_STATUS_PUSHED) == (
        True, sgc.PUSH_STATE_PUSHED)
    assert sgc._resolve_push_report(sgc.PUSH_STATUS_NO_REMOTE) == (
        None, sgc.PUSH_STATE_NO_REMOTE)
    assert sgc._resolve_push_report(sgc.PUSH_STATUS_FAILED) == (
        False, sgc.PUSH_STATE_FAILED)
    assert sgc._resolve_push_report(sgc.PUSH_STATUS_NOT_ATTEMPTED) == (
        None, sgc.PUSH_STATE_UNCONFIRMED)


def test_unrecognized_push_status_is_unconfirmed_never_failure():
    """A status this mapping does not know must fail toward "unknown".

    The old code reached the probe on anything unrecognized and let the remote
    decide; the mapping has no such backstop, so the default rung is the whole
    safety argument. Failing toward FAILED would manufacture a breach out of a
    vocabulary drift between this module and `commit_pipeline`.
    """
    from coordinator_core.ops.ceremony import scoped_git_commit as sgc

    assert sgc._resolve_push_report("some-future-status") == (
        None, sgc.PUSH_STATE_UNCONFIRMED)


# ---------------------------------------------------------------------------
# C0 (docs/plans/2026-08-14-the-tool-stages-what-it-commits.md), AC6: the
# audit's own reproduction (state/audits/2026-08-14-scoped-commit-partial-
# stage-sweep.md) ported as a regression test, before its scratchpad fixture
# dies. S1 and S3 are passing negative controls; S5 is the live incident's
# own timeline -- headline case, still green today, because the hand-staged
# path's standing limit is exactly what it documents, not a defect this
# dispatch fixes. The `--stage-patch` half of AC6 (the fix) lives in
# test_commit_pipeline.py, `designed_red`, since C2/C3 have not landed yet.
# ---------------------------------------------------------------------------

_AUDIT_BASE = "\n".join(f"line {i}" for i in range(1, 61)) + "\n"


def _audit_variant(*, em: bool, peer: bool) -> str:
    """`substrate.py`'s 60-line body with the EM's line-5 hunk and/or the
    peer's line-55 hunk applied -- the audit's own fixture shape
    (`<scratchpad>/repro2.py::variant`), ported verbatim rather than
    re-derived.
    """
    lines = _AUDIT_BASE.splitlines()
    if em:
        lines[4] = "line 5 EM_CHANGE"
    if peer:
        lines[54] = "line 55 PEER_CHANGE"
    return "\n".join(lines) + "\n"


def _audit_repo(tmp_path: Path) -> Path:
    """A fresh repo carrying the audit's base commit, with the worktree
    already holding BOTH the EM's and the peer's uncommitted hunks --
    `<scratchpad>/repro2.py::mk`, ported verbatim.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "substrate.py", _AUDIT_BASE)
    _seed_file(repo, "other.py", "other\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    _seed_file(repo, "substrate.py", _audit_variant(em=True, peer=True))
    return repo


def _audit_partial_stage(repo: Path) -> None:
    """Deliberate partial stage: the index holds the EM hunk ONLY -- the
    deterministic equivalent of `git apply --cached` on a filtered patch
    (`<scratchpad>/repro2.py::partial_stage`, ported verbatim).
    """
    em_only = _audit_variant(em=True, peer=False)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo), input=em_only, capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(["update-index", "--cacheinfo", f"100644,{blob},substrate.py"], repo)


def test_s1_hand_staged_path_holds_with_no_peer_interference(tmp_path):
    """Audit S1 (state/audits/2026-08-14-scoped-commit-partial-stage-
    sweep.md), passing negative control: with no peer activity in the
    stage->commit window, the private-index branch commits the EM's own
    partial stage verbatim -- the protection this file's other CAS tests
    pin working exactly as designed, unrelated to this scenario's own
    concurrency shape.
    """
    repo = _audit_repo(tmp_path)
    _audit_partial_stage(repo)

    result = _call(
        {"worktree_root": str(repo), "paths": ["substrate.py"], "message": "s1 EM commit"}
    )

    assert result["committed"] is True
    head = subprocess.run(
        ["git", "show", "HEAD:substrate.py"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert "EM_CHANGE" in head
    assert "PEER_CHANGE" not in head


def test_s3_peer_add_before_invocation_destroys_the_partial_stage_and_sweeps(tmp_path):
    """Audit S3, passing negative control documenting the OTHER defeat
    mechanism (the `index != worktree` conjunct, not `index != HEAD`): a
    peer's plain `git add` on the same path -- no commit -- before this
    invocation begins makes `index == worktree`, so `diverging_paths()`'s
    plain-`git diff` leg empties and the partial stage is already gone
    before any check runs. The commit still lands (nothing refuses this
    today), and it carries the peer's hunk too -- the audit's own second
    independent defeat of the divergence predicate.
    """
    repo = _audit_repo(tmp_path)
    _audit_partial_stage(repo)

    _git(["add", "--", "substrate.py"], repo)  # peer's own restage, no commit

    result = _call(
        {"worktree_root": str(repo), "paths": ["substrate.py"], "message": "s3 EM commit"}
    )

    assert result["committed"] is True
    head = subprocess.run(
        ["git", "show", "HEAD:substrate.py"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert "EM_CHANGE" in head
    assert "PEER_CHANGE" in head  # swept -- documents the standing limit


def test_s5_hand_staged_path_sweeps_peer_hunks_when_peer_absorbs_before_invocation_begins(tmp_path):
    """Audit S5 -- the live incident's own timeline (state/audits/2026-08-
    14-scoped-commit-partial-stage-sweep.md), and this plan's Problem
    statement's own falsifier: a peer commits the EM's hand-staged blob
    into HEAD via a SEPARATE, already-completed invocation of the real op,
    entirely BEFORE this call's own process starts. Unlike the
    `test_agree_branch_cas_refuses_when_peer_absorbs_the_stage_mid_call`
    sibling test above (which the Layer-1 CAS DOES catch, because that
    race lands inside the call's own check-then-act window), no in-process
    CAS can observe an absorption that predates this call's own snapshot:
    `pre_index_blobs == pre_head_blobs` already at entry, so
    `_agree_branch_cas_refusal`'s `absorbed_candidates` condition
    (`pre_index != pre_head` at snapshot time) never fires for this path.
    The commit still lands, and it sweeps the peer's own further worktree
    hunk in -- the standing limit this plan's `--stage-patch` primitive
    (C2/C3) closes; see the `designed_red` sibling in
    test_commit_pipeline.py for that half.
    """
    repo = _audit_repo(tmp_path)
    _audit_partial_stage(repo)

    peer_msg = repo / "peer_msg.txt"
    peer_msg.write_text("peer absorbs the EM's hand-staged blob\n", encoding="utf-8")
    peer_result = git_native.commit_scoped(["substrate.py"], str(peer_msg), str(repo))
    assert peer_result.ok, peer_result.stderr  # the peer's own invocation lands first, cleanly

    result = _call(
        {"worktree_root": str(repo), "paths": ["substrate.py"], "message": "s5 EM commit"}
    )

    assert result["committed"] is True
    log_subjects = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert "s5 EM commit" in log_subjects
    head = subprocess.run(
        ["git", "show", "HEAD:substrate.py"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert "EM_CHANGE" in head
    assert "PEER_CHANGE" in head  # swept -- the incident, reproduced


def test_cli_parses_stage_patch_flag_into_params_shape():
    """AC1, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md:
    `--stage-patch <file>` does not exist yet -- `_parse_args` raises
    `_Usage("unrecognized argument '--stage-patch' ...")` on this token
    today. `designed_red` until C3 wires the flag through `_parse_args`;
    asserts the FUTURE parsed shape (a `stage_patch` value threaded
    alongside the existing return tuple) and lets today's `_Usage` propagate
    uncaught -- the right-reason failure (the flag/primitive is absent),
    never an import or fixture bug -- so this flips green exactly when C3
    lands, never earlier.
    """
    cli = _load_cli_module()
    (
        subject, repo, paths, as_json, include_orphans, deliverable_id, stage_patch, mangled_cr_paths,
    ) = cli._parse_args(
        ["-m", "subject", "--stage-patch", "patch.diff", "--", "substrate.py"]
    )
    assert stage_patch == "patch.diff"
    assert paths == ["substrate.py"]


def test_resolve_content_sources_supplied_blob_never_worktree_sourced():
    """C1, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md: pins
    the property the plan names -- a path carrying a supplied blob can
    NEVER appear in the worktree-sourced resolution, regardless of which
    (if any) of `diverged`/`non_diverged` it also happens to be a member
    of. Exercised directly against `_resolve_content_sources`, the total
    per-path resolution function `_commit_scoped_private_index` now
    consumes -- `--stage-patch` does not exist until C3, so this does not
    route through the CLI.
    """
    resolution = git_native._resolve_content_sources(
        diverged=["diverged_and_supplied.py", "diverged_only.py"],
        non_diverged=["non_diverged_and_supplied.py", "non_diverged_only.py"],
        supplied_blobs={
            "diverged_and_supplied.py": "a" * 40,
            "non_diverged_and_supplied.py": "b" * 40,
        },
    )

    assert resolution["diverged_and_supplied.py"] == git_native._SOURCE_SUPPLIED
    assert resolution["non_diverged_and_supplied.py"] == git_native._SOURCE_SUPPLIED
    assert resolution["diverged_only.py"] == git_native._SOURCE_STAGED
    assert resolution["non_diverged_only.py"] == git_native._SOURCE_WORKTREE

    worktree_sourced = {p for p, src in resolution.items() if src == git_native._SOURCE_WORKTREE}
    staged_sourced = {p for p, src in resolution.items() if src == git_native._SOURCE_STAGED}
    supplied_sourced = {p for p, src in resolution.items() if src == git_native._SOURCE_SUPPLIED}
    assert supplied_sourced.isdisjoint(worktree_sourced)
    assert supplied_sourced.isdisjoint(staged_sourced)


def test_resolve_content_sources_empty_supplied_blobs_matches_prior_partition():
    """C1: an empty (or omitted) `supplied_blobs` map reproduces the
    PRIOR binary `diverged`/`non_diverged` partition exactly -- the
    behaviour-preservation guarantee this refactor's hard constraint
    requires.
    """
    resolution = git_native._resolve_content_sources(
        diverged=["staged.py"], non_diverged=["worktree.py"]
    )
    assert resolution == {
        "staged.py": git_native._SOURCE_STAGED,
        "worktree.py": git_native._SOURCE_WORKTREE,
    }


# ---------------------------------------------------------------------------
# C2, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md:
# `stage_from_patch()` / `stage_from_patch_cas_refusal()`.
# ---------------------------------------------------------------------------


def _write_patch(
    repo: Path, rel_path: str, old_content: str, new_content: str
) -> Path:
    """Seed `rel_path` at `old_content` (committed), edit it to
    `new_content` in the worktree, capture git's OWN unified diff of that
    edit as a patch file, then revert the worktree back to `old_content` --
    leaving the repo exactly as it was before this helper ran, with the
    patch file the only trace. Using a git-authored diff (never a hand-typed
    one) means the patch's hunk framing/context matches this repo's own
    `git apply` expectations exactly.
    """
    _seed_file(repo, rel_path, old_content)
    _git(["add", "--", rel_path], repo)
    _git(["commit", "-q", "-m", f"seed {rel_path}"], repo)

    _seed_file(repo, rel_path, new_content)
    diff_result = subprocess.run(
        ["git", "diff", "--", rel_path], cwd=str(repo), capture_output=True, text=True, check=True,
    )
    patch_path = repo.parent / f"{rel_path.replace('/', '_')}.patch"
    # `newline=""` is load-bearing on Windows: `git diff`'s captured stdout
    # is LF-only (matching the LF blob `core.autocrlf=true` stores), and a
    # default-newline write here would translate every `\n` to `\r\n`,
    # corrupting the patch against the LF index content `git apply --cached`
    # matches hunks against -- an artifact of THIS test helper writing the
    # file, never of `git diff`'s own output.
    patch_path.write_text(diff_result.stdout, encoding="utf-8", newline="")

    _git(["checkout", "--", rel_path], repo)
    return patch_path


def _two_hunk_content(marker: str) -> str:
    lines = [f"line{i}" for i in range(1, 31)]
    lines[1] = f"{marker}-two"
    lines[27] = f"{marker}-twentyeight"
    return "\n".join(lines) + "\n"


def test_stage_from_patch_writes_only_to_private_index(tmp_path):
    """AC1: the shared index and worktree are byte-unchanged after
    `stage_from_patch()` -- the write landed ONLY in the process-private
    temp index, and the returned blob is the applied content, never a
    worktree re-read.
    """
    repo = _init_repo(tmp_path)
    old = _two_hunk_content("old")
    new = _two_hunk_content("new")
    patch_path = _write_patch(repo, "a.txt", old, new)

    result = git_native.stage_from_patch(patch_path, ["a.txt"], repo)

    assert result.ok, result.stderr
    assert "a.txt" in result.blobs
    assert _porcelain(repo) == []  # shared index/worktree: no visible change
    assert (repo / "a.txt").read_text(encoding="utf-8") == old  # worktree untouched

    blob_text = subprocess.run(
        ["git", "cat-file", "-p", result.blobs["a.txt"]],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert blob_text == new


def test_stage_from_patch_records_head_blob_at_apply_time(tmp_path):
    """AC2: `head_blobs` on the result is each path's HEAD blob, taken
    before the private index is touched -- must equal the real HEAD blob
    for that path at the time of the call.
    """
    repo = _init_repo(tmp_path)
    old = _two_hunk_content("old")
    new = _two_hunk_content("new")
    patch_path = _write_patch(repo, "a.txt", old, new)

    real_head_blob = subprocess.run(
        ["git", "rev-parse", "HEAD:a.txt"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()

    result = git_native.stage_from_patch(patch_path, ["a.txt"], repo)

    assert result.ok, result.stderr
    assert result.head_blobs["a.txt"] == real_head_blob


def test_stage_from_patch_bad_second_hunk_refuses_atomically(tmp_path):
    """AC3: a patch whose SECOND hunk does not apply refuses the WHOLE
    call -- `git apply --cached` is all-or-nothing per invocation. Blobs is
    empty (no partial write reported), the shared index/worktree are
    untouched, and no private temp-index file is left behind.
    """
    repo = _init_repo(tmp_path)
    old = _two_hunk_content("old")
    new = _two_hunk_content("new")
    patch_path = _write_patch(repo, "a.txt", old, new)

    patch_text = patch_path.read_text(encoding="utf-8")
    # Corrupt the second hunk's context so it can never match -- this
    # repo's own diff always has TWO "@@" hunk headers here (edits at
    # line 2 and line 28, far enough apart that default 3-line context
    # keeps them separate hunks).
    assert patch_text.count("@@ -") == 2, patch_text
    corrupted = patch_text.replace("line27", "THIS-CONTEXT-LINE-DOES-NOT-EXIST")
    patch_path.write_text(corrupted, encoding="utf-8", newline="")

    before = list(Path(tempfile.gettempdir()).glob(f"git-index-{os.getpid()}-*"))

    result = git_native.stage_from_patch(patch_path, ["a.txt"], repo)

    assert result.ok is False
    assert result.reason == "apply-failed"
    assert result.blobs == {}
    assert _porcelain(repo) == []
    assert (repo / "a.txt").read_text(encoding="utf-8") == old

    after = list(Path(tempfile.gettempdir()).glob(f"git-index-{os.getpid()}-*"))
    assert after == before  # no residue left behind (`finally: unlink`)


def test_stage_from_patch_multi_path_mixed_already_applied_and_conflict_fails_closed(tmp_path):
    """AC6 (review: coordinator:code-reviewer 1ead6ae2, finding 2) -- the
    reverse-apply-check fallback is a WHOLE-PATCH check (same `--include`
    filters as the forward apply), so a multi-path patch where ONE file is
    already-applied (peer landed the identical content) and ANOTHER file
    genuinely conflicts must still refuse the whole call: the reverse-check
    only masks a forward failure when the ENTIRE bounded patch reverses
    cleanly, and a genuine conflict on `c.txt` means it does not. This must
    fall through to `reason="apply-failed"`, never silently treat the
    already-applied file as cover for the still-conflicting one.
    """
    repo = _init_repo(tmp_path)
    old_a = _two_hunk_content("old")
    new_a = _two_hunk_content("new")
    patch_a = _write_patch(repo, "a.txt", old_a, new_a)

    old_c = _two_hunk_content("oldc")
    new_c = _two_hunk_content("newc")
    patch_c = _write_patch(repo, "c.txt", old_c, new_c)

    combined = repo.parent / "combined.patch"
    combined.write_text(
        patch_a.read_text(encoding="utf-8") + patch_c.read_text(encoding="utf-8"),
        encoding="utf-8", newline="",
    )

    # a.txt: a peer already landed the patch's own target content -- the
    # forward apply's context match will fail (HEAD already carries new_a),
    # but the reverse-check for THIS file alone would succeed.
    _seed_file(repo, "a.txt", new_a)
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "peer already applied a.txt"], repo)

    # c.txt: genuinely diverged -- neither the patch's pre-image nor its
    # post-image is present, so neither the forward apply nor the reverse
    # check can succeed for this file.
    _seed_file(repo, "c.txt", "totally-unrelated-content\n")
    _git(["add", "--", "c.txt"], repo)
    _git(["commit", "-q", "-m", "peer diverged c.txt"], repo)

    result = git_native.stage_from_patch(combined, ["a.txt", "c.txt"], repo)

    assert result.ok is False
    assert result.reason == "apply-failed"
    assert result.blobs == {}
    assert _porcelain(repo) == []


def test_stage_from_patch_cas_refusal_when_peer_commits_between(tmp_path):
    """AC2's base hole: a peer commits to the SAME path between
    `stage_from_patch()` and the caller's own commit step --
    `stage_from_patch_cas_refusal()` must refuse rather than let the
    caller silently commit a supplied blob over the peer's now-landed
    history.
    """
    repo = _init_repo(tmp_path)
    old = _two_hunk_content("old")
    new = _two_hunk_content("new")
    patch_path = _write_patch(repo, "a.txt", old, new)

    result = git_native.stage_from_patch(patch_path, ["a.txt"], repo)
    assert result.ok, result.stderr

    # Peer commits to the SAME path in between.
    _seed_file(repo, "a.txt", "peer content\n")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "peer commit"], repo)

    refusal = git_native.stage_from_patch_cas_refusal(repo, ["a.txt"], result.head_blobs)

    assert refusal is not None
    assert refusal.ok is False
    assert "a.txt" in refusal.stderr


def test_stage_from_patch_cas_refusal_none_when_head_unmoved(tmp_path):
    """Sanity control: with no intervening peer commit, the CAS re-check
    passes (returns `None`) -- the overwhelming-majority case.
    """
    repo = _init_repo(tmp_path)
    old = _two_hunk_content("old")
    new = _two_hunk_content("new")
    patch_path = _write_patch(repo, "a.txt", old, new)

    result = git_native.stage_from_patch(patch_path, ["a.txt"], repo)
    assert result.ok, result.stderr

    refusal = git_native.stage_from_patch_cas_refusal(repo, ["a.txt"], result.head_blobs)

    assert refusal is None


# ---------------------------------------------------------------------------
# AC4/C3, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md: the
# `--stage-patch` end-to-end op-level shape -- a real `stage_patch` param,
# through the op boundary, mixed with an unprovenanced path in the same
# invocation.
# ---------------------------------------------------------------------------


def test_stage_patch_op_commits_patched_and_names_unprovenanced_path(tmp_path):
    """AC4: a mixed invocation is provenanced for the patched path and
    worktree-sourced for the other -- and the op response NAMES the
    unprovenanced subset, never leaving it for the caller to infer from
    absence (mirrors `worktree_excluded`'s own posture).
    """
    repo = _init_repo(tmp_path)
    old = _two_hunk_content("old")
    new = _two_hunk_content("new")
    patch_path = _write_patch(repo, "a.txt", old, new)

    # `b.txt` is untouched by the patch -- an ordinary worktree edit.
    _seed_file(repo, "b.txt", "b-v1\n")
    _git(["add", "--", "b.txt"], repo)
    _git(["commit", "-q", "-m", "seed b.txt"], repo)
    _seed_file(repo, "b.txt", "b-v2\n")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["a.txt", "b.txt"],
            "message": "mixed stage-patch commit",
            "stage_patch": str(patch_path),
        }
    )

    assert result["committed"] is True, result
    assert result.get("unprovenanced_paths") == ["b.txt"]
    head_a = subprocess.run(
        ["git", "show", "HEAD:a.txt"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    head_b = subprocess.run(
        ["git", "show", "HEAD:b.txt"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert head_a == new
    assert head_b == "b-v2\n"


def test_stage_patch_op_missing_patch_file_refused_before_mutation(tmp_path):
    """AC1/AC3: a missing `stage_patch` path refuses BEFORE anything
    mutates -- a validation error, same shape as a missing required param,
    never a partial commit.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a.txt", "v1\n")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "a.txt", "v2\n")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["a.txt"],
            "message": "should not land",
            "stage_patch": str(repo.parent / "does-not-exist.patch"),
        }
    )

    assert result["committed"] is False
    assert "error" in result
    assert _porcelain(repo)  # untouched -- a.txt's worktree edit is still there, unstaged


# ---------------------------------------------------------------------------
# AC7, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md: five
# distinct machine-readable `reason`s -- `patch-did-not-apply` is exercised
# end-to-end here (op level); the remaining four are pinned at
# `commit_pipeline.py`'s own `commit()` boundary in test_commit_pipeline.py,
# where the CAS/failure shapes are far cheaper to construct deterministically.
# ---------------------------------------------------------------------------


def test_stage_patch_op_reason_patch_did_not_apply(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a.txt", "v1\n")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    bad_patch = repo.parent / "bad.patch"
    bad_patch.write_text(
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-this context does not exist\n+nope\n",
        encoding="utf-8",
    )

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["a.txt"],
            "message": "should refuse",
            "stage_patch": str(bad_patch),
        }
    )

    assert result["committed"] is False
    assert result.get("reason") == "patch-did-not-apply"
    assert result["commit_failed"] is True


def test_stage_patch_infra_failure_does_not_masquerade_as_a_bad_patch(monkeypatch, tmp_path):
    """The staging primitive and this layer speak different failure
    vocabularies (`apply-failed`/`index-infra-failure` vs AC7's
    `patch-did-not-apply` family). `commit()` translates between them; a
    pass-through would replace AC7's vocabulary wholesale, and collapsing
    every failure onto `patch-did-not-apply` would send an operator to debug
    a patch that was never the fault. Pins the translation in both
    directions -- the genuine-bad-hunk case is the sibling test above.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a.txt", "v1\n")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    patch_file = repo.parent / "irrelevant.patch"
    patch_file.write_text("", encoding="utf-8")

    real = git_native.stage_from_patch

    def _infra_failure(*a, **k):
        outcome = real(*a, **k)
        return outcome.__class__(
            ok=False,
            blobs={},
            head_blobs={},
            reason="index-infra-failure",
            stderr="read-tree HEAD failed",
        )

    monkeypatch.setattr(git_native, "stage_from_patch", _infra_failure)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["a.txt"],
            "message": "should refuse distinctly",
            "stage_patch": str(patch_file),
        }
    )

    assert result["committed"] is False
    assert result.get("reason") == "stage-infra-failure"


def test_classify_uncommitted_bare_exit_1_with_clean_tree_reclassifies_as_noop(tmp_path):
    """Pins the genuine-idempotency shape `_classify_uncommitted` must keep
    reclassifying: `exit_code == 1` AND a bare `stderr` (no real diagnostic
    text -- exactly what `commit_pipeline.commit()`'s `not result.ok` branch
    leaves it in for git's own "nothing to commit" no-op) AND a porcelain-
    clean tree together are the benign already-committed case.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    commit_outcome = SimpleNamespace(exit_code=1, stderr="exit_code=1")
    result = SimpleNamespace(
        commit_failed=True,
        diagnostics=["exit_code=1"],
        committed_sha=None,
        commit=commit_outcome,
    )

    commit_failed, diagnostics, empty_commit_set = scoped_git_commit._classify_uncommitted(
        str(repo), ["README.md"], result
    )

    assert commit_failed is False
    assert diagnostics == []
    assert empty_commit_set is True


def test_classify_uncommitted_hook_rejection_with_clean_tree_stays_failed(tmp_path):
    """Review: code-reviewer -- Finding [P3], 2026-08-15 (chain-ancestry
    slice). The residual gap: `exit_code == 1` alone cannot distinguish
    git's own "nothing to commit" no-op from a rejecting `pre-commit`/
    `commit-msg` hook, both of which conventionally exit 1. The ordinary
    hook-rejection case is caught downstream by `_commit_paths_are_clean()`'s
    porcelain probe (a rejected hook normally leaves the pathspec still
    dirty) -- but a hook that reverts its own edits on failure, leaving the
    tree byte-identical to HEAD, would pass that probe too. This exercises
    exactly that: a real, clean-relative-to-HEAD tree (so the porcelain probe
    alone WOULD reclassify), paired with a `CommitOutcome.stderr` carrying
    real hook-diagnostic text rather than the bare `exit_code=N` shape --
    the fix's `_BARE_EXIT_CODE_STDERR_RE` gate must keep this `commit_failed`.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    # Tree is byte-identical to HEAD -- nothing dirty under any pathspec,
    # exactly the shape `_commit_paths_are_clean()` alone cannot distinguish
    # from a genuine no-op.

    commit_outcome = SimpleNamespace(
        exit_code=1,
        stderr="hook 'pre-commit' rejected: lint failure on README.md",
    )
    result = SimpleNamespace(
        commit_failed=True,
        diagnostics=["hook 'pre-commit' rejected: lint failure on README.md"],
        committed_sha=None,
        commit=commit_outcome,
    )

    commit_failed, diagnostics, empty_commit_set = scoped_git_commit._classify_uncommitted(
        str(repo), ["README.md"], result
    )

    assert commit_failed is True
    assert diagnostics == ["hook 'pre-commit' rejected: lint failure on README.md"]
    assert empty_commit_set is False


def test_classify_uncommitted_surfaces_stdout_diagnostic_on_real_failure(tmp_path):
    """AC10 (docs/plans/2026-08-15-the-ceremony-tail-stops-lying-about-why-it-
    failed.md): a real commit-step failure whose diagnosis landed on stdout
    (`CommitOutcome.stdout_diagnostic`) must surface that text in the returned
    `diagnostics` -- `stderr` alone stays the bare `exit_code=N` shape two
    other consumers match on, so without this the operator sees nothing at
    all explaining the refusal. The dirty tree here (a real uncommitted
    modification under the pathspec) keeps this off the benign-no-op branch
    via `_commit_paths_are_clean()`, independent of `stderr`'s bare shape.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "README.md", "still dirty -- never landed")

    commit_outcome = SimpleNamespace(
        exit_code=1,
        stderr="exit_code=1",
        stdout_diagnostic="error: gpg failed to sign the data",
    )
    result = SimpleNamespace(
        commit_failed=True,
        diagnostics=["exit_code=1"],
        committed_sha=None,
        commit=commit_outcome,
    )

    commit_failed, diagnostics, empty_commit_set = scoped_git_commit._classify_uncommitted(
        str(repo), ["README.md"], result
    )

    assert commit_failed is True
    assert diagnostics == ["exit_code=1", "error: gpg failed to sign the data"]
    assert empty_commit_set is False


def test_classify_uncommitted_benign_noop_diagnostics_stay_empty_regardless_of_stdout_diagnostic(
    tmp_path,
):
    """The benign already-committed no-op's `diagnostics` must stay `[]`
    byte-identical to before, even when `CommitOutcome.stdout_diagnostic` is
    populated (it is populated UNCONDITIONALLY on the `not result.ok` branch
    per its own docstring, so a genuine no-op can carry one too) -- the
    2026-08-03 cry-wolf incident this module's docstring documents is exactly
    what re-surfacing anything on this branch would reintroduce.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    commit_outcome = SimpleNamespace(
        exit_code=1,
        stderr="exit_code=1",
        stdout_diagnostic="nothing to commit, working tree clean",
    )
    result = SimpleNamespace(
        commit_failed=True,
        diagnostics=["exit_code=1"],
        committed_sha=None,
        commit=commit_outcome,
    )

    commit_failed, diagnostics, empty_commit_set = scoped_git_commit._classify_uncommitted(
        str(repo), ["README.md"], result
    )

    assert commit_failed is False
    assert diagnostics == []
    assert empty_commit_set is True


# ---------------------------------------------------------------------------
# Stale-index rejection (`_reject_stale_index_paths`)
#
# state/bug-backlog/2026-08-19-shared-git-index-holds-stale-pre-head-sn-
# b5b83e42e275.yaml: a path whose worktree matches HEAD while its index does
# not is never a legitimate commit intent -- it is either the pre-commit blob
# a pathspec commit left behind (committing it REVERTS that commit) or content
# whose sole copy is the index. On 2026-08-20 the first shape landed through
# `session.safe_commit_offer` as a54addce, reverting cd751b79's `ipc.py` fix
# while every ordinary signal reported success.
# ---------------------------------------------------------------------------


def _stage_a_revert_of_head(repo: Path, rel_path: str) -> None:
    """Reproduce the index residue: worktree == HEAD, index == the PREVIOUS
    blob. Uses `update-index --cacheinfo` so the worktree is never touched --
    the same divergence the shared tree exhibits after a pathspec commit."""
    prev_blob = subprocess.run(
        ["git", "rev-parse", f"HEAD~1:{rel_path}"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(["update-index", "--cacheinfo", f"100644,{prev_blob},{rel_path}"], repo)


def test_refuses_a_path_whose_index_reverts_head(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "ipc.py", "original\n")
    _git(["add", "--", "ipc.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "ipc.py", "the landed fix\n")
    _git(["add", "--", "ipc.py"], repo)
    _git(["commit", "-q", "-m", "land the fix"], repo)
    landed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    _stage_a_revert_of_head(repo, "ipc.py")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["ipc.py"],
            "message": "session tail: close-out",
        }
    )

    assert result["committed"] is False
    assert result["sha"] is None
    assert "stale index" in result["error"]
    assert "ipc.py" in result["error"]
    # The landed fix is still what HEAD says, and still what the worktree says.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head == landed
    assert (repo / "ipc.py").read_text(encoding="utf-8") == "the landed fix\n"


def test_refuses_the_whole_call_when_one_grouped_path_is_stale(tmp_path):
    """safe-commit-offer commits GROUPS it computed itself. A group carrying one
    stale path must refuse rather than land the group and revert that path."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "ipc.py", "original\n")
    _git(["add", "--", "ipc.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "ipc.py", "the landed fix\n")
    _git(["add", "--", "ipc.py"], repo)
    _git(["commit", "-q", "-m", "land the fix"], repo)

    _stage_a_revert_of_head(repo, "ipc.py")
    _seed_file(repo, "notes.md", "ordinary in-scope edit\n")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["ipc.py", "notes.md"],
            "message": "session tail: close-out",
        }
    )

    assert result["committed"] is False
    assert "stale index" in result["error"]
    assert _committed_files_at_head(repo) == ["ipc.py"]


def test_index_only_content_is_refused_not_swept(tmp_path):
    """The other direction the entry's negative_spec names: staged content with
    a worktree identical to HEAD may be a peer's ONLY copy. Refuse; never
    commit it, never clear it."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "peer.py", "seed\n")
    _git(["add", "--", "peer.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "peer.py", "a live peer's uncommitted work\n")
    _git(["add", "--", "peer.py"], repo)
    _seed_file(repo, "peer.py", "seed\n")  # worktree back to HEAD, index is not

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["peer.py"],
            "message": "close-out",
        }
    )

    assert result["committed"] is False
    assert "stale index" in result["error"]
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.split()
    assert staged == ["peer.py"]


def test_ordinary_staged_commit_is_not_refused(tmp_path):
    """The guard keys on worktree == HEAD AND index != HEAD. A deliberate
    `git add` leaves worktree == index != HEAD and must commit unchanged."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "feature.py", "new work\n")
    _git(["add", "--", "feature.py"], repo)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["feature.py"],
            "message": "add feature",
        }
    )

    assert result["committed"] is True
    assert _committed_files_at_head(repo) == ["feature.py"]
