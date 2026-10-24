"""An indeterminate or errored re-verify is an engine defect finding, never fail or pre-existing-only."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import landing_integrity as li

IND = li.REVIEW_REVERIFY_INDETERMINATE


def _params(**reverify) -> dict:
    return {"inline_review": {li.REVERIFY_KEY: reverify}}


def test_constants():
    assert li.REVERIFY_INDETERMINATE == "indeterminate"
    assert IND == "review-reverify-indeterminate"


def test_indeterminate_record_gives_one_finding(tmp_path):
    p = _params(status_before="indeterminate", status_after=None, reverify_error="warm dispatch indeterminate")
    found = li.reverify_red_findings(tmp_path, p, ["a.py"])
    assert len(found) == 1 and found[0].reason == IND and found[0].path == ""
    assert "warm dispatch indeterminate" in found[0].detail and "engine defect" in found[0].detail


def test_indeterminate_without_error_names_none_recorded(tmp_path):
    found = li.reverify_red_findings(tmp_path, _params(status_before="indeterminate"), [])
    assert len(found) == 1 and "no error recorded" in found[0].detail


def test_reverify_error_alone_gives_one_finding(tmp_path):
    found = li.reverify_red_findings(tmp_path, _params(status_before="pass", reverify_error="boom"), [])
    assert len(found) == 1 and found[0].reason == IND and "boom" in found[0].detail


def test_blank_error_and_pass_gives_none(tmp_path):
    assert li.reverify_red_findings(tmp_path, _params(status_before="pass", reverify_error="  "), []) == []


def test_fail_record_still_reviews_as_failed(tmp_path):
    found = li.reverify_red_findings(tmp_path, _params(status_before="fail", failing=[]), [])
    assert len(found) == 1 and found[0].reason == li.REVIEW_REVERIFY_FAILED


def test_failed_targeted_tests_indeterminate_is_failed_not_preexisting():
    for extra in ({}, {"tests_run": 0}, {"tests_failed": 0}, {"baseline": {"caused": []}}):
        verdict = {"status": "fail", "status_before": "indeterminate", **extra}
        findings, check = li.failed_targeted_tests(verdict)
        assert check == "failed" and [f.reason for f in findings] == [IND]


def test_gate_never_launders_indeterminate_with_explicit_empty_failed_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(li, "_retest_record_status", lambda *a, **k: "pass")
    params = {"targeted_tests": {"status": "fail", "status_before": "indeterminate"}, "failed_tests": []}
    findings, check = li.landing_test_gate(tmp_path, params, "run1")
    assert check == "failed" and [f.reason for f in findings] == [IND]


def test_gate_never_launders_indeterminate_with_unchanged_failing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(li, "_changed_since_base", lambda *a, **k: [])
    params = {
        "targeted_tests": {"status": "fail", "status_before": "indeterminate"},
        "failed_tests": ["pkg/tests/test_x.py::t"],
    }
    findings, check = li.landing_test_gate(tmp_path, params, "run1")
    assert check == "failed" and [f.reason for f in findings] == [IND]


def test_gate_plain_fail_with_unchanged_files_stays_preexisting(tmp_path, monkeypatch):
    monkeypatch.setattr(li, "_changed_since_base", lambda *a, **k: [])
    params = {
        "targeted_tests": {"status": "fail", "status_before": "fail"},
        "failed_tests": ["pkg/tests/test_x.py::t"],
    }
    assert li.landing_test_gate(tmp_path, params, "run1") == ([], "pre-existing-only")


RUN = "run-ind"


def _gate(repo, record, targeted=("tests/test_mod.py",)):
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import _git

    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    if record is not None:
        import json

        path = li.fresh_verdict_path(repo, RUN)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"run_id": RUN, "head": _git(["rev-parse", "HEAD"], repo), "tests": ["tests/test_mod.py::t"],
                "covered": li.landing_fingerprint(repo, ["mod.py"]), **record}
        path.write_text(json.dumps(body), encoding="utf-8")
    params = {"inline_review": _params(status_before="indeterminate",
                                       reverify_error="no pre-review snapshot for x-prep")["inline_review"],
              "targeted_test_paths": list(targeted)}
    return terminal_commit._landing_integrity_gate(repo, ["mod.py"], [], None, params, RUN)


from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import repo  # noqa: E402,F401


def test_fresh_passing_retest_clears_the_indeterminate_refusal(repo):
    out = _gate(repo, {"status": "pass"})
    assert out["targeted_tests_check"] == "pass-on-retest"
    assert "refused" not in out


def test_passing_retest_omitting_a_targeted_path_keeps_refusing(repo):
    out = _gate(repo, {"status": "pass"}, targeted=("tests/test_mod.py", "tests/test_other.py"))
    assert out["refused"] == IND


def test_dropped_nonexistent_targeted_path_does_not_block_clearing(repo):
    out = _gate(repo, {"status": "pass", "dropped": ["tests/test_other.py"]},
                targeted=("tests/test_mod.py", "tests/test_other.py"))
    assert "refused" not in out


def test_absent_retest_record_keeps_refusing(repo):
    assert _gate(repo, None)["refused"] == IND


def test_stale_running_and_failed_retest_records_keep_refusing(repo):
    for record in ({"status": "pass", "stale": True}, {"status": "running"}, {"status": "fail"}):
        out = _gate(repo, record)
        assert out.get("refused"), record


def test_retest_with_drifted_head_keeps_refusing(repo):
    out = _gate(repo, {"status": "pass", "head": "0" * 40})
    assert out["refused"] == IND
