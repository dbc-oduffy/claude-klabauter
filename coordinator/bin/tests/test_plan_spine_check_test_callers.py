"""`plan-spine-check`'s test-referencer advisory and unlisted-test STRUCTURAL finding."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_callers", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_callers"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()


@pytest.fixture(autouse=True)
def _restore():
    yield
    psc._TEST_REFERENCER_GREP_OVERRIDE = None


def _plan(tmp_path: Path, rows: str) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "plan.md"
    path.write_text(
        "---\ntitle: t\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "\n```\n",
        encoding="utf-8",
    )
    return path


def _row(body: str = "do it") -> str:
    return (
        "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: pkg/mod.py\n"
        "  writes:\n    - pkg/mod.py\n    - pkg/tests/test_mod.py\n"
        f"  body: |\n    {body}"
    )


def _grep(hits):
    return lambda root, patterns: hits


def test_orphaned_test_referencer_is_reported(tmp_path):
    psc._TEST_REFERENCER_GREP_OVERRIDE = _grep(
        [("pkg/tests/test_other.py", "from pkg.mod import f")]
    )
    report = psc.check_plan(_plan(tmp_path, _row()))
    found = [a for a in report["advisories"] if "TEST-REFERENCER-OUTSIDE-WRITES" in a["error"]]
    assert len(found) == 1
    assert found[0]["row"] == "C1"
    assert "pkg/tests/test_other.py" in found[0]["error"] and "pkg/mod.py" in found[0]["error"]
    assert report["verdict"] != "INVALID"


def test_done_when_citing_an_unlisted_test_is_structural(tmp_path):
    psc._TEST_REFERENCER_GREP_OVERRIDE = _grep([])
    report = psc.check_plan(_plan(tmp_path, _row("Done when pkg/tests/test_unlisted.py passes.")))
    assert report["verdict"] == "INVALID"
    assert any("UNLISTED-TEST" in f["error"] and f["class"] == "structural" for f in report["rows"])


def test_clean_spine_reports_neither(tmp_path):
    psc._TEST_REFERENCER_GREP_OVERRIDE = _grep(
        [("pkg/tests/test_mod.py", "from pkg.mod import f")]
    )
    report = psc.check_plan(_plan(tmp_path, _row("Done when pkg/tests/test_mod.py passes.")))
    assert report["verdict"] == "VALID"
    assert not any("TEST-REFERENCER" in a["error"] for a in report["advisories"])
    assert not any("UNLISTED-TEST" in f["error"] for f in report["rows"])
