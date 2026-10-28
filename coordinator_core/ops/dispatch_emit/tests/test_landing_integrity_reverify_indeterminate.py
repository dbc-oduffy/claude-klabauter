"""An indeterminate or errored re-verify is an engine defect finding, never fail or pre-existing-only."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import landing_targeted_tests

IND = landing_targeted_tests.REVIEW_REVERIFY_INDETERMINATE


def _params(**reverify) -> dict:
    return {"inline_review": {landing_targeted_tests.REVERIFY_KEY: reverify}}


def test_constants():
    assert landing_targeted_tests.REVERIFY_INDETERMINATE == "indeterminate"
    assert IND == "review-reverify-indeterminate"


def test_indeterminate_record_gives_one_finding(tmp_path):
    p = _params(status_before="indeterminate", status_after=None, reverify_error="warm dispatch indeterminate")
    found = landing_targeted_tests.reverify_red_findings(tmp_path, p, ["a.py"])
    assert len(found) == 1 and found[0].reason == IND and found[0].path == ""
    assert "warm dispatch indeterminate" in found[0].detail and "engine defect" in found[0].detail


def test_indeterminate_without_error_names_none_recorded(tmp_path):
    found = landing_targeted_tests.reverify_red_findings(tmp_path, _params(status_before="indeterminate"), [])
    assert len(found) == 1 and "no error recorded" in found[0].detail


def test_reverify_error_alone_gives_one_finding(tmp_path):
    found = landing_targeted_tests.reverify_red_findings(tmp_path, _params(status_before="pass", reverify_error="boom"), [])
    assert len(found) == 1 and found[0].reason == IND and "boom" in found[0].detail


def test_blank_error_and_pass_gives_none(tmp_path):
    assert landing_targeted_tests.reverify_red_findings(tmp_path, _params(status_before="pass", reverify_error="  "), []) == []


def test_fail_record_still_reviews_as_failed(tmp_path):
    found = landing_targeted_tests.reverify_red_findings(tmp_path, _params(status_before="fail", failing=[]), [])
    assert len(found) == 1 and found[0].reason == landing_targeted_tests.REVIEW_REVERIFY_FAILED


def test_failed_targeted_tests_indeterminate_is_failed_not_preexisting():
    for extra in ({}, {"tests_run": 0}, {"tests_failed": 0}, {"baseline": {"caused": []}}):
        verdict = {"status": "fail", "status_before": "indeterminate", **extra}
        findings, check = landing_targeted_tests.failed_targeted_tests(verdict)
        assert check == "failed" and [f.reason for f in findings] == [IND]


def test_gate_never_launders_indeterminate_with_explicit_empty_failed_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(landing_retest, "_retest_record_status", lambda *a, **k: "pass")
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


def test_gate_plain_fail_with_failing_files_and_no_baseline_is_a_caused_finding(tmp_path):
    params = {
        "targeted_tests": {"status": "fail", "status_before": "fail"},
        "failed_tests": ["pkg/tests/test_x.py::t"],
    }
    findings, check = li.landing_test_gate(tmp_path, params, "run1")
    assert check == "failed" and [f.path for f in findings] == ["pkg/tests/test_x.py"]
    assert "counted caused" in findings[0].detail


RUN = "run-ind"


def _gate(repo, record, targeted=("tests/test_mod.py",)):
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import _git

    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    if record is not None:
        import json

        path = landing_retest.fresh_verdict_path(repo, RUN)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"run_id": RUN, "head": _git(["rev-parse", "HEAD"], repo), "tests": ["tests/test_mod.py::t"],
                "covered": landing_retest.landing_fingerprint(repo, ["mod.py"]), **record}
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


def _advisory_reasons(out) -> list:
    return [a["reason"] for a in out.get("advisory", [])]


def test_passing_retest_omitting_a_targeted_path_keeps_the_advisory(repo):
    out = _gate(repo, {"status": "pass"}, targeted=("tests/test_mod.py", "tests/test_other.py"))
    assert "refused" not in out
    assert _advisory_reasons(out) == [IND]


def test_dropped_nonexistent_targeted_path_does_not_block_clearing(repo):
    out = _gate(repo, {"status": "pass", "dropped": ["tests/test_other.py"]},
                targeted=("tests/test_mod.py", "tests/test_other.py"))
    assert "refused" not in out


def test_absent_retest_record_keeps_the_advisory(repo):
    out = _gate(repo, None)
    assert "refused" not in out
    assert _advisory_reasons(out) == [IND]


def test_stale_running_and_failed_retest_records_keep_the_advisory(repo):
    for record in ({"status": "pass", "stale": True}, {"status": "running"}, {"status": "fail"}):
        out = _gate(repo, record)
        assert "refused" not in out, record
        assert IND in _advisory_reasons(out), record


def test_retest_with_drifted_head_but_unchanged_covered_bytes_still_clears(repo):
    out = _gate(repo, {"status": "pass", "head": "0" * 40})
    assert out["targeted_tests_check"] == "pass-on-retest"
    assert "refused" not in out and not _advisory_reasons(out)


def test_retest_whose_covered_bytes_changed_keeps_the_advisory(repo):
    out = _gate(repo, {"status": "pass", "covered": {"mod.py": "0" * 64}})
    assert "refused" not in out
    assert _advisory_reasons(out) == [IND]


def test_retest_with_drifted_head_and_no_covered_files_keeps_the_advisory(repo):
    out = _gate(repo, {"status": "pass", "head": "0" * 40, "covered": {}})
    assert "refused" not in out
    assert _advisory_reasons(out) == [IND]


def test_real_commits_unrelated_keeps_fresh_covered_touching_stales(repo):
    import json

    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import _git

    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    record = {"run_id": RUN, "status": "pass", "head": _git(["rev-parse", "HEAD"], repo),
              "covered": li.landing_fingerprint(repo, ["mod.py"])}
    path = li.fresh_verdict_path(repo, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    (repo / "other.txt").write_text("unrelated\n", encoding="utf-8")
    _git(["add", "other.txt"], repo)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "unrelated"], repo)
    assert record["head"] != _git(["rev-parse", "HEAD"], repo)
    assert li.fresh_targeted_verdict(repo, RUN) is not None
    (repo / "mod.py").write_text("x = 2\n", encoding="utf-8")
    _git(["add", "mod.py"], repo)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "covered"], repo)
    assert li.fresh_targeted_verdict(repo, RUN) is None


def test_live_shape_retest_with_dropped_targets_and_no_snapshot_clears(repo):
    out = _gate(
        repo,
        {"status": "pass", "dropped": ["tests/test_gone.py", "tests/test_gone2.py"], "note": "dropped nonexistent"},
        targeted=("tests/test_mod.py", "tests/test_gone.py", "tests/test_gone2.py"),
    )
    assert "refused" not in out
