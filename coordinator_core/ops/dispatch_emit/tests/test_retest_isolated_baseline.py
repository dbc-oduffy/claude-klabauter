"""``retest_targeted --isolated-export`` writes ``baseline.caused`` the landing gate folds, and
``_failed_node_ids`` keeps parametrised ids whole. A stub runner and stub exports stand in for pytest/git."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_export as le
from coordinator_core.ops.dispatch_emit import landing_retest, landing_targeted_tests, retest_targeted

RUN_ID = "run-base"
PARAM = "test_a.py::test_x[## status: done]"
PRE = "test_a.py::test_pre"
CAUSED = "test_a.py::test_new"


def _setup(tmp_path: Path, monkeypatch, *, export_red: list[str], head_red: list[str], head_broken=False,
           head_counts=None, export_counts=None, extra_files=(), collect_rc=0, head_files=("test_a.py",),
           head_collect_error=False, head_ran_short=0, land=("src/own.py",), dirty_files=(),
           foreign_reds_export=()):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = tmp_path / "repo"
    root.mkdir()
    head_counts = {"test_a.py": 2} if head_counts is None else head_counts
    export_counts = {"test_a.py": 2, **{f: 2 for f in extra_files}} if export_counts is None else export_counts
    for f in ("test_a.py", *extra_files):
        (root / f).write_text("", encoding="utf-8")
    if land is not None:
        rec = root / "state" / "warp" / RUN_ID
        rec.mkdir(parents=True)
        (rec / "manifest.json").write_text(json.dumps({"review_declared_paths": list(land)}), encoding="utf-8")
    for f in (*(land or ()), *dirty_files):
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("x", encoding="utf-8")
    params = {"run_id": RUN_ID, "targeted_test_paths": ["test_a.py", *extra_files]}
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    built: list[Path] = []
    exported: list[list[str]] = []

    def _build(worktree, out_dir, paths):
        Path(out_dir).mkdir(parents=True)
        for f in head_files if Path(out_dir).name == "export-head" else ("test_a.py", *extra_files):
            (Path(out_dir) / f).write_text("", encoding="utf-8")
        built.append(Path(out_dir))
        exported.append(list(paths))
        for f in paths:
            (Path(out_dir) / f).parent.mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / f).write_text("x", encoding="utf-8")
        return le.LandingExport(Path(out_dir).as_posix(), "h" * 40, (), ())

    monkeypatch.setattr(le, "build_head_export", _build)

    def _run(argv, cwd, *, log=None, timeout=None, append=False):
        cwd = Path(cwd)
        if cwd.name == "export" and any((cwd / f).exists() for f in foreign_reds_export):
            red, code = [*export_red, CAUSED], 1
        elif cwd.name == "export-head":
            red, code = head_red, (2 if head_broken else (1 if head_red else 0))
        elif cwd.name == "export":
            red, code = export_red, 1 if export_red else 0
        else:
            red, code = [], 0
        if "--collect-only" in argv:
            counts = head_counts if cwd.name == "export-head" else export_counts
            lines = [f"{f}::t{k}\n" for f, n in counts.items() for k in range(n)]
            if cwd.name == "export-head" and head_collect_error:
                lines.append("ERROR collecting test_a.py\n")
            with open(log, "ab" if append else "wb") as sink:
                sink.write("".join(lines).encode())
            return 2 if cwd.name == "export-head" and head_collect_error else collect_rc
        with open(log, "ab" if append else "wb") as sink:
            sink.write("".join(f"FAILED {n} - AssertionError: boom\n" for n in red).encode())
            if cwd.name == "export-head":
                total = sum(head_counts.values()) - head_ran_short
                sink.write(f"{total - len(red)} passed, {len(red)} failed in 0.1s\n".encode())
        return code

    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root), "--isolated-export", "--detached-child"],
        run=_run, dirty=lambda r: list(dirty_files),
    )
    record = json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))
    _setup.exported = exported
    return rc, record, built


def test_pre_existing_red_folds_to_pre_existing_only(tmp_path, monkeypatch):
    rc, record, built = _setup(tmp_path, monkeypatch, export_red=[PARAM, PRE], head_red=[PARAM, PRE])
    assert rc == 1 and record["baseline"] == {
        "method": "isolated-export", "caused": [], "foreign": [PARAM, PRE], "evidence": sorted([PARAM, PRE]),
    }
    assert record["export"]["failed_tests"] == [PARAM, PRE]
    assert landing_targeted_tests.failed_targeted_tests(record) == ([], "pre-existing-only")
    assert not built[-1].exists()


def test_landing_caused_red_yields_targeted_tests_failed(tmp_path, monkeypatch):
    rc, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE])
    assert record["baseline"]["caused"] == [CAUSED]
    findings, check = landing_targeted_tests.failed_targeted_tests(record)
    assert check == "failed"
    assert [(f.path, f.reason) for f in findings] == [("test_a.py", landing_targeted_tests.TARGETED_TESTS_FAILED)]


def test_broken_head_run_leaves_no_baseline_so_the_gate_refuses(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE], head_red=[], head_broken=True)
    assert "baseline" not in record
    assert record["baseline_skipped"]["reason"] == "HEAD pass error"
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


def test_file_new_at_head_counts_as_caused_and_baseline_is_written(tmp_path, monkeypatch):
    new_red = "test_new.py::test_n"
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE, new_red], head_red=[PRE], extra_files=("test_new.py",))
    assert record["baseline"] == {
        "method": "isolated-export", "caused": [new_red], "foreign": [PRE], "evidence": [PRE],
    }
    assert "baseline_skipped" not in record


def test_existing_file_collecting_zero_at_head_yields_no_baseline(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[], head_counts={})
    assert "baseline" not in record
    assert record["baseline_skipped"]["files"] == {"test_a.py": {"head": 0}}
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


def test_one_existing_file_collecting_zero_at_head_is_named(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], extra_files=("test_b.py",),
        head_files=("test_a.py", "test_b.py"), head_counts={"test_a.py": 2},
    )
    assert "baseline" not in record
    assert "test_b.py" in record["baseline_skipped"]["reason"]


def test_new_id_in_existing_file_is_caused_when_red_and_baseline_is_written(tmp_path, monkeypatch):
    new = "test_a.py::t2"
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[PRE, new], head_red=[PRE], head_counts={"test_a.py": 2},
        export_counts={"test_a.py": 3},
    )
    assert record["baseline"] == {
        "method": "isolated-export", "caused": [new], "foreign": [PRE], "evidence": [PRE],
    }
    assert "baseline_skipped" not in record


def test_removed_id_is_ignored_and_baseline_is_written(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], head_counts={"test_a.py": 3},
        export_counts={"test_a.py": 2},
    )
    assert record["baseline"] == {
        "method": "isolated-export", "caused": [CAUSED], "foreign": [PRE], "evidence": [PRE],
    }


def test_head_collection_error_in_existing_file_writes_no_baseline_and_names_it(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], head_collect_error=True)
    assert "baseline" not in record
    assert "test_a.py" in record["baseline_skipped"]["reason"]
    assert record["baseline_skipped"]["files"]["test_a.py"]["collection_error"] is True


def test_head_run_that_ran_fewer_than_it_collected_writes_no_baseline(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], head_ran_short=1)
    assert "baseline" not in record
    assert "did not run what it collected" in record["baseline_skipped"]["reason"]


def test_equal_zero_counts_on_both_sides_yield_no_baseline(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], head_counts={}, export_counts={},
    )
    assert "baseline" not in record
    assert record["baseline_skipped"]["reason"] == "HEAD pass collected 0 items"


def test_broken_collect_only_session_yields_no_baseline(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], collect_rc=4)
    assert "baseline" not in record
    assert record["baseline_skipped"]["reason"] == "collection unavailable"


def test_green_export_runs_no_head_pass(tmp_path, monkeypatch):
    _, record, built = _setup(tmp_path, monkeypatch, export_red=[], head_red=[])
    assert "baseline" not in record
    assert all(b.name != "export-head" for b in built)


def test_parametrised_id_with_spaces_is_captured_whole(tmp_path):
    log = tmp_path / "x.log"
    log.write_text(
        f"FAILED {PARAM} - assert 1 == 2\nERROR test_b.py::test_y[a b] \nFAILED test_c.py::test_z\n",
        encoding="utf-8",
    )
    assert retest_targeted._failed_node_ids(log, tmp_path) == [PARAM, "test_b.py::test_y[a b]", "test_c.py::test_z"]


def _export_with(tmp_path: Path, files: tuple[str, ...]) -> Path:
    export = tmp_path / "export"
    for f in files:
        (export / f).parent.mkdir(parents=True, exist_ok=True)
        (export / f).write_text("", encoding="utf-8")
    return export


def _collect(tmp_path, monkeypatch, export, lines):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    seen: list[list[str]] = []

    def _run(argv, cwd, *, log=None, timeout=None, append=False):
        seen.append(argv)
        Path(log).write_text("".join(f"{x}\n" for x in lines), encoding="utf-8")
        return 0

    out = retest_targeted._collect_ids(
        _run, export, [TEST_FILE], tmp_path / "c.log", retest_targeted.time.monotonic() + 60,
    )
    return out, seen


TEST_FILE = "coordinator_core/ops/dispatch_emit/tests/test_terminal_commit.py"


def test_rootdir_relative_collect_log_maps_onto_repo_relative_keys(tmp_path, monkeypatch):
    export = _export_with(tmp_path, (TEST_FILE,))
    rootdir = export / "coordinator_core"
    ids = ["ops/dispatch_emit/tests/test_terminal_commit.py::test_x", "ops/dispatch_emit/tests/test_terminal_commit.py::test_y"]
    out, seen = _collect(tmp_path, monkeypatch, export, [f"rootdir: {rootdir}", *ids])
    assert out[0] == {f"{TEST_FILE}::test_x", f"{TEST_FILE}::test_y"}
    assert f"--rootdir={export}" in seen[0]


def test_pinned_rootdir_collect_log_without_header_is_repo_relative(tmp_path, monkeypatch):
    export = _export_with(tmp_path, (TEST_FILE,))
    out, _ = _collect(tmp_path, monkeypatch, export, [f"{TEST_FILE}::test_x"])
    assert out[0] == {f"{TEST_FILE}::test_x"}


def test_real_pytest_header_line_with_configfile_is_parsed_to_the_directory(tmp_path, monkeypatch):
    export = _export_with(tmp_path, (TEST_FILE,))
    header = f"rootdir: {export / 'coordinator_core'}, configfile: pytest.ini, testpaths: tests"
    out, _ = _collect(
        tmp_path, monkeypatch, export, [header, "ops/dispatch_emit/tests/test_terminal_commit.py::test_x"],
    )
    assert out[0] == {f"{TEST_FILE}::test_x"}


def test_collect_rootdir_outside_the_export_is_a_reason(tmp_path, monkeypatch):
    export = _export_with(tmp_path, (TEST_FILE,))
    out, _ = _collect(tmp_path, monkeypatch, export, [f"rootdir: {tmp_path / 'elsewhere'}", f"{TEST_FILE}::test_x"])
    assert isinstance(out, str) and "outside" in out


def test_collect_ids_that_resolve_to_no_file_are_a_reason(tmp_path, monkeypatch):
    export = _export_with(tmp_path, (TEST_FILE,))
    out, _ = _collect(tmp_path, monkeypatch, export, ["ops/dispatch_emit/tests/test_terminal_commit.py::test_x"])
    assert isinstance(out, str) and "does not resolve" in out


def test_unmappable_collect_log_writes_no_baseline_and_names_the_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(retest_targeted, "_collect_ids", lambda *a, **k: "collect rootdir /x resolves outside /y")
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE])
    assert "baseline" not in record
    assert record["baseline_skipped"]["reason"] == "collect rootdir /x resolves outside /y"


def test_collected_and_failed_ids_share_one_normalised_form(tmp_path, monkeypatch):
    export = _export_with(tmp_path, (TEST_FILE,))
    rootdir = export / "coordinator_core"
    rel = "ops/dispatch_emit/tests/test_terminal_commit.py::test_x"
    collected = retest_targeted._normalise_node_id(rel, rootdir, export)
    log = tmp_path / "f.log"
    log.write_text(f"FAILED {TEST_FILE}::test_x - boom\n", encoding="utf-8")
    assert retest_targeted._failed_node_ids(log, export) == [collected] == [f"{TEST_FILE}::test_x"]
    assert retest_targeted._normalise_node_id("../../x.py::t", export, export) is None


def test_dirty_non_land_file_is_absent_from_export_and_cannot_cause_a_red(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[PRE], head_red=[PRE], dirty_files=("peer/sizing_fire.py",),
        foreign_reds_export=("peer/sizing_fire.py",),
    )
    assert _setup.exported[0] == ["src/own.py"]
    assert record["baseline"] == {
        "method": "isolated-export", "caused": [], "foreign": [PRE], "evidence": [PRE],
    }


def test_record_lists_the_exported_set(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[], head_red=[], dirty_files=("peer/x.py",))
    assert record["isolated_export"]["files"] == ["src/own.py"]


def test_underivable_landing_set_writes_no_baseline_and_no_export(tmp_path, monkeypatch):
    rc, record, built = _setup(
        tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], land=None, dirty_files=("peer/x.py",),
    )
    assert rc == 2 and not built and "baseline" not in record
    assert "not derivable" in record["baseline_skipped"]["reason"]
    assert record["status"] == "error"
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"
