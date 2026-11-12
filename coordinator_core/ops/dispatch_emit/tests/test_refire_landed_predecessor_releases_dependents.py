"""A predecessor landed by a ledger-check halt commit is seeded as landed on refire, so its dependent dispatches."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import checkpoint_landed_state, emit_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = "docs/plans/p.md"
_ROWS = (
    "- id: C1\n  title: first\n  change_kind: doc-edit\n  surface: docs/x/c1.md\n"
    "  writes:\n    - docs/x/c1.md\n  agent_type: coordinator:executor\n"
    "- id: C3\n  title: dependent\n  change_kind: doc-edit\n  surface: docs/x/c3.md\n"
    "  writes:\n    - docs/x/c3.md\n  agent_type: coordinator:executor\n"
    "  depends_on:\n    - chunk: C1\n      gate_kind: output-consumption-runtime\n"
)


def _repo(tmp_path, halted_by, plan_trailer=True):
    root = tmp_path / "repo"
    root.mkdir(parents=True)

    def git(*a):
        subprocess.run(["git", *a], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())

    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    plan = root / _PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{_ROWS}```\n", encoding="utf-8", newline="\n"
    )
    git("add", ".")
    git("commit", "-q", "-m", "seed")
    (root / "landed.txt").write_text("x", encoding="utf-8")
    git("add", "landed.txt")
    trailers = [f"Halted-By: {halted_by}", "Lands-Chunks: C1"]
    if plan_trailer:
        trailers.insert(0, f"Halt-Plan: {_PLAN}")
    git("commit", "-q", "-m", "warp: record halted run-x", "-m", "\n".join(trailers))
    return root


def test_ledger_check_halt_lands_its_chunks_for_the_plan(tmp_path):
    root = _repo(tmp_path, "ledger-check")
    assert checkpoint_landed_state(root, _PLAN)[0] == {"C1"}


def test_a_non_ledger_halt_or_another_plan_lands_nothing(tmp_path):
    assert checkpoint_landed_state(_repo(tmp_path / "a", "refusal"), _PLAN)[0] == frozenset()
    assert checkpoint_landed_state(_repo(tmp_path / "b", "ledger-check"), "docs/plans/other.md")[0] == frozenset()


def test_refire_drops_the_hand_landed_predecessor_and_dispatches_its_dependent(tmp_path):
    root = _repo(tmp_path, "ledger-check")
    script = emit_script(root / _PLAN, repo_root=root, landed_rows=frozenset(), **REVIEW_KW)
    assert "docs/x/c3.md" in script
    assert "Execute C3: dependent" in script
    assert "Execute C1" not in script
    assert "chunk: C1" not in script
