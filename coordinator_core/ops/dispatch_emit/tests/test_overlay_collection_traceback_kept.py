"""A clean-mirror verdict built from a collection error keeps the collection traceback (frames plus the
ImportError line) on the red verdict and on both ENV_DEFECT branches, even when trailing output pushes the
traceback out of the `_OUTPUT_TAIL` tail."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import READER, _fake_pytest, _repo

pytestmark = [pytest.mark.complete]

_FRAMES = "".join(f"t/layer_{i}.py:{i + 1}: in step_{i}\n    from t.layer_{i + 1} import value_{i}\n" for i in range(40))
_RAISING_FRAME = "t/layer_40.py:41: in step_40"


def _output(cause: str, summary: str = "") -> str:
    """A collection error whose traceback is far longer than the tail, then noise longer than the tail."""
    return (
        "==================================== ERRORS ====================================\n"
        f"_________________________ ERROR collecting {READER} _________________________\n"
        "ImportError while importing test module '/mirror/t/test_reader.py'.\nTraceback:\n"
        f"{_FRAMES}{_RAISING_FRAME}\n    import t.layer_41\n{cause}\n"
        "=============================== warnings summary ===============================\n"
        + "".join(f"t/noise_{i}.py:1: DeprecationWarning: padding line {i}\n" for i in range(80))
        + summary
    )


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def test_collection_traceback_keeps_the_header_and_the_raising_end_of_a_long_section():
    text = cov.collection_traceback(_output("E   ImportError: cannot import name 'x' from 'y'"))

    assert text.startswith("_") and f"ERROR collecting {READER}" in text.splitlines()[0]
    assert _RAISING_FRAME in text and "ImportError: cannot import name 'x'" in text
    assert len(text) <= cov._OUTPUT_TAIL
    assert "padding line" not in text


def test_collection_traceback_is_empty_without_a_collection_error_and_counts_further_ones():
    assert cov.collection_traceback("FAILED t/a.py::test_x - assert 0\n1 failed in 0.1s\n") == ""
    two = "_ ERROR collecting a.py _\nE   ImportError: one\n_ ERROR collecting b.py _\nE   ImportError: two\n"
    text = cov.collection_traceback(two)
    assert "ImportError: one" in text and "ImportError: two" not in text and "(+1 more collection error)" in text


def test_a_traceback_the_tail_already_holds_is_not_repeated():
    out = "_ ERROR collecting a.py _\nE   ImportError: one\n1 error in 0.1s\n"
    assert cov.kept_collection_traceback(out, out) == ""
    assert cov.kept_collection_traceback(out, "unrelated tail").startswith("collection traceback:\n")


@pytest.mark.spawns_process
def test_the_red_verdict_keeps_the_collection_traceback_the_tail_cuts(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    cause = "E   ImportError: cannot import name 'value_40' from 't.layer_41'"
    out = _output(cause, "Interrupted: 1 error during collection\n1 error in 0.1s\n")
    assert _RAISING_FRAME not in out.strip()[-cov._OUTPUT_TAIL:]
    _fake_pytest(monkeypatch, 2, out)

    verdict = cov.verify(root, "run1", head, [READER])

    assert verdict.verdict == "red", verdict
    assert _RAISING_FRAME in verdict.detail and cause in verdict.detail, verdict.detail


@pytest.mark.spawns_process
def test_the_interpreter_defect_verdict_keeps_the_collection_traceback(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    cause = "E   ModuleNotFoundError: No module named 'zz_absent_dep'"
    out = _output(cause, "ERROR t/test_reader.py - ModuleNotFoundError: No module named 'zz_absent_dep'\n")
    _fake_pytest(monkeypatch, 2, out)

    verdict = cov.verify(root, "run1", head, [READER])

    assert verdict.verdict == cov.ENV_DEFECT and "lacks module(s) zz_absent_dep" in verdict.detail, verdict
    assert _RAISING_FRAME in verdict.detail and cause in verdict.detail, verdict.detail


@pytest.mark.spawns_process
def test_the_guard_exit_verdict_keeps_the_collection_traceback(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    cause = "E   ImportError: cannot import name 'value_40' from 't.layer_41'"
    out = _output(cause)
    assert cov.guard_exit(1, out)
    _fake_pytest(monkeypatch, 1, out)

    verdict = cov.verify(root, "run1", head, [READER])

    assert verdict.verdict == cov.ENV_DEFECT and "a repo guard ended pytest" in verdict.detail, verdict
    assert _RAISING_FRAME in verdict.detail and cause in verdict.detail, verdict.detail


@pytest.mark.spawns_process
def test_a_red_without_a_collection_error_carries_no_traceback_prefix(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    _fake_pytest(monkeypatch, 1, "FAILED t/test_reader.py::test_reads_data - assert 0\n1 failed in 0.1s\n")

    verdict = cov.verify(root, "run1", head, [READER])

    assert verdict.verdict == "red" and "collection traceback" not in verdict.detail, verdict
