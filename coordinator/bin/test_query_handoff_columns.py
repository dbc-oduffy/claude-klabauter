from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import types

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "query-handoff-columns.py")

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
    sys.modules.pop("query-handoff-columns", None)
    spec = importlib.util.spec_from_file_location("query-handoff-columns", SUBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(route_fn, argv, repo_root=os.path.join(SCRIPT_DIR, "fake-repo")):
    prior_cc_invoke = _install_fake_cc_invoke(route_fn)
    out, err = io.StringIO(), io.StringIO()
    try:
        subject = _load_subject_fresh()
        subject._resolve_repo_root = lambda: repo_root
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject.main(argv)
    finally:
        _restore_cc_invoke(prior_cc_invoke)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


def test_parse_args_no_flags():
    subject = _load_subject_fresh()
    assert subject._parse_args([]) == {}


def test_parse_args_where_alone():
    subject = _load_subject_fresh()
    assert subject._parse_args(["--where", "status=open"]) == {"where": "status=open"}


def test_parse_args_since_alone():
    subject = _load_subject_fresh()
    assert subject._parse_args(["--since", "2026-08-01"]) == {"since": "2026-08-01"}


def test_parse_args_no_archive_alone():
    subject = _load_subject_fresh()
    assert subject._parse_args(["--no-archive"]) == {"archive": False}


def test_parse_args_all_combined():
    subject = _load_subject_fresh()
    parsed = subject._parse_args(
        ["--where", "status=open", "--since", "2026-08-01", "--no-archive"]
    )
    assert parsed == {"where": "status=open", "since": "2026-08-01", "archive": False}


def test_parse_args_unrecognized_token_exits_1():
    subject = _load_subject_fresh()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc_info:
            subject._parse_args(["--wehre", "status=open"])
    assert exc_info.value.code == 1
    assert "--wehre" in err.getvalue()


def test_parse_args_where_trailing_with_no_value_exits_1():
    subject = _load_subject_fresh()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc_info:
            subject._parse_args(["--where"])
    assert exc_info.value.code == 1
    assert "--where" in err.getvalue()


def test_parse_args_since_trailing_with_no_value_exits_1():
    subject = _load_subject_fresh()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as exc_info:
            subject._parse_args(["--since"])
    assert exc_info.value.code == 1
    assert "--since" in err.getvalue()


def test_op_success_routes_handoff_columns_and_prints_json():
    captured = {}

    def _route(op, params, repo_root, legacy_fn):
        captured["op"] = op
        captured["params"] = params
        captured["repo_root"] = repo_root
        return {"rows": [{"path": "state/handoffs/x.md", "status": "open"}]}

    code, out, err = _run_main(_route, ["--where", "status=open"], repo_root="/repo")
    assert code == 0
    assert captured["op"] == "handoff.columns"
    assert captured["params"] == {"where": "status=open"}
    assert captured["repo_root"] == "/repo"
    assert '"rows"' in out
    assert '"path": "state/handoffs/x.md"' in out


def test_op_failure_returns_exit_1_and_prints_stderr():
    def _route(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure")

    code, out, err = _run_main(_route, [])
    assert code == 1
    assert "simulated transport failure" in err
    assert out == ""


def test_repo_root_resolution_failure_exits_1_without_routing(monkeypatch):
    reached = {"called": False}

    def _route(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return {"rows": []}

    prior_cc_invoke = _install_fake_cc_invoke(_route)
    out, err = io.StringIO(), io.StringIO()

    def _boom_resolve(explicit_root=None):
        # resolver's own UNRESOLVED-with-no-root shape: `resolve_checked_
        return None, {"verdict": "UNRESOLVED", "message": "simulated: no git root"}

    try:
        subject = _load_subject_fresh()
        monkeypatch.setattr(subject, "resolve_checked_repo_root", _boom_resolve)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with pytest.raises(SystemExit) as exc_info:
                subject.main([])
    finally:
        _restore_cc_invoke(prior_cc_invoke)

    assert exc_info.value.code == 1
    assert "cannot resolve git repo root" in err.getvalue()
    assert reached["called"] is False
