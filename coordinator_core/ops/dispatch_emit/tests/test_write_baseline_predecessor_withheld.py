"""predecessor_withheld: sizing binding, path hygiene, index-only read, content-tied credit; pre_existing_dirt exemption."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_withheld_index
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
from coordinator_core.ops.dispatch_emit.write_baseline import pre_existing_dirt, predecessor_withheld

SIZING = "state/sizings/a.yaml"


def _index(root: Path, run_id: str, paths, *, sizing=SIZING, prints=None, at_ns=None) -> None:
    """Write the run's files, then its index entry; prints default to the files' current blob ids."""
    for k in paths:
        if k.startswith(("/", "C:")) or ".." in k or k == ".":
            continue
        f = root / k
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(k, encoding="utf-8")
    if prints is None:
        prints = {k: fingerprint(root, k) for k in paths if (root / k).is_file()}
    halted_withheld_index.add_run(root, run_id, paths, sizing_rel=sizing, prints=prints, at_ns=at_ns)
    (root / halted_withheld_index.INDEX_DIR / halted_withheld_index.SENTINEL).write_bytes(b"")


def test_matching_sizing_returns_keys(tmp_path):
    _index(tmp_path, "r1", ["a.py", "b/c.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "run-x") == frozenset({"a.py", "b/c.py"})


def test_backslash_sizing_normalized(tmp_path):
    _index(tmp_path, "r1", ["a.py"], sizing="state\\sizings\\a.yaml")
    assert predecessor_withheld(tmp_path, "state\\sizings\\a.yaml", tmp_path / "x") == frozenset({"a.py"})
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"a.py"})


def test_different_or_unbound_sizing_contributes_nothing(tmp_path):
    _index(tmp_path, "r1", ["a.py"], sizing="state/sizings/other.yaml")
    _index(tmp_path, "r2", ["b.py"], sizing="")
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_empty_sizing_returns_empty(tmp_path):
    assert predecessor_withheld(tmp_path, None, tmp_path / "x") == frozenset()


def test_absolute_and_parent_paths_rejected(tmp_path):
    bad = {"/etc/passwd": "a" * 40, "../up.py": "a" * 40, "C:/x.py": "a" * 40}  # abs-path-ok: rejected-input fixture, never opened
    (tmp_path / "ok.py").write_text("ok.py", encoding="utf-8")
    _index(tmp_path, "r1", ["ok.py", *bad], prints={**bad, "ok.py": fingerprint(tmp_path, "ok.py")})
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"ok.py"})


def test_run_dir_skipped(tmp_path):
    d = tmp_path / "self"
    _index(tmp_path, "self", ["mine.py"])
    _index(tmp_path, "peer", ["peer.py"])
    assert predecessor_withheld(tmp_path, SIZING, d) == frozenset({"peer.py"})


def test_self_run_never_credits_itself_when_only_indexed_run(tmp_path):
    _index(tmp_path, "self", ["mine.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "self") == frozenset()


def test_run_absent_from_index_not_scanned(tmp_path):
    _index(tmp_path, "other", ["zzz.py"], sizing="state/sizings/other.yaml")
    d = tmp_path / "state" / "warp" / "r1"
    d.mkdir(parents=True)
    (d / "halted.json").write_text('{"status": "halted", "sizing_rel": "%s", "run_artifacts": {"withheld": {"a.py": "w"}}}' % SIZING, encoding="utf-8")
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_opens_no_halted_json(tmp_path, monkeypatch):
    _index(tmp_path, "r1", ["a.py"])
    opened: list[str] = []
    real_read_text, real_read_bytes, real_open = Path.read_text, Path.read_bytes, Path.open

    def spy(real):
        def wrapped(self, *a, **k):
            opened.append(self.name)
            return real(self, *a, **k)

        return wrapped

    monkeypatch.setattr(Path, "read_text", spy(real_read_text))
    monkeypatch.setattr(Path, "read_bytes", spy(real_read_bytes))
    monkeypatch.setattr(Path, "open", spy(real_open))
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"a.py"})
    assert "halted.json" not in opened and "withheld-prints.json" not in opened


def test_newest_print_wins_per_path(tmp_path):
    _index(tmp_path, "old", ["a.py"], at_ns=1)
    current = fingerprint(tmp_path, "a.py")
    _index(tmp_path, "new", ["a.py"], prints={"a.py": "b" * 40}, at_ns=2)
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()
    _index(tmp_path, "new", ["a.py"], prints={"a.py": current}, at_ns=3)
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"a.py"})


def test_pre_existing_dirt_exempts_head_equal_path_only(tmp_path, monkeypatch):
    (tmp_path / "same.py").write_text("one\n", encoding="utf-8")
    (tmp_path / "diff.py").write_text("two\n", encoding="utf-8")
    base = {"same.py": fingerprint(tmp_path, "same.py"), "diff.py": fingerprint(tmp_path, "diff.py")}
    head = {"same.py": (0o100644, fingerprint(tmp_path, "same.py")), "diff.py": (0o100644, "0" * 40)}
    import coordinator_core.git.git_state as git_state

    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {p: head[p] for p in paths if p in head})
    assert pre_existing_dirt(tmp_path, base, ["same.py", "diff.py"]) == ["diff.py"]


def test_head_blob_differing_from_tree_is_still_dirt(tmp_path, monkeypatch):
    (tmp_path / "p.py").write_text("tree\n", encoding="utf-8")
    base = {"p.py": fingerprint(tmp_path, "p.py")}
    import coordinator_core.git.git_state as git_state

    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {p: (0o100644, "1" * 40) for p in paths})
    assert pre_existing_dirt(tmp_path, base, ["p.py"]) == ["p.py"]


def test_edit_after_halt_is_not_credited(tmp_path):
    _index(tmp_path, "r1", ["a.py", "b.py"])
    (tmp_path / "a.py").write_text("foreign edit", encoding="utf-8")
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"b.py"})


def test_missing_or_empty_print_fails_closed(tmp_path):
    _index(tmp_path, "r1", ["a.py"], prints={})
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()
    _index(tmp_path, "r1", ["a.py"], prints={"a.py": ""})
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_absent_file_not_credited(tmp_path):
    _index(tmp_path, "r1", ["a.py"])
    (tmp_path / "a.py").unlink()
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()
