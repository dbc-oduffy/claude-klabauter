"""
coordinator_core.ops.tests.test_completion_parity — byte-parity harness for
plan.append_session.

Purpose: Assert the Python op produces byte-identical file-write output to its legacy
bash oracle for the same inputs. This is the strangler invariant (strang-10 C2): if the
byte output drifts, the DoE facade routing will silently produce different on-disk content.
(``completion.reconcile_commits``'s parity coverage was removed with the op — killed and
rebuilt from scratch per PM ruling, 2026-08-23.)

Coverage:
  (a) plan.append_session — byte-parity: session entry appended to existing agent_sessions:
  (b) plan.append_session — byte-parity: agent_sessions: block created when key absent
  (c) plan.append_session — idempotency: same session_id → no-op

Oracle skip semantics: when the oracle CLI (or its dependency, bash) is absent, tests are
skipped with a clear diagnostic — NOT silently passed. Oracle skips are expected in CI
environments that don't have the DoE-claude sibling repo.

Spec backlink: pln-strang-10-residual-writer-clus-b67ff8 § C2
Oracle: [DoE-claude] coordinator/bin/append-plan-session.py
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
"""

from __future__ import annotations
import sys

import difflib
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Direct function imports — no registration required for parity tests.
# The ops are NOT yet in ops/__init__.py (that lands in C3).
# ---------------------------------------------------------------------------

from coordinator_core.ops.completion_ops import (
    _apply_session_append,
    append_plan_session,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Oracle / DoE-root path resolution
# ---------------------------------------------------------------------------

_DOE_ROOT_SENTINEL = Path.home() / ".claude" / ".doe-root"
_ORACLE_APPEND_SESSION: Optional[Path] = None

if _DOE_ROOT_SENTINEL.exists():
    try:
        _doe_root = _DOE_ROOT_SENTINEL.read_text(encoding="utf-8").strip()
        _ORACLE_APPEND_SESSION = (
            Path(_doe_root) / "coordinator" / "bin" / "append-plan-session.py"
        )
    except OSError:
        print(f"skip: <module>: _doe_root = _DOE_ROOT_SENTINEL.read_text(encoding=\"utf-8\").strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass

# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------

_ORACLE_APPEND_SESSION_AVAILABLE = (
    _ORACLE_APPEND_SESSION is not None and _ORACLE_APPEND_SESSION.is_file()
)

_requires_oracle_append_session = pytest.mark.skipif(
    not _ORACLE_APPEND_SESSION_AVAILABLE,
    reason=(
        "append-plan-session.py oracle absent "
        f"(oracle={_ORACLE_APPEND_SESSION_AVAILABLE})"
    ),
)

# ---------------------------------------------------------------------------
# Fixed test constants
# ---------------------------------------------------------------------------

_TEST_SESSION = "test-completion-parity-session-0001"
_TEST_SESSION_2 = "test-completion-parity-session-0002"

# Fixture: plan completion entry in the EXACT production on-disk shape.
# Matches archive/completed/**/*.md format: YAML frontmatter with commits: key.


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
# Oracle runner helpers
# ---------------------------------------------------------------------------


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
# Tests: path-containment guard (handler-level)
#
# Op-family path-containment sweep, 2026-07-08: the handler confines the
# resolved plan_path via the shared _path_guard helper, mirroring
# handoff.has_live_children's dual-root state/handoffs/ + archive/handoffs/
# allow-list. plan.append_session allows docs/plans/ only (confirmed correct
# — see Finding 5/3 of the 2026-07-08 completion-ops path-containment review).
#
# This test calls the JSON-RPC handler directly (_append_session_handler) —
# NOT the underlying append_plan_session function — because the guard lives
# in the handler.
#
# Spec backlink: docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4
# ---------------------------------------------------------------------------


import asyncio

from coordinator_core.ops.completion_ops import _append_session_handler
from coordinator_core.win_portability import no_console_creationflags


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
            **no_console_creationflags(),
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
