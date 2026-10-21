from coordinator_core.ops.dispatch_emit import row_excerpts as re_
from coordinator_core.ops.dispatch_emit.executor_economy import (
    EXCERPT_HEADING,
    EXCERPT_MAX_BYTES,
    EXCERPT_MAX_SYMBOLS_PER_FILE,
)

SAMPLE = "import os\n\ndef a():\n    return 1\n\nclass B:\n    def inner(self):\n        pass\n\nasync def c():\n    pass\n"


def test_symbol_spans_top_level_only():
    assert re_.symbol_spans(SAMPLE) == [("a", 3, 5), ("B", 6, 9), ("c", 10, 11)]


def test_block_lists_spans_and_named_first(tmp_path):
    (tmp_path / "m.py").write_text(SAMPLE, encoding="utf-8")
    out = re_.excerpt_block("touch c please", ["m.py"], tmp_path, "abc123", {})
    assert out.startswith(EXCERPT_HEADING)
    assert "touch c please" in out
    assert "m.py: c L10-11, a L3-5, B L6-9" in out
    assert "abc123" in out


def test_missing_and_non_py_list_path_only(tmp_path):
    out = re_.excerpt_block("x", ["gone.py", "notes.md"], tmp_path, "h", {})
    assert "\ngone.py\n" in out and "\nnotes.md\n" in out


def test_cache_scans_each_file_once(tmp_path, monkeypatch):
    (tmp_path / "m.py").write_text(SAMPLE, encoding="utf-8")
    calls = []
    real = re_.symbol_spans
    monkeypatch.setattr(re_, "symbol_spans", lambda t: calls.append(1) or real(t))
    cache = {}
    re_.excerpt_block("x", ["m.py"], tmp_path, "h", cache)
    re_.excerpt_block("x", ["m.py"], tmp_path, "h", cache)
    assert len(calls) == 1


def test_byte_cap_truncates(tmp_path):
    out = re_.excerpt_block("y" * 5000, [], tmp_path, "h", {})
    assert len(out.encode("utf-8")) <= EXCERPT_MAX_BYTES
    assert out.endswith("(truncated)")


def test_symbol_cap(tmp_path):
    src = "".join(f"def f{i}():\n    pass\n" for i in range(EXCERPT_MAX_SYMBOLS_PER_FILE + 10))
    (tmp_path / "big.py").write_text(src, encoding="utf-8")
    line = re_._file_line("big.py", tmp_path, "", {})
    assert line.count(" L") == EXCERPT_MAX_SYMBOLS_PER_FILE
