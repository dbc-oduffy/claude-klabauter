"""product_residue: declared product writes dirty and unlanded."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.product_residue import (
    PRODUCT_DIRTY,
    declared_product_residue,
)


def test_product_dirty_returns_dirty_declared_path():
    out = declared_product_residue(["a/product.md"], ["a/product.md", "docs/run.md"], ["docs/run.md"])
    assert out == {"a/product.md": PRODUCT_DIRTY}


def test_landed_declared_path_is_not_residue():
    assert declared_product_residue(["a.py"], ["a.py"], ["a.py"]) == {}


def test_clean_declared_path_is_not_residue():
    assert declared_product_residue(["a.py"], ["other.py"], []) == {}


def test_undeclared_dirty_path_is_not_residue():
    assert declared_product_residue(["a.py"], ["b.py"], []) == {}


def test_backslashes_normalised_and_sorted():
    out = declared_product_residue(["z\\y.py", "a.py"], ["z/y.py", "a.py"], [])
    assert list(out) == ["a.py", "z/y.py"]


def test_empty_inputs():
    assert declared_product_residue([], [], []) == {}
