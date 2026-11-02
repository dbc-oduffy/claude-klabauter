"""--describe prints the op contract as JSON, exit 0, without running the op."""

import io
import json
from contextlib import redirect_stdout

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv_body


def _describe(op: str) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        _dispatch_argv_body([op, "--describe"], ".", allow_warm=False)
    assert exc.value.code == 0
    return json.loads(buf.getvalue())


def test_describe_prints_handler_docstring_as_summary_and_exits_zero():
    c = _describe("memo.draft")
    assert c["name"] == "memo.draft"
    assert "kind" in c["description"]
    assert c["summary"] and c["summary"] in c["description"]


def test_describe_deferring_handler_docstring_prints_module_docstring():
    import inspect

    from coordinator_core.ops import plan_seam_fix

    c = _describe("plan.seam_fix")
    assert inspect.getdoc(plan_seam_fix).strip() in c["description"]
    assert c["description"].count("See module docstring") == 1
    assert c["summary"] in c["description"]


def test_describe_one_line_handler_docstring_appends_module_docstring_params():
    c = _describe("dispatch.record_halted_run_landed")
    for param in ("run_id", "sha", "shas", "session_id"):
        assert f"``{param}``" in c["description"]
    assert c["description"].startswith(c["summary"])


def test_describe_carries_contract_fields():
    c = _describe("ping")
    for key in ("op_class", "timeout_s", "module", "params_schema", "summary"):
        assert key in c


def test_describe_unknown_op_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        _dispatch_argv_body(["no.such.op", "--describe"], ".", allow_warm=False)
    assert exc.value.code == 1
