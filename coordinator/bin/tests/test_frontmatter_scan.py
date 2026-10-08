"""Pins the contract of coordinator/lib/frontmatter_scan.py, clause by clause."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

_MODULE = Path(__file__).resolve().parents[2] / "lib" / "frontmatter_scan.py"
_spec = importlib.util.spec_from_file_location("frontmatter_scan_under_test", _MODULE)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)


def test_import_surface_is_future_only():
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 1
    assert isinstance(imports[0], ast.ImportFrom)
    assert imports[0].module == "__future__"


def test_bom_via_read_text_and_direct(tmp_path):
    p = tmp_path / "a.md"
    p.write_bytes("\ufeff---\nstatus: ok\n---\nbody\n".encode("utf-8"))
    assert fs.read_text(p).startswith("---")
    assert fs.scan_frontmatter(fs.read_text(p)) == {"status": "ok"}
    assert fs.scan_frontmatter("\ufeff---\nstatus: ok\n---\n") == {"status": "ok"}
    assert fs.scan_mapping("\ufeffroute: x\n") == {"route": "x"}


def test_crlf_identical_to_lf():
    lf = "---\na: 'x'\nb: [p, q]  # c\nc:\n  - one\n  - two\nd: |\n  body: no\n---\nz: 1\n"
    crlf = lf.replace("\n", "\r\n")
    assert fs.scan_frontmatter(crlf) == fs.scan_frontmatter(lf)
    assert fs.scan_mapping(crlf) == fs.scan_mapping(lf)


def test_quoted_scalars_keep_hash():
    m = fs.scan_mapping("a: 'x y'\nb: \"a # b\"\nc: \"q\"  # note\n")
    assert m == {"a": "x y", "b": "a # b", "c": "q"}


def test_unquoted_trailing_comment():
    assert fs.scan_mapping("a: value  # why\nb: # only comment\n") == {"a": "value", "b": ""}
    assert fs.scan_mapping("a: x#y\n") == {"a": "x#y"}


def test_inline_list_with_and_without_trailing_comment():
    m = fs.scan_mapping(
        "a: [x, 'y z', \"w\"]\nb: [x]  # boundary detents\nc: []  # none\nd: [\"a,b\", c]\n"
    )
    assert m == {"a": ["x", "y z", "w"], "b": ["x"], "c": [], "d": ["a,b", "c"]}


def test_block_list_with_blank_and_comment_lines():
    m = fs.scan_mapping("k:\n  - a\n\n  # note\n  - 'b' # t\nnext: v\n")
    assert m == {"k": ["a", "b"], "next": "v"}


def test_block_scalars_return_indicator_and_hide_continuations():
    m = fs.scan_mapping("a: |\n  inner: 1\nb: |-\n  x: 2\nc: >-\n  y: 3\nd: z\n")
    assert m == {"a": "|", "b": "|-", "c": ">-", "d": "z"}


def test_indented_key_invisible_including_nested_status():
    m = fs.scan_mapping("status: implemented\ngroup:\n  do:\n    status: pending\n  status: no\n")
    assert m == {"status": "implemented", "group": ""}


def test_duplicate_key_first_wins():
    assert fs.scan_mapping("fork: null\nfork: cut_to_fit\n")["fork"] == "null"


def test_fence_failures_give_empty():
    assert fs.scan_frontmatter("---\na: 1\nno close\n") == {}
    assert fs.scan_frontmatter("a: 1\n---\nb: 2\n") == {}
    assert fs.scan_frontmatter("---\na: 1\n---\nb: 2\n") == {"a": "1"}


def test_fence_lines_inert_in_mapping():
    assert fs.scan_mapping("---\na: 1\n---\n") == {"a": "1"}


def test_read_text_missing_is_none(tmp_path):
    assert fs.read_text(tmp_path / "nope.md") is None


def test_read_text_non_utf8_keeps_ascii_keys(tmp_path):
    p = tmp_path / "b.md"
    p.write_bytes(b"---\nroute: r\nnote: caf\xe9\n---\n")
    assert fs.scan_frontmatter(fs.read_text(p))["route"] == "r"


def test_none_and_empty_give_empty():
    for scan in (fs.scan_mapping, fs.scan_frontmatter):
        assert scan(None) == {}
        assert scan("") == {}


def test_non_mapping_text_is_empty():
    assert fs.scan_mapping("just prose\nmore prose\n") == {}
