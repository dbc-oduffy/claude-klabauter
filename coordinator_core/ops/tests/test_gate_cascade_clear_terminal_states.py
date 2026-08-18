"""
coordinator_core.ops.tests.test_gate_cascade_clear_terminal_states

Purpose: gate-cascade-clear (`handoff_transition._gate_cascade_clear`) act-time
re-verification coverage for the DR-084-widened terminal deployment_state set.
Before this fix, the loop treated `shipped` as the ONLY deployment_state a
blocker could hold before its gate edge could clear -- a blocker terminating
as `closed` or `continued` (both terminal per
`lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT`) permanently wedged its
dependent at `awaiting_gate` with no verb able to advance it.

Spec backlink: dispatch brief "gate-cascade-clear DR-084 terminal states"
(2026-08-13), `coordinator_core/lifecycle_constants.py:42`.

Negative-spec: `closed` clears ONLY when `closed_reason == "displaced"` --
`cancelled` and `stale` do NOT clear (the dependent's premise evaporated;
needs human adjudication via closing/re-scoping the dependent, not
cascade-clear). `abandoned` never clears. `continued` clears only when its
`continued_into` chain resolves (iteratively, cycle-guarded, depth-capped)
to a clearing state.

This module-local fixture spawns real git explicitly (one `git init` per
test, via an explicit import -- never an ambient conftest fixture) because
`locked_rmw` resolves the git common dir via a real `git rev-parse` call.
Mirrors the governed model at
`coordinator_core/ops/ceremony/tests/fixtures/real_git.py` -- the file kept
during the 2026-08-07 spawn-heavy test excision as "the one governed
real-git fixture, kept as the model the restoration should copy" (see
`state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md`). The prior
ambient `handoff_repo` conftest fixture and its ~60-test consumer file were
deleted in that excision; this file is a small, explicit, non-ambient
replacement scoped to ONLY the terminal-state gap this dispatch fixes.
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
from coordinator_core.frontmatter.primitives import split_frontmatter, read_fm_field
from coordinator_core.win_portability import no_console_creationflags

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_NO_CONSOLE = no_console_creationflags()


# ---------------------------------------------------------------------------
# Minimal real-git repo builder (explicit, module-local -- not an ambient
# conftest fixture; see module docstring)
# ---------------------------------------------------------------------------


class _GateCascadeRepo:
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

    def seed_handoff(self, name: str, fm_lines: list, *, root: Optional[str] = None) -> Path:
        """Write + commit a handoffs record with the exact given frontmatter lines.

        `root` overrides the default state/handoffs/ location (e.g.
        "archive/handoffs/2026-07" for an archived successor).
        """
        rel_dir = root or "state/handoffs"
        path = self.root / rel_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_block = "\n".join(fm_lines)
        path.write_text(f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def read_text(self, name: str) -> str:
        return (self.root / "state" / "handoffs" / name).read_text(encoding="utf-8")

    def abs_path(self, name: str) -> str:
        return str(self.root / "state" / "handoffs" / name)


@pytest.fixture
def gcc_repo(tmp_path) -> _GateCascadeRepo:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True, check=True, **_NO_CONSOLE
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "gcc-terminal-test@claude-claude-klabauter.test")
    _git("config", "user.name", "Gate Cascade Terminal Test")
    _git("config", "commit.gpgsign", "false")

    (repo_root / "state" / "handoffs").mkdir(parents=True)
    (repo_root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return _GateCascadeRepo(repo_root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _gate_cascade_clear_params(handoff_path: str, blocker_ids: list, blocker_shas: list) -> dict:
    return {
        "verb": "gate-cascade-clear",
        "handoff_path": handoff_path,
        "blocker_ids": blocker_ids,
        "blocker_shas": blocker_shas,
    }


def _roadmap_extra(stub_id: str, blocked_by_yaml: str, gate_dependency: str = "") -> list:
    """Frontmatter lines for a spinoff-roadmap dependent handoff.

    kind: spinoff-roadmap requires roadmap_id/stub_id/wave/blocks/blocked_by
    (schema _cf_spinoff_roadmap_requires_graph).
    """
    lines = [
        'title: "Test Dependent"',
        "created: 2026-01-01",
        "branch: work/test/2026-01-01",
        'predecessor: "none"',
        "status: open",
        "deployment_state: awaiting_gate",
        "kind: spinoff-roadmap",
        'roadmap_id: "rdm-gcc-terminal"',
        f'stub_id: "{stub_id}"',
        "wave: 1",
        "blocks: []",
        f"blocked_by: {blocked_by_yaml}",
    ]
    if gate_dependency:
        lines.append(f'gate_dependency: "{gate_dependency}"')
    return lines


def _blocker_lines(
    stub_id: str,
    deployment_state: str,
    *,
    closed_reason: Optional[str] = None,
    continued_into: Optional[str] = None,
) -> list:
    lines = [
        f'stub_id: "{stub_id}"',
        "status: open",
        f"deployment_state: {deployment_state}",
    ]
    if closed_reason is not None:
        lines.append(f"closed_reason: {closed_reason}")
    if continued_into is not None:
        lines.append(f'continued_into: "{continued_into}"')
    return lines


def _read_fm(path_str: str) -> str:
    content = open(path_str, encoding="utf-8").read()
    split = split_frontmatter(content)
    assert split is not None, f"no parseable frontmatter in {path_str}"
    return split.fm_text


# ---------------------------------------------------------------------------
# shipped clears (unchanged baseline)
# ---------------------------------------------------------------------------


def test_shipped_blocker_clears(gcc_repo):
    gcc_repo.seed_handoff("blocker-a.md", _blocker_lines("stub-a", "shipped"))
    gcc_repo.seed_handoff(
        "gcc-shipped.md", _roadmap_extra("gcc-shipped", "['stub-a']", "stub-a work")
    )
    abs_path = gcc_repo.abs_path("gcc-shipped.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-a"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"


# ---------------------------------------------------------------------------
# no_longer_blocked_by MOVE (AC1/AC2/AC3, bug-backlog 2026-08-14)
# ---------------------------------------------------------------------------


def test_flip_moves_cleared_id_into_no_longer_blocked_by(gcc_repo):
    """AC1: a full-drain clear MOVES the cleared id into
    no_longer_blocked_by rather than dropping it — the union of blocked_by
    and no_longer_blocked_by is invariant across the call."""
    gcc_repo.seed_handoff("blocker-move.md", _blocker_lines("stub-move", "shipped"))
    gcc_repo.seed_handoff(
        "gcc-move.md", _roadmap_extra("gcc-move", "['stub-move']", "stub-move work")
    )
    abs_path = gcc_repo.abs_path("gcc-move.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-move"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    assert yaml.safe_load(fm)["blocked_by"] == []
    assert yaml.safe_load(fm)["no_longer_blocked_by"] == ["stub-move"]


def test_narrow_moves_only_the_shipped_blocker_and_stays_awaiting_gate(gcc_repo):
    """AC2: narrowing a two-blocker gate where only one blocker (tc-1) has
    shipped must move ONLY tc-1 into no_longer_blocked_by, leave tc-5 in
    blocked_by untouched, and must NOT flip deployment_state — clearing a
    shipped blocker is the ruling, clearing an unshipped one was never
    asked for."""
    gcc_repo.seed_handoff("blocker-tc-1.md", _blocker_lines("tc-1", "shipped"))
    gcc_repo.seed_handoff("blocker-tc-5.md", _blocker_lines("tc-5", "in_flight"))
    gcc_repo.seed_handoff(
        "gcc-narrow.md",
        _roadmap_extra("gcc-narrow", "['tc-1', 'tc-5']", "tc-1 work, tc-5 work"),
    )
    abs_path = gcc_repo.abs_path("gcc-narrow.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["tc-1"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "awaiting_gate"
    parsed = yaml.safe_load(fm)
    assert parsed["blocked_by"] == ["tc-5"]
    assert parsed["no_longer_blocked_by"] == ["tc-1"]


def test_replay_against_prepopulated_no_longer_blocked_by_appends_no_duplicate(gcc_repo):
    """AC3: the move and the shrink occur in one locked_rmw mutate() closure,
    and a replay against a handoff whose no_longer_blocked_by already
    carries the id being cleared (e.g. a prior partial history) appends no
    duplicate."""
    gcc_repo.seed_handoff("blocker-replay.md", _blocker_lines("stub-replay", "shipped"))
    gcc_repo.seed_handoff(
        "gcc-replay.md",
        _roadmap_extra("gcc-replay", "['stub-replay']", "stub-replay work")
        + ["no_longer_blocked_by: ['stub-replay']"],
    )
    abs_path = gcc_repo.abs_path("gcc-replay.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-replay"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"
    parsed = yaml.safe_load(fm)
    assert parsed["blocked_by"] == []
    assert parsed["no_longer_blocked_by"] == ["stub-replay"]


# ---------------------------------------------------------------------------
# closed + closed_reason
# ---------------------------------------------------------------------------


def test_closed_displaced_refuses(gcc_repo):
    """Ratified rule (1)/(2) in `reconcile.gate_eval` (see the TERMINAL-
    PREDICATE DERIVATION block and `_NON_SHIPPED_TERMINAL_STATES`): a
    `closed` blocker means the work was deliberately stopped, never shipped
    -- `closed_reason` does NOT distinguish a clearable case, `displaced`
    included. `_blocker_clears_gate` is the act-time counterpart of that
    same rule and must refuse identically."""
    gcc_repo.seed_handoff(
        "blocker-displaced.md",
        _blocker_lines("stub-disp", "closed", closed_reason="displaced"),
    )
    gcc_repo.seed_handoff(
        "gcc-displaced.md", _roadmap_extra("gcc-displaced", "['stub-disp']", "stub-disp work")
    )
    abs_path = gcc_repo.abs_path("gcc-displaced.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-disp"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


@pytest.mark.parametrize("closed_reason", ["cancelled", "displaced", "stale"])
def test_closed_refuses_for_every_closed_reason(gcc_repo, closed_reason):
    """Pin: `closed` never clears a gate edge regardless of `closed_reason`
    -- `reconcile.gate_eval` ratified rule (1)/(2) treats the entire
    `closed` terminal state as non-discharge (only `shipped` is evidence the
    blocked-on work landed); the adjudication path is `gate-recheck` with
    `cleared: true`, not a `closed_reason` carve-out. Pinned by test across
    all three known enum values so a future `closed_reason` addition, or a
    re-widening attempt on any one of these, cannot silently reopen a
    clearing branch."""
    stub_id = f"stub-cr-{closed_reason}"
    gcc_repo.seed_handoff(
        f"blocker-cr-{closed_reason}.md",
        _blocker_lines(stub_id, "closed", closed_reason=closed_reason),
    )
    gcc_repo.seed_handoff(
        f"gcc-cr-{closed_reason}.md",
        _roadmap_extra(f"gcc-cr-{closed_reason}", f"['{stub_id}']", f"{stub_id} work"),
    )
    abs_path = gcc_repo.abs_path(f"gcc-cr-{closed_reason}.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, [stub_id], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_closed_cancelled_refuses(gcc_repo):
    gcc_repo.seed_handoff(
        "blocker-cancelled.md",
        _blocker_lines("stub-canc", "closed", closed_reason="cancelled"),
    )
    gcc_repo.seed_handoff(
        "gcc-cancelled.md", _roadmap_extra("gcc-cancelled", "['stub-canc']", "stub-canc work")
    )
    abs_path = gcc_repo.abs_path("gcc-cancelled.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-canc"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_closed_stale_refuses(gcc_repo):
    gcc_repo.seed_handoff(
        "blocker-stale.md",
        _blocker_lines("stub-stale", "closed", closed_reason="stale"),
    )
    gcc_repo.seed_handoff(
        "gcc-stale.md", _roadmap_extra("gcc-stale", "['stub-stale']", "stub-stale work")
    )
    abs_path = gcc_repo.abs_path("gcc-stale.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-stale"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_closed_no_reason_refuses(gcc_repo):
    gcc_repo.seed_handoff(
        "blocker-noreason.md",
        _blocker_lines("stub-noreason", "closed"),
    )
    gcc_repo.seed_handoff(
        "gcc-noreason.md",
        _roadmap_extra("gcc-noreason", "['stub-noreason']", "stub-noreason work"),
    )
    abs_path = gcc_repo.abs_path("gcc-noreason.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-noreason"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# abandoned
# ---------------------------------------------------------------------------


def test_abandoned_refuses(gcc_repo):
    gcc_repo.seed_handoff("blocker-abandoned.md", _blocker_lines("stub-aband", "abandoned"))
    gcc_repo.seed_handoff(
        "gcc-abandoned.md", _roadmap_extra("gcc-abandoned", "['stub-aband']", "stub-aband work")
    )
    abs_path = gcc_repo.abs_path("gcc-abandoned.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-aband"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# continued -> successor chain
# ---------------------------------------------------------------------------


def test_continued_to_shipped_successor_clears(gcc_repo):
    gcc_repo.seed_handoff("successor-shipped.md", _blocker_lines("stub-succ-a", "shipped"))
    gcc_repo.seed_handoff(
        "blocker-continued-a.md",
        _blocker_lines("stub-cont-a", "continued", continued_into="stub-succ-a"),
    )
    gcc_repo.seed_handoff(
        "gcc-continued-shipped.md",
        _roadmap_extra("gcc-continued-shipped", "['stub-cont-a']", "stub-cont-a work"),
    )
    abs_path = gcc_repo.abs_path("gcc-continued-shipped.md")

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-cont-a"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    fm = _read_fm(abs_path)
    assert read_fm_field(fm, "deployment_state") == "ready_to_fire"


def test_continued_to_closed_cancelled_successor_refuses(gcc_repo):
    gcc_repo.seed_handoff(
        "successor-cancelled.md",
        _blocker_lines("stub-succ-b", "closed", closed_reason="cancelled"),
    )
    gcc_repo.seed_handoff(
        "blocker-continued-b.md",
        _blocker_lines("stub-cont-b", "continued", continued_into="stub-succ-b"),
    )
    gcc_repo.seed_handoff(
        "gcc-continued-cancelled.md",
        _roadmap_extra("gcc-continued-cancelled", "['stub-cont-b']", "stub-cont-b work"),
    )
    abs_path = gcc_repo.abs_path("gcc-continued-cancelled.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-cont-b"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_continued_blank_continued_into_refuses(gcc_repo):
    gcc_repo.seed_handoff(
        "blocker-continued-blank.md",
        [
            'stub_id: "stub-cont-blank"',
            "status: open",
            "deployment_state: continued",
        ],
    )
    gcc_repo.seed_handoff(
        "gcc-continued-blank.md",
        _roadmap_extra("gcc-continued-blank", "['stub-cont-blank']", "stub-cont-blank work"),
    )
    abs_path = gcc_repo.abs_path("gcc-continued-blank.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-cont-blank"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


def test_continued_chain_cycle_refuses(gcc_repo):
    gcc_repo.seed_handoff(
        "blocker-cycle-1.md",
        _blocker_lines("stub-cycle-1", "continued", continued_into="stub-cycle-2"),
    )
    gcc_repo.seed_handoff(
        "blocker-cycle-2.md",
        _blocker_lines("stub-cycle-2", "continued", continued_into="stub-cycle-1"),
    )
    gcc_repo.seed_handoff(
        "gcc-cycle.md",
        _roadmap_extra("gcc-cycle", "['stub-cycle-1']", "stub-cycle-1 work"),
    )
    abs_path = gcc_repo.abs_path("gcc-cycle.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(abs_path, ["stub-cycle-1"], ["a" * 40]),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# multi-blocker: one refusal leaves the file byte-identical (no partial write)
# ---------------------------------------------------------------------------


def test_multi_blocker_one_refuses_leaves_file_byte_identical(gcc_repo):
    gcc_repo.seed_handoff("blocker-multi-shipped.md", _blocker_lines("stub-multi-a", "shipped"))
    gcc_repo.seed_handoff(
        "blocker-multi-abandoned.md", _blocker_lines("stub-multi-b", "abandoned")
    )
    gcc_repo.seed_handoff(
        "gcc-multi.md",
        _roadmap_extra(
            "gcc-multi", "['stub-multi-a', 'stub-multi-b']", "stub-multi-a work, stub-multi-b work"
        ),
    )
    abs_path = gcc_repo.abs_path("gcc-multi.md")
    original = open(abs_path, encoding="utf-8").read()

    result = _run(_handler(
        _gate_cascade_clear_params(
            abs_path, ["stub-multi-a", "stub-multi-b"], ["a" * 40, "b" * 40]
        ),
        repo_root=gcc_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert open(abs_path, encoding="utf-8").read() == original, (
        "partial-cascade writes must never apply piecemeal -- one refusing "
        "blocker aborts the whole call with no write"
    )
