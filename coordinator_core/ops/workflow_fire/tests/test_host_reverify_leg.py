"""The host runs reverify outside the worker's shell deadline."""

import json
import time

from coordinator_core.ops.workflow_fire import headless


def test_run_diff_uses_host_test_receipt_after_worker_timeout(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    repo.mkdir()
    engine = tmp_path / "engine"
    module = engine / "coordinator_core" / "ops" / "test_leg_run.py"
    module.parent.mkdir(parents=True)
    for package in (module.parent, module.parent.parent):
        (package / "__init__.py").touch()
    module.write_text(
        "import json, pathlib, sys, pytest\n"
        "rc = pytest.main(sys.argv[sys.argv.index('--')+1:])\n"
        "assert rc == 0\n"
        "pathlib.Path(sys.argv[sys.argv.index('--out')+1]).write_text("
        "json.dumps(dict(status='pass', status_after='pass', tests_run=1)))\n"
        "pathlib.Path(sys.argv[sys.argv.index('--base-export-out')+1]).write_text("
        "'FAILED test_reviewed.py::test_other\\n{}\\n')\n",
        encoding="utf-8",
    )
    (repo / "test_reviewed.py").write_text(
        "import time\ndef test_long_leg():\n    time.sleep(10.2)\n", encoding="utf-8",
    )
    (repo / "delivery.md").touch()
    monkeypatch.setattr(headless, "_executor_root", lambda: engine)
    monkeypatch.setattr(headless, "_invoke_source", lambda *_args: {"inventories": [{
        "edited": ["module.py", "test_reviewed.py"], "created": [],
        "owned": ["test_reviewed.py", "delivery.md"], "tripwires": [],
        "tripwires_not_selected": [], "error": None, "derived_from": "run_diff",
    }], "error": None})
    start = time.monotonic()
    result = headless._reverify_test_leg(
        {"base": "a" * 40, "slice_prefixes": ["prep"], "run_key": "run"},
        str(repo), tmp_path / "operations", 30,
    )
    assert time.monotonic() - start > 10
    assert result["edited"] == ["module.py", "test_reviewed.py"]
    assert result["status_before"] == "pass"
    assert result["tests_run"] == 1
    assert result["not_run"] == ["delivery.md"]
    assert json.loads((repo / result["sidecar_path"]).read_text())["tests_run"] == 1

    assert (repo / "scratch/reverify-base-export-run.txt").read_text().startswith("FAILED ")


def test_host_verify_uses_typed_tally_and_keeps_raw_phase_output(tmp_path):
    path = tmp_path / "test_product.py"
    path.write_text("def test_ok():\n    assert True\ndef test_red():\n    assert False\n", encoding="utf-8")
    run_dir = tmp_path / "state/warp/verify-45637a"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"test_targets": ["test_product.py"]}), encoding="utf-8")
    result = headless._verify_test_leg(
        {"targets": ["test_product.py"], "out_path": "state/warp/verify-45637a/test-leg.json"},
        str(tmp_path), tmp_path / "host-artifacts", 60, run_id="verify-45637a",
    )
    assert result["status"] == "fail"
    assert (result["tests_run"], result["tests_failed"]) == (2, 1)
    assert "pytest_phases" not in result
    receipt = json.loads((tmp_path / result["sidecar_path"]).read_text(encoding="utf-8"))
    phase, = receipt["pytest_phases"]
    assert phase["exit_code"] == 1
    assert phase["encoding"] == "utf-8"
    assert "1 failed, 1 passed" in phase["stdout"]


def test_host_verify_rejects_other_run_and_pytest_argument_injection(tmp_path):
    import pytest

    run_dir = tmp_path / "state/warp/active"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.json"
    for target in ("@arguments", "test_product.py::node\n--help"):
        manifest.write_text(json.dumps({"test_targets": [target]}))
        with pytest.raises(ValueError, match="repository-relative"):
            headless._verify_test_leg(
                {"targets": [target], "out_path": "state/warp/active/test-leg.json"},
                str(tmp_path), tmp_path / "host-artifacts", 60, run_id="active",
            )
    with pytest.raises(ValueError, match="active run"):
        headless._verify_test_leg(
            {"targets": [], "out_path": "state/warp/foreign/test-leg.json"},
            str(tmp_path), tmp_path / "host-artifacts", 60, run_id="active",
        )


def test_review_reverify_batches_prefixes_into_one_reply(tmp_path):
    from coordinator_core.ops import review_reverify

    reply = review_reverify._handler(
        {"base": "a" * 40, "slice_prefixes": ["p0", "p1"], "session_id": "s"}, tmp_path,
    )
    assert reply["error"] is None
    assert [r["error"] for r in reply["inventories"]] == [
        "no pre-review snapshot for p0", "no pre-review snapshot for p1",
    ]
    assert review_reverify._handler({"slice_prefixes": "p0"}, tmp_path)["error"]


def _evicted_run(tmp_path, targets, evicted):
    run_dir = tmp_path / "state/warp/r1"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"test_targets": targets}), encoding="utf-8")
    (run_dir / "evicted.json").write_text(
        json.dumps({p: {"blob": "0" * 40, "head": None} for p in evicted}), encoding="utf-8",
    )
    return {"targets": targets, "out_path": "state/warp/r1/test-leg.json"}


def test_host_verify_drops_evicted_targets_and_all_evicted_is_not_a_failure(tmp_path, monkeypatch):
    seen = []

    def fake(repo, output, artifacts, targets, base, timeout, baseline_output=None):
        seen.append(list(targets))
        return 0, {"status": "pass", "tests_run": 1, "tests_failed": 0}

    monkeypatch.setattr(headless, "_run_pinned_test_leg", fake)
    params = _evicted_run(tmp_path, ["tests/test_a.py", "tests/test_b.py"], ["tests/test_a.py"])
    result = headless._verify_test_leg(params, str(tmp_path), tmp_path / "art", 60, run_id="r1")
    assert seen == [["tests/test_b.py"]] and result["status"] == "pass-with-skips"
    assert result["evicted_targets"] == ["tests/test_a.py"]
    assert [s["test"] for s in result["skipped"]] == ["tests/test_a.py"]

    seen.clear()
    params = _evicted_run(tmp_path / "all", ["tests/test_a.py"], ["tests/test_a.py"])
    result = headless._verify_test_leg(params, str(tmp_path / "all"), tmp_path / "art", 60, run_id="r1")
    assert seen == []
    assert result["status"] == "pass-with-skips" and result["tests_run"] == 0
    assert result["evicted_targets"] == ["tests/test_a.py"]


def test_host_verify_reports_a_missing_declared_target_as_a_skip_not_a_pass(tmp_path, monkeypatch):
    """0db00a: 19 tests passed while the run's declared regression test was absent from the tree."""
    def fake(repo, output, artifacts, targets, base, timeout, baseline_output=None):
        return 0, {"status": "pass", "tests_run": 19, "tests_failed": 0,
                   "missing_targets": ["tests/test_memory.py"]}

    monkeypatch.setattr(headless, "_run_pinned_test_leg", fake)
    params = _evicted_run(tmp_path, ["tests/test_a.py", "tests/test_memory.py"], [])
    result = headless._verify_test_leg(params, str(tmp_path), tmp_path / "art", 60, run_id="r1")
    assert result["status"] == "pass-with-skips" and result["tests_run"] == 19
    assert [s["test"] for s in result["skipped"]] == ["tests/test_memory.py"]
