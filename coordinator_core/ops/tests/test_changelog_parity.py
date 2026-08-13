"""
coordinator_core.ops.tests.test_changelog_parity — byte-parity harness for changelog ops.

Purpose: Assert the Python ops (changelog.append_day / changelog.backfill_gaps)
produce byte-identical file content to their example-doctrine-repo oracle CLIs
(workday-complete-step9-append-changelog.py / backfill-week-changelog-gaps.py)
for the same inputs.

When example-doctrine-repo oracle scripts are absent (~/.claude/.doe-root not set or scripts missing), all
oracle-comparison tests are skipped with an informative message. The tests still run the
native op in isolation to verify basic smoke behavior.

Coverage:
    (a) append_day byte-parity — date 2000-01-01 (no commits in window) against step9 oracle
    (b) backfill_gaps byte-parity — today's date with one commit against backfill oracle
    (c) smoke: native append_day writes valid file with correct format when oracle absent
    (d) smoke: native backfill_gaps skips when HEADER.md absent

Spec backlink: docs/plans/2026-07-06-strang-10-residual-writer-strangle-command-type.md § C1
Oracle (a): [example-doctrine-repo] coordinator/bin/workday-complete-step9-append-changelog.py
Oracle (b): [example-doctrine-repo] coordinator/bin/backfill-week-changelog-gaps.py
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires all @register_op(...) side-effects including changelog ops.
# Must precede all test functions.
# Note: changelog_ops is not yet wired into coordinator_core/ops/__init__.py
# (that wiring is C3's job). Import it here explicitly to trigger @register_op.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY from existing ops
import coordinator_core.ops.changelog_ops  # noqa: F401 — registers changelog.{append_day,backfill_gaps,inject_anchor}

from coordinator_core.ipc import _REGISTRY

assert len(_REGISTRY) > 0, (
    "registry is empty after import — "
    "all @register_op decorators must have fired at module import time"
)
assert "changelog.append_day" in _REGISTRY, (
    "import guard failed: 'changelog.append_day' not in _REGISTRY — "
    "coordinator_core.ops.changelog_ops @register_op did not fire"
)
assert "changelog.backfill_gaps" in _REGISTRY, (
    "import guard failed: 'changelog.backfill_gaps' not in _REGISTRY"
)
assert "changelog.inject_anchor" in _REGISTRY, (
    "import guard failed: 'changelog.inject_anchor' not in _REGISTRY"
)

from coordinator_core.ops.changelog_ops import (  # noqa: E402
    append_day,
    backfill_gaps,
    inject_anchor,
    _anchor_present,
    _compose_block,
    _today_utc,
)

# The byte-parity assertions (append_day/backfill_gaps window-commit content)
# read commits from a REAL repo so the emitted bytes reflect actual git log
# output, matching what the example-doctrine-repo oracle CLIs themselves read — a mocked git
# would validate against invented commit data, not the oracle's real input.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Oracle path resolution (reads ~/.claude/.doe-root sentinel)
# ---------------------------------------------------------------------------

_DOE_ROOT_SENTINEL = Path.home() / ".claude" / ".doe-root"
_DOE_ROOT: Optional[str] = None

_DOE_APPEND_ORACLE: Optional[Path] = None
_DOE_BACKFILL_ORACLE: Optional[Path] = None

if _DOE_ROOT_SENTINEL.exists():
    try:
        _DOE_ROOT = _DOE_ROOT_SENTINEL.read_text(encoding="utf-8").strip()
        _doe = Path(_DOE_ROOT) / "coordinator" / "bin"
        _DOE_APPEND_ORACLE = _doe / "workday-complete-step9-append-changelog.py"
        _DOE_BACKFILL_ORACLE = _doe / "backfill-week-changelog-gaps.py"
    except OSError:
        print(f"skip: <module>: _DOE_ROOT = _DOE_ROOT_SENTINEL.read_text(encoding=\"utf-8\").strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass

_ORACLE_APPEND_AVAILABLE = bool(
    _DOE_APPEND_ORACLE and _DOE_APPEND_ORACLE.is_file()
)
_ORACLE_BACKFILL_AVAILABLE = bool(
    _DOE_BACKFILL_ORACLE and _DOE_BACKFILL_ORACLE.is_file()
)

# ---------------------------------------------------------------------------
# inject_anchor oracle resolution — this oracle is a claude-klabauter-owned in-repo CLI
# (coordinator/bin/workday-complete-backfill-inject-anchor.py), NOT a example-doctrine-repo
# artifact behind the ~/.claude/.doe-root sentinel used above. Resolved
# directly relative to this repo; skipped gracefully (not failed hard) if the
# file is ever moved/absent, per the strang-10 handoff's dispatch note.
# ---------------------------------------------------------------------------

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
_INJECT_ANCHOR_ORACLE = (
    _CLAUDE_KLABAUTER_ROOT / "coordinator" / "bin" / "workday-complete-backfill-inject-anchor.py"
)
_ORACLE_INJECT_ANCHOR_AVAILABLE = _INJECT_ANCHOR_ORACLE.is_file()

_requires_inject_anchor_oracle = pytest.mark.skipif(
    not _ORACLE_INJECT_ANCHOR_AVAILABLE,
    reason=(
        "workday-complete-backfill-inject-anchor.py oracle not found at its "
        "expected in-repo path — skipping gracefully rather than failing hard"
    ),
)

_requires_append_oracle = pytest.mark.skipif(
    not _ORACLE_APPEND_AVAILABLE,
    reason="step9-append-changelog oracle not available (example-doctrine-repo not configured or absent)",
)
_requires_backfill_oracle = pytest.mark.skipif(
    not _ORACLE_BACKFILL_AVAILABLE,
    reason="backfill-week-changelog-gaps oracle not available (example-doctrine-repo not configured or absent)",
)

# ---------------------------------------------------------------------------
# Git repo fixture helpers
# ---------------------------------------------------------------------------

# Minimal git config so commits work in isolated test trees.
_GIT_ENV_BASE = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

#: Fixed author/committer date for `_make_git_repo`'s `commit_env` param —
#: pins the initial empty commit's SHA so two independently-created repos
#: (same tree, no parent, same message, now same dates) hash identically.
#: Needed wherever a `full_sha` computed against one repo must resolve
#: against another (see `TestInjectAnchorByteParity`'s two-repo setup).
_FIXED_INITIAL_COMMIT_ENV = {
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


def _git(repo: Path, *args: str, extra_env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run a git command in `repo`, raise on non-zero exit."""
    env = os.environ.copy()
    env.update(_GIT_ENV_BASE)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args!r} failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _make_git_repo(
    base: Path, branch_name: str = "main", *, commit_env: Optional[dict] = None
) -> Path:
    """Initialize a git repo at base, create an initial empty commit, return the repo path.

    Uses `git init` followed by `git checkout -b` to set a known branch, avoiding
    reliance on `init.defaultBranch` config which varies across environments.

    `commit_env` (e.g. fixed GIT_AUTHOR_DATE/GIT_COMMITTER_DATE) lets a caller
    pin the initial commit's SHA deterministically — needed by tests that
    compare two independently-created repos byte-for-byte (a `full_sha`
    computed in one must actually resolve in the other; see
    `TestInjectAnchorByteParity`, whose two-repo setup relies on repo_a's
    and repo_b's initial commit hashing identically).
    """
    repo = base / "repo"
    repo.mkdir(parents=True)
    env = os.environ.copy()
    env.update(_GIT_ENV_BASE)

    subprocess.run(
        ["git", "init", str(repo)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Checkout a known branch name so tests have deterministic branch expectations.
    _git(repo, "checkout", "-b", branch_name)
    _git(repo, "commit", "--allow-empty", "-m", "initial: test repo setup", extra_env=commit_env)
    return repo


def _read_branch(repo: Path) -> str:
    """Return the current branch of `repo` (git branch --show-current)."""
    r = _git(repo, "branch", "--show-current")
    return r.stdout.strip()


def _commit_on_date(repo: Path, date: str, message: str) -> str:
    """Create an empty commit dated at noon UTC on `date`; return its full SHA.

    Used by the content-gap-guard tests to build deterministic commit windows
    independent of wall-clock time (the guards' git-log queries are date-window
    scoped, same as the oracle's)."""
    iso = f"{date}T12:00:00+00:00"
    _git(repo, "commit", "--allow-empty", "-m", message, extra_env={
        "GIT_AUTHOR_DATE": iso, "GIT_COMMITTER_DATE": iso,
    })
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _seed_daily_summary(repo: Path, date: str, machine: str, body: Optional[str] = None) -> Path:
    """Seed archive/daily-summaries/{date}-{machine}.md — the CURRENT production
    filename shape (§ strang-10 handoff / retired plan reconciliation pass item
    2: the real directory is a mix of this shape and legacy bare "{date}.md"
    files; every fixture in this module seeds the current shape explicitly,
    never a helper that assumes uniform naming across the directory)."""
    ds_dir = repo / "archive" / "daily-summaries"
    ds_dir.mkdir(parents=True, exist_ok=True)
    target = ds_dir / f"{date}-{machine}.md"
    target.write_text(body or "# Daily Summary\n\nSome pre-existing narrative content.\n", encoding="utf-8")
    return target


def _get_short_hostname() -> str:
    """Return the system's short hostname (matches oracle's `hostname -s || hostname`)."""
    for args in (["hostname", "-s"], ["hostname"]):
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            print(f"skip: _get_short_hostname: r = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
    import socket
    return socket.gethostname().split(".")[0]


# ---------------------------------------------------------------------------
# Oracle runner helpers
# ---------------------------------------------------------------------------

def _run_oracle_append_day(
    oracle: Path,
    repo: Path,
    date: str,
    machine: str,
    rc_validate: str = "0",
    rc_plugin_suite: str = "0",
) -> bytes:
    """Run the step9-append-changelog oracle in a controlled env and return output file bytes.

    Runs with CWD=$repo so:
      - `git rev-parse --show-toplevel` resolves to repo (coordinator_state_root)
      - `git branch --show-current` (used by coordinator-current-branch) resolves from repo
      - `git log` commit queries run against repo

    Sets COORDINATOR_ROOT_WARN_SUPPRESS=1 so the test-override warning is silenced when
    COORDINATOR_ROOT matches the resolved CWD toplevel (may differ on symlinked tmpdirs).

    Oracle-reader path note (runtime-discovered, deviates from the original dispatch
    brief): the step9-append-changelog.sh oracle's Zone B (write) already finished its
    strangler cutover to an unconditional native dispatch via cc_invoke changelog.append_day
    (see the oracle script's own header comment) — it is NOT independent legacy bash, it
    IS the same native append_day() this parity harness is exercising. Once C3's per-day
    filename collapse landed, the oracle's real on-disk output moved from
    "{date}-{machine}.md" to the collapsed "{date}.md" in lockstep (confirmed empirically:
    the oracle's own captured stdout JSON shows out_path=".../{date}.md"). The reader path
    below was updated to match; the oracle's internal "[step9] block written: ...-{machine}.md"
    log line is stale progress-message text from before the Zone B cutover and does not
    reflect where the file actually lands.
    """
    assert _DOE_APPEND_ORACLE is not None
    env = os.environ.copy()
    env.update(_GIT_ENV_BASE)
    env["COORDINATOR_ROOT"] = str(repo.resolve())
    env["COORDINATOR_ROOT_WARN_SUPPRESS"] = "1"
    env["COORDINATOR_MACHINE"] = machine
    env["RC_VALIDATE"] = rc_validate
    env["RC_PLUGIN_SUITE"] = rc_plugin_suite
    env.pop("CLAUDE_KLABAUTER_ROOT", None)

    result = subprocess.run(
        [sys.executable, str(_DOE_APPEND_ORACLE), "--for-date", date, "--no-push"],
        env=env,
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Oracle may exit non-zero for warnings (e.g. push skip). Accept 0 and 2 (push rejected).
    # Exit 3 = HEADER staleness skip (--for-date bypasses this; treat as failure here).
    assert result.returncode in (0, 2), (
        f"oracle step9-append-changelog exited {result.returncode} for date={date!r}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Per-day filename collapse (PM ruling 2026-07-19, AC11/DEC-4): the oracle's Zone B
    # already fully delegates to native changelog.append_day, so its real output path
    # collapsed to "{date}.md" in lockstep with C3's change — NOT "{date}-{machine}.md".
    out_file = repo / "state" / "week-changelog" / f"{date}.md"
    assert out_file.exists(), (
        f"oracle output file not found at {out_file}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return out_file.read_bytes()


def _run_oracle_backfill_gaps(
    oracle: Path,
    repo: Path,
    today: str,
) -> Optional[Path]:
    """Run the backfill-week-changelog-gaps oracle (CWD=$repo). Returns path to written file.

    today is used only to confirm which file the oracle should have written.
    Oracle computes HOST internally from `hostname -s`. Returns None if oracle wrote nothing.
    """
    assert _DOE_BACKFILL_ORACLE is not None
    env = os.environ.copy()
    env.update(_GIT_ENV_BASE)
    env.pop("CLAUDE_KLABAUTER_ROOT", None)
    # Do NOT set COORDINATOR_MACHINE — backfill oracle uses hostname, not COORDINATOR_MACHINE.

    result = subprocess.run(
        [sys.executable, str(_DOE_BACKFILL_ORACLE)],
        env=env,
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Advisory oracle: trap 'exit 0' ERR — always exits 0.
    assert result.returncode == 0, (
        f"backfill oracle exited non-zero {result.returncode}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Find any backfill file written for `today` by any machine.
    week_dir = repo / "state" / "week-changelog"
    candidates = sorted(week_dir.glob(f"{today}-*-backfill.md"))
    if not candidates:
        return None
    return candidates[0]


def _run_oracle_inject_anchor(
    repo: Path,
    date: str,
    sha: str,
    today: Optional[str] = None,
    machine: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run the in-repo workday-complete-backfill-inject-anchor.py oracle.

    Usage: <ROOT> <DATE> <DESCENDANT_TIP_SHA> [TODAY] [MACHINE]. `machine` is
    always passed explicitly by every call site in this module so the oracle
    never falls into its own `_derive_machine` ref-enumeration / cc_invoke
    fallback path (which depends on host machine-local registry state
    unrelated to what this parity harness is verifying).

    Exit codes: 0 injected/bumped, 10 already-anchored, 20 summary-absent,
    30 content-gap guard fired, 1 usage/malformed-structure error.
    """
    assert _INJECT_ANCHOR_ORACLE.is_file()
    env = os.environ.copy()
    env.update(_GIT_ENV_BASE)
    argv = [sys.executable, str(_INJECT_ANCHOR_ORACLE), str(repo), date, sha]
    if today is not None or machine is not None:
        argv.append(today or "")
    if machine is not None:
        argv.append(machine)
    return subprocess.run(
        argv,
        env=env,
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ===========================================================================
# Tests: append_day byte-parity
# ===========================================================================


@_requires_append_oracle
@pytest.mark.real_home  # oracle subprocess resolves CLAUDE_KLABAUTER_ROOT via the real machine-local
# registry / <settings-home>/machine-local/.claude-klabauter-root pointer — see note below.
class TestAppendDayByteParity:
    """Facade-wiring parity: changelog.append_day env/arg mapping vs. step9-append-changelog.sh
    (strang-10 AC).

    Review: code-reviewer (F4) — this class name/docstring previously read as an
    independent-implementation byte-parity comparison ("changelog.append_day ==
    step9-append-changelog.sh"). That framing is no longer accurate: the oracle's
    Zone B (write) has completed its strangler cutover to an unconditional native
    dispatch via cc_invoke changelog.append_day (see `_run_oracle_append_day`'s
    docstring) — the oracle IS the same native `append_day()` this test calls
    directly, not an independent legacy-bash implementation. A bug inside
    `append_day()`/`_compose_block()` would reproduce identically on both sides
    and this test would still pass. What this class DOES still verify, and
    verifies validly, is the oracle-facade's env/arg → native-params mapping
    (--for-date, COORDINATOR_MACHINE, RC_VALIDATE, RC_PLUGIN_SUITE, etc. reaching
    native append_day with the fields the test expects) — not independent-
    implementation byte parity of the write logic itself. See the
    "differential ≠ correctness, pin an independent oracle" lesson
    (coverage-gate-single-graph-walk-shipped-reviewed memory entry).

    Strategy: use --for-date=2000-01-01 so the oracle's commit window is empty (no commits
    in a fresh 2026 test repo fall in year 2000). All computed fields fall to deterministic
    defaults (commit_count=0, commit_range='n/a', scope='' (Scope line omitted — oracle C4
    omit-by-default), all 'none').
    The branch name is known because the test fixture creates the repo on 'test-branch'.
    Both oracle and native run against the same controlled git repo.

    ``@pytest.mark.real_home`` (added — Cluster B triage, 2026-07-21): without this
    marker the suite-root ``_quarantine_real_home`` autouse fixture (coordinator_core/
    conftest.py) points ``HOME``/``USERPROFILE`` at a throwaway per-test tmpdir *before*
    this test body runs. ``_run_oracle_append_day`` does ``env = os.environ.copy()``,
    which inherits that quarantined HOME into the oracle subprocess's environment. The
    oracle's CLAUDE_KLABAUTER_ROOT resolver (coordinator-claude-klabauter-root.sh, sourced transitively via
    coordinator-core-invoke.sh's ``_cc_resolve_deps``) then looks for the settings-home
    pointer file and the machine-local registry entry *under the quarantined HOME*,
    finds neither, and hard-fails with "repos.claude_klabauter is not set" — even on a
    machine where the real registry has that key populated. This is a false negative
    from test-isolation leakage, NOT a genuine host-config gap (verified: the real
    ``coordinator_claude_klabauter_root`` resolves correctly outside the quarantine). The write
    target (``COORDINATOR_ROOT``, set explicitly to a controlled ``tmp_path`` repo) never
    touches real ``$HOME``, so opting this class out of the quarantine is safe under the
    same "read-only-ish, tmp-scoped writes only" contract ``real_home``'s docstring
    requires — this mirrors ``TestReconcileCommitsByteParity`` in
    ``test_completion_parity.py``, which already carries the marker for the identical
    reason.
    """

    def test_byte_parity_fresh_file(self, tmp_path):
        """Oracle and native produce byte-identical files for an empty-day date.

        Review: code-reviewer (F2) — oracle and native now run in separate repos (repo_a /
        repo_b) so native sees a fresh empty directory and exercises the create-new-file path,
        not the idempotency path triggered when the oracle's file is already present.
        """
        # Two independent repos from the same initial state — oracle in repo_a, native in repo_b.
        repo_a = _make_git_repo(tmp_path / "a", branch_name="test-branch")
        repo_b = _make_git_repo(tmp_path / "b", branch_name="test-branch")

        oracle_bytes = _run_oracle_append_day(
            _DOE_APPEND_ORACLE,
            repo=repo_a,
            date="2000-01-01",
            machine="test-machine",
            rc_validate="0",
            rc_plugin_suite="0",
        )

        # Native: all fields are predictable for a zero-commit date window.
        # reviewed_lines=[] + has_non_trivial=False → no **Reviewed:** line (zero commits).
        #
        # is_backfill threading: changelog_ops.py:799-805 documents that
        # compute_day_fields computes+returns `is_backfill` but does NOT thread
        # it into append_day for the caller — "the caller must thread the
        # returned is_backfill value through ... itself". The oracle is a
        # correct caller and derives is_backfill = bool(for_date) and
        # for_date != local_today (workday-complete-step9-append-changelog.py:292),
        # threading it at :373/:404. This harness's `date="2000-01-01"` is never
        # today, so a correct caller must set is_backfill=True here too —
        # hardcoding it would pin a constant instead of modeling the contract,
        # so it's derived with the same predicate.
        is_backfill = "2000-01-01" != _today_utc()
        native_result = append_day(
            worktree=repo_b,
            date="2000-01-01",
            machine="test-machine",
            branch="test-branch",
            commit_count=0,
            commit_range="n/a",
            scope="",
            plans_touched="none",
            handoffs_list="none",
            decisions="none",
            blockers="none",
            rc_validate="0",
            rc_plugin_suite="0",
            reviewed_lines=[],
            has_non_trivial=False,
            is_backfill=is_backfill,
        )
        native_file = Path(native_result["out_path"])
        native_bytes = native_file.read_bytes()

        if oracle_bytes != native_bytes:
            oracle_lines = oracle_bytes.decode(errors="replace").splitlines(keepends=True)
            native_lines = native_bytes.decode(errors="replace").splitlines(keepends=True)
            diff = "".join(
                difflib.unified_diff(oracle_lines, native_lines, fromfile="oracle", tofile="native")
            )
            pytest.fail(
                f"BYTE-PARITY FAIL for append_day (fresh file):\n{diff}\n"
                f"oracle={oracle_bytes!r}\nnative={native_bytes!r}"
            )

    def test_byte_parity_idempotent_unchanged(self, tmp_path):
        """Running native a second time with identical inputs → action='unchanged'."""
        repo = _make_git_repo(tmp_path, branch_name="test-branch")

        kwargs = dict(
            worktree=repo,
            date="2000-01-02",
            machine="test-machine",
            branch="test-branch",
            commit_count=0,
            commit_range="n/a",
            scope="",
            plans_touched="none",
            handoffs_list="none",
            decisions="none",
            blockers="none",
            rc_validate="skipped",
            rc_plugin_suite="n/a",
            reviewed_lines=[],
            has_non_trivial=False,
        )
        result1 = append_day(**kwargs)
        assert result1["action"] in ("written",), f"first write expected 'written', got: {result1}"

        result2 = append_day(**kwargs)
        assert result2["action"] == "unchanged", (
            f"second write with identical inputs expected 'unchanged', got: {result2}"
        )


# ===========================================================================
# Tests: backfill_gaps byte-parity
# ===========================================================================


@_requires_backfill_oracle
@pytest.mark.real_home  # oracle subprocess resolves CLAUDE_KLABAUTER_ROOT via the real machine-local
# registry / <settings-home>/machine-local/.claude-klabauter-root pointer — see note below.
class TestBackfillGapsByteParity:
    """Byte-identical parity: changelog.backfill_gaps == backfill-week-changelog-gaps.py.

    Strategy: create a git repo with HEADER.md week_start=today, run oracle, read the
    backfill file it wrote, delete it, run native with same today, compare.
    The hostname is derived from `hostname -s` at test time (same as oracle calls internally).

    ``@pytest.mark.real_home`` (added — bash-to-.py oracle repoint, 2026-07-22): the
    same test-isolation leakage documented on ``TestAppendDayByteParity`` above applies
    here identically — without this marker the suite-root ``_quarantine_real_home``
    autouse fixture points ``HOME``/``USERPROFILE`` at a throwaway per-test tmpdir before
    ``_run_oracle_backfill_gaps``'s ``env = os.environ.copy()`` inherits it into the oracle
    subprocess, whose CLAUDE_KLABAUTER_ROOT resolver then fails to find the real settings-home
    pointer / machine-local registry entry and hard-fails with "repos.claude_klabauter is
    not set" — a false negative from test-isolation leakage, not a genuine host-config
    gap. This class was missing the marker before this repoint (the prior ``.sh`` oracle
    invocation apparently never surfaced it, or was itself always skipped in whatever
    environment last exercised it); it is required now that the oracle actually runs.
    """

    def test_byte_parity_single_day_backfill(self, tmp_path):
        """Oracle and native produce byte-identical backfill for today's date."""
        repo = _make_git_repo(tmp_path, branch_name="main")

        today = _today_utc()
        hostname = _get_short_hostname()

        # Create HEADER.md with week_start = today so the backfill window = [today, today].
        week_dir = repo / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        header_file = week_dir / "HEADER.md"
        header_file.write_text(
            f"**Week starting:** {today}\n\nTest week header for parity harness.\n",
            encoding="utf-8",
        )

        # Run oracle (CWD=$repo — oracle's coordinator_state_root uses CWD git root).
        oracle_file = _run_oracle_backfill_gaps(_DOE_BACKFILL_ORACLE, repo=repo, today=today)
        if oracle_file is None:
            # Oracle wrote nothing (e.g. no commits in today's window). Skip gracefully.
            pytest.skip(
                f"backfill oracle wrote no file for today={today} — "
                "likely no commits in today's UTC window; non-fatal skip"
            )
        oracle_bytes = oracle_file.read_bytes()
        oracle_host = oracle_file.stem.split("-backfill")[0].split(f"{today}-", 1)[-1]
        # Delete oracle file so native doesn't see a sacred daily file and skip.
        oracle_file.unlink()

        # Run native with matching host and today override.
        native_result = backfill_gaps(
            repo_root=repo / ".git",
            host=oracle_host,
            today_override=today,
        )
        if not native_result["backfilled"]:
            pytest.fail(
                f"native backfill_gaps wrote no files (oracle wrote {oracle_file.name}). "
                f"skipped={native_result.get('skipped')}, error={native_result.get('error')}"
            )

        native_file = Path(native_result["backfilled"][0])
        native_bytes = native_file.read_bytes()

        if oracle_bytes != native_bytes:
            oracle_lines = oracle_bytes.decode(errors="replace").splitlines(keepends=True)
            native_lines = native_bytes.decode(errors="replace").splitlines(keepends=True)
            diff = "".join(
                difflib.unified_diff(oracle_lines, native_lines, fromfile="oracle", tofile="native")
            )
            pytest.fail(f"BYTE-PARITY FAIL for backfill_gaps (today={today}):\n{diff}")


# ===========================================================================
# Tests: smoke (no oracle required — validate native op behavior in isolation)
# ===========================================================================


class TestAppendDaySmoke:
    """Smoke tests for changelog.append_day (no oracle required).

    Validates the compose_block format, file creation, and idempotency without
    comparing against the oracle. These run even when example-doctrine-repo is not installed.
    """

    def test_compose_block_format(self):
        """compose_block() produces the expected field structure."""
        block = _compose_block(
            date="2026-01-15",
            machine="myhost",
            branch="work/main/2026-01-15",
            commit_count=3,
            commit_range="abc1234..def5678",
            scope="Shipped the changelog op.",
            plans_touched="docs/plans/2026-01-15-foo.md",
            handoffs_list="none",
            decisions="DR-216",
            blockers="none",
            rc_validate="0",
            rc_plugin_suite="0",
            reviewed_lines=["code: the Staff Engineer found 1 finding (P2, applied)."],
            has_non_trivial=True,
        )

        # Field order and format (byte-parity target)
        assert block.startswith("## 2026-01-15 — myhost\n"), (
            f"block must start with '## DATE — MACHINE', got:\n{block[:100]}"
        )
        assert "**Branch:** work/main/2026-01-15\n" in block
        assert "**Commits:** 3 (range: abc1234..def5678)\n" in block
        assert "**Scope:** Shipped the changelog op.\n" in block
        assert "**Plans touched:** docs/plans/2026-01-15-foo.md\n" in block
        assert "**Handoffs:** none\n" in block
        assert "**Decisions:** DR-216\n" in block
        assert "**Blockers:** none\n" in block
        assert "**Validation:** validate=0 plugin-suite=0\n" in block
        assert "**Reviewed:** code: the Staff Engineer found 1 finding (P2, applied)." in block
        assert '**Links:** archive/daily-summaries/2026-01-15-myhost.md' in block
        assert 'archive/completed/2026-01/' in block
        # Block must NOT end with trailing newline (caller adds it on write)
        assert not block.endswith("\n"), "compose_block returns block without trailing newline"

    def test_compose_block_no_reviewed_no_nontrivial(self):
        """No **Reviewed:** line when reviewed_lines=[] and has_non_trivial=False."""
        block = _compose_block(
            date="2000-01-01",
            machine="test-machine",
            branch="main",
            commit_count=0,
            commit_range="n/a",
            scope="",
            plans_touched="none",
            handoffs_list="none",
            decisions="none",
            blockers="none",
            rc_validate="skipped",
            rc_plugin_suite="n/a",
            reviewed_lines=[],
            has_non_trivial=False,
        )
        assert "**Reviewed:**" not in block, (
            "no **Reviewed:** line expected when reviewed_lines=[] and has_non_trivial=False"
        )

    def test_compose_block_omits_empty_scope(self):
        """Empty scope → **Scope:** line omitted entirely (oracle C4 omit-by-default)."""
        block = _compose_block(
            date="2000-01-01",
            machine="test-machine",
            branch="main",
            commit_count=0,
            commit_range="n/a",
            scope="",
            plans_touched="none",
            handoffs_list="none",
            decisions="none",
            blockers="none",
            rc_validate="skipped",
            rc_plugin_suite="n/a",
            reviewed_lines=[],
            has_non_trivial=False,
        )
        assert "**Scope:**" not in block, (
            "no **Scope:** line expected when scope=='' (oracle C4 omit-by-default)"
        )
        assert "no work today" not in block, (
            "oracle never injects 'no work today' — native must not either"
        )

    def test_compose_block_emits_nonempty_scope(self):
        """Non-empty scope → **Scope:** line present with the given value."""
        block = _compose_block(
            date="2000-01-01",
            machine="test-machine",
            branch="main",
            commit_count=1,
            commit_range="abc1234",
            scope="Real scope summary.",
            plans_touched="none",
            handoffs_list="none",
            decisions="none",
            blockers="none",
            rc_validate="0",
            rc_plugin_suite="0",
            reviewed_lines=[],
            has_non_trivial=False,
        )
        assert "**Scope:** Real scope summary.\n" in block

    def test_compose_block_has_nontrivial_no_reviewed_lines(self):
        """has_non_trivial=True, reviewed_lines=[] → '**Reviewed:** none — flag for /workweek-complete Step 7'."""
        block = _compose_block(
            date="2000-01-01",
            machine="m",
            branch="b",
            commit_count=1,
            commit_range="abc1234",
            scope="did stuff",
            plans_touched="none",
            handoffs_list="none",
            decisions="none",
            blockers="none",
            rc_validate="0",
            rc_plugin_suite="0",
            reviewed_lines=[],
            has_non_trivial=True,
        )
        assert "**Reviewed:** none — flag for /workweek-complete Step 7" in block

    def test_append_day_creates_file(self, tmp_path):
        """append_day creates state/week-changelog/{date}.md.

        Filename assertion updated for the per-day filename collapse (PM ruling
        2026-07-19, AC11/DEC-4/DR-216 amendment): the native write target moved
        from "{date}-{machine}.md" to the bare "{date}.md" — this test runs
        unconditionally (no oracle guard), so it broke on the native-only path
        until updated here. The oracle-comparison tests above also collapsed to
        "{date}.md": the step9 oracle's Zone B is a finished strangler that
        delegates unconditionally to native changelog.append_day, so its real
        on-disk output moved in lockstep (see _run_oracle_append_day's note).
        """
        result = append_day(
            worktree=tmp_path,
            date="2026-01-15",
            machine="smokehost",
            branch="feature/smoke",
            commit_count=2,
            commit_range="aaaa..bbbb",
            scope="Smoke test day.",
            plans_touched="none",
            handoffs_list="none",
            decisions="none",
            blockers="none",
            rc_validate="skipped",
            rc_plugin_suite="n/a",
        )
        assert result["action"] == "written"
        out = Path(result["out_path"])
        assert out.exists()
        assert out.name == "2026-01-15.md"
        content = out.read_text(encoding="utf-8")
        assert "## 2026-01-15 — smokehost" in content
        assert "**Commits:** 2 (range: aaaa..bbbb)" in content
        assert content.endswith("\n"), "file must end with a trailing newline"

    def test_append_day_handler_smoke(self, tmp_path):
        """JSON-RPC handler wires correctly through asyncio.to_thread."""
        from coordinator_core.ops.changelog_ops import _append_day_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        result = asyncio.run(
            _append_day_handler(
                {
                    "machine": "handler-machine",
                    "branch": "main",
                    "commit_count": 0,
                    "commit_range": "n/a",
                    "date": "2026-01-15",
                },
                repo_root=fake_git_dir,
            )
        )
        assert "out_path" in result, f"expected out_path in result, got: {result}"
        assert result.get("action") in ("written", "unchanged", "replaced")

    def test_append_day_handler_omits_scope_and_no_no_work_today(self, tmp_path):
        """Regression guard for Edit C: omitted 'scope' param + commit_count=0 → no
        **Scope:** line and no 'no work today' string in the written file (oracle
        omit-by-default, never the legacy 'no work today' injection)."""
        from coordinator_core.ops.changelog_ops import _append_day_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        result = asyncio.run(
            _append_day_handler(
                {
                    "machine": "handler-machine",
                    "branch": "main",
                    "commit_count": 0,
                    "commit_range": "n/a",
                    "date": "2026-01-16",
                },
                repo_root=fake_git_dir,
            )
        )
        assert "out_path" in result, f"expected out_path in result, got: {result}"
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        assert "**Scope:**" not in content, (
            f"no **Scope:** line expected when scope param omitted, got:\n{content}"
        )
        assert "no work today" not in content, (
            f"'no work today' must never appear (oracle C4 omit-by-default), got:\n{content}"
        )

    def test_append_day_handler_missing_machine_returns_error(self, tmp_path):
        """Handler returns error dict when 'machine' param is missing."""
        from coordinator_core.ops.changelog_ops import _append_day_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        result = asyncio.run(
            _append_day_handler({"commit_count": 0, "commit_range": "n/a"}, repo_root=fake_git_dir)
        )
        assert "error" in result, f"expected error in result for missing machine, got: {result}"

    def test_append_day_handler_rejects_machine_with_slash(self, tmp_path):
        """A 'machine' containing '/' is rejected before it reaches the filename
        (safe_id guard) — see op-family path-containment sweep, 2026-07-08."""
        from coordinator_core.ops.changelog_ops import _append_day_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        result = asyncio.run(
            _append_day_handler(
                {
                    "machine": "../../etc",
                    "branch": "main",
                    "commit_count": 0,
                    "commit_range": "n/a",
                    "date": "2026-01-17",
                },
                repo_root=fake_git_dir,
            )
        )
        assert "error" in result, f"traversal-shaped machine must be rejected, got: {result}"
        # No file must have been written outside state/week-changelog/.
        assert not (tmp_path / "etc").exists()

    def test_append_day_handler_rejects_machine_with_traversal_dots(self, tmp_path):
        """A 'machine' of '..' is rejected by the explicit bare-traversal guard."""
        from coordinator_core.ops.changelog_ops import _append_day_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        result = asyncio.run(
            _append_day_handler(
                {
                    "machine": "..",
                    "branch": "main",
                    "commit_count": 0,
                    "commit_range": "n/a",
                    "date": "2026-01-17",
                },
                repo_root=fake_git_dir,
            )
        )
        assert "error" in result, f"bare '..' machine must be rejected, got: {result}"

    def test_append_day_handler_rejects_malformed_date(self, tmp_path):
        """Review: code-reviewer (F3) — AC13's date-containment guard (F2) must be
        exercised by the test suite: both a traversal-shaped date and a
        calendar-invalid-but-shape-valid date must be rejected."""
        from coordinator_core.ops.changelog_ops import _append_day_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        for bad_date in ("../../etc", "2026-13-45"):
            result = asyncio.run(
                _append_day_handler(
                    {
                        "machine": "handler-machine",
                        "branch": "main",
                        "commit_count": 0,
                        "commit_range": "n/a",
                        "date": bad_date,
                    },
                    repo_root=fake_git_dir,
                )
            )
            assert "error" in result, (
                f"malformed date {bad_date!r} must be rejected, got: {result}"
            )


class TestBackfillGapsSmoke:
    """Smoke tests for changelog.backfill_gaps (no oracle required)."""

    def test_no_header_md_returns_message(self, tmp_path):
        """backfill_gaps returns informative message when HEADER.md is absent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()

        result = backfill_gaps(repo_root=git_dir, host="test-host", today_override="2026-01-15")
        assert "message" in result or "error" in result, (
            f"expected 'message' or 'error' when HEADER.md absent, got: {result}"
        )
        assert result.get("backfilled") == [], f"expected empty backfilled list, got: {result}"

    def test_backfill_handler_smoke_no_header(self, tmp_path):
        """Handler returns gracefully when HEADER.md absent (advisory non-fatal path)."""
        from coordinator_core.ops.changelog_ops import _backfill_gaps_handler

        repo = tmp_path / "repo"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()

        result = asyncio.run(
            _backfill_gaps_handler({"host": "test-host", "today": "2026-01-15"}, repo_root=git_dir)
        )
        assert "backfilled" in result, f"expected 'backfilled' key in result, got: {result}"

    def test_backfill_handler_rejects_host_with_slash(self, tmp_path):
        """A 'host' containing '/' is rejected before it reaches the filename
        (safe_id guard) — see op-family path-containment sweep, 2026-07-08."""
        from coordinator_core.ops.changelog_ops import _backfill_gaps_handler

        repo = tmp_path / "repo"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()

        result = asyncio.run(
            _backfill_gaps_handler(
                {"host": "../../etc", "today": "2026-01-15"}, repo_root=git_dir
            )
        )
        assert "error" in result, f"traversal-shaped host must be rejected, got: {result}"
        assert result.get("backfilled") == []

    def test_backfill_handler_rejects_host_traversal_dots(self, tmp_path):
        """A 'host' of '..' is rejected by the explicit bare-traversal guard."""
        from coordinator_core.ops.changelog_ops import _backfill_gaps_handler

        repo = tmp_path / "repo"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()

        result = asyncio.run(
            _backfill_gaps_handler({"host": "..", "today": "2026-01-15"}, repo_root=git_dir)
        )
        assert "error" in result, f"bare '..' host must be rejected, got: {result}"
        assert result.get("backfilled") == []

    def test_backfill_handler_rejects_malformed_today(self, tmp_path):
        """Review: code-reviewer (F3) — AC13's date-containment guard (F2) must be
        exercised by the test suite: both a traversal-shaped 'today' and a
        calendar-invalid-but-shape-valid 'today' must be rejected."""
        from coordinator_core.ops.changelog_ops import _backfill_gaps_handler

        repo = tmp_path / "repo"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()

        for bad_today in ("../../etc", "9999-99-99"):
            result = asyncio.run(
                _backfill_gaps_handler(
                    {"host": "test-host", "today": bad_today}, repo_root=git_dir
                )
            )
            assert "error" in result, (
                f"malformed today {bad_today!r} must be rejected, got: {result}"
            )
            assert result.get("backfilled") == []


# ===========================================================================
# Tests: inject_anchor byte-parity (injection path only)
# ===========================================================================


@_requires_inject_anchor_oracle
class TestInjectAnchorByteParity:
    """Byte-parity: changelog.inject_anchor (injection path only) vs. the
    in-repo workday-complete-backfill-inject-anchor.py oracle's injection path.

    Unlike append_day/backfill_gaps, this oracle is a claude-klabauter-owned CLI resolved
    directly at coordinator/bin/ in this repo, not a example-doctrine-repo artifact behind the
    ~/.claude/.doe-root sentinel (see the module-level oracle-resolution
    comment above `_requires_inject_anchor_oracle`).
    """

    def test_byte_parity_fresh_injection(self, tmp_path):
        """Oracle and native produce byte-identical files when injecting a
        fresh anchor into a summary with no anchor present."""
        # Fixed commit_env so repo_a's and repo_b's initial commit hash
        # identically (same tree/parent/message + now same dates) — required
        # since inject_anchor's `full_sha` is resolved against its own
        # `worktree` (Finding 2 of the port review): a SHA taken from repo_a
        # must actually resolve in repo_b for the native call below.
        repo_a = _make_git_repo(tmp_path / "a", commit_env=_FIXED_INITIAL_COMMIT_ENV)
        repo_b = _make_git_repo(tmp_path / "b", commit_env=_FIXED_INITIAL_COMMIT_ENV)
        date = "2026-02-01"
        machine = "test-machine"
        body = "# Daily Summary\n\nSame pre-existing narrative in both repos.\n"
        _seed_daily_summary(repo_a, date, machine, body=body)
        _seed_daily_summary(repo_b, date, machine, body=body)

        sha = _git(repo_a, "rev-parse", "HEAD").stdout.strip()
        today = "2026-02-02"

        oracle_result = _run_oracle_inject_anchor(repo_a, date, sha, today=today, machine=machine)
        assert oracle_result.returncode == 0, (
            f"oracle exited {oracle_result.returncode}:\n"
            f"stdout: {oracle_result.stdout}\nstderr: {oracle_result.stderr}"
        )
        oracle_file = repo_a / "archive" / "daily-summaries" / f"{date}-{machine}.md"
        oracle_bytes = oracle_file.read_bytes()

        native_result = inject_anchor(
            worktree=repo_b, date=date, machine=machine, full_sha=sha, today=today
        )
        assert native_result["action"] == "injected", f"expected 'injected', got: {native_result}"
        native_bytes = Path(native_result["out_path"]).read_bytes()

        if oracle_bytes != native_bytes:
            oracle_lines = oracle_bytes.decode(errors="replace").splitlines(keepends=True)
            native_lines = native_bytes.decode(errors="replace").splitlines(keepends=True)
            diff = "".join(
                difflib.unified_diff(oracle_lines, native_lines, fromfile="oracle", tofile="native")
            )
            pytest.fail(
                f"BYTE-PARITY FAIL for inject_anchor (fresh injection):\n{diff}\n"
                f"oracle={oracle_bytes!r}\nnative={native_bytes!r}"
            )

    def test_byte_parity_no_frontmatter_shape(self, tmp_path):
        """Same parity check for a summary with no YAML frontmatter (the
        oracle's second insertion branch — after the H1, no closing ---)."""
        # See test_byte_parity_fresh_injection's comment: fixed commit_env so
        # repo_a's and repo_b's initial commit SHA matches, since full_sha
        # must resolve in whichever repo it's checked against.
        repo_a = _make_git_repo(tmp_path / "a", commit_env=_FIXED_INITIAL_COMMIT_ENV)
        repo_b = _make_git_repo(tmp_path / "b", commit_env=_FIXED_INITIAL_COMMIT_ENV)
        date = "2026-02-10"
        machine = "no-fm-host"
        body = "# Daily Summary\n\nNarrative with no frontmatter block.\n"
        _seed_daily_summary(repo_a, date, machine, body=body)
        _seed_daily_summary(repo_b, date, machine, body=body)

        sha = _git(repo_a, "rev-parse", "HEAD").stdout.strip()
        today = "2026-02-11"

        oracle_result = _run_oracle_inject_anchor(repo_a, date, sha, today=today, machine=machine)
        assert oracle_result.returncode == 0, (
            f"oracle exited {oracle_result.returncode}:\n"
            f"stdout: {oracle_result.stdout}\nstderr: {oracle_result.stderr}"
        )
        oracle_bytes = (repo_a / "archive" / "daily-summaries" / f"{date}-{machine}.md").read_bytes()

        native_result = inject_anchor(
            worktree=repo_b, date=date, machine=machine, full_sha=sha, today=today
        )
        assert native_result["action"] == "injected"
        native_bytes = Path(native_result["out_path"]).read_bytes()

        assert oracle_bytes == native_bytes, (
            f"BYTE-PARITY FAIL (no-frontmatter shape):\noracle={oracle_bytes!r}\nnative={native_bytes!r}"
        )


class TestInjectAnchorSmoke:
    """Smoke + idempotency + content-additive tests for changelog.inject_anchor
    (no oracle required — run unconditionally)."""

    def test_idempotent_second_inject_is_byte_stable_noop(self, tmp_path):
        """A second inject_anchor call for the same <date>-<machine> is a
        byte-stable no-op (D2(i) per-record idempotency)."""
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-02-03", "idempotent-host"
        _seed_daily_summary(repo, date, machine)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        first = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert first["action"] == "injected", f"expected 'injected', got: {first}"
        first_bytes = Path(first["out_path"]).read_bytes()

        second = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert second["action"] == "already_anchored", f"expected 'already_anchored', got: {second}"
        second_bytes = Path(second["out_path"]).read_bytes()

        assert first_bytes == second_bytes, "second inject must be byte-stable (no-op)"

    def test_content_additive_existing_body_preserved(self, tmp_path):
        """Existing narrative content is unchanged; only the anchor block is
        appended (D2(ii)/(iii): content-additive, git-reversible)."""
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-02-04", "additive-host"
        body = (
            "# Daily Summary\n\n"
            "Original narrative line one.\n"
            "Original narrative line two.\n"
        )
        target = _seed_daily_summary(repo, date, machine, body=body)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert result["action"] == "injected"

        new_content = target.read_text(encoding="utf-8")
        assert "Original narrative line one.\n" in new_content
        assert "Original narrative line two.\n" in new_content
        assert f"covered_tip_sha: {sha}\n" in new_content
        assert f"covered_machine: {machine}\n" in new_content

    def test_summary_absent_returns_no_write(self, tmp_path):
        """No archive/daily-summaries/ file for (date, machine) → summary_absent,
        no file written (mirrors the oracle's exit 20 real-content-gap signal).

        `full_sha` must be a real, resolvable commit — resolution happens
        BEFORE the target-file lookup (mirrors the oracle's own ordering,
        which verifies DESCENDANT_TIP_SHA before resolving the target file;
        see Finding 2 of the inject_anchor port review)."""
        repo = _make_git_repo(tmp_path)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        result = inject_anchor(
            worktree=repo, date="2026-02-20", machine="absent-host", full_sha=sha
        )
        assert result["action"] == "summary_absent"
        assert result["out_path"] is None

    def test_stale_ancestor_anchor_bumps_to_new_tip(self, tmp_path):
        """DR-216 § D2(iii-b): a recorded covered_tip_sha: that is a STRICT
        ANCESTOR of the new target tip is bumped in place — 'bumped', not
        'already_anchored' — and the anchor line advances to the new SHA."""
        repo = _make_git_repo(tmp_path)
        old_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "commit", "--allow-empty", "-m", "second: advances the tip past the recorded anchor")
        new_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert old_sha != new_sha

        date, machine = "2026-02-06", "stale-host"
        body = (
            "# Daily Summary\n\n"
            f"covered_tip_sha: {old_sha}\n"
            f"covered_machine: {machine}\n"
            "> _Record anchor injected 2026-02-06 by /workday-complete backfill "
            "(mechanical) — summary content pre-existing._\n"
        )
        target = _seed_daily_summary(repo, date, machine, body=body)

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=new_sha)
        assert result["action"] == "bumped", f"expected 'bumped', got: {result}"

        new_content = target.read_text(encoding="utf-8")
        assert f"covered_tip_sha: {new_sha}\n" in new_content
        assert f"covered_machine: {machine}\n" in new_content
        assert old_sha not in new_content, "stale SHA must not survive the bump"

    def test_handler_smoke(self, tmp_path):
        """JSON-RPC handler wires correctly through asyncio.to_thread.

        Uses a real git repo (not the bare `.git`-dir stand-in the param-
        validation-only tests below use) and a real, resolvable `full_sha` —
        resolution is now mandatory (Finding 2 of the inject_anchor port
        review), so a fake SHA against a fake repo no longer reaches
        'injected'."""
        from coordinator_core.ops.changelog_ops import _inject_anchor_handler

        repo = _make_git_repo(tmp_path)
        fake_git_dir = repo / ".git"
        date, machine = "2026-02-07", "handler-host"
        _seed_daily_summary(repo, date, machine)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        result = asyncio.run(
            _inject_anchor_handler(
                {"date": date, "machine": machine, "full_sha": sha},
                repo_root=fake_git_dir,
            )
        )
        assert result.get("action") == "injected", f"expected 'injected', got: {result}"

    def test_handler_rejects_malformed_date(self, tmp_path):
        """Mirrors test_append_day_handler_rejects_malformed_date: both a
        traversal-shaped date and a calendar-invalid-but-shape-valid date must
        be rejected before any path join."""
        from coordinator_core.ops.changelog_ops import _inject_anchor_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        for bad_date in ("../../../etc/x", "2026-13-45"):
            result = asyncio.run(
                _inject_anchor_handler(
                    {"date": bad_date, "machine": "handler-machine", "full_sha": "abc123"},
                    repo_root=fake_git_dir,
                )
            )
            assert "error" in result, f"malformed date {bad_date!r} must be rejected, got: {result}"
        assert not (tmp_path / "etc").exists()

    def test_handler_rejects_machine_with_traversal(self, tmp_path):
        """A 'machine' containing '/' or '..' is rejected before it reaches the
        filename (safe_id guard) — mirrors the sibling ops' equivalent test."""
        from coordinator_core.ops.changelog_ops import _inject_anchor_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        for bad_machine in ("../../etc", ".."):
            result = asyncio.run(
                _inject_anchor_handler(
                    {"date": "2026-02-08", "machine": bad_machine, "full_sha": "abc123"},
                    repo_root=fake_git_dir,
                )
            )
            assert "error" in result, f"traversal-shaped machine {bad_machine!r} must be rejected, got: {result}"
        assert not (tmp_path / "etc").exists()

    def test_handler_missing_full_sha_returns_error(self, tmp_path):
        """Handler returns error dict when 'full_sha' param is missing."""
        from coordinator_core.ops.changelog_ops import _inject_anchor_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        result = asyncio.run(
            _inject_anchor_handler(
                {"date": "2026-02-09", "machine": "m"}, repo_root=fake_git_dir
            )
        )
        assert "error" in result, f"expected error for missing full_sha, got: {result}"


class TestAnchorPresentDetection:
    """Unit tests for _anchor_present's fenced-code-block false-positive guard."""

    def test_does_not_false_positive_inside_code_fence(self):
        content = (
            "# Daily Summary\n\n"
            "Example of the anchor format this backfill tool injects:\n"
            "```\n"
            "covered_tip_sha: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
            "covered_machine: examplehost\n"
            "```\n\n"
            "No real anchor exists in this file outside the fence above.\n"
        )
        assert _anchor_present(content, "2026-02-05", "m3") is False

    def test_detects_live_frontmatter_anchor(self):
        content = (
            "---\n"
            "covered_tip_sha: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
            "covered_machine: host\n"
            "---\n"
            "# Daily Summary\n"
        )
        assert _anchor_present(content, "2026-02-05", "m3") is True

    def test_detects_live_anchor_after_h1_no_frontmatter(self):
        content = (
            "# Daily Summary\n\n"
            "covered_tip_sha: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
            "covered_machine: host\n"
            "> _Record anchor injected 2026-02-05 by /workday-complete backfill "
            "(mechanical) — summary content pre-existing._\n"
        )
        assert _anchor_present(content, "2026-02-05", "m3") is True


# ===========================================================================
# Tests: inject_anchor bump path (DR-216 § D2(iii-b), PM-ratified 2026-07-28)
# ===========================================================================


class TestInjectAnchorBumpPath:
    """Pins the D2(iii-b) convergence bound: bump ONLY when the recorded
    anchor is a strict ancestor of the target tip, or unresolvable. Never
    bump backwards (recorded is equal/descendant) or across a fork
    (divergent)."""

    @staticmethod
    def _seed_with_anchor(repo: Path, date: str, machine: str, recorded_sha: str) -> Path:
        body = (
            "# Daily Summary\n\n"
            "Original narrative, untouched by a bump.\n\n"
            f"covered_tip_sha: {recorded_sha}\n"
            f"covered_machine: {machine}\n"
            "> _Record anchor injected 2026-01-01 by /workday-complete backfill "
            "(mechanical) — summary content pre-existing._\n"
        )
        return _seed_daily_summary(repo, date, machine, body=body)

    def test_equal_anchor_is_already_anchored_byte_identical(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        date, machine = "2026-04-01", "equal-host"
        target = self._seed_with_anchor(repo, date, machine, sha)
        before = target.read_bytes()

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert result["action"] == "already_anchored", f"expected 'already_anchored', got: {result}"
        assert target.read_bytes() == before

    def test_abbreviated_equal_anchor_is_already_anchored_not_bumped(self, tmp_path):
        """An ABBREVIATED recorded anchor naming the same commit as full_sha is
        equal, and DR-216 § D2(iii-b) forbids rewriting an equal anchor.

        Regression guard: raw string equality fails here (7 chars vs 40), and
        `git merge-base --is-ancestor X X` succeeds for an equal commit — so an
        implementation that compares only the raw string before the ancestor
        test falls into the bump branch and rewrites an anchor already pointing
        at the target tip. Equality must be re-checked after resolution, which
        is what the oracle does (`rec_full == full_sha`, post-rev-parse).
        """
        repo = _make_git_repo(tmp_path)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        short = sha[:7]
        date, machine = "2026-04-01", "abbrev-host"
        target = self._seed_with_anchor(repo, date, machine, short)
        before = target.read_bytes()

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert result["action"] == "already_anchored", (
            f"abbreviated anchor naming the same commit must not bump, got: {result}"
        )
        assert target.read_bytes() == before

    def test_descendant_anchor_is_already_anchored_not_bumped_backwards(self, tmp_path):
        """Recorded anchor is NEWER than the target tip — the target tip is
        an ancestor of the recorded anchor, not the other way round. Must
        stay already_anchored; the anchor must never move backwards."""
        repo = _make_git_repo(tmp_path)
        older_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "commit", "--allow-empty", "-m", "second: the recorded (newer) anchor commit")
        newer_recorded_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        date, machine = "2026-04-02", "descendant-host"
        target = self._seed_with_anchor(repo, date, machine, newer_recorded_sha)
        before = target.read_bytes()

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=older_sha)
        assert result["action"] == "already_anchored", f"expected 'already_anchored', got: {result}"
        assert target.read_bytes() == before, "a descendant anchor must never be bumped backwards"

    def test_divergent_anchor_is_already_anchored_not_bumped_across_fork(self, tmp_path):
        """Recorded anchor and target tip are on two branches from a common
        base, neither an ancestor of the other. Must stay already_anchored."""
        repo = _make_git_repo(tmp_path)
        base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        _git(repo, "checkout", "-b", "branch-a")
        _git(repo, "commit", "--allow-empty", "-m", "branch-a: the recorded anchor commit")
        recorded_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        _git(repo, "checkout", base_sha)
        _git(repo, "checkout", "-b", "branch-b")
        _git(repo, "commit", "--allow-empty", "-m", "branch-b: the target tip commit")
        target_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        date, machine = "2026-04-03", "divergent-host"
        target = self._seed_with_anchor(repo, date, machine, recorded_sha)
        before = target.read_bytes()

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=target_sha)
        assert result["action"] == "already_anchored", f"expected 'already_anchored', got: {result}"
        assert target.read_bytes() == before, "a divergent anchor must never be bumped across a fork"

    def test_unresolvable_recorded_anchor_bumps(self, tmp_path):
        """A recorded covered_tip_sha: that no longer resolves in this repo
        (a dangling SHA) is treated as convergent — bump."""
        repo = _make_git_repo(tmp_path)
        dangling_sha = "deadbeef" * 5  # 40 hex chars, well-formed, never committed
        full_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        date, machine = "2026-04-04", "unresolvable-host"
        target = self._seed_with_anchor(repo, date, machine, dangling_sha)

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=full_sha)
        assert result["action"] == "bumped", f"expected 'bumped', got: {result}"
        new_content = target.read_text(encoding="utf-8")
        assert f"covered_tip_sha: {full_sha}\n" in new_content
        assert dangling_sha not in new_content

    def test_bump_is_convergent_second_call_is_fixed_point(self, tmp_path):
        """Bump once, then re-invoke with the same tip: the second call must
        be 'already_anchored' and byte-stable (D2(i)/D2(iii-b) convergence)."""
        repo = _make_git_repo(tmp_path)
        old_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "commit", "--allow-empty", "-m", "second: advances past the recorded anchor")
        new_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        date, machine = "2026-04-05", "convergent-host"
        target = self._seed_with_anchor(repo, date, machine, old_sha)

        first = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=new_sha)
        assert first["action"] == "bumped", f"expected 'bumped', got: {first}"
        after_first = target.read_bytes()

        second = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=new_sha)
        assert second["action"] == "already_anchored", f"expected 'already_anchored', got: {second}"
        assert target.read_bytes() == after_first, "second call must be a byte-stable fixed point"

    def test_bump_rewrites_only_the_two_anchor_lines(self, tmp_path):
        """The prose note and the summary body must be byte-identical after a
        bump; only covered_tip_sha:/covered_machine: change."""
        repo = _make_git_repo(tmp_path)
        old_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "commit", "--allow-empty", "-m", "second: advances past the recorded anchor")
        new_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        date, machine = "2026-04-06", "surgical-host"
        note = (
            "> _Record anchor injected 2026-01-01 by /workday-complete backfill "
            "(mechanical) — summary content pre-existing._\n"
        )
        body = (
            "# Daily Summary\n\n"
            "Original narrative, untouched by a bump.\n\n"
            f"covered_tip_sha: {old_sha}\n"
            f"covered_machine: {machine}\n"
            f"{note}"
        )
        target = _seed_daily_summary(repo, date, machine, body=body)

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=new_sha)
        assert result["action"] == "bumped", f"expected 'bumped', got: {result}"

        new_content = target.read_text(encoding="utf-8")
        expected = body.replace(f"covered_tip_sha: {old_sha}\n", f"covered_tip_sha: {new_sha}\n")
        assert new_content == expected, (
            "bump must rewrite ONLY the two anchor lines — prose note and body must be "
            "byte-identical otherwise"
        )
        assert "Original narrative, untouched by a bump.\n" in new_content
        assert note in new_content

    def test_abbreviated_full_sha_naming_anchored_commit_is_already_anchored(self, tmp_path):
        """An ABBREVIATED `full_sha` naming the same commit as a full-form
        recorded anchor must be `already_anchored`, byte-identical — never
        bumped.

        Regression guard for Finding 2 of the inject_anchor port review:
        `full_sha` was previously never resolved/canonicalized. The raw
        `resolved_recorded == full_sha` string compare fails for an
        abbreviated `full_sha` (different lengths) even though the commits
        are identical, and `git merge-base --is-ancestor <full> <abbrev>`
        succeeds (ancestor-of-self) — so an unfixed implementation falls
        into the bump branch and rewrites an anchor that already points at
        the target tip, with the abbreviated form. `full_sha` must be
        resolved once, up front, and used for every comparison and write.
        """
        repo = _make_git_repo(tmp_path)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        date, machine = "2026-04-07", "abbrev-full-sha-host"
        target = self._seed_with_anchor(repo, date, machine, sha)
        before = target.read_bytes()

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha[:7])
        assert result["action"] == "already_anchored", (
            f"abbreviated full_sha naming the already-anchored commit must not bump, got: {result}"
        )
        assert target.read_bytes() == before, (
            "the abbreviated form must never be written to covered_tip_sha:"
        )

    def test_bump_path_skips_fenced_anchor_example_preceding_live_anchor(self, tmp_path):
        """A fenced `covered_tip_sha:`/`covered_machine:` documentation
        example appearing BEFORE the live anchor must be left byte-identical
        by a bump — only the live anchor line changes.

        Regression guard for Finding 3 of the inject_anchor port review:
        `_render_anchor_bump` previously used a plain first-match loop with
        no fenced-code-block awareness (unlike `_recorded_anchor_sha`, which
        skips fenced content so a documentation example is never mistaken
        for a live anchor). A fenced example appearing earlier in the file
        than the real anchor would be rewritten instead of the real, stale
        anchor — corrupting documentation content while leaving the actual
        anchor untouched.
        """
        repo = _make_git_repo(tmp_path)
        old_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "commit", "--allow-empty", "-m", "second: advances past the recorded anchor")
        new_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        date, machine = "2026-04-08", "fenced-example-host"
        fenced_example = (
            "```\n"
            "covered_tip_sha: 0000000000000000000000000000000000000000\n"
            "covered_machine: example-machine\n"
            "```\n"
        )
        body = (
            "# Daily Summary\n\n"
            "This summary documents the backfill-anchor mechanism itself:\n\n"
            f"{fenced_example}\n"
            "Original narrative, untouched by a bump.\n\n"
            f"covered_tip_sha: {old_sha}\n"
            f"covered_machine: {machine}\n"
        )
        target = _seed_daily_summary(repo, date, machine, body=body)

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=new_sha)
        assert result["action"] == "bumped", f"expected 'bumped', got: {result}"

        new_content = target.read_text(encoding="utf-8")
        assert fenced_example in new_content, (
            "the fenced documentation example must be byte-identical — it is not the live anchor"
        )
        assert f"covered_tip_sha: {new_sha}\n" in new_content
        assert f"covered_tip_sha: {old_sha}\n" not in new_content

    def test_bump_path_skips_tilde_fenced_anchor_example(self, tmp_path):
        """Same as the backtick case, for `~~~` fences.

        Markdown accepts both ``` and ~~~ as fence delimiters. The original
        fence guard recognised only backticks, so a ~~~-fenced example was
        treated as live by BOTH the detector and the rewriter — they agreed
        with each other, so the detection/rewrite mismatch did not occur, but
        the documented promise ("a documentation example is never mistaken for
        a live anchor") was only half true. Both loops now toggle on either
        delimiter; this pins the ~~~ half.
        """
        repo = _make_git_repo(tmp_path)
        old_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "commit", "--allow-empty", "-m", "second: advances past the recorded anchor")
        new_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        date, machine = "2026-04-09", "tilde-fence-host"
        fenced_example = (
            "~~~\n"
            "covered_tip_sha: 0000000000000000000000000000000000000000\n"
            "covered_machine: example-machine\n"
            "~~~\n"
        )
        body = (
            "# Daily Summary\n\n"
            f"{fenced_example}\n"
            "Narrative.\n\n"
            f"covered_tip_sha: {old_sha}\n"
            f"covered_machine: {machine}\n"
        )
        target = _seed_daily_summary(repo, date, machine, body=body)

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=new_sha)
        assert result["action"] == "bumped", f"expected 'bumped', got: {result}"

        new_content = target.read_text(encoding="utf-8")
        assert fenced_example in new_content, (
            "the ~~~-fenced example must be byte-identical — it is not the live anchor"
        )
        assert f"covered_tip_sha: {new_sha}\n" in new_content


# ===========================================================================
# Tests: inject_anchor content-gap guards (ported from the oracle, run BEFORE
# injection; guard 1 is a native records_query-based replacement, guards 2/3
# are direct byte-parity ports)
# ===========================================================================


class TestInjectAnchorContentGapGuards:
    """One fires / one healthy-does-not-fire case per guard, plus a byte-
    identical-on-fire assertion (shared by the first case of each guard)."""

    def test_completion_vs_bullets_guard_fires(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-01", "gap-host"
        body = "# Daily Summary\n\nNo Work Completed section at all.\n"
        target = _seed_daily_summary(repo, date, machine, body=body)
        before = target.read_bytes()

        completed_dir = repo / "archive" / "completed" / "2026-03"
        completed_dir.mkdir(parents=True)
        for i in range(3):
            (completed_dir / f"item-{i}.md").write_text(
                f'---\ntitle: "item {i}"\ncreated: {date}\n---\n\nBody.\n', encoding="utf-8"
            )

        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert result["action"] == "content_gap", f"expected 'content_gap', got: {result}"
        assert "completion entries" in result.get("error", "")
        assert target.read_bytes() == before, "content-gap guard must leave the file byte-identical"

    def test_completion_vs_bullets_guard_does_not_fire_on_healthy_input(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-02", "healthy-host"
        body = (
            "# Daily Summary\n\n"
            "## Work Completed\n"
            "- did thing one\n"
            "- did thing two\n"
            "- did thing three\n"
        )
        _seed_daily_summary(repo, date, machine, body=body)

        completed_dir = repo / "archive" / "completed" / "2026-03"
        completed_dir.mkdir(parents=True)
        for i in range(3):
            (completed_dir / f"item-{i}.md").write_text(
                f'---\ntitle: "item {i}"\ncreated: {date}\n---\n\nBody.\n', encoding="utf-8"
            )

        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert result["action"] == "injected", f"expected 'injected', got: {result}"

    def test_commit_density_guard_fires(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-03", "density-host"
        sha1 = _commit_on_date(repo, date, "work one")
        _commit_on_date(repo, date, "work two")
        sha3 = _commit_on_date(repo, date, "work three")
        body = f"# Daily Summary\n\nOnly discusses {sha1[:10]} today.\n"
        target = _seed_daily_summary(repo, date, machine, body=body)
        before = target.read_bytes()

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha3)
        assert result["action"] == "content_gap", f"expected 'content_gap', got: {result}"
        assert "in-range commit SHAs" in result.get("error", "")
        assert target.read_bytes() == before

    def test_commit_density_guard_does_not_fire_on_healthy_input(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-04", "density-healthy-host"
        sha1 = _commit_on_date(repo, date, "work one")
        sha2 = _commit_on_date(repo, date, "work two")
        sha3 = _commit_on_date(repo, date, "work three")
        body = f"# Daily Summary\n\nCovered {sha1[:10]}, {sha2[:10]}, and {sha3[:10]} today.\n"
        _seed_daily_summary(repo, date, machine, body=body)

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha3)
        assert result["action"] == "injected", f"expected 'injected', got: {result}"

    def test_morning_signal_guard_fires(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-05", "morning-host"
        body = "# Daily Summary\n\nQuick morning run wraps up a busy stretch.\n"
        target = _seed_daily_summary(repo, date, machine, body=body)
        before = target.read_bytes()

        last_sha = None
        for i in range(10):
            last_sha = _commit_on_date(repo, date, f"commit {i}")

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=last_sha)
        assert result["action"] == "content_gap", f"expected 'content_gap', got: {result}"
        assert "morning-run" in result.get("error", "")
        assert target.read_bytes() == before

    def test_morning_signal_guard_does_not_fire_with_few_commits(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-06", "morning-few-host"
        body = "# Daily Summary\n\nQuick morning run wraps up a light day.\n"
        _seed_daily_summary(repo, date, machine, body=body)
        sha = _commit_on_date(repo, date, "single commit")

        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert result["action"] == "injected", (
            f"expected 'injected' (fewer than 3 in-range commits, guard 2/3 inapplicable), got: {result}"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="chmod 0o000 permission denial is not reliable on Windows or as root",
    )
    def test_unreadable_completed_dir_returns_error_not_silent_inject(self, tmp_path):
        """A directory-scan failure on archive/completed/ must surface as
        action: 'error', never as a silent inject.

        Regression guard for Finding 1 of the inject_anchor port review: the
        completion-count guard's "fails CLOSED" claim was false for exactly
        this case — records_query's own collection path swallows a scan
        failure and returns `[]`, which reads identically to "zero
        completions today", so the guard never fired and the summary got
        anchored despite the count being genuinely unresolvable.
        """
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-07", "unreadable-completed-host"
        body = "# Daily Summary\n\nNo Work Completed section at all.\n"
        target = _seed_daily_summary(repo, date, machine, body=body)
        before = target.read_bytes()

        completed_dir = repo / "archive" / "completed"
        completed_dir.mkdir(parents=True)
        original_mode = completed_dir.stat().st_mode
        os.chmod(completed_dir, 0o000)
        try:
            sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
            result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        finally:
            os.chmod(completed_dir, original_mode)

        assert result["action"] == "error", f"expected 'error', got: {result}"
        assert "completion-count guard unresolved" in result.get("error", "")
        assert target.read_bytes() == before, "an unresolved guard must leave the file untouched"

    def test_non_directory_at_completed_path_is_error_not_legitimate_zero(self, tmp_path):
        """A plain FILE where archive/completed/ belongs is an anomalous
        filesystem state, not a legitimate zero.

        `Path.is_dir()` returns False for both "absent" and "exists but is a
        file", so a single is_dir() check cannot tell them apart — that is the
        blind spot `_walk_glob_segments` has, and closing it is why this probe
        exists. Absent stays a legitimate zero; wrong-type must surface.
        """
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-08", "notadir-host"
        body = "# Daily Summary\n\nNo Work Completed section at all.\n"
        target = _seed_daily_summary(repo, date, machine, body=body)
        before = target.read_bytes()

        completed_path = repo / "archive" / "completed"
        completed_path.parent.mkdir(parents=True, exist_ok=True)
        completed_path.write_text("not a directory\n", encoding="utf-8")

        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)

        assert result["action"] == "error", f"expected 'error', got: {result}"
        assert "non-directory" in result.get("error", "")
        assert target.read_bytes() == before, "an unresolved guard must leave the file untouched"

    def test_absent_completed_dir_is_a_legitimate_zero_not_an_error(self, tmp_path):
        """The counterpart to the two above: archive/completed/ genuinely
        absent is a legitimate zero and must NOT block injection. Without this,
        the fail-closed hardening would break every fresh worktree."""
        repo = _make_git_repo(tmp_path)
        date, machine = "2026-03-09", "no-completed-host"
        body = "# Daily Summary\n\n## Work Completed\n\n- a\n- b\n- c\n"
        _seed_daily_summary(repo, date, machine, body=body)
        assert not (repo / "archive" / "completed").exists()

        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        result = inject_anchor(worktree=repo, date=date, machine=machine, full_sha=sha)
        assert result["action"] == "injected", f"expected 'injected', got: {result}"


