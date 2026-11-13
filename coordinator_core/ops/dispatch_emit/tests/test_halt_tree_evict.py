"""halt_tree_evict: bytes leave the tree into the record, return only while the tree holds HEAD bytes."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halt_tree_evict as hte
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

BAD = b"from x import undefined_name\r\n"
HEAD = b"ok = 1\r\n"


RUN = "ask-20261113T000000-aaaaaa"


def _manifest(root, baseline):
    """Stage `RUN` against `baseline` ({path: bytes | None}); returns nothing, objects land in the store."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    shas = {
        p: None if b is None else git_objects.write_object(resolve_git_common_dir(root), b"blob", b)
        for p, b in baseline.items()
    }
    rdir = root / "state" / "warp" / RUN
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


def test_evict_stores_bytes_resets_tree_and_leaves_peer(tmp_path):
    root, rec = _setup(tmp_path)
    idx = hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None}, run_id=RUN)
    assert (root / "tracked.py").read_bytes() == HEAD
    assert not (root / "new.py").exists()
    assert (root / "peer.py").read_bytes() == b"peer broken\n"
    assert idx["tracked.py"] == {"blob": blob_sha(BAD), "head": blob_sha(HEAD)}
    assert idx["new.py"]["head"] is None
    assert (rec / "withheld-blobs" / blob_sha(BAD)).read_bytes() == BAD
    assert json.loads((rec / "evicted.json").read_text()) == idx


def test_restore_round_trip_byte_identical_and_idempotent(tmp_path):
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None}, run_id=RUN)
    first = hte.restore(root, rec)
    assert first == {"restored": ["new.py", "tracked.py"], "drift": {}}
    assert (root / "tracked.py").read_bytes() == BAD
    assert (root / "new.py").read_bytes() == BAD
    assert hte.restore(root, rec) == {"restored": [], "drift": {}}


def test_restore_refuses_on_drift_and_leaves_tree(tmp_path):
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py", "new.py"], {"tracked.py": HEAD, "new.py": None}, run_id=RUN)
    (root / "tracked.py").write_bytes(b"someone else\n")
    (root / "new.py").write_bytes(b"peer made this\n")
    out = hte.restore(root, rec)
    assert out == {
        "restored": [],
        "drift": {"tracked.py": "restore-drift", "new.py": "restore-drift"},
    }
    assert (root / "tracked.py").read_bytes() == b"someone else\n"
    assert (root / "new.py").read_bytes() == b"peer made this\n"


def test_restore_without_index_is_empty(tmp_path):
    assert hte.restore(tmp_path, tmp_path / "none") == {"restored": [], "drift": {}}


def test_evict_skips_absent_path_and_unlisted_paths(tmp_path):
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


def test_broken_paths_flags_undefined_import_and_cascading_importer(tmp_path):
    from coordinator_core.ops.dispatch_emit import halt_tree_check

    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "def old():\n    pass\n",
                                "pkg/b.py": "x = 1\n"})
    (repo / "pkg/a.py").write_text("from pkg.b import undefined_name\ndef old():\n    pass\n")
    (repo / "pkg/b.py").write_text("from pkg.a import old\nx = 1\n")
    out = halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py", "pkg/b.py"])
    assert "pkg/a.py" in out
    assert "pkg/b.py" not in out


def test_broken_paths_clean_and_syntax(tmp_path):
    from coordinator_core.ops.dispatch_emit import halt_tree_check

    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "y = 1\n"})
    (repo / "pkg/a.py").write_text("y = 2\n")
    assert halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py"]) == {}
    (repo / "pkg/a.py").write_text("def (:\n")
    assert "syntax error" in halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py"])["pkg/a.py"]


def test_second_evict_after_reset_keeps_original_entry(tmp_path):
    root, rec = _setup(tmp_path)
    hte.evict(root, rec, ["tracked.py"], {"tracked.py": HEAD}, run_id=RUN)
    again = hte.evict(root, rec, ["tracked.py"], {"tracked.py": HEAD}, run_id=RUN)
    assert again["tracked.py"]["blob"] == blob_sha(BAD)
    assert (root / "tracked.py").read_bytes() == HEAD
    assert hte.restore(root, rec)["restored"] == ["tracked.py"]


def _lines(edits=None):
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


def test_unattributed_foreign_hunk_survives_byte_identical_and_own_hunk_reverts(tmp_path):
    root, rec = _shared(tmp_path, FOREIGN, {**FOREIGN, **OWN})
    before = (root / "p.py").read_bytes()
    refused: dict = {}
    idx = hte.evict(root, rec, ["p.py"], {"p.py": _lines()}, run_id=RUN, refused=refused)
    assert refused == {}
    assert (root / "p.py").read_bytes() == _lines(FOREIGN)
    assert idx["p.py"] == {"blob": blob_sha(before), "head": blob_sha(_lines(FOREIGN))}
    assert (rec / "withheld-blobs" / blob_sha(before)).read_bytes() == before
    assert hte.restore(root, rec) == {"restored": ["p.py"], "drift": {}}
    assert (root / "p.py").read_bytes() == before


def test_nonrun_foreign_hunk_survives(tmp_path):
    root, rec = _shared(tmp_path, {}, {**FOREIGN, **OWN}, prints_edits=OWN)
    hte.evict(root, rec, ["p.py"], {"p.py": _lines()}, run_id=RUN)
    assert (root / "p.py").read_bytes() == _lines(FOREIGN)


def test_wholly_own_file_resets_to_head(tmp_path):
    root, rec = _shared(tmp_path, {}, OWN)
    hte.evict(root, rec, ["p.py"], {"p.py": _lines()}, run_id=RUN)
    assert (root / "p.py").read_bytes() == _lines()


def _refused_untouched(root, rec, run_id=RUN):
    before = (root / "p.py").read_bytes()
    refused: dict = {}
    idx = hte.evict(root, rec, ["p.py"], {"p.py": _lines()}, run_id=run_id, refused=refused)
    assert idx == {}
    assert "p.py" in refused
    assert (root / "p.py").read_bytes() == before
    assert not (rec / hte.EVICTED_FILE).exists()
    assert not (rec / hte.BLOBS_DIR).exists()


def test_no_attributable_hunk_refuses_and_writes_nothing(tmp_path):
    root, rec = _shared(tmp_path, {**FOREIGN, **OWN}, {**FOREIGN, **OWN})
    _refused_untouched(root, rec)


def test_unknown_run_refuses_and_writes_nothing(tmp_path):
    root, rec = _shared(tmp_path, FOREIGN, {**FOREIGN, **OWN})
    _refused_untouched(root, rec, run_id=None)


def test_overlapping_own_and_foreign_hunk_refuses(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    root, rec = _shared(tmp_path, FOREIGN, {**FOREIGN, **OWN})
    own, other = rhc.Hunk("p.py", 2, 1, 2, 1), rhc.Hunk("p.py", 2, 2, 2, 1)
    monkeypatch.setattr(rhc, "own_hunks", lambda *a, **k: rhc.HunkSplit(own=[own], unattributed=[other]))
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.own_hunk_stage._hunks_of", lambda *a, **k: [own, other]
    )
    _refused_untouched(root, rec)


def test_coarse_diff_not_wholly_own_refuses(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    root, rec = _shared(tmp_path, FOREIGN, {**FOREIGN, **OWN})
    monkeypatch.setattr(rhc, "exceeds_match_bound", lambda *a, **k: True)
    _refused_untouched(root, rec)
