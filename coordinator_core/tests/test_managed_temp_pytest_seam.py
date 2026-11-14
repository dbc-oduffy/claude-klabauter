"""The pytest Temp seam: tempfile.tempdir is a managed session dir and a leftover fails the session."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from coordinator_core.temp_layout import RUN_TEMP_DIRNAME

pytest_plugins = ["pytester"]

_SEAM_CONFTEST = """
from coordinator_core.conftest import managed_temp_session_finish, managed_temp_session_start


def pytest_sessionstart(session):
    managed_temp_session_start(session)


def pytest_sessionfinish(session, exitstatus):
    managed_temp_session_finish(session, exitstatus)
"""


def _run(pytester):
    # An explicit basetemp keeps the inner session from minting pytest-of-<user> in the outer session dir.
    return pytester.runpytest_inprocess("--basetemp", str(pytester.path / "bt"))


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
