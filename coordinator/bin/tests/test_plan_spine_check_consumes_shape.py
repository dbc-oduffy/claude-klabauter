"""plan-spine-check refuses a dict-shaped `consumes` entry as STRUCTURAL instead of crashing."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"
_spec = importlib.util.spec_from_file_location("plan_spine_check_consumes_shape", _MOD)
psc = importlib.util.module_from_spec(_spec)
sys.modules["plan_spine_check_consumes_shape"] = psc
_spec.loader.exec_module(psc)


def test_dict_consumes_entry_is_structural_not_a_crash(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text(
        "---\ntitle: t\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        '- id: C1\n  title: "x"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n  writes: []\n'
        "  consumes:\n    - {chunk: C0, artifact: a.md}\n```\n",
        encoding="utf-8",
    )
    report = psc.check_plan(path)
    assert report["verdict"] == "INVALID"
    assert any(f["at"].startswith("consumes") and f["class"] == "structural" for f in report["rows"])
    assert psc.main([str(path)]) == psc.EXIT_INVALID
