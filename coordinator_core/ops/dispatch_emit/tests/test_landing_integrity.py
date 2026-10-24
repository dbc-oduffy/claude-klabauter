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


@pytest.mark.parametrize("status", ["error", "fail"])
def test_zero_tests_run_is_not_run_never_a_finding(status):
    verdict = {"status": status, "tests_run": 0, "tests_failed": 0, "baseline": None}
    assert li.failed_targeted_tests(verdict) == ([], li.NOT_RUN_CHECK) == ([], "not-run")


@pytest.mark.parametrize("run", [None, "0", False, 3])
def test_non_zero_or_non_int_tests_run_keeps_unattributed_failure(run):
    findings, check = li.failed_targeted_tests({"status": "error", "tests_run": run, "baseline": None})
    assert check == "failed" and findings[0].path == ""


def test_inline_review_run_zero_is_not_run():
    params = {"inline_review": {"tests": {"status": "error", "run": 0, "failed": 0}}}
    assert li.targeted_test_verdict(params)["tests_run"] == 0
    assert li.landing_test_gate(Path("."), params, None) == ([], "not-run")


def test_recorded_not_run_skips_failure_attribution():
    params = {"targeted_tests": {"status": "error", "tests_run": 0, "baseline": None},
              "failed_tests": ["a/test_x.py::t1"]}
    assert li.landing_test_gate(Path("."), params, None) == ([], "not-run")


def test_fresh_retest_verdict_with_zero_run_is_not_run(tmp_path):
    root = _repo(tmp_path)
    path = _write_fresh(root, status="error")
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "tests_run": 0}), encoding="utf-8")
    assert li.landing_test_gate(root, _RECORDED_FAIL, "r1") == ([], "not-run")


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


@pytest.mark.parametrize("breakage", ["head", "covered", "run_id", "corrupt"])
def test_invalid_fresh_verdict_falls_back_to_recorded(tmp_path, breakage):
    root = _repo(tmp_path)
    path = _write_fresh(root, head="b" * 40 if breakage == "head" else _HEAD,
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
    _write_fresh(root, status="pass", head="f" * 40)  # HEAD moved since the retest: record is stale
    params = {**_RECORDED_FAIL, "failed_tests": []}
    assert li.landing_test_gate(root, params, "r1") == ([], "pre-existing-only")


def test_empty_failed_tests_without_a_retest_record_still_refuses(tmp_path):
    root = _repo(tmp_path)
    findings, check = li.landing_test_gate(root, {**_RECORDED_FAIL, "failed_tests": []}, "r1")
    assert check == "failed" and findings


def test_refusal_names_a_drifted_retest_record_and_its_route(tmp_path):
    root = _repo(tmp_path)
    _write_fresh(root, status="pass", head="f" * 40)
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed"
    assert "retest record (pass) no longer matches the tree" in findings[0].detail
    assert li.RETEST_LAUNCHER in findings[0].detail


def test_unchanged_failing_files_since_base_are_pre_existing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    seen = {}

    def fake(worktree_root, base, paths):
        seen["args"] = (base, list(paths))
        return True

    monkeypatch.setattr(li, "_unchanged_since_base", fake)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "failed_tests": ["./t/test_x.py::test_a", "t/test_x.py::test_b"]}
    assert li.landing_test_gate(root, params, None) == ([], "pre-existing-only")
    assert seen["args"] == (B, ["t/test_x.py"])


def test_changed_failing_files_since_base_still_refuse(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(li, "_unchanged_since_base", lambda *a: False)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "failed_tests": ["t/test_x.py::test_a"]}
    findings, check = li.landing_test_gate(root, params, None)
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_failed_tests_without_a_run_base_still_refuse(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(li, "_unchanged_since_base", lambda *a: True)
    params = {**_RECORDED_FAIL, "failed_tests": ["t/test_x.py::test_a"]}
    assert li.landing_test_gate(root, params, None)[1] == "failed"


def test_unchanged_since_base_real_tree_check(tmp_path):
    root = _repo(tmp_path)
    assert li._unchanged_since_base(root, B, ["t.py"]) is False


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
    seen = []
    monkeypatch.setattr(li, "_unchanged_since_base", lambda r, b, paths: seen.append(list(paths)) or False)
    out = _digest(tmp_path, _NAMED)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "task_output_path": out}
    findings, check = li.landing_test_gate(root, params, None)
    assert check == "failed"
    assert [f.path for f in findings] == ["src/lib/mcp/tool-manifest.test.ts", "a/test_y.py"]
    assert "--tests src/lib/mcp/tool-manifest.test.ts" in findings[0].detail
    assert seen[0] == ["src/lib/mcp/tool-manifest.test.ts", "a/test_y.py"]


def test_digest_named_failure_unchanged_since_base_is_pre_existing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(li, "_unchanged_since_base", lambda *a: True)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "task_output_path": _digest(tmp_path, _NAMED)}
    assert li.landing_test_gate(root, params, None) == ([], "pre-existing-only")


def test_only_the_changed_failing_file_refuses(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(li, "_unchanged_since_base", lambda r, b, paths: paths == ["t/test_old.py"])
    params = {**_RECORDED_FAIL, "run_base_sha": B, "failed_tests": ["t/test_old.py::a", "t/test_new.py::b"]}
    findings, check = li.landing_test_gate(root, params, None)
    assert check == "failed" and [f.path for f in findings] == ["t/test_new.py"]
    assert "--tests t/test_new.py" in findings[0].detail


def test_failed_tests_nested_under_params_is_honoured(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(li, "_unchanged_since_base", lambda *a: True)
    params = {**_RECORDED_FAIL, "run_base_sha": B, "params": {"failed_tests": ["t/test_x.py::a"]}}
    assert li.landing_test_gate(root, params, None) == ([], "pre-existing-only")


def test_failed_test_with_no_base_still_refuses(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(li, "_unchanged_since_base", lambda *a: True)
    params = {**_RECORDED_FAIL, "failed_tests": ["t/test_x.py::a"]}
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


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


@pytest.fixture
def closure_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _w(tmp_path, "cc/__init__.py", "")
    _w(tmp_path, "cc/_probe.py", "def _old():\n    pass\n")
    _w(tmp_path, "cc/guard.py", "def f():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_closure_incident_shape_lazy_import_of_worktree_only_package(closure_repo):
    _w(closure_repo, "cc/gm/__init__.py", "")
    _w(closure_repo, "cc/gm/rec.py", "X = 1\n")
    _w(closure_repo, "cc/guard.py", "def f():\n    from cc.gm.rec import X\n    return X\n")
    out = li.import_closure_findings(closure_repo, ["cc/guard.py"], [])
    assert [(f.path, f.reason) for f in out] == [("cc/guard.py", li.IMPORT_CLOSURE_UNTRACKED)]
    assert out[0].detail.startswith("2: ")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_closure_n1_shape_name_bound_only_by_modified_tracked_module(closure_repo):
    _w(closure_repo, "cc/_probe.py", "def _old():\n    pass\n\ndef _offer():\n    pass\n")
    _w(closure_repo, "cc/commit.py", "from cc._probe import _offer\n")
    out = li.import_closure_findings(closure_repo, ["cc/commit.py"], [])
    assert len(out) == 1 and out[0].reason == li.IMPORT_CLOSURE_UNTRACKED and "_offer" in out[0].detail


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_closure_landed_text_without_the_import_yields_none(closure_repo):
    _w(closure_repo, "cc/gm/__init__.py", "")
    _w(closure_repo, "cc/gm/rec.py", "X = 1\n")
    _w(closure_repo, "cc/guard.py", "def f():\n    from cc.gm.rec import X\n    return X\n")
    landed = {"cc/guard.py": "def f():\n    return 2\n"}
    assert li.import_closure_findings(closure_repo, ["cc/guard.py"], [], landed.get) == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_closure_clean_landing_and_deletion_of_imported_module(closure_repo):
    _w(closure_repo, "cc/guard.py", "from cc._probe import _old\n")
    assert li.import_closure_findings(closure_repo, ["cc/guard.py"], []) == []
    (closure_repo / "cc/_probe.py").unlink()
    out = li.import_closure_findings(closure_repo, ["cc/guard.py"], ["cc/_probe.py"])
    assert len(out) == 1 and "deleted by this commit" in out[0].detail


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_closure_detector_exception_fails_closed(closure_repo, monkeypatch):
    from coordinator_core.authoring_leaks import import_closure

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(import_closure, "detect", boom)
    out = li.import_closure_findings(closure_repo, ["cc/guard.py"], [])
    assert len(out) == 1 and out[0].reason == li.IMPORT_CLOSURE_UNTRACKED and "boom" in out[0].detail


def test_zero_fail_no_baseline_names_its_cause_not_targeted_tests_failed():
    verdict = {"status": "fail", "tests_failed": 0, "tests_run": 249, "baseline": None}
    findings, check = li.landing_test_gate(Path("."), {"targeted_tests": verdict}, None)
    assert check == li.ZERO_FAIL_CHECK
    assert [f.reason for f in findings] == [li.ZERO_FAIL_UNATTRIBUTED]
    assert li.TARGETED_TESTS_FAILED not in {f.reason for f in findings}


def test_zero_fail_with_status_before_fail_lands_as_pre_existing():
    verdict = {"status": "fail", "tests_failed": 0, "tests_run": 249, "status_before": "fail"}
    assert li.landing_test_gate(Path("."), {"targeted_tests": verdict}, None) == ([], "pre-existing-only")


def test_status_before_rides_the_inline_review_mapping():
    params = {"inline_review": {"tests": {"status": "fail", "failed": 0, "run": 9, "status_before": "fail"}}}
    assert li.landing_test_gate(Path("."), params, None) == ([], "pre-existing-only")


def test_caused_ids_still_refuse_even_with_status_before_fail():
    verdict = {"status": "fail", "tests_failed": 1, "status_before": "fail",
               "baseline": {"caused": ["t/test_a.py::test_x"]}}
    findings, check = li.failed_targeted_tests(verdict)
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_nonzero_fail_with_no_baseline_still_refuses():
    findings, check = li.failed_targeted_tests({"status": "fail", "tests_failed": 2, "status_before": "fail"})
    assert check == "failed" and findings[0].reason == li.TARGETED_TESTS_FAILED


def _write_record(root, record):
    path = li.fresh_verdict_path(root, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    full = {"run_id": "r1", "head": _HEAD, "covered": li.landing_fingerprint(root, ["t.py"]), **record}
    path.write_text(json.dumps(full), encoding="utf-8")


def test_fresh_no_tests_record_blocks_nothing_and_leaves_failed_tests_remedy_open(tmp_path):
    root = _repo(tmp_path)
    _write_record(root, {"status": "no-tests", "tests_run": 0, "dropped": ["gone.py"], "note": "dropped"})
    assert li.landing_test_gate(root, {}, "r1") == ([], "no-verdict")
    findings, check = li.landing_test_gate(root, _RECORDED_FAIL, "r1")
    assert check == "failed" and f"or pass params.{li.FAILED_TESTS_PARAM}" in findings[0].detail


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
    findings, check = li.landing_test_gate(root, {"failed_tests": ["a/test_x.py::t"]}, "r1")
    assert check == "failed" and "launcher crashed" in findings[0].detail
