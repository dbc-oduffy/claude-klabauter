"""Every failed or declined publish row carries a real reason into the outcome block.

Drives `publish.main` at default arguments with stubbed targets and engine (the wiring is the
sibling outcome-block test's `_wire`).

Run: python -m pytest coordinator/bin/tests/test_publish_failed_row_reason.py -q
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_TESTS_DIR = Path(__file__).resolve().parent
_PUBLISH_PY = _TESTS_DIR.parent / "publish.py"


def _load_sibling():
    spec = importlib.util.spec_from_file_location(
        "publish_outcome_block_for_failed_row_reason", _TESTS_DIR / "test_publish_outcome_block_and_exit.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_sibling = _load_sibling()
publish = _sibling.publish


def _run_with(monkeypatch, tmp_path, double):
    _sibling._wire(monkeypatch, tmp_path, ["row-a"], declined=set())
    monkeypatch.setattr(publish, "process_target", double)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", buf)
    rc = publish.main(["row-a"])
    out = buf.getvalue()
    return rc, out[out.index("=== publish outcome ==="):]


def test_silent_decline_names_the_engine_defect_not_a_pointer(monkeypatch, tmp_path):
    def silent(target, setup_dir, totals, **kwargs):
        return None

    rc, tail = _run_with(monkeypatch, tmp_path, silent)

    assert rc != 0
    assert "row-a: REFUSED/FAILED" in tail
    assert "process_target declined without a reason (engine defect)" in tail
    assert "see the Error lines" not in tail


def test_silent_decline_restates_the_rows_last_output_line(monkeypatch, tmp_path):
    def chatty(target, setup_dir, totals, **kwargs):
        print("  gate X looked at the tree")
        print("  gate X is satisfied but returned early")
        print("")

    rc, tail = _run_with(monkeypatch, tmp_path, chatty)

    assert rc != 0
    assert "last output: gate X is satisfied but returned early" in tail


def test_decline_site_string_on_totals_is_the_reason(monkeypatch, tmp_path):
    def declines_at_site(target, setup_dir, totals, *, timing_sink=None, **kwargs):
        publish._decline_row(totals, None, target.name, "dest not ready")

    rc, tail = _run_with(monkeypatch, tmp_path, declines_at_site)

    assert rc != 0
    assert "reason:    dest not ready" in tail
    assert "engine defect" not in tail


def test_decline_row_records_reason_and_refused_timing_label():
    totals = publish.RunTotals()
    sink: list = []

    publish._decline_row(totals, sink, "row-a", "dest not ready")

    assert totals.decline_reason == "dest not ready"
    assert sink == [("row-a", "REFUSED: dest not ready", 0.0, 0.0)]
    publish._decline_row(totals, None, "row-a", "again")
    assert totals.decline_reason == "again"


def test_raising_row_shows_the_exception_repr(monkeypatch, tmp_path):
    def raises(target, setup_dir, totals, **kwargs):
        raise ValueError("denied by test")

    rc, tail = _run_with(monkeypatch, tmp_path, raises)

    assert rc != 0
    assert "ValueError('denied by test')" in tail


def test_error_line_still_outranks_the_backstop(monkeypatch, tmp_path):
    def errors(target, setup_dir, totals, **kwargs):
        print("  Error: real gate reason", file=sys.stderr)
        publish._decline_row(totals, None, target.name, "site label")

    rc, tail = _run_with(monkeypatch, tmp_path, errors)

    assert "reason:    Error: real gate reason" in tail
    assert "site label" not in tail


def _run_two_rows(monkeypatch, tmp_path, *, engine_root_names=()):
    """row-a stages fine, row-b is refused; row-a's dest reads as a stamped engine root."""
    from coordinator_core.warm import engine_root as warm_engine_root

    _sibling._wire(monkeypatch, tmp_path, ["row-a", "row-b"], declined={"row-b"})
    stamped = {tmp_path / f"dst-{n}" for n in engine_root_names}
    monkeypatch.setattr(
        warm_engine_root, "is_engine_root", lambda path: Path(path).resolve() in {p.resolve() for p in stamped}
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", buf)
    rc = publish.main(["row-a,row-b"])
    out = buf.getvalue()
    return rc, out[out.index("=== publish outcome ==="):]


def test_clean_row_demoted_by_a_refused_peer_names_why_it_was_not_landed(monkeypatch, tmp_path):
    rc, tail = _run_two_rows(monkeypatch, tmp_path, engine_root_names=["row-a"])

    assert rc != 0
    assert "row-a: REFUSED/FAILED" in tail
    assert "staged fine but not landed into engine root dst-row-a" in tail
    assert "refused row(s) in the round: row-b" in tail
    assert "no reason was captured" not in tail


def test_clean_row_outside_an_engine_root_is_not_demoted_by_a_refused_peer(monkeypatch, tmp_path):
    rc, tail = _run_two_rows(monkeypatch, tmp_path, engine_root_names=[])

    assert rc != 0
    assert "staged fine but not landed" not in tail
    assert "row-b: REFUSED/FAILED" in tail


def test_landing_exception_is_the_rows_reason(monkeypatch, tmp_path):
    _sibling._wire(monkeypatch, tmp_path, ["row-a"], declined=set())
    real_assemble = publish._assemble_root_union

    def with_a_survivor(*args, **kwargs):
        union = real_assemble(*args, **kwargs)
        union.survivors = {"payload.txt": object()}
        return union

    monkeypatch.setattr(publish, "_assemble_root_union", with_a_survivor)

    def boom(*args, **kwargs):
        raise OSError("disk said no")

    import percolate.diff_commit as diff_commit

    monkeypatch.setattr(diff_commit, "land_diff", boom)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(sys, "stderr", buf)
    rc = publish.main(["row-a"])
    out = buf.getvalue()
    tail = out[out.index("=== publish outcome ==="):]

    assert rc != 0
    assert "landing failed: OSError('disk said no')" in tail
