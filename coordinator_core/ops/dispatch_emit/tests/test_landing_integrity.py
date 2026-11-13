"""landing_integrity: parse arm, run-base resolver order, no spawn."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li

pytestmark = pytest.mark.complete

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


def _count_parses(monkeypatch):
    calls = []
    real = li.ast.parse

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(li.ast, "parse", counting)
    return calls


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_verdict_cache_cold_and_warm_still_find_and_second_call_parses_nothing(tmp_path, monkeypatch):
    _repo(tmp_path)
    _w(tmp_path, "bad.py", "def (\n")
    _w(tmp_path, "ok.py", "x = 1\n")
    calls = _count_parses(monkeypatch)
    cold = li.unparseable_py(tmp_path, ["bad.py", "ok.py"])
    assert len(calls) == 2
    warm = li.unparseable_py(tmp_path, ["bad.py", "ok.py"])
    assert len(calls) == 2
    assert [(f.path, f.reason, f.detail) for f in cold] == [(f.path, f.reason, f.detail) for f in warm]
    assert [f.reason for f in warm] == [li.PY_UNPARSEABLE]


def test_a_batch_resolves_the_common_dir_once_and_reads_each_verdict_once(tmp_path, monkeypatch):
    _repo(tmp_path)
    names = [f"m{i}.py" for i in range(6)]
    for n in names:
        _w(tmp_path, n, "x = 1\n")  # identical bytes: one verdict file
    _w(tmp_path, "bad.py", "def (\n")
    _w(tmp_path, "other.py", "y = 2\n")
    paths = [*names, "bad.py", "other.py", "note.txt", "gone.py"]
    cold = li.unparseable_py(tmp_path, paths)
    commons: list = []
    reads: list = []
    real_common, real_read = li._git_common_dir, li._read_verdict
    monkeypatch.setattr(li, "_git_common_dir", lambda r: (commons.append(r), real_common(r))[1])
    monkeypatch.setattr(li, "_read_verdict", lambda p: (reads.append(p), real_read(p))[1])
    monkeypatch.setattr(li, "_parse_finding", lambda rel, src: pytest.fail("parsed"))
    warm = li.unparseable_py(tmp_path, paths)
    assert len(commons) == 1
    assert len(reads) == 3 == len(set(reads))
    assert [(f.path, f.detail) for f in warm] == [(f.path, f.detail) for f in cold] == [("bad.py", cold[0].detail)]


def test_batched_verdict_write_still_never_blocks_the_caller(tmp_path, monkeypatch):
    _repo(tmp_path)
    _w(tmp_path, "bad.py", "def (\n")
    stored: list = []
    monkeypatch.setattr(li, "_write_verdict", lambda path, found: stored.append((path, found)))
    assert [f.path for f in li.unparseable_py(tmp_path, ["bad.py", "bad.py"])] == ["bad.py", "bad.py"]
    assert len(stored) == 1  # the second occurrence reads the in-batch verdict


def test_verdict_cache_hit_serves_a_bad_verdict_without_parsing(tmp_path, monkeypatch):
    _repo(tmp_path)
    _w(tmp_path, "bad.py", "def (\n")
    li.unparseable_py(tmp_path, ["bad.py"])
    monkeypatch.setattr(li, "_parse_finding", lambda rel, src: pytest.fail("parsed"))
    assert li._cached_parse_finding(tmp_path, "bad.py", b"def (\n").reason == li.PY_UNPARSEABLE


def test_changed_bytes_miss(tmp_path, monkeypatch):
    _repo(tmp_path)
    _w(tmp_path, "m.py", "def (\n")
    assert li.unparseable_py(tmp_path, ["m.py"])
    _w(tmp_path, "m.py", "x = 1\n")
    calls = _count_parses(monkeypatch)
    assert li.unparseable_py(tmp_path, ["m.py"]) == []
    assert len(calls) == 1


def test_corrupt_or_wrong_shape_verdict_is_a_miss_never_a_pass(tmp_path):
    _repo(tmp_path)
    src = b"def (\n"
    (tmp_path / "bad.py").write_bytes(src)
    path = li._verdict_path(tmp_path, src)
    path.parent.mkdir(parents=True)
    for junk in ("{not json", '{"ok": "yes"}', '{"ok": false}', "[]", ""):
        path.write_text(junk, encoding="utf-8")
        assert [f.reason for f in li.unparseable_py(tmp_path, ["bad.py"])] == [li.PY_UNPARSEABLE]


def test_verdict_key_carries_the_python_version(tmp_path, monkeypatch):
    _repo(tmp_path)
    here = li._verdict_path(tmp_path, b"x = 1\n")
    import sys

    monkeypatch.setattr(sys, "version_info", (3, 99, 0))
    assert li._verdict_path(tmp_path, b"x = 1\n") != here


def test_no_git_dir_means_no_cache_and_still_parses(tmp_path):
    _w(tmp_path, "bad.py", "def (\n")
    assert li._verdict_path(tmp_path, b"def (\n") is None
    assert [f.reason for f in li.unparseable_py(tmp_path, ["bad.py"])] == [li.PY_UNPARSEABLE]


def test_worktree_gitfile_resolves_to_the_common_dir(tmp_path):
    common = tmp_path / "main" / ".git"
    wt_git = common / "worktrees" / "w"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "w"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert (li._git_common_dir(wt) / "x").resolve() == (common / "x").resolve()


def test_verdict_store_is_bounded(tmp_path, monkeypatch):
    _repo(tmp_path)
    monkeypatch.setattr(li, "_VERDICT_MAX", 5)
    monkeypatch.setattr(li, "_VERDICT_KEEP", 2)
    for i in range(8):
        li.unparseable_py(tmp_path, [_w(tmp_path, f"f{i}.py", f"x = {i}\n") or f"f{i}.py"])
    store = tmp_path / ".git" / li._VERDICT_DIR
    assert len(list(store.iterdir())) <= 5


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


def test_baseline_with_nothing_caused_and_no_failing_id_is_not_a_clear():
    findings, check = li.failed_targeted_tests(_v("fail", []))
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_nothing_caused_clears_only_when_every_failing_id_is_evidenced():
    ok = {"status": "fail", "baseline": {"caused": [], "foreign": ["t/a.py::x"], "evidence": ["t/a.py::x"]}}
    assert li.failed_targeted_tests(ok) == ([], "pre-existing-only")
    bad = {"status": "fail", "baseline": {"caused": [], "foreign": ["t/a.py::x", "t/b.py::y"],
                                          "evidence": ["t/a.py::x"]}}
    findings, check = li.failed_targeted_tests(bad)
    assert check == "failed" and [f.path for f in findings] == ["t/b.py"]
    unlabelled = {"status": "fail", "baseline": {"caused": [], "pre_existing": ["t/a.py::x"]}}
    assert li.failed_targeted_tests(unlabelled)[1] == "failed"
    unverified = {"status": "fail", "baseline": {"caused": [], "foreign": ["t/a.py::x"],
                                                 "evidence": ["t/a.py::x"], "unverified": ["t/c.py::z"]}}
    assert li.failed_targeted_tests(unverified)[1] == "failed"


def test_error_without_baseline_is_one_unattributed_finding():
    findings, check = li.failed_targeted_tests(_v("error", None, baseline=None))
    assert check == "failed"
    assert [(f.path, f.reason) for f in findings] == [("", li.TARGETED_TESTS_FAILED)]


@pytest.mark.parametrize("status", ["error", "fail"])
def test_zero_tests_run_is_not_run_never_a_finding(status):
    verdict = {"status": status, "tests_run": 0, "tests_failed": 0, "baseline": None}
    assert li.failed_targeted_tests(verdict) == ([], li.NOT_RUN_CHECK) == ([], "not-run")


@pytest.mark.parametrize("run", [None, "0", False, 3])
def test_non_zero_or_non_int_tests_run_keeps_unattributed_failure(run):
    findings, check = li.failed_targeted_tests({"status": "error", "tests_run": run, "baseline": None})
    assert check == "failed" and findings[0].path == ""


def test_inline_review_run_zero_refuses_when_targeted_tests_exist():
    params = {"inline_review": {"tests": {"status": "error", "run": 0, "failed": 0}},
              "targeted_test_paths": ["a/test_x.py"]}
    assert li.targeted_test_verdict(params)["tests_run"] == 0
    findings, check = li.landing_test_gate(Path("."), params, None)
    assert check == "failed" and findings and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_inline_review_run_zero_with_no_targeted_tests_stays_not_run():
    params = {"inline_review": {"tests": {"status": "error", "run": 0, "failed": 0}}}
    assert li.landing_test_gate(Path("."), params, None) == ([], "not-run")


def test_recorded_zero_run_with_explicit_failed_tests_refuses():
    params = {"targeted_tests": {"status": "error", "tests_run": 0, "baseline": None},
              "failed_tests": ["a/test_x.py::t1"]}
    findings, check = li.landing_test_gate(Path("."), params, None)
    assert check == "failed" and findings


def test_fresh_retest_verdict_with_zero_run_refuses(tmp_path):
    root = _repo(tmp_path)
    path = _write_fresh(root, status="error")
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "tests_run": 0}), encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and findings
    zero = {"targeted_tests": {"status": "error", "tests_run": 0, "baseline": None}}
    findings, check = li.landing_test_gate(root, zero, "r1")
    assert check == "failed" and findings and "errored without running tests" in findings[0].detail


@pytest.mark.parametrize("cause", ["budget-exhausted", "collection-failure", "empty-selection"])
def test_errored_retest_naming_a_zero_run_cause_is_not_run_not_failed(tmp_path, cause):
    root = _repo(tmp_path)
    path = _write_fresh(root, status="error")
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "zero_run_cause": cause, "reason": "why-text"}), encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == li.NOT_RUN_CHECK and not [f for f in findings if f.reason == li.TARGETED_TESTS_FAILED]
    assert cause in findings.deleted_note and "why-text" in findings.deleted_note
    assert "not a test failure" in findings.deleted_note


def test_errored_retest_with_unknown_zero_run_cause_or_ran_tests_still_fails(tmp_path):
    root = _repo(tmp_path)
    path = _write_fresh(root, status="error")
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "zero_run_cause": "bogus"}), encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and "errored without running tests" in findings[0].detail
    path.write_text(json.dumps({**record, "zero_run_cause": "budget-exhausted", "tests_run": 3,
                                "failed_tests": ["t.py::test_x"]}), encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and findings and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_failed_retest_that_ran_tests_is_still_targeted_tests_failed(tmp_path):
    root = _repo(tmp_path)
    path = _write_fresh(root, status="fail")
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "failed_tests": ["t.py::test_x"]}), encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_absent_verdict_is_no_verdict():
    assert li.failed_targeted_tests(None) == ([], "no-verdict")


def test_verdict_prefers_targeted_tests_over_both_inline_review_locations():
    direct = {"status": "pass", "tests_failed": 0, "baseline": None}
    inline = {"tests": {"status": "fail", "failed": 3}}
    params = {"targeted_tests": direct, "inline_review": inline, "run_outcome": {"inline_review": inline}}
    assert li.targeted_test_verdict(params) == direct


def test_verdict_falls_back_to_inline_review_then_run_outcome():
    tests = {"status": "fail", "failed": 3}
    expected = {"status": "fail", "tests_failed": 3, "tests_run": None, "baseline": None}
    assert li.targeted_test_verdict({"inline_review": {"tests": tests}}) == expected
    assert li.targeted_test_verdict({"run_outcome": {"inline_review": {"tests": tests}}}) == expected
    assert li.targeted_test_verdict({"targeted_tests": None}) is None
    assert li.targeted_test_verdict({}) is None


_HEAD = "a" * 40
_RECORDED_FAIL = {"targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None}}


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(_HEAD + "\n", encoding="utf-8")
    (tmp_path / "t.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def _write_fresh(root, *, status="pass", run_id="r1", head=_HEAD, covered=None):
    if covered is None:
        covered = li.landing_fingerprint(root, ["t.py"])
    path = li.fresh_verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "head": head, "tests": ["t.py"], "covered": covered, "status": status}),
        encoding="utf-8",
    )
    return path


def test_fingerprint_hashes_bytes_and_marks_absent(tmp_path):
    root = _repo(tmp_path)
    fp = li.landing_fingerprint(root, ["t.py", "gone.py"])
    assert len(fp["t.py"]) == 64
    assert fp["gone.py"] == "absent"


def test_fresh_pass_over_recorded_fail_is_pass_on_retest(tmp_path):
    root = _repo(tmp_path)
    _write_fresh(root)
    assert li.landing_test_gate(root, _RECORDED_FAIL, "r1") == ([], "pass-on-retest")


def test_head_drift_alone_leaves_a_fresh_pass_a_pass_on_retest(tmp_path):
    root = _repo(tmp_path)
    _write_fresh(root, head="b" * 40)
    assert li.landing_test_gate(root, _RECORDED_FAIL, "r1") == ([], "pass-on-retest")


@pytest.mark.parametrize("breakage", ["empty_covered_head", "covered", "run_id", "corrupt"])
def test_invalid_fresh_verdict_falls_back_to_recorded(tmp_path, breakage):
    root = _repo(tmp_path)
    path = _write_fresh(root, head="b" * 40 if breakage == "empty_covered_head" else _HEAD,
                        covered={} if breakage == "empty_covered_head" else None,
                        run_id="other" if breakage == "run_id" else "r1")
    if breakage == "covered":
        (root / "t.py").write_text("x = 2\n", encoding="utf-8")
    if breakage == "corrupt":
        path.write_text("{not json", encoding="utf-8")
    if breakage == "run_id":
        li.fresh_verdict_path(root, "r1").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed"
    assert li.RETEST_LAUNCHER in findings[0].detail


def test_fresh_fail_refuses(tmp_path):
    root = _repo(tmp_path)
    _write_fresh(root, status="fail")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_none_run_id_skips_fresh_read(tmp_path):
    root = _repo(tmp_path)
    _write_fresh(root)
    assert li.landing_test_gate(root, _RECORDED_FAIL, None)[1] == "failed"


def test_no_baseline_detail_names_retest_command():
    findings, _ = li.failed_targeted_tests({"status": "fail", "baseline": None})
    assert "emit-dispatch-workflow --retest-targeted --from-task-output" in findings[0].detail
    assert "PYTHONPATH" not in findings[0].detail and "<" not in findings[0].detail


def test_remedy_names_the_task_output_path_as_printed():
    params = {**_RECORDED_FAIL, "task_output_path": "tasks\\a.output"}
    findings, _ = li.landing_test_gate(Path("."), params, None)
    assert f"{li.RETEST_LAUNCHER} --from-task-output tasks/a.output" in findings[0].detail
    spaced = li.retest_remedy({"task_output_path": "/a b/c.output"})
    assert spaced.endswith('--from-task-output "/a b/c.output"')


def test_unattributable_refusal_names_the_failed_tests_param():
    findings, check = li.landing_test_gate(Path("."), _RECORDED_FAIL, None)
    assert check == "failed" and "params.failed_tests" in findings[0].detail


def test_explicit_empty_failed_tests_after_a_passing_retest_clears_a_drifted_record(tmp_path):
    root = _repo(tmp_path)
    _write_fresh(root, status="pass")
    (root / "t.py").write_text("x = 2\n", encoding="utf-8")  # covered blob changed: record is stale
    params = {**_RECORDED_FAIL, "failed_tests": []}
    findings, reason = li.landing_test_gate(root, params, "r1")
    assert findings == [] and reason in ("pass-on-retest", "pre-existing-only")


def test_empty_failed_tests_without_a_retest_record_still_refuses(tmp_path):
    root = _repo(tmp_path)
    findings, check = li.landing_test_gate(root, {**_RECORDED_FAIL, "failed_tests": []}, "r1")
    assert check == "failed" and findings


def test_failed_tests_param_outranks_an_errored_no_results_retest(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    path = _write_fresh(root, status="error")
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "reason": "launcher exited 2"}), encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and "errored without running tests" in findings[0].detail
    params = {**_RECORDED_FAIL, "failed_tests": ["t.py::test_x"]}
    findings, check = li.landing_test_gate(root, params, "r1")
    assert check == "failed" and findings[0].path == "t.py"


def test_refusal_names_a_drifted_retest_record_and_its_route(tmp_path):
    root = _repo(tmp_path)
    _write_fresh(root, status="pass")
    (root / "t.py").write_text("x = 2\n", encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed"
    assert "retest record (pass) no longer matches the tree" in findings[0].detail
    assert li.RETEST_LAUNCHER in findings[0].detail


def test_head_movement_alone_never_stales_a_covered_retest(tmp_path):
    root = _repo(tmp_path)
    _write_fresh(root, status="pass", head="f" * 40)  # unrelated + halt-record commits moved HEAD
    assert li.fresh_targeted_verdict(root, "r1")["status"] == "pass"
    assert li.landing_test_gate(root, _RECORDED_FAIL, "r1") == ([], "pass-on-retest")
    (root / "t.py").write_text("x = 2\n", encoding="utf-8")
    assert li.fresh_targeted_verdict(root, "r1") is None


def test_unchanged_failing_files_since_base_are_not_inferred_pre_existing(tmp_path):
    root = _repo(tmp_path)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "failed_tests": ["./t/test_x.py::test_a", "t/test_x.py::test_b"]}
    findings, check = li.landing_test_gate(root, params, None)
    assert check == "failed" and [f.path for f in findings] == ["t/test_x.py"]
    assert "unevidenced red" in findings[0].detail


def test_failed_tests_without_a_run_base_still_refuse(tmp_path):
    root = _repo(tmp_path)
    params = {**_RECORDED_FAIL, "failed_tests": ["t/test_x.py::test_a"]}
    assert li.landing_test_gate(root, params, None)[1] == "failed"


@pytest.mark.parametrize("marker", [{"stale": True, "note": "no targeted set recorded"}, {"status": "stale"}])
def test_stale_record_does_not_admit_a_recorded_failure(tmp_path, marker):
    root = _repo(tmp_path)
    path = _write_fresh(root)
    record = {**json.loads(path.read_text(encoding="utf-8")), **marker}
    path.write_text(json.dumps(record), encoding="utf-8")
    assert li.fresh_targeted_verdict(root, "r1") is None
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED
    assert li.RETEST_LAUNCHER in findings[0].detail
    if marker.get("status") == "stale":
        assert "(stale) ran no tests" in findings[0].detail
    passed = {"targeted_tests": {"status": "pass", "tests_failed": 0}}
    assert li.landing_test_gate(root, passed, "r1")[0] == []


def test_running_record_refuses_with_poll_remedy(tmp_path):
    root = _repo(tmp_path)
    path = li.fresh_verdict_path(root, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": "r1", "status": li.RETEST_RUNNING, "tests": ["t.py"]}), encoding="utf-8")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "retest-running" and findings[0].reason == li.TARGETED_TESTS_FAILED
    assert "--poll" in findings[0].detail and li.RETEST_LAUNCHER in findings[0].detail
    passed = {"targeted_tests": {"status": "pass", "tests_failed": 0}}
    assert li.landing_test_gate(root, passed, "r1")[1] == "retest-running"


def _running(tmp_path):
    root = _repo(tmp_path)
    path = li.fresh_verdict_path(root, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": "r1", "status": li.RETEST_RUNNING, "tests": ["t.py"]}), encoding="utf-8")
    return root


def test_running_retest_is_an_await_with_poll_and_rerun_steps(tmp_path, monkeypatch):
    root = _running(tmp_path)
    monkeypatch.setattr(
        li, "invariant_findings", lambda *a, **k: pytest.fail("invariant check ran with no verdict"),
    )
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1", ["x.py"])
    assert check == "retest-running"
    step = li.retest_await(findings, _RECORDED_FAIL)
    assert step["awaiting"] == "retest"
    assert step["poll"].endswith(" --poll") and li.RETEST_LAUNCHER in step["poll"]
    assert step["rerun"] == li.retest_remedy(_RECORDED_FAIL)


def test_a_real_failure_is_never_an_await(tmp_path):
    root = _repo(tmp_path)
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and findings
    assert li.retest_await(findings, _RECORDED_FAIL) is None
    assert li.retest_await([li.Finding("", li.TARGETED_TESTS_FAILED, "d")], {}) is None


def test_fresh_pass_naming_the_invariant_clears_it(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
    from coordinator_core.ops.dispatch_emit.landing_targeted_tests import RETEST_ATTEMPTS_PARAM

    spent = {RETEST_ATTEMPTS_PARAM: MAX_IN_RUN_REMEDY_ATTEMPTS}
    node = "pkg/tests/test_inv.py::test_invariant"
    monkeypatch.setattr(li, "landing_invariant_nodes", lambda *a, **k: [node])
    monkeypatch.setattr(
        li, "fresh_targeted_verdict", lambda *a, **k: {"status": "pass", "tests": [node]},
    )
    assert li.invariant_findings(tmp_path, ["x.py"], "r1", spent) == []
    monkeypatch.setattr(li, "fresh_targeted_verdict", lambda *a, **k: None)
    (owed,) = li.invariant_findings(tmp_path, ["x.py"], "r1", spent)
    assert "no fresh retest verdict exists" in owed.detail
    assert owed.reason == li.INVARIANT_UNMEASURED != li.TARGETED_TESTS_FAILED
    assert li.retest_remedy(spent, [node]) in owed.detail


def test_a_red_invariant_still_refuses_targeted_tests_failed(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
    from coordinator_core.ops.dispatch_emit.landing_targeted_tests import RETEST_ATTEMPTS_PARAM

    node = "pkg/tests/test_inv.py::test_invariant"
    monkeypatch.setattr(li, "landing_invariant_nodes", lambda *a, **k: [node])
    monkeypatch.setattr(li, "fresh_targeted_verdict", lambda *a, **k: {"status": "fail"})
    (owed,) = li.invariant_findings(
        tmp_path, ["x.py"], "r1", {RETEST_ATTEMPTS_PARAM: MAX_IN_RUN_REMEDY_ATTEMPTS},
    )
    assert owed.reason == li.TARGETED_TESTS_FAILED


def _digest(tmp_path, deviations, wrap=True):
    body = {"deviations": deviations, "tests": {"status": "fail", "per_row": {"failed": []}}}
    path = tmp_path / "task.output"
    path.write_text(json.dumps({"result": body} if wrap else body), encoding="utf-8")
    return str(path)


_NAMED = [
    {"kind": "gate_owed", "anchor": "gate-blocker: outside-footprint: src/lib/mcp/tool-manifest.test.ts > records the env gate"},
    {"kind": "partial", "anchor": ""},
    {"kind": "gate_owed", "anchor": "gate-blocker: outside-footprint: a/test_y.py::test_z"},
    {"kind": "gate_owed", "anchor": "gate-blocker: outside-footprint: none"},
]


@pytest.mark.parametrize("wrap", [True, False])
def test_named_failures_reads_digest_deviations(tmp_path, wrap):
    got = li.named_failures(_digest(tmp_path, _NAMED, wrap))
    assert got == ["src/lib/mcp/tool-manifest.test.ts > records the env gate", "a/test_y.py::test_z"]
    assert li.named_failures(str(tmp_path / "missing")) == []
    assert li.named_failures(None) == []


def test_node_file_splits_vitest_style_ids():
    assert li._node_file("src/a.test.ts > suite > case") == "src/a.test.ts"


def test_digest_named_failure_reaches_the_gate_and_is_retested_by_remedy(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    out = _digest(tmp_path, _NAMED)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "task_output_path": out}
    findings, check = li.landing_test_gate(root, params, None)
    assert check == "failed"
    assert [f.path for f in findings] == ["src/lib/mcp/tool-manifest.test.ts", "a/test_y.py"]
    assert "--tests src/lib/mcp/tool-manifest.test.ts" in findings[0].detail


def test_every_failing_file_refuses_whatever_its_age(tmp_path):
    root = _repo(tmp_path)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "failed_tests": ["t/test_old.py::a", "t/test_new.py::b"]}
    findings, check = li.landing_test_gate(root, params, None)
    assert check == "failed" and [f.path for f in findings] == ["t/test_old.py", "t/test_new.py"]
    assert "--tests t/test_new.py" in findings[1].detail


def test_failed_tests_nested_under_params_is_honoured(tmp_path):
    root = _repo(tmp_path)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "params": {"failed_tests": ["t/test_x.py::a"]}}
    findings, check = li.landing_test_gate(root, params, None)
    assert check == "failed" and findings[0].path == "t/test_x.py"


def test_stale_verdict_after_head_and_tree_drift_still_refuses_a_recorded_failure(tmp_path):
    root = _repo(tmp_path)
    path = _write_fresh(root, head="b" * 40)
    record = {**json.loads(path.read_text(encoding="utf-8")), "status": "stale"}
    path.write_text(json.dumps(record), encoding="utf-8")
    (root / "t.py").write_text("x = 2\n", encoding="utf-8")
    assert li.landing_test_gate(root, _RECORDED_FAIL, "r1")[1] == "failed"


def test_drifted_stale_verdict_of_another_run_is_ignored(tmp_path):
    root = _repo(tmp_path)
    path = _write_fresh(root, run_id="other")
    record = {**json.loads(path.read_text(encoding="utf-8")), "status": "stale"}
    path.write_text(json.dumps(record), encoding="utf-8")
    li.fresh_verdict_path(root, "r1").write_text(json.dumps(record), encoding="utf-8")
    assert li.landing_test_gate(root, _RECORDED_FAIL, "r1")[1] == "failed"


def test_retest_launcher_runs_through_emit_cli(monkeypatch):
    from coordinator_core.ops.dispatch_emit import cli, retest_targeted

    seen = {}
    monkeypatch.setattr(retest_targeted, "main", lambda argv=None: seen.setdefault("argv", argv) and 0)
    assert cli.main(["--retest-targeted", "--from-task-output", "x.output", "--tests", "t/test_a.py"]) == 0
    assert seen["argv"] == ["--from-task-output", "x.output", "--tests", "t/test_a.py"]



def test_error_with_zero_failures_names_the_runner_error_not_a_blank_count():
    verdict = {"status": "error", "tests_failed": 0, "tests_run": 10, "reason": "collection error in t.py"}
    findings, check = li.landing_test_gate(Path("."), {"targeted_tests": verdict}, None)
    assert check == "failed"
    detail = findings[0].detail
    assert "errored with 0 failing tests of 10 run" in detail and "collection error in t.py" in detail
    assert "params.failed_tests" in detail


def test_fail_with_nonzero_failures_keeps_the_count_wording():
    findings, _ = li.failed_targeted_tests({"status": "fail", "tests_failed": 2})
    assert findings[0].detail.startswith("2 tests fail, no baseline")


def test_zero_fail_no_baseline_names_its_cause_not_targeted_tests_failed():
    verdict = {"status": "fail", "tests_failed": 0, "tests_run": 249, "baseline": None}
    findings, check = li.landing_test_gate(Path("."), {"targeted_tests": verdict}, None)
    assert check == li.ZERO_FAIL_CHECK
    assert [f.reason for f in findings] == [li.ZERO_FAIL_UNATTRIBUTED]
    assert li.TARGETED_TESTS_FAILED not in {f.reason for f in findings}


def test_zero_fail_with_status_before_fail_refuses_without_evidence():
    verdict = {"status": "fail", "tests_failed": 0, "tests_run": 249, "status_before": "fail"}
    findings, check = li.landing_test_gate(Path("."), {"targeted_tests": verdict}, None)
    assert check == li.ZERO_FAIL_CHECK and findings[0].reason == li.ZERO_FAIL_UNATTRIBUTED


def test_status_before_rides_the_inline_review_mapping_and_still_refuses():
    params = {"inline_review": {"tests": {"status": "fail", "failed": 0, "run": 9, "status_before": "fail"}}}
    assert li.landing_test_gate(Path("."), params, None)[1] == li.ZERO_FAIL_CHECK


def test_caused_ids_still_refuse_even_with_status_before_fail():
    verdict = {"status": "fail", "tests_failed": 1, "status_before": "fail",
               "baseline": {"caused": ["t/test_a.py::test_x"]}}
    findings, check = li.failed_targeted_tests(verdict)
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_nonzero_fail_with_no_baseline_still_refuses():
    findings, check = li.failed_targeted_tests({"status": "fail", "tests_failed": 2, "status_before": "fail"})
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED
    named, named_check = li.failed_targeted_tests(
        {"status": "fail", "tests_failed": 2, "status_before": "fail", "failed_tests": ["t/test_x.py::test_a"]})
    assert named_check == "failed" and named[0].reason == li.TARGETED_TESTS_FAILED


def _write_record(root, record):
    path = li.fresh_verdict_path(root, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    full = {"run_id": "r1", "head": _HEAD, "covered": li.landing_fingerprint(root, ["t.py"]), **record}
    path.write_text(json.dumps(full), encoding="utf-8")


def test_fresh_no_tests_record_with_only_missing_targets_refuses_and_leaves_failed_tests_remedy_open(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "no-tests", "tests_run": 0, "tests": [], "dropped": ["gone.py"], "note": "dropped"})
    findings, check = li.landing_test_gate(root, {}, "r1")
    assert check == "failed" and "ran 0 tests" in findings[0].detail
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and f"or pass params.{li.FAILED_TESTS_PARAM}" in findings[0].detail


def test_fresh_no_tests_record_with_no_verdict_and_a_targeted_set_refuses(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "no-tests", "tests_run": 0, "tests": ["t.py"], "reason": "collected no tests"})
    findings, check = li.landing_test_gate(root, {}, "r1")
    assert check == "failed" and findings and "ran 0 tests" in findings[0].detail


def test_stale_record_with_no_targeted_set_stays_not_run(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "stale", "tests": [], "note": "no targeted test set recorded"})
    params = {"targeted_tests": {"status": "error", "tests_run": 0, "baseline": None}}
    assert li.landing_test_gate(root, params, "r1") == ([], "not-run")


def test_stale_record_does_not_count_as_a_targeted_set(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "stale", "tests": ["t.py"]})
    assert not li._targeted_set_exists(root, {}, None, "r1")
    _write_record(root, {"status": "no-tests", "tests": [], "dropped": ["gone.py"]})
    assert li._targeted_set_exists(root, {}, None, "r1")


def test_pass_verdict_that_ran_zero_tests_refuses_only_with_a_targeted_set():
    verdict = {"status": "pass", "tests_run": 0, "baseline": None}
    assert li.landing_test_gate(Path("."), {"targeted_tests": verdict}, None) == ([], "pass")
    findings, check = li.landing_test_gate(
        Path("."), {"targeted_tests": verdict, "targeted_test_paths": ["a/test_x.py"]}, None)
    assert check == "failed" and findings
    skips = {**verdict, "status": "pass-with-skips"}
    assert li.landing_test_gate(
        Path("."), {"targeted_tests": skips, "targeted_test_paths": ["a/test_x.py"]}, None) == ([], "pass")


def test_fresh_pass_record_with_zero_tests_run_refuses(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "pass", "tests_run": 0, "tests": ["t.py"]})
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and findings


def test_fresh_no_tests_record_does_not_clear_a_recorded_failure(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "no-tests", "tests_run": 0})
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and findings


def test_fresh_fail_record_node_ids_reach_attribution(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "fail", li.FAILED_TESTS_PARAM: ["t.py::test_x"]})
    findings, check = li.landing_test_gate(root, {}, "r1")
    assert check == "failed" and "t.py::test_x" in findings[0].detail


def test_fresh_launcher_crash_without_tests_run_still_refuses(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "error", "reason": "launcher crashed"})
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and "launcher crashed" in findings[0].detail


def _git_repo(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    return tmp_path, git


@pytest.mark.spawns_process
def test_gate_receipt_names_run_deleted_targets_and_stays_no_verdict(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    root, git = _git_repo(tmp_path)
    (root / "test_gone.py").write_text("def test_g():\n    pass\n", encoding="utf-8")
    (root / "mod.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    base = git("rev-parse", "HEAD")
    git("rm", "-q", "test_gone.py")
    git("commit", "-q", "-m", "delete")
    record = {"run_id": "r1", "head": git("rev-parse", "HEAD"), "status": "no-tests", "tests": [],
              "tests_run": 0, "dropped": ["test_gone.py"], "covered": li.landing_fingerprint(root, ["mod.py"])}
    path = li.fresh_verdict_path(root, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    params = {"run_base_sha": base}
    out = terminal_commit._landing_integrity_gate(root, ["mod.py"], [], None, params, "r1")
    assert out["targeted_tests_check"] == "no-verdict"
    assert "test_gone.py" in out["targeted_tests_note"]


_INV = "coordinator_core/tests/test_inv.py::test_inv"


def _fail_retest(monkeypatch, baseline):
    from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp

    monkeypatch.setattr(imp, "invariants_for_paths", lambda paths, root: [_INV])
    monkeypatch.setattr(imp, "existing_invariants", lambda root, nodes: list(nodes))
    monkeypatch.setattr(li, "fresh_targeted_verdict", lambda root, rid: {
        "status": "fail", "tests": [_INV.split("::")[0]], "baseline": baseline,
    })


def test_invariant_red_on_head_is_credited_foreign_with_its_evidence(tmp_path, monkeypatch):
    _fail_retest(monkeypatch, {"caused": [], "foreign": [_INV], "evidence": [_INV]})
    foreign: dict = {}
    assert li.invariant_findings(tmp_path, ["mod.py"], "r1", {}, foreign) == []
    assert foreign == {_INV: [_INV]}
    findings, check = li.landing_test_gate(tmp_path, {}, "r1", ["mod.py"])
    assert findings.foreign_evidence == {_INV: [_INV]}


def test_invariant_red_without_head_evidence_still_refuses(tmp_path, monkeypatch):
    _fail_retest(monkeypatch, {"caused": [], "foreign": [_INV], "evidence": []})
    foreign: dict = {}
    found = li.invariant_findings(tmp_path, ["mod.py"], "r1", {}, foreign)
    assert [f.reason for f in found] == [li.TARGETED_TESTS_FAILED] and foreign == {}


def test_landing_invariant_nodes_is_the_set_invariant_findings_owes(tmp_path, monkeypatch):
    _fail_retest(monkeypatch, {"caused": [], "foreign": [], "evidence": []})
    assert li.landing_invariant_nodes(tmp_path, ["mod.py"], "r1") == [_INV]
    assert [f.path for f in li.invariant_findings(tmp_path, ["mod.py"], "r1", {})] == [_INV.split("::")[0]]


_RED = "t.py::test_red"
_RUN_BASE = "b" * 40


def _gate_with_baseline(tmp_path, baseline):
    root = _repo(tmp_path)
    _write_record(root, {"status": "fail", "tests": ["t.py"], "baseline": baseline})
    return li.landing_test_gate(root, {"run_base_sha": _RUN_BASE}, "r1")


def test_red_measured_at_the_run_base_lands_as_failing_at_base_advisory(tmp_path):
    findings, check = _gate_with_baseline(
        tmp_path, {"base_sha": _RUN_BASE, "caused": [], "foreign": [_RED], "evidence": []})
    assert check == "pre-existing-only" and list(findings) == []
    assert findings.foreign_evidence == {_RED: [li.FAILING_AT_BASE]}


def test_red_measured_at_the_run_base_but_caused_still_refuses(tmp_path):
    findings, check = _gate_with_baseline(
        tmp_path, {"base_sha": _RUN_BASE, "caused": [_RED], "foreign": [], "evidence": []})
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_foreign_red_measured_at_another_sha_and_unevidenced_still_refuses(tmp_path):
    findings, check = _gate_with_baseline(
        tmp_path, {"base_sha": "c" * 40, "caused": [], "foreign": [_RED], "evidence": []})
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED
    assert "base check owed" in findings[0].detail


def test_pre_base_sha_record_with_matching_evidence_still_lands(tmp_path):
    findings, check = _gate_with_baseline(
        tmp_path, {"caused": [], "foreign": [_RED], "evidence": [_RED]})
    assert check == "pre-existing-only" and list(findings) == []


def test_run_landing_paths_drops_deleted_and_the_record(tmp_path):
    rec = tmp_path / "state" / "rec.md"
    got = li.run_landing_paths(tmp_path, ["a.py", "gone.py", "state/rec.md"], ["gone.py"], rec)
    assert got == ["a.py"]
    assert li.run_landing_paths(tmp_path, ["a.py", "gone.py"], []) == ["a.py", "gone.py"]


def test_owed_invariant_branch_keeps_the_gate_findings_shape(tmp_path, monkeypatch):
    owed = [li.Finding("test_inv.py", li.TARGETED_TESTS_FAILED, "invariant owed")]
    monkeypatch.setattr(li, "invariant_findings", lambda *a, **k: owed)
    findings, check = li.landing_test_gate(tmp_path, {}, None, ["mod.py"])
    assert isinstance(findings, li.GateFindings)
    assert findings.deleted_note == ""
    assert check == "failed"
    assert list(findings)[-1] == owed[0]


def test_terminal_commit_gate_returns_owed_invariant_findings_without_raising(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    owed = li.Finding("test_inv.py", li.TARGETED_TESTS_FAILED, "invariant owed")
    monkeypatch.setattr(li, "invariant_findings", lambda *a, **k: [owed])
    out = terminal_commit._landing_integrity_gate(tmp_path, ["mod.py"], [], None, {}, "r1")
    assert out["targeted_tests_check"] == "failed"
    assert "refused" not in out
    assert {"path": "test_inv.py", "reason": li.TARGETED_TESTS_FAILED, "detail": "invariant owed"} in out["advisory"]


_ALLOWLIST_NODE = (
    "coordinator_core/session/tests/test_machinery_paths.py"
    "::test_every_tracked_state_first_segment_is_on_the_allowlist"
)
_RUN = "warp-2026-10-27-x-aaaaaa-20261027T000000-bbbbbb"


def _invariant_repo(tmp_path):
    _w(tmp_path, _ALLOWLIST_NODE.split("::")[0], "")
    _w(tmp_path, f"state/warp/{_RUN}/run.json", json.dumps({"sizing_rel": "state/sizings/s.yaml"}))
    _w(tmp_path, "state/sizings/s.yaml", "plan: docs/plans/p.md\n")
    return tmp_path


def test_run_artifact_paths_select_no_repo_wide_invariant(tmp_path):
    root = _invariant_repo(tmp_path)
    own = [f"state/warp/{_RUN}/run.json", f"state/warp/{_RUN}/notes.jsonl", "state/sizings/s.yaml"]
    assert li.landing_invariant_nodes(root, own, _RUN) == []


def test_a_chunk_written_state_path_still_selects_the_allowlist_invariant(tmp_path):
    root = _invariant_repo(tmp_path)
    landed = [f"state/warp/{_RUN}/run.json", "state/sizings/other.yaml", f"state/warp/{_RUN}-sibling/x.json"]
    assert li.landing_invariant_nodes(root, landed, _RUN) == [_ALLOWLIST_NODE]


def test_a_run_json_sizing_rel_outside_state_sizings_exempts_nothing(tmp_path):
    root = _invariant_repo(tmp_path)
    _w(root, f"state/warp/{_RUN}/run.json", json.dumps({"sizing_rel": "state/other/x.yaml"}))
    assert li.landing_invariant_nodes(root, ["state/other/x.yaml"], _RUN) == [_ALLOWLIST_NODE]


@pytest.mark.parametrize("detail, module", [
    ("12: imports helper from pkg.mod; pkg/mod.py is untracked", "pkg.mod"),
    ("7: references name from a.b.c; a/b/c.py is uncommitted in the worktree", "a.b.c"),
    ("a detail with no module clause", ""),
])
def test_provenance_entries_name_the_importer_and_module_for_import_closure(detail, module):
    closure = li.Finding("src/user.py", li.IMPORT_CLOSURE_UNTRACKED, detail)
    other = li.Finding("src/user.py", li.PY_UNPARSEABLE, detail)

    got_closure, got_other = li.provenance_entries([closure, other])

    assert got_closure == {"path": "src/user.py", "reason": li.IMPORT_CLOSURE_UNTRACKED, "detail": detail,
                           "importer": "src/user.py", "module": module}
    assert "importer" not in got_other and "module" not in got_other


def _packed_repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    git("init", "-q")
    _w(tmp_path, "a.txt", "one\n")
    git("add", "a.txt")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c1")
    git("repack", "-adq")
    git("prune-packed")
    return tmp_path


def test_since_base_findings_names_an_unreadable_pack_index(tmp_path):
    root = _packed_repo(tmp_path)
    idx = next((root / ".git" / "objects" / "pack").glob("*.idx"))
    idx.chmod(0o666)
    idx.write_bytes(b"not an index")

    got = li.since_base_findings(root, ["a.txt"], "0" * 40, run_id=None)

    assert [(f.path, f.reason) for f in got] == [("a.txt", li.UNREADABLE_PACK_INDEX)]
    assert idx.name in got[0].detail


def test_since_base_findings_readable_pack_index_has_no_such_finding(tmp_path):
    root = _packed_repo(tmp_path)

    assert li.since_base_findings(root, ["a.txt"], "0" * 40, run_id=None) == []


def _measured_with_peer(root: Path, run_id: str, record: dict) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

    path = measured_rev_record_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _red_body(monkeypatch):
    from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

    monkeypatch.setattr(
        li, "_landing_test_gate_body",
        lambda *a, **k: ([ltt.Finding("a.py", ltt.TARGETED_TESTS_FAILED, "boom")], "failed"),
    )


def test_gate_names_the_peer_owner_of_a_declared_path_in_a_red_finding(tmp_path, monkeypatch):
    _red_body(monkeypatch)
    _measured_with_peer(tmp_path, "r1", {"peer_hunks": {"a.py": {"owner": "peer-run", "lines": "1-2", "split": False}}})
    findings, check = li._landing_test_gate(tmp_path, {}, "r1")
    assert check == "failed" and "peer-run" in findings[0].detail and "a.py" in findings[0].detail


def test_gate_leaves_the_red_finding_unnamed_without_peer_hunks(tmp_path, monkeypatch):
    _red_body(monkeypatch)
    _measured_with_peer(tmp_path, "r1", {"sha": "x"})
    findings, _ = li._landing_test_gate(tmp_path, {}, "r1")
    assert findings[0].detail == "boom"
    assert li._landing_test_gate(tmp_path, {}, None)[0][0].detail == "boom"
