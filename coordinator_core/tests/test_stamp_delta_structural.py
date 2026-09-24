"""
coordinator_core.tests.test_stamp_delta_structural

R6: `pickup_brief._classify_stamp_delta` compares parsed frontmatter and
spine sets at one spawn on an ANCHORED artifact (a parseable frontmatter
`scope`, `prime_exit_criterion`, or ` ```yaml plan-tasks` spine), rather
than diffing lines. An UNANCHORED artifact keeps the original line-shape
`git diff` path unchanged.

Spec: docs/plans/2026-09-22-spawn-budget-and-census.md (P153-C5).

Run: cd claude-klabauter && python3 -m pytest coordinator_core/tests/test_stamp_delta_structural.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pb
from coordinator_core.frontmatter.primitives import canonical_body_sha
from coordinator_core.telemetry import spawn_counter
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


_ANCHORED_FM = (
    'title: "Anchored Test Plan"\n'
    "created: 2026-01-01\n"
    "execution_authorized_by: \"PM (Test)\"\n"
    "execution_authorized_at: 2026-01-01\n"
    "execution_authorized_sha: PENDING\n"
    "scope:\n"
    "  - coordinator_core/a.py\n"
    "  - coordinator_core/b.py\n"
    "prime_exit_criterion:\n"
    "  statement: \"Foo holds\"\n"
    "  falsifier:\n"
    "    check: \"run x\"\n"
    "    expected: \"y\"\n"
)

_ANCHORED_BODY = (
    "# Plan\n\n"
    "## Tasks\n\n"
    "```yaml plan-tasks\n"
    "- id: C1\n"
    '  title: "first"\n'
    "  writes:\n"
    "    - coordinator_core/a.py\n"
    "- id: C2\n"
    '  title: "second"\n'
    "  writes:\n"
    "    - coordinator_core/b.py\n"
    "```\n"
)


def _write_anchored_plan(repo: Path, name: str, fm: str = _ANCHORED_FM, body: str = _ANCHORED_BODY) -> Path:
    plan_path = repo / "docs" / "plans" / name
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    template = f"---\n{fm}---\n\n{body}"
    stamped = canonical_body_sha(template)
    plan_path.write_text(template.replace("PENDING", stamped), encoding="utf-8")
    _git(repo, "add", str(plan_path.relative_to(repo)))
    _git(repo, "commit", "-m", f"stamp {name}")
    return plan_path


def _commit(repo: Path, path: Path, message: str) -> None:
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", message)


class TestAnchoredStructuralComparison:
    """Case (a): the R6 measured instance, reproduced as a fixture — body
    gains disposition records, correction notes and strikethroughs; the
    spine is reordered; the sets are unchanged. Expect `bookkeeping`."""

    def test_disposition_notes_and_spine_reorder_are_bookkeeping(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path = _write_anchored_plan(repo, "p-a.md")
        stamp_commit = _head_sha(repo)

        reordered_body = (
            "# Plan\n\n"
            "~~stale note~~\n"
            "Correction: see below.\n\n"
            "## Tasks\n\n"
            "```yaml plan-tasks\n"
            "- id: C2\n"
            '  title: "second"\n'
            "  writes:\n"
            "    - coordinator_core/b.py\n"
            "  disposition: coded\n"
            "  disposition_ref: abc1234\n"
            '  disposition_detail: "shipped in abc1234"\n'
            "- id: C1\n"
            '  title: "first"\n'
            "  writes:\n"
            "    - coordinator_core/a.py\n"
            "  disposition: coded\n"
            "  disposition_ref: def5678\n"
            '  disposition_detail: "shipped in def5678"\n'
            "```\n"
        )
        # Keep the original stamped frontmatter untouched; only the body changes.
        head_text = plan_path.read_text(encoding="utf-8")
        split_idx = head_text.index("---\n", 4)
        fm_text = head_text[: split_idx + 4]
        new_text = fm_text + "\n" + reordered_body
        plan_path.write_text(new_text, encoding="utf-8")
        _commit(repo, plan_path, "add disposition notes, reorder spine")

        head_text = plan_path.read_text(encoding="utf-8")
        result = pb._classify_stamp_delta(
            repo, stamp_commit, plan_path.relative_to(repo).as_posix(), head_text
        )
        assert result == "bookkeeping"

    def test_reorder_alone_reports_no_phantom_removal(self, tmp_path):
        """Case (d): a reordered block must not report a phantom removal."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path = _write_anchored_plan(repo, "p-d.md")
        stamp_commit = _head_sha(repo)

        head_text = plan_path.read_text(encoding="utf-8")
        split_idx = head_text.index("---\n", 4)
        fm_text = head_text[: split_idx + 4]
        reordered_body = (
            "# Plan\n\n"
            "## Tasks\n\n"
            "```yaml plan-tasks\n"
            "- id: C2\n"
            '  title: "second"\n'
            "  writes:\n"
            "    - coordinator_core/b.py\n"
            "- id: C1\n"
            '  title: "first"\n'
            "  writes:\n"
            "    - coordinator_core/a.py\n"
            "```\n"
        )
        plan_path.write_text(fm_text + "\n" + reordered_body, encoding="utf-8")
        _commit(repo, plan_path, "reorder spine only")

        head_text = plan_path.read_text(encoding="utf-8")
        result = pb._classify_stamp_delta(
            repo, stamp_commit, plan_path.relative_to(repo).as_posix(), head_text
        )
        assert result == "bookkeeping"

    def test_acceptance_criterion_change_is_substantive(self, tmp_path):
        """Case (b): the acceptance criterion changes with nothing else
        describable. Expect `substantive`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path = _write_anchored_plan(repo, "p-b.md")
        stamp_commit = _head_sha(repo)

        head_text = plan_path.read_text(encoding="utf-8")
        new_fm = _ANCHORED_FM.replace('"Foo holds"', '"Bar holds instead"')
        new_text = f"---\n{new_fm}---\n\n" + _ANCHORED_BODY
        # Preserve the original stamped sha line (do not re-stamp).
        orig_stamp_line = [ln for ln in head_text.splitlines() if ln.startswith("execution_authorized_sha:")][0]
        new_text = "\n".join(
            orig_stamp_line if ln.startswith("execution_authorized_sha:") else ln
            for ln in new_text.splitlines()
        ) + "\n"
        plan_path.write_text(new_text, encoding="utf-8")
        _commit(repo, plan_path, "change acceptance criterion statement")

        head_text = plan_path.read_text(encoding="utf-8")
        result = pb._classify_stamp_delta(
            repo, stamp_commit, plan_path.relative_to(repo).as_posix(), head_text
        )
        assert result == "substantive"

    def test_scope_entry_added_is_substantive(self, tmp_path):
        """Case (c): one scope entry is added. Expect `substantive`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path = _write_anchored_plan(repo, "p-c.md")
        stamp_commit = _head_sha(repo)

        head_text = plan_path.read_text(encoding="utf-8")
        orig_stamp_line = [ln for ln in head_text.splitlines() if ln.startswith("execution_authorized_sha:")][0]
        new_fm = _ANCHORED_FM.replace(
            "  - coordinator_core/b.py\n",
            "  - coordinator_core/b.py\n  - coordinator_core/c.py\n",
        )
        new_text = f"---\n{new_fm}---\n\n" + _ANCHORED_BODY
        new_text = "\n".join(
            orig_stamp_line if ln.startswith("execution_authorized_sha:") else ln
            for ln in new_text.splitlines()
        ) + "\n"
        plan_path.write_text(new_text, encoding="utf-8")
        _commit(repo, plan_path, "add scope entry")

        head_text = plan_path.read_text(encoding="utf-8")
        result = pb._classify_stamp_delta(
            repo, stamp_commit, plan_path.relative_to(repo).as_posix(), head_text
        )
        assert result == "substantive"


class TestSpawnCountIsExactlyOne:
    """Case (e): a `spawn_counter` delta of exactly 1 on both the anchored
    and the unanchored path."""

    def test_anchored_path_spawns_exactly_once(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path = _write_anchored_plan(repo, "p-e-anchored.md")
        stamp_commit = _head_sha(repo)

        head_text = plan_path.read_text(encoding="utf-8")
        split_idx = head_text.index("---\n", 4)
        fm_text = head_text[: split_idx + 4]
        plan_path.write_text(fm_text + "\n" + "# Plan\n\n**Status:** shipped\n\n"
                              + _ANCHORED_BODY[_ANCHORED_BODY.index("## Tasks"):], encoding="utf-8")
        _commit(repo, plan_path, "bookkeeping-only body edit")

        head_text = plan_path.read_text(encoding="utf-8")
        before = spawn_counter.spawn_count()
        pb._classify_stamp_delta(
            repo, stamp_commit, plan_path.relative_to(repo).as_posix(), head_text
        )
        delta = spawn_counter.spawn_count() - before
        assert delta == 1

    def test_unanchored_path_spawns_exactly_once(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path = repo / "docs" / "plans" / "p-e-unanchored.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Unanchored Test Plan"\n'
            "created: 2026-01-01\n"
            "execution_authorized_by: \"PM (Test)\"\n"
            "execution_authorized_at: 2026-01-01\n"
            "execution_authorized_sha: PENDING\n"
        )
        body = "# Plan\n\nBody.\n"
        template = f"---\n{fm}---\n\n{body}"
        stamped = canonical_body_sha(template)
        plan_path.write_text(template.replace("PENDING", stamped), encoding="utf-8")
        _git(repo, "add", str(plan_path.relative_to(repo)))
        _git(repo, "commit", "-m", "stamp unanchored plan")
        stamp_commit = _head_sha(repo)

        plan_path.write_text(
            plan_path.read_text(encoding="utf-8") + "**Status:** shipped\n", encoding="utf-8"
        )
        _commit(repo, plan_path, "bookkeeping-only body edit")

        head_text = plan_path.read_text(encoding="utf-8")
        assert not pb._has_structural_anchor(head_text)
        before = spawn_counter.spawn_count()
        pb._classify_stamp_delta(
            repo, stamp_commit, plan_path.relative_to(repo).as_posix(), head_text
        )
        delta = spawn_counter.spawn_count() - before
        assert delta == 1
