"""land_run.script_read residue: the per-path and per-record-dir reads keep their answers."""
from __future__ import annotations

import hashlib

from coordinator_core.ops.dispatch_emit import land_run


def _sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_worktree_blob_hashes_a_file_and_reads_absent_and_dir_as_none(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello\n")
    (tmp_path / "d").mkdir()
    assert land_run._worktree_blob(tmp_path, "a.txt") == _sha(b"hello\n")
    assert land_run._worktree_blob(tmp_path, "missing.txt") is None
    assert land_run._worktree_blob(tmp_path, "d") is None


def test_worktree_blob_does_not_stat_before_the_read(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_bytes(b"x\n")
    calls = []
    real = type(tmp_path).is_file

    def spy(self):
        calls.append(str(self))
        return real(self)

    monkeypatch.setattr(type(tmp_path), "is_file", spy)
    land_run._worktree_blob(tmp_path, "a.txt")
    assert [c for c in calls if c.endswith("a.txt")] == []


def test_superseded_finds_an_earlier_same_sizing_dir_and_skips_a_malformed_one(tmp_path, monkeypatch):
    rec = tmp_path / land_run.RECORD_DIR_ROOT
    earlier = "ask-20261030T100000-aaaaaa"
    other = "ask-20261030T110000-bbbbbb"
    for name, sizing in ((earlier, "state/sizings/2026-10-30-x-111111.yaml"), (other, "state/sizings/2026-10-30-y.yaml")):
        (rec / name).mkdir(parents=True)
        (rec / name / "run.json").write_text('{"sizing_rel": "%s"}' % sizing, encoding="utf-8")
    (rec / "ask-20261030T120000-cccccc").mkdir()  # no run.json: unreadable, skipped

    seen = {}

    def fake_superseded_runs(root, run_id, sizing_rel, candidates, live_run_ids=()):
        seen.setdefault("candidates", list(candidates))
        return ()

    from coordinator_core.ops.dispatch_emit import run_artifacts, run_token

    monkeypatch.setattr(run_artifacts, "superseded_runs", fake_superseded_runs)
    monkeypatch.setattr(run_token, "held_records", lambda root: [])
    out = land_run._superseded(
        tmp_path, "ask-20261030T200000-dddddd", "state/sizings/2026-10-30-x-111111.yaml", object(), run_token,
    )
    assert out == ((), set())
    assert seen["candidates"] == [f"{land_run.RECORD_DIR_ROOT}/{earlier}/run.json"]


# ---- the halted scan answers from the withheld index -------------------------------------------

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index, sibling_claims
from coordinator_core.session import core as session_core

WANTED = "src/landing.py"
SESSION = "5e1f0000-0000-4000-8000-0000000000c2"


def _run_id(n: int) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{n:06x}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SESSION)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SESSION, cwd=str(root))
    return root


def _halted(root, n: int, withheld: str) -> str:
    run_id = _run_id(n)
    rdir = root / land_run.RECORD_DIR_ROOT / run_id
    rdir.mkdir(parents=True)
    (root / withheld).parent.mkdir(parents=True, exist_ok=True)
    (root / withheld).write_text("dirty\n", encoding="utf-8")
    marker = {"status": "halted", "run_artifacts": {"withheld": {withheld: "left in tree"}}}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    return run_id


@pytest.fixture
def marker_reads(monkeypatch):
    seen: list = []
    real = halted_run._marker_text

    def spy(record_dir):
        seen.append(record_dir.name)
        return real(record_dir)

    monkeypatch.setattr(halted_run, "_marker_text", spy)
    return seen


def _scan_reads(root, seen: list) -> int:
    halted_withheld_index.rebuild(root)  # the index is built once, as it is on a live box
    seen.clear()
    land_run.foreign_halted_withheld(root, "ask-own", None, [WANTED])
    sibling_claims.halted_records(root, "ask-own", [WANTED])
    return len(seen)


def test_marker_reads_do_not_grow_with_halted_runs_that_withhold_nothing_of_the_landing(
    tmp_path, repo, marker_reads
):
    for n in range(1, 7):
        _halted(repo, n, f"elsewhere/{n}.py")
    few = _scan_reads(repo, marker_reads)
    for n in range(7, 13):
        _halted(repo, n, f"elsewhere/{n}.py")
    many = _scan_reads(repo, marker_reads)
    assert few == many == 0


def test_a_run_withholding_a_landing_path_still_holds_it_and_is_the_only_marker_read(repo, marker_reads):
    for n in range(1, 6):
        _halted(repo, n, f"elsewhere/{n}.py")
    holder = _halted(repo, 9, WANTED)
    halted_withheld_index.rebuild(repo)
    marker_reads.clear()
    assert land_run.foreign_halted_withheld(repo, "ask-own", None, [WANTED]) == {WANTED: holder}
    assert {name for name in marker_reads} == {holder}


def test_a_directory_withhold_over_a_landing_path_is_not_cleared(repo):
    holder = _halted(repo, 1, "src")
    halted_withheld_index.rebuild(repo)
    assert holder not in sibling_claims.index_clear_runs(repo, [WANTED])


def test_a_run_without_an_index_entry_has_its_marker_read(repo, marker_reads):
    holder = _halted(repo, 1, WANTED)
    halted_withheld_index.rebuild(repo)
    halted_withheld_index.remove_run(repo, holder)
    marker_reads.clear()
    assert sibling_claims.index_clear_runs(repo, [WANTED]) == frozenset()
    assert [r.name for r, _ in sibling_claims.halted_records(repo, "ask-own", [WANTED])] == [holder]
    assert holder in marker_reads


def test_an_unreadable_index_entry_falls_back_to_the_marker(repo):
    holder = _halted(repo, 1, WANTED)
    halted_withheld_index.rebuild(repo)
    (repo / halted_withheld_index.INDEX_DIR / f"{holder}.json").write_text("{not json", encoding="utf-8")
    assert sibling_claims.index_clear_runs(repo, [WANTED]) == frozenset()
    assert [r.name for r, _ in sibling_claims.halted_records(repo, "ask-own", [WANTED])] == [holder]


def test_a_declared_only_entry_is_not_cleared(repo):
    run_id = _run_id(1)
    (repo / land_run.RECORD_DIR_ROOT / run_id).mkdir(parents=True)
    halted_withheld_index.rebuild(repo)
    halted_withheld_index.record_declared(repo, run_id, ["other/x.py"])
    assert sibling_claims.index_clear_runs(repo, [WANTED]) == frozenset()


def test_a_corrupt_marker_of_an_unindexed_run_still_holds_every_landing_path(repo):
    bad = repo / land_run.RECORD_DIR_ROOT / _run_id(1)
    bad.mkdir(parents=True)
    (bad / "halted.json").write_text("{corrupt", encoding="utf-8")
    halted_withheld_index.rebuild(repo)
    assert land_run.foreign_halted_withheld(repo, "ask-own", None, [WANTED]) == {
        WANTED: land_run.UNREADABLE_HALT_RECORD
    }