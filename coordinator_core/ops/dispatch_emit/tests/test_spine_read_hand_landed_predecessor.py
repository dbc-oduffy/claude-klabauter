"""A `depends_on_plan` predecessor row delivered by a trailer-less hand-landing does not hold its
dependent; an undelivered one still does."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import resolve_plan_edge

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_EDGE = {"plan": "docs/plans/pred.md", "chunk": "P1", "gate_kind": "output-consumption-runtime"}
_PLAN = (
    "---\nstatus: executing\n---\n# plan\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: P1\n  title: pred\n  surface: src/a.py\n  writes: [src/a.py, src/tests/test_a.py]\n"
    "```\n"
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, env=dict(os.environ),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(root: Path, msg: str, *rels: str) -> None:
    _git(root, "add", *rels)
    _git(root, "commit", "-qm", msg)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "seed.txt", "seed")
    _commit(tmp_path, "seed", "seed.txt")
    _write(tmp_path, "src/a.py", "old\n")
    _write(tmp_path, "docs/plans/pred.md", _PLAN)
    _commit(tmp_path, "plan", "src/a.py", "docs/plans/pred.md")
    return tmp_path


def test_nothing_delivered_still_holds(repo):
    res = resolve_plan_edge("row 'D1'", _EDGE, repo, {})
    assert res.hold is not None and "not yet coded" in res.hold


def test_partial_delivery_still_holds(repo):
    _write(repo, "src/a.py", "new\n")
    _commit(repo, "touch only one write", "src/a.py")
    assert "not yet coded" in resolve_plan_edge("row 'D1'", _EDGE, repo, {}).hold


def test_hand_landed_predecessor_does_not_hold_dependent(repo):
    _write(repo, "src/a.py", "new\n")
    _write(repo, "src/tests/test_a.py", "def test_a(): pass\n")
    _commit(repo, "003672: hand-landed, no checkpoint trailer", "src/a.py", "src/tests/test_a.py")
    cache: dict = {}
    assert resolve_plan_edge("row 'D1'", _EDGE, repo, cache).hold is None
    assert resolve_plan_edge("row 'D1'", _EDGE, repo, cache).hold is None


def test_uncommitted_writes_are_not_delivery(repo):
    _write(repo, "src/a.py", "new\n")
    _write(repo, "src/tests/test_a.py", "def test_a(): pass\n")
    assert "not yet coded" in resolve_plan_edge("row 'D1'", _EDGE, repo, {}).hold
