"""reconcile_done_noop flips open rows coded at HEAD with the no-op detail."""

from __future__ import annotations

import textwrap

from coordinator_core.ops.dispatch_emit.landed_reconcile import reconcile_done_noop
from coordinator_core.session.record_homes import home_dir
from pathlib import Path

_SHA = "0123456789abcdef0123456789abcdef01234567"

_PLAN = textwrap.dedent(
    """\
    ---
    status: draft
    ---

    # Plan

    ## Tasks

    ```yaml plan-tasks
    - id: C1
      title: first
      change_kind: code-edit
      surface: src/a.py
      writes: [src/a.py]
      disposition: open
    - id: C3
      title: shipped earlier
      change_kind: code-edit
      surface: src/c.py
      writes: [src/c.py]
      disposition: coded
      disposition_ref: abc1234
    ```
    """
)


def _repo(tmp_path, with_head=True):
    git = tmp_path / ".git"
    git.mkdir()
    if with_head:
        (git / "HEAD").write_text(_SHA + "\n", encoding="utf-8")
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "p.md").write_text(_PLAN, encoding="utf-8")
    inv_dir = Path(home_dir(str(tmp_path), "mise-inventory"))
    inv_dir.mkdir(parents=True)
    inventory = inv_dir / "run.md"
    inventory.write_text("---\nrun_id: run\n---\n", encoding="utf-8")
    return inventory


def test_open_row_flips_with_detail(tmp_path):
    inv = _repo(tmp_path)
    got = reconcile_done_noop(inv, {"docs/plans/p.md": {"C1": "r/C1.md"}})
    assert got == {"docs/plans/p.md": ["C1"]}
    text = (tmp_path / "docs" / "plans" / "p.md").read_text(encoding="utf-8")
    assert _SHA in text
    assert "done-no-op: executor report r/C1.md DONE, no declared write dirty" in text


def test_coded_row_and_outside_plan_left_alone(tmp_path):
    inv = _repo(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text(_PLAN, encoding="utf-8")
    before = (tmp_path / "docs" / "plans" / "p.md").read_text(encoding="utf-8")
    got = reconcile_done_noop(
        inv, {"docs/plans/p.md": {"C3": "r.md"}, f"../{outside.name}": {"C1": "r.md"}}
    )
    assert got == {}
    assert (tmp_path / "docs" / "plans" / "p.md").read_text(encoding="utf-8") == before
    assert outside.read_text(encoding="utf-8") == _PLAN


def test_unreadable_head_returns_empty(tmp_path):
    inv = _repo(tmp_path, with_head=False)
    assert reconcile_done_noop(inv, {"docs/plans/p.md": {"C1": "r.md"}}) == {}
