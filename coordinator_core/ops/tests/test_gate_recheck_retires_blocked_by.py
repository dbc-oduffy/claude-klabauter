"""
coordinator_core.ops.tests.test_gate_recheck_retires_blocked_by

Purpose: pin the adjudication door open. `_gate_cascade_clear` refuses any
blocker whose id does not resolve to exactly one live record (duplicate
`stub_id` across a continuation chain yields `<ambiguous-duplicate-id>`), and
its refusal directs the operator to "Adjudicate the dependent instead:
gate-recheck with cleared: true". That door did not open: `_gate_recheck`'s
`cleared` path retired `gate_dependency` and `gate_evidence` but left
`blocked_by` populated, so its own post-mutation `_validate_fm` refused the
write ("blocked_by: must be empty or fully cleared when
deployment_state=ready_to_fire"). Each verb pointed at the other and no verb
could discharge the gate -- a dependent wedged at `awaiting_gate` permanently.

Discovered picking up state/handoffs/2026-08-25_roadmap-archival-sweeps-03.md,
whose blocker stub `ceremony-restore-01` spans five live continuation records.

Negative-spec: the retirement MOVES entries into `no_longer_blocked_by`
(handoff.schema.json union invariant), never drops them -- the defect
bug-backlog 2026-08-14-gate-cascade-clear-drops-blocked-by-entries-instead-of-
moving.yaml records against the sibling verb. It writes no `gate_cleared_by`:
that field is SHA provenance for "which commit discharged the gate", and an
adjudicated clear has no commit to name.

Real-git fixture is explicit and module-local (never an ambient conftest
fixture) because `locked_rmw` resolves the git common dir via a real
`git rev-parse`. Mirrors the governed model this repo kept at
`coordinator_core/ops/ceremony/tests/fixtures/real_git.py`.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

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

    def _git(self, *args: str) -> None:
        subprocess.run(
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

    def seed(self, name: str, fm_lines: list) -> Path:
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
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, check=True, **_NO_CONSOLE
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "gate-recheck-test@claude-claude-klabauter.test")
    _git("config", "user.name", "Gate Recheck Test")
    _git("config", "commit.gpgsign", "false")

    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return _Repo(root)


def _dependent_lines(blocked_by_yaml: str, *, no_longer: str = "") -> list:
    lines = [
        'title: "Test Dependent"',
        "created: 2026-01-01",
        "branch: work/test/2026-01-01",
        'predecessor: "none"',
        "status: open",
        "deployment_state: awaiting_gate",
        "kind: spinoff-roadmap",
        'roadmap_id: "rdm-recheck"',
        'stub_id: "dependent-01"',
        "wave: 1",
        "blocks: []",
        f"blocked_by: {blocked_by_yaml}",
    ]
    if no_longer:
        lines.append(f"no_longer_blocked_by: {no_longer}")
    return lines


def _blocker_lines(stub_id: str, title: str) -> list:
    return [
        f'title: "{title}"',
        "created: 2026-01-01",
        "branch: work/test/2026-01-01",
        'predecessor: "none"',
        "status: open",
        "deployment_state: in_flight",
        "kind: spinoff-roadmap",
        'roadmap_id: "rdm-recheck"',
        f'stub_id: "{stub_id}"',
        "wave: 1",
        'blocks: ["dependent-01"]',
        "blocked_by: []",
    ]


def _run(coro):
    return asyncio.run(coro)


def test_ambiguous_blocker_deadlock_is_broken_by_adjudication(repo: _Repo) -> None:
    """Both halves in one test -- the deadlock only exists as a pair.

    Two live records share stub_id `blk-dup`, so the blocker id is ambiguous.
    gate-cascade-clear must refuse (it cannot know which record to believe),
    and the door it names must then actually open.
    """
    repo.seed("blocker-a.md", _blocker_lines("blk-dup", "Blocker A"))
    repo.seed("blocker-b.md", _blocker_lines("blk-dup", "Blocker B"))
    repo.seed("dependent.md", _dependent_lines('["blk-dup"]'))

    common_dir = repo.common_dir
    dependent = repo.abs_path("dependent.md")

    cascade = _run(
        _handler(
            {
                "verb": "gate-cascade-clear",
                "handoff_path": dependent,
                "blocker_ids": ["blk-dup"],
                "blocker_shas": ["abc1234"],
            },
            common_dir,
        )
    )
    assert cascade["exit_code"] == 1
    assert "<ambiguous-duplicate-id>" in cascade["error"]
    # The refusal names a specific remedy; the rest of this test is that remedy.
    assert "gate-recheck with cleared: true" in cascade["error"]

    recheck = _run(
        _handler(
            {
                "verb": "gate-recheck",
                "handoff_path": dependent,
                "at": "2026-08-27",
                "cleared": True,
            },
            common_dir,
        )
    )
    assert recheck["exit_code"] == 0, recheck.get("error")
    assert recheck["applied"] is True

    fm = repo.frontmatter("dependent.md")
    assert fm["deployment_state"] == "ready_to_fire"
    assert fm["blocked_by"] == []
    assert fm["no_longer_blocked_by"] == ["blk-dup"]
    # yaml.safe_load promotes a bare ISO date to datetime.date; str() it back.
    assert str(fm["last_gate_recheck"]) == "2026-08-27"
    # SHA provenance belongs to gate-cascade-clear; an adjudicated clear has no
    # commit to name and must not invent one.
    assert "gate_cleared_by" not in fm


def test_retirement_moves_and_dedupes_rather_than_dropping(repo: _Repo) -> None:
    """Union invariant across the two arrays, against a pre-populated target."""
    repo.seed("blocker-a.md", _blocker_lines("blk-dup", "Blocker A"))
    repo.seed("blocker-b.md", _blocker_lines("blk-dup", "Blocker B"))
    repo.seed(
        "dependent.md",
        _dependent_lines('["blk-dup", "blk-other"]', no_longer='["blk-earlier", "blk-other"]'),
    )

    result = _run(
        _handler(
            {
                "verb": "gate-recheck",
                "handoff_path": repo.abs_path("dependent.md"),
                "at": "2026-08-27",
                "cleared": True,
            },
            repo.common_dir,
        )
    )
    assert result["exit_code"] == 0, result.get("error")

    fm = repo.frontmatter("dependent.md")
    assert fm["blocked_by"] == []
    # blk-other was already present -- appended once, not twice; nothing dropped.
    assert fm["no_longer_blocked_by"] == ["blk-earlier", "blk-other", "blk-dup"]


def test_bare_recheck_leaves_blocked_by_untouched(repo: _Repo) -> None:
    """Negative-spec: retirement is the `cleared` path's alone.

    A bare re-check makes no lifecycle claim, so it must re-stamp
    last_gate_recheck and change nothing structural.
    """
    repo.seed("blocker-a.md", _blocker_lines("blk-solo", "Blocker A"))
    repo.seed("dependent.md", _dependent_lines('["blk-solo"]'))

    result = _run(
        _handler(
            {
                "verb": "gate-recheck",
                "handoff_path": repo.abs_path("dependent.md"),
                "at": "2026-08-27",
            },
            repo.common_dir,
        )
    )
    assert result["exit_code"] == 0, result.get("error")

    fm = repo.frontmatter("dependent.md")
    assert fm["deployment_state"] == "awaiting_gate"
    assert fm["blocked_by"] == ["blk-solo"]
    assert "no_longer_blocked_by" not in fm
