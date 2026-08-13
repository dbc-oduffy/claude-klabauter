"""test_merge_recovery_and_tag_cut — pytest tests for merge-recovery-and-tag-cut.py.

Covers the two genuinely imperative cores ported from example-doctrine-repo's
merging-to-main SKILL.md: the idempotent annotated-tag cut (`cut_tag`) and the
`tag_prefix:` frontmatter parse (`resolve_tag_prefix`), including its
fail-loud quoted-value branch.

Spec backlink: coordinator/bin/merge-recovery-and-tag-cut.py module docstring.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

# Declared, not excused: 6 of this file's tests spawn a real git process because the
# property under test is git's own behaviour -- idempotent annotated-tag cut/push
# (test_cut_tag_*) and branch/HEAD state after a real recovery-branch dance
# (test_*_verdict_*), neither reproducible against a mock. Each mutation test needs
# its own fresh repo (tag-cut idempotency, branch creation, and push-landing checks
# all depend on starting from a known-clean state), so the per-test
# `_init_repo_with_origin` fixture is not hoisted to module scope -- see
# test_verify_shipped.py's docstring for the failure mode that hoisting produces here.
# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is explicitly
# not the route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "merge_recovery_and_tag_cut",
        _BIN_DIR / "merge-recovery-and-tag-cut.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()
cut_tag = _mod.cut_tag
resolve_tag_prefix = _mod.resolve_tag_prefix
cmd_recovery_branch = _mod.cmd_recovery_branch


# ---------------------------------------------------------------------------
# Helpers — minimal git repo + bare "origin" remote factory
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _init_repo_with_origin(tmp_path: Path) -> Path:
    """Bare 'origin' remote + a work clone with one commit on main, pushed."""
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)

    work = tmp_path / "work"
    work.mkdir()
    _git(["init"], cwd=work)
    _git(["config", "user.email", "test@example.com"], cwd=work)
    _git(["config", "user.name", "Test User"], cwd=work)
    _git(["checkout", "-b", "main"], cwd=work)
    (work / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "f.txt"], cwd=work)
    _git(["commit", "-m", "initial"], cwd=work)
    _git(["remote", "add", "origin", str(origin)], cwd=work)
    _git(["push", "-u", "origin", "main"], cwd=work)
    return work


# ---------------------------------------------------------------------------
# cut_tag
# ---------------------------------------------------------------------------

def test_cut_tag_creates_and_pushes_annotated_tag(tmp_path: Path) -> None:
    work = _init_repo_with_origin(tmp_path)
    head_sha = _git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

    cut, merge_sha = cut_tag(work, "v1.0.0")

    assert cut is True
    assert merge_sha == head_sha

    # Tag object is annotated (not lightweight) and peels to the commit.
    tag_type = _git(["cat-file", "-t", "v1.0.0"], cwd=work).stdout.strip()
    assert tag_type == "tag"
    peeled = _git(["rev-parse", "v1.0.0^{}"], cwd=work).stdout.strip()
    assert peeled == head_sha

    # Pushed to origin, not just local.
    origin_tags = _git(["ls-remote", "--tags", "origin"], cwd=work).stdout
    assert "refs/tags/v1.0.0" in origin_tags


def test_cut_tag_is_idempotent_on_retry(tmp_path: Path) -> None:
    """Second call against an unchanged origin/main skips — no re-tag error."""
    work = _init_repo_with_origin(tmp_path)

    first_cut, first_sha = cut_tag(work, "v1.0.0")
    assert first_cut is True

    second_cut, second_sha = cut_tag(work, "v1.0.0")
    assert second_cut is False
    assert second_sha == first_sha


# ---------------------------------------------------------------------------
# resolve_tag_prefix
# ---------------------------------------------------------------------------

def test_resolve_tag_prefix_extracts_value(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "project_type: game-dev\n"
        "tag_prefix: example-game-repo-\n"
        "---\n"
        "# Body\n",
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == "example-game-repo-"


def test_resolve_tag_prefix_strips_inline_comment(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "tag_prefix: example-game-repo-  # namespace prefix\n"
        "---\n",
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == "example-game-repo-"


def test_resolve_tag_prefix_absent_key_returns_empty(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        "project_type: general\n"
        "---\n"
        "tag_prefix: should-not-be-seen\n",  # outside frontmatter — must be ignored
        encoding="utf-8",
    )
    assert resolve_tag_prefix(config) == ""


def test_resolve_tag_prefix_quoted_value_fails_loud(tmp_path: Path) -> None:
    config = tmp_path / "coordinator.local.md"
    config.write_text(
        "---\n"
        'tag_prefix: "example-game-repo-"\n'
        "---\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc_info:
        resolve_tag_prefix(config)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# recovery-branch liveness gate
# ---------------------------------------------------------------------------
#
# Spec backlink: coordinator_core.session.worktree_safety.branch_mutation_verdict
# and its precedent application in coordinator/lib/session_ensure_branch.py
# (commit bc756ce3f534). This subcommand is strictly worse than that seam
# because it hard-resets main in addition to switching branches, so it MUST
# consult the same verdict before its first git mutation.

class _FakeVerdict:
    def __init__(self, outcome: str, reason: str) -> None:
        self.outcome = outcome
        self.reason = reason


class _Args:
    def __init__(self, repo_root: Path, branch_name: str = "work/testhost/2026-08-07") -> None:
        self.repo_root = str(repo_root)
        self.branch_name = branch_name


def _current_branch(work: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=work).stdout.strip()


def _head_sha(work: Path, ref: str = "HEAD") -> str:
    return _git(["rev-parse", ref], cwd=work).stdout.strip()


def test_refused_verdict_blocks_all_mutation(tmp_path: Path, monkeypatch) -> None:
    work = _init_repo_with_origin(tmp_path)
    before_branch = _current_branch(work)
    before_sha = _head_sha(work)
    before_branches = _git(["branch"], cwd=work).stdout

    monkeypatch.setattr(
        _mod,
        "_branch_mutation_verdict",
        lambda: (lambda cwd=None: _FakeVerdict(
            "refused", "1 live peer session(s): abc123 (on main)"
        )),
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_recovery_branch(_Args(work))
    assert exc_info.value.code == 1

    assert _current_branch(work) == before_branch
    assert _head_sha(work) == before_sha
    assert _git(["branch"], cwd=work).stdout == before_branches
    assert "work/testhost/2026-08-07" not in before_branches


def test_unknown_verdict_treated_same_as_refused(tmp_path: Path, monkeypatch) -> None:
    work = _init_repo_with_origin(tmp_path)
    before_branch = _current_branch(work)
    before_sha = _head_sha(work)
    before_branches = _git(["branch"], cwd=work).stdout

    monkeypatch.setattr(
        _mod,
        "_branch_mutation_verdict",
        lambda: (lambda cwd=None: _FakeVerdict(
            "unknown", "cannot resolve live-session set"
        )),
    )

    with pytest.raises(SystemExit) as exc_info:
        cmd_recovery_branch(_Args(work))
    assert exc_info.value.code == 1

    assert _current_branch(work) == before_branch
    assert _head_sha(work) == before_sha
    assert _git(["branch"], cwd=work).stdout == before_branches


def test_ok_verdict_still_runs_recovery_dance(tmp_path: Path, monkeypatch, capsys) -> None:
    work = _init_repo_with_origin(tmp_path)

    monkeypatch.setattr(
        _mod,
        "_branch_mutation_verdict",
        lambda: (lambda cwd=None: _FakeVerdict("ok", "no live peer sessions")),
    )

    rc = cmd_recovery_branch(_Args(work))
    assert rc == 0

    out = capsys.readouterr().out
    assert "BRANCH=work/testhost/2026-08-07" in out
    assert _current_branch(work) == "work/testhost/2026-08-07"
    assert "refs/heads/work/testhost/2026-08-07" in _git(
        ["ls-remote", "--heads", "origin"], cwd=work
    ).stdout
