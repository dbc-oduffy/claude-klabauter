"""emit_script refuses a plan whose register_claims pins no longer match the sizing's rows."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import emit

from .conftest import REVIEW_KW

_SPINE = """
## Tasks

```yaml plan-tasks
- id: C1
  title: ship it
  body: ship the thing
  change_kind: code-edit
  surface: coordinator_core/thing.py
  writes: [coordinator_core/thing.py]
  queue_scope: project
  disposition: open
```
"""
_SIZING_REL = "state/sizings/s.yaml"


def _sizing(tmp_path, text):
    p = tmp_path / _SIZING_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(
            {"requirement_register": {"sources": [{"kind": "pm"}],
                                      "rows": [{"id": "R1", "source_text": text}]}}
        ),
        encoding="utf-8",
    )


def _plan(tmp_path, pins):
    fm = "title: fixture\n"
    if pins is not None:
        fm += f"register_claims:\n  sizing: {_SIZING_REL}\n  rows: [R1]\n  pins:\n    R1: '{pins}'\n"
    path = tmp_path / "plan.md"
    path.write_text(f"---\n{fm}---\n\n# fixture\n{_SPINE}", encoding="utf-8")
    (tmp_path / "coordinator_core").mkdir(exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def _no_fidelity(monkeypatch):
    monkeypatch.delenv(emit._FIDELITY_MARKER_ENV, raising=False)


def test_matching_pins_emit(tmp_path):
    _sizing(tmp_path, "do the thing")
    plan = _plan(tmp_path, rr.row_pin("do the thing"))
    assert emit.emit_script(plan, repo_root=tmp_path, **REVIEW_KW)


def test_edited_row_refuses_naming_the_row(tmp_path):
    _sizing(tmp_path, "do the thing")
    plan = _plan(tmp_path, rr.row_pin("do the thing"))
    _sizing(tmp_path, "do another thing")
    with pytest.raises(emit.RegisterPinDriftError, match="register row R1"):
        emit.emit_script(plan, repo_root=tmp_path, **REVIEW_KW)


def test_whitespace_only_edit_emits(tmp_path):
    _sizing(tmp_path, "do the thing")
    plan = _plan(tmp_path, rr.row_pin("do the thing"))
    _sizing(tmp_path, "do   the\nthing")
    assert emit.emit_script(plan, repo_root=tmp_path, **REVIEW_KW)


def test_plan_without_pins_emits(tmp_path):
    _sizing(tmp_path, "anything")
    plan = _plan(tmp_path, None)
    assert emit.emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
