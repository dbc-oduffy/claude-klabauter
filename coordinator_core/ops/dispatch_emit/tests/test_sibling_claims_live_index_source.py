"""sibling_claims sources its candidate runs from the live-run and halted-withheld indexes, never a `state/warp` scan."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import (
    halted_hold_release,
    halted_withheld_index,
    landing_halted_view,
    live_run_index,
    live_run_writes,
    run_token,
    sibling_claims,
)
from coordinator_core.ops.dispatch_emit.halt_resume_record import MISSING, STALLED
from coordinator_core.ops.dispatch_emit.tests.test_sibling_claims import (  # noqa: F401 -- `repo` is a fixture
    OWN,
    SELF,
    SIZING,
    _aged,
    _declare,
    _halt,
    repo,
)

pytestmark = pytest.mark.complete

STALE_AGE = run_token.TTL_SECONDS + 7200


def _warp(repo: Path) -> str:
    return os.fspath(repo / "state" / "warp")


def _watch_warp_listings(monkeypatch, repo: Path) -> list:
    """Record each `os.scandir`/`os.listdir` of `state/warp`."""
    seen: list = []
    warp = _warp(repo)
    real_scandir, real_listdir = os.scandir, os.listdir

    def scandir(path=".", *a, **k):
        if os.fspath(path) == warp:
            seen.append(("scandir", warp))
        return real_scandir(path, *a, **k)

    def listdir(path=".", *a, **k):
        if os.fspath(path) == warp:
            seen.append(("listdir", warp))
        return real_listdir(path, *a, **k)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    return seen


def _halted_indexed(repo: Path, run_id: str, path: str) -> None:
    _declare(repo, run_id, path)
    _halt(repo / "state" / "warp" / run_id, path)
    halted_withheld_index.add_run(repo, run_id, [path], declared=[path])


def test_candidates_follow_mint_halt_and_land(repo):
    live, halted = _aged(100, "aa0001"), _aged(90, "aa0002")
    assert sibling_claims.live_unlanded_candidates(repo) == []

    _declare(repo, live, "p/a.py")
    run_token.mint(repo, live, "a.mjs", session_id=SELF)
    assert sibling_claims.live_unlanded_candidates(repo) == [live]

    _halted_indexed(repo, halted, "p/b.py")
    assert sibling_claims.live_unlanded_candidates(repo) == sorted([live, halted])

    live_run_writes.index_remove(repo, live)
    halted_withheld_index.remove_run(repo, halted)
    assert sibling_claims.live_unlanded_candidates(repo) == []


def test_candidates_drop_runs_past_the_token_ttl_but_keep_unstamped_ids(repo):
    stale, fresh = _aged(STALE_AGE, "aa0003"), _aged(60, "aa0004")
    for run_id in (stale, fresh, "run-plain"):
        run_token.mint(repo, run_id, "a.mjs", session_id=SELF)
    assert sibling_claims.live_unlanded_candidates(repo) == sorted([fresh, "run-plain"])


def test_thousands_of_stale_run_dirs_are_never_listed_or_opened(repo, monkeypatch):
    for i in range(1500):
        (repo / "state" / "warp" / _aged(STALE_AGE + i, f"{i:06x}")).mkdir(parents=True)
    live, halted = _aged(100, "bb0001"), _aged(90, "bb0002")
    _declare(repo, live, "p/a.py")
    run_token.mint(repo, live, "a.mjs", session_id=SELF)
    _halted_indexed(repo, halted, "p/b.py")
    halted_withheld_index.rebuild(repo)  # the one migration; a built index is what a warm engine holds

    def no_scan(root):
        raise AssertionError("record_stamps scans every run dir")

    monkeypatch.setattr(halted_hold_release, "record_stamps", no_scan)
    seen = _watch_warp_listings(monkeypatch, repo)
    opened: list = []
    real_read = run_token.read_cached
    monkeypatch.setattr(run_token, "read_cached", lambda p: opened.append(os.fspath(p)) or real_read(p))

    with halted_hold_release.release_scope():
        assert sibling_claims._in_window_run_ids(repo, OWN) == [halted]
        assert [r.name for r, _m in sibling_claims.halted_records(repo, OWN)] == [halted]
        assert sibling_claims._released_ask_ids(repo, OWN) == frozenset()
        assert set(sibling_claims._unlanded_run_ids(repo, OWN)) <= {live, halted}

    assert seen == []
    assert all(Path(p).stem in (live, halted) for p in opened)


def test_unlanded_run_ids_lists_a_live_and_a_halted_indexed_run_and_skips_the_own_run(repo):
    live, halted = _aged(100, "cc0001"), _aged(90, "cc0002")
    _declare(repo, live, "p/a.py")
    run_token.mint(repo, live, "a.mjs", session_id=SELF)
    _halted_indexed(repo, halted, "p/b.py")
    assert sibling_claims._unlanded_run_ids(repo, OWN) == sorted([live, halted])
    assert sibling_claims._unlanded_run_ids(repo, halted) == [live]


@pytest.mark.parametrize("sentinel", [STALLED, MISSING])
def test_unreadable_live_index_holds_rather_than_clears(repo, monkeypatch, sentinel):
    run_id = _aged(100, "dd0001")
    _declare(repo, run_id, "p/a.py")
    run_token.mint(repo, run_id, "a.mjs", session_id="5e1f0000-0000-4000-8000-0000000000d1")
    monkeypatch.setattr(live_run_index, "read", lambda root, deadline_ms, **k: sentinel)

    with pytest.raises(sibling_claims.LiveIndexUnavailable):
        sibling_claims.live_unlanded_candidates(repo)
    claims = sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py", "q/z.py"])
    assert claims == {"p/a.py": sibling_claims.LIVE_INDEX_UNAVAILABLE, "q/z.py": sibling_claims.LIVE_INDEX_UNAVAILABLE}
    assert sibling_claims.shared_dir_holds(repo, OWN, ["p/own.py"], ["p/x.py"]) == ["p/x.py"]
    assert sibling_claims._unlanded_run_ids(repo, OWN) == []


def test_missing_live_index_with_no_token_records_is_an_empty_live_set_not_a_hold(repo, monkeypatch):
    monkeypatch.setattr(live_run_index, "read", lambda root, deadline_ms, **k: MISSING)
    assert sibling_claims.live_unlanded_candidates(repo) == []
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {}


def test_readable_live_index_does_not_hold(repo):
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {}


def test_ask_successor_landed_lists_state_warp_once_per_scope_and_reads_only_the_sizing_prefix(repo, monkeypatch):
    stem = Path(SIZING).stem
    since = "20261119T000000"
    ask = f"ask-{since}-aaaaaa"
    landed = f"warp-{stem}-20261119T010000-abcdef"
    unlanded = f"warp-{stem}-20261119T020000-abcd01"
    older = f"warp-{stem}-20261118T010000-abcd02"
    for i in range(400):
        (repo / "state" / "warp" / f"warp-other-{i:04d}-20261119T010000-abcdef").mkdir(parents=True)
    for name in (landed, unlanded, older):
        (repo / "state" / "warp" / name).mkdir(parents=True)
    run_token.mint(repo, landed, "a.mjs", session_id=SELF)
    path = run_token._record_path(repo, landed, spent=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"landed": {"sha": "x"}}', encoding="utf-8")

    seen = _watch_warp_listings(monkeypatch, repo)
    read: list = []
    real_read = run_token.read_cached
    monkeypatch.setattr(run_token, "read_cached", lambda p: read.append(Path(p).stem) or real_read(p))

    def no_stamp(run_id):
        raise AssertionError("a stamp is parsed")

    monkeypatch.setattr(landing_halted_view, "stamp", no_stamp)

    with halted_hold_release.release_scope():
        assert sibling_claims._ask_successor_landed(repo, ask, SIZING) is True
        assert sibling_claims._ask_successor_landed(repo, ask, SIZING) is True
    assert len(seen) == 1
    assert set(read) <= {landed, unlanded}


def test_ask_successor_landed_is_false_when_no_later_run_of_the_sizing_landed(repo):
    stem = Path(SIZING).stem
    (repo / "state" / "warp" / f"warp-{stem}-20261119T020000-abcd01").mkdir(parents=True)
    (repo / "state" / "warp" / "warp-other-20261119T020000-abcd01").mkdir(parents=True)
    assert sibling_claims._ask_successor_landed(repo, "ask-20261119T000000-aaaaaa", SIZING) is False
