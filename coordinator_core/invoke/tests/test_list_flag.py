"""--list prints the registered op names, sorted, one per line, exit 0."""

import io
from contextlib import redirect_stdout

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv_body


def test_list_prints_sorted_op_names_and_exits_zero():
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        _dispatch_argv_body(["--list"], ".", allow_warm=False)
    assert exc.value.code == 0
    names = buf.getvalue().splitlines()
    assert "ping" in names
    assert names == sorted(names)
