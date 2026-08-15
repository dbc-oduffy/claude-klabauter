"""
coordinator_core.ops.tests.test_handoff_reconcile_disposition_discharge

docs/plans/2026-08-13-reconcile-surfaces-nobody-reads.md § P1 / C2 — AC2
proof that the D1 conservation discharge path actually works end to end,
not merely that the two field names exist on the schema (that is C1's own
scope, `handoff.schema.json` / `handoff-archived.schema.json`).

Before this file, nothing on disk exercised `reconcile_disposition` /
`reconcile_disposition_reason` as a WRITE — a live dry-run pass on this repo
returns 125 conservation violations, and that number alone cannot
distinguish "nobody has ever dispositioned anything" from "the field names
handoff_reconcile.py reads (`_DISPOSITION_FIELD`/`_DISPOSITION_REASON_FIELD`)
are misspelled or mismatched against what C1 declared". This file removes
that ambiguity via two REAL `_handler` passes over a throwaway git fixture:
a candidate surfaces in pass 1, and writing both disposition fields to it
between passes clears it in pass 2 (count drops by exactly one) — the
negative case (only one of the two fields populated) is asserted to still
violate, proving `_has_recorded_disposition`'s both-fields-required
contract is what actually gates the discharge, not a looser check.

Deliberately a NEW file, not a restoration of the deleted
`coordinator_core/ops/tests/test_handoff_reconcile_d1.py` (culled 2026-08-07,
commit 1d4e686a9; that restoration is tracked separately under
`2026-08-13-archive-family-coverage-restoration.md`, which names
`handoff_reconcile*` in its ~161-test restoration set — this file adds to,
never renumbers or restructures, that set). Real-git fixture model borrowed
from `test_handoff_reconcile_close_terminal_defects.py`'s own `_Repo`/`repo`
fixture (itself a trimmed re-derivation of the deleted conftest.py
`HandoffRepo`) — `locked_rmw`'s D1 surfaced-history persistence resolves its
lock directory via `git_common_dir`, which shells out to `git rev-parse`,
so a real (not mocked) repo is unavoidable here too.

Import guard: `coordinator_core.ops.handoff_reconcile` MUST be imported at
module load time to fire `@register_op("handoff.reconcile_open")` — mirrors
every other op-test file's own import-guard precedent.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.ops.handoff_reconcile  # noqa: F401 — fires @register_op

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_reconcile import (
    _DISPOSITION_FIELD,
    _DISPOSITION_REASON_FIELD,
)
from coordinator_core.ops.handoff_reconcile import _handler as _reconcile_handler

# Declared, not excused: two real `_handler` passes over a real git repo are
# the only way to exercise the D1 surfaced-history round trip (`locked_rmw`
# resolves its lock dir via `git rev-parse`) — same rationale as
# test_handoff_reconcile_close_terminal_defects.py's own pytestmark.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_OP_NAME = "handoff.reconcile_open"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_reconcile @register_op did not fire"
)


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, check=True,
            **no_console_creationflags(),
        )

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root), capture_output=True, check=True,
            **no_console_creationflags(),
        )
        return Path(result.stdout.decode().strip()).resolve()

    def seed_ambiguous(self, name: str, scope_file: str) -> Path:
        """An open handoff whose scope names a file with no matching
        candidate commit — resolver verdict is always no-match/ambiguous,
        which lands it in surfaced[] on every pass (mirrors the deleted
        test_handoff_reconcile_d1.py's own `_seed_ambiguous` helper)."""
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f'title: "Test Handoff {name}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            "predecessor: null\n"
            "deployment_state: open\n"
            f"scope:\n  - {scope_file}\n"
            "---\n\n# Handoff\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def abs_path(self, name: str) -> Path:
        return self.root / "state" / "handoffs" / name

    def write_disposition(
        self, name: str, *, disposition: str | None, reason: str | None
    ) -> None:
        """Write ONE or BOTH of the two D1 disposition fields onto an
        already-seeded handoff, between reconcile passes. Passing None for
        either arg omits that field entirely, for the negative-case tests."""
        path = self.abs_path(name)
        text = path.read_text(encoding="utf-8")
        lines = []
        if disposition is not None:
            lines.append(f'{_DISPOSITION_FIELD}: "{disposition}"')
        if reason is not None:
            lines.append(f'{_DISPOSITION_REASON_FIELD}: "{reason}"')
        insertion = "\n" + "\n".join(lines) if lines else ""
        new_text = text.replace(
            "\n---\n\n# Handoff", f"{insertion}\n---\n\n# Handoff", 1
        )
        assert new_text != text, "fixture's closing frontmatter delimiter shape changed"
        path.write_text(new_text, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"disposition {name}")


@pytest.fixture
def repo(tmp_path) -> _Repo:
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, check=True,
            **no_console_creationflags(),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "reconcile-disposition-discharge-test@claude-klabauter.test")
    _git("config", "user.name", "reconcile-disposition-discharge Test")
    _git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")
    return _Repo(root)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


def _reconcile(repo: _Repo) -> dict:
    """A single dry-run reconcile pass. dry_run is left to the policy default
    (conservative/True per D2(a) — see handoff_reconcile._handler's own
    docstring) — this file must never arm a live transition."""
    return _run(_reconcile_handler({}, repo_root=repo.common_dir))


def _violation_ids(result: dict) -> set:
    return {v["handoff_id"] for v in result["conservation_violations"]}


def test_disposition_discharge_clears_violation_on_next_pass(repo):
    """AC2 core proof: (1) surfaces on pass 1, (2) is a conservation
    violation on pass 2 with nothing recorded, (3) writing BOTH disposition
    fields between passes clears it on pass 3, and the violation count drops
    by exactly one."""
    name = "2026-01-01-d1-discharge.md"
    repo.seed_ambiguous(name, "nonexistent-discharge.py")

    result1 = _reconcile(repo)
    assert result1["exit_code"] == 0
    assert result1["conservation_violations"] == []
    assert any(e["handoff_id"] == "2026-01-01-d1-discharge" for e in result1["surfaced"])

    result2 = _reconcile(repo)
    assert result2["exit_code"] == 2
    assert len(result2["conservation_violations"]) == 1
    assert "2026-01-01-d1-discharge" in _violation_ids(result2)

    repo.write_disposition(
        name,
        disposition="acknowledged",
        reason="known false positive, tracked externally",
    )

    fm = split_frontmatter(repo.abs_path(name).read_text(encoding="utf-8"))
    assert fm is not None
    assert read_fm_field(fm.fm_text, _DISPOSITION_FIELD) == '"acknowledged"'
    assert read_fm_field(fm.fm_text, _DISPOSITION_REASON_FIELD) == (
        '"known false positive, tracked externally"'
    )

    result3 = _reconcile(repo)
    assert result3["exit_code"] == 0
    assert result3["conservation_violations"] == []
    before = len(result2["conservation_violations"])
    after = len(result3["conservation_violations"])
    assert before - after == 1, (
        f"violation count must drop by exactly one candidate discharged, "
        f"got before={before} after={after}"
    )
    assert "2026-01-01-d1-discharge" not in _violation_ids(result3)


def test_disposition_field_alone_does_not_discharge(repo):
    """Negative case: `reconcile_disposition` populated,
    `reconcile_disposition_reason` absent — `_has_recorded_disposition`
    requires BOTH non-empty, so this must still count as a violation."""
    name = "2026-01-01-d1-disposition-only.md"
    repo.seed_ambiguous(name, "nonexistent-disposition-only.py")

    _reconcile(repo)  # pass 1 — surfaces
    result2 = _reconcile(repo)
    assert "2026-01-01-d1-disposition-only" in _violation_ids(result2)

    repo.write_disposition(name, disposition="acknowledged", reason=None)

    result3 = _reconcile(repo)
    assert result3["exit_code"] == 2
    assert "2026-01-01-d1-disposition-only" in _violation_ids(result3), (
        "disposition alone (no reason) must NOT discharge the violation"
    )


def test_disposition_reason_alone_does_not_discharge(repo):
    """Negative case, the other half: `reconcile_disposition_reason`
    populated, `reconcile_disposition` absent — same both-required contract."""
    name = "2026-01-01-d1-reason-only.md"
    repo.seed_ambiguous(name, "nonexistent-reason-only.py")

    _reconcile(repo)  # pass 1 — surfaces
    result2 = _reconcile(repo)
    assert "2026-01-01-d1-reason-only" in _violation_ids(result2)

    repo.write_disposition(name, disposition=None, reason="tracked externally")

    result3 = _reconcile(repo)
    assert result3["exit_code"] == 2
    assert "2026-01-01-d1-reason-only" in _violation_ids(result3), (
        "reason alone (no disposition) must NOT discharge the violation"
    )
