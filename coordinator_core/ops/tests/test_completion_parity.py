"""
coordinator_core.ops.tests.test_completion_parity — byte-parity harness for
completion.reconcile_commits and plan.append_session.

Purpose: Assert the Python ops produce byte-identical file-write output to their legacy
bash oracles for the same inputs. This is the strangler invariant (strang-10 C2): if the
byte output drifts, the example-doctrine-repo facade routing will silently produce different on-disk content.

Coverage:
  (a) completion.reconcile_commits — byte-parity: empty commits: [] expanded with new SHAs
  (b) completion.reconcile_commits — byte-parity: new SHAs appended after existing entries
  (c) completion.reconcile_commits — idempotency: re-folding same SHAs → file unchanged
  (d) plan.append_session — byte-parity: session entry appended to existing agent_sessions:
  (e) plan.append_session — byte-parity: agent_sessions: block created when key absent
  (f) plan.append_session — idempotency: same session_id → no-op

Oracle skip semantics: when the oracle CLI (or its dependency, bash) is absent, tests are
skipped with a clear diagnostic — NOT silently passed. Oracle skips are expected in CI
environments that don't have the example-doctrine-repo sibling repo.

Spec backlink: docs/plans/2026-07-06-strang-10-residual-writer-strangle-command-type.md § C2
Oracles:
  [example-doctrine-repo] coordinator/bin/reconcile-completion-commits.py (--append mode)
  [example-doctrine-repo] coordinator/bin/append-plan-session.py
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
"""

from __future__ import annotations
import sys

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import pytest
import yaml

# ---------------------------------------------------------------------------
# Direct function imports — no registration required for parity tests.
# The ops are NOT yet in ops/__init__.py (that lands in C3).
# ---------------------------------------------------------------------------

from coordinator_core.ops.completion_ops import (
    _apply_commits_fold,
    _apply_session_append,
    _parse_existing_commits,
    append_plan_session,
    reconcile_completion_commits,
)

# reconcile_completion_commits' byte-parity assertions are pinned against real
# commit SHAs produced by a real repo, matching the shape the legacy bash
# oracle itself reads — a mocked git would validate invented SHAs, not the
# oracle's real input contract.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Oracle / example-doctrine-repo-root path resolution
# ---------------------------------------------------------------------------

_DOE_ROOT_SENTINEL = Path.home() / ".claude" / ".doe-root"
_ORACLE_RECONCILE: Optional[Path] = None
_ORACLE_APPEND_SESSION: Optional[Path] = None
_QUERY_RECORDS_JS: Optional[Path] = None

if _DOE_ROOT_SENTINEL.exists():
    try:
        _doe_root = _DOE_ROOT_SENTINEL.read_text(encoding="utf-8").strip()
        _ORACLE_RECONCILE = (
            Path(_doe_root) / "coordinator" / "bin" / "reconcile-completion-commits.py"
        )
        _ORACLE_APPEND_SESSION = (
            Path(_doe_root) / "coordinator" / "bin" / "append-plan-session.py"
        )
        _QUERY_RECORDS_JS = Path(_doe_root) / "coordinator" / "bin" / "query-records.js"
    except OSError:
        print(f"skip: <module>: _doe_root = _DOE_ROOT_SENTINEL.read_text(encoding=\"utf-8\").strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass

# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------

_ORACLE_RECONCILE_AVAILABLE = (
    _ORACLE_RECONCILE is not None and _ORACLE_RECONCILE.is_file()
)
_ORACLE_APPEND_SESSION_AVAILABLE = (
    _ORACLE_APPEND_SESSION is not None and _ORACLE_APPEND_SESSION.is_file()
)

_requires_oracle_reconcile = pytest.mark.skipif(
    not _ORACLE_RECONCILE_AVAILABLE,
    reason=(
        "reconcile-completion-commits.py oracle absent "
        f"(oracle={_ORACLE_RECONCILE_AVAILABLE})"
    ),
)
_requires_oracle_append_session = pytest.mark.skipif(
    not _ORACLE_APPEND_SESSION_AVAILABLE,
    reason=(
        "append-plan-session.py oracle absent "
        f"(oracle={_ORACLE_APPEND_SESSION_AVAILABLE})"
    ),
)

_QUERY_RECORDS_JS_AVAILABLE = (
    _QUERY_RECORDS_JS is not None and _QUERY_RECORDS_JS.is_file()
)
_NODE_PATH = shutil.which("node")

_requires_query_records_js = pytest.mark.skipif(
    not _QUERY_RECORDS_JS_AVAILABLE or not _NODE_PATH,
    reason=(
        "query-records.js absent (example-doctrine-repo not at .doe-root) or node not on PATH "
        f"(query_records_js={_QUERY_RECORDS_JS_AVAILABLE}, node={bool(_NODE_PATH)})"
    ),
)

# ---------------------------------------------------------------------------
# Fixed test constants
# ---------------------------------------------------------------------------

_TEST_SESSION = "test-completion-parity-session-0001"
_TEST_SESSION_2 = "test-completion-parity-session-0002"

# Fixture: plan completion entry in the EXACT production on-disk shape.
# Matches archive/completed/**/*.md format: YAML frontmatter with commits: key.
_COMPLETION_ENTRY_EMPTY_COMMITS = """\
---
title: "Parity test completion entry (empty commits)"
created: 2026-01-15
nature: infra
chain: "parity-test-chain"
commits: []
status: pending-release
authored_by: "00000000-0000-0000-0000-000000000001"
---

Completion body for byte-parity testing.
"""

_COMPLETION_ENTRY_EXISTING_COMMITS = """\
---
title: "Parity test completion entry (existing commits)"
created: 2026-01-15
nature: infra
chain: "parity-test-chain"
commits:
  - "aaa1111"
  - "bbb2222"
status: pending-release
authored_by: "00000000-0000-0000-0000-000000000002"
---

Completion body with existing commits.
"""

# Fixture: plan file with agent_sessions: key (production shape).
_PLAN_WITH_AGENT_SESSIONS_KEY = """\
---
title: "Parity test plan (with agent_sessions key)"
created: 2026-01-15
status: in-progress
agent_sessions:
---

# Plan Body
"""

_PLAN_WITH_EXISTING_SESSION = """\
---
title: "Parity test plan (with existing session)"
created: 2026-01-15
status: in-progress
agent_sessions:
  - "existing-session-abc|working|2026-01-01T00:00:00Z"
---

# Plan Body
"""

_PLAN_WITHOUT_AGENT_SESSIONS_KEY = """\
---
title: "Parity test plan (no agent_sessions key)"
created: 2026-01-15
status: in-progress
---

# Plan Body
"""


# ---------------------------------------------------------------------------
# Git repo fixture for reconcile_commits oracle tests
# ---------------------------------------------------------------------------


def _make_reconcile_git_repo(tmp_path: Path, session_id: str) -> tuple[Path, list[str]]:
    """Create a temp git repo with commits bearing ``Session-Id: <session_id>`` trailers.

    Returns ``(git_root, [sha1, sha2, ...])`` with short SHAs newest-first
    (matching oracle's ``git log`` default order, Axis 1).

    Sets up ``refs/remotes/origin/main`` pointing to the initial commit so that
    ``git merge-base origin/main HEAD`` resolves (oracle Step 4 guard).
    """
    git_root = tmp_path / "reconcile_repo"
    git_root.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(git_root), *args],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, (
            f"git {' '.join(args)} failed: {result.stderr}"
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.email", "parity@test.local")
    git("config", "user.name", "ParityTest")
    git("commit", "--allow-empty", "-m", "Initial commit")
    # Pin origin/main to the base commit so merge-base resolves.
    git("update-ref", "refs/remotes/origin/main", "HEAD")

    # Make two commits with Session-Id trailers for the test session.
    git(
        "commit",
        "--allow-empty",
        "-m",
        f"Work commit 1\n\nSession-Id: {session_id}",
    )
    git(
        "commit",
        "--allow-empty",
        "-m",
        f"Work commit 2\n\nSession-Id: {session_id}",
    )

    # Collect the short SHAs (newest-first, matching oracle's git log order).
    raw = git(
        "log",
        "--pretty=%h",
        f"--grep=Session-Id: {session_id}",
        "refs/remotes/origin/main..HEAD",
    )
    session_shas = [s.strip() for s in raw.splitlines() if s.strip()]

    return git_root, session_shas


# ---------------------------------------------------------------------------
# Oracle runner helpers
# ---------------------------------------------------------------------------


def _run_oracle_reconcile(
    plan_content: str,
    session_id: str,
    git_root: Path,
) -> bytes:
    """Write ``plan_content`` to a file in ``git_root``, run the oracle in --append mode,
    and return the resulting file bytes.

    The oracle resolves session commits from the git repo (merge-base..HEAD) so it must
    run from inside the repo (cwd=git_root). The plan file also lives in the repo so the
    oracle's compare-before-write guard reads the same file.

    The fixture file lives under ``git_root/archive/completed/`` (not directly under
    ``git_root``): this is the real production noun for completion entries (empirically
    confirmed — 61 ``status: pending-release`` entries live under ``archive/completed/`` in
    this repo, 0 under ``docs/plans/``; the chain-slug resolution that used to live in the
    retired ``reconcile-completion-commits.sh`` now lives server-side in
    ``resolve_chain_commits`` / ``reconcile_completion_commits``, claude-klabauter's
    ``completion_ops.py`` — see that module's own "REOPENED session_id call path"
    negative-spec). The oracle (``reconcile-completion-commits.py``, the F1-d-reconcile
    facade-collapse trampoline — see that file's module docstring) routes ``--append``
    writes through ``cc_invoke.route_mutation`` → ``completion.reconcile_commits``, which
    resolves ``repo_root`` from ``git -C "$PWD" rev-parse --show-toplevel`` (== ``git_root``,
    since the oracle runs with ``cwd=git_root``) and enforces containment against BOTH
    ``archive/completed/`` and ``docs/plans/`` (op-family path-containment sweep, 2026-07-08;
    widened per Finding 1 of the 2026-07-08 completion-ops path-containment review —
    ``docs/plans/``-only rejected this real production shape). A fixture path outside both
    allowed roots would be rejected by the guard.
    (Review: code-reviewer — Finding 2, fixture relocated from the artificial docs/plans/
    shape to the real archive/completed/ production shape now that Finding 1's dual-root
    guard supports it.)
    """
    assert _ORACLE_RECONCILE is not None
    completed_dir = git_root / "archive" / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    plan_file = completed_dir / "test_entry.md"
    plan_file.write_text(plan_content, encoding="utf-8")

    env = os.environ.copy()
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    # Strip CLAUDE_KLABAUTER_ROOT so oracle doesn't write to a live tree.
    env.pop("CLAUDE_KLABAUTER_ROOT", None)

    result = subprocess.run(
        [
            sys.executable,
            str(_ORACLE_RECONCILE),
            "--append",
            "--session-id",
            session_id,
            str(plan_file),
        ],
        env=env,
        cwd=str(git_root),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"oracle reconcile-completion-commits.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return plan_file.read_bytes()


def _run_native_reconcile(
    plan_content: str,
    commits: list[str],
    tmp_path: Path,
) -> bytes:
    """Write ``plan_content`` to a temp file and run native ``reconcile_completion_commits``."""
    plan_file = tmp_path / "native_entry.md"
    plan_file.write_text(plan_content, encoding="utf-8")
    reconcile_completion_commits(str(plan_file), commits=commits)
    return plan_file.read_bytes()


def _run_oracle_append_session(
    plan_content: str,
    session_id: str,
    git_root: Path,
) -> tuple[bytes, str]:
    """Write ``plan_content`` to a file in ``git_root``, run the oracle, and return
    ``(resulting_bytes, created_at_extracted)``.

    The oracle generates ``created_at`` from ``_cs_now_iso()``. We extract it from the
    oracle-modified file so the native op can be called with the same timestamp.

    The fixture file lives under ``git_root/docs/plans/`` (not directly under
    ``git_root``): ``plan.append_session``'s handler (``_append_session_handler``)
    enforces docs/plans/ path containment (op-family path-containment sweep,
    2026-07-08) — a plan_path outside that root is rejected with
    ``"plan_path escapes docs/plans/"`` before this oracle ever gets to write. This
    containment guard predates the append-plan-session.py repoint and was never
    exercised while this test class silently skipped on the absent .sh oracle.
    """
    assert _ORACLE_APPEND_SESSION is not None
    plans_dir = git_root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plans_dir / "test_plan.md"
    plan_file.write_text(plan_content, encoding="utf-8")

    env = os.environ.copy()
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    env.pop("CLAUDE_KLABAUTER_ROOT", None)

    result = subprocess.run(
        [sys.executable, str(_ORACLE_APPEND_SESSION), str(plan_file)],
        env=env,
        cwd=str(git_root),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"oracle append-plan-session.py failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    oracle_bytes = plan_file.read_bytes()
    oracle_text = oracle_bytes.decode("utf-8")

    # Extract created_at from the new entry: `  - "session_id|status|created_at"`
    pattern = rf'^\s+-\s+"?{re.escape(session_id)}\|[^|]+\|([^"]+)"?\s*$'
    created_at = ""
    for line in oracle_text.splitlines():
        m = re.match(pattern, line)
        if m:
            created_at = m.group(1).strip()
            break

    assert created_at, (
        f"could not extract created_at from oracle-modified plan:\n{oracle_text}"
    )
    return oracle_bytes, created_at


def _run_native_append_session(
    plan_content: str,
    session_id: str,
    created_at: str,
    tmp_path: Path,
    status: str = "working",
) -> bytes:
    """Write ``plan_content`` to a temp file and run native ``append_plan_session``."""
    plan_file = tmp_path / "native_plan.md"
    plan_file.write_text(plan_content, encoding="utf-8")
    append_plan_session(str(plan_file), session_id=session_id, status=status, created_at=created_at)
    return plan_file.read_bytes()


# ---------------------------------------------------------------------------
# Git repo fixture for append_session oracle tests
# (oracle needs git rev-parse --git-dir for lock dir; plan file must be in a repo)
# ---------------------------------------------------------------------------


def _make_bare_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo for oracle tests that need git context."""
    git_root = tmp_path / "append_session_repo"
    git_root.mkdir()

    def git(*args: str) -> None:
        result = subprocess.run(
            ["git", "-C", str(git_root), *args],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"

    git("init")
    git("config", "user.email", "parity@test.local")
    git("config", "user.name", "ParityTest")
    git("commit", "--allow-empty", "-m", "Initial")
    return git_root


# ---------------------------------------------------------------------------
# Byte-diff helper
# ---------------------------------------------------------------------------


def _byte_diff(oracle_bytes: bytes, native_bytes: bytes) -> str:
    oracle_lines = oracle_bytes.decode(errors="replace").splitlines(keepends=True)
    native_lines = native_bytes.decode(errors="replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(oracle_lines, native_lines, fromfile="oracle", tofile="native")
    )


# ---------------------------------------------------------------------------
# Tests: completion.reconcile_commits byte-parity (oracle required)
# ---------------------------------------------------------------------------


@_requires_oracle_reconcile
@pytest.mark.real_home  # live-tree parity oracle: resolves the real coordinator root
class TestReconcileCommitsByteParity:
    """Byte-identical parity: native reconcile_completion_commits == oracle --append mode.

    Cluster B triage (2026-07-21) — crux finding, now doubly confirmed by the
    ``.sh`` → ``.py`` repoint (2026-07-22): the retired ``reconcile-completion-commits.sh``
    had ALSO completed its strangler cutover (its own header comment: "FINISH-STRANGLER
    (native-unconditional, T2-g1): the claude-klabauter native op (completion.reconcile_commits)
    is now the only supported --append write path. The bash awk/mv legacy body is
    deleted"), and its Windows de-bash replacement ``reconcile-completion-commits.py``
    (F1-d-reconcile, "FULL FACADE COLLAPSE" per that file's own module docstring) makes
    the same fact explicit in Python: ``legacy_reconcile_commits`` in the oracle script
    is a fail-loud stub that never writes — every ``--append`` invocation dispatches
    through ``cc_invoke.route_mutation`` to the SAME native ``completion.reconcile_commits``
    op. In that sense this class shares ``TestAppendDayByteParity``'s (test_changelog_parity.py)
    self-comparison problem for the *write* logic (``_apply_commits_fold``): a bug there
    would reproduce identically on both sides.

    Unlike ``TestAppendDayByteParity``, this class is NOT a pure self-comparison overall,
    because the two sides exercise different RESOLUTION code: the oracle side passes only
    ``plan_path`` + ``session_id`` to the op, which resolves the commit SHA list internally
    via ``resolve_chain_commits`` (merge-base against origin/main, chain-slug expansion,
    multi-session ``git log --grep`` collection, SHA canonicalization — the module's own
    "REOPENED session_id call path" negative-spec in completion_ops.py). The native side
    (``_run_native_reconcile``) instead passes a pre-computed ``commits=`` list that THIS
    TEST FILE resolves independently via its own plain ``git log --grep`` in
    ``_make_reconcile_git_repo`` — separate code from ``resolve_chain_commits``. So the
    byte comparison is really checking that the op's internal git-resolution logic agrees
    with a straightforward, independently-written git query over the same fixture repo —
    a genuine differential check on the resolution machinery, not a null comparison.
    Empirically verified (perturbation test, not committed): truncating one SHA from the
    native-side ``commits=`` list makes ``test_empty_commits_list_expanded`` and
    ``test_existing_commits_appended_after`` fail with a real unified diff; reverting
    restores green — the comparison is live, not tautological.

    Keep this class; do not delete or fold into a self-comparison-only bucket. If
    ``resolve_chain_commits`` is ever refactored to stop doing its own git resolution
    (e.g. degenerating to "just call the same helper the test uses"), re-audit this
    docstring's differential-value claim.
    """

    def test_empty_commits_list_expanded(self, tmp_path: Path) -> None:
        """``commits: []`` → expanded to multi-line with new SHAs (parity: empty case)."""
        git_root, session_shas = _make_reconcile_git_repo(tmp_path, _TEST_SESSION)

        oracle_bytes = _run_oracle_reconcile(
            _COMPLETION_ENTRY_EMPTY_COMMITS, _TEST_SESSION, git_root
        )
        native_bytes = _run_native_reconcile(
            _COMPLETION_ENTRY_EMPTY_COMMITS, session_shas, tmp_path
        )

        if oracle_bytes != native_bytes:
            diff = _byte_diff(oracle_bytes, native_bytes)
            pytest.fail(
                "BYTE-PARITY FAIL for reconcile_commits (empty commits list).\n"
                f"Diff (oracle→native):\n{diff}"
            )

    def test_existing_commits_appended_after(self, tmp_path: Path) -> None:
        """New SHAs appended after existing ``commits:`` entries (parity: existing case)."""
        git_root, session_shas = _make_reconcile_git_repo(tmp_path, _TEST_SESSION)

        oracle_bytes = _run_oracle_reconcile(
            _COMPLETION_ENTRY_EXISTING_COMMITS, _TEST_SESSION, git_root
        )
        native_bytes = _run_native_reconcile(
            _COMPLETION_ENTRY_EXISTING_COMMITS, session_shas, tmp_path
        )

        if oracle_bytes != native_bytes:
            diff = _byte_diff(oracle_bytes, native_bytes)
            pytest.fail(
                "BYTE-PARITY FAIL for reconcile_commits (existing commits present).\n"
                f"Diff (oracle→native):\n{diff}"
            )

    def test_idempotent_no_change(self, tmp_path: Path) -> None:
        """Re-folding the same SHAs → file unchanged (oracle delta=0, native no_op)."""
        git_root, session_shas = _make_reconcile_git_repo(tmp_path, _TEST_SESSION)

        # First pass: both oracle and native fold SHAs in.
        oracle_first = _run_oracle_reconcile(
            _COMPLETION_ENTRY_EMPTY_COMMITS, _TEST_SESSION, git_root
        )
        oracle_first_text = oracle_first.decode("utf-8")

        # Second pass on native: re-fold the same SHAs → no_op.
        plan_file = tmp_path / "idempotent_plan.md"
        plan_file.write_text(oracle_first_text, encoding="utf-8")
        result = reconcile_completion_commits(str(plan_file), commits=session_shas)

        assert result["no_op"] is True, (
            f"expected no_op=True on re-fold of already-present SHAs, got: {result}"
        )
        assert plan_file.read_text(encoding="utf-8") == oracle_first_text, (
            "file content must not change on idempotent re-fold"
        )


# ---------------------------------------------------------------------------
# Tests: completion.reconcile_commits internal logic (oracle absent — always run)
# ---------------------------------------------------------------------------


class TestReconcileCommitsInternalLogic:
    """Internal logic tests for _apply_commits_fold — run without oracle dependency."""

    def test_fold_into_empty_inline_list(self) -> None:
        """``commits: []`` → ``commits:`` + SHA lines."""
        content = "---\ncommits: []\n---\n\nBody.\n"
        result = _apply_commits_fold(content, ["abc1234", "def5678"])
        assert 'commits:\n  - "abc1234"\n  - "def5678"\n' in result
        assert "commits: []" not in result

    def test_fold_into_populated_flow_list_yields_valid_yaml(self) -> None:
        """Populated ``commits: ["a", "b"]`` normalizes to block form; result parses.

        Regression: cross-repo memo 2026-07-22 (example-market-data-repo-em). The old
        ``^commits:`` match treated a populated flow list as a block key and appended
        block items beneath it, producing frontmatter yaml.safe_load rejects.
        """
        content = '---\ntitle: "x"\ncommits: ["eb78685", "3ef3328"]\n---\n\nBody.\n'
        result = _apply_commits_fold(content, ["18c3109", "c5523ab"])

        assert "commits: [" not in result, "flow form must be normalized away"
        assert (
            'commits:\n  - "eb78685"\n  - "3ef3328"\n  - "18c3109"\n  - "c5523ab"\n' in result
        )
        fm = result.split("---")[1]
        parsed = yaml.safe_load(fm)
        assert parsed["commits"] == ["eb78685", "3ef3328", "18c3109", "c5523ab"]

    def test_fold_into_unquoted_flow_list(self) -> None:
        """Unquoted flow items (``commits: [a, b]``) normalize identically."""
        content = "---\ncommits: [9a10aba9, 01e1b5a3]\n---\n\nBody.\n"
        result = _apply_commits_fold(content, ["ff00ff0"])
        assert 'commits:\n  - "9a10aba9"\n  - "01e1b5a3"\n  - "ff00ff0"\n' in result
        assert yaml.safe_load(result.split("---")[1])["commits"] == [
            "9a10aba9",
            "01e1b5a3",
            "ff00ff0",
        ]

    def test_flow_list_with_trailing_comment_normalizes(self) -> None:
        """A trailing ``#`` comment on the flow line does not defeat the match."""
        content = '---\ncommits: ["aaa1111"]  # filled by Step 2.6.8\n---\nBody.\n'
        result = _apply_commits_fold(content, ["bbb2222"])
        assert 'commits:\n  - "aaa1111"\n  - "bbb2222"\n' in result

    def test_parse_existing_collects_flow_items(self) -> None:
        """Flow-style SHAs are collected for dedup — not silently missed.

        Without this, every stored SHA in a flow-style entry reads as missing and
        gets re-appended as a duplicate.
        """
        content = '---\ncommits: ["aaa1111", "bbb2222"]\n---\nBody.\n'
        assert _parse_existing_commits(content) == {"aaa1111", "bbb2222"}

    def test_flow_entry_reconcile_is_idempotent(self, tmp_path: Path) -> None:
        """End-to-end: re-folding SHAs already in a flow list is a no_op, not a duplicate."""
        plan_file = tmp_path / "flow.md"
        plan_file.write_text('---\ncommits: ["aaa1111", "bbb2222"]\n---\nBody.\n', encoding="utf-8")
        result = reconcile_completion_commits(str(plan_file), commits=["aaa1111", "bbb2222"])
        assert result["no_op"] is True
        assert result["appended"] == 0
        assert result["skipped"] == 2

    def test_unrecognized_commits_shape_fails_loud(self) -> None:
        """A ``commits:`` shape that is neither flow nor block raises, never corrupts."""
        content = '---\ncommits: "not-a-list"\n---\nBody.\n'
        with pytest.raises(ValueError, match="unrecognized commits: shape"):
            _apply_commits_fold(content, ["abc1234"])
        with pytest.raises(ValueError, match="unrecognized commits: shape"):
            _parse_existing_commits(content)

    def test_fold_after_existing_entries(self) -> None:
        """New SHAs appended after existing list entries."""
        content = '---\ncommits:\n  - "aaa1111"\n---\n\nBody.\n'
        result = _apply_commits_fold(content, ["bbb2222"])
        assert '  - "aaa1111"\n  - "bbb2222"\n' in result

    def test_empty_new_shas_returns_content_unchanged(self) -> None:
        """Empty commits list → content returned unchanged."""
        content = "---\ncommits: []\n---\n\nBody.\n"
        result = _apply_commits_fold(content, [])
        assert result == content

    def test_trailing_newline_preserved(self) -> None:
        """Output ends with exactly one trailing newline (mirrors oracle printf '%s\\n')."""
        content = "---\ncommits: []\n---\nBody.\n"
        result = _apply_commits_fold(content, ["abc1234"])
        assert result.endswith("\n")
        assert not result.endswith("\n\n")

    def test_idempotency_via_parse(self, tmp_path: Path) -> None:
        """reconcile_completion_commits returns no_op when all SHAs already present."""
        plan_file = tmp_path / "idempotent.md"
        plan_file.write_text('---\ncommits:\n  - "aaa1111"\n---\nBody.\n', encoding="utf-8")
        result = reconcile_completion_commits(str(plan_file), commits=["aaa1111"])
        assert result["no_op"] is True
        assert result["appended"] == 0
        assert result["skipped"] == 1

    def test_non_idempotent_new_sha_appended(self, tmp_path: Path) -> None:
        """reconcile_completion_commits appends new SHAs and returns appended count."""
        plan_file = tmp_path / "non_idempotent.md"
        plan_file.write_text('---\ncommits:\n  - "aaa1111"\n---\nBody.\n', encoding="utf-8")
        result = reconcile_completion_commits(str(plan_file), commits=["bbb2222"])
        assert result["no_op"] is False
        assert result["appended"] == 1
        content = plan_file.read_text(encoding="utf-8")
        assert '  - "aaa1111"' in content
        assert '  - "bbb2222"' in content

    def test_native_to_native_idempotent(self, tmp_path: Path) -> None:
        """Native write then native re-fold → no_op=True (native→native idempotency).

        Review: code-reviewer (F10) — the oracle-class test_idempotent_no_change writes oracle
        output first then re-folds through native; this test exercises the symmetric case: native
        writes first, then native re-folds the same SHAs, confirming native-produced output is
        also correctly parsed by native's own _parse_existing_commits.
        """
        shas = ["aaa1234567", "bbb8901234"]
        plan_file = tmp_path / "native_idempotent.md"
        plan_file.write_text(_COMPLETION_ENTRY_EMPTY_COMMITS, encoding="utf-8")

        # First pass: native folds SHAs into the empty list.
        first = reconcile_completion_commits(str(plan_file), commits=shas)
        assert first["no_op"] is False, "first native fold should write SHAs"
        assert first["appended"] == 2
        first_text = plan_file.read_text(encoding="utf-8")

        # Second pass: re-fold the same SHAs through native — must be a no-op.
        second = reconcile_completion_commits(str(plan_file), commits=shas)
        assert second["no_op"] is True, (
            f"expected no_op=True on native→native re-fold, got: {second}"
        )
        assert plan_file.read_text(encoding="utf-8") == first_text, (
            "file content must not change on native→native idempotent re-fold"
        )

    def test_flow_list_trailing_comma_normalizes(self) -> None:
        """A trailing comma inside the flow brackets (``[a, b,]``) does not emit a blank item."""
        content = '---\ncommits: ["aaa1111", "bbb2222",]\n---\nBody.\n'
        result = _apply_commits_fold(content, ["ccc3333"])
        assert (
            'commits:\n  - "aaa1111"\n  - "bbb2222"\n  - "ccc3333"\n' in result
        ), result
        assert yaml.safe_load(result.split("---")[1])["commits"] == [
            "aaa1111",
            "bbb2222",
            "ccc3333",
        ]

    def test_multiline_flow_list_fails_loud_not_corrupt(self) -> None:
        """A flow list split across physical lines is a shape this fold does not understand.

        ``_COMMITS_FLOW_RE`` and ``_COMMITS_BLOCK_RE`` are both matched line-by-line, so a
        flow list whose closing ``]`` is on a later physical line matches neither — it falls
        through to the ``_COMMITS_ANY_RE`` guard and raises rather than being silently
        mishandled. No known writer emits this shape (the fold's own output and the oracle
        awk both write single-line flow or block form), so failing loud here is strictly
        better than guessing at a shape nothing produces.
        """
        content = '---\ncommits: ["aaa1111",\n  "bbb2222"]\n---\nBody.\n'
        with pytest.raises(ValueError, match="unrecognized commits: shape"):
            _apply_commits_fold(content, ["ccc3333"])


# ---------------------------------------------------------------------------
# Tests: completion.reconcile_commits × query-completions round-trip
# (oracle: example-doctrine-repo coordinator/bin/query-records.js, --type completion)
# ---------------------------------------------------------------------------
#
# Purpose: the flow-style corruption bug (cross-repo memo 2026-07-22,
# example-market-data-repo-em) was dangerous specifically because it was silent on BOTH
# sides — the fold reported success and query-completions dropped the mangled entry
# with no warning. A test that only asserts _apply_commits_fold's string output is
# valid YAML does not cover that symptom; these tests additionally run the folded
# entry through the real query-records.js CLI and assert it is not dropped.


@_requires_query_records_js
class TestReconcileCommitsQueryCompletionsRoundTrip:
    """End-to-end: a folded populated-flow entry survives query-completions --type completion."""

    def _query_completion(self, root: Path) -> list[dict]:
        result = subprocess.run(
            [
                _NODE_PATH,
                str(_QUERY_RECORDS_JS),
                "--type",
                "completion",
                "--root",
                str(root),
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, (
            f"query-records.js exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        payload = json.loads(result.stdout)
        records = payload["records"] if isinstance(payload, dict) else payload
        return records

    def test_folded_populated_flow_entry_still_appears(self, tmp_path: Path) -> None:
        """Regression: pre-fix, folding a populated flow list produced invalid YAML that
        query-records.js silently dropped (the entry vanished from query-completions
        output with no error on either side). Post-fix, the entry must still appear.
        """
        entry_dir = tmp_path / "archive" / "completed" / "2026-07"
        entry_dir.mkdir(parents=True)
        entry_path = entry_dir / "2026-07-22-roundtrip-test-abc123.md"
        entry_path.write_text(
            '---\n'
            'title: "Round-trip test completion entry"\n'
            "created: 2026-07-22\n"
            "nature: infra\n"
            'commits: ["eb78685", "3ef3328"]\n'
            "status: pending-release\n"
            "---\n\n"
            "Body.\n",
            encoding="utf-8",
        )

        before = self._query_completion(tmp_path)
        assert any("roundtrip-test-abc123" in r["path"] for r in before), (
            "fixture entry must be visible before the fold (sanity check)"
        )

        folded = _apply_commits_fold(entry_path.read_text(encoding="utf-8"), ["18c3109"])
        entry_path.write_text(folded, encoding="utf-8")

        after = self._query_completion(tmp_path)
        matches = [r for r in after if "roundtrip-test-abc123" in r["path"]]
        assert matches, (
            "entry vanished from query-completions after folding — this is exactly the "
            "silent-corruption symptom the fix exists to prevent"
        )
        assert matches[0]["frontmatter"]["commits"] == ["eb78685", "3ef3328", "18c3109"]


# ---------------------------------------------------------------------------
# Tests: plan.append_session byte-parity (oracle required)
# ---------------------------------------------------------------------------


@_requires_oracle_append_session
@pytest.mark.real_home  # live-tree parity oracle: resolves the real CLAUDE_KLABAUTER_ROOT via the
# settings-home pointer file / machine-local registry (cc_invoke._resolve_claude_klabauter_root),
# same rationale as TestReconcileCommitsByteParity above — quarantining HOME turns this
# into a spurious "CLAUDE_KLABAUTER_ROOT could not be resolved" transport failure rather than a
# meaningful byte-parity comparison.
class TestAppendSessionByteParity:
    """Byte-identical parity: native append_plan_session == oracle append-plan-session.py."""

    def test_session_appended_to_existing_key(self, tmp_path: Path) -> None:
        """Session entry appended to existing ``agent_sessions:`` block."""
        git_root = _make_bare_git_repo(tmp_path)

        oracle_bytes, created_at = _run_oracle_append_session(
            _PLAN_WITH_AGENT_SESSIONS_KEY, _TEST_SESSION, git_root
        )
        native_bytes = _run_native_append_session(
            _PLAN_WITH_AGENT_SESSIONS_KEY, _TEST_SESSION, created_at, tmp_path
        )

        if oracle_bytes != native_bytes:
            diff = _byte_diff(oracle_bytes, native_bytes)
            pytest.fail(
                "BYTE-PARITY FAIL for append_plan_session (existing agent_sessions: key).\n"
                f"Diff (oracle→native):\n{diff}"
            )

    def test_session_appended_after_existing_entry(self, tmp_path: Path) -> None:
        """Session entry appended after existing entries in ``agent_sessions:`` block."""
        git_root = _make_bare_git_repo(tmp_path)

        oracle_bytes, created_at = _run_oracle_append_session(
            _PLAN_WITH_EXISTING_SESSION, _TEST_SESSION_2, git_root
        )
        native_bytes = _run_native_append_session(
            _PLAN_WITH_EXISTING_SESSION, _TEST_SESSION_2, created_at, tmp_path
        )

        if oracle_bytes != native_bytes:
            diff = _byte_diff(oracle_bytes, native_bytes)
            pytest.fail(
                "BYTE-PARITY FAIL for append_plan_session (appended after existing entry).\n"
                f"Diff (oracle→native):\n{diff}"
            )

    def test_block_created_when_key_absent(self, tmp_path: Path) -> None:
        """``agent_sessions:`` block created when key absent."""
        git_root = _make_bare_git_repo(tmp_path)

        oracle_bytes, created_at = _run_oracle_append_session(
            _PLAN_WITHOUT_AGENT_SESSIONS_KEY, _TEST_SESSION, git_root
        )
        native_bytes = _run_native_append_session(
            _PLAN_WITHOUT_AGENT_SESSIONS_KEY, _TEST_SESSION, created_at, tmp_path
        )

        if oracle_bytes != native_bytes:
            diff = _byte_diff(oracle_bytes, native_bytes)
            pytest.fail(
                "BYTE-PARITY FAIL for append_plan_session (key absent → block created).\n"
                f"Diff (oracle→native):\n{diff}"
            )


# ---------------------------------------------------------------------------
# Tests: plan.append_session internal logic (oracle absent — always run)
# ---------------------------------------------------------------------------


class TestAppendSessionInternalLogic:
    """Internal logic tests for _apply_session_append — run without oracle dependency."""

    def test_entry_format_is_pipe_delimited(self) -> None:
        """Entry is formatted as ``session_id|status|created_at``."""
        content = "---\nagent_sessions:\n---\n\nBody.\n"
        modified, appended = _apply_session_append(
            content, "test-sid", "working", "2026-01-15T10:00:00Z"
        )
        assert appended is True
        assert '  - "test-sid|working|2026-01-15T10:00:00Z"' in modified

    def test_idempotent_same_session_id(self) -> None:
        """Same session_id → idempotent no-op (oracle Layer-3 dedup)."""
        content = (
            '---\nagent_sessions:\n  - "test-sid|working|2026-01-15T10:00:00Z"\n---\n\nBody.\n'
        )
        modified, appended = _apply_session_append(
            content, "test-sid", "working", "2026-01-15T12:00:00Z"
        )
        assert appended is False
        assert modified == content

    def test_key_absent_creates_block_before_closing_fence(self) -> None:
        """When ``agent_sessions:`` key absent, block inserted before closing ``---``."""
        content = "---\ntitle: test\n---\n\nBody.\n"
        modified, appended = _apply_session_append(
            content, "test-sid", "working", "2026-01-15T10:00:00Z"
        )
        assert appended is True
        # agent_sessions: must appear in the frontmatter (before the second ---)
        lines = modified.splitlines()
        fm_close = next(i for i, l in enumerate(lines) if i > 0 and l == "---")
        fm_content = "\n".join(lines[:fm_close])
        assert "agent_sessions:" in fm_content
        assert '  - "test-sid|working|2026-01-15T10:00:00Z"' in fm_content

    def test_inserted_after_last_existing_entry(self) -> None:
        """New entry inserted after last existing entry (not after key)."""
        content = (
            "---\nagent_sessions:\n"
            '  - "s1|working|2026-01-01T00:00:00Z"\n'
            '  - "s2|working|2026-01-02T00:00:00Z"\n'
            "---\nBody.\n"
        )
        modified, appended = _apply_session_append(
            content, "s3", "working", "2026-01-03T00:00:00Z"
        )
        assert appended is True
        # s3 must come after s2
        idx_s2 = modified.index("s2|")
        idx_s3 = modified.index("s3|")
        assert idx_s3 > idx_s2, "new entry must appear after last existing entry"

    def test_trailing_newline_preserved(self) -> None:
        """Output ends with exactly one trailing newline (mirrors printf '%s\\n')."""
        content = "---\nagent_sessions:\n---\nBody.\n"
        modified, _ = _apply_session_append(
            content, "sid", "working", "2026-01-15T10:00:00Z"
        )
        assert modified.endswith("\n")

    def test_malformed_no_closing_fence_raises(self) -> None:
        """Malformed frontmatter (no closing ``---``) raises ValueError."""
        content = "---\ntitle: test\n"
        with pytest.raises(ValueError, match="closing"):
            _apply_session_append(content, "sid", "working", "2026-01-15T10:00:00Z")

    def test_append_plan_session_idempotent_via_public_api(self, tmp_path: Path) -> None:
        """``append_plan_session`` returns ``no_op=True`` on re-append of same session."""
        plan_file = tmp_path / "idem.md"
        plan_file.write_text(
            '---\nagent_sessions:\n  - "s1|working|2026-01-15T10:00:00Z"\n---\nBody.\n',
            encoding="utf-8",
        )
        result = append_plan_session(
            str(plan_file), session_id="s1", created_at="2026-01-15T12:00:00Z"
        )
        assert result["no_op"] is True
        assert result["appended"] is False

    def test_append_plan_session_writes_entry(self, tmp_path: Path) -> None:
        """``append_plan_session`` writes a new entry and returns ``appended=True``."""
        plan_file = tmp_path / "new_entry.md"
        plan_file.write_text("---\nagent_sessions:\n---\nBody.\n", encoding="utf-8")
        result = append_plan_session(
            str(plan_file),
            session_id="new-session",
            status="working",
            created_at="2026-01-15T10:00:00Z",
        )
        assert result["appended"] is True
        assert result["no_op"] is False
        content = plan_file.read_text(encoding="utf-8")
        assert '  - "new-session|working|2026-01-15T10:00:00Z"' in content

    def test_append_plan_session_creates_lock_sidecar(self, tmp_path: Path) -> None:
        """D2b (DR-216 § D2(vi), AMENDED 2026-08-06): a real write for a plan
        file living inside a git repo runs under ``locked_rmw`` — asserts the
        sidecar lock file lands under the repo's git common dir."""
        repo, plan_file = _make_repo_with_plan(
            tmp_path, "lock-check.md", "---\nagent_sessions:\n---\nBody.\n"
        )

        lock_dir = repo / ".git" / "coordinator-locks"
        assert not lock_dir.exists()

        result = append_plan_session(
            str(plan_file),
            session_id="lock-session",
            status="working",
            created_at="2026-01-15T10:00:00Z",
        )

        assert result["appended"] is True
        assert lock_dir.is_dir()
        assert list(lock_dir.glob("*.lock"))


# ---------------------------------------------------------------------------
# Tests: path-containment guard (handler-level; both ops)
#
# Op-family path-containment sweep, 2026-07-08: both handlers now confine the
# resolved plan_path via the shared _path_guard helper, mirroring
# handoff.has_live_children's dual-root state/handoffs/ + archive/handoffs/
# allow-list. completion.reconcile_commits allows BOTH archive/completed/ (the
# empirical production noun) and docs/plans/ (DR-216's originally-stated noun,
# kept as a conservative superset); plan.append_session allows docs/plans/ only
# (confirmed correct — see Finding 5/3 of the 2026-07-08 completion-ops
# path-containment review).
#
# These tests call the JSON-RPC handlers directly (_reconcile_commits_handler /
# _append_session_handler) — NOT the underlying reconcile_completion_commits /
# append_plan_session functions — because the guard lives in the handler.
#
# Spec backlink: docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4
# ---------------------------------------------------------------------------


import asyncio

from coordinator_core.ops.completion_ops import (
    _append_session_handler,
    _reconcile_commits_handler,
)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_repo_with_plan(tmp_path: Path, plan_name: str, plan_content: str) -> tuple[Path, Path]:
    """Create a minimal git repo with a docs/plans/<plan_name> fixture.

    Returns (repo_root, plan_file_path). repo_root / ".git" is the common_dir the
    handler expects as its repo_root argument (common_dir scope; P9 worktree derivation).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "completion-guard-test@claude-klabauter.test")
    _git("config", "user.name", "Completion Guard Test")
    _git("config", "commit.gpgsign", "false")

    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plans_dir / plan_name
    plan_file.write_text(plan_content, encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo, plan_file


def _make_repo_with_completed_entry(
    tmp_path: Path, entry_name: str, entry_content: str
) -> tuple[Path, Path]:
    """Create a minimal git repo with an archive/completed/<entry_name> fixture.

    Mirrors ``_make_repo_with_plan`` but rooted at ``archive/completed/`` — the real
    production noun for completion entries (empirically confirmed: 61 status:
    pending-release entries under archive/completed/, 0 under docs/plans/).

    Returns (repo_root, entry_file_path). repo_root / ".git" is the common_dir the
    handler expects as its repo_root argument (common_dir scope; P9 worktree derivation).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "completion-guard-test@claude-klabauter.test")
    _git("config", "user.name", "Completion Guard Test")
    _git("config", "commit.gpgsign", "false")

    completed_dir = repo / "archive" / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    entry_file = completed_dir / entry_name
    entry_file.write_text(entry_content, encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo, entry_file


class TestCompletionReconcileCommitsPathContainment:
    """completion.reconcile_commits handler: dual-root (archive/completed/ + docs/plans/)
    containment reject tests."""

    def test_rejects_traversal_path(self, tmp_path: Path) -> None:
        """A plan_path with '../' segments escaping docs/plans/ is rejected;
        the out-of-tree target is NOT mutated."""
        repo, _plan_file = _make_repo_with_plan(
            tmp_path, "in-scope.md", _COMPLETION_ENTRY_EMPTY_COMMITS
        )
        secret = repo / "docs" / "secret.md"
        secret.write_text(_COMPLETION_ENTRY_EMPTY_COMMITS, encoding="utf-8")
        original = secret.read_text(encoding="utf-8")

        result = _run(_reconcile_commits_handler(
            {
                "plan_path": "docs/plans/../secret.md",
                "commits": ["abc1234"],
            },
            repo_root=repo / ".git",
        ))

        assert "error" in result, f"traversal path must be rejected; got {result!r}"
        assert result.get("no_op") is True
        assert secret.read_text(encoding="utf-8") == original

    def test_rejects_out_of_tree_absolute_path(self, tmp_path: Path) -> None:
        """An out-of-tree absolute plan_path is rejected; target is NOT mutated."""
        repo, _plan_file = _make_repo_with_plan(
            tmp_path, "in-scope.md", _COMPLETION_ENTRY_EMPTY_COMMITS
        )
        outside = tmp_path / "outside" / "secret.md"
        outside.parent.mkdir(parents=True)
        outside.write_text(_COMPLETION_ENTRY_EMPTY_COMMITS, encoding="utf-8")
        original = outside.read_text(encoding="utf-8")

        result = _run(_reconcile_commits_handler(
            {"plan_path": str(outside), "commits": ["abc1234"]},
            repo_root=repo / ".git",
        ))

        assert "error" in result, f"out-of-tree path must be rejected; got {result!r}"
        assert result.get("no_op") is True
        assert outside.read_text(encoding="utf-8") == original

    def test_rejects_when_repo_root_none(self, tmp_path: Path) -> None:
        """repo_root=None is rejected outright — no containment fallback exists
        for the callerless-repo_root case (mirrors handoff.stamp)."""
        outside = tmp_path / "outside" / "secret.md"
        outside.parent.mkdir(parents=True)
        outside.write_text(_COMPLETION_ENTRY_EMPTY_COMMITS, encoding="utf-8")
        original = outside.read_text(encoding="utf-8")

        result = _run(_reconcile_commits_handler(
            {"plan_path": str(outside), "commits": ["abc1234"]},
            repo_root=None,
        ))

        assert "error" in result, f"repo_root=None must be rejected; got {result!r}"
        assert result.get("no_op") is True
        assert outside.read_text(encoding="utf-8") == original

    def test_in_scope_docs_plans_path_still_succeeds(self, tmp_path: Path) -> None:
        """Sanity check: a genuine docs/plans/ path still folds SHAs successfully
        through the guarded handler (guard does not regress the happy path;
        docs/plans/ remains an allowed root alongside archive/completed/)."""
        repo, plan_file = _make_repo_with_plan(
            tmp_path, "in-scope.md", _COMPLETION_ENTRY_EMPTY_COMMITS
        )

        result = _run(_reconcile_commits_handler(
            {"plan_path": str(plan_file), "commits": ["abc1234"]},
            repo_root=repo / ".git",
        ))

        assert "error" not in result, f"in-scope path must succeed; got {result!r}"
        assert result.get("appended") == 1
        assert '  - "abc1234"' in plan_file.read_text(encoding="utf-8")

    def test_in_scope_archive_completed_path_succeeds(self, tmp_path: Path) -> None:
        """The real production noun: a genuine archive/completed/ path folds SHAs
        successfully — this is the shape Finding 1 widened the guard to accept
        (previously rejected under the docs/plans/-only guard)."""
        repo, entry_file = _make_repo_with_completed_entry(
            tmp_path, "in-scope-entry.md", _COMPLETION_ENTRY_EMPTY_COMMITS
        )

        result = _run(_reconcile_commits_handler(
            {"plan_path": str(entry_file), "commits": ["abc1234"]},
            repo_root=repo / ".git",
        ))

        assert "error" not in result, f"archive/completed/ path must succeed; got {result!r}"
        assert result.get("appended") == 1
        assert '  - "abc1234"' in entry_file.read_text(encoding="utf-8")


class TestAppendSessionPathContainment:
    """plan.append_session handler: docs/plans/ containment reject tests."""

    def test_rejects_traversal_path(self, tmp_path: Path) -> None:
        """A plan_path with '../' segments escaping docs/plans/ is rejected;
        the out-of-tree target is NOT mutated."""
        repo, _plan_file = _make_repo_with_plan(
            tmp_path, "in-scope.md", _PLAN_WITH_AGENT_SESSIONS_KEY
        )
        secret = repo / "docs" / "secret.md"
        secret.write_text(_PLAN_WITH_AGENT_SESSIONS_KEY, encoding="utf-8")
        original = secret.read_text(encoding="utf-8")

        result = _run(_append_session_handler(
            {
                "plan_path": "docs/plans/../secret.md",
                "session_id": "test-sid",
                "created_at": "2026-01-15T10:00:00Z",
            },
            repo_root=repo / ".git",
        ))

        assert "error" in result, f"traversal path must be rejected; got {result!r}"
        assert result.get("no_op") is True
        assert secret.read_text(encoding="utf-8") == original

    def test_rejects_out_of_tree_absolute_path(self, tmp_path: Path) -> None:
        """An out-of-tree absolute plan_path is rejected; target is NOT mutated."""
        repo, _plan_file = _make_repo_with_plan(
            tmp_path, "in-scope.md", _PLAN_WITH_AGENT_SESSIONS_KEY
        )
        outside = tmp_path / "outside" / "secret.md"
        outside.parent.mkdir(parents=True)
        outside.write_text(_PLAN_WITH_AGENT_SESSIONS_KEY, encoding="utf-8")
        original = outside.read_text(encoding="utf-8")

        result = _run(_append_session_handler(
            {
                "plan_path": str(outside),
                "session_id": "test-sid",
                "created_at": "2026-01-15T10:00:00Z",
            },
            repo_root=repo / ".git",
        ))

        assert "error" in result, f"out-of-tree path must be rejected; got {result!r}"
        assert result.get("no_op") is True
        assert outside.read_text(encoding="utf-8") == original

    def test_rejects_when_repo_root_none(self, tmp_path: Path) -> None:
        """repo_root=None is rejected outright — no containment fallback exists
        for the callerless-repo_root case (mirrors handoff.stamp)."""
        outside = tmp_path / "outside" / "secret.md"
        outside.parent.mkdir(parents=True)
        outside.write_text(_PLAN_WITH_AGENT_SESSIONS_KEY, encoding="utf-8")
        original = outside.read_text(encoding="utf-8")

        result = _run(_append_session_handler(
            {
                "plan_path": str(outside),
                "session_id": "test-sid",
                "created_at": "2026-01-15T10:00:00Z",
            },
            repo_root=None,
        ))

        assert "error" in result, f"repo_root=None must be rejected; got {result!r}"
        assert result.get("no_op") is True
        assert outside.read_text(encoding="utf-8") == original

    def test_in_scope_path_still_succeeds(self, tmp_path: Path) -> None:
        """Sanity check: a genuine docs/plans/ path still appends the session entry
        successfully through the guarded handler (guard does not regress the happy path)."""
        repo, plan_file = _make_repo_with_plan(
            tmp_path, "in-scope.md", _PLAN_WITH_AGENT_SESSIONS_KEY
        )

        result = _run(_append_session_handler(
            {
                "plan_path": str(plan_file),
                "session_id": "test-sid",
                "created_at": "2026-01-15T10:00:00Z",
            },
            repo_root=repo / ".git",
        ))

        assert "error" not in result, f"in-scope path must succeed; got {result!r}"
        assert result.get("appended") is True
        assert (
            '  - "test-sid|working|2026-01-15T10:00:00Z"'
            in plan_file.read_text(encoding="utf-8")
        )
