from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "red-set-report.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("red_set_report", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


red_set_report = _load_module()


@pytest.fixture
def synthetic_suite(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "markers = [\n"
        '    "designed_red: red by design; failure output is a worklist",\n'
        '    "cadence: heavy suite, runs at cadence gates",\n'
        '    "pending_fix: known-broken path assumption",\n'
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "test_synthetic.py").write_text(
        "import pytest\n"
        "\n"
        "def test_passing():\n"
        "    assert True\n"
        "\n"
        "@pytest.mark.designed_red\n"
        "def test_marked_failure():\n"
        "    assert False, 'expected: carries designed_red marker'\n"
        "\n"
        "def test_unmarked_failure():\n"
        "    assert False, 'expected: carries no marker at all'\n",
        encoding="utf-8",
    )
    return tmp_path


def test_marked_failure_excluded_from_unmarked_set(synthetic_suite: Path):
    report = red_set_report.derive_red_set(
        str(synthetic_suite),
        marker_expr="not designed_red and not cadence and not pending_fix",
    )
    marked_ids = {nodeid.split("::")[-1] for nodeid in report["marked_failed"]}
    assert "test_marked_failure" in marked_ids
    unmarked_ids = {nodeid.split("::")[-1] for nodeid in report["unmarked_failed"]}
    assert "test_marked_failure" not in unmarked_ids


def test_unmarked_failure_reported_as_unmarked(synthetic_suite: Path):
    report = red_set_report.derive_red_set(
        str(synthetic_suite),
        marker_expr="not designed_red and not cadence and not pending_fix",
    )
    unmarked_ids = {nodeid.split("::")[-1] for nodeid in report["unmarked_failed"]}
    assert "test_unmarked_failure" in unmarked_ids


def test_unfiltered_totals_include_all_three_cells(synthetic_suite: Path):
    report = red_set_report.derive_red_set(
        str(synthetic_suite),
        marker_expr="not designed_red and not cadence and not pending_fix",
    )
    unfiltered = report["unfiltered"]
    assert unfiltered["total"] == 3
    assert unfiltered["passed"] == 1
    assert unfiltered["failed"] == 2


def test_capped_worker_count_never_returns_none_for_bare_auto_style_request():
    import os

    capped = red_set_report.capped_worker_count(10_000)
    assert capped is not None
    physical_cores = os.cpu_count() or 1
    assert capped <= max(1, physical_cores // 2)


def test_capped_worker_count_default_is_serial():
    assert red_set_report.capped_worker_count(None) is None
    assert red_set_report.capped_worker_count(1) is None


def test_marked_split_is_derived_from_collection_not_execution_outcome():
    collected_all = {
        "test_mod.py::test_marked",
        "test_mod.py::test_unmarked",
        "test_mod.py::test_transient",
    }
    collected_fast = {
        "test_mod.py::test_unmarked",
        "test_mod.py::test_transient",
    }
    marked = red_set_report.derive_marked_split(collected_all, collected_fast)
    assert marked == {"test_mod.py::test_marked"}

    failed_all = {"test_mod.py::test_marked", "test_mod.py::test_transient"}
    unmarked_failed = sorted(failed_all & collected_fast)
    marked_failed = sorted(failed_all & marked)
    assert unmarked_failed == ["test_mod.py::test_transient"]
    assert marked_failed == ["test_mod.py::test_marked"]


def test_collected_fast_not_subset_of_collected_all_fails_loudly():
    collected_all = {"test_mod.py::test_a"}
    collected_fast = {"test_mod.py::test_a", "test_mod.py::test_b_new_from_peer"}
    with pytest.raises(RuntimeError, match="test_b_new_from_peer"):
        red_set_report.derive_marked_split(collected_all, collected_fast)
