"""Pins engine_key's digest against an independent os.stat reference."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT
from coordinator_core.roadmap import probe_store
from coordinator_core.roadmap.probe_store import engine_key


def _reference_key(sha, root):
    """HEAD's digest recomputed from the closure file list and one os.stat per file."""
    digest = hashlib.sha256()
    for path in probe_store._closure(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            st = os.stat(path)
        except OSError:
            digest.update(f"{rel}\0absent\n".encode())
            continue
        digest.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return f"{sha}:{digest.hexdigest()[:32]}"


def _old(path: Path) -> None:
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))


def _fake_engine(tmp_path: Path, monkeypatch) -> Path:
    eng = tmp_path / "eng"
    (eng / ".git").mkdir(parents=True)
    files = {
        "coordinator_core/__init__.py": "",
        "coordinator_core/ops/__init__.py": "",
        "coordinator_core/ops/plan_mise_prep_probe.py":
            "from coordinator_core import mod_a\n",
        "coordinator_core/mod_a.py":
            "def f():\n    from coordinator_core import mod_b\n    return mod_b\n",
        "coordinator_core/mod_b.py": "x = 1\n",
        "coordinator_core/outside.py": "y = 2\n",
        "coordinator_core/frontmatter/schemas/plan.schema.json": "{}",
        "coordinator/bin/mise-prep-upgrade.py": "import json\n",
    }
    for rel, text in files.items():
        path = eng / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _old(path)
    monkeypatch.setattr(probe_store, "ENGINE_CHECKOUT_ROOT", eng)
    return eng


def test_live_tree_key_matches_stat_reference():
    key = engine_key("abc")
    if key is None:
        pytest.skip("a closure file was edited since import")
    root = os.path.normpath(str(ENGINE_CHECKOUT_ROOT))
    assert key == _reference_key("abc", root)


def test_fake_engine_key_matches_stat_reference_including_absent(tmp_path, monkeypatch):
    eng = _fake_engine(tmp_path, monkeypatch)
    root = os.path.normpath(str(eng))
    probe_store._GRAPH_SEEDED.discard(root)
    key = engine_key("abc")
    assert key is not None
    assert key == _reference_key("abc", root)
    (eng / "coordinator_core" / "mod_b.py").unlink()
    after = engine_key("abc")
    assert after is not None
    assert after == _reference_key("abc", root)
    assert after != key
