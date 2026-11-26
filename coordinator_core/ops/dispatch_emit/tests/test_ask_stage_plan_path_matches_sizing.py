"""ask_stage stages the sizing-named plan path, or refuses a same-stem plan at another path, naming both."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import _handler, _sizing_named_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

RUN = "ask-20261112T000000-abc123"
STEM = "2026-11-12-memo-blitz"
SIZING_REL = f"state/sizings/{STEM}.yaml"
NAMED = f"docs/plans/2026-11/{STEM}.md"
FLAT = f"docs/plans/{STEM}.md"

PLAN = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: U1
  title: ungated
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
```
"""

SIZING = {
    "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
    "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
    "premise": {"provenance": "not-applicable", "evidence": "fixture"},
}


def _stage(tmp_path, *, plan_path: str, landed: tuple[str, ...]):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(SIZING, sort_keys=False), encoding="utf-8")
    for rel in landed:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(PLAN, encoding="utf-8", newline="\n")
    params = {"run_id": RUN, "plan_path": plan_path, "commit_sizing_path": SIZING_REL}
    return read_stage_reply(tmp_path, _handler(params, repo_root=tmp_path))


def test_matching_plan_path_stages_unchanged(tmp_path):
    reply = _stage(tmp_path, plan_path=NAMED, landed=(NAMED,))
    assert "error" not in reply


def test_flat_plan_path_stages_the_sizing_named_file_when_it_exists(tmp_path):
    reply = _stage(tmp_path, plan_path=FLAT, landed=(NAMED,))
    assert "error" not in reply


def test_flat_plan_path_stages_where_written_when_the_sizing_named_file_is_absent(tmp_path):
    reply = _stage(tmp_path, plan_path=FLAT, landed=(FLAT,))
    assert "error" not in reply


def test_refuses_naming_both_when_neither_file_exists(tmp_path):
    reply = _stage(tmp_path, plan_path=FLAT, landed=())
    assert FLAT in reply["error"] and NAMED in reply["error"]
    assert "PLAN-PATH-MISMATCH" in reply["error"]


def test_a_plan_with_an_unrelated_name_is_not_compared(tmp_path):
    reply = _stage(tmp_path, plan_path="docs/plans/2026-11-12-other.md", landed=("docs/plans/2026-11-12-other.md",))
    assert "error" not in reply


def test_missing_caller_plan_stages_the_existing_sizing_plan(tmp_path):
    """A caller path that is not in the repo stages the sizing's plan file."""
    missing = "docs/plans/2026-11/2026-11-26-add-polygon-tally-82acbb.md"
    reply = _stage(tmp_path, plan_path=missing, landed=(NAMED,))
    assert "error" not in reply
    assert _sizing_named_plan(tmp_path, missing, SIZING_REL) == NAMED
