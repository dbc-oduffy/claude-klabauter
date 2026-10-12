"""Unit tests for check_unmanaged_temp_creation, on synthetic sources only."""

from __future__ import annotations

import pytest

from coordinator_core.ops import check_unmanaged_temp_creation as m


def _scan(src: str, rel: str = "coordinator_core/x.py"):
    return m._scan_source(src, rel)


@pytest.mark.parametrize(
    "src",
    [
        "import tempfile\ndef f():\n    return tempfile.mkdtemp()\n",
        "import tempfile\ndef f():\n    return tempfile.mkstemp(suffix='.x')\n",
        "import tempfile\ndef f():\n    return tempfile.mkdtemp(dir=None)\n",
        "import tempfile\ndef f():\n    with tempfile.NamedTemporaryFile(delete=False) as t:\n        pass\n",
        "import tempfile\ndef f():\n    t = tempfile.NamedTemporaryFile()\n",
        "import tempfile\ndef f():\n    return tempfile.TemporaryDirectory()\n",
        "from tempfile import mkdtemp\ndef f():\n    return mkdtemp('a', 'b')\n",
    ],
)
def test_bare_creation_is_a_finding(src):
    found = _scan(src)
    assert [f.kind for f in found] == ["bare-temp-create"]
    assert found[0].function == "f"


@pytest.mark.parametrize(
    "src",
    [
        "import tempfile\ndef f(d):\n    return tempfile.mkdtemp(dir=d)\n",
        "import tempfile\ndef f(d):\n    return tempfile.mkstemp('a', 'b', d)\n",
        "import tempfile\ndef f(d):\n    return tempfile.NamedTemporaryFile('w', -1, None, None, '', '', d)\n",
        "import tempfile\ndef f(**k):\n    return tempfile.mkdtemp(**k)\n",
        "import tempfile\ndef f():\n    with tempfile.NamedTemporaryFile() as t:\n        pass\n",
        "import tempfile\ndef f():\n    with tempfile.TemporaryDirectory() as t:\n        pass\n",
        "import tempfile\ndef f():\n    with tempfile.NamedTemporaryFile(delete=True) as t:\n        pass\n",
        "import tempfile\ndef f():\n    return tempfile.TemporaryFile()\n",
        "import tempfile\ndef f():\n    return tempfile.SpooledTemporaryFile()\n",
    ],
)
def test_exempt_shapes_are_not_findings(src):
    assert _scan(src) == []


def test_unlisted_gettempdir_is_a_finding():
    src = "import tempfile\ndef g():\n    return tempfile.gettempdir()\n"
    found = _scan(src)
    assert [(f.kind, f.function) for f in found] == [("gettempdir", "g")]


def test_aliased_gettempdir_is_a_finding():
    src = "def g(t):\n    return t().gettempdir()\n"
    assert [f.kind for f in _scan(src)] == ["gettempdir"]


def test_module_level_gettempdir_reports_module():
    found = _scan("import tempfile\nX = tempfile.gettempdir()\n")
    assert found[0].function == "<module>"


def test_listed_pair_is_exempt_and_other_function_is_not(monkeypatch):
    monkeypatch.setattr(
        m, "READ_ONLY_GETTEMPDIR", frozenset({("coordinator_core/x.py", "ok")})
    )
    src = (
        "import tempfile\n"
        "def ok():\n    return tempfile.gettempdir()\n"
        "def bad():\n    return tempfile.gettempdir()\n"
    )
    assert [f.function for f in _scan(src)] == ["bad"]
    assert _scan(src, rel="coordinator_core/y.py") != [] and len(_scan(src, rel="coordinator_core/y.py")) == 2


def test_nested_function_reports_innermost():
    src = "import tempfile\ndef a():\n    def b():\n        return tempfile.mkdtemp()\n    return b\n"
    assert _scan(src)[0].function == "b"


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("coordinator_core/a.py", True),
        ("coordinator/bin/x.py", True),
        ("scripts/s.py", True),
        ("setup/s.py", True),
        ("top.py", True),
        ("coordinator_core/tests/test_a.py", False),
        ("coordinator_core/ops/tests/helper.py", False),
        ("coordinator_core/test_a.py", False),
        ("coordinator_core/conftest.py", False),
        ("docs/plans/p.py", False),
        ("state/p.py", False),
        ("archive/p.py", False),
        ("coordinator_core/a.txt", False),
    ],
)
def test_scope(rel, expected):
    assert m._in_scope(rel) is expected


def test_scan_explicit_paths_and_main_exit_codes(tmp_path, capsys):
    (tmp_path / "bad.py").write_text("import tempfile\nd = tempfile.mkdtemp()\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    assert [f.path for f in m.scan(["bad.py"], root=tmp_path)] == ["bad.py"]
    assert m.scan(["good.py"], root=tmp_path) == []

    monkey_root = tmp_path
    import coordinator_core.ops.check_unmanaged_temp_creation as mod

    orig = mod.show_toplevel
    mod.show_toplevel = lambda: str(monkey_root)
    try:
        assert mod.main(["bad.py"]) == 1
        assert "bad.py:2: bare-temp-create in <module>" in capsys.readouterr().out
        assert mod.main(["good.py"]) == 0
    finally:
        mod.show_toplevel = orig


def test_syntax_error_source_yields_no_findings():
    assert _scan("def (:\n") == []
