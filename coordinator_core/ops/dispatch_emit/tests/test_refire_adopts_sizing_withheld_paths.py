"""A refire credits every path its sizing's earlier halts withheld, chained across runs, keyed to the newest print."""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_withheld_index
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE, predecessor_withheld

SIZING = "state/sizings/a.yaml"


def _halt(root: Path, run_id: str, sizing: str, path: str, content: str, mtime: int) -> None:
    f = root / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    d = root / RECORD_DIR_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / WITHHELD_PRINTS_FILE).write_text(json.dumps({path: fingerprint(root, path)}), encoding="utf-8")
    marker = d / "halted.json"
    marker.write_text(
        json.dumps({"status": "halted", "sizing_rel": sizing, "run_artifacts": {"withheld": {path: "w"}}}),
        encoding="utf-8",
    )
    os.utime(marker, ns=(mtime, mtime))
    halted_withheld_index.add_run(
        root, run_id, [path], sizing_rel=sizing, prints={path: fingerprint(root, path)}, at_ns=mtime
    )


def _built(root: Path) -> None:
    (root / halted_withheld_index.INDEX_DIR / halted_withheld_index.SENTINEL).write_bytes(b"")


def test_earlier_same_sizing_halt_credits_when_a_later_foreign_sizing_holds_the_path_last(tmp_path):
    _halt(tmp_path, "r1", SIZING, "p.py", "mine", 1_000)
    (tmp_path / "scratch").mkdir()
    other = tmp_path / RECORD_DIR_ROOT / "r9"
    other.mkdir(parents=True)
    (other / "halted.json").write_text(
        json.dumps({"sizing_rel": "state/sizings/z.yaml", "run_artifacts": {"withheld": {"p.py": "w"}}}), encoding="utf-8"
    )
    halted_withheld_index.add_run(tmp_path, "r9", ["p.py"])
    _built(tmp_path)
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"p.py"})


def test_chain_credits_the_path_a_later_run_changed_and_rewithheld(tmp_path):
    _halt(tmp_path, "r1", SIZING, "p.py", "v1", 1_000)
    _halt(tmp_path, "r2", SIZING, "p.py", "v2", 2_000)
    _built(tmp_path)
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"p.py"})


def test_current_blob_matching_only_the_older_print_is_dirt(tmp_path):
    _halt(tmp_path, "r1", SIZING, "p.py", "v1", 1_000)
    _halt(tmp_path, "r2", SIZING, "p.py", "v2", 2_000)
    _built(tmp_path)
    (tmp_path / "p.py").write_text("v1", encoding="utf-8")
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_newest_wins_by_mtime_not_run_id_order(tmp_path):
    _halt(tmp_path, "z-old", SIZING, "p.py", "v1", 1_000)
    _halt(tmp_path, "a-new", SIZING, "p.py", "v2", 2_000)
    _built(tmp_path)
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset({"p.py"})
    (tmp_path / "p.py").write_text("v1", encoding="utf-8")
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / "x") == frozenset()


def test_own_run_is_skipped_even_when_it_holds_the_path_last(tmp_path):
    _halt(tmp_path, "r1", SIZING, "p.py", "v1", 1_000)
    _halt(tmp_path, "r2", SIZING, "p.py", "v2", 2_000)
    _built(tmp_path)
    (tmp_path / "p.py").write_text("v1", encoding="utf-8")
    assert predecessor_withheld(tmp_path, SIZING, tmp_path / RECORD_DIR_ROOT / "r2") == frozenset({"p.py"})
