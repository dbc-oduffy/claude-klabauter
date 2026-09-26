from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import types

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "cutover-cli.py")


_ABSENT = object()


def _install_fake_cc_invoke(route_fn):
    fake = types.ModuleType("cc_invoke")
    fake.route = route_fn
    fake._resolve_claude_klabauter_root = lambda: "/fake/claude-klabauter/root"
    fake.require_dispatch_engine_on_path = lambda: "/fake/claude-klabauter/root"
    prior = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake
    return prior


def _restore_cc_invoke(prior) -> None:
    if prior is _ABSENT:
        sys.modules.pop("cc_invoke", None)
    else:
        sys.modules["cc_invoke"] = prior


def _load_subject_fresh():
    from importlib.machinery import SourceFileLoader

    sys.modules.pop("cutover-cli", None)
    loader = SourceFileLoader("cutover-cli", SUBJECT_PATH)
    spec = importlib.util.spec_from_loader("cutover-cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run_show(route_fn, record_path):
    prior_cc_invoke = _install_fake_cc_invoke(route_fn)
    out, err = io.StringIO(), io.StringIO()
    try:
        subject = _load_subject_fresh()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject._cmd_show([record_path])
    finally:
        _restore_cc_invoke(prior_cc_invoke)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


def test_empty_verdict_line_prints_notes_to_stderr():
    def _route(op, params, repo_root, legacy_fn):
        return {
            "verdict_line": "",
            "notes": ["cutover.gate: record has no confirmed_consumers entries"],
            "exit_code": 1,
        }

    code, out, err = _run_show(_route, "state/roadmap/lifecycle-vocab/cutovers/demo.md")
    assert code == 1
    assert "coordinator_core returned empty verdict_line" in err
    assert "cutover.gate: record has no confirmed_consumers entries" in err


def test_empty_verdict_line_with_no_notes_does_not_crash():
    def _route(op, params, repo_root, legacy_fn):
        return {"verdict_line": "", "notes": [], "exit_code": 1}

    code, out, err = _run_show(_route, "state/roadmap/lifecycle-vocab/cutovers/demo.md")
    assert code == 1
    assert "coordinator_core returned empty verdict_line" in err


def test_nonempty_verdict_line_still_prints_verdict_and_notes():
    def _route(op, params, repo_root, legacy_fn):
        return {
            "verdict_line": "COVERAGE_OK",
            "notes": ["some informational note"],
            "exit_code": 0,
        }

    code, out, err = _run_show(_route, "state/roadmap/lifecycle-vocab/cutovers/demo.md")
    assert code == 0
    assert "COVERAGE_OK" in out
    assert "some informational note" in err
