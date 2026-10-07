"""Blinding, can-report-red computation, and label stability of the falsifier-integrity phase."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import falsifier_integrity_phase as fip

_PLAN = """---
title: x
prime_exit_criterion:
  statement: the gate can fail
  falsifier:
    how: {how}
    baseline_output: green today
    baseline_ref: abc1234
    expected_when_true: red
---
# Plan

## Acceptance Criteria
| AC-1 | secret |

```yaml plan-tasks
- id: C1
```
"""


def _repo(tmp_path: Path, how: str, name: str = "p1") -> str:
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "inst.py").write_text("print('ok')\n", encoding="utf-8")
    rel = f"docs/plans/{name}.md"
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(_PLAN.format(how=how), encoding="utf-8")
    return rel


def test_plans_with_falsifier_filters(tmp_path):
    a = _repo(tmp_path, "run tools/inst.py")
    (tmp_path / "docs/plans/none.md").write_text("---\ntitle: y\n---\n# P\n", encoding="utf-8")
    got = fip.plans_with_falsifier([a, "docs/plans/none.md"], repo_root=tmp_path)
    assert got == frozenset({a})


def test_py_instrument_yields_report(tmp_path):
    a = _repo(tmp_path, "run tools/inst.py")
    [inp] = fip.review_inputs([a], repo_root=tmp_path, report_dir=Path("reports"))
    data = json.loads(inp.report_json)
    assert {"filename", "verdict", "findings"} <= set(data)
    assert data["filename"] == "tools/inst.py"
    assert inp.report_path == "reports/p1.json"
    assert inp.baseline_ref == "abc1234"
    assert inp.criterion == "the gate can fail"


def test_report_has_no_line_a_capped_reader_would_truncate(tmp_path, monkeypatch):
    a = _repo(tmp_path, "run tools/inst.py")
    findings = [{"code": "NO_EXIT_PATH", "line": i, "detail": "x" * 120} for i in range(40)]
    monkeypatch.setattr(
        fip, "_load_verdict_reaches_exit",
        lambda: lambda source, name: {"filename": name, "verdict": "NO_EXIT_PATH", "findings": findings},
    )
    [inp] = fip.review_inputs([a], repo_root=tmp_path, report_dir=Path("reports"))
    assert len(inp.report_json) > 1500
    assert max(len(line) for line in inp.report_json.splitlines()) <= 1500


def test_command_shaped_how_yields_none(tmp_path):
    a = _repo(tmp_path, "run pytest -q")
    [inp] = fip.review_inputs([a], repo_root=tmp_path, report_dir=Path("r"))
    assert inp.report_path is None and inp.report_json is None
    [spec] = fip.review_specs([inp])
    assert "no code to walk" in spec.prompt


def test_missing_py_file_yields_none(tmp_path):
    a = _repo(tmp_path, "run tools/absent.py")
    [inp] = fip.review_inputs([a], repo_root=tmp_path, report_dir=Path("r"))
    assert inp.report_json is None


def test_prompt_is_blinded_and_labels_are_ordinal_and_stable(tmp_path):
    a = _repo(tmp_path, "run tools/inst.py", "stem-one")
    b = _repo(tmp_path, "run pytest", "other")
    inputs = fip.review_inputs([a, b], repo_root=tmp_path, report_dir=Path("r"))
    specs = fip.review_specs(inputs)
    assert [s.label for s in specs] == ["falsifier-integrity:1", "falsifier-integrity:2"]
    assert [s.label for s in fip.review_specs(inputs)] == [s.label for s in specs]
    for s in specs:
        for leak in ("docs/plans", "Acceptance", "plan-tasks", "secret"):
            assert leak not in s.prompt
        assert "stem-one" not in s.label
        assert s.agent_type == fip.REVIEWER_AGENT_TYPE
        assert s.model == fip.REVIEWER_AGENT_MODEL
        assert s.schema == "falsifier_integrity_result"


def test_sh_instrument_yields_report(tmp_path):
    a = _repo(tmp_path, "run tools/probe.sh")
    (tmp_path / "tools" / "probe.sh").write_text("#!/bin/sh\ngrep x y || true\n", encoding="utf-8")
    [inp] = fip.review_inputs([a], repo_root=tmp_path, report_dir=Path("reports"))
    assert json.loads(inp.report_json)["verdict"] == "NO_EXIT_PATH"
    assert inp.report_path == "reports/p1.json"
