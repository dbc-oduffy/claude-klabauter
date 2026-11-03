"""Pins `pycallsig.bind_check` and the extracted payload-parity binder surface."""

from __future__ import annotations

import ast

from coordinator_core import pycallsig
from coordinator_core.percolate import payload_parity as pp


def _bind(callee_src: str, call_src: str) -> "list[str]":
    fn = pycallsig.top_level_functions(ast.parse(callee_src))["f"]
    call = ast.parse(call_src).body[0].value
    return pycallsig.bind_check(call, fn.args)


def test_clean_bind():
    assert _bind("def f(a, b=1): pass", "f(1, b=2)") == []


def test_unexpected_keyword():
    out = _bind("def f(a): pass", "f(1, withheld=2)")
    assert out == ["unexpected keyword argument 'withheld'"]


def test_accepted_keyword_is_clean():
    assert _bind("def f(a, *, withheld=None): pass", "f(1, withheld=2)") == []


def test_arity_overflow():
    out = _bind("def f(a): pass", "f(1, 2)")
    assert out and out[0].startswith("too many positional arguments")


def test_vararg_absorbs_extra_positionals():
    assert _bind("def f(a, *rest): pass", "f(1, 2, 3)") == []


def test_missing_required_argument():
    out = _bind("def f(a, b): pass", "f(1)")
    assert out == ["missing required argument(s): ['b']"]


def test_positional_only_passed_as_keyword():
    out = _bind("def f(a, /, b): pass", "f(b=3, a=2)")
    assert "positional-only argument 'a' passed as keyword" in out


def test_kwargs_callee_accepts_any_keyword():
    assert _bind("def f(a, **kw): pass", "f(1, anything=2)") == []


def test_payload_parity_reuses_the_shared_binder():
    assert pp._bind_check is pycallsig.bind_check
    assert pp._FileImportMap is pycallsig.FileImportMap
    assert pp._resolve_call_target is pycallsig.resolve_call_target
    assert pp._top_level_functions is pycallsig.top_level_functions
    assert pp._calls_asserting_typeerror is pycallsig.calls_asserting_typeerror
