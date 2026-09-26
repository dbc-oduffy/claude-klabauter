"""
coordinator_core.ops.tests.test_gate_recheck_cleared_readiness

Purpose: pin item 9 (DoE inbox-blitz thread, plan row C12). `_gate_recheck`'s
`cleared` branch flips `deployment_state: awaiting_gate -> ready_to_fire` but,
before this fix, left `pickup_ready` exactly as authored -- a record parked
with `pickup_ready: false` while its lone blocker was outstanding stayed
`pickup_ready: false` forever after the gate cleared, even though nothing
else on the record still names a reason to withhold it. `_gate_recheck` now
calls `_apply_derived_readiness` on the post-mutation frontmatter, on the
`cleared` branch only, so `pickup_ready` follows the cleared state instead of
being preserved untouched.

Negative-spec: a bare (non-cleared) recheck call makes no lifecycle claim and
must NOT have pickup_ready re-derived -- it stays exactly as authored.

Real-git fixture is explicit and module-local (never an ambient conftest
fixture) because `locked_rmw` resolves the git common dir via a real
`git rev-parse`. Mirrors the governed model this repo kept at
`coordinator_core/ops/ceremony/tests/fixtures/real_git.py` and this module's
own sibling `test_gate_recheck_retires_blocked_by.py`.
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
    _git("config", "user.email", "gate-recheck-readiness-test@claude-claude-klabauter.test")
    _git("config", "user.name", "Gate Recheck Readiness Test")
    _git("config", "commit.gpgsign", "false")

    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return _Repo(root)


def _parked_dependent_lines() -> list:
    # blocked_by is empty -- the blocker already shipped and was retired by
    # an earlier gate-cascade-clear call, leaving deployment_state stuck at
    # awaiting_gate with a stale pickup_ready:false the author never revisited.
    return [
        'title: "Test Dependent"',
        "created: 2026-01-01",
        "branch: work/test/2026-01-01",
        'predecessor: "none"',
        "status: open",
        "deployment_state: awaiting_gate",
        'gate_notes: "blocker shipped; awaiting recheck"',
        "pickup_ready: false",
        "kind: spinoff-roadmap",
        'roadmap_id: "rdm-recheck"',
        'stub_id: "dependent-01"',
        "wave: 1",
        "blocks: []",
        "blocked_by: []",
    ]


def _run(coro):
    return asyncio.run(coro)


def test_cleared_recheck_re_derives_pickup_ready(repo: _Repo) -> None:
    """The cleared branch must not leave a stale pickup_ready:false standing."""
    repo.seed("dependent.md", _parked_dependent_lines())

    result = _run(
        _handler(
            {
                "verb": "gate-recheck",
                "handoff_path": repo.abs_path("dependent.md"),
                "at": "2026-09-26",
                "cleared": True,
            },
            repo.common_dir,
        )
    )
    assert result["exit_code"] == 0, result.get("error")
    assert result["applied"] is True

    fm = repo.frontmatter("dependent.md")
    assert fm["deployment_state"] == "ready_to_fire"
    # THE FIX: pickup_ready followed the cleared state instead of being
    # preserved untouched at its stale authored value.
    assert fm["pickup_ready"] is True


def test_bare_recheck_does_not_re_derive_pickup_ready(repo: _Repo) -> None:
    """Negative-spec: a bare (non-cleared) recheck makes no lifecycle claim
    and must leave pickup_ready exactly as authored."""
    repo.seed("dependent.md", _parked_dependent_lines())

    result = _run(
        _handler(
            {
                "verb": "gate-recheck",
                "handoff_path": repo.abs_path("dependent.md"),
                "at": "2026-09-26",
            },
            repo.common_dir,
        )
    )
    assert result["exit_code"] == 0, result.get("error")

    fm = repo.frontmatter("dependent.md")
    assert fm["deployment_state"] == "awaiting_gate"
    assert fm["pickup_ready"] is False
