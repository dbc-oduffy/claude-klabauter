"""`plan-spine-check` PM-brief traceability: a plan with a `## PM brief` section must trace every
open or coded, non-deferred row to it by substring; a plan with no brief reports NO-BRIEF and
sets no failure.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_pm_brief", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_pm_brief"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()


def _plan(tmp_path: Path, rows: str, *, pm_brief: str | None = None) -> Path:
    path = tmp_path / "plan.md"
    brief_block = f"\n## PM brief\n\n{pm_brief}\n" if pm_brief is not None else ""
    path.write_text(
        "---\ntitle: t\n---\n\n# A plan\n" + brief_block
        + "\n## Tasks\n\n```yaml plan-tasks\n" + rows + "\n```\n",
        encoding="utf-8",
    )
    return path


_VALID_ROW = (
    '- id: C1\n  title: "do the thing"\n  change_kind: doc-edit\n  surface: "docs/a.md"'
)
_PM_BRIEF_TEXT = "make --utterance mandatory and injected, since the brief never reaches an executor"


def test_a_plan_with_no_pm_brief_section_reports_no_brief_and_no_failure(tmp_path):
    path = _plan(tmp_path, _VALID_ROW)
    report = psc.check_plan(path)
    assert report["verdict"] == "VALID"
    assert report["no_brief"] is True
    assert psc.main([str(path)]) == psc.EXIT_OK
    assert "NO-BRIEF" in psc._render(report)


def test_a_traced_row_against_a_pm_brief_is_valid(tmp_path):
    path = _plan(
        tmp_path,
        _VALID_ROW + '\n  traces_to_brief: "make --utterance mandatory"',
        pm_brief=_PM_BRIEF_TEXT,
    )
    report = psc.check_plan(path)
    assert report["verdict"] == "VALID"
    assert report["no_brief"] is False
    assert psc.main([str(path)]) == psc.EXIT_OK


def test_an_untraced_row_against_a_pm_brief_is_structural(tmp_path):
    path = _plan(tmp_path, _VALID_ROW, pm_brief=_PM_BRIEF_TEXT)
    report = psc.check_plan(path)
    assert report["verdict"] == "INVALID"
    assert any(f["class"] == "structural" and f["row"] == "C1" for f in report["rows"])
    assert psc.main([str(path)]) == psc.EXIT_INVALID


def test_a_paraphrased_trace_against_a_pm_brief_is_structural(tmp_path):
    path = _plan(
        tmp_path,
        _VALID_ROW + '\n  traces_to_brief: "make the executor read the ask"',
        pm_brief=_PM_BRIEF_TEXT,
    )
    report = psc.check_plan(path)
    assert report["verdict"] == "INVALID"
    assert any(f["class"] == "structural" for f in report["rows"])


def test_a_deferred_row_is_exempt_from_the_trace_requirement(tmp_path):
    path = _plan(tmp_path, _VALID_ROW + "\n  deferred: true", pm_brief=_PM_BRIEF_TEXT)
    report = psc.check_plan(path)
    assert not any(f["at"] == "traces_to_brief" for f in report["rows"])
    assert psc.main([str(path)]) == psc.EXIT_OK
