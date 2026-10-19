"""landing_integrity: parse arm, run-base resolver order, no spawn."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.win_portability import no_console_creationflags

A, B, C = "a" * 40, "b" * 40, "c" * 40


def _w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="")


def test_parse_arm(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _w(tmp_path, "bad.py", 'x = "a\nb"\n')
    _w(tmp_path, "ok.py", "x = 1\n")
    _w(tmp_path, "notes.txt", "def (\n")
    out = li.unparseable_py(tmp_path, ["bad.py", "ok.py", "notes.txt", "gone.py"])
    assert [(f.path, f.reason) for f in out] == [("bad.py", li.PY_UNPARSEABLE)]
    assert out[0].detail.startswith("line ")


def test_null_byte_source_is_a_finding(tmp_path):
    (tmp_path / "n.py").write_bytes(b"x = 1\x00\n")
    assert [f.reason for f in li.unparseable_py(tmp_path, ["n.py"])] == [li.PY_UNPARSEABLE]


def test_run_base_resolver_order(tmp_path):
    assert li.run_base_for(tmp_path, "r1", {}) is None
    _w(tmp_path, "scratch/warp/r1/run-base", C + "\n")
    assert li.run_base_for(tmp_path, "r1", {}) == C
    _w(tmp_path, "scratch/warp/r1/submission.json", json.dumps({"run_base_sha": B}))
    assert li.run_base_for(tmp_path, "r1", {}) == B
    assert li.run_base_for(tmp_path, "r1", {"run_base_sha": A}) == A
    assert li.run_base_for(tmp_path, "r1", {"run_base_sha": "zz"}) == B
    assert li.run_base_for(tmp_path, None, {}) is None


def _v(status, caused=None, baseline="absent"):
    if baseline == "absent":
        baseline = None if caused is None else {"caused": caused, "pre_existing": [], "unverified": []}
    return {"status": status, "tests_failed": 2, "baseline": baseline}


@pytest.mark.parametrize("status", ["pass", "pass-with-skips"])
def test_passing_verdict_has_no_findings(status):
    assert li.failed_targeted_tests(_v(status)) == ([], "pass")


def test_caused_failures_yield_one_finding_per_test_file():
    caused = ["a/test_x.py::t1", ".\\b\\test_y.py::T::t2", "a/test_x.py::t3"]
    findings, check = li.failed_targeted_tests(_v("fail", caused))
    assert check == "failed"
    assert [(f.path, f.reason) for f in findings] == [
        ("a/test_x.py", li.TARGETED_TESTS_FAILED),
        ("b/test_y.py", li.TARGETED_TESTS_FAILED),
    ]
    assert "a/test_x.py::t1" in findings[0].detail and "a/test_x.py::t3" in findings[0].detail


def test_detail_names_at_most_three_node_ids():
    caused = [f"t/test_z.py::t{i}" for i in range(5)]
    (finding,), _ = li.failed_targeted_tests(_v("fail", caused))
    assert finding.detail.count("::") == 3


def test_baseline_present_with_nothing_caused_is_pre_existing_only():
    assert li.failed_targeted_tests(_v("fail", [])) == ([], "pre-existing-only")


def test_error_without_baseline_is_one_unattributed_finding():
    findings, check = li.failed_targeted_tests(_v("error", None, baseline=None))
    assert check == "failed"
    assert [(f.path, f.reason) for f in findings] == [("", li.TARGETED_TESTS_FAILED)]


def test_absent_verdict_is_no_verdict():
    assert li.failed_targeted_tests(None) == ([], "no-verdict")


def test_verdict_prefers_targeted_tests_over_both_inline_review_locations():
    direct = {"status": "pass", "tests_failed": 0, "baseline": None}
    inline = {"tests": {"status": "fail", "failed": 3}}
    params = {"targeted_tests": direct, "inline_review": inline, "run_outcome": {"inline_review": inline}}
    assert li.targeted_test_verdict(params) == direct


def test_verdict_falls_back_to_inline_review_then_run_outcome():
    tests = {"status": "fail", "failed": 3}
    expected = {"status": "fail", "tests_failed": 3, "baseline": None}
    assert li.targeted_test_verdict({"inline_review": {"tests": tests}}) == expected
    assert li.targeted_test_verdict({"run_outcome": {"inline_review": {"tests": tests}}}) == expected
    assert li.targeted_test_verdict({"targeted_tests": None}) is None
    assert li.targeted_test_verdict({}) is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_recorded_base_branch_does_not_import_dispatch_emit_op(tmp_path):
    _w(tmp_path, "scratch/warp/r1/run-base", C + "\n")
    repo_root = Path(__file__).resolve().parents[4]
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from pathlib import Path\n"
        "from coordinator_core.ops.dispatch_emit import landing_integrity as li\n"
        f"got = li.run_base_for(Path({str(tmp_path)!r}), 'r1', {{}})\n"
        f"assert got == {C!r}, got\n"
        "assert 'coordinator_core.ops.dispatch_emit.op' not in sys.modules\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr

