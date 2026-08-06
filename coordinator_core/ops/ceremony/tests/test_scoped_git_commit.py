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
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.ops.session.safe_commit_offer import compute_offer
from coordinator_core.ops.session.scope_report import (
    assert_paths_in_session_scope as _real_assert_paths_in_session_scope,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness
from coordinator_core.session import scope as session_scope


@pytest.fixture(autouse=True)
def _bypass_ownership_gate(monkeypatch):
    """C4c (docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-
    classes.md) added a pathspec ownership-scope gate to `_handler`. This
    module's throwaway repos never seed a real `.git/coordinator-sessions/
    <sid>/touched.txt` claim for the ambient test session, so the REAL
    `assert_paths_in_session_scope` predicate (strict allow-list — see
    `scope_report`'s own module docstring, "REVERTED 2026-08-03", for why the
    gate composes this and not a permissive alternative) would deny nearly
    every dirty path this file's fixtures write, for every test in it. This
    autouse fixture always-allows so those tests can exercise commit-
    pipeline/push-reporting mechanics without also having to construct a
    fully-seeded session scope for content that is not what each test is
    actually about.

    NEW TEST HERE? This fixture is autouse — every test in this file
    inherits the always-allow bypass automatically. If your test cares about
    ownership REJECTION (an allow/deny decision, not commit-pipeline
    mechanics), write it in `test_scoped_git_commit_ownership.py` instead,
    which does NOT take this bypass and exercises the real predicate against
    seeded session scope.
    """
    monkeypatch.setattr(
        scoped_git_commit, "_assert_paths_in_session_scope", lambda *a, **k: (True, "")
    )


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
    return asyncio.run(scoped_git_commit._handler(params, repo_root=None))


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


def test_message_file_path_is_rejected_not_committed_verbatim(tmp_path):
    """Regression for `fdbff578b7dc` / `40bf1064a124` (example-doctrine-repo-em FYI memo,
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

    `ownership_gate` joins the fixed shape on the same footing (2026-08-04,
    example-cockpit-repo-em's consumer-side probe): the ownership gate emitted text
    only when it DENIED, so a consumer reading a successful response could
    only infer the ALLOW backwards from a commit having happened. See
    `scoped_git_commit`'s module docstring, § Ownership-gate observability,
    for the negative spec — it is a visibility field, read by nothing.
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
    assert set(result) == {
        "committed", "sha", "pushed", "push_state", "ownership_gate"
    }


def test_no_false_integrity_breach_when_something_else_pushed(tmp_path, monkeypatch):
    """`integrity_breach` is derived as "committed AND not pushed", which
    over-fires wherever a post-commit auto-push hook wins the race: the hook
    pushes, this op's own push step then finds nothing to push and reports
    False, and the commit IS on the remote. Reporting a breach there trains
    the reader to ignore the field. Confirmed-absent-from-remote is the bar.

    Forces the targeted scenario directly rather than relying on the op's
    own push step to race a real auto-push hook (which it never actually
    exercised -- see finding 1): monkeypatch `run_commit_pipeline` to report
    exactly `pushed=False`/`integrity_breach=True` for a sha that IS already
    genuinely on a real bare-repo remote (pushed via a side channel,
    independent of the mocked-out pipeline call), so the confirmation logic
    in `_handler` -- `_remote_sha_state` -- is the only thing that can make
    this assertion pass.

    Second assertion set (2026-07-30, example-doctrine-repo-em memo): suppressing the
    breach was only half the fix. The op still REPORTED `pushed=False` for a
    sha it had just confirmed on the remote, and `scoped-git-commit` rendered
    that as `(not pushed)` -- three times in one example-doctrine-repo session on commits that
    had all pushed. `pushed` answers "is this commit published", so a
    confirmed-present sha is `True`, whoever's push put it there.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "HEAD"], repo)

    _seed_file(repo, "notes/alpha.md", "content")
    _git(["add", "--", "notes/alpha.md"], repo)
    _git(["commit", "-q", "-m", "add notes"], repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    # Side channel: push it for real, independent of anything the (mocked)
    # op call does.
    _git(["push", "-q", "origin", "HEAD"], repo)

    fake_result = SimpleNamespace(
        committed_sha=sha,
        pushed=False,
        commit_failed=False,
        integrity_breach=True,
        diagnostics=[],
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
    assert result["sha"] == sha
    assert result["pushed"] is True
    assert result["push_state"] == scoped_git_commit.PUSH_STATE_PUSHED
    assert "integrity_breach" not in result


def _commit_and_fake_pipeline(tmp_path, monkeypatch, *, with_remote: bool):
    """Land a real local commit, then make the pipeline claim `pushed=False`
    for it — the shape `derive_pushed_tristate` produces when this op's own
    push loses a race to the detached auto-push, or genuinely fails.
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
    repo, sha = _commit_and_fake_pipeline(tmp_path, monkeypatch, with_remote=True)
    # Deliberately never pushed: genuinely absent from origin.
    monkeypatch.setattr(
        scoped_git_commit,
        "_remote_sha_state",
        lambda *a, **k: scoped_git_commit._REMOTE_ABSENT,
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
        {"worktree_root": str(repo), "paths": ["notes/alpha.md"], "message": "add notes"}
    )

    assert result["committed"] is True
    assert result["pushed"] is None
    assert result["push_state"] == scoped_git_commit.PUSH_STATE_UNCONFIRMED
    assert "integrity_breach" not in result


def test_remote_sha_state_distinguishes_absent_from_unknown(tmp_path):
    """`_sha_missing_from_remote` collapses absent and unknown into one bool;
    the tri-state probe underneath it must keep them apart, since that
    collapse is precisely what a human-facing render must not inherit.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    # No upstream configured: unknowable, NOT absent.
    assert scoped_git_commit._remote_sha_state(str(repo), sha) == scoped_git_commit._REMOTE_UNKNOWN
    assert scoped_git_commit._sha_missing_from_remote(str(repo), sha) is False

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "HEAD"], repo)
    assert scoped_git_commit._remote_sha_state(str(repo), sha) == scoped_git_commit._REMOTE_PRESENT

    _seed_file(repo, "notes/alpha.md", "content")
    _git(["add", "--", "notes/alpha.md"], repo)
    _git(["commit", "-q", "-m", "add notes"], repo)
    unpushed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert (
        scoped_git_commit._remote_sha_state(str(repo), unpushed, attempts=1)
        == scoped_git_commit._REMOTE_ABSENT
    )


def test_sha_missing_from_remote_false_when_sha_is_on_remote(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "HEAD"], repo)

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    assert scoped_git_commit._sha_missing_from_remote(str(repo), sha) is False


def test_sha_missing_from_remote_true_when_sha_genuinely_absent(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "HEAD"], repo)

    _seed_file(repo, "notes/alpha.md", "content")
    _git(["add", "--", "notes/alpha.md"], repo)
    _git(["commit", "-q", "-m", "add notes"], repo)
    # Deliberately never pushed -- genuinely absent from origin.
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    # attempts=1: this is a stable-absent case, not a race; skip the
    # production retry/backoff so the test stays fast.
    assert scoped_git_commit._sha_missing_from_remote(str(repo), sha, attempts=1) is True


def test_sha_missing_from_remote_false_when_no_upstream_configured(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    assert scoped_git_commit._sha_missing_from_remote(str(repo), sha) is False


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


def test_motivating_scenario_peer_committed_and_stays_live_co_toucher_can_commit(
    tmp_path, monkeypatch
):
    """AC1/AC6, end-to-end through the ceremony (C4,
    docs/plans/2026-08-03-scope-guard-peer-claim-release.md).

    Session A touches a shared path, commits it via `ceremony.scoped_git_commit`,
    and STAYS LIVE. Session B then touches the SAME path and must be able to
    commit it -- B's `my_scope` contains the path and `skipped` does not name A.

    This is the one test in the file that restores the REAL
    `assert_paths_in_session_scope` predicate (the module's `_bypass_ownership_
    gate` autouse fixture always-allows for every other test here) -- a bypassed
    gate would make `committed=True` trivially true regardless of whether the
    release actually freed the path, pinning nothing.
    """
    monkeypatch.setattr(
        scoped_git_commit,
        "_assert_paths_in_session_scope",
        _real_assert_paths_in_session_scope,
    )

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    shared_path = "shared/handoff.md"
    sid_a = "scenario-session-a"
    sid_b = "scenario-session-b"

    session_core.init(sid_a, cwd=str(repo))
    _seed_file(repo, shared_path, "from A")
    session_scope.touch(sid_a, shared_path, cwd=str(repo))

    result_a = _call(
        {
            "worktree_root": str(repo),
            "paths": [shared_path],
            "message": "A commits the shared path",
            "session_id": sid_a,
        }
    )
    assert result_a["committed"] is True
    assert "error" not in result_a

    # A must be live for the ENTIRE test -- if A read dead here, the
    # pre-existing liveness gate would release the claim on its own and the
    # rest of this test would pin nothing about this plan's own release path.
    assert liveness.session_live(sid_a, cwd=str(repo)) is True

    session_core.init(sid_b, cwd=str(repo))
    _seed_file(repo, shared_path, "from B")
    session_scope.touch(sid_b, shared_path, cwd=str(repo))

    # A is STILL live at the moment B's scope/commit is evaluated.
    assert liveness.session_live(sid_a, cwd=str(repo)) is True

    scope_b = session_scope.compute_scope(sid_b, cwd=str(repo))
    assert shared_path in scope_b.my_scope
    assert all(owner != sid_a for _path, owner in scope_b.skipped)

    result_b = _call(
        {
            "worktree_root": str(repo),
            "paths": [shared_path],
            "message": "B commits the shared path",
            "session_id": sid_b,
        }
    )
    assert result_b["committed"] is True
    assert "error" not in result_b
    assert _committed_files_at_head(repo) == [shared_path]


def test_motivating_scenario_peer_with_genuine_uncommitted_content_still_blocks(
    tmp_path, monkeypatch
):
    """AC2 at the ceremony layer -- the negative pair to the scenario above.
    Session A commits a path, then leaves GENUINE uncommitted content in that
    same path, and STAYS LIVE. Session B touching the same path must still be
    correctly blocked -- a test that only proved the guard releases a clean
    path would pass equally if the guard released everything.
    """
    monkeypatch.setattr(
        scoped_git_commit,
        "_assert_paths_in_session_scope",
        _real_assert_paths_in_session_scope,
    )

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    shared_path = "shared/still-dirty.md"
    sid_a = "scenario-session-a2"
    sid_b = "scenario-session-b2"

    session_core.init(sid_a, cwd=str(repo))
    _seed_file(repo, shared_path, "v1")
    session_scope.touch(sid_a, shared_path, cwd=str(repo))

    result_a = _call(
        {
            "worktree_root": str(repo),
            "paths": [shared_path],
            "message": "A commits v1",
            "session_id": sid_a,
        }
    )
    assert result_a["committed"] is True
    head_after_a = _committed_files_at_head(repo)

    # A leaves GENUINE uncommitted content in the same path -- re-claims it
    # (last event was released to R by the commit above, so a fresh T is
    # required for it to be seen as claimed again).
    _seed_file(repo, shared_path, "v2 -- uncommitted")
    session_scope.touch(sid_a, shared_path, cwd=str(repo))

    # A must be live for the ENTIRE test.
    assert liveness.session_live(sid_a, cwd=str(repo)) is True

    session_core.init(sid_b, cwd=str(repo))
    session_scope.touch(sid_b, shared_path, cwd=str(repo))

    # A is STILL live at the moment B's scope/commit is evaluated.
    assert liveness.session_live(sid_a, cwd=str(repo)) is True

    scope_b = session_scope.compute_scope(sid_b, cwd=str(repo))
    assert shared_path not in scope_b.my_scope
    assert any(
        path == shared_path and owner == sid_a for path, owner in scope_b.skipped
    )

    head_before_b = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    result_b = _call(
        {
            "worktree_root": str(repo),
            "paths": [shared_path],
            "message": "B tries to sweep A's uncommitted content",
            "session_id": sid_b,
        }
    )
    assert result_b["committed"] is False
    assert result_b["sha"] is None
    assert "error" in result_b
    assert sid_a in result_b["error"]

    # Nothing landed -- HEAD did not move, and A's uncommitted content is
    # still sitting dirty on disk, untouched.
    head_after_b = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_after_b == head_before_b
    assert _committed_files_at_head(repo) == head_after_a


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


def test_directory_expansion_still_denies_a_live_peers_member_file_by_name(
    tmp_path, monkeypatch
):
    """Guard rail: a directory expanding to 10 (here 2) files, one of which
    belongs to a live peer session, still denies on THAT file, named
    individually -- expansion must never launder a peer's file into this
    session's commit.
    """
    monkeypatch.setattr(
        scoped_git_commit,
        "_assert_paths_in_session_scope",
        _real_assert_paths_in_session_scope,
    )

    repo = _init_repo(tmp_path)
    _seed_file(repo, "waivers/mine.json", "v1")
    _seed_file(repo, "waivers/theirs.json", "v1")
    _git(["add", "--", "waivers/mine.json", "waivers/theirs.json"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    sid_me = "expansion-session-me"
    sid_peer = "expansion-session-peer"
    session_core.init(sid_me, cwd=str(repo))
    session_core.init(sid_peer, cwd=str(repo))

    _seed_file(repo, "waivers/mine.json", "v2")
    session_scope.touch(sid_me, "waivers/mine.json", cwd=str(repo))

    _seed_file(repo, "waivers/theirs.json", "v2")
    session_scope.touch(sid_peer, "waivers/theirs.json", cwd=str(repo))
    assert liveness.session_live(sid_peer, cwd=str(repo)) is True

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["waivers/"],
            "message": "try to sweep the peer's file via the directory",
            "session_id": sid_me,
        }
    )

    assert result["committed"] is False
    assert "error" in result
    assert "waivers/theirs.json" in result["error"]
    assert sid_peer in result["error"]
    # Nothing landed for either file.
    assert _porcelain(repo)


def test_directory_expansion_include_orphans_adopts_orphan_members(tmp_path, monkeypatch):
    """A (guard rail) + existing `include_orphans` contract: a directory
    whose expanded members are all orphans (claimed by no session) commits
    once `include_orphans: true` is set -- expansion composes with the
    existing orphan-adoption opt-in rather than bypassing it.
    """
    monkeypatch.setattr(
        scoped_git_commit,
        "_assert_paths_in_session_scope",
        _real_assert_paths_in_session_scope,
    )

    repo = _init_repo(tmp_path)
    _seed_file(repo, "waivers/orphan1.json", "v1")
    _seed_file(repo, "waivers/orphan2.json", "v1")
    _git(["add", "--", "waivers/orphan1.json", "waivers/orphan2.json"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # Dirtied by nobody's claimed touch -- an orphan, per `compute_offer`.
    _seed_file(repo, "waivers/orphan1.json", "v2")
    _seed_file(repo, "waivers/orphan2.json", "v2")

    sid = "expansion-orphan-session"
    session_core.init(sid, cwd=str(repo))

    denied = _call(
        {
            "worktree_root": str(repo),
            "paths": ["waivers/"],
            "message": "adopt the orphaned waivers",
            "session_id": sid,
        }
    )
    assert denied["committed"] is False
    assert "waivers/orphan1.json" in denied["error"] or "orphan" in denied["error"]

    adopted = _call(
        {
            "worktree_root": str(repo),
            "paths": ["waivers/"],
            "message": "adopt the orphaned waivers",
            "session_id": sid,
            "include_orphans": True,
        }
    )
    assert adopted["committed"] is True
    assert "error" not in adopted
    assert sorted(_committed_files_at_head(repo)) == [
        "waivers/orphan1.json",
        "waivers/orphan2.json",
    ]


def test_directory_denial_message_explains_directory_vs_file(tmp_path):
    """B: a denial that still carries an unexpanded directory (no dirty
    TRACKED member beneath it -- here, only an untracked file) must say it
    is a directory and what that means, rather than the bare
    "unclaimed/never classified" wording that gave no signal at all.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/only-untracked.md", "content")

    # Real ownership predicate (never seeded a session scope) so the
    # directory string itself reaches the denial branch this asserts on.
    scoped_git_commit._assert_paths_in_session_scope = _real_assert_paths_in_session_scope
    try:
        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["notes/"],
                "message": "try to commit the untracked-only directory",
                "session_id": "directory-denial-session",
            }
        )
    finally:
        scoped_git_commit._assert_paths_in_session_scope = (
            lambda *a, **k: (True, "")
        )

    assert result["committed"] is False
    assert "error" in result
    assert "directory" in result["error"]
    assert "notes/" in result["error"]


def test_include_orphans_ineffective_note_explains_peer_claimed_denial(
    tmp_path, monkeypatch
):
    """B: re-invoking with `include_orphans: true` against a path claimed by
    a live peer must say WHY the flag made no difference, rather than
    repeating the exact same unexplained refusal a second time.
    """
    monkeypatch.setattr(
        scoped_git_commit,
        "_assert_paths_in_session_scope",
        _real_assert_paths_in_session_scope,
    )

    repo = _init_repo(tmp_path)
    _seed_file(repo, "shared/peer-owned.md", "v1")
    _git(["add", "--", "shared/peer-owned.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    sid_peer = "ineffective-note-peer"
    sid_caller = "ineffective-note-caller"
    session_core.init(sid_peer, cwd=str(repo))
    session_core.init(sid_caller, cwd=str(repo))

    _seed_file(repo, "shared/peer-owned.md", "v2")
    session_scope.touch(sid_peer, "shared/peer-owned.md", cwd=str(repo))
    assert liveness.session_live(sid_peer, cwd=str(repo)) is True

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["shared/peer-owned.md"],
            "message": "try again with include_orphans",
            "session_id": sid_caller,
            "include_orphans": True,
        }
    )

    assert result["committed"] is False
    assert "error" in result
    assert "include-orphans" in result["error"]
    assert "made no difference" in result["error"]


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
