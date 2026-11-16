"""A publish.py running from a commit clone keys its round pin by the source repo, not the clone dir."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_chain_export_pin_under_test", "publish.py")


def test_engine_key_of_a_commit_clone_is_the_source_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "commit-clone.json").write_text(
        json.dumps({"sha": "a" * 40, "source_root": str(source)}), encoding="utf-8"
    )
    monkeypatch.setattr(publish, "_REPO_ROOT", clone)
    monkeypatch.setattr(publish, "_bootstrap_engine", lambda: None)
    publish._engine_toplevel_key.cache_clear()
    try:
        assert Path(publish._engine_toplevel_key()) == source
    finally:
        publish._engine_toplevel_key.cache_clear()


def test_commit_clone_carries_the_files_publish_loads_beside_itself() -> None:
    clone = _load("commit_clone_sparse_roots_under_test", "lib/commit_clone.py")
    src = (_BIN_DIR / "publish.py").read_text(encoding="utf-8")
    sibling_names = set(re.findall(r'Path\(__file__\)\.resolve\(\)\.parent / "([^"/]+)"', src))
    seeded = {Path(p).name for p in clone._SEED_FILES}
    missing = sorted(n for n in sibling_names if "." in n and n not in seeded)
    assert missing == []
