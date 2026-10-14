"""Tests for handoff_claim_index: signature revalidation, corrupt index, racing writers, no spawns."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from unittest import mock

from coordinator_core.session import handoff_claim_index as idx


def _handoff(d: Path, name: str, extra: str = "") -> Path:
    p = d / name
    p.write_text(f"---\ntitle: t\n{extra}\n---\n\nBody\n", encoding="utf-8")
    return p


def _setup(tmp_path):
    hdir = tmp_path / "state" / "handoffs"
    hdir.mkdir(parents=True)
    return hdir, idx.cache_path_for(tmp_path / ".git")


def _holders(hdir, cache, paths=None):
    paths = paths if paths is not None else sorted(hdir.glob("*.md"))
    return idx.mirror_holders(paths, handoffs_dir=hdir, cache_path=cache)


def _reads():
    return mock.patch.object(idx, "_read_mirror_claim", wraps=idx._read_mirror_claim)


def test_cold_then_warm_reads_nothing(tmp_path):
    hdir, cache = _setup(tmp_path)
    a = _handoff(hdir, "a.md", "claimed_by: s1")
    b = _handoff(hdir, "b.md")
    with _reads() as spy:
        cold = _holders(hdir, cache)
    assert cold == {a: "s1", b: None} and spy.call_count == 2 and cache.exists()
    with _reads() as spy:
        warm = _holders(hdir, cache)
    assert warm == cold and spy.call_count == 0


def test_in_place_same_size_edit_is_reread(tmp_path):
    hdir, cache = _setup(tmp_path)
    a = _handoff(hdir, "a.md", "claimed_by: s1")
    st = a.stat()
    _holders(hdir, cache)
    a.write_text(a.read_text(encoding="utf-8").replace("s1", "s2"), encoding="utf-8")
    assert a.stat().st_size == st.st_size
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    with _reads() as spy:
        assert _holders(hdir, cache) == {a: "s2"}
    assert spy.call_count == 1


def test_added_and_removed(tmp_path):
    hdir, cache = _setup(tmp_path)
    a = _handoff(hdir, "a.md", "claimed_by: s1")
    b = _handoff(hdir, "b.md", "claimed_by: s2")
    _holders(hdir, cache)
    b.unlink()
    c = _handoff(hdir, "c.md", "claimed_by: s3")
    with _reads() as spy:
        assert _holders(hdir, cache) == {a: "s1", c: "s3"}
    assert spy.call_count == 1
    assert set(json.loads(cache.read_text(encoding="utf-8"))["entries"]) == {"a.md", "c.md"}


def test_unchanged_run_does_not_rewrite_index(tmp_path):
    hdir, cache = _setup(tmp_path)
    _handoff(hdir, "a.md", "claimed_by: s1")
    _holders(hdir, cache)
    with mock.patch.object(idx, "_save") as save:
        _holders(hdir, cache)
    save.assert_not_called()


def test_corrupt_missing_and_skewed_index_is_a_cold_build(tmp_path):
    hdir, cache = _setup(tmp_path)
    a = _handoff(hdir, "a.md", "claimed_by: s1")
    cache.parent.mkdir(parents=True)
    for bad in ("{not json", "[]", json.dumps({"version": 999, "handoffs_dir": str(hdir), "entries": {}}),
                json.dumps({"version": idx.INDEX_SCHEMA_VERSION, "handoffs_dir": "elsewhere", "entries": {}}),
                json.dumps({"version": idx.INDEX_SCHEMA_VERSION, "handoffs_dir": str(hdir), "entries": {"a.md": "x"}})):
        cache.write_text(bad, encoding="utf-8")
        assert _holders(hdir, cache) == {a: "s1"}
        assert json.loads(cache.read_text(encoding="utf-8"))["version"] == idx.INDEX_SCHEMA_VERSION
    cache.unlink()
    assert _holders(hdir, cache) == {a: "s1"}


def test_no_cache_path_still_answers(tmp_path):
    hdir, _ = _setup(tmp_path)
    a = _handoff(hdir, "a.md", "claimed_by: s1")
    assert idx.mirror_holders([a], handoffs_dir=hdir, cache_path=None) == {a: "s1"}


def test_racing_writers_leave_a_readable_index(tmp_path):
    hdir, cache = _setup(tmp_path)
    for i in range(30):
        _handoff(hdir, f"h{i:02}.md", f"claimed_by: s{i}")
    errors = []

    def run():
        try:
            for _ in range(5):
                os.utime(hdir / "h00.md", None)
                _holders(hdir, cache)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    loaded = idx._load(cache, str(hdir))
    assert len(loaded) == 30
    assert not [p for p in cache.parent.iterdir() if p.name.endswith(".tmp")]


def test_zero_spawns(tmp_path):
    hdir, cache = _setup(tmp_path)
    a = _handoff(hdir, "a.md", "claimed_by: s1")
    with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawn")):
        _holders(hdir, cache)
        assert _holders(hdir, cache) == {a: "s1"}
