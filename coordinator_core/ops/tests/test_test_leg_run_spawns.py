"""test_leg_run over a throwaway repo: the test-shaped verdict, the spawn budget, the one deadline."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.review_reverify import _read_base_export

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FAIL_ID = "tests/test_a.py::test_fails"
PASS_ID = "tests/test_a.py::test_passes"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(
        "def test_passes():\n    assert True\n\ndef test_fails():\n    assert 1 == 2\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _view(argv) -> list[str]:
    """`argv` with an ids-runner spawn read back as `[exe, -m, pytest, *flags, *ids]`; any other argv unchanged."""
    argv = list(argv)
    if tlr._IDS_RUNNER not in argv:
        return argv
    at = argv.index(tlr._IDS_RUNNER)
    ids = [i for i in Path(argv[at + 1]).read_text(encoding="utf-8").split("\n") if i]
    return [*argv[:at - 1], "-m", "pytest", *argv[at + 2:], *ids]


def _spawns(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = subprocess.run

    def counting(argv, *a, **kw):
        view = _view(argv)
        if "pytest" in view:
            seen.append(view)
        return real(argv, *a, **kw)

    monkeypatch.setattr(tlr.subprocess, "run", counting)
    return seen


def _main(argv: list[str], out: Path) -> tuple[int, dict]:
    cut = argv.index("--")
    code = tlr.main([*argv[:cut], "--out", str(out), *argv[cut:]])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_real_base_measure_fills_pre_existing_and_writes_the_export(repo, tmp_path):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_a.py").write_text(
        "from pkg import VALUE\n\ndef test_red_at_base():\n    assert VALUE == 2\n", encoding="utf-8")
    (repo / "tests" / "test_b.py").write_text(
        "from pkg import VALUE\n\ndef test_green_at_base():\n    assert VALUE == 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg" / "__init__.py").write_text("VALUE = 3\n", encoding="utf-8")
    export = tmp_path / "export.txt"
    _code, data = _main(["--base-sha", base, "--base-export-out", str(export), "--", "tests"], tmp_path / "o.json")
    assert sorted(data["failing"]) == ["tests/test_a.py::test_red_at_base", "tests/test_b.py::test_green_at_base"]
    assert data["pre_existing"] == ["tests/test_a.py::test_red_at_base"]
    assert data["passed_at_base"] == ["tests/test_b.py::test_green_at_base"]
    assert _read_base_export(repo, str(export)) == {"tests/test_a.py::test_red_at_base"}


@pytest.fixture
def landing(tmp_path, monkeypatch):
    """A git repo: `pkg/mod.py` with a direct importer test and a pre-existing red; returns (root, base sha).
    The mirror's system Temp is private to the test."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_importer.py").write_text(
        "from pkg import mod\n\ndef test_uses_value():\n    assert mod.VALUE == 1\n\n"
        "def test_always_red():\n    assert False\n", encoding="utf-8")
    (root / "tests" / "test_unrelated.py").write_text("def test_other():\n    assert 1 == 2\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    return root, _git(root, "rev-parse", "HEAD")


def _land(root: Path, rel: str, text: str) -> str:
    (root / rel).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "land")
    return _git(root, "rev-parse", "HEAD")


def test_default_leg_reports_a_direct_importers_new_red_and_spares_a_pre_existing_one(landing, tmp_path):
    root, base = landing
    post = _land(root, "pkg/mod.py", "VALUE = 2\n")
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")  # worktree dirt the clone must not see

    code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "pkg/mod.py", "--", "tests/test_unrelated.py"],
                       tmp_path / "o.json")

    assert code == 0 and data["over_budget"] is False
    assert data["importer_targets"] == ["tests/test_importer.py"]
    assert data["new_red"] == ["tests/test_importer.py::test_uses_value"]
    assert sorted(data["pre_existing"]) == ["tests/test_importer.py::test_always_red", "tests/test_unrelated.py::test_other"]
    assert data["status_after"] == "fail"
    assert (cov.mirror_root(root) / root.name / ".git").is_dir()


def test_default_leg_passes_when_every_red_is_red_at_base(landing, tmp_path):
    root, base = landing
    post = _land(root, "pkg/mod.py", "VALUE = 1\nOTHER = 3\n")

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "pkg/mod.py", "--", "tests/test_unrelated.py"],
                        tmp_path / "o.json")

    assert data["new_red"] == [] and data["status_after"] == "pass"
    assert "tests/test_importer.py::test_always_red" in data["pre_existing"]


def test_a_new_test_red_with_no_counterpart_at_base_is_a_new_red(landing, tmp_path):
    root, base = landing
    (root / "tests" / "test_fresh.py").write_text("def test_fresh():\n    assert False\n", encoding="utf-8")
    post = _land(root, "pkg/mod.py", "VALUE = 1\n# landed\n")

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "tests/test_fresh.py", "--", "tests/test_fresh.py"],
                        tmp_path / "o.json")

    assert data["new_red"] == ["tests/test_fresh.py::test_fresh"]


def test_touched_without_a_base_sha_excuses_no_red(landing, tmp_path):
    root, _base = landing
    post = _land(root, "pkg/mod.py", "VALUE = 1\n# landed\n")

    _code, data = _main(["--head-sha", post, "--touched", "pkg/mod.py", "--", "tests/test_unrelated.py"], tmp_path / "o.json")

    assert "tests/test_importer.py::test_always_red" in data["new_red"] and data["pre_existing"] == []


def test_without_touched_the_leg_keeps_running_in_cwd_and_reports_no_new_red(landing, tmp_path):
    _code, data = _main(["--", "tests/test_unrelated.py"], tmp_path / "o.json")
    assert "new_red" not in data and data["failing"] == ["tests/test_unrelated.py::test_other"]


def _committed_engine_with_dirty_worktree(root: Path) -> str:
    """A base commit whose `coordinator_core/dirty.py` imports fine, then the worktree copy broken; returns the base sha."""
    (root / "coordinator_core").mkdir()
    (root / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "coordinator_core" / "dirty.py").write_text("from coordinator_core.clean import X\n", encoding="utf-8")
    (root / "coordinator_core" / "clean.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests" / "test_dirty.py").write_text(
        "from coordinator_core import dirty\n\ndef test_a():\n    assert True\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "engine")
    (root / "coordinator_core" / "dirty.py").write_text("from coordinator_core.clean import _GONE\n", encoding="utf-8")
    return _git(root, "rev-parse", "HEAD")


def test_a_foreign_import_error_reruns_the_targets_on_the_base_export_and_reports_tests_run(landing, tmp_path):
    root, _base = landing
    base = _committed_engine_with_dirty_worktree(root)

    _code, data = _main(["--base-sha", base, "--", "tests/test_dirty.py"], tmp_path / "o.json")

    assert data["tests_run"] > 0 and data["status"] == "pass"
    assert data["overlay_rerun"]["from"] == "coordinator_core/dirty.py"
    assert data.get("zero_run_cause") != ltt.ZERO_RUN_IMPORT_ERROR


def test_an_owned_import_error_keeps_the_zero_run_import_error_verdict(landing, tmp_path):
    from coordinator_core.ops.dispatch_emit import kept_work, stranded_sibling

    root, _base = landing
    base = _committed_engine_with_dirty_worktree(root)
    blob = kept_work.fingerprint(root, "coordinator_core/dirty.py")
    stranded_sibling.write(root / "state" / "warp" / "run-mine", {"coordinator_core/dirty.py": {"base": None, "run": blob}})

    _code, data = _main(["--base-sha", base, "--", "tests/test_dirty.py"], tmp_path / "o.json")

    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_IMPORT_ERROR
    assert data["import_error"] == {"path": "coordinator_core/dirty.py", "run_id": "run-mine"}
    assert "overlay_rerun" not in data


def test_clone_leg_with_only_absent_targets_is_a_missing_target_zero_run(landing, tmp_path):
    root, _base = landing
    post = _land(root, "README.md", "x\n")
    _code, data = _main(["--head-sha", post, "--touched", "README.md", "--", "tests/nonexistent_dir_placeholder.py"],
                        tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_MISSING_TARGET
    assert data["missing_targets"] == ["tests/nonexistent_dir_placeholder.py"]


def test_clone_leg_runs_the_present_target_and_records_the_absent_one(landing, tmp_path):
    root, _base = landing
    post = _land(root, "README.md", "x\n")
    _code, data = _main(["--head-sha", post, "--touched", "README.md", "--", "tests/test_unrelated.py", "tests/gone.py"],
                        tmp_path / "o.json")
    assert data["missing_targets"] == ["tests/gone.py"]
    assert data.get("zero_run_cause") != ltt.ZERO_RUN_MISSING_TARGET and data["tests_run"] == 1


def test_clone_leg_runs_only_the_touched_tests_of_a_touched_file_and_skips_slow_tiers(landing, tmp_path):
    root, base = landing
    (root / "tests" / "test_pair.py").write_text(
        "import pytest\n\n\ndef test_a():\n    assert True\n\n\ndef test_b():\n    assert False\n\n\n"
        "@pytest.mark.cadence\ndef test_slow():\n    assert False\n", encoding="utf-8")
    base = _land(root, "pkg/mod.py", "VALUE = 1\n# pair\n")
    post = _land(root, "tests/test_pair.py", (root / "tests" / "test_pair.py").read_text(encoding="utf-8").replace("assert True", "assert 1"))

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "tests/test_pair.py", "--", "tests/test_pair.py"],
                        tmp_path / "o.json")

    assert data["targets_run"] == ["tests/test_pair.py::test_a"], data
    assert data["new_red"] == [] and data["status_after"] == "pass" and data["tests_run"] == 1


def test_clone_leg_over_only_slow_tier_tests_passes_instead_of_failing_on_exit_5(landing, tmp_path):
    root, _base = landing
    (root / "tests" / "test_slow_only.py").write_text(
        "import pytest\n\n\n@pytest.mark.cadence\ndef test_slow():\n    assert False\n", encoding="utf-8")
    post = _land(root, "tests/test_slow_only.py", (root / "tests" / "test_slow_only.py").read_text(encoding="utf-8"))

    _code, data = _main(["--head-sha", post, "--touched", "tests/test_slow_only.py", "--", "tests/test_slow_only.py"],
                        tmp_path / "o.json")

    assert data["status_after"] == "pass" and data["new_red"] == [], data


def test_clone_leg_runs_the_targets_when_an_importer_fails_to_collect(landing, tmp_path, monkeypatch):
    monkeypatch.setattr(cov, "xdist_workers", lambda _py, _units: 0)  # a serial run aborts at a collection error
    root, _base = landing
    base = _land(root, "tests/test_broken_importer.py", "import nonexistent_dep_xyz  # mod\n\n\ndef test_x():\n    pass\n")
    post = _land(root, "pkg/mod.py", "VALUE = 1\n# landed\n")

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "pkg/mod.py", "--", "tests/test_unrelated.py"],
                        tmp_path / "o.json")

    assert "tests/test_broken_importer.py" in data["importer_targets"]
    assert data["tests_run"] >= 1 and "zero_run_cause" not in data, data
    assert "tests/test_unrelated.py::test_other" in data["failing"]
    assert "tests/test_broken_importer.py" in data["pre_existing"] and "tests/test_broken_importer.py" not in data["new_red"]


def test_an_importer_overrun_keeps_the_handed_targets_results(landing, tmp_path, monkeypatch):
    root, base = landing
    post = _land(root, "pkg/mod.py", "VALUE = 1\n# landed\n")

    real = cov._run

    def overrun(argv, *a, **k):
        if "tests/test_importer.py" in _view(argv):
            raise subprocess.TimeoutExpired("pytest", 1)
        return real(argv, *a, **k)

    monkeypatch.setattr(cov, "_run", overrun)
    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "pkg/mod.py", "--", "tests/test_unrelated.py"],
                        tmp_path / "o.json")

    assert data["importer_overrun"] is True and data["over_budget"] is False
    assert data["tests_run"] >= 1 and "zero_run_cause" not in data, data


def test_a_broken_importer_new_at_the_landing_is_still_a_new_red(landing, tmp_path):
    root, base = landing
    post = _land(root, "tests/test_broken_importer.py", "import nonexistent_dep_xyz  # mod\n\n\ndef test_x():\n    pass\n")

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "tests/test_broken_importer.py", "--",
                         "tests/test_unrelated.py"], tmp_path / "o.json")

    assert "tests/test_broken_importer.py" in data["new_red"] and data["status_after"] == "fail", data


BIN_TEST = "coordinator/bin/tests/test_bin_tool.py"


@pytest.fixture
def bin_repo(tmp_path, monkeypatch):
    """A git repo holding a test under coordinator/bin/tests, the process started in coordinator/bin."""
    root = tmp_path / "repo"
    (root / "coordinator" / "bin" / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / BIN_TEST).write_text("def test_bin_tool():\n    assert True\n", encoding="utf-8")
    _git(root, "init", "-q")
    monkeypatch.chdir(root / "coordinator" / "bin")
    return root


def test_a_leg_started_below_the_repo_root_finds_a_bin_tests_file(bin_repo, tmp_path, monkeypatch):
    seen = _spawns(monkeypatch)
    _code, data = _main(["--", BIN_TEST], tmp_path / "o.json")
    assert "missing_targets" not in data and "zero_run_cause" not in data
    assert (data["tests_run"], data["status"], data["status_after"]) == (1, "pass", "pass")
    assert len(seen) == 1 and BIN_TEST in seen[0]


def test_a_leg_started_below_the_repo_root_still_refuses_a_genuinely_absent_target(bin_repo, tmp_path, monkeypatch):
    seen = _spawns(monkeypatch)
    gone = "coordinator/bin/tests/test_gone.py"
    _code, data = _main(["--", gone], tmp_path / "o.json")
    assert seen == [] and data["missing_targets"] == [gone]
    assert data["zero_run_cause"] == ltt.ZERO_RUN_MISSING_TARGET


OVERFLOW = 32 * 1024


BASE = "b" * 40
TRIPWIRE = "tests/test_tripwire.py"


def _foreign_dependency_landing(root: Path) -> str:
    """HEAD: `pkg/b.py` imports `new_name` from `pkg/a.py`, which defines it only in an uncommitted worktree change."""
    (root / "pkg" / "a.py").write_text("OLD = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "a")
    base = _git(root, "rev-parse", "HEAD")
    (root / "tests" / "test_b.py").write_text(
        "from pkg import b\n\ndef test_b():\n    assert b.VALUE == 1\n", encoding="utf-8")
    post = _land(root, "pkg/b.py", "from pkg.a import new_name\n\nVALUE = new_name()\n")
    (root / "pkg" / "a.py").write_text("OLD = 1\n\n\ndef new_name():\n    return 1\n", encoding="utf-8")
    return base, post


def test_a_hunk_importing_a_name_only_in_a_peers_uncommitted_change_halts_naming_the_symbol_and_source(landing, tmp_path):
    root, _base0 = landing
    base, post = _foreign_dependency_landing(root)

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "pkg/b.py", "--", "tests/test_b.py"],
                        tmp_path / "o.json")

    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_FOREIGN_DEPENDENCY == "uncommitted-foreign-dependency"
    assert data["import_error"]["symbol"] == "new_name"
    assert data["import_error"]["source_path"] == "pkg/a.py"
    note = ltt.zero_run_cause_note(data, "x")
    assert "new_name" in note and "pkg/a.py" in note


def test_a_missing_name_defined_nowhere_uncommitted_stays_collection_failure(landing, tmp_path):
    root, _base0 = landing
    base, post = _foreign_dependency_landing(root)
    (root / "pkg" / "a.py").write_text("OLD = 1\n", encoding="utf-8")

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "pkg/b.py", "--", "tests/test_b.py"],
                        tmp_path / "o.json")

    assert data["zero_run_cause"] != ltt.ZERO_RUN_FOREIGN_DEPENDENCY and "import_error" not in data
