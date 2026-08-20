"""test_query_work_state.py — pytest suite for query-work-state.py.

Mirrors `test_query_handoff_columns.py`'s fake-`cc_invoke` fixture pattern:
seed `sys.modules["cc_invoke"]` with a fake module BEFORE importing the
subject via `importlib` (the subject's `import cc_invoke` after its own
`sys.path.insert(0, lib_dir)` resolves against `sys.modules` first, so a
pre-seeded entry fully short-circuits the file search — no live engine-root
/ coordinator_core.invoke subprocess is ever spawned and no real
`session.work_state` op fires). Each test loads a FRESH copy of the subject
module so a per-test fake `route` never leaks across tests.

Test coverage:
  T1  argv parsing — no flags: repo_root defaults to os.getcwd()
  T2  argv parsing — `--repo-root <path>` sets repo_root
  T3  argv parsing — unrecognized token is a hard usage error, exit 1
  T4  argv parsing — `--repo-root` as the trailing token with no value is a
      hard usage error, exit 1
  T5  op success — `cc_invoke.route()` called with op="session.work_state",
      params={}, and the resolved repo_root; result printed to stdout as
      JSON; exit 0
  T6  op failure — `route()` raises RuntimeError (transport failure or
      State-1 seam-absent legacy raise); exit 1, message on stderr
  T7  no `--fleet` flag exists — passing it is a hard usage error (negative
      spec: this CLI is per-repo by construction, see the module docstring)

Negative-spec: no test invokes the real `session.work_state` op — the fake
`cc_invoke.route()` never spawns `coordinator_core.invoke`.

Spec backlink: pln-a-pull-surface-for-cockpit-the-b8e2f3 § C7
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import types

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "query-work-state.py")

_ABSENT = object()


def _install_fake_cc_invoke(route_fn):
    """Seed sys.modules["cc_invoke"] with a fake module exposing route().
    Must run BEFORE the subject module is imported.

    Returns the prior sys.modules entry (or `_ABSENT`) — hand it to
    `_restore_cc_invoke` in a `finally`.

    Negative spec: `sys.modules["cc_invoke"]` is process-global, and 30+
    `coordinator/bin/` scripts import `cc_invoke` by bare name. A fake left
    installed past the test that seeded it makes every later such import in
    the same worker resolve against a module carrying only these attributes;
    never install without a paired restore.
    """
    fake = types.ModuleType("cc_invoke")
    fake.route = route_fn
    fake.resolve_engine_root = lambda _file: SCRIPT_DIR
    prior = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake
    return prior


def _restore_cc_invoke(prior) -> None:
    """Undo `_install_fake_cc_invoke`, restoring absence as absence."""
    if prior is _ABSENT:
        sys.modules.pop("cc_invoke", None)
    else:
        sys.modules["cc_invoke"] = prior


def _load_subject_fresh():
    """Import a brand-new copy of the subject module (bypassing any cached
    entry) so per-test route() fakes never leak across tests."""
    sys.modules.pop("query-work-state", None)
    spec = importlib.util.spec_from_file_location("query-work-state", SUBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(route_fn, argv):
    """Load a fresh subject with the given fake route(), call main(argv),
    and capture (exit_code, stdout, stderr)."""
    prior_cc_invoke = _install_fake_cc_invoke(route_fn)
    out, err = io.StringIO(), io.StringIO()
    try:
        subject = _load_subject_fresh()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject.main(argv)
    finally:
        _restore_cc_invoke(prior_cc_invoke)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


# --- Argv parsing (no op routing needed — call _parse_args directly) ---


def test_parse_args_no_flags_defaults_to_cwd():
    subject = _load_subject_fresh()
    assert subject._parse_args([]) == {"repo_root": os.getcwd()}


def test_parse_args_repo_root_sets_value():
    subject = _load_subject_fresh()
    assert subject._parse_args(["--repo-root", "/some/repo"]) == {
        "repo_root": "/some/repo"
    }


def test_parse_args_unrecognized_token_exits_1():
    subject = _load_subject_fresh()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc_info:
            subject._parse_args(["--wrong-flag", "x"])
    assert exc_info.value.code == 1
    assert "--wrong-flag" in err.getvalue()


def test_parse_args_repo_root_trailing_with_no_value_exits_1():
    subject = _load_subject_fresh()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc_info:
            subject._parse_args(["--repo-root"])
    assert exc_info.value.code == 1
    assert "--repo-root" in err.getvalue()


def test_parse_args_fleet_flag_is_unrecognized():
    """Negative spec: no `--fleet`/multi-root flag exists on this CLI."""
    subject = _load_subject_fresh()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc_info:
            subject._parse_args(["--fleet"])
    assert exc_info.value.code == 1
    assert "--fleet" in err.getvalue()


# --- main() / op routing ---


def test_op_success_routes_session_work_state_and_prints_json():
    captured = {}

    def _route(op, params, repo_root, legacy_fn):
        captured["op"] = op
        captured["params"] = params
        captured["repo_root"] = repo_root
        return {"work_items": [{"id": "x"}]}

    code, out, err = _run_main(_route, ["--repo-root", "/repo"])
    assert code == 0
    assert captured["op"] == "session.work_state"
    assert captured["params"] == {}
    assert captured["repo_root"] == "/repo"
    assert '"work_items"' in out
    assert '"id": "x"' in out


def test_op_failure_returns_exit_1_and_prints_stderr():
    def _route(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure")

    code, out, err = _run_main(_route, ["--repo-root", "/repo"])
    assert code == 1
    assert "simulated transport failure" in err
    assert out == ""
