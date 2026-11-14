"""End-of-run gates find the destination repo's cache from a subdirectory row and report progress."""
from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish():
    spec = importlib.util.spec_from_file_location("publish_gate_cache_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish()


def _gate_module(tmp_path: Path):
    path = tmp_path / "fake_gate.py"
    path.write_text("X = 1\n", encoding="utf-8")
    return types.SimpleNamespace(__file__=str(path))


_ENGINE = types.SimpleNamespace(_transform_identity=lambda params: f"identity:{params}")


def test_a_subdirectory_row_finds_the_destination_repos_cache(tmp_path):
    repo = tmp_path / "mirror"
    (repo / ".git").mkdir(parents=True)
    target = types.SimpleNamespace(dest_dir=repo / "coordinator_core")
    cache, digest = publish._gate_result_cache(target, _gate_module(tmp_path), "live-identifier", _ENGINE, "P")
    assert cache is not None and digest == "identity:P"
    cache.put(cache.key("a.py", b"x", digest), {"ok": True})
    cache.flush()
    assert (repo / ".git" / "claude-klabauter-gate-result").is_dir()


def test_no_destination_repo_runs_uncached(tmp_path):
    target = types.SimpleNamespace(dest_dir=tmp_path / "loose" / "dir")
    assert publish._gate_result_cache(target, _gate_module(tmp_path), "g", _ENGINE, "P") == (None, "")


def test_heartbeat_is_silent_before_the_interval_and_names_progress_after():
    now = [0.0]
    out = io.StringIO()
    tick = publish._gate_heartbeat("row live identifiers", out=out, every=30.0, clock=lambda: now[0])
    now[0] = 10.0
    tick(5, 100)
    assert out.getvalue() == ""
    now[0] = 31.0
    tick(40, 100)
    assert "row live identifiers: 40/100 files" in out.getvalue()
    now[0] = 40.0
    tick(50, 100)
    assert out.getvalue().count("[progress]") == 1
