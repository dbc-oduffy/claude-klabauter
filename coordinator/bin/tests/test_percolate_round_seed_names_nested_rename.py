from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import coordinator_core.percolate.store as store

_BIN_DIR = Path(__file__).resolve().parent.parent
_LIB_DIR = _BIN_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_seed_names", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _seed_names(tmp_path, monkeypatch, seed):
    store_file = tmp_path / _mod._STORE_REL
    store_file.parent.mkdir(parents=True)
    store_file.write_text("", encoding="utf-8")
    section = {
        "basename_rename": [
            {"src": "example-game-repo-for-your-ue-project.md", "dst": "example-game-repo-for-your-ue-project.md"},
            {"src": "codename-dir/", "dst": "public-dir/"},
        ]
    }
    monkeypatch.setattr(store, "load_store", lambda path: {})
    monkeypatch.setattr(store, "resolve_target", lambda loaded, target: section)
    return _mod._published_seed_names(str(tmp_path), "wiki", set(seed))


def test_flat_entry_is_renamed(tmp_path, monkeypatch):
    assert _seed_names(tmp_path, monkeypatch, ["example-game-repo-for-your-ue-project.md"]) == {
        "example-game-repo-for-your-ue-project.md"
    }


def test_nested_entry_is_renamed_and_keeps_its_directory(tmp_path, monkeypatch):
    assert _seed_names(
        tmp_path, monkeypatch, ["mcp-topology/example-game-repo-for-your-ue-project.md", "mcp-topology/other.md"]
    ) == {"mcp-topology/example-game-repo-for-your-ue-project.md", "mcp-topology/other.md"}
