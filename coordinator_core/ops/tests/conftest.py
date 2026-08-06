"""Shared pytest fixtures for coordinator_core.ops.tests.

Provides ``handoff_repo`` — a temporary git-init'd repository with
state/handoffs/ pre-created, plus a ``HandoffRepo`` helper for seeding
fully-valid (schema-compliant) handoff files and asserting on their state.

Pattern mirrors coordinator_core/ops/fleet/tests/conftest.py (fleet_repo).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# HandoffRepo helper
# ---------------------------------------------------------------------------


class HandoffRepo:
    """Wrapper around a temporary git repository for handoff.transition op tests.

    Negative-spec: seed_handoff writes FULL schema-valid frontmatter (title,
    created, branch, status, predecessor) so post-mutation validate_frontmatter
    passes on old-date (pre-2026-05-29) handoffs.  Tests that exercise
    category/summary requirements use the optional keyword args.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )

    @property
    def common_dir(self) -> Path:
        """Absolute path to the git common dir (.git/ for a standard repo)."""
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    def seed_handoff(
        self,
        name: str,
        status: str,
        *,
        created: str = "2026-01-01",
        branch: str = "work/test/2026-01-01",
        predecessor: Optional[str] = "none",
        deployment_state: Optional[str] = None,
        claimed_at: Optional[str] = None,
        claimed_by: Optional[str] = None,
        shipped_in: Optional[str] = None,
        shipped_in_kind: Optional[str] = None,
        category: Optional[str] = None,
        summary: Optional[str] = None,
        extra: str = "",
    ) -> Path:
        """Write and commit a state/handoffs/<name>.md with full schema-required frontmatter.

        Uses created=2026-01-01 by default so pre-cutoff rules (category/summary)
        do NOT fire.  Pass created >= '2026-05-29' and category + summary to test
        post-cutoff validation paths.

        Returns the absolute Path of the created file.
        """
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)

        # Build frontmatter lines in insertion order.
        lines = [
            f'title: "Test Handoff {name}"',
            f"created: {created}",
            f"branch: {branch}",
            f"status: {status}",
        ]
        # Review: code-reviewer (F9) — bare `none` is a YAML 1.1 null scalar (PyYAML
        # parses it as None, not the string "none"). Quote string values; emit bare null
        # for Python None so the intent is explicit.
        if predecessor is None:
            lines.append("predecessor: null")
        else:
            lines.append(f'predecessor: "{predecessor}"')
        if deployment_state is not None:
            lines.append(f"deployment_state: {deployment_state}")
        if claimed_at is not None:
            lines.append(f"claimed_at: {claimed_at}")
        if claimed_by is not None:
            lines.append(f"claimed_by: {claimed_by}")
        if shipped_in is not None:
            lines.append(f"shipped_in: {shipped_in}")
            if shipped_in_kind is not None:
                lines.append(f"shipped_in_kind: {shipped_in_kind}")
        if category is not None:
            lines.append(f"category: {category}")
        if summary is not None:
            # Wrap in quotes; escape internal quotes.
            safe = summary.replace('"', '\\"')
            lines.append(f'summary: "{safe}"')
        if extra:
            lines.append(extra)

        fm_block = "\n".join(lines)
        # Construct directly — textwrap.dedent cannot handle a multi-line fm_block
        # substitution correctly (subsequent lines of fm_block have 0 leading spaces,
        # which makes the minimum-indentation stripping fail).
        content = f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def read_text(self, name: str) -> str:
        """Return the raw content of state/handoffs/<name>."""
        return (self.root / "state" / "handoffs" / name).read_text(encoding="utf-8")

    def abs_path(self, name: str) -> str:
        """Return the absolute path string for state/handoffs/<name>."""
        return str(self.root / "state" / "handoffs" / name)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def handoff_repo(tmp_path) -> HandoffRepo:
    """Provide a temporary git repository for handoff.transition op tests.

    The repo has:
    - git config user.email / user.name / commit.gpgsign=false set
    - state/handoffs/ skeleton committed (so git read-tree HEAD works)
    - An initial commit at HEAD

    Usage::

        def test_something(handoff_repo):
            handoff_repo.seed_handoff("test.md", "open")
            result = _run(_handler(
                {"verb": "consume", "handoff_path": handoff_repo.abs_path("test.md"),
                 "session_id": "s-abc", "at": "2026-01-02T00:00:00Z"},
                None,
                repo_root=handoff_repo.common_dir,
            ))
            assert result["exit_code"] == 0
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo_root),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "handoff-test@claude-klabauter.test")
    _git("config", "user.name", "Handoff Test")
    _git("config", "commit.gpgsign", "false")

    # Pre-create state/handoffs/ so the directory exists from the initial commit.
    (repo_root / "state" / "handoffs").mkdir(parents=True)
    (repo_root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return HandoffRepo(repo_root)


# ---------------------------------------------------------------------------
# NormRepo helper — lightweight fixture for handoff.normalize tests
# ---------------------------------------------------------------------------


class NormRepo:
    """Wrapper around a temporary git repository for handoff.normalize op tests.

    Unlike HandoffRepo (which seeds schema-valid files and commits them),
    NormRepo accepts raw file content so tests can craft exact frontmatter
    (quoted booleans, ISO timestamps, missing fields, etc.) without going
    through a higher-level serializer.

    The normalize op does NOT perform any git operations, so this repo
    needs only a .git directory (for main_worktree_root P9 derivation).
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def common_dir(self) -> Path:
        """Absolute path to the git common dir (.git/ for a non-worktree repo).

        P9: handlers receive this path as their ``repo_root`` arg; the handler
        calls main_worktree_root(repo_root) = common_dir.parent to obtain the
        actual worktree root and build state/handoffs/ paths from it.
        """
        return (self.root / ".git").resolve()

    def seed_handoff(self, name: str, raw_content: str) -> Path:
        """Write raw markdown content to state/handoffs/<name>.

        Raw content is written verbatim — no frontmatter serialization, no
        git commit.  The normalize handler reads from disk directly.

        Returns the absolute path to the created file.
        """
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw_content, encoding="utf-8")
        return path

    def read_handoff(self, name: str) -> str:
        """Return the current on-disk content of state/handoffs/<name>."""
        return (self.root / "state" / "handoffs" / name).read_text(encoding="utf-8")

    def path_exists(self, repo_rel: str) -> bool:
        """Return True iff repo_root / repo_rel exists on disk."""
        return (self.root / repo_rel).exists()


@pytest.fixture
def norm_repo(tmp_path) -> NormRepo:
    """Provide a temporary git-init'd repository for handoff.normalize op tests.

    The repo has:
      - git config user.email / user.name / commit.gpgsign=false set
      - state/handoffs/ directory pre-created
      - No initial commit needed (normalize op does no git operations)

    Usage::

        def test_something(norm_repo):
            norm_repo.seed_handoff("2026-07-01-h.md", "---\\ntitle: T\\n---\\n")
            result = asyncio.run(_handler({"write": True}, None,
                                          repo_root=norm_repo.common_dir))
            assert result["exit_code"] == 0
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo_root),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "norm-test@claude-klabauter.test")
    _git("config", "user.name", "Normalize Test")
    _git("config", "commit.gpgsign", "false")

    (repo_root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)

    return NormRepo(repo_root)
