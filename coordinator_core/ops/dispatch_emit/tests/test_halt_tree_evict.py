"""halt_tree_evict: a halt records the tree's bytes in the halted record and writes nothing to the tree;
`restore` still puts back bytes an older halt moved out, only while the tree holds the recorded head."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halt_tree_evict as hte
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from pathlib import Path
import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BAD = b"from x import undefined_name\r\n"
HEAD = b"ok = 1\r\n"


RUN = "ask-20261113T000000-aaaaaa"


def _setup(tmp_path):
    root = tmp_path / "root"
    rec = tmp_path / "rec"
    root.mkdir()
    (root / "tracked.py").write_bytes(BAD)
    (root / "new.py").write_bytes(BAD)
    (root / "peer.py").write_bytes(b"peer broken\n")
    return root, rec


def test_evict_records_bytes_and_writes_nothing_to_the_tree(tmp_path: Path) -> None:
    root, rec = _setup(tmp_path)
    idx = hte.evict(root, rec, ["tracked.py", "new.py"])
    assert (root / "tracked.py").read_bytes() == BAD
    assert (root / "new.py").read_bytes() == BAD
    assert (root / "peer.py").read_bytes() == b"peer broken\n"
    assert idx["tracked.py"] == {"blob": blob_sha(BAD), "head": blob_sha(BAD)}
    assert idx["new.py"] == {"blob": blob_sha(BAD), "head": blob_sha(BAD)}
    assert "peer.py" not in idx
    assert (rec / "withheld-blobs" / blob_sha(BAD)).read_bytes() == BAD
    assert json.loads((rec / "evicted.json").read_text()) == idx


def test_restore_after_evict_finds_the_tree_in_place_and_is_idempotent(tmp_path: Path) -> None:
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py", "new.py"])
    own = ["tracked.py", "new.py"]
    assert hte.restore(root, rec, owned_paths=own) == {"restored": [], "drift": {}, "refused": {}}
    assert hte.restore(root, rec, owned_paths=own) == {"restored": [], "drift": {}, "refused": {}}
    assert (root / "tracked.py").read_bytes() == BAD


def test_restore_refuses_on_drift_and_leaves_tree(tmp_path: Path) -> None:
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py", "new.py"])
    (root / "tracked.py").write_bytes(b"someone else\n")
    (root / "new.py").unlink()
    out = hte.restore(root, rec, owned_paths=["tracked.py", "new.py"])
    assert out == {
        "restored": [],
        "drift": {"tracked.py": "restore-drift", "new.py": "restore-drift"},
        "refused": {},
    }
    assert (root / "tracked.py").read_bytes() == b"someone else\n"
    assert not (root / "new.py").exists()


def test_restore_without_index_is_empty(tmp_path: Path) -> None:
    assert hte.restore(tmp_path, tmp_path / "none") == {"restored": [], "drift": {}, "refused": {}}


def _git(repo, *args) -> None:
    import os
    import subprocess

    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _evicted_world(tmp_path):
    """A git repo whose HEAD holds A, an older-format record (bytes WITHHELD, tree reset to A), and the
    record dir; the working file holds A's bytes, exactly what `restore` requires before it writes."""
    repo, rec = tmp_path / "repo", tmp_path / "rec"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "t.py").write_bytes(b"a = 1\n")
    (repo / "u.py").write_bytes(b"u = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "A")
    withheld = b"a = 1\nfrom x import undefined_name\n"
    (rec / "withheld-blobs").mkdir(parents=True)
    (rec / "withheld-blobs" / blob_sha(withheld)).write_bytes(withheld)
    (rec / "evicted.json").write_text(json.dumps({
        "t.py": {"blob": blob_sha(withheld), "head": blob_sha(b"a = 1\n")},
        "u.py": {"blob": blob_sha(withheld), "head": blob_sha(b"u = 1\n")},
    }))
    return repo, rec, withheld


def _refusals(rec):
    return {e["path"]: e["reason"] for e in json.loads((rec / "tree-write-refusals.json").read_text())}


def test_restore_after_head_moved_past_recorded_head_leaves_tree_and_records(tmp_path: Path) -> None:
    repo, rec, _ = _evicted_world(tmp_path)
    (repo / "t.py").write_bytes(b"a = 2\n")  # commit B changes the path at HEAD ...
    _git(repo, "add", "t.py")
    _git(repo, "commit", "-q", "-m", "B")
    (repo / "t.py").write_bytes(b"a = 1\n")  # ... while the working file still holds A's bytes
    out = hte.restore(repo, rec, owned_paths=["t.py", "u.py"], run_id=RUN)
    assert out["refused"] == {"t.py": "older-than-head"}
    assert (repo / "t.py").read_bytes() == b"a = 1\n"
    assert _refusals(rec)["t.py"] == "older-than-head"


def test_restore_outside_own_paths_leaves_tree_and_records(tmp_path: Path) -> None:
    repo, rec, withheld = _evicted_world(tmp_path)
    out = hte.restore(repo, rec, owned_paths=["t.py"], run_id=RUN)
    assert out["restored"] == ["t.py"] and out["refused"] == {"u.py": "outside-own-paths"}
    assert (repo / "t.py").read_bytes() == withheld
    assert (repo / "u.py").read_bytes() == b"u = 1\n"
    assert _refusals(rec) == {"u.py": "outside-own-paths"}


def test_restore_without_owned_paths_writes_nothing(tmp_path: Path) -> None:
    repo, rec, _ = _evicted_world(tmp_path)
    out = hte.restore(repo, rec)
    assert out["restored"] == [] and set(out["refused"].values()) == {"outside-own-paths"}
    assert (repo / "t.py").read_bytes() == b"a = 1\n"


def test_evict_skips_absent_path_and_unlisted_paths(tmp_path: Path) -> None:
    root, rec = _setup(tmp_path)
    assert hte.evict(root, rec, ["gone.py"]) == {}
    assert not (rec / "evicted.json").exists()
    assert (root / "peer.py").exists()


def _git_repo(tmp_path, files):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    import os

    full = {**os.environ, **env}
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, env=full, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return repo


def test_broken_paths_flags_undefined_import_and_cascading_importer(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import halt_tree_check

    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "def old():\n    pass\n",
                                "pkg/b.py": "x = 1\n"})
    (repo / "pkg/a.py").write_text("from pkg.b import undefined_name\ndef old():\n    pass\n")
    (repo / "pkg/b.py").write_text("from pkg.a import old\nx = 1\n")
    out = halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py", "pkg/b.py"])
    assert "pkg/a.py" in out
    assert "pkg/b.py" not in out


def test_broken_paths_clean_and_syntax(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import halt_tree_check

    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "y = 1\n"})
    (repo / "pkg/a.py").write_text("y = 2\n")
    assert halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py"]) == {}
    (repo / "pkg/a.py").write_text("def (:\n")
    assert "syntax error" in halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py"])["pkg/a.py"]


def test_second_evict_records_the_bytes_the_tree_holds_now(tmp_path: Path) -> None:
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py"])
    (root / "tracked.py").write_bytes(HEAD)
    again = hte.evict(root, rec, ["tracked.py"])
    assert again["tracked.py"] == {"blob": blob_sha(HEAD), "head": blob_sha(HEAD)}
    assert (root / "tracked.py").read_bytes() == HEAD
    assert hte.restore(root, rec, owned_paths=["tracked.py"])["restored"] == []


def _declare(repo, run_id, paths) -> None:
    """Give `run_id` a record whose manifest declares `paths` (the run's write set)."""
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "review_declared_paths": paths}), encoding="utf-8"
    )


def test_restore_for_run_writes_only_the_record_runs_write_set(tmp_path: Path) -> None:
    repo, rec, withheld = _evicted_world(tmp_path)
    _declare(repo, RUN, ["t.py"])
    out = hte.restore_for_run(repo, rec, RUN)
    assert out["restored"] == ["t.py"] and out["refused"] == {"u.py": "outside-own-paths"}
    assert (repo / "t.py").read_bytes() == withheld
    assert (repo / "u.py").read_bytes() == b"u = 1\n"
    assert _refusals(rec) == {"u.py": "outside-own-paths"}


def test_restore_for_run_refuses_the_path_head_changed_and_restores_the_unchanged_one(tmp_path: Path) -> None:
    repo, rec, withheld = _evicted_world(tmp_path)
    _declare(repo, RUN, ["t.py", "u.py"])
    (repo / "t.py").write_bytes(b"a = 2\n")  # HEAD moves t.py past the recorded run base ...
    _git(repo, "add", "t.py")
    _git(repo, "commit", "-q", "-m", "B")
    (repo / "t.py").write_bytes(b"a = 1\n")  # ... while the working file still holds the old bytes
    out = hte.restore_for_run(repo, rec, RUN)
    assert out["refused"] == {"t.py": "older-than-head"}
    assert out["restored"] == ["u.py"]
    assert (repo / "t.py").read_bytes() == b"a = 1\n"
    assert (repo / "u.py").read_bytes() == withheld
    assert _refusals(rec) == {"t.py": "older-than-head"}


def test_restore_for_run_with_an_unreadable_record_writes_nothing(tmp_path: Path) -> None:
    repo, rec, _ = _evicted_world(tmp_path)
    out = hte.restore_for_run(repo, rec, RUN)
    assert out["restored"] == [] and set(out["refused"].values()) == {"outside-own-paths"}
    assert (repo / "t.py").read_bytes() == b"a = 1\n"
    assert (repo / "u.py").read_bytes() == b"u = 1\n"


@pytest.mark.parametrize("foreign_write", [False, True])
def test_refire_uses_only_the_recorded_own_delta(tmp_path, foreign_write):
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import write_object
    from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE

    repo, rec, raw_parked = _evicted_world(tmp_path)
    _declare(repo, RUN, ["t.py"])
    own = b"a = 2\n"
    assert raw_parked != own
    oid = write_object(resolve_git_common_dir(repo), b"blob", own)
    (rec / WITHHELD_OWN_DELTA_FILE).write_text(
        json.dumps({"paths": {"t.py": {"blob": oid}}}), encoding="utf-8",
    )
    if foreign_write:
        (repo / "t.py").write_bytes(b"a = 3\n")

    result = hte.restore_own_delta(repo, rec, RUN)

    assert result["u.py"] == {"status": "skipped", "reason": "outside-own-paths"}
    if foreign_write:
        assert result["t.py"] == {"status": "skipped", "reason": "restore-drift"}
        assert (repo / "t.py").read_bytes() == b"a = 3\n"
    else:
        assert result["t.py"] == {"status": "restored"}
        assert (repo / "t.py").read_bytes() == own


def test_restore_own_delta_writes_nothing_over_a_tree_that_holds_the_recorded_bytes(tmp_path: Path) -> None:
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import write_object
    from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE

    repo, rec, _ = _evicted_world(tmp_path)
    _declare(repo, RUN, ["t.py"])
    parked = b"a = 2\n"
    (repo / "t.py").write_bytes(parked)
    hte.evict(repo, rec, ["t.py"])
    oid = write_object(resolve_git_common_dir(repo), b"blob", parked)
    (rec / WITHHELD_OWN_DELTA_FILE).write_text(json.dumps({"paths": {"t.py": {"blob": oid}}}), encoding="utf-8")

    assert hte.restore_own_delta(repo, rec, RUN)["t.py"] == {"status": "already-restored"}
    assert (repo / "t.py").read_bytes() == parked


def _lines(edits=None) -> bytes:
    edits = edits or {}
    return b"".join(edits.get(i, b"line%d\r\n" % i) for i in range(1, 9))


FOREIGN = {7: b"foreign7\r\n"}
OWN = {2: b"from x import undefined_name\r\n"}


def _spliced_world(tmp_path, withheld_edits):
    """Git repo whose HEAD holds `_lines()`, a live file carrying a foreign line-7 hunk, and an older-format
    record whose withheld bytes are `_lines(withheld_edits)` with `head` = the live bytes, `base` = HEAD."""
    repo, rec = tmp_path / "repo", tmp_path / "rec"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "p.py").write_bytes(_lines())
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "A")
    live = _lines(FOREIGN)
    (repo / "p.py").write_bytes(live)
    withheld = _lines(withheld_edits)
    (rec / "withheld-blobs").mkdir(parents=True)
    (rec / "withheld-blobs" / blob_sha(withheld)).write_bytes(withheld)
    (rec / "evicted.json").write_text(json.dumps({
        "p.py": {"blob": blob_sha(withheld), "head": blob_sha(live), "base": blob_sha(_lines())},
    }))
    return repo, rec, live


def test_restore_over_a_live_foreign_hunk_keeps_that_hunk(tmp_path: Path) -> None:
    repo, rec, _ = _spliced_world(tmp_path, OWN)
    out = hte.restore(repo, rec, owned_paths=["p.py"], run_id=RUN)
    assert out == {"restored": ["p.py"], "drift": {}, "refused": {}}
    assert (repo / "p.py").read_bytes() == _lines({**FOREIGN, **OWN})


def test_restore_that_clashes_with_a_live_foreign_hunk_is_refused_and_recorded(tmp_path: Path) -> None:
    repo, rec, live = _spliced_world(tmp_path, {7: b"mine7\r\n"})
    out = hte.restore(repo, rec, owned_paths=["p.py"], run_id=RUN)
    assert out["restored"] == [] and "overlaps" in out["refused"]["p.py"]
    assert (repo / "p.py").read_bytes() == live
    assert _refusals(rec)["p.py"] == out["refused"]["p.py"]


def test_repeated_restore_records_each_refusal_once(tmp_path: Path) -> None:
    repo, rec, _ = _evicted_world(tmp_path)
    hte.restore(repo, rec, owned_paths=["t.py"], run_id=RUN)
    hte.restore(repo, rec, owned_paths=["t.py"], run_id=RUN)
    assert len(json.loads((rec / "tree-write-refusals.json").read_text())) == 1
