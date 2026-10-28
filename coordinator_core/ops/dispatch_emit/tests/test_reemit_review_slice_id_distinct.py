"""A same-session re-emit of one plan mints distinct review group slice ids, and a review group prep
halt leaves that group's rows owing review in the production landed-row reckoning."""

from __future__ import annotations

import re
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import emit_script, landed_rows_from_text
from coordinator_core.ops.review_mint.execute_review import GROUP_PREP_HALT
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SESSION = "620dde2b-aaaa-bbbb-cccc-111122223333"

_PLAN = """---
title: "p"
sizing_object: null
---

# p

## Goal

g

## Tasks

```yaml plan-tasks
- id: D0
  title: tD0
  change_kind: doc-edit
  surface: docs/x/d0.md
  writes:
    - docs/x/d0.md
  queue_scope: project
  disposition: open
  body: |
    Do D0.
- id: X0
  title: tX0
  change_kind: doc-edit
  surface: docs/x/x0.md
  writes:
    - docs/x/x0.md
  queue_scope: project
  disposition: open
  body: |
    Do X0.
```
"""

_SLICE_PREFIX_RE = re.compile(r'slice_prefix\\*":\\*"([^"\\]+)')


def _git(root, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    plan = root / "docs" / "plans" / "p.md"
    plan.write_text(_PLAN, encoding="utf-8")
    _git(root, "add", "--", "docs/plans/p.md")
    _git(root, "commit", "-q", "-m", "seed")
    return root, plan


def _prefixes(script: str) -> set:
    return set(_SLICE_PREFIX_RE.findall(script))


def test_same_session_reemit_mints_disjoint_group_slice_prefixes(tmp_path):
    root, plan = _repo(tmp_path)
    first = emit_script(
        plan, repo_root=root, session_id=_SESSION, script_path=str(tmp_path / "r1.workflow.mjs"),
        landed_rows=None, **REVIEW_KW,
    )
    second = emit_script(
        plan, repo_root=root, session_id=_SESSION, script_path=str(tmp_path / "r2.workflow.mjs"),
        landed_rows=frozenset(), **REVIEW_KW,
    )

    a, b = _prefixes(first), _prefixes(second)
    assert a and b, "both emits must carry review group slice prefixes"
    assert a.isdisjoint(b), f"re-emit reuses a frozen group slice id: {sorted(a & b)}"


def test_group_prep_halt_leaves_the_groups_rows_owing_review():
    run_text = (
        "checkpoint(wave 1): 1 rows — D0\n"
        f"{{halted: '{GROUP_PREP_HALT} (freeze of 620dde2b-p.g0 failed: slice_id already names a "
        "frozen diff with different content)'}\n"
    )

    assert "D0" not in landed_rows_from_text(run_text), (
        "a row whose group prep halted is credited as landed and never reviewed"
    )
    assert "D0" in landed_rows_from_text(run_text, exclude_stop_rule=False), (
        "--review-only still reviews the committed bytes"
    )


def test_group_prep_halt_with_owing_review_strikes_only_the_named_rows():
    run_text = (
        "checkpoint(wave 1): 2 rows — D0, X0\n"
        f'{{"halted": "{GROUP_PREP_HALT} (freeze failed)", "owing_review": ["X0"]}}\n'
    )

    landed = landed_rows_from_text(run_text)

    assert "X0" not in landed
    assert "D0" in landed
