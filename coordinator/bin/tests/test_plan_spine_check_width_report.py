"""`plan-spine-check`'s WIDTH report and the `WIDTH<3` flag over the engine's `wave_map.build_dag`,
injectable at `_ENGINE_WIDTH_FNS_OVERRIDE` for fixture-only tests.
"""
from __future__ import annotations

import importlib.util
import sys
from collections import namedtuple
from pathlib import Path

import pytest

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_width", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_width"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()

_DagPlan = namedtuple("DagPlan", ["nodes", "waves", "max_concurrent_rows", "critical_path_rows"])


@pytest.fixture(autouse=True)
def _restore_override():
    yield
    psc._ENGINE_WIDTH_FNS_OVERRIDE = None


def _stub_engine(*, nodes_count: int, max_width: int, critical_path_rows: int):
    def read_spine(path):
        return list(range(nodes_count))

    def build_dag(rows):
        return _DagPlan(
            nodes=tuple(range(nodes_count)),
            waves=[],
            max_concurrent_rows=max_width,
            critical_path_rows=critical_path_rows,
        )

    return read_spine, build_dag


def _plan(tmp_path: Path, rows: str, *, width_rationale: str | None = None) -> Path:
    path = tmp_path / "plan.md"
    rationale_block = f"\n## Width rationale\n\n{width_rationale}\n" if width_rationale else ""
    path.write_text(
        "---\ntitle: t\n---\n\n# A plan\n" + rationale_block
        + "\n## Tasks\n\n```yaml plan-tasks\n" + rows + "\n```\n",
        encoding="utf-8",
    )
    return path


_FOUR_ROW_SERIAL = "\n".join(
    f'- id: C{i}\n  title: "row {i}"\n  change_kind: doc-edit\n  surface: "docs/{i}.md"\n'
    f'  writes:\n    - "docs/{i}.md"'
    for i in range(1, 5)
)

_TWO_ROW = "\n".join(
    f'- id: C{i}\n  title: "row {i}"\n  change_kind: doc-edit\n  surface: "docs/{i}.md"\n'
    f'  writes:\n    - "docs/{i}.md"'
    for i in range(1, 3)
)


def test_four_row_serial_prints_width_and_flags(tmp_path):
    psc._ENGINE_WIDTH_FNS_OVERRIDE = _stub_engine(
        nodes_count=4, max_width=1, critical_path_rows=4
    )
    report = psc.check_plan(_plan(tmp_path, _FOUR_ROW_SERIAL))
    assert report["width"] == {
        "max_width": 1,
        "critical_path_rows": 4,
        "dispatchable_rows": 4,
        "critical_path_share": 1.0,
    }
    assert "WIDTH max=1 critical-path=4/4" in psc._render(report)
    assert any(a["at"] == "width" for a in report["advisories"])


def test_four_row_serial_with_width_rationale_suppresses_flag(tmp_path):
    psc._ENGINE_WIDTH_FNS_OVERRIDE = _stub_engine(
        nodes_count=4, max_width=1, critical_path_rows=4
    )
    path = _plan(tmp_path, _FOUR_ROW_SERIAL, width_rationale="Genuinely serial by design.")
    report = psc.check_plan(path)
    assert not any(a["at"] == "width" for a in report["advisories"])


def test_two_row_plan_never_flags(tmp_path):
    psc._ENGINE_WIDTH_FNS_OVERRIDE = _stub_engine(
        nodes_count=2, max_width=1, critical_path_rows=2
    )
    report = psc.check_plan(_plan(tmp_path, _TWO_ROW))
    assert not any(a["at"] == "width" for a in report["advisories"])


def test_width_unavailable_reports_a_line_and_never_crashes(tmp_path):
    def _read_spine(path):
        raise ImportError("no width function on this engine")

    psc._ENGINE_WIDTH_FNS_OVERRIDE = (_read_spine, lambda rows: None)
    report = psc.check_plan(_plan(tmp_path, _TWO_ROW))
    assert report["width"] is None
    assert report["width_unavailable"]
    assert "WIDTH unavailable:" in psc._render(report)


def test_against_the_real_engine_asserts_shape_only(tmp_path):
    report = psc.check_plan(_plan(tmp_path, _TWO_ROW))
    assert report["width"] is not None, report["width_unavailable"]
    for key in ("max_width", "critical_path_rows", "dispatchable_rows", "critical_path_share"):
        assert key in report["width"]
