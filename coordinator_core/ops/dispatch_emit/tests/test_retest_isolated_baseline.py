"""``retest_targeted --isolated-export`` writes ``baseline.caused`` the landing gate folds, and
``_failed_node_ids`` keeps parametrised ids whole. A stub runner and stub exports stand in for pytest/git."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_export as le
from coordinator_core.ops.dispatch_emit import landing_retest, landing_targeted_tests, retest_targeted

RUN_ID = "run-base"
PARAM = "test_a.py::test_x[## status: done]"
PRE = "test_a.py::test_pre"
CAUSED = "test_a.py::test_new"
#: Landing checks the child records when the run base is HEAD and no owed id exists.
MEASURED = {"head_red": [], "owed_cleared": [], "head_sha": "h" * 40}


def _setup(tmp_path: Path, monkeypatch, *, export_red: list[str], head_red: list[str], head_broken=False,
           head_counts=None, export_counts=None, extra_files=(), collect_rc=0, head_files=("test_a.py",),
           head_collect_error=False, head_ran_short=0, land=("src/own.py",), dirty_files=(),
           foreign_reds_export=(), run_base_sha=None, head2_red=None):
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
    if run_base_sha:
        params["run_base_sha"] = run_base_sha
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    built: list[Path] = []
    exported: list[list[str]] = []
    shas: list[tuple[str, str | None]] = []

    def _build(worktree, out_dir, paths, sha=None):
        shas.append((Path(out_dir).name, sha))
        shutil.rmtree(out_dir, ignore_errors=True)
        Path(out_dir).mkdir(parents=True)
        for f in head_files if Path(out_dir).name == "export-head" else ("test_a.py", *extra_files):
            (Path(out_dir) / f).write_text("", encoding="utf-8")
        built.append(Path(out_dir))
        exported.append(list(paths))
        for f in paths:
            (Path(out_dir) / f).parent.mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / f).write_text("x", encoding="utf-8")
        return le.LandingExport(Path(out_dir).as_posix(), sha or "h" * 40, (), ())

    monkeypatch.setattr(le, "build_head_export", _build)

    def _run(argv, cwd, *, log=None, timeout=None, append=False):
        cwd = Path(cwd)
        if cwd.name == "export" and any((cwd / f).exists() for f in foreign_reds_export):
            red, code = [*export_red, CAUSED], 1
        elif cwd.name == "export-head":
            red = head2_red if head2_red is not None and [n for n, _ in shas].count("export-head") > 1 else head_red
            code = 2 if head_broken else (1 if red else 0)
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
    _setup.shas = shas
    return rc, record, built


def test_pre_existing_red_folds_to_pre_existing_only(tmp_path, monkeypatch):
    rc, record, built = _setup(tmp_path, monkeypatch, export_red=[PARAM, PRE], head_red=[PARAM, PRE])
    assert rc == 1 and record["baseline"] == {
        "method": "isolated-export", "base_sha": "h" * 40, "caused": [], "foreign": [PARAM, PRE], "evidence": sorted([PARAM, PRE]),
        **MEASURED,
    }
    assert record["export"]["failed_tests"] == [PARAM, PRE]
    assert landing_targeted_tests.failed_targeted_tests(record) == ([], "pre-existing-only")
    assert not built[-1].exists()


def test_baseline_measures_the_run_base_and_records_its_sha(tmp_path, monkeypatch):
    base = "b" * 40
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE], head_red=[PRE], run_base_sha=base)
    assert record["baseline"]["base_sha"] == base
    assert [s for n, s in _setup.shas if n == "export-head"][0] == base
    assert dict(_setup.shas)["export"] is None


def test_baseline_without_a_recorded_run_base_measures_head(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[PRE], head_red=[PRE])
    assert dict(_setup.shas)["export-head"] is None
    assert record["baseline"]["base_sha"] == "h" * 40


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
        "method": "isolated-export", "base_sha": "h" * 40, "caused": [new_red], "foreign": [PRE], "evidence": [PRE],
        **MEASURED,
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
        "method": "isolated-export", "base_sha": "h" * 40, "caused": [new], "foreign": [PRE], "evidence": [PRE],
        **MEASURED,
    }
    assert "baseline_skipped" not in record


def test_removed_id_is_ignored_and_baseline_is_written(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], head_counts={"test_a.py": 3},
        export_counts={"test_a.py": 2},
    )
    assert record["baseline"] == {
        "method": "isolated-export", "base_sha": "h" * 40, "caused": [CAUSED], "foreign": [PRE], "evidence": [PRE],
        **MEASURED,
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
        "method": "isolated-export", "base_sha": "h" * 40, "caused": [], "foreign": [PRE], "evidence": [PRE],
        **MEASURED,
    }


def test_record_lists_the_exported_set(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[], head_red=[], dirty_files=("peer/x.py",))
    assert record["isolated_export"]["files"] == ["src/own.py"]


RED_ID = "test_a.py::t1"


def test_caused_id_red_at_pure_head_is_recorded_when_the_run_base_differs(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[RED_ID], head_red=[], head2_red=[RED_ID], run_base_sha="b" * 40,
    )
    assert record["baseline"]["caused"] == [RED_ID]
    assert record["baseline"]["head_red"] == [RED_ID]
    assert record["baseline"]["head_sha"] == "h" * 40
    assert [s for n, s in _setup.shas if n == "export-head"] == ["b" * 40, None]


def test_caused_id_green_at_pure_head_stays_out_of_head_red(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[RED_ID], head_red=[], head2_red=[], run_base_sha="b" * 40,
    )
    assert record["baseline"]["caused"] == [RED_ID] and record["baseline"]["head_red"] == []


def test_run_base_equal_to_head_reuses_the_base_export(tmp_path, monkeypatch):
    monkeypatch.setattr("coordinator_core.git.git_state.head_sha", lambda *a, **k: "b" * 40)
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[RED_ID], head_red=[], run_base_sha="b" * 40)
    assert [n for n, _ in _setup.shas].count("export-head") == 1
    assert record["baseline"]["head_red"] == [] and record["baseline"]["head_sha"] == "b" * 40


def _checks(tmp_path, monkeypatch, *, ran: int, red: list[str], baseline: dict, base_sha=None, build=None):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    export = tmp_path / "export"
    export.mkdir(exist_ok=True)
    (export / "test_a.py").write_text("", encoding="utf-8")

    def _run(argv, cwd, *, log=None, timeout=None, append=False):
        text = "".join(f"{f}::t{k}\n" for f in ("test_a.py",) for k in range(2)) if "--collect-only" in argv else (
            "".join(f"FAILED {n} - boom\n" for n in red) + f"{ran - len(red)} passed, {len(red)} failed in 0.1s\n"
        )
        Path(log).write_text(text, encoding="utf-8")
        return 0 if "--collect-only" in argv else (1 if red else 0)

    if build is not None:
        monkeypatch.setattr(le, "build_head_export", build)
    return retest_targeted._landing_checks(
        tmp_path, _run, baseline, export, tmp_path / "export-head", tmp_path / "x.head.log",
        retest_targeted.time.monotonic() + 60, "h" * 40, base_sha, "e" * 40,
    )


OWED = {"caused": [], "foreign": [], "evidence": [], "unverified": ["test_a.py::t0", "test_a.py::t1"]}


def test_owed_ids_green_at_head_plus_landing_paths_are_cleared(tmp_path, monkeypatch):
    out = _checks(tmp_path, monkeypatch, ran=2, red=[RED_ID], baseline=OWED)
    assert out["owed_cleared"] == ["test_a.py::t0"] and out["head_sha"] == "h" * 40


def test_owed_run_that_ran_fewer_than_named_records_no_owed_cleared(tmp_path, monkeypatch):
    out = _checks(tmp_path, monkeypatch, ran=1, red=[], baseline=OWED)
    assert "owed_cleared" not in out


def test_head_export_failure_records_no_head_red_and_keeps_the_owed_check(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise le.LandingExportError("no space")

    monkeypatch.setattr("coordinator_core.git.git_state.head_sha", lambda *a, **k: "z" * 40)
    out = _checks(
        tmp_path, monkeypatch, ran=2, red=[], base_sha="b" * 40, build=_boom,
        baseline={"caused": [RED_ID], "foreign": [], "evidence": [], "unverified": ["test_a.py::t0"]},
    )
    assert "head_red" not in out and out["owed_cleared"] == ["test_a.py::t0"] and out["head_sha"] == "e" * 40


def test_underivable_landing_set_writes_no_baseline_and_no_export(tmp_path, monkeypatch):
    rc, record, built = _setup(
        tmp_path, monkeypatch, export_red=[PRE, CAUSED], head_red=[PRE], land=None, dirty_files=("peer/x.py",),
    )
    assert rc == 2 and not built and "baseline" not in record
    assert "not derivable" in record["baseline_skipped"]["reason"]
    assert record["status"] == "error"
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"
