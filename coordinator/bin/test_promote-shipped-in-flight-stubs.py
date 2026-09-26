from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import types

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "promote-shipped-in-flight-stubs.py")

_ABSENT = object()


def _install_fakes(op_main_fn):
    fake_cc_invoke = types.ModuleType("cc_invoke")
    fake_cc_invoke.require_dispatch_engine_on_path = lambda: "/nonexistent/fake-claude-klabauter-live-root"
    prior_cc_invoke = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake_cc_invoke

    import coordinator_core.cli_entry as _real_cli_entry

    fake_pkg = types.ModuleType("coordinator_core")
    fake_ops_pkg = types.ModuleType("coordinator_core.ops")
    fake_op_module = types.ModuleType("coordinator_core.ops.promote_shipped_in_flight_stubs")
    fake_op_module.main = op_main_fn
    fake_pkg.ops = fake_ops_pkg
    fake_pkg.cli_entry = _real_cli_entry
    fake_ops_pkg.promote_shipped_in_flight_stubs = fake_op_module

    prior_pkg = sys.modules.get("coordinator_core", _ABSENT)
    prior_ops_pkg = sys.modules.get("coordinator_core.ops", _ABSENT)
    prior_cli_entry = sys.modules.get("coordinator_core.cli_entry", _ABSENT)
    prior_op_module = sys.modules.get(
        "coordinator_core.ops.promote_shipped_in_flight_stubs", _ABSENT
    )
    sys.modules["coordinator_core"] = fake_pkg
    sys.modules["coordinator_core.ops"] = fake_ops_pkg
    sys.modules["coordinator_core.cli_entry"] = _real_cli_entry
    sys.modules["coordinator_core.ops.promote_shipped_in_flight_stubs"] = fake_op_module

    return {
        "cc_invoke": prior_cc_invoke,
        "coordinator_core": prior_pkg,
        "coordinator_core.ops": prior_ops_pkg,
        "coordinator_core.cli_entry": prior_cli_entry,
        "coordinator_core.ops.promote_shipped_in_flight_stubs": prior_op_module,
    }


def _restore_fakes(prior: dict) -> None:
    for name, value in prior.items():
        if value is _ABSENT:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = value


def _load_subject_fresh():
    sys.modules.pop("promote-shipped-in-flight-stubs", None)
    spec = importlib.util.spec_from_file_location(
        "promote-shipped-in-flight-stubs", SUBJECT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(op_main_fn):
    prior = _install_fakes(op_main_fn)
    out, err = io.StringIO(), io.StringIO()
    try:
        subject = _load_subject_fresh()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = subject.main()
            except SystemExit as exc:
                code = exc.code
    finally:
        _restore_fakes(prior)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


def test_nonzero_exit_propagates_unchanged():
    code, out, err = _run_main(lambda argv, repo_root=None: 1)
    assert code == 1


def test_zero_exit_propagates_unchanged():
    code, out, err = _run_main(lambda argv, repo_root=None: 0)
    assert code == 0


def test_repo_root_passed_script_dir_relative():
    """repo_root is derived from THIS file's own grandparent directory
    (SCRIPT_DIR-relative), not the invoking shell's cwd — see subject
    module docstring "Usage"."""
    captured = {}

    def _fake_main(argv, repo_root=None):
        captured["repo_root"] = repo_root
        return 0

    code, out, err = _run_main(_fake_main)
    assert code == 0
    expected_repo_root = os.path.dirname(os.path.dirname(SCRIPT_DIR))
    assert captured["repo_root"] == expected_repo_root
