"""predecessor_withheld: sizing binding, path hygiene, index-bounded scan, content-tied credit; pre_existing_dirt exemption."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_withheld_index
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE, pre_existing_dirt, predecessor_withheld

SIZING = "state/sizings/a.yaml"


def _halt(root: Path, run_id: str, sizing, withheld, *, raw: str | None = None, prints: bool = True) -> Path:
    d = root / RECORD_DIR_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    if prints and isinstance(withheld, dict):
        rec = {}
        for k in withheld:
            if k.startswith(("/", ".", "C:")) and k != "./ok.py":
                continue
            f = root / k
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(k, encoding="utf-8")
            rec[k] = fingerprint(root, k)
        (d / WITHHELD_PRINTS_FILE).write_text(json.dumps(rec), encoding="utf-8")
    body = raw if raw is not None else json.dumps(
        {"status": "halted", "sizing_rel": sizing, "run_artifacts": {"withheld": withheld}}
    )
    (d / "halted.json").write_text(body, encoding="utf-8")
    return d


def _index(root: Path, run_id: str, paths) -> None:
    halted_withheld_index.add_run(root, run_id, paths)
    (root / halted_withheld_index.INDEX_DIR / halted_withheld_index.SENTINEL).write_bytes(b"")


def test_matching_sizing_returns_keys(tmp_path):
    _halt(tmp_path, "r1", SIZING, {"a.py": "why", "b/c.py": "why"})
    _index(tmp_path, "r1", ["a.py", "b/c.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "run-x") == frozenset({"a.py", "b/c.py"})


def test_backslash_and_dot_prefix_sizing_normalized(tmp_path):
    _halt(tmp_path, "r1", "state\\sizings\\a.yaml", {"a.py": "w"})
    _index(tmp_path, "r1", ["a.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"a.py"})


def test_different_sizing_contributes_nothing(tmp_path):
    _halt(tmp_path, "r1", "state/sizings/other.yaml", {"a.py": "w"})
    _index(tmp_path, "r1", ["a.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_empty_sizing_returns_empty(tmp_path):
    assert predecessor_withheld(tmp_path, None, tmp_path / "x") == frozenset()


def test_malformed_halted_json_skipped(tmp_path):
    _halt(tmp_path, "bad", None, None, raw="{not json")
    _halt(tmp_path, "good", SIZING, {"g.py": "w"})
    _index(tmp_path, "bad", ["z.py"])
    _index(tmp_path, "good", ["g.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"g.py"})


def test_absolute_and_parent_paths_rejected(tmp_path):
    _halt(
        tmp_path,
        "r1",
        SIZING,
        # abs-path-ok: rejected-input fixture, never opened
        {"/etc/passwd": "w", "../up.py": "w", "C:/x.py": "w", ".": "w", "./ok.py": "w", "ok2.py": "w"},
    )
    _index(tmp_path, "r1", ["ok.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"ok.py", "ok2.py"})


def test_run_dir_skipped(tmp_path):
    d = _halt(tmp_path, "self", SIZING, {"mine.py": "w"})
    _halt(tmp_path, "peer", SIZING, {"peer.py": "w"})
    _index(tmp_path, "self", ["mine.py"])
    _index(tmp_path, "peer", ["peer.py"])
    assert predecessor_withheld(tmp_path, SIZING, d) == frozenset({"peer.py"})


def test_run_absent_from_index_not_scanned(tmp_path):
    _halt(tmp_path, "r1", SIZING, {"a.py": "w"})
    _index(tmp_path, "other", ["zzz.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


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
    _halt(tmp_path, "r1", SIZING, {"a.py": "w", "b.py": "w"})
    _index(tmp_path, "r1", ["a.py", "b.py"])
    (tmp_path / "a.py").write_text("foreign edit", encoding="utf-8")
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"b.py"})


def test_no_prints_record_fails_closed(tmp_path):
    _halt(tmp_path, "r1", SIZING, {"a.py": "w"}, prints=False)
    (tmp_path / "a.py").write_text("a.py", encoding="utf-8")
    _index(tmp_path, "r1", ["a.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_malformed_prints_record_fails_closed(tmp_path):
    d = _halt(tmp_path, "r1", SIZING, {"a.py": "w"})
    _index(tmp_path, "r1", ["a.py"])
    for raw in ("{nope", "[]", json.dumps({"a.py": 5}), json.dumps({"a.py": ""})):
        (d / WITHHELD_PRINTS_FILE).write_text(raw, encoding="utf-8")
        assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_absent_file_not_credited(tmp_path):
    _halt(tmp_path, "r1", SIZING, {"a.py": "w"})
    _index(tmp_path, "r1", ["a.py"])
    (tmp_path / "a.py").unlink()
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_non_dict_halted_and_bad_withheld_shapes_fail_closed(tmp_path):
    _halt(tmp_path, "a", None, None, raw="[1, 2]")
    _halt(tmp_path, "b", SIZING, ["a.py"], raw=json.dumps({"sizing_rel": SIZING, "run_artifacts": {"withheld": ["a.py"]}}))
    _halt(tmp_path, "c", SIZING, None, raw=json.dumps({"sizing_rel": SIZING, "run_artifacts": "x"}))
    _halt(tmp_path, "d", SIZING, None, raw=json.dumps({"sizing_rel": 7, "run_artifacts": {"withheld": {"a.py": "w"}}}))
    for r in "abcd":
        _index(tmp_path, r, ["a.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_bad_withheld_keys_never_credited_even_with_prints(tmp_path):
    d = _halt(tmp_path, "r1", SIZING, {"../up.py": "w", "ok.py": "w"})
    prints = json.loads((d / WITHHELD_PRINTS_FILE).read_text(encoding="utf-8"))
    prints["../up.py"] = "a" * 40
    (d / WITHHELD_PRINTS_FILE).write_text(json.dumps(prints), encoding="utf-8")
    _index(tmp_path, "r1", ["ok.py"])
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"ok.py"})


def test_self_run_never_credits_itself_when_only_indexed_run(tmp_path):
    d = _halt(tmp_path, "self", SIZING, {"mine.py": "w"})
    _index(tmp_path, "self", ["mine.py"])
    assert predecessor_withheld(tmp_path, SIZING, d) == frozenset()
