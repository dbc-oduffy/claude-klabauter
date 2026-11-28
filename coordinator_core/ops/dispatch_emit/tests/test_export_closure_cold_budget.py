"""export_closure.test_closure in a fresh interpreter: the 200ms process-time bar and cache fidelity."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import test_reach

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ENGINE_ROOT = Path(__file__).resolve().parents[4]
_CHAIN = 500
BAR_MS = 200

_PROBE = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from coordinator_core.ops.dispatch_emit import export_closure, test_reach
counts = {"imports": 0, "index": 0}
for attr, key in (("_scan_imports", "imports"), ("_scan_index", "index")):
    real = getattr(test_reach, attr)
    def wrap(*a, _real=real, _key=key, **k):
        counts[_key] += 1
        return _real(*a, **k)
    setattr(test_reach, attr, wrap)
start = time.process_time()
closure = export_closure.test_closure(Path(sys.argv[2]), [], [sys.argv[3]])
ms = (time.process_time() - start) * 1000
print(json.dumps({"ms": ms, "none": closure.paths is None, "paths": sorted(closure.paths or ()), **counts}))
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True, creationflags=_NOWIN,
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A root conftest importing a chain of modules: the conftest walk reaches every one."""
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, "conftest.py", "import chain.m0\n")
    _write(root, "chain/__init__.py", "")
    for i in range(_CHAIN):
        _write(root, f"chain/m{i}.py", f"from chain import m{i + 1}\n" if i + 1 < _CHAIN else "X = 1\n")
    _write(root, "extra.py", "Y = 1\n")
    _write(root, "tests/test_a.py", "import chain.m0\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _expected() -> set:
    return {"conftest.py", "chain/__init__.py", "tests/test_a.py", *(f"chain/m{i}.py" for i in range(_CHAIN))}


def _probe(repo: Path, token: str = "tests/test_a.py") -> dict:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(_ENGINE_ROOT), str(repo), token],
        check=True, capture_output=True, text=True, creationflags=_NOWIN,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_second_process_reuses_persisted_parse_and_meets_the_bar(repo: Path) -> None:
    first = _probe(repo)
    second = _probe(repo)
    assert set(first["paths"]) == _expected()
    assert second["paths"] == first["paths"]
    assert first["imports"] >= _CHAIN and first["index"] == 1
    assert (second["imports"], second["index"]) == (0, 0)
    assert second["ms"] < BAR_MS


def test_directory_token_still_refuses_to_scope_from_the_persisted_view(repo: Path) -> None:
    _probe(repo)
    assert _probe(repo, "chain")["none"] is True
    assert _probe(repo, "chain/m1.py")["none"] is False


def test_cache_is_keyed_by_blob_not_by_process_or_worktree_path(repo: Path) -> None:
    _probe(repo)
    moved = repo.parent / "moved"
    repo.rename(moved)
    again = _probe(moved)
    assert set(again["paths"]) == _expected()


def test_dirty_file_is_reread_and_its_new_import_followed(repo: Path) -> None:
    _probe(repo)
    _write(repo, "chain/m5.py", "from chain import m6\nimport extra\n")
    dirty = _probe(repo)
    assert "extra.py" in dirty["paths"]
    assert dirty["imports"] == 1
    again = _probe(repo)
    assert "extra.py" in again["paths"]
    assert again["imports"] == 0


def test_staged_change_invalidates_the_view_and_adds_the_new_tracked_module(repo: Path) -> None:
    _probe(repo)
    _write(repo, "chain/m7.py", "from chain import m8\nimport brand_new\n")
    _write(repo, "brand_new.py", "Z = 1\n")
    _git(repo, "add", "-A")
    after = _probe(repo)
    assert "brand_new.py" in after["paths"]
    assert after["index"] == 1


@pytest.mark.parametrize("victim", ["index-view.bin", "import-records.bin"])
def test_corrupt_cache_file_is_a_miss_not_a_failure(repo: Path, victim: str) -> None:
    first = _probe(repo)
    cache = test_reach._cache_file(repo, victim)
    assert cache.is_file()
    cache.write_bytes(cache.read_bytes()[:40] + b"\xff" * 64)
    assert _probe(repo)["paths"] == first["paths"]


def test_cache_from_another_interpreter_version_is_ignored(repo: Path) -> None:
    first = _probe(repo)
    cache = test_reach._cache_file(repo, "index-view.bin")
    data = bytearray(cache.read_bytes())
    data[4] ^= 0xFF
    cache.write_bytes(bytes(data))
    redo = _probe(repo)
    assert redo["paths"] == first["paths"]
    assert redo["index"] == 1


def test_flush_keeps_only_the_newest_content_keyed_entries_past_the_cap(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    key = str(tmp_path)
    over = test_reach._MAX_CONTENT_KEYED + 5
    store = {test_reach._CONTENT_KEY + i.to_bytes(20, "big"): () for i in range(over)}
    monkeypatch.setitem(test_reach._PY_TABLES, key, ({}, 0))
    monkeypatch.setitem(test_reach._RECORD_STORE, key, store)
    monkeypatch.setattr(test_reach, "_RECORD_DIRTY", {key})
    test_reach.flush_record_cache(tmp_path)
    kept = test_reach._blob_load(test_reach._cache_file(tmp_path, "import-records.bin"))
    assert kept is not None and len(kept) == test_reach._MAX_CONTENT_KEYED
    assert test_reach._CONTENT_KEY + (over - 1).to_bytes(20, "big") in kept
    assert test_reach._CONTENT_KEY + (0).to_bytes(20, "big") not in kept
