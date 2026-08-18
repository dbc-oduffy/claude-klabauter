"""
coordinator_core.session_baton.tests.test_store — round-trip, concurrent-write
tolerance, and the no-write-outside-.git/ negative-spec for
coordinator_core.session_baton.store.

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C1.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from coordinator_core.session_baton import store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _all_paths(root: Path):
    return {p for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# baton_path / baton_dir
# ---------------------------------------------------------------------------


def test_baton_path_lives_under_git_coordinator_sessions(tmp_path):
    repo = _make_repo(tmp_path)
    path = store.baton_path("sid-1", cwd=str(repo))
    assert path == repo / ".git" / "coordinator-sessions" / "sid-1" / "baton.json"


def test_baton_path_none_for_empty_sid(tmp_path):
    repo = _make_repo(tmp_path)
    assert store.baton_path("", cwd=str(repo)) is None


def test_baton_path_none_outside_a_git_repo(tmp_path):
    assert store.baton_path("sid-1", cwd=str(tmp_path)) is None


# ---------------------------------------------------------------------------
# read defaults
# ---------------------------------------------------------------------------


def test_read_baton_missing_file_returns_default_skeleton(tmp_path):
    repo = _make_repo(tmp_path)
    record = store.read_baton("sid-missing", cwd=str(repo))
    assert record == store.default_record("sid-missing")


def test_read_baton_corrupt_file_degrades_to_default(tmp_path):
    repo = _make_repo(tmp_path)
    path = repo / ".git" / "coordinator-sessions" / "sid-corrupt" / "baton.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    record = store.read_baton("sid-corrupt", cwd=str(repo))
    assert record == store.default_record("sid-corrupt")


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_round_trips(tmp_path):
    repo = _make_repo(tmp_path)
    record = store.default_record("sid-rt")
    record["first_prompt"] = "hello world"
    record["title"] = "a title"
    record["commits"] = ["abc123"]
    ok = store.write_baton("sid-rt", record, cwd=str(repo))
    assert ok is True

    reread = store.read_baton("sid-rt", cwd=str(repo))
    assert reread["first_prompt"] == "hello world"
    assert reread["title"] == "a title"
    assert reread["commits"] == ["abc123"]
    assert reread["session_id"] == "sid-rt"


def test_merge_baton_first_call_stamps_created_at_once(tmp_path):
    repo = _make_repo(tmp_path)
    merged = store.merge_baton("sid-merge", cwd=str(repo), first_prompt="p1")
    assert merged is not None
    assert merged["created_at"] is not None
    assert merged["first_prompt"] == "p1"

    first_created_at = merged["created_at"]
    merged2 = store.merge_baton("sid-merge", cwd=str(repo), title="t1")
    assert merged2["created_at"] == first_created_at  # not re-stamped
    assert merged2["first_prompt"] == "p1"  # untouched field survives
    assert merged2["title"] == "t1"


def test_merge_baton_is_idempotent_second_call_updates_not_duplicates(tmp_path):
    repo = _make_repo(tmp_path)
    store.merge_baton("sid-idem", cwd=str(repo), title="first")
    merged = store.merge_baton("sid-idem", cwd=str(repo), title="second")
    assert merged["title"] == "second"
    on_disk = store.read_baton("sid-idem", cwd=str(repo))
    assert on_disk["title"] == "second"


def test_merge_baton_dedup_extends_list_fields(tmp_path):
    repo = _make_repo(tmp_path)
    store.merge_baton("sid-list", cwd=str(repo), commits=["c1", "c2"])
    merged = store.merge_baton("sid-list", cwd=str(repo), commits=["c2", "c3"])
    assert merged["commits"] == ["c1", "c2", "c3"]

    store.merge_baton(
        "sid-list", cwd=str(repo), adopted_artifacts=["state/handoffs/a.md"]
    )
    merged2 = store.merge_baton(
        "sid-list", cwd=str(repo), adopted_artifacts=["state/handoffs/a.md", "state/handoffs/b.md"]
    )
    assert merged2["adopted_artifacts"] == [
        "state/handoffs/a.md",
        "state/handoffs/b.md",
    ]


def test_merge_baton_promoted_to_explicit_none_is_settable(tmp_path):
    repo = _make_repo(tmp_path)
    store.merge_baton("sid-promo", cwd=str(repo), promoted_to="state/handoffs/x.md")
    merged = store.read_baton("sid-promo", cwd=str(repo))
    assert merged["promoted_to"] == "state/handoffs/x.md"

    # omitting the kwarg entirely leaves it untouched
    store.merge_baton("sid-promo", cwd=str(repo), title="unrelated")
    still = store.read_baton("sid-promo", cwd=str(repo))
    assert still["promoted_to"] == "state/handoffs/x.md"

    # explicit None resets it
    store.merge_baton("sid-promo", cwd=str(repo), promoted_to=None)
    reset = store.read_baton("sid-promo", cwd=str(repo))
    assert reset["promoted_to"] is None


# ---------------------------------------------------------------------------
# concurrent-write tolerance
# ---------------------------------------------------------------------------


def test_concurrent_merge_calls_do_not_lose_writes(tmp_path):
    repo = _make_repo(tmp_path)
    sid = "sid-concurrent"
    n_threads = 8
    errors = []

    def _worker(i: int) -> None:
        try:
            store.merge_baton(sid, cwd=str(repo), commits=[f"commit-{i}"])
        except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    final = store.read_baton(sid, cwd=str(repo))
    assert sorted(final["commits"]) == sorted(f"commit-{i}" for i in range(n_threads))
    assert len(final["commits"]) == n_threads  # no entry lost, no duplicate


def test_concurrent_write_baton_never_produces_corrupt_json(tmp_path):
    repo = _make_repo(tmp_path)
    sid = "sid-corrupt-race"
    n_threads = 6
    errors = []

    def _worker(i: int) -> None:
        try:
            record = store.default_record(sid)
            record["title"] = f"writer-{i}"
            store.write_baton(sid, record, cwd=str(repo))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    path = store.baton_path(sid, cwd=str(repo))
    text = path.read_text(encoding="utf-8")
    parsed = json.loads(text)  # must never be torn/corrupt
    assert parsed["session_id"] == sid


# ---------------------------------------------------------------------------
# HARD CONSTRAINT: no path outside .git/ is ever written
# ---------------------------------------------------------------------------


def test_no_path_outside_git_is_written(tmp_path):
    repo = _make_repo(tmp_path)
    before = _all_paths(repo)

    store.merge_baton(
        "sid-scope",
        cwd=str(repo),
        first_prompt="p",
        title="t",
        intent="i",
        adopted_artifacts=["state/handoffs/x.md"],
        commits=["c1"],
        promoted_to="state/handoffs/y.md",
    )
    store.write_baton("sid-scope", store.default_record("sid-scope"), cwd=str(repo))

    after = _all_paths(repo)
    new_paths = after - before
    assert new_paths, "expected the baton write to land at least one new file"
    for p in new_paths:
        rel = p.relative_to(repo)
        assert rel.parts[0] == ".git", f"wrote outside .git/: {rel}"

    # explicitly: no file under state/handoffs/ (or anywhere else in the
    # tracked tree) was created by minting/merging a baton.
    assert not (repo / "state" / "handoffs").exists()
