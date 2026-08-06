"""Shared pytest fixtures for coordinator_core.ops.fleet tests.

Primary fixture: ``fleet_repo`` — a temporary ``git init -b main`` repository with:
- user.email / user.name / commit.gpgsign=false configured
- Standard artifact directory tree (docs/plans/, state/handoffs/,
  state/bug-backlog/, archive/specs/, archive/handoffs/, archive/bug-backlog/)
- An initial commit so git read-tree HEAD and git log work from the first test

The ``FleetRepo`` helper returned by the fixture provides:
- ``seed_plan(name, status, title)``        — write + commit a docs/plans/<name>.md
- ``seed_handoff(name, status, ...)``        — write + commit a state/handoffs/<name>.md
- ``seed_bug(name, status, ...)``            — write + commit a state/bug-backlog/<name>.yaml
- ``git_status_clean()``                     — True iff git status --porcelain is empty
- ``git_log_names(n)``                       — list of file paths in the last n commits
- ``git_log_subject()``                      — subject line of the most recent commit
- ``common_dir``                             — Path to git common dir (.git for main repo)

Pattern mirrors: coordinator_core/tests/test_dispatch_message.py:737-797 (git init -b main,
config user, initial commit) and coordinator_core/ops/emit/tests/ (co-located conftest).

Negative-spec:
- The git repo uses --no-gpg-sign via git config (commit.gpgsign=false) so test
  commits do not require a GPG keyring.
- Each call to a seed_* method stages and commits immediately so that subsequent
  git mv calls find the files in the git index.
- seed_* helpers create the minimum valid frontmatter — just enough for the
  parse_frontmatter_status predicate used by the handlers.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import List, Optional

import pytest


# ---------------------------------------------------------------------------
# FleetRepo helper
# ---------------------------------------------------------------------------

class FleetRepo:
    """Wrapper around a temporary git repository for fleet op tests.

    Do not instantiate directly — use the ``fleet_repo`` fixture.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- Internal git wrapper ------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )

    def _git_unchecked(self, *args: str) -> subprocess.CompletedProcess:
        """Like _git but does not raise on non-zero exit."""
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(self.root),
            capture_output=True,
        )

    # -- common_dir property -------------------------------------------------

    @property
    def common_dir(self) -> Path:
        """Absolute path to the git common dir (always .git/ for a non-worktree repo)."""
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    # -- Seed helpers --------------------------------------------------------

    def seed_plan(
        self,
        name: str,
        status: str,
        title: str = "Test Plan",
        created: str = "2026-01-01",
        extra_frontmatter: str = "",
    ) -> Path:
        """Write and commit a docs/plans/<name>.md with the given frontmatter status.

        Returns the absolute path to the created file.
        The file is staged and committed so it appears in the git index.
        """
        path = self.root / "docs" / "plans" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = textwrap.dedent(f"""\
            ---
            title: "{title}"
            status: {status}
            created: {created}
            {extra_frontmatter}
            ---

            # {title}

            Plan body.
        """)
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add plan {name}")
        return path

    def seed_plan_sidecar(
        self,
        stem: str,
        sidecar_suffix: str = ".review.md",
        content: str = "sidecar content",
    ) -> Path:
        """Write and commit a sidecar file alongside a plan with the given stem.

        Sidecar name is <stem><sidecar_suffix>, e.g. "2026-01-01-my-plan.review.md".
        Returns the absolute path to the sidecar.
        """
        name = stem + sidecar_suffix
        path = self.root / "docs" / "plans" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add plan sidecar {name}")
        return path

    def seed_handoff(
        self,
        name: str,
        status: str,
        predecessor: Optional[str] = None,
        claimed_by: Optional[str] = None,
        created: str = "2026-01-01",
        extra_frontmatter: str = "",
    ) -> Path:
        """Write and commit a state/handoffs/<name>.md with the given frontmatter.

        predecessor: if set, adds a 'predecessor: <predecessor>' frontmatter key
            so reverse_membership tests can simulate live-children scenarios.
        claimed_by: if set, adds 'claimed_by: <claimed_by>' frontmatter key
            (session id that claimed this handoff).

        Returns the absolute path to the created file.
        """
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)

        predecessor_line = f"predecessor: {predecessor}" if predecessor else ""
        claimed_by_line = f"claimed_by: {claimed_by}" if claimed_by else ""

        # Build frontmatter lines individually rather than via textwrap.dedent on
        # one interpolated f-string block: dedent requires a common leading-whitespace
        # prefix across ALL lines, but a multi-line extra_frontmatter value (e.g.
        # "deployment_state: shipped\nshipped_in: <sha>") substitutes its second line
        # with ZERO leading indentation, breaking dedent's common-prefix computation
        # for the WHOLE block — the resulting file keeps stray leading spaces on
        # every line ("    ---" instead of "---") and _read_meta silently returns {}
        # (latent-bug fix, see test_archive_handoffs.py Branch-B shipped/abandoned
        # tests, 2026-07-13).
        frontmatter_lines = ["---", 'title: "Test Handoff"', f"status: {status}", f"created: {created}"]
        for extra_block in (predecessor_line, claimed_by_line, extra_frontmatter):
            if extra_block:
                frontmatter_lines.extend(extra_block.splitlines())
        frontmatter_lines.append("---")
        content = "\n".join(frontmatter_lines) + "\n\n# Handoff\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def seed_bug(
        self,
        name: str,
        status: str,
        created: str = "2026-01-01",
        title: str = "Test Bug",
        extra_frontmatter: str = "",
    ) -> Path:
        """Write and commit a state/bug-backlog/<name>.yaml with the given status.

        Returns the absolute path to the created file.
        YAML frontmatter at the top (no --- delimiters — YAML file, not Markdown).
        """
        path = self.root / "state" / "bug-backlog" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = textwrap.dedent(f"""\
            title: "{title}"
            status: {status}
            created: {created}
            {extra_frontmatter}
        """)
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add bug {name}")
        return path

    def update_file_status(self, path: Path, new_status: str) -> None:
        """Overwrite the 'status:' line in a file and commit the change.

        Used in D1 drift tests: mutate a terminal artifact back to live between
        preview and act to verify the re-verify guard classifies it as skipped.
        """
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        new_lines = []
        replaced = False
        for line in lines:
            if not replaced and line.startswith("status:"):
                new_lines.append(f"status: {new_status}\n")
                replaced = True
            else:
                new_lines.append(line)
        path.write_text("".join(new_lines), encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"drift {path.name} status → {new_status}")

    # -- Git state assertion helpers -----------------------------------------

    def git_status_clean(self) -> bool:
        """Return True iff the working tree + index are clean (no staged or unstaged changes)."""
        result = self._git_unchecked("status", "--porcelain")
        return result.stdout.strip() == b""

    def git_log_names(self, n: int = 1) -> List[str]:
        """Return a list of filenames touched in the last n commits.

        Each entry is a repo-relative path (forward slashes on all platforms).
        Empty lines and the blank separator between --format= and filenames are excluded.
        """
        result = subprocess.run(
            ["git", "log", f"-{n}", "--name-only", "--format="],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        return [
            line
            for line in result.stdout.decode(errors="replace").strip().splitlines()
            if line.strip()
        ]

    def git_log_subject(self, n: int = 1) -> str:
        """Return the commit subject of the most recent (or nth most recent) commit."""
        result = subprocess.run(
            ["git", "log", f"-{n}", "--format=%s"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        lines = [l for l in result.stdout.decode(errors="replace").strip().splitlines() if l]
        return lines[0] if lines else ""

    def path_exists(self, repo_rel: str) -> bool:
        """Return True iff repo_root / repo_rel exists on disk."""
        return (self.root / repo_rel).exists()

    def repo_rel(self, absolute: Path) -> str:
        """Return the repo-relative wire id for an absolute path — POSIX shape.

        Mirrors coordinator_core.ops.fleet._common.rel_id: forward-slash on every
        host OS.  Deliberately NOT ``str(...relative_to(...))`` — that renders with
        os.sep and would make these assertions agree with a Windows-only wire shape.
        """
        return absolute.relative_to(self.root).as_posix()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fleet_repo(tmp_path) -> FleetRepo:
    """Provide a temporary git repository pre-configured for fleet op tests.

    The repo has:
    - git config user.email / user.name / commit.gpgsign=false set
    - Standard artifact directory skeleton committed:
        docs/plans/    state/handoffs/    state/bug-backlog/
        archive/specs/ archive/handoffs/  archive/bug-backlog/
    - An initial commit so git read-tree HEAD succeeds from the first call

    Usage in tests::

        def test_something(fleet_repo):
            plan_path = fleet_repo.seed_plan("2026-01-01-my-plan.md", "implemented")
            # ... call handler ...
            assert not fleet_repo.path_exists("docs/plans/2026-01-01-my-plan.md")
            assert fleet_repo.git_status_clean()
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

    # Initialise the repo.
    _git("init", "-b", "main")
    _git("config", "user.email", "fleet-test@claude-klabauter.test")
    _git("config", "user.name", "Fleet Test")
    _git("config", "commit.gpgsign", "false")

    # Create standard directory skeleton with .gitkeep sentinels so the
    # directories are tracked and present for handlers that glob them.
    dirs = [
        "docs/plans",
        "state/handoffs",
        "state/bug-backlog",
        "archive/specs",
        "archive/handoffs",
        "archive/bug-backlog",
    ]
    for d in dirs:
        (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / d / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return FleetRepo(repo_root)
