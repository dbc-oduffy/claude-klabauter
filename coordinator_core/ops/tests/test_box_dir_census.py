"""box.dir_census: owner/age/size/liveness rows, the warm size cache, and the op's registration."""
import os
import time
from pathlib import Path

import pytest

from coordinator_core import owner_mark
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import box_dir_census as bdc
from coordinator_core.ops.claude_home_census import Walk


@pytest.fixture(autouse=True)
def _fresh_cache():
    bdc._CACHE.clear()
    yield
    bdc._CACHE.clear()


def _no_keep(_path):
    return False


def _fill(directory: Path, nbytes: int, name: str = "blob.bin") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"x" * nbytes)
    return directory


def _census(roots, **kw):
    kw.setdefault("keep", _no_keep)
    kw.setdefault("alive", owner_mark.owner_alive)
    return bdc.census(roots, **kw)


def test_rows_carry_owner_repo_age_size_and_liveness(tmp_path):
    root = tmp_path / "scratch"
    _fill(root / "repo-a" / "run1", 100)
    _fill(root / "repo-b" / "run2", 300)
    owner_mark.write_mark(root / "repo-a" / "run1", "pid", 424242, repo=str(tmp_path / "marked-repo"))
    old = time.time() - 7200
    os.utime(root / "repo-b" / "run2", (old, old))

    out = _census(
        [bdc.Root("scratch_root", root, depth=2, owner_from_path=True)],
        alive=lambda mark: False if mark else None,
    )
    rows = {Path(r["path"]).name: r for r in out["rows"]}

    assert set(rows) == {"run1", "run2"}
    assert rows["run1"]["owner_repo"] == "marked-repo"
    assert rows["run1"]["live"] is False
    assert rows["run1"]["owner_kind"] == "pid"
    assert rows["run2"]["owner_repo"] == "repo-b"
    assert rows["run2"]["live"] is None
    assert rows["run2"]["age_s"] >= 7200
    assert rows["run2"]["size_bytes"] == 300 and rows["run2"]["size_stale"] is False
    mark_bytes = (root / "repo-a" / "run1" / owner_mark.MARK_FILE).stat().st_size
    assert [Path(r["path"]).name for r in out["rows"]] == ["run1", "run2"]
    assert rows["run1"]["size_bytes"] == 100 + mark_bytes
    assert out["total_bytes"] == 400 + mark_bytes and out["stale_rows"] == 0


def test_live_owner_reads_true_and_kept_follows_the_keep_set(tmp_path):
    root = tmp_path / "warp"
    _fill(root / "mine", 10)
    _fill(root / "publish", 10)
    owner_mark.write_mark(root / "mine", "pid", os.getpid())

    out = _census(
        [bdc.Root("warp", root, repo="claude-klabauter")],
        alive=lambda mark: True if mark else None,
        keep=lambda p: Path(p).name == "publish",
    )
    rows = {Path(r["path"]).name: r for r in out["rows"]}

    assert rows["mine"]["live"] is True and rows["mine"]["kept"] is False
    assert rows["publish"]["live"] is None and rows["publish"]["kept"] is True
    assert rows["publish"]["owner_repo"] == "claude-klabauter"


def test_warm_hit_walks_nothing_below_an_unchanged_mtime(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _fill(root / "a" / "deep" / "deeper", 50)
    _fill(root / "b", 70)
    roots = [bdc.Root("r", root)]
    first = _census(roots)

    def refuse(*_a, **_k):
        raise AssertionError("a warm hit must not walk")

    monkeypatch.setattr(bdc, "measure", refuse)
    second = _census(roots)

    assert [r["size_bytes"] for r in second["rows"]] == [r["size_bytes"] for r in first["rows"]] == [70, 50]
    assert second["stale_rows"] == 0


def test_a_finished_size_older_than_the_age_cap_is_remeasured(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _fill(root / "a" / "deep", 50)
    roots = [bdc.Root("r", root)]
    _census(roots)
    for entry in bdc._CACHE.values():
        entry.born -= bdc._MAX_ENTRY_AGE_S + 1
    (root / "a" / "deep" / "blob.bin").write_bytes(b"x" * 90)  # subdir growth leaves the top mtime alone
    out = _census(roots)

    assert [r["size_bytes"] for r in out["rows"]] == [90]


def test_a_moved_mtime_remeasures_only_that_dir(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _fill(root / "a", 50)
    _fill(root / "b", 70)
    roots = [bdc.Root("r", root)]
    _census(roots)

    _fill(root / "a", 25, name="more.bin")
    later = time.time() + 5
    os.utime(root / "a", (later, later))
    measured = []
    real = bdc.measure
    monkeypatch.setattr(bdc, "measure", lambda p, b, resume=None: measured.append(Path(p).name) or real(p, b, resume=resume))
    out = _census(roots)

    assert measured == ["a"]
    assert {Path(r["path"]).name: r["size_bytes"] for r in out["rows"]} == {"a": 75, "b": 70}


def test_an_unfinished_size_is_reported_stale_then_resumed(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _fill(root / "big", 40)
    roots = [bdc.Root("r", root)]
    real = bdc.measure
    monkeypatch.setattr(bdc, "measure", lambda p, b, resume=None: _started_only(p, resume))

    out = _census(roots)
    assert out["rows"][0]["size_stale"] is True and out["stale_rows"] == 1

    monkeypatch.setattr(bdc, "measure", real)
    out = _census(roots)
    assert out["rows"][0]["size_stale"] is False and out["rows"][0]["size_bytes"] == 40


def _started_only(path, resume):
    walk = resume if resume is not None else Walk()
    walk.started = True
    walk.pending = [os.fspath(path)]
    return walk


def test_the_cache_drops_dirs_that_left_the_enumeration(tmp_path):
    root = tmp_path / "r"
    _fill(root / "a", 5)
    _fill(root / "b", 5)
    roots = [bdc.Root("r", root)]
    _census(roots)
    assert len(bdc._CACHE) == 2

    import shutil
    shutil.rmtree(root / "a")
    _census(roots)
    assert list(bdc._CACHE) == [str(root / "b")]


def test_default_keep_set_marks_members_their_descendants_and_ancestors_only(tmp_path, monkeypatch):
    member = tmp_path / "fleet" / "claude-klabauter-publish-engine"
    for p in (member, member / "inner", tmp_path / "fleet" / "other", tmp_path / "unrelated"):
        p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(owner_mark, "keep_paths", lambda: {"publish-engine": [member]})

    kept = bdc._keep_predicate(owner_mark)

    assert kept(member) is True
    assert kept(member / "inner") is True
    assert kept(tmp_path / "fleet") is True
    assert kept(tmp_path / "fleet" / "other") is False
    assert kept(tmp_path / "unrelated") is False


def test_a_missing_root_yields_no_rows(tmp_path):
    out = _census([bdc.Root("gone", tmp_path / "nope")])
    assert out["rows"] == [] and out["total_bytes"] == 0


def test_warm_census_over_many_dirs_is_under_the_bar(tmp_path):
    root = tmp_path / "r"
    for i in range(60):
        _fill(root / f"d{i}", 10)
    roots = [bdc.Root("r", root)]
    _census(roots)

    start = time.process_time()
    out = _census(roots)
    elapsed = time.process_time() - start

    assert len(out["rows"]) == 60 and out["stale_rows"] == 0
    assert elapsed < 0.5


def test_op_rejects_unknown_and_out_of_range_params():
    with pytest.raises(CallerFacingValidationError):
        bdc.dir_census({"bogus": 1})
    with pytest.raises(CallerFacingValidationError):
        bdc.dir_census({"budget_ms": bdc.MAX_BUDGET_MS + 1})
    with pytest.raises(CallerFacingValidationError):
        bdc.dir_census({"limit": True})


def test_op_default_arguments_return_rows_over_the_resolved_roots(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _fill(root / "a", 9)
    monkeypatch.setattr(bdc, "default_roots", lambda repo_root=None: [bdc.Root("r", root, repo="x")])
    monkeypatch.setattr(owner_mark, "keep_paths", lambda: {})

    out = bdc.dir_census({})

    assert [Path(r["path"]).name for r in out["rows"]] == ["a"]
    assert set(out["rows"][0]) >= {"path", "owner_repo", "age_s", "size_bytes", "live"}
    assert [r["path"] for r in bdc.dir_census({"limit": 1})["rows"]] == [out["rows"][0]["path"]]


def test_op_is_registered_compute_only():
    from coordinator_core import ipc

    assert OP_CLASSIFICATION["box.dir_census"] is OpClass.COMPUTE_ONLY
    assert ipc.get_op_handler("box.dir_census") is not None


def test_default_roots_name_the_engine_writer_roots(tmp_path, monkeypatch):
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: str(tmp_path / "sr") if key == "paths.scratch_root" else None)
    labels = [r.label for r in bdc.default_roots(tmp_path / "repo")]

    assert labels[:3] == ["scratch_root", "coordinator-local", "warp"]
    assert "fleet-temp" in labels
