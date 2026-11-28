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


def test_host_verify_keeps_the_leg_stamp_on_the_verdict_and_stamps_the_all_evicted_file(tmp_path, monkeypatch):
    stamp = {"measured_at": "2026-11-24T20:00:00+00:00", "head_sha": "c" * 40}

    def fake(repo, output, artifacts, targets, base, timeout, baseline_output=None):
        return 0, {"status": "pass", "tests_run": 1, "tests_failed": 0, **stamp}

    monkeypatch.setattr(headless, "_run_pinned_test_leg", fake)
    params = _evicted_run(tmp_path, ["tests/test_a.py"], [])
    result = headless._verify_test_leg(params, str(tmp_path), tmp_path / "art", 60, run_id="r1")
    assert {k: result[k] for k in stamp} == stamp

    params = _evicted_run(tmp_path / "all", ["tests/test_a.py"], ["tests/test_a.py"])
    result = headless._verify_test_leg(params, str(tmp_path / "all"), tmp_path / "art", 60, run_id="r1")
    saved = json.loads((tmp_path / "all" / "state/warp/r1/test-leg.json").read_text(encoding="utf-8"))
    assert result["measured_at"] and "head_sha" in result
    assert saved["measured_at"] == result["measured_at"] and saved["head_sha"] == result["head_sha"]


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


def _infra_run(tmp_path, targets, measured, monkeypatch):
    def fake(repo, output, artifacts, targets_, base, timeout, baseline_output=None):
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "test-leg-start.json").write_text(json.dumps({"argv": ["py", "--", *targets_]}), encoding="utf-8")
        return 0, measured

    monkeypatch.setattr(headless, "_run_pinned_test_leg", fake)
    params = _evicted_run(tmp_path, targets, [])
    return headless._verify_test_leg(params, str(tmp_path), tmp_path / "art", 60, run_id="r1")


def test_host_verify_maps_pytest_exit_4_to_typed_test_infra_failure(tmp_path, monkeypatch):
    """e60147: pytest exit 4 on a missing named target read as a plain error with no named cause."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_here.py").write_text("", encoding="utf-8")
    measured = {"status": "error", "tests_run": 0, "tests_failed": 0,
                "pytest_phases": [{"exit_code": 4, "stderr": "", "stdout": ""}]}
    result = _infra_run(tmp_path, ["tests/test_here.py", "tests/test_gone.py"], measured, monkeypatch)
    infra = result["test_infra_failure"]
    assert result["status"] == "error" and infra["kind"] == "test_infra_failure"
    assert infra["pytest_exit_code"] == 4 and infra["bad_targets"] == ["tests/test_gone.py"]
    assert infra["argv"][-2:] == ["tests/test_here.py", "tests/test_gone.py"]
    assert "tests/test_gone.py" in result["summary"]


def test_host_verify_maps_pytest_exit_5_and_all_missing_to_typed_test_infra_failure(tmp_path, monkeypatch):
    measured = {"status": "error", "tests_run": 0, "pytest_phases": [{"exit_code": 5}]}
    result = _infra_run(tmp_path, ["tests/test_a.py"], measured, monkeypatch)
    assert result["status"] == "error" and result["test_infra_failure"]["pytest_exit_code"] == 5

    measured = {"status": "error", "tests_run": 0, "missing_targets": ["tests/test_a.py"]}
    result = _infra_run(tmp_path / "m", ["tests/test_a.py"], measured, monkeypatch)
    assert result["status"] == "error" and result["test_infra_failure"]["bad_targets"] == ["tests/test_a.py"]


def test_host_verify_exit_4_never_downgrades_a_run_that_ran_tests(tmp_path, monkeypatch):
    measured = {"status": "pass", "tests_run": 3, "pytest_phases": [{"exit_code": 0}]}
    result = _infra_run(tmp_path, ["tests/test_a.py"], measured, monkeypatch)
    assert "test_infra_failure" not in result and result["status"] == "pass"


def _selected_targets(tmp_path, monkeypatch, scope, owned, edited=("module.py",)):
    repo = tmp_path / "product"
    repo.mkdir()
    for name in ("test_scoped.py", "test_owned.py", "test_wire.py"):
        (repo / name).write_text("def test_x():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(headless, "_invoke_source", lambda *_args: {"inventories": [{
        "edited": list(edited), "created": [], "owned": owned, "tripwires": ["test_wire.py"],
        "tripwires_not_selected": [], "error": None,
    }], "error": None})
    seen = []

    def _leg(_root, _out, _artifacts, selected, *_rest):
        seen.extend(selected)
        return 0, {"tests_run": len(selected), "status": "pass", "status_after": "pass"}

    monkeypatch.setattr(headless, "_run_pinned_test_leg", _leg)
    params = {"base": "a" * 40, "slice_prefixes": ["prep"], "run_key": "run"}
    if scope is not None:
        params["verify_scope_targets"] = scope
    headless._reverify_test_leg(params, str(repo), tmp_path / "operations", 30)
    return seen


def test_a_declared_verify_scope_replaces_the_whole_owned_inventory(tmp_path, monkeypatch):
    seen = _selected_targets(tmp_path, monkeypatch, {"module.py": ["test_scoped.py"]}, ["test_owned.py"])
    assert seen == ["test_scoped.py", "test_wire.py"]


def test_no_verify_scope_keeps_the_owned_inventory(tmp_path, monkeypatch):
    seen = _selected_targets(tmp_path, monkeypatch, None, ["test_owned.py"])
    assert seen == ["test_owned.py", "test_wire.py"]


def test_a_partly_scoped_edit_set_keeps_the_owned_inventory(tmp_path, monkeypatch):
    seen = _selected_targets(
        tmp_path, monkeypatch, {"module.py": ["test_scoped.py"]}, ["test_owned.py"], edited=("module.py", "other.py"),
    )
    assert seen == ["test_owned.py", "test_scoped.py", "test_wire.py"]


def _base_run(tmp_path, *, pinned, manifest_head):
    run_dir = tmp_path / "state/warp/active"
    run_dir.mkdir(parents=True)
    (tmp_path / "test_product.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps({"test_targets": ["test_product.py"], "head_sha": manifest_head}), encoding="utf-8")
    if pinned:
        (tmp_path / "scratch/warp/active").mkdir(parents=True)
        (tmp_path / "scratch/warp/active/run-base").write_text(pinned + "\n", encoding="utf-8")


def _verify_with_base(tmp_path, base):
    return headless._verify_test_leg(
        {"targets": ["test_product.py"], "out_path": "state/warp/active/test-leg.json", "base": base},
        str(tmp_path), tmp_path / "host-artifacts", 60, run_id="active",
    )


def test_sibling_halt_commit_moving_manifest_head_does_not_refuse_the_pinned_base(tmp_path):
    run_base, sibling_halt = "8" * 40, "6" * 40
    _base_run(tmp_path, pinned=run_base, manifest_head=sibling_halt)
    assert _verify_with_base(tmp_path, run_base)["tests_run"] == 1


def test_a_base_that_is_not_the_pinned_base_refuses_with_a_typed_cause(tmp_path):
    import pytest

    _base_run(tmp_path, pinned="8" * 40, manifest_head="6" * 40)
    with pytest.raises(headless.RunBaseMismatch, match="run_base_mismatch") as info:
        _verify_with_base(tmp_path, "1" * 40)
    assert info.value.pinned == ["8" * 40]
