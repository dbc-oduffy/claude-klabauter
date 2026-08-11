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
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.ops.session.safe_commit_offer import compute_offer
from coordinator_core.ops.session import scope_report
from coordinator_core.ops.session.scope_report import (
    assert_paths_in_session_scope as _real_assert_paths_in_session_scope,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness
from coordinator_core.session import scope as session_scope


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
    subject, repo, paths, as_json, include_orphans, deliverable_id, mangled_cr_paths = (
        cli._parse_args(["-m", "subject", "--deliverable-id", "dlv-abc123", "--", "a.md"])
    )
    assert deliverable_id == "dlv-abc123"
    assert subject == "subject"
    assert paths == ["a.md"]
    assert mangled_cr_paths == []


def test_cli_omits_deliverable_id_when_not_given():
    cli = _load_cli_module()
    (
        _subject, _repo, _paths, _as_json, _include_orphans, deliverable_id, _mangled_cr_paths,
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
        push_status=scoped_git_commit.PUSH_STATUS_NOT_ATTEMPTED,
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
            push_status=scoped_git_commit.PUSH_STATUS_NOT_ATTEMPTED,
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


def test_declined_push_status_short_circuits_before_remote_probe(tmp_path, monkeypatch):
    """AC5 (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md,
    C5): a policy decline is known and deliberate, not the genuinely-unknown
    case `_resolve_push_report`'s remote probe exists for -- it must render
    `PUSH_STATE_DECLINED` WITHOUT ever calling `_remote_sha_state`. Asserting
    only the returned string would pass even if the probe still ran (its
    result is simply discarded by the branch order); the probe-not-called
    assertion is the actual AC.
    """
    from coordinator_core.ops.ceremony import scoped_git_commit as sgc

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    probe_calls = []
    monkeypatch.setattr(
        sgc,
        "_remote_sha_state",
        lambda *a, **k: probe_calls.append((a, k)) or sgc._REMOTE_UNKNOWN,
    )

    pushed, push_state = sgc._resolve_push_report(str(repo), sha, sgc.PUSH_STATUS_DECLINED)

    assert push_state == sgc.PUSH_STATE_DECLINED
    assert pushed is None
    assert probe_calls == []


def test_unknown_push_status_still_probes_remote(tmp_path, monkeypatch):
    """The regression that matters most: the short-circuit added for
    `PUSH_STATUS_DECLINED` must not swallow the genuinely-unknown case --
    anything other than pushed/declined/no-remote still reaches the remote
    probe exactly as before this change.
    """
    from coordinator_core.ops.ceremony import scoped_git_commit as sgc

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    probe_calls = []

    def _fake_probe(*a, **k):
        probe_calls.append((a, k))
        return sgc._REMOTE_PRESENT

    monkeypatch.setattr(sgc, "_remote_sha_state", _fake_probe)

    pushed, push_state = sgc._resolve_push_report(str(repo), sha, sgc.PUSH_STATUS_NOT_ATTEMPTED)

    assert push_state == sgc.PUSH_STATE_PUSHED
    assert pushed is True
    assert len(probe_calls) == 1


def test_no_remote_push_status_maps_to_push_state_no_remote(tmp_path):
    """`PUSH_STATUS_NO_REMOTE` maps onto the existing `PUSH_STATE_NO_REMOTE`
    rather than falling through to the unknown/probe path.
    """
    from coordinator_core.ops.ceremony import scoped_git_commit as sgc

    pushed, push_state = sgc._resolve_push_report(str(tmp_path), None, sgc.PUSH_STATUS_NO_REMOTE)

    assert push_state == sgc.PUSH_STATE_NO_REMOTE
    assert pushed is None
