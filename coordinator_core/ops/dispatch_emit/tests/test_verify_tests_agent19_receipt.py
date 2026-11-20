"""Replay the strict-schema failure from DoE fire 83eafd37, agent 19."""

from jsonschema import Draft202012Validator

from coordinator_core.ops.dispatch_emit import ask_compose


def test_verify_tests_projects_the_agent19_receipt_to_the_declared_schema(monkeypatch):
    # Preserve the archived JSON keys and values except for the local warning text.
    receipt = {
        "status_after": "pass",
        "failing": [],
        "failing_detail": [],
        "pre_existing": [],
        "reverted": [],
        "passed_alone": [],
        "passed_at_base": [],
        "diff_named": [],
        "not_a_test": [],
        "over_budget": False,
        "pytest_exit_code": 0,
        "pytest_stderr": "PytestDeprecationWarning",
        "tests_run": 37,
        "tests_failed": 0,
        "status": "pass",
        "elapsed_s": 1.684,
        "budget_s": 540.0,
    }
    monkeypatch.setattr(ask_compose, "_agent", lambda prompt, **options: (prompt, options["schema"]))
    path = "test-result.json"
    prompt, schema = ask_compose._verify_tests_call(
        head="", anchor="", test_js="''", run_id="r", out_path=path,
    )
    validator = Draft202012Validator(schema)
    summary = dict(
        sidecar_path=path,
        **{name: receipt[name] for name in ("status", "tests_run", "tests_failed")},
    )
    assert validator.is_valid(summary)
    assert not validator.is_valid({**receipt, "sidecar_path": path})
    for field in ("pytest_exit_code", "pytest_stderr"):
        assert field not in prompt
    assert "sidecar_path" in prompt
    assert "JSON verbatim" not in prompt
    for field in ("baseline", "baseline_method", "build_clean", "skipped", "summary",
                  "importer_overrun", "tripwires_not_run"):
        assert field in schema["properties"]
        assert field in prompt
