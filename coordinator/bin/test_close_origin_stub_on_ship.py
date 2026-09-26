from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import types

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "close-origin-stub-on-ship.py")


class _FakeRouteMutationError(RuntimeError):

    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.result = result


_ABSENT = object()


def _install_fake_cc_invoke(route_mutation_fn):
    fake = types.ModuleType("cc_invoke")
    fake.route_mutation = route_mutation_fn
    fake.RouteMutationError = _FakeRouteMutationError
    prior = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake
    return prior


def _restore_cc_invoke(prior) -> None:
    if prior is _ABSENT:
        sys.modules.pop("cc_invoke", None)
    else:
        sys.modules["cc_invoke"] = prior


def _load_subject_fresh():
    sys.modules.pop("close-origin-stub-on-ship", None)
    spec = importlib.util.spec_from_file_location("close-origin-stub-on-ship", SUBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(route_mutation_fn, argv):
    prior_cc_invoke = _install_fake_cc_invoke(route_mutation_fn)
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


def test_transport_failure_runtime_error():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure (rc=127)")

    code, out, err = _run_main(_route_mutation, ["--plan", "docs/plans/foo.md"])
    assert code == 1
    assert "transport" in err


def test_op_level_refusal_route_mutation_error():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise _FakeRouteMutationError(
            "op refused: exit_code=1", {"exit_code": 1, "failed": ["bad join"]}
        )

    code, out, err = _run_main(_route_mutation, ["--handoff", "state/handoffs/foo.md"])
    assert code == 1
    assert "op reported failure" in err


def test_op_success_summary_printed():
    captured_params = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        captured_params["op"] = op
        captured_params["params"] = params
        return {
            "exit_code": 0,
            "closed": [{"stub_path": "state/handoffs/x.md", "stub_id": "lvv-09"}],
            "skipped": [],
            "pairs_resolved": 1,
            "message": "closed 1 stub",
        }

    code, out, err = _run_main(_route_mutation, ["--plan", "docs/plans/foo.md"])
    assert code == 0
    assert captured_params.get("op") == "handoff.close_origin_stub"
    assert "closed=1" in out
    assert "skipped=0" in out
    assert "lvv-09" in out
    assert "closed 1 stub" in out


def test_usage_error_no_args():
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return {"exit_code": 0}

    code, out, err = _run_main(_route_mutation, [])
    assert code == 2
    assert "at least one of --plan / --handoff" in err
    assert reached["called"] is False


def test_sha_plumbing():
    sha_params = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        sha_params["params"] = params
        return {"exit_code": 0, "closed": [], "skipped": [], "pairs_resolved": 0, "message": ""}

    code, out, err = _run_main(
        _route_mutation, ["--plan", "docs/plans/foo.md", "--sha", "deadbeef01"]
    )
    assert code == 0
    assert sha_params["params"].get("sha") == "deadbeef01"


def test_params_shape_null_when_absent_populated_when_present():
    shape_params = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        shape_params["params"] = params
        return {"exit_code": 0, "closed": [], "skipped": [], "pairs_resolved": 0, "message": ""}

    code, out, err = _run_main(_route_mutation, ["--handoff", "state/handoffs/foo.md"])
    assert code == 0
    p = shape_params["params"]
    assert p.get("plan_path") is None
    assert p.get("handoff_path") == "state/handoffs/foo.md"
    assert p.get("sha") is None
