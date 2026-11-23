"""store.load reads PM-accepted decision records and APM memos relaying a PM turn."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


@pytest.fixture(autouse=True)
def _fresh_cache():
    store._FEED_CACHE.clear()
    yield
    store._FEED_CACHE.clear()


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _by_id(root: Path) -> dict[str, Ruling]:
    return {r.id: r for r in store.load(root)}


def test_dr_with_pm_verbatim_heading(tmp_path):
    _w(tmp_path, "docs/decisions/DR-1-verbatim.md",
       "---\ntitle: Gate thresholds\nstatus: accepted\nenforced_by: coordinator_core/x/test_y.py\n"
       "created: 2026-11-01\n---\n\n# DR-1\n\n## PM verbatim\n\nOne millimetre, not five.\n\n## Other\n\nx\n")
    r = _by_id(tmp_path)["DR-1-verbatim"]
    assert r.text == "One millimetre, not five."
    assert r.pm_turn == "docs/decisions/DR-1-verbatim.md"
    assert r.enforcement == "coordinator_core/x/test_y.py"
    assert r.status == "accepted" and r.recorded == "2026-11-01"
    assert "gate" in r.scope


def test_dr_with_authority_pm_uses_summary(tmp_path):
    _w(tmp_path, "coordinator/docs/decisions/DR-2-auth.md",
       "---\ntitle: Auth\nstatus: accepted\nauthority: PM ruling 2026-11-02\nsummary: No licence gate.\n---\nbody\n")
    r = _by_id(tmp_path)["DR-2-auth"]
    assert r.text == "No licence gate."
    assert r.enforcement is None


def test_dr_with_pm_decider_prose_only_owed_reads_as_unenforced(tmp_path):
    _w(tmp_path, "docs/decisions/DR-3-decider.md",
       "---\ntitle: Brightline budget\nstatus: accepted\ndeciders:\n  - \"dbc (PM)\"\n  - em\n"
       "enforcement: prose-only-owed\n---\nbody text\n")
    r = _by_id(tmp_path)["DR-3-decider"]
    assert r.text == "Brightline budget"
    assert r.enforcement is None


def test_non_pm_and_non_accepted_drs_are_excluded(tmp_path):
    _w(tmp_path, "docs/decisions/DR-4-em.md",
       "---\ntitle: EM call\nstatus: accepted\ndeciders:\n  - em\n---\nbody\n")
    _w(tmp_path, "docs/decisions/DR-5-proposed.md",
       "---\ntitle: P\nstatus: proposed\ndeciders:\n  - \"x (PM)\"\n---\nbody\n")
    assert store.load(tmp_path) == []


def test_apm_memo_relaying_pm_turn_is_read_and_unmarked_is_skipped(tmp_path):
    _w(tmp_path, "state/apm-rulings/2026-11-03-relay.md",
       "---\ntitle: Relay\nrelays_pm_turn: sizing.yaml R2\nscope: [licensing]\n---\n\n# APM ruling\n\nPrivate repo.\n")
    _w(tmp_path, "state/apm-rulings/2026-11-03-plain.md", "---\ntitle: Plain\n---\n\n# APM\n\nopinion\n")
    got = _by_id(tmp_path)
    assert set(got) == {"2026-11-03-relay"}
    r = got["2026-11-03-relay"]
    assert (r.pm_turn, r.scope, r.text) == ("sizing.yaml R2", ("licensing",), "Private repo.")


def _apm(root: Path, name: str) -> None:
    _w(root, f"state/apm-rulings/{name}.md",
       f"---\ntitle: {name}\nrelays_pm_turn: sizing.yaml R2\nscope: [x]\n---\n\n# T\n\nBody {name}.\n")


def test_blocking_feed_file_is_a_typed_problem_and_scan_returns_bounded(tmp_path, monkeypatch):
    import threading
    import time

    _apm(tmp_path, "2026-11-03-ok")
    _apm(tmp_path, "2026-11-03-stuck")
    release = threading.Event()
    real = store._read_bytes

    def slow(path, limit):
        if path.stem == "2026-11-03-stuck":
            release.wait(10)
        return real(path, limit)

    monkeypatch.setattr(store, "_read_bytes", slow)
    monkeypatch.setattr(store, "_READ_BOUND_S", 0.05)
    t0 = time.monotonic()
    try:
        rulings, problems = store.scan(tmp_path)
    finally:
        release.set()
    assert time.monotonic() - t0 < 1.0
    assert [r.id for r in rulings] == ["2026-11-03-ok"]
    assert [p["id"] for p in problems] == ["2026-11-03-stuck"]
    assert problems[0]["reason"].startswith("unreadable:")


def test_repeated_stalls_skip_remaining_unread_feeds_as_typed_problems(tmp_path, monkeypatch):
    import threading

    names = [f"2026-11-03-s{i}" for i in range(store._MAX_STALLS + 2)]
    for n in names:
        _apm(tmp_path, n)
    release = threading.Event()
    real = store._read_bytes
    reads = []

    def slow(path, limit):
        reads.append(path.stem)
        release.wait(10)
        return real(path, limit)

    monkeypatch.setattr(store, "_read_bytes", slow)
    monkeypatch.setattr(store, "_READ_BOUND_S", 0.05)
    try:
        rulings, problems = store.scan(tmp_path)
    finally:
        release.set()
    assert rulings == []
    assert len(reads) == store._MAX_STALLS
    assert [p["id"] for p in problems] == names
    assert all(p["reason"].startswith("unreadable:") for p in problems)


def test_unchanged_feed_is_a_cache_hit_and_a_changed_one_is_reread(tmp_path, monkeypatch):
    _apm(tmp_path, "2026-11-03-a")
    assert [r.id for r in store.scan(tmp_path)[0]] == ["2026-11-03-a"]

    def boom(path, limit):
        raise AssertionError("file body read on a cache hit")

    real = store._read_bytes
    monkeypatch.setattr(store, "_read_bytes", boom)
    assert [r.id for r in store.scan(tmp_path)[0]] == ["2026-11-03-a"]

    monkeypatch.setattr(store, "_read_bytes", real)
    p = tmp_path / "state/apm-rulings/2026-11-03-a.md"
    p.write_text(p.read_text(encoding="utf-8") + "more body\n", encoding="utf-8", newline="\n")
    assert "more body" in store.scan(tmp_path)[0][0].text


def test_warm_scan_never_stats_a_feed_file_and_still_sees_edit_add_remove(tmp_path, monkeypatch):
    import os
    import sys

    for n in ("2026-11-03-a", "2026-11-03-b", "2026-11-03-c"):
        _apm(tmp_path, n)
    assert len(store.scan(tmp_path)[0]) == 3

    stated = []
    real_stat = os.stat

    def spy(p, *a, **k):
        stated.append(str(p))
        return real_stat(p, *a, **k)

    monkeypatch.setattr(os, "stat", spy)
    assert len(store.scan(tmp_path)[0]) == 3
    if sys.platform == "win32":  # POSIX DirEntry.stat is one stat per file; only Windows serves it from the listing
        assert not [s for s in stated if s.endswith(".md")]

    monkeypatch.setattr(os, "stat", real_stat)
    p = tmp_path / "state/apm-rulings/2026-11-03-a.md"
    p.write_text(p.read_text(encoding="utf-8") + "edited in place\n", encoding="utf-8", newline="\n")
    _apm(tmp_path, "2026-11-03-d")
    (tmp_path / "state/apm-rulings/2026-11-03-b.md").unlink()
    got = {r.id: r for r in store.scan(tmp_path)[0]}
    assert set(got) == {"2026-11-03-a", "2026-11-03-c", "2026-11-03-d"}
    assert "edited in place" in got["2026-11-03-a"].text


def test_non_matching_feed_reads_only_the_head(tmp_path, monkeypatch):
    _w(tmp_path, "state/apm-rulings/2026-11-03-plain.md", "---\ntitle: Plain\n---\n" + "x" * 50000)
    limits = []
    real = store._read_bytes

    def spy(path, limit):
        limits.append(limit)
        return real(path, limit)

    monkeypatch.setattr(store, "_read_bytes", spy)
    assert store.scan(tmp_path)[0] == []
    assert limits == [store._FM_HEAD_BYTES]


def test_register_id_wins_over_feed_and_bad_feed_is_a_problem(tmp_path):
    store.append(tmp_path, Ruling(id="DR-6-dup", pm_turn="t", scope=("a",), text="register copy"))
    _w(tmp_path, "docs/decisions/DR-6-dup.md",
       "---\ntitle: D\nstatus: accepted\nauthority: PM\n---\nbody\n")
    _w(tmp_path, "docs/decisions/DR-7-bad.md",
       "---\ntitle: D\nstatus: accepted\nauthority: PM\ntargets: [{quantity: q}]\n---\nbody\n")
    rulings, problems = store.scan(tmp_path)
    assert [(r.id, r.text) for r in rulings] == [("DR-6-dup", "register copy")]
    assert [p["id"] for p in problems] == ["DR-7-bad"]
