"""--describe prints the op handler docstring, exit 0, without running the op."""

import io
from contextlib import redirect_stdout

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv_body


def test_describe_prints_handler_docstring_and_exits_zero():
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        _dispatch_argv_body(["memo.draft", "--describe"], ".", allow_warm=False)
    assert exc.value.code == 0
    assert "kind" in buf.getvalue()


def test_describe_deferring_handler_docstring_prints_module_docstring():
    import inspect

    from coordinator_core.ops import plan_seam_fix

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        _dispatch_argv_body(["plan.seam_fix", "--describe"], ".", allow_warm=False)
    assert exc.value.code == 0
    out = buf.getvalue()
    assert inspect.getdoc(plan_seam_fix).strip() in out
    assert out.count("See module docstring") == 1


def test_describe_unknown_op_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        _dispatch_argv_body(["no.such.op", "--describe"], ".", allow_warm=False)
    assert exc.value.code == 1
