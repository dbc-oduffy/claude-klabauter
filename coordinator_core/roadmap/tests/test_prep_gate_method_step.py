"""
coordinator_core/roadmap/tests/test_prep_gate_method_step.py — REGISTER method-step seams.

Subject: `prep_gate._register`, which reports a claimed method-step row with no
`method-trace` block, and a `depends_on_plan` predecessor whose trace carries a
`deviation` that this plan's `anchor_premises` do not name.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import prep_gate as pg

_SIZING_REL = "state/sizings/2026-11-02-fixture.yaml"
_PRED_REL = "docs/plans/2026-11-01-predecessor.md"

_SPINE = """- id: C1
  title: Ship the thing
  body: Map values.
  change_kind: code-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: [coordinator_core/roadmap/prep_gate.py]
  queue_scope: project
  disposition: open
  register_rows: [R1.b]
"""

_REGISTER = """title: fixture-sizing
requirement_register:
  rows:
  - id: R1.b
    status: open
    anchor: relayed#method-step:likeness/b
    source_text: (b) map into expected-value space
"""

_TRACE = """```yaml method-trace
row: R1.b
mechanism: "map scan landmarks into expected-value space via mapper"
at: {path: a.py, contains: "def map"}
```
"""

_DEVIATION_TRACE = """```yaml method-trace
row: R1.b
mechanism: "conform scan values onto the mesh"
at: {path: a.py, contains: "def map"}
deviation: {delivers_by: "mesh conform", reason: "no mapper", ratified_by: "APM"}
```
"""


def _plan(root: Path, body_extra: str = "", fm_extra: str = "", name: str = "2026-11-02-fixture.md") -> Path:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    fm = [
        "title: fixture",
        "status: draft",
        "created: 2026-11-02",
        "author: fixture-session",
        "census: []",
        "capabilities: []",
        "prime_exit_criterion:",
        "  statement: the register resolves",
        f"  derived_from: {_SIZING_REL}",
        f"sizing_object: {_SIZING_REL}",
        f"register_claims:\n  sizing: {_SIZING_REL}\n  rows: [R1.b]",
    ]
    if fm_extra:
        fm.append(fm_extra)
    body = ["", "# Fixture", "", body_extra, "## Tasks", "", "```yaml plan-tasks", _SPINE.strip("\n"), "```", ""]
    path = plans / name
    path.write_text("---\n" + "\n".join(fm) + "\n---\n" + "\n".join(body), encoding="utf-8")
    return path


def _setup(root: Path) -> None:
    sizing = root / _SIZING_REL
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text(_REGISTER, encoding="utf-8")
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)


def _reg(root: Path, path: Path) -> dict:
    return pg.gate_plan(root, path)["classes"]["REGISTER"]


def _predecessor(root: Path, trace: str) -> None:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (root / _PRED_REL).write_text("---\ntitle: pred\n---\n# Pred\n\n" + trace, encoding="utf-8")


_EDGE = f"depends_on_plan:\n- {{plan: {_PRED_REL}, chunk: C1, gate_kind: output-consumption-runtime}}"
_PRED_SPINE = """---
title: pred
status: draft
---
# Pred

{trace}
## Tasks

```yaml plan-tasks
- id: C1
  title: t
  body: b
  change_kind: code-edit
  surface: x.py
  writes: [x.py]
  queue_scope: project
  disposition: coded
```
"""


def _pred(root: Path, trace: str) -> None:
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (root / _PRED_REL).write_text(_PRED_SPINE.format(trace=trace), encoding="utf-8")


def test_untraced_step_row_is_a_register_defect(tmp_path):
    _setup(tmp_path)
    reg = _reg(tmp_path, _plan(tmp_path))
    assert reg["status"] == "DEFECT" and reg["kind"] == "register-method-trace-missing"
    assert "method-step row R1.b has no method-trace block" in reg["detail"]


def test_traced_step_row_passes(tmp_path):
    _setup(tmp_path)
    assert _reg(tmp_path, _plan(tmp_path, _TRACE))["status"] == "PASS"


def test_unmarked_row_needs_no_trace(tmp_path):
    _setup(tmp_path)
    (tmp_path / _SIZING_REL).write_text(_REGISTER.replace("#method-step:likeness/b", ""), encoding="utf-8")
    assert _reg(tmp_path, _plan(tmp_path))["status"] == "PASS"


def test_predecessor_deviation_unnamed_is_inherited_finding(tmp_path):
    _setup(tmp_path)
    _pred(tmp_path, _DEVIATION_TRACE)
    reg = _reg(tmp_path, _plan(tmp_path, _TRACE, _EDGE + "\nanchor_premises: []"))
    assert reg["status"] == "DEFECT" and reg["kind"] == "method-step-inherited-deviation"
    assert _PRED_REL in reg["detail"] and "R1.b" in reg["detail"]


def test_predecessor_deviation_named_in_anchor_premises_passes(tmp_path):
    _setup(tmp_path)
    _pred(tmp_path, _DEVIATION_TRACE)
    premises = (
        "anchor_premises:\n- id: A1\n"
        f"  claim: \"{_PRED_REL} R1.b delivers by mesh conform, accepted\"\n"
        "  check: {command: \"grep -c x a.py\", expect: \"1\"}"
    )
    assert _reg(tmp_path, _plan(tmp_path, _TRACE, _EDGE + "\n" + premises))["status"] == "PASS"


def test_premise_naming_path_only_still_refused(tmp_path):
    _setup(tmp_path)
    _pred(tmp_path, _DEVIATION_TRACE)
    premises = (
        "anchor_premises:\n- id: A1\n"
        f"  claim: \"{_PRED_REL} is in place\"\n"
        "  check: {command: \"grep -c x a.py\", expect: \"1\"}"
    )
    reg = _reg(tmp_path, _plan(tmp_path, _TRACE, _EDGE + "\n" + premises))
    assert reg["kind"] == "method-step-inherited-deviation"


def test_predecessor_trace_without_deviation_passes(tmp_path):
    _setup(tmp_path)
    _pred(tmp_path, _TRACE)
    assert _reg(tmp_path, _plan(tmp_path, _TRACE, _EDGE + "\nanchor_premises: []"))["status"] == "PASS"
