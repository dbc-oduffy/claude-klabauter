"""Tests for coordinator/bin/resolve-coordinator-clone.py, the entrypoint over coordinator/lib."""

from __future__ import annotations

import importlib.util
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parents[1]
_ENTRY_PATH = _BIN_DIR / "resolve-coordinator-clone.py"

_spec = importlib.util.spec_from_file_location("resolve_coordinator_clone_entry", _ENTRY_PATH)
_entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_entry)


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = _entry.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_help_flags_exit_zero_with_usage_on_stdout():
    for flag in ("--help", "-h"):
        rc, out, err = _run([flag])
        assert rc == 0
        assert out.startswith("Usage: resolve-coordinator-clone.py")
        assert err == ""


def test_no_argument_and_unknown_flag_exit_two_with_usage_on_stderr():
    for argv in ([], ["--bogus"]):
        rc, out, err = _run(argv)
        assert rc == 2
        assert out == ""
        assert "Usage: resolve-coordinator-clone.py" in err


def test_for_content_prints_resolver_value():
    real_load = _entry._load_lib

    def patched_load():
        lib = real_load()
        lib.resolve_content = lambda: "/fake/content-root"
        return lib

    with mock.patch.object(_entry, "_load_lib", patched_load):
        rc, out, _ = _run(["--for-content"])
    assert rc == 0
    assert out == "/fake/content-root"


def test_source_has_no_execv():
    assert "execv" not in _ENTRY_PATH.read_text(encoding="utf-8")
