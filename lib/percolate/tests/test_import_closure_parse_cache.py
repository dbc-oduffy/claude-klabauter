"""The import-closure gate parses each distinct file content once: a second call over an
unchanged tree (or a fresh copy of it) is served from the content-hash cache, verdicts are
unchanged, and the publish call site reports progress."""
from __future__ import annotations

import ast
import importlib.util
import io
import shutil
import sys
import types
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from coordinator_core.percolate.gate_result_cache import GateResultCache  # noqa: E402
from percolate import import_closure as ic  # noqa: E402

_BIN_DIR = Path(__file__).resolve().parents[2].parent / "bin"

EXPECTED = (
    3,
    [
        ("a.py", "gone"),
        ("a.py", "x.y"),
        ("a.py", "scripts.gen"),
        ("a.py", "foo_mod"),
    ],
)


def _build(root: Path, source: Path) -> None:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("from os import path\n__all__ = ['lazy']\n", encoding="utf-8")
    (root / "a.py").write_text(
        "from coordinator_core import gone, lazy\n"
        "import coordinator_core.x.y\n"
        "import foo_mod\n"
        "import scripts.gen\n"
        "import os\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text("def broken(:\n", encoding="utf-8")
    (root / "x").mkdir()
    (root / "x" / "__init__.py").write_text("", encoding="utf-8")
    source.mkdir(parents=True)
    (source / "foo_mod.py").write_text("", encoding="utf-8")


@pytest.fixture
def parse_calls(monkeypatch):
    calls: list[str] = []
    real = ast.parse

    def counting(src, filename="<unknown>", *args, **kwargs):
        calls.append(filename)
        return real(src, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting)
    return calls


def _cache(tmp_path: Path, version: str = "v1") -> GateResultCache:
    return GateResultCache(tmp_path / "cache", "import-closure", version)


def test_second_call_over_a_fresh_copy_parses_nothing(tmp_path, parse_calls):
    _build(tmp_path / "t1", tmp_path / "src")
    shutil.copytree(tmp_path / "t1", tmp_path / "t2")
    cache = _cache(tmp_path)

    cold = ic.find_import_closure_violations(tmp_path / "t1", tmp_path / "src", cache=cache)
    assert parse_calls
    cache.flush()
    parse_calls.clear()

    warm = ic.find_import_closure_violations(tmp_path / "t2", tmp_path / "src", cache=_cache(tmp_path))

    assert parse_calls == []
    assert warm == cold


def test_verdicts_are_identical_cold_warm_and_uncached(tmp_path):
    _build(tmp_path / "t", tmp_path / "src")
    cache = _cache(tmp_path)

    uncached = ic.find_import_closure_violations(tmp_path / "t", tmp_path / "src")
    cold = ic.find_import_closure_violations(tmp_path / "t", tmp_path / "src", cache=cache)
    warm = ic.find_import_closure_violations(tmp_path / "t", tmp_path / "src", cache=cache)

    assert uncached == cold == warm == EXPECTED


def test_a_changed_file_is_reparsed_and_judged_on_its_new_bytes(tmp_path, parse_calls):
    _build(tmp_path / "t", tmp_path / "src")
    cache = _cache(tmp_path)
    ic.find_import_closure_violations(tmp_path / "t", cache=cache)
    parse_calls.clear()

    (tmp_path / "t" / "a.py").write_text("from coordinator_core import fresh\n", encoding="utf-8")
    examined, violations = ic.find_import_closure_violations(tmp_path / "t", cache=cache)

    assert parse_calls == ["a.py"]
    assert (examined, violations) == (3, [("a.py", "fresh")])


def test_a_different_gate_version_is_a_miss(tmp_path, parse_calls):
    _build(tmp_path / "t", tmp_path / "src")
    first = _cache(tmp_path, "v1")
    ic.find_import_closure_violations(tmp_path / "t", cache=first)
    first.flush()
    parse_calls.clear()

    ic.find_import_closure_violations(tmp_path / "t", cache=_cache(tmp_path, "v2"))

    assert parse_calls


def test_resolution_is_never_cached(tmp_path):
    _build(tmp_path / "t", tmp_path / "src")
    cache = _cache(tmp_path)
    assert ("a.py", "x.y") in ic.find_import_closure_violations(tmp_path / "t", cache=cache)[1]

    (tmp_path / "t" / "x" / "y.py").write_text("", encoding="utf-8")

    assert ("a.py", "x.y") not in ic.find_import_closure_violations(tmp_path / "t", cache=cache)[1]


def test_progress_is_called_once_per_file(tmp_path):
    _build(tmp_path / "t", tmp_path / "src")
    seen: list[tuple[int, int]] = []

    ic.find_import_closure_violations(tmp_path / "t", progress=lambda d, n: seen.append((d, n)))

    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]


def _load_publish():
    spec = importlib.util.spec_from_file_location("publish_import_closure_cache_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._bootstrap_engine()
    return module


def test_publish_call_site_caches_in_the_destination_repo_and_reports_progress(tmp_path, monkeypatch, parse_calls):
    publish = _load_publish()
    _build(tmp_path / "t", tmp_path / "src")
    repo = tmp_path / "mirror"
    (repo / ".git").mkdir(parents=True)
    target = types.SimpleNamespace(name="row", source_dir=tmp_path / "src", dest_dir=repo / "coordinator_core")
    now = [0.0]
    out = io.StringIO()
    real_heartbeat = publish._gate_heartbeat
    monkeypatch.setattr(
        publish, "_gate_heartbeat",
        lambda label: real_heartbeat(label, out=out, every=1.0, clock=lambda: now.__setitem__(0, now[0] + 2.0) or now[0]),
    )

    cold = publish._import_closure_gate(target, tmp_path / "t")
    parse_calls.clear()
    warm = publish._import_closure_gate(target, tmp_path / "t")

    assert cold == warm == EXPECTED
    assert parse_calls == []
    assert "[progress] row import closure:" in out.getvalue()
    assert list((repo / ".git" / "claude-klabauter-gate-result").glob("gate-result-import-closure-*.json"))


def test_publish_call_site_without_a_destination_repo_runs_uncached(tmp_path):
    publish = _load_publish()
    _build(tmp_path / "t", tmp_path / "src")
    target = types.SimpleNamespace(name="row", source_dir=tmp_path / "src", dest_dir=None)

    assert publish._import_closure_gate(target, tmp_path / "t") == EXPECTED
