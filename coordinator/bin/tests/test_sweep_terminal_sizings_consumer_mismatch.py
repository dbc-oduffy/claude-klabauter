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


def test_act_path_calls_drain_once_without_candidate_ids_and_prints_counters(tmp_path):
    mod = _load()
    import lib  # noqa: F401
    import repo_identity
    from coordinator_core.ops.fleet import terminal_sizing_drain

    calls: list = []

    def fake_drain(root, *, dry_run=False):
        calls.append((Path(root), dry_run))
        return {"acted": [{"id": "a"}, {"id": "b"}], "candidates": [{}, {}], "skipped": [], "failed": [],
                "scan_skipped": [{"id": "state/sizings/x.yaml", "reason": "forward-plan-not-terminal"}]}

    out = io.StringIO()
    with unittest.mock.patch.object(
        repo_identity, "resolve_checked_repo_root", return_value=(str(tmp_path), {"verdict": "OK"})
    ), unittest.mock.patch.object(mod, "_ensure_claude_klabauter_on_path", return_value="."), \
            unittest.mock.patch.object(mod, "_stamp_archive_sweeps_liveness"), \
            unittest.mock.patch("coordinator_core.git.repo_root.git_common_dir", return_value=str(tmp_path / ".git")), \
            unittest.mock.patch.object(terminal_sizing_drain, "drain", fake_drain), \
            contextlib.redirect_stdout(out):
        rc = mod.main([])

    assert rc == 0
    assert calls == [(tmp_path, False)]
    assert "2 terminal sizings archived" in out.getvalue()
    assert "forward-plan-not-terminal" in out.getvalue()


def test_consumer_mismatch_is_a_stdout_warning():
    _rc, out, err = _run(consumer=True)
    assert f"warning: {_MESSAGE}" in out
    assert _MESSAGE not in err


def test_author_mismatch_stays_on_stderr():
    _rc, out, err = _run(consumer=False)
    assert _MESSAGE in err
    assert _MESSAGE not in out
