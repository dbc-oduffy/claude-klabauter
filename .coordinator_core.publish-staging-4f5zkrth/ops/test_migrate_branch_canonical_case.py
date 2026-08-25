"""
coordinator_core.ops.test_migrate_branch_canonical_case — behavior-parity
tests for the naked-Python port (DR-059).

Tests exercise the module against real `git` subprocesses in `tmp_path`-scoped
throwaway repos (hermetic; no shared/real repo state touched) and assert
parity with the golden-oracle behavior captured from the retired bash script
during the port (see module docstring "Negative-spec" for the two documented
pre-existing bash-oracle behaviors reproduced verbatim, NOT "fixed").

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292
Port of: migrate-branch-canonical-case.sh (DoE b5a4192c, 2026-07-20)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.ops import migrate_branch_canonical_case as mbcc  # noqa: E402

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )


def _mkrepo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "t@t.com").returncode == 0
    assert _git(repo, "config", "user.name", "t").returncode == 0
    (repo / "f.txt").write_text("x\n")
    assert _git(repo, "add", "f.txt").returncode == 0
    assert _git(repo, "commit", "-q", "-m", "init").returncode == 0


def _run(repo: Path, args, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    rc = mbcc.main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# Usage / argument handling
# ---------------------------------------------------------------------------


def test_git_timeout_degrades_to_synthetic_nonzero_completed_process(monkeypatch):
    """Review: code-reviewer (F4, P2) — `_git`'s `TimeoutExpired` -> synthetic
    non-zero `CompletedProcess` degradation was the only spawn/timeout change
    in this slice shipped without test coverage. Pins that a timed-out call
    returns a `CompletedProcess` (not a raised exception) with
    `returncode == 1`, so callers' existing `.returncode` checks see it as an
    ordinary git failure rather than aborting mid-flight."""

    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["git", "status"], timeout=1.0)

    monkeypatch.setattr(mbcc.subprocess, "run", _raise_timeout)

    result = mbcc._git("/some/repo", "status", timeout=1.0)

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 1
    assert "timed out" in result.stderr


def test_unknown_argument_exits_1(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    rc, out, err = _run(repo, ["--bogus"], monkeypatch, capsys)
    assert rc == 1
    assert "ERROR: unknown argument: --bogus" in err
    assert "Usage: migrate-branch-canonical-case.sh [--push-cleanup]" in err


def test_help_prints_verbatim_header_and_exits_0(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    rc, out, err = _run(repo, ["-h"], monkeypatch, capsys)
    assert rc == 0
    assert out == mbcc._USAGE_TEXT + "\n"
    assert "Idempotent: a second invocation finds no mixed-case refs" in out


def test_not_a_git_repo_exits_1(tmp_path, monkeypatch, capsys):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    rc, out, err = _run(not_a_repo, [], monkeypatch, capsys)
    assert rc == 1
    assert "ERROR: not in a git repo" in err


# ---------------------------------------------------------------------------
# Step 1: mixed-case ref rename
# ---------------------------------------------------------------------------


def test_single_segment_mixed_case_branch_is_renamed_or_skipped_by_fs(
    tmp_path, monkeypatch, capsys
):
    """On a case-INSENSITIVE filesystem (default macOS/Windows), `git
    show-ref --verify refs/heads/work/mixedcase` resolves case-insensitively
    to the same loose-ref file as `work/MixedCase` -- so the script's own
    "does a canonical sibling already exist?" check reports SKIP even though
    only one branch was ever created. This is identical behavior in the
    retired bash oracle (verified empirically against a live bash run on this
    FS during the port) -- both the RENAME and this FS-driven SKIP are valid
    outcomes; the two must simply agree with each other on this filesystem.
    """
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "branch", "work/MixedCase").returncode == 0

    rc, out, err = _run(repo, [], monkeypatch, capsys)

    assert rc == 0
    assert ("RENAME: work/MixedCase → work/mixedcase" in out and "Renamed: 1" in out) or (
        "SKIP: 'work/MixedCase' — canonical sibling 'work/mixedcase' already exists" in out
        and "Skipped: 1" in out
    )
    # Either way, the original mixed-case branch was never force-deleted.
    assert _git(repo, "show-ref", "--verify", "--quiet", "refs/heads/work/mixedcase").returncode == 0


def test_mixed_case_branch_with_existing_canonical_sibling_is_skipped_not_deleted(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "branch", "work/dup").returncode == 0
    _git(repo, "pack-refs", "--all")
    sha = _git(repo, "rev-parse", "work/dup").stdout.strip()
    packed_refs = repo / ".git" / "packed-refs"
    # newline="" — text-mode append translates "\n" to "\r\n" on Windows, and the
    # CR lands inside the ref name: git then reports `ignoring ref with broken
    # name refs/heads/work/DUP`, the sibling never exists, and this test's SKIP
    # assertion fails against a run that correctly found nothing to skip.
    with packed_refs.open("a", newline="") as f:
        f.write(f"{sha} refs/heads/work/DUP\n")

    rc, out, err = _run(repo, [], monkeypatch, capsys)

    assert rc == 0
    assert "SKIP: 'work/DUP' — canonical sibling 'work/dup' already exists" in out
    assert "git branch -D 'work/DUP'" in out
    assert "Skipped: 1" in out
    # Neither ref was force-deleted by the script itself.
    assert _git(repo, "show-ref", "--verify", "--quiet", "refs/heads/work/dup").returncode == 0


def test_no_mixed_case_refs_is_a_pure_noop(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "branch", "work/already-lower").returncode == 0

    rc, out, err = _run(repo, [], monkeypatch, capsys)

    assert rc == 0
    assert "Renamed: 0" in out
    assert "Skipped: 0" in out
    assert "Failed:  0" in out


def test_rerun_after_first_pass_is_idempotent(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "branch", "work/MixedCase").returncode == 0
    _run(repo, [], monkeypatch, capsys)  # first pass: rename or FS-driven skip

    rc, out, err = _run(repo, [], monkeypatch, capsys)

    assert rc == 0
    assert "Renamed: 0" in out
    # A second pass never re-renames; it either finds nothing left mixed-case
    # (0 skipped) or keeps flagging the same never-auto-deleted branch (1
    # skipped, unchanged from pass 1) -- both are valid idempotent outcomes.
    assert "Skipped: 0" in out or "Skipped: 1" in out


# ---------------------------------------------------------------------------
# Negative-spec: FNM_PATHNAME glob-pattern bug reproduced verbatim
# ---------------------------------------------------------------------------


def test_two_segment_daily_convention_branch_is_not_matched_bash_oracle_bug(
    tmp_path, monkeypatch, capsys
):
    """Reproduces the retired bash script's `git for-each-ref
    'refs/heads/work/*'` FNM_PATHNAME limitation: a single `*` does not match
    `/`, so this project's real daily-branch convention
    (`work/{machine}/{date}`, two segments after `work/`) is silently NOT
    enumerated. This is NOT a bug this port introduces or should fix -- it is
    the byte-parity oracle's own pre-existing behavior. See module docstring
    Negative-spec.
    """
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "branch", "work/MACHINE-A/2026-07-16").returncode == 0

    rc, out, err = _run(repo, [], monkeypatch, capsys)

    assert rc == 0
    assert "Renamed: 0" in out
    assert "Skipped: 0" in out
    # The mixed-case two-segment ref is untouched -- still present, unrenamed.
    assert (
        _git(repo, "show-ref", "--verify", "--quiet", "refs/heads/work/MACHINE-A/2026-07-16").returncode
        == 0
    )


def test_enumerate_work_refs_matches_only_single_segment_names(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "branch", "work/one-level").returncode == 0
    assert _git(repo, "branch", "work/two/levels").returncode == 0

    refs = mbcc._enumerate_work_refs(str(repo))

    assert refs == ["work/one-level"]


# ---------------------------------------------------------------------------
# Step 0: HEAD-text fix
# ---------------------------------------------------------------------------


def test_head_text_fix_rewrites_mixed_case_symbolic_ref(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "checkout", "-q", "-b", "work/head-case/2026-07-16").returncode == 0
    (repo / ".git" / "HEAD").write_text(
        "ref: refs/heads/work/Head-Case/2026-07-16\n"
    )

    rc, out, err = _run(repo, [], monkeypatch, capsys)

    assert rc == 0
    assert (
        "HEAD-FIX: refs/heads/work/Head-Case/2026-07-16 → "
        "refs/heads/work/head-case/2026-07-16" in out
    )
    head_after = _git(repo, "symbolic-ref", "HEAD").stdout.strip()
    assert head_after == "refs/heads/work/head-case/2026-07-16"


def test_head_text_already_canonical_is_a_noop(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "checkout", "-q", "-b", "work/already-lower/2026-07-16").returncode == 0

    rc, out, err = _run(repo, [], monkeypatch, capsys)

    assert rc == 0
    assert "HEAD-FIX:" not in out
    assert "HEAD-FIX-SKIP:" not in out


def test_head_text_mismatch_without_canonical_sibling_is_skip_not_crash(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "checkout", "-q", "-b", "work/OnlyMixedCase").returncode == 0
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/work/OnlyMixedCase\n")
    # No lowercase sibling exists on disk at all (simulate a detached-looking
    # HEAD text with no real canonical counterpart) -- for-each-ref won't find
    # refs/heads/work/onlymixedcase as a distinct ref because the underlying
    # loose-ref file IS the mixed-case one and show-ref resolves it
    # case-insensitively on this FS, so this asserts the code path doesn't
    # crash regardless of FS case-sensitivity.
    rc, out, err = _run(repo, [], monkeypatch, capsys)
    assert rc in (0, 2)


# ---------------------------------------------------------------------------
# push-cleanup mode without a configured remote
# ---------------------------------------------------------------------------


def test_push_cleanup_without_remote_does_not_crash(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "branch", "work/pushcase").returncode == 0

    rc, out, err = _run(repo, ["--push-cleanup"], monkeypatch, capsys)

    assert rc == 0
    assert "Mode: local + remote cleanup" in out
    assert "Renamed: 0" in out


def test_push_cleanup_attempts_remote_push_when_a_rename_actually_happens(
    tmp_path, monkeypatch, capsys
):
    # Two-level branches never collide case-insensitively with themselves the
    # way `test_single_segment_mixed_case_branch_is_renamed_or_skipped_by_fs`
    # documents for a bare `work/MixedCase` -- but per the FNM_PATHNAME
    # negative-spec, two-level names aren't enumerated at all. So exercise
    # the RENAME + push-cleanup path with a distinct sibling-free
    # single-segment name and accept the FS-driven SKIP alternative too, same
    # as the sibling rename test above.
    repo = tmp_path / "repo"
    _mkrepo(repo)
    assert _git(repo, "branch", "work/PushMixedCase").returncode == 0

    rc, out, err = _run(repo, ["--push-cleanup"], monkeypatch, capsys)

    assert rc == 0
    if "RENAME: work/PushMixedCase → work/pushmixedcase" in out:
        # No remote configured -> ls-remote fails (non-zero), so no
        # REMOTE-DELETE line, but the REMOTE-PUSH attempt still happens and
        # warns rather than crashing.
        assert "REMOTE-DELETE" not in out
        assert "REMOTE-PUSH: origin/work/pushmixedcase" in out
        assert "WARN: push of 'work/pushmixedcase' returned non-zero" in out
    else:
        assert "SKIP: 'work/PushMixedCase'" in out


# ---------------------------------------------------------------------------
# Exit-code parity summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,expect_rc",
    [
        ([], 0),
        (["--push-cleanup"], 0),
        (["-h"], 0),
        (["--help"], 0),
    ],
)
def test_exit_codes_on_clean_repo(tmp_path, monkeypatch, capsys, args, expect_rc):
    repo = tmp_path / "repo"
    _mkrepo(repo)
    rc, out, err = _run(repo, args, monkeypatch, capsys)
    assert rc == expect_rc


def test_bad_arg_short_circuits_before_git_root_lookup():
    # main() reads cwd via os.getcwd(); a bogus argument short-circuits
    # before any git-root lookup, so this doesn't need a real repo/cwd chdir.
    rc = mbcc.main(["--nope"])
    assert rc == 1
