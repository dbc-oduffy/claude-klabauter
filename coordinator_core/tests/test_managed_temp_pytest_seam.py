"""The pytest Temp seam: tempfile.tempdir is a managed session dir and a leftover fails the session."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from coordinator_core.conftest import (
    MANAGED_TEMP_AFTER_LAST_TEST,
    MANAGED_TEMP_LEFT_BY_PREFIX,
    MANAGED_TEMP_ROOT_LEFT_PREFIX,
)
from coordinator_core.temp_layout import RUN_TEMP_DIRNAME

pytest_plugins = ["pytester"]

_SEAM_CONFTEST = """
from coordinator_core.conftest import managed_temp_session_finish, managed_temp_session_start


def pytest_sessionstart(session):
    managed_temp_session_start(session)


def pytest_sessionfinish(session, exitstatus):
    managed_temp_session_finish(session, exitstatus)
"""


_ATTRIBUTING_CONFTEST = """
import os
import tempfile

import pytest

from coordinator_core.conftest import (
    managed_temp_note_test,
    managed_temp_session_finish,
    managed_temp_session_start,
)


def pytest_sessionstart(session):
    managed_temp_session_start(session)


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    managed_temp_note_test(item.session, item.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("SEAM_LEAK_AT_FINISH"):
        os.mkdir(os.path.join(tempfile.gettempdir(), "leaked-after-last-test"))
    managed_temp_session_finish(session, exitstatus)
"""


def _run(pytester):
    # An explicit basetemp keeps the inner session from minting pytest-of-<user> in the outer session dir.
    return pytester.runpytest_inprocess("--basetemp", str(pytester.path / "bt"))


def test_default_basetemp_is_gone_after_a_passing_finish(tmp_path, monkeypatch):
    import types

    from coordinator_core import conftest as seam
    from coordinator_core import temp_layout

    monkeypatch.setattr(temp_layout, "_inside_worktree", lambda path: False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(temp_layout.tempfile, "gettempdir", lambda: str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    config = types.SimpleNamespace(
        option=types.SimpleNamespace(basetemp=None),
        rootpath=repo,
        _tmp_path_factory=None,
    )
    seam.default_basetemp_to_temp_layout(config)
    path = Path(config.option.basetemp)
    path.mkdir()
    (path / "f.txt").write_text("x")
    session = types.SimpleNamespace(config=config, exitstatus=0)
    seam.managed_temp_session_finish(session, 0)
    assert not path.exists()
    assert not any(path.parent.glob(f"{os.getpid()}-*"))


def test_tempfile_tempdir_is_a_managed_session_dir():
    session_dir = Path(tempfile.gettempdir())
    assert RUN_TEMP_DIRNAME in session_dir.parts
    assert session_dir.name.endswith("-pytest-session")


def test_clean_session_passes_and_restores_tempdir_and_removes_dir(pytester):
    pytester.makeconftest(_SEAM_CONFTEST)
    pytester.makepyfile(
        test_clean="""
        import tempfile
        from pathlib import Path

        def test_uses_session_dir():
            with tempfile.TemporaryDirectory() as d:
                assert Path(d).parent == Path(tempfile.gettempdir())
        """
    )
    before = tempfile.tempdir
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    assert tempfile.tempdir == before


def test_leftover_entry_fails_the_session_naming_it(pytester):
    pytester.makeconftest(_SEAM_CONFTEST)
    pytester.makepyfile(
        test_leaky="""
        import tempfile

        def test_leaks():
            tempfile.mkdtemp(prefix="leaked-seam-probe-")
        """
    )
    before = tempfile.tempdir
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    assert result.ret != 0
    err = result.stderr.str()
    assert "leaked-seam-probe-" in err
    assert tempfile.tempdir == before


def test_session_dir_is_removed_after_a_failing_session(pytester, monkeypatch):
    pytester.makeconftest(_SEAM_CONFTEST)
    pytester.makepyfile(
        test_dirpath="""
        import os, tempfile

        def test_records():
            os.environ["SEAM_PROBE_DIR"] = tempfile.gettempdir()
            tempfile.mkdtemp(prefix="leak-")
        """
    )
    monkeypatch.setenv("SEAM_PROBE_DIR", "")
    result = _run(pytester)
    assert result.ret != 0
    probe = os.environ["SEAM_PROBE_DIR"]
    assert probe and not Path(probe).exists()


def test_leak_is_attributed_to_the_test_that_left_it(pytester):
    pytester.makeconftest(_ATTRIBUTING_CONFTEST)
    pytester.makepyfile(
        test_attr="""
        import tempfile

        def test_clean_first():
            pass

        def test_leaks():
            tempfile.mkdtemp(prefix="leaked-attr-probe-")

        def test_clean_last():
            pass
        """
    )
    result = _run(pytester)
    result.assert_outcomes(passed=3)
    assert result.ret != 0
    lines = [ln for ln in result.stderr.str().splitlines() if ln.startswith(MANAGED_TEMP_LEFT_BY_PREFIX)]
    assert len(lines) == 1
    nodeid, _, entry = lines[0][len(MANAGED_TEMP_LEFT_BY_PREFIX):].partition(": ")
    assert nodeid.endswith("test_attr.py::test_leaks")
    assert entry.startswith("leaked-attr-probe-")


def test_entry_appearing_after_the_last_test_is_charged_to_the_file(pytester, monkeypatch):
    monkeypatch.setenv("SEAM_LEAK_AT_FINISH", "1")
    pytester.makeconftest(_ATTRIBUTING_CONFTEST)
    pytester.makepyfile(test_late="def test_ok():\n    pass\n")
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    assert result.ret != 0
    err = result.stderr.str()
    assert f"{MANAGED_TEMP_LEFT_BY_PREFIX}test_late.py{MANAGED_TEMP_AFTER_LAST_TEST}: leaked-after-last-test" in err
    assert "test_late.py::test_ok: leaked-after-last-test" not in err


_DEFAULT_RUN_CONFTEST = """
import pytest

from coordinator_core.conftest import managed_temp_session_finish, managed_temp_session_start

pytest_plugins = ["coordinator_core.testing.fs_containment"]


def pytest_sessionstart(session):
    managed_temp_session_start(session)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    managed_temp_session_finish(session, exitstatus)
"""

_MINTING_OFF_CONFTEST = _DEFAULT_RUN_CONFTEST.replace(
    'pytest_plugins = ["coordinator_core.testing.fs_containment"]\n', ""
)


def _default_run(pytester, conftest, body):
    from coordinator_core import temp_layout

    pytester.makeconftest(conftest)
    pytester.makepyfile(test_inner=body)
    pytester.makeini("[pytest]\n")
    repo_root = str(Path(temp_layout.__file__).resolve().parents[1])
    pytester._monkeypatch.setenv("PYTHONPATH", repo_root)
    root = temp_layout.pytest_basetemp_root(pytester.path)
    before = set(os.listdir(root)) if root.is_dir() else set()
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    after = set(os.listdir(root)) if root.is_dir() else set()
    return result, root, after - before


def _cleanup(root, gained):
    import shutil

    for name in gained:
        shutil.rmtree(root / name, ignore_errors=True)


@pytest.mark.parametrize("conftest", [_DEFAULT_RUN_CONFTEST, _MINTING_OFF_CONFTEST], ids=["minted", "layout-default"])
def test_finished_default_run_leaves_no_basetemp(pytester, conftest):
    result, root, gained = _default_run(pytester, conftest, "def test_ok(tmp_path):\n    (tmp_path / 'f').write_text('x')\n")
    try:
        result.assert_outcomes(passed=1)
        assert result.ret == 0
        assert gained == set()
    finally:
        _cleanup(root, gained)


@pytest.mark.parametrize("conftest", [_DEFAULT_RUN_CONFTEST, _MINTING_OFF_CONFTEST], ids=["minted", "layout-default"])
def test_failing_default_run_leaves_at_most_the_kept_cap(pytester, conftest):
    from coordinator_core.testing import fs_containment

    result, root, gained = _default_run(pytester, conftest, "def test_bad():\n    assert False\n")
    try:
        result.assert_outcomes(failed=1)
        assert len(gained) <= fs_containment._KEEP_FAILING
    finally:
        _cleanup(root, gained)


_ROOT_BASE_ENV = "SEAM_ROOT_BASE"


def _root_run_temp(base: str) -> Path:
    return Path(base) / "coordinator" / "seam-probe-repo" / RUN_TEMP_DIRNAME


def test_run_temp_dir_left_under_the_coordinator_root_fails_the_session_naming_it(pytester, monkeypatch):
    monkeypatch.setenv(_ROOT_BASE_ENV, tempfile.gettempdir())
    pytester.makeconftest(_SEAM_CONFTEST)
    pytester.makepyfile(
        test_root_leak=f"""
        import os
        from pathlib import Path

        def test_leaks_into_the_root():
            run_temp = Path(os.environ["{_ROOT_BASE_ENV}"]) / "coordinator" / "seam-probe-repo" / "{RUN_TEMP_DIRNAME}"
            (run_temp / f"{{os.getpid()}}-abc123-root-leak-probe").mkdir(parents=True)
        """
    )
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    assert result.ret != 0
    err = result.stderr.str()
    assert MANAGED_TEMP_ROOT_LEFT_PREFIX in err
    assert f"{os.getpid()}-abc123-root-leak-probe" in err
    assert not any(_root_run_temp(tempfile.gettempdir()).glob("*root-leak-probe"))


def test_stranded_entries_beyond_the_print_cap_are_all_removed(pytester, monkeypatch):
    monkeypatch.setenv(_ROOT_BASE_ENV, tempfile.gettempdir())
    pytester.makeconftest(_SEAM_CONFTEST)
    pytester.makepyfile(
        test_root_flood=f"""
        import os
        from pathlib import Path

        def test_strands_many():
            run_temp = Path(os.environ["{_ROOT_BASE_ENV}"]) / "coordinator" / "seam-probe-repo" / "{RUN_TEMP_DIRNAME}"
            run_temp.mkdir(parents=True, exist_ok=True)
            for i in range(25):
                (run_temp / f"{{os.getpid()}}-abc123-flood-probe-{{i:02d}}").mkdir()
        """
    )
    result = _run(pytester)
    assert result.ret != 0
    err = result.stderr.str()
    assert err.count(MANAGED_TEMP_ROOT_LEFT_PREFIX) == 20
    assert not any(_root_run_temp(tempfile.gettempdir()).glob("*flood-probe-*"))


def test_dir_removed_by_its_writer_and_preexisting_own_pid_dir_pass(pytester, monkeypatch):
    base = tempfile.gettempdir()
    monkeypatch.setenv(_ROOT_BASE_ENV, base)
    preexisting = _root_run_temp(base) / f"{os.getpid()}-def456-preexisting"
    preexisting.mkdir(parents=True)
    pytester.makeconftest(_SEAM_CONFTEST)
    pytester.makepyfile(
        test_root_clean=f"""
        import os, shutil
        from pathlib import Path

        def test_creates_and_removes():
            run_temp = Path(os.environ["{_ROOT_BASE_ENV}"]) / "coordinator" / "seam-probe-repo" / "{RUN_TEMP_DIRNAME}"
            mine = run_temp / f"{{os.getpid()}}-abc123-root-clean-probe"
            mine.mkdir()
            shutil.rmtree(mine)
        """
    )
    result = _run(pytester)
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    assert MANAGED_TEMP_ROOT_LEFT_PREFIX not in result.stderr.str()
    assert preexisting.is_dir()


def test_clean_attributing_session_prints_no_left_by_line(pytester):
    pytester.makeconftest(_ATTRIBUTING_CONFTEST)
    pytester.makepyfile(test_ok="def test_ok():\n    pass\n")
    result = _run(pytester)
    assert result.ret == 0
    assert MANAGED_TEMP_LEFT_BY_PREFIX not in result.stderr.str()
