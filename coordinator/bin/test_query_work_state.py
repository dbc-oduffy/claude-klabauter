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
    fake = types.ModuleType("cc_invoke")
    fake.route = route_fn
    fake.resolve_engine_root = lambda _file: SCRIPT_DIR
    prior = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake
    return prior


def _restore_cc_invoke(prior) -> None:
    if prior is _ABSENT:
        sys.modules.pop("cc_invoke", None)
    else:
        sys.modules["cc_invoke"] = prior


def _load_subject_fresh():
    sys.modules.pop("query-work-state", None)
    spec = importlib.util.spec_from_file_location("query-work-state", SUBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(route_fn, argv):
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
    subject = _load_subject_fresh()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc_info:
            subject._parse_args(["--fleet"])
    assert exc_info.value.code == 1
    assert "--fleet" in err.getvalue()


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
