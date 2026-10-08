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


def _write_verdict(root: Path, inp, **over):
    doc = {
        "falsifier_sha": inp.falsifier_sha,
        "plan_sha": inp.plan_sha,
        "verdict": "BROKEN",
        "tells": ["SCOPE-WIDER-THAN-CLAIM"],
    }
    doc.update(over)
    target = root / inp.cache_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc), encoding="utf-8")


def test_cache_miss_dispatches_a_reviewer_and_names_where_to_persist(tmp_path):
    a = _repo(tmp_path, "run tools/inst.py")
    [inp] = fip.review_inputs([a], repo_root=tmp_path, report_dir=Path("state/mi/run.can-report-red"))
    assert inp.cached is None and inp.report_json is not None
    assert inp.cache_path == "state/mi/.can-report-red/p1.verdict.json"
    [spec] = fip.review_specs([inp])
    assert spec.schema == "falsifier_integrity_result"
    assert spec.cache_key == (inp.falsifier_sha, inp.plan_sha)
    assert spec.model == "sonnet"


def test_cache_hit_skips_the_reviewer_and_writes_no_report(tmp_path):
    a = _repo(tmp_path, "run tools/inst.py")
    report_dir = Path("state/mi/run.can-report-red")
    [miss] = fip.review_inputs([a], repo_root=tmp_path, report_dir=report_dir)
    _write_verdict(tmp_path, miss)
    [hit] = fip.review_inputs([a], repo_root=tmp_path, report_dir=report_dir)
    assert hit.cached == {"verdict": "BROKEN", "tells": ["SCOPE-WIDER-THAN-CLAIM"]}
    assert hit.report_path is None and hit.report_json is None
    [spec] = fip.review_specs([hit])
    assert spec.schema == "cached_result" and spec.cache_key is None
    assert json.loads(spec.prompt) == {
        "verdict": "BROKEN",
        "tells": [{"tell": "SCOPE-WIDER-THAN-CLAIM", "status": "FIRED"}],
    }


def test_cache_is_shared_across_run_ids(tmp_path):
    a = _repo(tmp_path, "run tools/inst.py")
    [first] = fip.review_inputs([a], repo_root=tmp_path, report_dir=Path("state/mi/run1.can-report-red"))
    _write_verdict(tmp_path, first, verdict="SOUND", tells=[])
    [second] = fip.review_inputs([a], repo_root=tmp_path, report_dir=Path("state/mi/run2.can-report-red"))
    assert second.cached == {"verdict": "SOUND", "tells": []}


def test_plan_edit_instrument_edit_and_corrupt_file_each_invalidate(tmp_path):
    a = _repo(tmp_path, "run tools/inst.py")
    report_dir = Path("state/mi/run.can-report-red")
    [base] = fip.review_inputs([a], repo_root=tmp_path, report_dir=report_dir)
    _write_verdict(tmp_path, base)

    plan = tmp_path / a
    plan.write_text(plan.read_text(encoding="utf-8") + "\nedit\n", encoding="utf-8")
    [edited_plan] = fip.review_inputs([a], repo_root=tmp_path, report_dir=report_dir)
    assert edited_plan.cached is None

    _write_verdict(tmp_path, edited_plan)
    (tmp_path / "tools" / "inst.py").write_text("print('changed')\n", encoding="utf-8")
    [edited_instrument] = fip.review_inputs([a], repo_root=tmp_path, report_dir=report_dir)
    assert edited_instrument.cached is None

    (tmp_path / edited_instrument.cache_path).write_text("{not json", encoding="utf-8")
    [corrupt] = fip.review_inputs([a], repo_root=tmp_path, report_dir=report_dir)
    assert corrupt.cached is None


def test_only_sound_and_broken_verdicts_are_ever_replayed(tmp_path):
    a = _repo(tmp_path, "run tools/inst.py")
    report_dir = Path("state/mi/run.can-report-red")
    [base] = fip.review_inputs([a], repo_root=tmp_path, report_dir=report_dir)
    _write_verdict(tmp_path, base, verdict="UNREVIEWABLE", tells=[])
    [again] = fip.review_inputs([a], repo_root=tmp_path, report_dir=report_dir)
    assert again.cached is None
