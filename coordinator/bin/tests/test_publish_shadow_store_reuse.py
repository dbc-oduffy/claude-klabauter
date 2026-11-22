"""`publish._git_materialize_ref` reuses a completed `ShadowStore` entry across processes;
`_cleanup_shadow_roots` retains a store shadow and still removes a non-store one."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_BIN_DIR = str(REPO_ROOT / "coordinator" / "bin")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

import publish  # noqa: E402

# Spawns real git and child interpreters; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CHILD = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, sys.argv[1])
    sys.path.insert(0, sys.argv[2])
    import publish
    from pathlib import Path

    calls = []
    real = publish._extract_git_archive
    publish._extract_git_archive = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    withheld = json.loads(sys.argv[4]) or None
    out = publish._git_materialize_ref(Path(sys.argv[3]), withheld=withheld)
    print("CHILD-RESULT " + json.dumps({"extractions": len(calls), "exists": (out / "a.txt").is_file()}))
    """
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _child(repo: Path, temp: Path, withheld: dict | None = None) -> dict:
    env = {**__import__("os").environ, "TMP": str(temp), "TEMP": str(temp), "TMPDIR": str(temp)}
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(REPO_ROOT), _BIN_DIR, str(repo), json.dumps(withheld or {})],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("CHILD-RESULT ")]
    assert lines, f"child failed: {proc.stdout}\n{proc.stderr}"
    result = json.loads(lines[-1].removeprefix("CHILD-RESULT "))
    result["hit"] = "store HIT" in proc.stdout
    return result


def test_second_process_reuses_the_first_process_shadow(repo: Path, tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    first = _child(repo, temp)
    second = _child(repo, temp)
    assert (first["extractions"], first["hit"]) == (1, False)
    assert (second["extractions"], second["hit"], second["exists"]) == (0, True, True)


def test_a_differing_withheld_digest_re_extracts(repo: Path, tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    _child(repo, temp)
    withheld = _child(repo, temp, {"a.txt": None})
    assert (withheld["extractions"], withheld["hit"], withheld["exists"]) == (1, False, False)
    assert _child(repo, temp, {"a.txt": None})["extractions"] == 0


def test_an_entry_without_its_marker_is_never_served(repo: Path, monkeypatch) -> None:
    publish._MATERIALIZED_REF_CACHE.clear()
    entry = publish._git_materialize_ref(repo)
    (entry / ".complete").unlink()
    publish._MATERIALIZED_REF_CACHE.clear()
    calls = []
    real = publish._extract_git_archive
    monkeypatch.setattr(publish, "_extract_git_archive", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    served = publish._git_materialize_ref(repo)
    assert calls == [1]
    publish._cleanup_shadow_roots(tuple(publish._MATERIALIZED_REF_CACHE.values()))
    assert not served.exists()


def test_cleanup_renames_a_stale_store_entry_and_never_deletes_it(repo: Path) -> None:
    import os
    import time

    publish._MATERIALIZED_REF_CACHE.clear()
    stored = publish._git_materialize_ref(repo)
    store, key = publish._STORE_SHADOWS[stored]
    stale = store.root / "stale-unmarked-entry"
    stale.mkdir()
    (stale / "big.bin").write_text("x", encoding="utf-8")
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))

    publish._cleanup_shadow_roots((stored,))

    assert not stale.exists()
    doomed = [p for p in store.root.iterdir() if ".deleting-" in p.name]
    assert len(doomed) == 1 and (doomed[0] / "big.bin").is_file()
    assert (stored / "a.txt").is_file()


def test_cleanup_retains_a_store_shadow_and_removes_a_non_store_one(repo: Path, tmp_path: Path, monkeypatch) -> None:
    publish._MATERIALIZED_REF_CACHE.clear()
    stored = publish._git_materialize_ref(repo)
    loose = tmp_path / "loose-shadow"
    loose.mkdir()
    (loose / "f.txt").write_text("x", encoding="utf-8")
    publish._MATERIALIZED_REF_CACHE[("loose#inject:f.txt", "HEAD")] = loose

    publish._cleanup_shadow_roots((stored, loose))

    assert (stored / "a.txt").is_file() and (stored / ".complete").is_file()
    assert not loose.exists()
    assert not publish._MATERIALIZED_REF_CACHE
    calls = []
    real = publish._extract_git_archive
    monkeypatch.setattr(publish, "_extract_git_archive", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    assert publish._git_materialize_ref(repo) == stored
    assert calls == []
