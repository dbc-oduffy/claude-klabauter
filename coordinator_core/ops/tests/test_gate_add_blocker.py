"""
coordinator_core.ops.tests.test_gate_add_blocker

Purpose: `handoff_transition._gate_add_blocker`, the inverse of
gate-cascade-clear. Adding a blocker was the only leg of the gate lifecycle with
no op behind it, so it was done by hand — and by hand it costs three edits for
one semantic change, each refused by a cross-field rule the PREVIOUS edit's
remedy created (example-retrieval-repo, 2026-09-11). The property under test is that the
co-required set lands together or not at all.

This module-local fixture spawns real git explicitly (one `git init` per test,
never an ambient conftest fixture) because `locked_rmw` resolves the git common
dir via a real `git rev-parse` call. Mirrors
test_gate_cascade_clear_terminal_states.py, this verb's own sibling.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_gate_add_blocker.py -q
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

import pytest
import yaml

import coordinator_core.ops.handoff_transition  # noqa: F401 -- fires @register_op
from coordinator_core.ops.handoff_transition import _handler
from coordinator_core.win_portability import no_console_creationflags

# Declares a real external-process spawn (spawn ratchet Rule 2).
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_NO_CONSOLE = no_console_creationflags()


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, check=True, **_NO_CONSOLE
        )

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
            **_NO_CONSOLE,
        )
        return Path(result.stdout.decode().strip()).resolve()

    def seed_handoff(self, name: str, fm_lines: list) -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_block = "\n".join(fm_lines)
        path.write_text(f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def frontmatter(self, name: str) -> dict:
        text = (self.root / "state" / "handoffs" / name).read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---", 2)[1]) or {}

    def abs_path(self, name: str) -> str:
        return str(self.root / "state" / "handoffs" / name)


@pytest.fixture
def repo(tmp_path) -> _Repo:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True, check=True, **_NO_CONSOLE
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "gate-add-blocker-test@claude-claude-klabauter.test")
    _git("config", "user.name", "Gate Add Blocker Test")
    _git("config", "commit.gpgsign", "false")

    (repo_root / "state" / "handoffs").mkdir(parents=True)
    (repo_root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return _Repo(repo_root)


def _dependent_lines(
    stub_id: str,
    *,
    deployment_state: str = "ready_to_fire",
    pickup_ready: Optional[bool] = None,
    blocked_by: str = "[]",
) -> list:
    lines = [
        'title: "Test Dependent"',
        "created: 2026-01-01",
        "branch: work/test/2026-01-01",
        'predecessor: "none"',
        "status: open",
        f"deployment_state: {deployment_state}",
        "kind: spinoff-roadmap",
        'roadmap_id: "rdm-add-blocker"',
        f'stub_id: "{stub_id}"',
        "wave: 1",
        "blocks: []",
        f"blocked_by: {blocked_by}",
    ]
    if pickup_ready is not None:
        lines.append(f"pickup_ready: {'true' if pickup_ready else 'false'}")
    return lines


def _blocker_lines(stub_id: str) -> list:
    return [
        f'stub_id: "{stub_id}"',
        "status: open",
        "deployment_state: ready_to_fire",
    ]


def _params(handoff_path: str, blocker_ids: list, **extra) -> dict:
    params = {
        "verb": "gate-add-blocker",
        "handoff_path": handoff_path,
        "blocker_ids": blocker_ids,
    }
    params.update(extra)
    return params


def _run(repo: _Repo, params: dict) -> dict:
    return asyncio.run(_handler(params, repo_root=repo.common_dir))


# ---------------------------------------------------------------------------
# The co-required set lands together
# ---------------------------------------------------------------------------


def test_the_three_coupled_fields_are_written_in_one_pass(repo):
    """By hand this is three edits: blocked_by is refused on a ready_to_fire
    record, the awaiting_gate remedy then makes pickup_ready refused, and an
    author who stops after either leaves a record whose own fields disagree
    about whether it may fire."""
    repo.seed_handoff("blocker.md", _blocker_lines("blk-1"))
    repo.seed_handoff(
        "dependent.md", _dependent_lines("dep-1", pickup_ready=True)
    )

    result = _run(repo, _params(repo.abs_path("dependent.md"), ["blk-1"]))

    assert result["exit_code"] == 0, result
    fm = repo.frontmatter("dependent.md")
    assert fm["blocked_by"] == ["blk-1"]
    assert fm["deployment_state"] == "awaiting_gate"
    assert fm["pickup_ready"] is False


def test_gate_dependency_prose_is_appended_as_its_own_clause(repo):
    """Comma-joined, matching the shape gate-cascade-clear's reducer reads back
    — a second clause written any other way is one its inverse cannot remove."""
    repo.seed_handoff("blocker.md", _blocker_lines("blk-1"))
    repo.seed_handoff(
        "dependent.md",
        _dependent_lines("dep-1") + ['gate_dependency: "an existing gate"'],
    )

    result = _run(
        repo,
        _params(
            repo.abs_path("dependent.md"),
            ["blk-1"],
            gate_dependency="reddit api credential",
        ),
    )

    assert result["exit_code"] == 0, result
    assert repo.frontmatter("dependent.md")["gate_dependency"] == (
        "an existing gate, reddit api credential"
    )


def test_an_existing_blocker_is_not_duplicated(repo):
    repo.seed_handoff("blocker.md", _blocker_lines("blk-1"))
    repo.seed_handoff(
        "dependent.md",
        _dependent_lines("dep-1", deployment_state="awaiting_gate", blocked_by="[blk-1]"),
    )

    result = _run(repo, _params(repo.abs_path("dependent.md"), ["blk-1"]))

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    assert repo.frontmatter("dependent.md")["blocked_by"] == ["blk-1"]


# ---------------------------------------------------------------------------
# Refusals — nothing partial reaches disk
# ---------------------------------------------------------------------------


def test_a_blocker_naming_no_record_is_refused(repo):
    """An edge naming nothing can never be cleared, so the gate it writes is one
    nobody can discharge — the permanent `unresolved_blockers` row example-cockpit-repo
    reported on 2026-09-11, reached by an owner who had no other way to write a
    hold that held."""
    before = repo.seed_handoff("dependent.md", _dependent_lines("dep-1")).read_text(
        encoding="utf-8"
    )

    result = _run(repo, _params(repo.abs_path("dependent.md"), ["blk-nonexistent"]))

    assert result["exit_code"] == 1
    assert "resolves to no record" in result["error"]
    assert (repo.root / "state" / "handoffs" / "dependent.md").read_text(
        encoding="utf-8"
    ) == before


def test_a_terminal_baton_does_not_acquire_a_new_gate(repo):
    repo.seed_handoff("blocker.md", _blocker_lines("blk-1"))
    before = repo.seed_handoff(
        "dependent.md", _dependent_lines("dep-1", deployment_state="shipped")
    ).read_text(encoding="utf-8")

    result = _run(repo, _params(repo.abs_path("dependent.md"), ["blk-1"]))

    assert result["exit_code"] == 1
    assert "terminal" in result["error"]
    assert (repo.root / "state" / "handoffs" / "dependent.md").read_text(
        encoding="utf-8"
    ) == before


def test_an_empty_blocker_list_is_refused(repo):
    repo.seed_handoff("dependent.md", _dependent_lines("dep-1"))

    result = _run(repo, _params(repo.abs_path("dependent.md"), []))

    assert result["exit_code"] == 1
    assert "non-empty" in result["error"]


def test_the_verb_is_named_in_the_unknown_verb_message(repo):
    """A verb absent from its own op's error text is one nobody discovers."""
    repo.seed_handoff("dependent.md", _dependent_lines("dep-1"))

    result = _run(repo, {"verb": "nonsense", "handoff_path": repo.abs_path("dependent.md")})

    assert result["exit_code"] == 1
    assert "gate-add-blocker" in result["error"]
