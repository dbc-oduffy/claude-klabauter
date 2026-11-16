"""stranded_sibling: the touched-blobs record, the stranded() judgement and ownerless()."""

from __future__ import annotations

import json
import os

from coordinator_core.ops.dispatch_emit import stranded_sibling as ss
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from pathlib import Path

MOD = "coordinator_core/x/mod.py"
TST = "coordinator_core/x/tests/test_mod.py"


def _blob(b: bytes) -> str:
    return blob_sha(b)


def _record(warp, run_id, touched, mtime=None):
    d = warp / run_id
    d.mkdir(parents=True)
    ss.write(d, touched)
    if mtime is not None:
        os.utime(d / ss.TOUCHED_FILE, (mtime, mtime))
    return d


def _tree(tmp_path, files) -> None:
    for rel, data in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def test_write_read_roundtrip_and_bad_record(tmp_path: Path) -> None:
    ss.write(tmp_path, {MOD: {"base": None, "run": "aa"}})
    assert ss.read(tmp_path) == {MOD: {"base": None, "run": "aa"}}
    (tmp_path / ss.TOUCHED_FILE).write_text("{not json", encoding="utf-8")
    assert ss.read(tmp_path) == {}
    assert ss.read(tmp_path / "absent") == {}


def test_kind() -> None:
    assert ss.kind_of(MOD) == "module"
    assert ss.kind_of(TST) == "test"
    assert ss.kind_of("a/test_b.py") == "test"


def test_unlanded_module_is_named(tmp_path: Path) -> None:
    warp = tmp_path / "state" / "warp"
    run_mod = _blob(b"new module")
    _record(warp, "r1", {MOD: {"base": None, "run": run_mod}, TST: {"base": None, "run": _blob(b"t")}})
    _tree(tmp_path, {MOD: b"new module"})
    hits = ss.stranded(tmp_path, warp, [TST], {MOD: None, TST: None})
    assert hits == [{"path": MOD, "run": "r1", "kind": "module", "reason": "unlanded"}]


def test_lost_when_worktree_differs_from_run_blob(tmp_path: Path) -> None:
    warp = tmp_path / "state" / "warp"
    _record(warp, "r1", {MOD: {"base": "b0", "run": _blob(b"run")}, TST: {"base": None, "run": "t"}})
    _tree(tmp_path, {MOD: b"someone else"})
    hits = ss.stranded(tmp_path, warp, [TST], {MOD: "b0"})
    assert [(h["path"], h["reason"]) for h in hits] == [(MOD, "lost")]


def test_not_refused_when_sibling_landed_or_already_at_run_blob(tmp_path: Path) -> None:
    warp = tmp_path / "state" / "warp"
    run = _blob(b"run")
    _record(warp, "r1", {MOD: {"base": None, "run": run}, TST: {"base": None, "run": "t"}})
    assert ss.stranded(tmp_path, warp, [MOD, TST], {}) == []
    assert ss.stranded(tmp_path, warp, [TST], {MOD: run}) == []


def test_no_claim_when_landing_misses_the_record(tmp_path: Path) -> None:
    warp = tmp_path / "state" / "warp"
    _record(warp, "r1", {MOD: {"base": None, "run": "x"}})
    assert ss.stranded(tmp_path, warp, ["other.py"], {}) == []
    assert ss.stranded(tmp_path, tmp_path / "nowhere", [MOD], {}) == []


def test_newest_record_judges_a_shared_path(tmp_path: Path) -> None:
    warp = tmp_path / "state" / "warp"
    _record(warp, "old", {MOD: {"base": None, "run": "old-blob"}, TST: {"base": None, "run": "t"}}, 1000)
    _record(warp, "new", {MOD: {"base": "old-blob", "run": "new-blob"}}, 2000)
    # Landing the test of the old run: MOD was superseded by the newer run, so the old run never claims it.
    hits = ss.stranded(tmp_path, warp, [TST], {MOD: "old-blob"})
    assert hits == []


def test_shared_file_names_the_other_runs_sibling(tmp_path: Path) -> None:
    warp = tmp_path / "state" / "warp"
    shared = "coordinator/bin/unimatrix-install.py"
    seed = "coordinator/bin/unimatrix-doctrine-seed.py"
    _record(warp, "6ad434", {shared: {"base": "s0", "run": "s1"}, seed: {"base": "d0", "run": _blob(b"seed")}})
    _tree(tmp_path, {seed: b"seed"})
    hits = ss.stranded(tmp_path, warp, [shared], {shared: "s0", seed: "d0"})
    assert hits == [{"path": seed, "run": "6ad434", "kind": "module", "reason": "unlanded"}]


def test_single_record_dir_is_judged(tmp_path: Path) -> None:
    d = _record(tmp_path, "r1", {MOD: {"base": None, "run": "x"}, TST: {"base": None, "run": "t"}})
    assert [h["path"] for h in ss.stranded(tmp_path, d, [TST], {})] == [MOD]


def test_ownerless_names_undeclared_dirty_paths(tmp_path: Path) -> None:
    run = tmp_path / "state" / "warp" / "r1"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps({"write_baseline": {MOD: "x"}, "rows": [{"writes": [TST]}], "doc_writes": ["d.md"]}),
        encoding="utf-8",
    )
    assert ss.ownerless(tmp_path, [MOD, TST, "d.md", "stray.py"]) == ["stray.py"]
    assert ss.ownerless(tmp_path / "none", ["stray.py"]) == []


def test_recent_run_dirs_skips_runs_older_than_the_cap(tmp_path: Path) -> None:
    n = ss.OWNERLESS_SCAN_CAP + 1
    for i in range(n):
        d = tmp_path / f"r{i:03d}"
        d.mkdir()
        os.utime(d, (1_000_000 + i, 1_000_000 + i))
    names = [name for name, _ in ss.recent_run_dirs(tmp_path)]
    assert len(names) == ss.OWNERLESS_SCAN_CAP
    assert "r000" not in names
    assert f"r{n - 1:03d}" in names
    assert [name for name, _ in ss.recent_run_dirs(tmp_path, exclude=f"r{n - 1:03d}")][-1] == f"r{n - 2:03d}"
