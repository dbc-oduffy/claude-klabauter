"""A spine row declaring `expect_partial: true` releases its dependents on a PARTIAL reply, with no accept_partial record."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.tests.test_revise_accept_partial_releases_dependents import _FAN, _HARNESS
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

_PLAN = """---
title: t
---

## Tasks

```yaml plan-tasks
- id: X
  title: planned partial
  expect_partial: true
- id: W
  title: unplanned
- id: Y
  title: dependent
  depends_on: [X]
```
"""


def _go(plan_text, x_reply, w_reply="PARTIAL: other", record=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    seed = {**emit._planned_partial_seed(plan_text), **(record or {})}
    helper = emit._run_row_helper_js(accepted_seed=seed)
    rows = _FAN % (json.dumps(x_reply), json.dumps(w_reply))
    proc = subprocess.run(
        [node, "-"], input=_HARNESS % {"helper": helper, "rows": rows},
        capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_planned_partial_releases_its_dependent_with_no_record():
    out = _go(_PLAN, "PARTIAL: half done")
    assert "Y" in out["calls"] and "Y" not in out["heldBy"]


def test_a_planned_row_that_is_blocked_still_holds_its_dependent():
    out = _go(_PLAN, "BLOCKED: cannot start")
    assert "Y" not in out["calls"] and out["heldBy"]["Y"] == "X"


def test_an_unplanned_partial_still_holds_its_dependent():
    out = _go(_PLAN, "PARTIAL: half done")
    assert "Z" not in out["calls"] and out["heldBy"]["Z"] == "W"


def test_an_unplanned_partial_is_released_by_the_accept_partial_record():
    out = _go(_PLAN, "PARTIAL: half done", record={"W": "recorded"})
    assert "Z" in out["calls"] and "Z" not in out["heldBy"]


def test_seed_is_empty_without_the_declaration():
    assert emit._planned_partial_seed(_PLAN.replace("expect_partial: true", "")) == {}
    assert emit._planned_partial_seed(None) == {}


def test_compose_script_seeds_the_planned_row_at_default_arguments():
    row = WaveRow(id="X", title="t", surface="s", writes=["a.py"], reads=[], depends_on=[], body="")
    script = emit.compose_script(
        [[row]], name="n", description="d", plan_path="docs/plans/example.md",
        plan_text=_PLAN, **REVIEW_KW
    )
    assert f"const _acceptPartial = {json.dumps({'X': 'plan-declared expect_partial'})};" in script
