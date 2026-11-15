"""Tests for coordinator/bin/annotation-codemod.py: rewrite rules, refusals, idempotence, --check."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "annotation-codemod.py"
_spec = importlib.util.spec_from_file_location("annotation_codemod", _SCRIPT)
assert _spec is not None and _spec.loader is not None
codemod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(codemod)


def _rw(src: str) -> str:
    out, _ = codemod.rewrite_text(src)
    assert codemod.rewrite_text(out) == (out, 0), "second run must be a no-op"
    ast.parse(out)
    return out


def test_none_return_added_and_value_return_untouched() -> None:
    out = _rw("def a():\n    print(1)\n\ndef b():\n    return x\n")
    assert "def a() -> None:" in out
    assert "def b():" in out


def test_none_return_refusals() -> None:
    src = (
        "import abc\n"
        "def gen():\n    yield 1\n"
        "def stub():\n    pass\n"
        "def doc():\n    '''d'''\n"
        "def ni():\n    raise NotImplementedError\n"
        "def always():\n    raise ValueError('x')\n"
        "@abc.abstractmethod\ndef ab(self):\n    print(1)\n"
        "def mixed(x):\n    if x:\n        return 1\n"
    )
    assert _rw(src) == src


def test_return_none_literal_and_bare_return() -> None:
    out = _rw("def f(x):\n    if x:\n        return\n    return None\n")
    assert "def f(x) -> None:" in out


def test_literal_returns_need_every_return_and_no_fallthrough() -> None:
    out = _rw(
        "def t():\n    return True\n"
        "def s(x):\n    if x:\n        return 'a'\n    return f'b{x}'\n"
        "def i(x):\n    if x:\n        return 1\n    else:\n        return 2\n"
    )
    assert "def t() -> bool:" in out
    assert "def s(x) -> str:" in out
    assert "def i(x) -> int:" in out
    refused = "def m(x):\n    return 1 if x else 'a'\ndef n(x):\n    if x:\n        return 1\ndef o(x):\n    return 1\n    return 'a'\n"
    assert _rw(refused) == refused


def test_mixed_bool_int_literals_not_inferred() -> None:
    src = "def f(x):\n    if x:\n        return True\n    return 1\n"
    assert _rw(src) == src


def test_nested_function_returns_do_not_leak() -> None:
    out = _rw("def outer():\n    def inner():\n        return 1\n    inner()\n")
    assert "def outer() -> None:" in out
    assert "def inner() -> int:" in out


def test_literal_default_param_types_and_spacing() -> None:
    out = _rw("def f(a=1, b='x', *, c=True, d=-2.5, e=None, g = 3):\n    return a\n")
    assert "a: int = 1" in out
    assert "b: str = 'x'" in out
    assert "c: bool = True" in out
    assert "d: float = -2.5" in out
    assert "e=None" in out
    assert "g: int = 3" in out


def test_existing_annotations_left_alone() -> None:
    src = "def f(a: str = 1, b=2) -> int:\n    return 1\n"
    out = _rw(src)
    assert out == "def f(a: str = 1, b: int = 2) -> int:\n    return 1\n"


def test_pytest_fixture_types_and_imports_inserted() -> None:
    src = '"""Doc."""\n\nimport os\n\n\ndef test_x(tmp_path, monkeypatch, capsys, other):\n    assert os\n'
    out = _rw(src)
    assert "tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], other" in out
    assert out.startswith('"""Doc."""\n\nimport os\nfrom pathlib import Path\nimport pytest\n')
    assert "def test_x(" in out and ") -> None:" in out


def test_fixture_types_only_in_test_scope() -> None:
    src = "def helper(tmp_path, request):\n    return tmp_path\n"
    assert _rw(src) == src


def test_fixture_decorated_function_is_test_scope() -> None:
    out = _rw("import pytest\n\n\n@pytest.fixture\ndef fx(tmp_path):\n    return tmp_path\n")
    assert "def fx(tmp_path: Path):" in out
    assert "from pathlib import Path" in out
    assert out.count("import pytest") == 1


def test_existing_imports_not_duplicated_and_conflict_skipped() -> None:
    ok = "from pathlib import Path\nimport pytest\n\n\ndef test_a(tmp_path):\n    pass\n"
    assert _rw(ok).count("import") == 2
    conflict = "Path = str\n\n\ndef test_a(tmp_path):\n    pass\n"
    assert "tmp_path: Path" not in _rw(conflict)


def test_future_import_stays_first() -> None:
    out = _rw("from __future__ import annotations\n\n\ndef test_a(tmp_path):\n    pass\n")
    assert out.startswith("from __future__ import annotations\nfrom pathlib import Path\n")


def test_multiline_signature_comments_preserved() -> None:
    src = (
        "def f(\n    a=1,  # first\n    b: str = 'x',\n    c=False,\n):  # trailing\n"
        "    print(a)\n"
    )
    out = _rw(src)
    assert out == (
        "def f(\n    a: int = 1,  # first\n    b: str = 'x',\n    c: bool = False,\n) -> None:  # trailing\n"
        "    print(a)\n"
    )


def test_async_and_decorated_and_method() -> None:
    out = _rw(
        "import functools\n\n\nclass C:\n    @functools.cache\n    async def m(self, n=0):\n        await n\n"
    )
    assert "async def m(self, n: int = 0) -> None:" in out


def test_non_ascii_source_offsets() -> None:
    out = _rw('def f(a="héllo", b=1):\n    s = "é"; print(s)\n')
    assert 'def f(a: str = "héllo", b: int = 1) -> None:' in out


def test_crlf_and_bom_preserved(tmp_path: Path) -> None:
    f = tmp_path / "test_m.py"
    f.write_bytes(b"\xef\xbb\xbfdef test_a(tmp_path):\r\n    pass\r\n\r\ndef g():\r\n    print(1)\r\n")
    assert codemod.main([str(f)]) == 0
    data = f.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert data.startswith(b"\xef\xbb\xbffrom pathlib import Path\r\n")
    assert data.count(b"\n") == data.count(b"\r\n")
    assert b"def g() -> None:" in data


def test_no_trailing_newline_import_insert() -> None:
    out = _rw("import os")
    assert out == "import os"
    out = _rw("def test_a(tmp_path):\n    pass")
    assert out.startswith("from pathlib import Path\n")


def test_overload_skipped() -> None:
    src = "from typing import overload\n\n\n@overload\ndef f(a=1):\n    print(a)\n"
    assert _rw(src) == src


def test_check_counts_writes_nothing_and_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "m.py"
    body = "def f(a, b=1):\n    return a\n\n\nclass C:\n    def m(self, x):\n        pass\n"
    f.write_text(body)
    assert codemod.main(["--check", str(tmp_path)]) == 1
    assert capsys.readouterr().out.strip() == "missing: 5 in 1 files (skipped 0)"
    assert f.read_text() == body


def test_check_zero_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "m.py").write_text("def f(a: int) -> int:\n    return a\n")
    assert codemod.main(["--check", str(tmp_path)]) == 0
    assert "missing: 0" in capsys.readouterr().out


def test_rewrite_reports_remaining_and_skips_syntax_errors_and_skip_dirs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ok.py").write_text("def f(a):\n    print(a)\n")
    (tmp_path / "bad.py").write_text("def (:\n")
    skipped_dir = tmp_path / "__pycache__"
    skipped_dir.mkdir()
    (skipped_dir / "x.py").write_text("def g():\n    pass\n")
    assert codemod.main([str(tmp_path)]) == 0
    out = capsys.readouterr()
    assert "added: 1 in 1 files; still missing: 1 (skipped 1)" in out.out
    assert (tmp_path / "ok.py").read_text() == "def f(a) -> None:\n    print(a)\n"
    assert (skipped_dir / "x.py").read_text() == "def g():\n    pass\n"
    assert codemod.main(["--check", str(tmp_path)]) == 1


def test_cli_entrypoint_exit_code(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("def f():\n    print(1)\n")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check", str(f)],
        capture_output=True, text=True, creationflags=flags,
    )
    assert proc.returncode == 1
    assert "missing: 1 in 1 files" in proc.stdout
    subprocess.run([sys.executable, str(_SCRIPT), str(f)], check=True, creationflags=flags)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check", str(f)],
        capture_output=True, text=True, creationflags=flags,
    )
    assert proc.returncode == 0
