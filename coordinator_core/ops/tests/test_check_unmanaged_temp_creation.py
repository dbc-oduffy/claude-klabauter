"""Unit tests for check_unmanaged_temp_creation, on synthetic sources only."""

from __future__ import annotations

import pytest

from coordinator_core.ops import check_unmanaged_temp_creation as m
from pathlib import Path


_OUTSIDE_ENGINE = "scripts/x.py"


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
def test_bare_creation_is_a_finding(src) -> None:
    found = _scan(src, rel=_OUTSIDE_ENGINE)
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
def test_exempt_shapes_are_not_findings(src) -> None:
    assert _scan(src, rel=_OUTSIDE_ENGINE) == []


@pytest.mark.parametrize(
    "src",
    [
        "import tempfile\ndef f(d):\n    return tempfile.mkdtemp(dir=d)\n",
        "import tempfile\ndef f():\n    return tempfile.mkdtemp()\n",
        "import tempfile\ndef f():\n    with tempfile.TemporaryDirectory() as t:\n        pass\n",
        "import tempfile\ndef f(d):\n    with tempfile.TemporaryDirectory(dir=d) as t:\n        pass\n",
        "from tempfile import mkdtemp\ndef f(d):\n    return mkdtemp('a', 'b', d)\n",
    ],
)
def test_engine_directory_creator_is_unmanaged_regardless_of_dir_or_with(src) -> None:
    found = _scan(src)
    assert [f.kind for f in found] == ["unmanaged-temp-create"]


def test_engine_directory_creator_is_legal_in_temp_layout() -> None:
    src = "import tempfile\ndef f(d):\n    return tempfile.mkdtemp(dir=d)\n"
    assert _scan(src, rel="coordinator_core/temp_layout.py") == []


@pytest.mark.parametrize(
    "src",
    [
        "import tempfile\ndef f(d):\n    return tempfile.mkstemp(dir=d)\n",
        "import tempfile\ndef f(d):\n    return tempfile.NamedTemporaryFile(dir=d, delete=False)\n",
    ],
)
def test_engine_atomic_write_sibling_stays_legal(src) -> None:
    assert _scan(src) == []


def test_engine_bare_mkstemp_keeps_the_bare_rule() -> None:
    src = "import tempfile\ndef f():\n    return tempfile.mkstemp()\n"
    assert [f.kind for f in _scan(src)] == ["bare-temp-create"]


def test_other_trees_keep_the_dir_exemption_for_directory_creators() -> None:
    src = "import tempfile\ndef f(d):\n    return tempfile.mkdtemp(dir=d)\n"
    for rel in ("coordinator/bin/x.py", "scripts/x.py", "setup/x.py"):
        assert _scan(src, rel=rel) == []


def test_main_exits_1_on_engine_mkdtemp_with_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir()
    (pkg / "fix.py").write_text("import tempfile\nd = tempfile.mkdtemp(dir=x)\n", encoding="utf-8")
    monkeypatch.setattr(m, "show_toplevel", lambda: str(tmp_path))
    assert m.main(["coordinator_core/fix.py"]) == 1
    out = capsys.readouterr().out
    assert "unmanaged-temp-create" in out and "managed_temp_dir" in out


def test_unlisted_gettempdir_is_a_finding() -> None:
    src = "import tempfile\ndef g():\n    return tempfile.gettempdir()\n"
    found = _scan(src)
    assert [(f.kind, f.function) for f in found] == [("gettempdir", "g")]


def test_aliased_gettempdir_is_a_finding() -> None:
    src = "def g(t):\n    return t().gettempdir()\n"
    assert [f.kind for f in _scan(src)] == ["gettempdir"]


def test_module_level_gettempdir_reports_module() -> None:
    found = _scan("import tempfile\nX = tempfile.gettempdir()\n")
    assert found[0].function == "<module>"


def test_listed_pair_is_exempt_and_other_function_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_nested_function_reports_innermost() -> None:
    src = "import tempfile\ndef a():\n    def b():\n        return tempfile.mkdtemp()\n    return b\n"
    assert _scan(src, rel=_OUTSIDE_ENGINE)[0].function == "b"


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
def test_scope(rel, expected) -> None:
    assert m._in_scope(rel) is expected


def test_scan_explicit_paths_and_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_syntax_error_source_yields_no_findings() -> None:
    assert _scan("def (:\n") == []


_OUT = "writer-outside-scratch-root"


@pytest.mark.parametrize(
    "src",
    [
        "from pathlib import Path\ndef f():\n    Path('scratch/x').mkdir(parents=True)\n",
        "from pathlib import Path\ndef f(root):\n    d = root / 'state' / 'scratch' / 'run'\n    d.mkdir(parents=True, exist_ok=True)\n",
        "def f():\n    with open('/tmp/x.txt', 'w') as h:\n        h.write('x')\n",
        "import os\ndef f():\n    os.makedirs('scratch/a')\n",
        "import shutil\ndef f(src):\n    shutil.copy2(src, 'tmp/out')\n",
        "from pathlib import Path\nimport tempfile\ndef f():\n    p = Path(tempfile.gettempdir()) / 'x'\n    p.write_text('y')\n",
        "from pathlib import Path\n_S = Path('scratch')\ndef f():\n    (_S / 'a').mkdir()\n",
        "from pathlib import Path\ndef f():\n    a = Path('scratch')\n    b = a / 'x'\n    b.joinpath('y').write_bytes(b'')\n    b.mkdir()\n",
    ],
)
def test_engine_write_from_scratch_or_tmp_literal_is_a_finding(src) -> None:
    assert _OUT in [f.kind for f in _scan(src)]


def test_gettempdir_derived_write_in_a_listed_reader_is_still_a_writer_finding() -> None:
    src = "import tempfile, os\ndef g():\n    os.mkdir(tempfile.gettempdir() + '/x')\n"
    kinds = [f.kind for f in _scan(src)]
    assert kinds.count(_OUT) == 1


@pytest.mark.parametrize(
    "src",
    [
        "from pathlib import Path\nfrom coordinator_core.temp_layout import scratch_root\ndef f():\n    (scratch_root() / 'scratch').mkdir()\n",
        "from pathlib import Path\nfrom coordinator_core.temp_layout import scratch_root\ndef f():\n    d = scratch_root() / 'run'\n    d.mkdir(parents=True)\n    (d / 'tmp').mkdir()\n",
        "from coordinator_core.temp_layout import managed_temp_dir\ndef f():\n    with managed_temp_dir('x-') as d:\n        (d / 'scratch').mkdir()\n        open(d / 'tmp', 'w').close()\n",
        "from pathlib import Path\ndef f(d):\n    Path(d).mkdir()\n    open(d, 'w').close()\n",
        "def f():\n    return open('scratch/x.txt')\n",
        "def f():\n    return open('scratch/x.txt', 'r')\n",
        "from pathlib import Path\ndef f():\n    return Path('tmp').read_text()\n",
        "from pathlib import Path\ndef f(root):\n    (root / 'state' / 'tmpl').mkdir()\n",
    ],
)
def test_sanctioned_roots_and_non_writes_are_not_writer_findings(src) -> None:
    assert _OUT not in [f.kind for f in _scan(src)]


def test_writer_rule_is_engine_scope_only_and_temp_layout_is_exempt() -> None:
    src = "from pathlib import Path\ndef f():\n    Path('scratch/x').mkdir()\n"
    assert _OUT not in [f.kind for f in _scan(src, rel="scripts/x.py")]
    assert _scan(src, rel="coordinator_core/temp_layout.py") == []


def test_listed_writer_pair_is_exempt_and_other_function_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "OUTSIDE_SCRATCH_ROOT_WRITERS", frozenset({("coordinator_core/x.py", "ok")}))
    src = (
        "from pathlib import Path\n"
        "def ok():\n    Path('scratch/a').mkdir()\n"
        "def bad():\n    Path('scratch/a').mkdir()\n"
    )
    assert [f.function for f in _scan(src) if f.kind == _OUT] == ["bad"]


def test_main_exits_1_on_writer_outside_scratch_root(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir()
    (pkg / "fix.py").write_text("from pathlib import Path\nPath('scratch/x').mkdir()\n", encoding="utf-8")
    monkeypatch.setattr(m, "show_toplevel", lambda: str(tmp_path))
    assert m.main(["coordinator_core/fix.py"]) == 1
    out = capsys.readouterr().out
    assert "writer-outside-scratch-root" in out and "temp_layout.scratch_root" in out


def test_head_scan_has_no_writer_outside_scratch_root() -> None:
    assert [f for f in m.scan() if f.kind == _OUT] == []


def test_mkdtemp_in_call_outside_temp_layout_is_legal_because_helper_owns_the_root() -> None:
    src = (
        "from coordinator_core.temp_layout import mkdtemp_in\n"
        "def f():\n    return mkdtemp_in('x-', None)\n"
    )
    assert _scan(src, rel="coordinator_core/benchmarks/x.py") == []
