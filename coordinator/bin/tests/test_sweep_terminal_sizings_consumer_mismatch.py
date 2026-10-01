"""MISMATCH is a stdout warning on a consumer box and stays on stderr on an author box."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import unittest.mock
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_MESSAGE = "repo-identity: MISMATCH sentinel"


def _load():
    loader = importlib.machinery.SourceFileLoader("sweep_ts_mismatch_test", str(_BIN_DIR / "sweep-terminal-sizings.py"))
    spec = importlib.util.spec_from_loader("sweep_ts_mismatch_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _run(consumer: bool) -> tuple[int, str, str]:
    mod = _load()
    import lib  # noqa: F401
    import repo_identity

    verdict = {"verdict": "MISMATCH", "message": _MESSAGE}
    out, err = io.StringIO(), io.StringIO()
    with unittest.mock.patch.object(
        repo_identity, "resolve_checked_repo_root", return_value=("/nonexistent-root", verdict)
    ), unittest.mock.patch.object(mod, "_is_consumer_box", return_value=consumer), \
            unittest.mock.patch.object(mod, "_ensure_claude_klabauter_on_path", return_value="."), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mod.main(["--dry-run"])
    return rc, out.getvalue(), err.getvalue()


def test_consumer_mismatch_is_a_stdout_warning():
    _rc, out, err = _run(consumer=True)
    assert f"warning: {_MESSAGE}" in out
    assert _MESSAGE not in err


def test_author_mismatch_stays_on_stderr():
    _rc, out, err = _run(consumer=False)
    assert _MESSAGE in err
    assert _MESSAGE not in out
