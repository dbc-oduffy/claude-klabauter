"""Pure tests for module_attrs: alias binding and attribute-read scan."""

from __future__ import annotations

import ast

from coordinator_core.authoring_leaks import module_attrs
from coordinator_core.authoring_leaks.module_attrs import AttrRef, attribute_refs, module_aliases


def _refs(src: str, rel: str = "app/caller.py") -> "list[AttrRef]":
    return attribute_refs(ast.parse(src), rel)


def test_function_local_from_import_binds_and_reads() -> None:
    src = "def run():\n    from pkg import li\n    return li.new_fn()\n"
    assert module_aliases(src, "app/caller.py") == {"li": frozenset({"pkg.li"})}
    assert _refs(src) == [AttrRef("li", "pkg.li", "new_fn", 3)]


def test_import_as_binds_alias() -> None:
    src = "import pkg.li as x\nx.f()\n"
    assert module_aliases(src, "a.py") == {"x": frozenset({"pkg.li"})}
    assert _refs(src) == [AttrRef("x", "pkg.li", "f", 2)]


def test_plain_dotted_import_binds_root_and_resolves_longest_prefix() -> None:
    src = "import pkg.li\npkg.li.f()\npkg.other\n"
    assert module_aliases(src, "a.py") == {"pkg": frozenset({"pkg", "pkg.li"})}
    assert _refs(src) == [AttrRef("pkg", "pkg.li", "f", 2), AttrRef("pkg", "pkg", "other", 3)]


def test_relative_from_import_resolves_in_package_module() -> None:
    src = "from . import li\nli.f()\n"
    assert module_aliases(src, "pkg/caller.py") == {"li": frozenset({"pkg.li"})}
    assert _refs(src, "pkg/caller.py") == [AttrRef("li", "pkg.li", "f", 2)]


def test_relative_from_import_in_init_is_package_relative() -> None:
    assert module_aliases("from .sub import m\n", "pkg/__init__.py") == {"m": frozenset({"pkg.sub.m"})}


def test_parenthesised_multiline_from_import_with_as() -> None:
    src = "from pkg import (\n    a,  # c\n    b as bee,\n)\nbee.f()\n"
    assert module_aliases(src, "x.py") == {"a": frozenset({"pkg.a"}), "bee": frozenset({"pkg.b"})}
    assert _refs(src) == [AttrRef("bee", "pkg.b", "f", 5)]


def test_type_checking_only_import_yields_no_alias() -> None:
    src = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg import li\nelse:\n    from pkg import real\nx = li.f\n"
    assert module_aliases(src, "x.py") == {"TYPE_CHECKING": frozenset({"typing.TYPE_CHECKING"}), "real": frozenset({"pkg.real"})}
    assert _refs(src) == []


def test_reassigned_alias_yields_no_refs() -> None:
    assert _refs("from pkg import li\nli = []\nli.append(1)\n") == []


def test_parameter_alias_yields_no_refs() -> None:
    assert _refs("from pkg import li\ndef f(li):\n    return li.x\n") == []


def test_store_context_is_not_a_ref() -> None:
    assert _refs("from pkg import li\nli.x = 1\n") == []


def test_duplicate_reads_dedupe_to_first_line() -> None:
    assert _refs("from pkg import li\nli.f()\nli.f()\n") == [AttrRef("li", "pkg.li", "f", 2)]


def test_module_aliases_never_parses() -> None:
    real = ast.parse

    def boom(*a, **k):  # noqa: ANN001
        raise AssertionError("ast.parse called")

    module_attrs.ast.parse = boom  # type: ignore[assignment]
    try:
        assert module_aliases("from pkg import li\n", "a.py") == {"li": frozenset({"pkg.li"})}
    finally:
        module_attrs.ast.parse = real  # type: ignore[assignment]
