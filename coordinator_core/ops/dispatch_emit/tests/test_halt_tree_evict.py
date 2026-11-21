"""halt_tree_evict: bytes leave the tree into the record, return only while the tree holds HEAD bytes."""

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


def _manifest(root, baseline, run=RUN) -> None:
    """Stage `run` against `baseline` ({path: bytes | None}); returns nothing, objects land in the store."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    shas = {
        p: None if b is None else git_objects.write_object(resolve_git_common_dir(root), b"blob", b)
        for p, b in baseline.items()
    }
    rdir = root / "state" / "warp" / run
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [], "doc_writes": [], "write_baseline": shas}), encoding="utf-8"
    )


def _setup(tmp_path):
    root = tmp_path / "root"
    rec = tmp_path / "rec"
    root.mkdir()
    _manifest(root, {"tracked.py": HEAD, "new.py": None})
    (root / "tracked.py").write_bytes(BAD)
    (root / "new.py").write_bytes(BAD)
    (root / "peer.py").write_bytes(b"peer broken\n")
    return root, rec


def test_evict_stores_bytes_resets_tree_and_leaves_peer(tmp_path: Path) -> None:
    root, rec = _setup(tmp_path)
    idx = hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None}, run_id=RUN)
    assert (root / "tracked.py").read_bytes() == HEAD
    assert not (root / "new.py").exists()
    assert (root / "peer.py").read_bytes() == b"peer broken\n"
    assert idx["tracked.py"] == {"blob": blob_sha(BAD), "head": blob_sha(HEAD)}
    assert idx["new.py"]["head"] is None
    assert (rec / "withheld-blobs" / blob_sha(BAD)).read_bytes() == BAD
    assert json.loads((rec / "evicted.json").read_text()) == idx


def _heads(monkeypatch, heads) -> None:
    """Pin the HEAD blob shas `restore` reads ({path: bytes | None}); the fixture roots are not git repos."""
    shas = {p: blob_sha(b) for p, b in heads.items() if b is not None}
    monkeypatch.setattr(hte, "_current_heads", lambda root, paths: shas)


def test_restore_round_trip_byte_identical_and_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, rec = _setup(tmp_path)
    _heads(monkeypatch, {"tracked.py": HEAD, "new.py": None})
    hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None}, run_id=RUN)
    own = ["tracked.py", "new.py"]
    first = hte.restore(root, rec, owned_paths=own)
    assert first == {"restored": ["new.py", "tracked.py"], "drift": {}, "refused": {}}
    assert (root / "tracked.py").read_bytes() == BAD
    assert (root / "new.py").read_bytes() == BAD
    assert hte.restore(root, rec, owned_paths=own) == {"restored": [], "drift": {}, "refused": {}}


def test_restore_refuses_on_drift_and_leaves_tree(tmp_path: Path) -> None:
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None}, run_id=RUN)
    (root / "tracked.py").write_bytes(b"someone else\n")
    (root / "new.py").write_bytes(b"peer made this\n")
    out = hte.restore(root, rec, owned_paths=["tracked.py", "new.py"])
    assert out == {
        "restored": [],
        "drift": {"tracked.py": "restore-drift", "new.py": "restore-drift"},
        "refused": {},
    }
    assert (root / "tracked.py").read_bytes() == b"someone else\n"
    assert (root / "new.py").read_bytes() == b"peer made this\n"


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
    """A git repo whose HEAD holds A, an evicted record (bytes WITHHELD, tree reset to A), and the
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
    assert hte.evict(root, rec, ["gone.py"], {"gone.py": HEAD}, run_id=RUN) == {}
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


def test_second_evict_after_reset_keeps_original_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, rec = _setup(tmp_path)
    _heads(monkeypatch, {"tracked.py": HEAD})
    hte.evict(root, rec, ["tracked.py"], {"tracked.py": HEAD}, run_id=RUN)
    again = hte.evict(root, rec, ["tracked.py"], {"tracked.py": HEAD}, run_id=RUN)
    assert again["tracked.py"]["blob"] == blob_sha(BAD)
    assert (root / "tracked.py").read_bytes() == HEAD
    assert hte.restore(root, rec, owned_paths=["tracked.py"])["restored"] == ["tracked.py"]


def _lines(edits=None) -> bytes:
    edits = edits or {}
    return b"".join(edits.get(i, b"line%d\r\n" % i) for i in range(1, 9))


FOREIGN = {7: b"foreign7\r\n"}
OWN = {2: b"from x import undefined_name\r\n"}


def _shared(tmp_path, baseline_edits, tree_edits, prints_edits=None):
    """HEAD file with a foreign hunk (line 7) and the run's hunk (line 2); the run staged against `baseline_edits`."""
    root = tmp_path / "root"
    root.mkdir()
    _manifest(root, {"p.py": _lines(baseline_edits)})
    if prints_edits is not None:
        from coordinator_core.git import git_objects
        from coordinator_core.git.git_dir import resolve_git_common_dir

        sha = git_objects.write_object(resolve_git_common_dir(root), b"blob", _lines(prints_edits))
        (root / "state" / "warp" / RUN / "write_prints.json").write_text(json.dumps({"p.py": sha}))
    (root / "p.py").write_bytes(_lines(tree_edits))
    return root, tmp_path / "rec"


def test_unattributed_foreign_hunk_survives_byte_identical_and_own_hunk_reverts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, rec = _shared(tmp_path, FOREIGN, {**FOREIGN, **OWN})
    before = (root / "p.py").read_bytes()
    refused: dict = {}
    idx = hte.evict(root, rec, ["p.py"], {"p.py": _lines()}, run_id=RUN, refused=refused)
    assert refused == {}
    assert (root / "p.py").read_bytes() == _lines(FOREIGN)
    assert idx["p.py"] == {"blob": blob_sha(before), "head": blob_sha(_lines(FOREIGN)), "base": blob_sha(_lines())}
    assert (rec / "withheld-blobs" / blob_sha(before)).read_bytes() == before
    _heads(monkeypatch, {"p.py": _lines()})
    from coordinator_core.ops.dispatch_emit import live_write_guard

    monkeypatch.setattr(live_write_guard, "_head_lines", lambda root, path: _lines().splitlines(keepends=True))
    assert hte.restore(root, rec, owned_paths=["p.py"]) == {"restored": ["p.py"], "drift": {}, "refused": {}}
    assert (root / "p.py").read_bytes() == before


def test_nonrun_foreign_hunk_survives(tmp_path: Path) -> None:
    root, rec = _shared(tmp_path, {}, {**FOREIGN, **OWN}, prints_edits=OWN)
    hte.evict(root, rec, ["p.py"], {"p.py": _lines()}, run_id=RUN)
    assert (root / "p.py").read_bytes() == _lines(FOREIGN)


def test_wholly_own_file_resets_to_head(tmp_path: Path) -> None:
    root, rec = _shared(tmp_path, {}, OWN)
    hte.evict(root, rec, ["p.py"], {"p.py": _lines()}, run_id=RUN)
    assert (root / "p.py").read_bytes() == _lines()


def _refused_untouched(root, rec, run_id=RUN) -> None:
    before = (root / "p.py").read_bytes()
    refused: dict = {}
    idx = hte.evict(root, rec, ["p.py"], {"p.py": _lines()}, run_id=run_id, refused=refused)
    assert idx == {}
    assert "p.py" in refused
    assert (root / "p.py").read_bytes() == before
    assert not (rec / hte.EVICTED_FILE).exists()
    assert not (rec / hte.BLOBS_DIR).exists()


def test_no_attributable_hunk_refuses_and_writes_nothing(tmp_path: Path) -> None:
    root, rec = _shared(tmp_path, {**FOREIGN, **OWN}, {**FOREIGN, **OWN})
    _refused_untouched(root, rec)


def test_unknown_run_refuses_and_writes_nothing(tmp_path: Path) -> None:
    root, rec = _shared(tmp_path, FOREIGN, {**FOREIGN, **OWN})
    _refused_untouched(root, rec, run_id=None)


def test_overlapping_own_and_foreign_hunk_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    root, rec = _shared(tmp_path, FOREIGN, {**FOREIGN, **OWN})
    own, other = rhc.Hunk("p.py", 2, 1, 2, 1), rhc.Hunk("p.py", 2, 2, 2, 1)
    monkeypatch.setattr(rhc, "own_hunks", lambda *a, **k: rhc.HunkSplit(own=[own], unattributed=[other]))
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.own_hunk_stage._hunks_of", lambda *a, **k: [own, other]
    )
    _refused_untouched(root, rec)


def test_coarse_diff_not_wholly_own_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    root, rec = _shared(tmp_path, FOREIGN, {**FOREIGN, **OWN})
    monkeypatch.setattr(rhc, "exceeds_match_bound", lambda *a, **k: True)
    _refused_untouched(root, rec)


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


def _spliced_world(tmp_path, withheld_edits):
    """Git repo whose HEAD holds `_lines()`, a live file carrying a foreign line-7 hunk, and a record
    whose withheld bytes are `_lines(withheld_edits)` with `head` = the live bytes, `base` = HEAD."""
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


GEN_LINES = 300  # past run_hunk_claims.MAX_MATCHED_MIDDLE: one generator pass is a single coarse hunk
PRIOR = "ask-20261112T000000-bbbbbb"


def _doc(edits=None) -> bytes:
    edits = edits or {}
    return b"".join(edits.get(i, b"doc%d\n" % i) for i in range(1, GEN_LINES + 1))


GENERATED = {1: b"generated head\n", GEN_LINES: b"generated tail\n"}


def _generated_world(tmp_path, baseline: bytes, tree: bytes):
    """A file a script regenerated end to end (no Edit call): `RUN` staged against `baseline`, HEAD is `_doc()`."""
    root = tmp_path / "root"
    root.mkdir()
    _manifest(root, {"install.md": baseline})
    (root / "install.md").write_bytes(tree)
    return root, tmp_path / "rec"


def _evict_generated(root, rec) -> dict:
    refused: dict = {}
    hte.evict(root, rec, ["install.md"], {"install.md": _doc()}, run_id=RUN, refused=refused)
    return refused


def test_generator_rewrite_of_a_path_clean_at_stage_is_evicted(tmp_path: Path) -> None:
    root, rec = _generated_world(tmp_path, _doc(), _doc(GENERATED))
    assert _evict_generated(root, rec) == {}
    assert (root / "install.md").read_bytes() == _doc()
    assert (rec / "withheld-blobs" / blob_sha(_doc(GENERATED))).read_bytes() == _doc(GENERATED)


def test_generator_rewrite_with_crlf_output_is_evicted(tmp_path: Path) -> None:
    crlf = _doc(GENERATED).replace(b"\n", b"\r\n")
    root, rec = _generated_world(tmp_path, _doc(), crlf)
    assert _evict_generated(root, rec) == {}
    assert (root / "install.md").read_bytes() == _doc()


def test_coarse_dirt_that_predates_the_run_stays_kept(tmp_path: Path) -> None:
    peer_dirt = _doc({150: b"peer\n"})
    root, rec = _generated_world(tmp_path, peer_dirt, _doc({**GENERATED, 150: b"peer\n"}))
    before = (root / "install.md").read_bytes()
    refused = _evict_generated(root, rec)
    assert "no hunk attributable to this run" in refused["install.md"]
    assert (root / "install.md").read_bytes() == before
    assert not (rec / hte.EVICTED_FILE).exists()


def test_dirt_the_run_did_not_write_stays_kept_byte_identical(tmp_path: Path) -> None:
    root, rec = _generated_world(tmp_path, _doc(GENERATED), _doc(GENERATED))  # staged dirty, never rewritten
    before = (root / "install.md").read_bytes()
    assert "no hunk attributable to this run" in _evict_generated(root, rec)["install.md"]
    assert (root / "install.md").read_bytes() == before
    assert not (rec / hte.EVICTED_FILE).exists()


def test_generated_path_a_peer_rewrote_after_the_run_print_is_not_evicted_wholesale(tmp_path: Path) -> None:
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    root, rec = _generated_world(tmp_path, _doc(), _doc({**GENERATED, 150: b"peer\n"}))
    sha = git_objects.write_object(resolve_git_common_dir(root), b"blob", _doc(GENERATED))
    (root / "state" / "warp" / RUN / "write_prints.json").write_text(json.dumps({"install.md": sha}))
    before = (root / "install.md").read_bytes()
    assert "install.md" in _evict_generated(root, rec)
    assert (root / "install.md").read_bytes() == before
    assert not (rec / hte.EVICTED_FILE).exists()


def test_generated_bytes_already_in_the_refire_baseline_are_evicted_through_the_resumed_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.ops.dispatch_emit import landing_integrity

    root, rec = _generated_world(tmp_path, _doc(GENERATED), _doc(GENERATED))  # the refire staged over the prior pass
    _manifest(root, {"install.md": _doc()}, run=PRIOR)
    monkeypatch.setattr(landing_integrity, "_resumed_chain", lambda r, rid: [PRIOR] if rid == RUN else [])
    assert _evict_generated(root, rec) == {}
    assert (root / "install.md").read_bytes() == _doc()


def test_generated_bytes_in_the_baseline_of_an_unrelated_run_stay_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.ops.dispatch_emit import landing_integrity

    root, rec = _generated_world(tmp_path, _doc(GENERATED), _doc(GENERATED))
    _manifest(root, {"install.md": _doc()}, run=PRIOR)
    monkeypatch.setattr(landing_integrity, "_resumed_chain", lambda r, rid: [])
    assert "install.md" in _evict_generated(root, rec)
    assert (root / "install.md").read_bytes() == _doc(GENERATED)
