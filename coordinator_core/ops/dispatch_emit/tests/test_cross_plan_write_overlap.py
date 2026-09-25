"""Tests for coordinator_core.ops.dispatch_emit.cross_plan_write_overlap.

Fixture governance: `resolve_git_common_dir` is pure filesystem (no git
spawn -- see its own module docstring), so a bare `.git` directory under
`tmp_path` is enough to exercise it for real; no `git init` subprocess.
Liveness (`session.liveness.claim_holder_live`) is monkeypatched directly
-- this module's own concern is the overlap-detection logic, not liveness
derivation, which `session/tests/test_liveness.py` already covers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cross_plan_write_overlap as overlap_mod
from coordinator_core.ops.dispatch_emit.cross_plan_write_overlap import (
    CrossPlanWriteOverlap,
    check_cross_plan_write_overlap,
)
from coordinator_core.ops.dispatch_emit.spine_read import read_spine

_PLAN_TEMPLATE = """---
title: "{title}"
sizing_object: null
---

# {title}

## Problem

Test fixture.

## Tasks

```yaml plan-tasks
- id: C1
  title: Do the thing
  change_kind: doc-edit
  surface: {surface}
  writes:
{writes_block}
  queue_scope: project
  disposition: open
  body: |
    Do the thing.
```
"""


def _write_plan(root: Path, slug: str, writes: list[str]) -> Path:
    plan_path = root / "docs" / "plans" / f"{slug}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    writes_block = "\n".join(f"    - {w}" for w in writes)
    plan_path.write_text(
        _PLAN_TEMPLATE.format(
            title=slug, surface=writes[0], writes_block=writes_block
        ),
        encoding="utf-8",
    )
    return plan_path


def _write_claim(root: Path, slug: str, sid: str = "peer-sid") -> Path:
    claim_dir = root / ".git" / "coordinator-sessions" / "plan-claims" / slug
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    return claim_dir


def _init_git_dir(root: Path) -> None:
    (root / ".git").mkdir(parents=True, exist_ok=True)


def test_no_overlap_when_no_peer_claims(tmp_path, monkeypatch):
    _init_git_dir(tmp_path)
    plan_path = _write_plan(tmp_path, "plan-a", ["docs/reference/a.md"])
    rows = read_spine(plan_path)
    # Should not raise -- no plan-claims dir at all.
    check_cross_plan_write_overlap(plan_path, rows, tmp_path)


def test_no_overlap_when_peer_writes_a_disjoint_path(tmp_path, monkeypatch):
    _init_git_dir(tmp_path)
    plan_path = _write_plan(tmp_path, "plan-a", ["docs/reference/a.md"])
    _write_plan(tmp_path, "plan-b", ["docs/reference/b.md"])
    _write_claim(tmp_path, "plan-b")
    monkeypatch.setattr(overlap_mod, "claim_holder_live", lambda *a, **k: True)

    rows = read_spine(plan_path)
    check_cross_plan_write_overlap(plan_path, rows, tmp_path)  # does not raise


def test_refuses_when_a_live_peer_declares_the_same_write_path(tmp_path, monkeypatch):
    _init_git_dir(tmp_path)
    plan_path = _write_plan(tmp_path, "plan-a", ["docs/reference/shared.md"])
    _write_plan(tmp_path, "plan-b", ["docs/reference/shared.md"])
    _write_claim(tmp_path, "plan-b")
    monkeypatch.setattr(overlap_mod, "claim_holder_live", lambda *a, **k: True)

    rows = read_spine(plan_path)
    with pytest.raises(CrossPlanWriteOverlap) as exc_info:
        check_cross_plan_write_overlap(plan_path, rows, tmp_path)
    message = str(exc_info.value)
    assert "docs/plans/plan-b.md" in message
    assert "docs/reference/shared.md" in message


def test_dead_peer_claim_is_not_a_collision(tmp_path, monkeypatch):
    _init_git_dir(tmp_path)
    plan_path = _write_plan(tmp_path, "plan-a", ["docs/reference/shared.md"])
    _write_plan(tmp_path, "plan-b", ["docs/reference/shared.md"])
    _write_claim(tmp_path, "plan-b")
    monkeypatch.setattr(overlap_mod, "claim_holder_live", lambda *a, **k: False)

    rows = read_spine(plan_path)
    check_cross_plan_write_overlap(plan_path, rows, tmp_path)  # does not raise


def test_a_plans_own_claim_on_itself_is_never_a_collision(tmp_path, monkeypatch):
    _init_git_dir(tmp_path)
    plan_path = _write_plan(tmp_path, "plan-a", ["docs/reference/a.md"])
    _write_claim(tmp_path, "plan-a", sid="me-sid")
    monkeypatch.setattr(overlap_mod, "claim_holder_live", lambda *a, **k: True)

    rows = read_spine(plan_path)
    check_cross_plan_write_overlap(plan_path, rows, tmp_path)  # does not raise


def test_undeclared_writes_never_collide_with_anything(tmp_path, monkeypatch):
    """AC2 anti-scope: an UNDECLARED writes: (this plan declares none at
    all) must never be read as colliding with a live peer's real writes:."""
    _init_git_dir(tmp_path)
    plan_path = tmp_path / "docs" / "plans" / "plan-a.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        """---
title: "plan-a"
sizing_object: null
---

# plan-a

## Problem

Test fixture.

## Tasks

```yaml plan-tasks
- id: C1
  title: A row with no writes at all
  change_kind: doc-edit
  surface: docs/reference/shared.md
  queue_scope: project
  disposition: open
  body: |
    Examine only.
```
""",
        encoding="utf-8",
    )
    _write_plan(tmp_path, "plan-b", ["docs/reference/shared.md"])
    _write_claim(tmp_path, "plan-b")
    monkeypatch.setattr(overlap_mod, "claim_holder_live", lambda *a, **k: True)

    rows = read_spine(plan_path)
    check_cross_plan_write_overlap(plan_path, rows, tmp_path)  # does not raise


def test_repo_root_none_is_a_no_op(tmp_path):
    plan_path = _write_plan(tmp_path, "plan-a", ["docs/reference/a.md"])
    rows = read_spine(plan_path)
    check_cross_plan_write_overlap(plan_path, rows, None)  # does not raise
