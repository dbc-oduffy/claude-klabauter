"""The halted-run claim lookup reads git a constant number of times, whatever the halted-run count."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import sibling_claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str, stdin: str = "") -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _repo_with_halted_runs(root: Path, count: int) -> list:
    """`count` halted runs, each withholding its own file whose kept blob differs from HEAD (so each holds)."""
    _git(root, "init", "-q")
    paths = []
    for i in range(count):
        _write(root / f"f{i}.py", f"x = {i}\n")
        paths.append(f"f{i}.py")
        run_id = _rid(300 + i, f"{i:06x}")
        kept = _git(root, "hash-object", "-w", "--stdin", stdin=f"x = {i + 100}\n")
        rec = root / "state/warp" / run_id
        sizing = f"state/sizings/s{i}.yaml"
        _write(root / sizing, "status: open\n")
        _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
        _write(rec / "halted.json", json.dumps({
            "status": "halted", "sizing_rel": sizing,
            "run_artifacts": {"withheld": {f"f{i}.py": "halt-record-only"}},
        }))
        _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {f"f{i}.py": kept}}}, "plan_gate": None}))
    _git(root, "add", *paths)
    _git(root, "commit", "-q", "-m", "head")
    for p in paths:  # dirty withheld paths: a clean-tree release cannot free the run
        _write(root / p, "# uncommitted\n")
    return paths


def _count_fetches(monkeypatch) -> list:
    calls: list = []
    real = hr._blob_fetch

    def counting(root, specs):
        calls.append(list(specs))
        return real(root, specs)

    monkeypatch.setattr(hr, "_blob_fetch", counting)
    return calls


@pytest.mark.parametrize("count", [1, 6])
def test_halted_run_write_claims_spawns_once_for_any_halted_count(tmp_path, monkeypatch, count):
    paths = _repo_with_halted_runs(tmp_path, count)
    calls = _count_fetches(monkeypatch)
    claims = wrd.halted_run_write_claims(tmp_path, paths, exclude_run=None, own_plan=None)
    assert sorted(claims) == sorted(paths)  # every run still holds its withheld path
    assert len(calls) == 1


@pytest.mark.parametrize("count", [1, 6])
def test_sibling_run_claims_spawns_once_for_any_halted_count(tmp_path, monkeypatch, count):
    paths = _repo_with_halted_runs(tmp_path, count)
    calls = _count_fetches(monkeypatch)
    claims = sibling_claims.sibling_run_claims(tmp_path, _rid(5, "ffffff"), paths)
    assert sorted(claims) == sorted(paths)
    assert len(calls) == 1


@pytest.mark.parametrize("count", [1, 6])
def test_co_declared_peer_holds_reads_kept_blobs_once_for_any_halted_count(tmp_path, monkeypatch, count):
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    paths = _repo_with_halted_runs(tmp_path, count)
    calls = _count_fetches(monkeypatch)
    scans: list = []
    real_scan = sibling_claims._unlanded_run_ids
    monkeypatch.setattr(
        sibling_claims, "_unlanded_run_ids", lambda *a, **k: scans.append(a) or real_scan(*a, **k),
    )
    holds = peer_hunk_gate.co_declared_peer_holds(tmp_path, _rid(5, "ffffff"), paths, paths)
    assert sorted(p for p, _r in holds) == sorted(paths)  # every halted run still holds its path
    assert len(calls) == 1
    assert len(scans) == 1


def test_a_released_run_is_still_released_in_the_batch(tmp_path, monkeypatch):
    """Both sides: the run whose kept blob is at HEAD frees its path; the others still hold."""
    paths = _repo_with_halted_runs(tmp_path, 3)
    head_blob = _git(tmp_path, "rev-parse", "HEAD:f1.py")
    rec = next(p for p in (tmp_path / "state/warp").iterdir() if (p / "halted.json").read_text().find("f1.py") >= 0)
    _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {"f1.py": head_blob}}}, "plan_gate": None}))
    calls = _count_fetches(monkeypatch)
    claims = wrd.halted_run_write_claims(tmp_path, paths, exclude_run=None, own_plan=None)
    assert sorted(claims) == ["f0.py", "f2.py"]
    assert len(calls) == 1


def test_blob_scope_memoises_and_fetches_only_the_missing(tmp_path, monkeypatch):
    _repo_with_halted_runs(tmp_path, 1)
    calls = _count_fetches(monkeypatch)
    with hr.blob_scope(tmp_path):
        first = hr._blob_batch(tmp_path, ["HEAD:f0.py"])
        again = hr._blob_batch(tmp_path, ["HEAD:f0.py", "HEAD:nope.py"])
    assert first == {"HEAD:f0.py": b"x = 0\n"}
    assert again == {"HEAD:f0.py": b"x = 0\n", "HEAD:nope.py": None}
    assert calls == [["HEAD:f0.py"], ["HEAD:nope.py"]]
    hr._blob_batch(tmp_path, ["HEAD:f0.py"])
    assert len(calls) == 3  # outside a scope nothing is memoised


@pytest.fixture
def walk_repo(tmp_path):
    """A repo with a base commit then 5 commits, each rewriting a.py and b.py."""
    hr._WALKS.clear()
    hr._ENTRIES.clear()
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "a.py", "a = 0\n")
    _write(tmp_path / "b.py", "b = 0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    for i in range(1, 6):
        _write(tmp_path / "a.py", f"a = {i}\n")
        _write(tmp_path / "b.py", f"b = {i}\n")
        _git(tmp_path, "commit", "-aq", "-m", f"c{i}")
    return tmp_path, base


def _count_commit_reads(monkeypatch) -> list:
    from coordinator_core.git import git_objects

    calls: list = []
    real = git_objects.read_object

    def counting(common, sha):
        got = real(common, sha)
        if got is not None and got[0] == "commit":
            calls.append(sha)
        return got

    monkeypatch.setattr(git_objects, "read_object", counting)
    return calls


def test_history_blobs_second_call_at_same_head_reads_no_commit(walk_repo, monkeypatch):
    root, base = walk_repo
    first = hr._history_blobs(root, base, ["a.py", "b.py"])
    assert len(first["a.py"]) == 5
    reads = _count_commit_reads(monkeypatch)
    assert hr._history_blobs(root, base, ["a.py", "b.py"]) == first
    assert reads == []


def test_one_read_wall_runs_a_history_walk_on_one_worker_not_one_per_object(walk_repo, monkeypatch):
    from coordinator_core.git import git_objects

    root, base = walk_repo
    starts: list = []
    real = git_objects.run_bounded
    monkeypatch.setattr(git_objects, "run_bounded", lambda *a, **k: starts.append(a[0]) or real(*a, **k))

    git_objects._OBJECT_CACHE.clear()
    hr._WALKS.clear()
    hr._ENTRIES.clear()
    walled = hr._one_read_wall(root, "t", lambda: hr._history_blobs(root, base, ["a.py", "b.py"]))
    assert len(starts) == 1

    # Other side: the same walk outside the wall starts a worker per object read.
    starts.clear()
    git_objects._OBJECT_CACHE.clear()
    hr._WALKS.clear()
    hr._ENTRIES.clear()
    assert hr._history_blobs(root, base, ["a.py", "b.py"]) == walled
    assert len(starts) > 6


def test_history_blobs_other_record_paths_share_the_walk_without_leaking(walk_repo, monkeypatch):
    root, base = walk_repo
    only_a = hr._history_blobs(root, base, ["a.py"])
    reads = _count_commit_reads(monkeypatch)
    only_b = hr._history_blobs(root, base, ["b.py"])
    assert list(only_a) == ["a.py"] and list(only_b) == ["b.py"]
    assert only_b["b.py"] != only_a["a.py"]
    assert len(only_b["b.py"]) == 5
    assert len(reads) <= 6  # trees for the new path only; the spine is not walked again


def test_history_blobs_walks_again_after_head_moves_and_per_base(walk_repo, monkeypatch):
    root, base = walk_repo
    before = hr._history_blobs(root, base, ["a.py"])
    _write(root / "a.py", "a = 99\n")
    _git(root, "commit", "-aq", "-m", "c6")
    after = hr._history_blobs(root, base, ["a.py"])
    assert len(after["a.py"]) == len(before["a.py"]) + 1
    other_base = _git(root, "rev-parse", "HEAD~2")
    assert len(hr._history_blobs(root, other_base, ["a.py"])["a.py"]) == 2


def test_history_blobs_span_too_long_raises_for_every_caller_without_rewalking(walk_repo, monkeypatch):
    root, base = walk_repo
    monkeypatch.setattr(hr, "_HISTORY_COMMITS", 3)
    with pytest.raises(ValueError, match="span too long"):
        hr._history_blobs(root, base, ["a.py"])
    reads = _count_commit_reads(monkeypatch)
    with pytest.raises(ValueError, match="span too long"):
        hr._history_blobs(root, base, ["b.py"])
    assert reads == []


def test_history_blobs_unreadable_commit_is_not_cached(walk_repo, monkeypatch):
    from coordinator_core.git import git_objects

    root, base = walk_repo
    real = git_objects.read_object
    monkeypatch.setattr(git_objects, "read_object", lambda common, sha: None)
    with pytest.raises(ValueError, match="unreadable commit"):
        hr._history_blobs(root, base, ["a.py"])
    monkeypatch.setattr(git_objects, "read_object", real)
    assert len(hr._history_blobs(root, base, ["a.py"])["a.py"]) == 5


def _index_runs(root: Path, count: int) -> list:
    """`count` withholding runs, each with one index file naming `src/r<i>.py`; sentinel written."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    names = []
    for i in range(count):
        run_id = _rid(300 + i, f"{i:06x}")
        _write(idx._dir(root) / f"{run_id}.json", json.dumps({"paths": [f"src/r{i}.py"], "at_ns": 1}))
        names.append(run_id)
    _write(idx._dir(root) / idx.SENTINEL, "")
    return names


def _count_index_reads(monkeypatch) -> list:
    reads: list = []
    real = Path.read_bytes

    def counting(self):
        if "halt-withheld" in str(self):
            reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    return reads


def test_withheld_owners_repeat_call_reads_only_the_index_files_that_changed(tmp_path, monkeypatch):
    """A warm engine's second scan reads no unchanged run file; a rewritten one is re-read and answered anew."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    hr._OWNER_RAW.clear()
    names = _index_runs(tmp_path, 40)
    reads = _count_index_reads(monkeypatch)
    assert hr._withheld_owners(tmp_path, {"src/r7.py"}) == {"src/r7.py": names[7]}
    assert len(reads) == 40 + 1  # every file once, plus the parse of the one that names the wanted path
    reads.clear()
    assert hr._withheld_owners(tmp_path, {"src/r7.py"}) == {"src/r7.py": names[7]}
    assert reads == []

    _write(idx._dir(tmp_path) / f"{names[3]}.json", json.dumps({"paths": ["src/r7.py", "src/new.py"], "at_ns": 2}))
    assert hr._withheld_owners(tmp_path, {"src/r7.py", "src/new.py"}) == {
        "src/r7.py": max(names[3], names[7]), "src/new.py": names[3],  # lexically last run wins
    }
    assert names[3] + ".json" in reads and len(reads) <= 3


def test_withheld_owners_forgets_a_deleted_index_file(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    hr._OWNER_RAW.clear()
    names = _index_runs(tmp_path, 3)
    assert hr._withheld_owners(tmp_path, {"src/r1.py"}) == {"src/r1.py": names[1]}
    (idx._dir(tmp_path) / f"{names[1]}.json").unlink()
    assert hr._withheld_owners(tmp_path, {"src/r1.py"}) == {}
    assert not any(k.endswith(names[1] + ".json") for k in hr._OWNER_RAW)


def test_withheld_owners_stops_at_the_read_deadline_instead_of_reading_on(tmp_path, monkeypatch):
    """A stalled disk spends the deadline once; the files not reached read as withholding nothing."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    hr._OWNER_RAW.clear()
    names = _index_runs(tmp_path, 40)
    ticks = iter(range(10_000))
    monkeypatch.setattr(idx, "_clock", lambda: float(next(ticks)))  # one "second" per check
    reads = _count_index_reads(monkeypatch)
    got = hr._withheld_owners(tmp_path, {f"src/r{i}.py" for i in range(40)})
    assert len(reads) <= int(idx.READ_DEADLINE_SECS) + 1 + len(got)
    assert 0 < len(got) < 40
    assert all(got[f"src/r{i}.py"] == names[i] for i in range(40) if f"src/r{i}.py" in got)

    monkeypatch.undo()
    hr._OWNER_RAW.clear()
    assert len(hr._withheld_owners(tmp_path, {f"src/r{i}.py" for i in range(40)})) == 40  # unbounded clock: all read


@pytest.mark.parametrize("count", [1, 6])
def test_foreign_halted_withheld_fetches_only_blobs_of_runs_withholding_a_wanted_path(tmp_path, monkeypatch, count):
    """A landing whose paths no halted run withholds reads no blob; the one run that does is read alone."""
    from coordinator_core.ops.dispatch_emit import land_run

    paths = _repo_with_halted_runs(tmp_path, count)
    calls = _count_fetches(monkeypatch)
    assert land_run.foreign_halted_withheld(tmp_path, _rid(5, "ffffff"), None, ["unrelated.py"]) == {}
    assert calls == []
    held = land_run.foreign_halted_withheld(tmp_path, _rid(5, "ffffff"), None, [paths[0]])
    assert list(held) == [paths[0]]  # the withholding run still holds against its own path
    assert len(calls) == 1
    assert all(f"f{i}.py" not in spec for spec in calls[0] for i in range(1, count))
