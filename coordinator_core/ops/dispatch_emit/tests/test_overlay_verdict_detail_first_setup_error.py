"""The overlay red verdict names the first `ERROR at setup of` traceback head even when the output tail cuts it."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _repo

_OUTPUT = """\
==================================== ERRORS ====================================
_____________ ERROR at setup of test_alpha _____________

request = <FixtureRequest for <Function test_alpha>>

    def one(request):
>       return request.getfixturevalue("absent_dep")
E       fixture 'absent_dep' not found

t/test_x.py:5: FixtureLookupError
_____________ ERROR at setup of test_beta _____________
E       second error
=========================== short test summary info ============================
ERROR t/test_x.py::test_alpha - fixture 'absent_dep' not found
"""


def test_first_setup_error_names_header_and_cause_of_the_first_only():
    head = cov.first_setup_error(_OUTPUT)

    assert head.startswith("first setup ERROR: ")
    assert "ERROR at setup of test_alpha" in head
    assert "fixture 'absent_dep' not found" in head
    assert "second error" not in head


def test_first_collection_error_head_carries_the_import_cause():
    out = (
        "_ ERROR collecting t/test_land_run.py __\r\nImportError while importing test module\r\nTraceback:\r\n"
        "t/test_land_run.py:11: in <module>\r\nE   ImportError: cannot import name 'x' from 'y'\r\n"
        "_ ERROR collecting t/test_other.py __\r\nE   other\r\n"
    )

    head = cov.first_setup_error(out)

    assert "ERROR collecting t/test_land_run.py" in head and "cannot import name 'x'" in head
    assert "other" not in head


def test_no_setup_error_yields_empty():
    assert cov.first_setup_error("FAILED t/test_x.py::test_a - assert 0\n1 failed in 0.1s\n") == ""


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    """The persistent mirror lives under the system Temp's coordinator root; every test gets its own."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


_TRACKED = frozenset({"prompts/hl.txt", "t/test_x.py"})


def _missing(path: str) -> str:
    return f"E   FileNotFoundError: [Errno 2] No such file or directory: '{path}'\n1 failed in 0.1s\n"


def test_mirror_gap_names_a_tracked_relative_or_absolute_path_the_mirror_lacks(tmp_path):
    assert "prompts/hl.txt" in cov.mirror_gap(_missing("prompts/hl.txt"), tmp_path, _TRACKED)
    absolute = str(tmp_path / "prompts" / "hl.txt").replace("\\", "\\\\")
    assert "prompts/hl.txt" in cov.mirror_gap(_missing(absolute), tmp_path, _TRACKED)


def test_mirror_gap_is_empty_for_an_untracked_path_a_present_file_or_no_missing_file(tmp_path):
    assert cov.mirror_gap(_missing("made/by/the/code.txt"), tmp_path, _TRACKED) == ""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "hl.txt").write_text("x")
    assert cov.mirror_gap(_missing("prompts/hl.txt"), tmp_path, _TRACKED) == ""
    assert cov.mirror_gap("E   assert 0\n1 failed in 0.1s\n", tmp_path, _TRACKED) == ""


@pytest.mark.spawns_process
def test_verify_types_a_run_failing_on_a_tracked_path_absent_from_the_mirror_as_test_infra(tmp_path, monkeypatch):
    root, _ = _repo(tmp_path)
    _write(root, "prompts/hl.txt", "p\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "prompt")
    head = _git(root, "rev-parse", "HEAD").strip()
    real = cov._run

    def fake(argv, cwd, timeout, **kw):
        if "pytest" not in argv:
            return real(argv, cwd, timeout, **kw)
        (cwd / "prompts" / "hl.txt").unlink(missing_ok=True)
        return subprocess.CompletedProcess(
            argv, 1, "F  [100%]\nFAILED t/test_reader.py::test_reads_data\n" + _missing("prompts/hl.txt"), "",
        )

    monkeypatch.setattr(cov, "_run", fake)

    out = cov.verify(root, "run1", head, ["t/test_reader.py"])

    assert out.cause == cov.ZERO_RUN_TEST_INFRA == "test_infra_failure", out
    assert "prompts/hl.txt" in out.detail and "mirror gap" in out.detail


@pytest.mark.spawns_process
def test_verify_leaves_a_failure_on_an_untracked_missing_path_uncaused(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    real = cov._run

    def fake(argv, cwd, timeout, **kw):
        if "pytest" not in argv:
            return real(argv, cwd, timeout, **kw)
        return subprocess.CompletedProcess(
            argv, 1, "F  [100%]\nFAILED t/test_reader.py::test_reads_data\n" + _missing("out/never_tracked.txt"), "",
        )

    monkeypatch.setattr(cov, "_run", fake)

    out = cov.verify(root, "run1", head, ["t/test_reader.py"])

    assert out.cause == "", out


_CONFTEST_LOAD = (
    "ImportError while loading conftest '/shared/tests/conftest.py'.\n"
    "tests/conftest.py:1740: in <module>\n    @contextmanager\n"
    "E   NameError: name 'contextmanager' is not defined\n"
)


def test_a_conftest_import_error_head_names_the_foreign_conftest_and_its_cause():
    head = cov.first_setup_error(_CONFTEST_LOAD)

    assert "loading conftest '/shared/tests/conftest.py'" in head
    assert "NameError: name 'contextmanager' is not defined" in head
    assert cov.first_setup_error("1 passed in 0.1s\n") == ""


@pytest.mark.spawns_process
def test_a_zero_test_pytest_process_error_is_a_failing_verdict_naming_exit_interpreter_cwd_argv_and_unrun_files(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    seen: list = []
    real = cov._run

    def fake(argv, cwd, timeout, **kw):
        if "pytest" not in argv:
            return real(argv, cwd, timeout, **kw)
        seen.append((list(argv), cwd))
        return subprocess.CompletedProcess(argv, 4, "", _CONFTEST_LOAD)

    monkeypatch.setattr(cov, "_run", fake)

    out = cov.verify(root, "run1", head, ["t/test_reader.py"])

    argv, cwd = seen[0]
    assert out.verdict == cov.ENV_DEFECT, out
    assert "exit=4" in out.detail
    assert f"interpreter={argv[0]}" in out.detail
    assert f"cwd={cwd}" in out.detail
    assert "--basetemp" in out.detail and "t/test_reader.py" in out.detail
    assert "unrun test files: t/test_reader.py" in out.detail
    assert "loading conftest '/shared/tests/conftest.py'" in out.detail


@pytest.mark.spawns_process
def test_a_non_guard_process_error_with_no_completed_test_is_red_test_infra_with_the_process_facts(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    real = cov._run

    def fake(argv, cwd, timeout, **kw):
        if "pytest" not in argv:
            return real(argv, cwd, timeout, **kw)
        return subprocess.CompletedProcess(argv, 4, "1 error in 0.1s\n", "")

    monkeypatch.setattr(cov, "_run", fake)

    out = cov.verify(root, "run1", head, ["t/test_reader.py"])

    assert out.verdict == "red" and out.cause == cov.ZERO_RUN_TEST_INFRA, out
    assert "exit=4" in out.detail and "unrun test files: t/test_reader.py" in out.detail, out


@pytest.mark.spawns_process
def test_an_internal_error_after_tests_completed_stays_inconclusive_not_a_zero_test_red(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    real = cov._run

    def fake(argv, cwd, timeout, **kw):
        if "pytest" not in argv:
            return real(argv, cwd, timeout, **kw)
        return subprocess.CompletedProcess(argv, 3, "INTERNALERROR> KeyError: x\n2 passed in 0.1s\n", "")

    monkeypatch.setattr(cov, "_run", fake)

    out = cov.verify(root, "run1", head, ["t/test_reader.py"])

    assert out.verdict == "inconclusive" and out.cause == "", out
    assert "exit=3" in out.detail


@pytest.mark.spawns_process
def test_a_collection_failure_with_no_test_run_carries_the_process_facts(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    real = cov._run

    def fake(argv, cwd, timeout, **kw):
        if "pytest" not in argv:
            return real(argv, cwd, timeout, **kw)
        return subprocess.CompletedProcess(
            argv, 2, "_ ERROR collecting t/test_reader.py _\nE   ImportError: x\n1 error in 0.1s\n"
            "Interrupted: 1 error during collection\n", "",
        )

    monkeypatch.setattr(cov, "_run", fake)

    out = cov.verify(root, "run1", head, ["t/test_reader.py"])

    assert out.verdict == "red" and "exit=2" in out.detail and "unrun test files: t/test_reader.py" in out.detail, out


def test_process_facts_caps_the_unrun_files_and_omits_the_clause_without_files():
    proc = subprocess.CompletedProcess(["py", "-m", "pytest", "a"], 4, "", "")
    many = [f"t/test_{i}.py::test_x" for i in range(20)]

    text = cov.process_facts(proc, "/cwd", many)

    assert "(+8 more)" in text and "t/test_0.py" in text and "test_x" not in text
    assert "unrun" not in cov.process_facts(proc, "/cwd")


def _leg_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text("def test_passes():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def test_a_zero_test_reverify_names_the_pytest_process_and_the_unrun_files(tmp_path, monkeypatch):
    from coordinator_core.ops import test_leg_run as tlr

    root = _leg_repo(tmp_path, monkeypatch)
    spawned: list = []

    def usage_error(*args, **kwargs):
        spawned.append((args[0], kwargs["cwd"]))
        return subprocess.CompletedProcess(args[0], 4, "", _CONFTEST_LOAD)

    monkeypatch.setattr(tlr.subprocess, "run", usage_error)

    out = tlr.run(["tests/test_a.py::test_passes"], base_sha=None, head_sha=None, base_export_out=None, budget_s=60, cwd=str(root))

    proc = out[tlr.PYTEST_PROCESS_KEY]
    assert (out["tests_run"], out["status"], out["status_after"]) == (0, "error", "fail")
    assert out["zero_run_cause"] == "collection-failure"
    assert proc["exit_code"] == 4 and proc["cwd"] == str(root) == out["cwd"]
    assert proc["interpreter"] == spawned[0][0][0] == out["interpreter"]
    assert "tests/test_a.py::test_passes" in proc["argv"] and "--basetemp" in proc["argv"]
    assert out["unrun_targets"] == ["tests/test_a.py"]
    assert "loading conftest '/shared/tests/conftest.py'" in out["first_setup_error"]
    assert "exit=4" in out["summary"] and "unrun test files: tests/test_a.py" in out["summary"]


def test_a_reverify_that_ran_tests_carries_no_process_facts(tmp_path, monkeypatch):
    from coordinator_core.ops import test_leg_run as tlr

    root = _leg_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        tlr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "1 passed in 0.01s\n", ""),
    )

    out = tlr.run(["tests/test_a.py::test_passes"], base_sha=None, head_sha=None, base_export_out=None, budget_s=60, cwd=str(root))

    assert out["tests_run"] == 1 and out["status"] == "pass"
    assert tlr.PYTEST_PROCESS_KEY not in out and "unrun_targets" not in out


@pytest.mark.spawns_process
def test_a_foreign_conftest_broken_mid_edit_never_zero_tests_the_reverify(tmp_path, monkeypatch):
    from coordinator_core.ops import test_leg_run as tlr

    root = _leg_repo(tmp_path, monkeypatch)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    (root / "tests" / "conftest.py").write_text("@contextmanager\ndef broken():\n    yield\n", encoding="utf-8")

    out = tlr.run(["tests/test_a.py::test_passes"], base_sha=None, head_sha=None, base_export_out=None, budget_s=120, cwd=str(root))

    assert (out["tests_run"], out["status"], out["status_after"]) == (1, "pass", "pass"), out
    assert "zero_run_cause" not in out and "own_tree_skipped" not in out
