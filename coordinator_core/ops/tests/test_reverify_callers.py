"""Tests for reverify_callers: symbol diff and each caller import form."""

from __future__ import annotations

from coordinator_core.ops.reverify_callers import callers_of, removed_symbols


def _hit(rows, caller="x/c.py", symbol="f"):
    return rows == [{"caller": caller, "symbol": symbol}]


def test_removed_symbols_kinds():
    before = b"def a(): pass\nasync def b(): pass\nclass C: pass\nD = 1\nE: int = 2\nF = G = 3\n"
    after = b"def a(): pass\n"
    assert removed_symbols(before, after) == {"b", "C", "D", "E", "F", "G"}


def test_removed_symbols_ignores_nested_and_unchanged():
    before = b"def a():\n    def inner(): pass\n"
    assert removed_symbols(before, before) == frozenset()


def test_removed_symbols_unparseable():
    assert removed_symbols(b"def (", b"x = 1") == frozenset()
    assert removed_symbols(b"def a(): pass\nB = 1\n", b"def (") == {"a", "B"}


def test_removed_reexport_is_detected():
    before = b"from .m import f, g as h\nimport os.path\nimport x as y\nfrom .z import *\n"
    assert removed_symbols(before, b"") == {"f", "h", "os", "y"}
    assert removed_symbols(before, b"from .m import f\n") == {"h", "os", "y"}


def test_removed_tuple_unpack_target_is_detected():
    before = b"a, (b, *c) = 1, (2, 3)\n[d, e] = 1, 2\n"
    assert removed_symbols(before, b"a = 1\n") == {"b", "c", "d", "e"}


def test_def_moved_into_top_level_if_or_try_is_not_removed():
    before = b"def f(): pass\nclass K: pass\n"
    for after in (
        b"if True:\n    def f(): pass\nelse:\n    class K: pass\n",
        b"try:\n    def f(): pass\nexcept E:\n    class K: pass\n",
        b"try:\n    pass\nexcept E:\n    pass\nelse:\n    def f(): pass\nfinally:\n    class K: pass\n",
    ):
        assert removed_symbols(before, after) == frozenset()
    assert removed_symbols(before, b"if True:\n    def f(): pass\n") == {"K"}


def test_from_import_plain_and_aliased():
    src = {"x/c.py": b"from a.m import f\n"}
    assert _hit(callers_of("a.m", ["f"], src))
    src = {"x/c.py": b"from a.m import f as g\ng()\n"}
    assert _hit(callers_of("a.m", ["f"], src))


def test_star_import_counts_every_wanted_name():
    src = {"x/c.py": b"from a.m import *\n"}
    assert _hit(callers_of("a.m", ["f"], src))
    assert callers_of("a.other", ["f"], src) == []


def test_import_dotted_attribute():
    src = {"x/c.py": b"import a.m\na.m.f()\n"}
    assert _hit(callers_of("a.m", ["f"], src))


def test_import_as_attribute():
    src = {"x/c.py": b"import a.m as x\nx.f()\n"}
    assert _hit(callers_of("a.m", ["f"], src))


def test_from_package_import_module_inside_function():
    src = {"x/c.py": b"def go():\n    from a import m\n    m.f()\n"}
    assert _hit(callers_of("a.m", ["f"], src))


def test_relative_from_dot_import_module():
    src = {"a/c.py": b"from . import m\nm.f()\n"}
    assert _hit(callers_of("a.m", ["f"], src), caller="a/c.py")


def test_relative_from_module_import_name():
    src = {"a/c.py": b"from .m import f\n"}
    assert _hit(callers_of("a.m", ["f"], src), caller="a/c.py")


def test_locally_defined_name_is_no_hit():
    src = {"x/c.py": b"def f(): pass\nf()\nm = object()\nm.f\n"}
    assert callers_of("a.m", ["f"], src) == []


def test_other_module_or_other_name_is_no_hit():
    src = {"x/c.py": b"from a.other import f\nimport a.m\na.m.g()\n"}
    assert callers_of("a.m", ["f"], src) == []


def test_unparseable_sources_skipped():
    src = {"x/bad.py": b"def (", "x/c.py": b"from a.m import f\n"}
    assert _hit(callers_of("a.m", ["f"], src))
    assert callers_of("a.m", [], src) == []


def test_rows_sorted():
    src = {"z.py": b"from a.m import f, e\n", "b.py": b"from a.m import f\n"}
    rows = callers_of("a.m", ["f", "e"], src)
    assert rows == [
        {"caller": "b.py", "symbol": "f"},
        {"caller": "z.py", "symbol": "e"},
        {"caller": "z.py", "symbol": "f"},
    ]


def test_incident_fixture_reply_sidecar_op():
    before = b"def ensure_twin_token(x):\n    return x\n\ndef keep(): pass\n"
    after = b"def keep(): pass\n"
    removed = removed_symbols(before, after)
    assert removed == {"ensure_twin_token"}
    op = (
        b"def run():\n"
        b"    from pkg.dispatch_emit import reply_sidecar\n"
        b"    return reply_sidecar.ensure_twin_token(1)\n"
    )
    rows = callers_of(
        "pkg.dispatch_emit.reply_sidecar",
        removed,
        {"pkg/dispatch_emit/op.py": op},
    )
    assert rows == [{"caller": "pkg/dispatch_emit/op.py", "symbol": "ensure_twin_token"}]
