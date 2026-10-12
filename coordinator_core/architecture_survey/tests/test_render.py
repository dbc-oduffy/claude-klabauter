"""Pins render_structure: determinism, marker handling, uncovered needs, index line."""
from __future__ import annotations

import copy
import json

from coordinator_core.architecture_survey import render
from coordinator_core.architecture_survey.contract import validate_structure

TODAY = "2026-10-12"


def _need(payload=None, covered=True, reason=None):
    return {"covered": covered, "door": "d" if covered else None, "calls": 1 if covered else 0,
            "reason": reason, "payload": payload if covered else None}


def _structure(behind=0, file_index=True, edges=True):
    return {
        "run_id": "20261012T093000Z-ab12",
        "index": {"commit": "abc1234", "commits_behind": behind,
                  "state": "current" if behind == 0 else "behind", "dropped_paths": 0, "unindexed": []},
        "needs": {
            "system-map": _need({"systems": [{"name": "b"}, {"name": "a"}]}),
            "edges": _need({"edges": [{"from_system": "b", "to_system": "a", "weight": 7},
                                      {"from_system": "a", "to_system": "b", "weight": 2}],
                            "limits": {"excludes": "dynamic imports", "floor": "weight>=1"}},
                           covered=edges, reason="gap"),
            "file-index": _need([{"path": "z/two.py", "system": "b"}, {"path": "a/one.py", "system": "a"}],
                                covered=file_index, reason="gap"),
            "dead-paths": _need([]),
            "last-touch": _need(covered=False, reason="none"),
        },
        "systems": [{"name": "b", "file_count": 3, "line_count": 30, "fingerprint": "fb"},
                    {"name": "a", "file_count": 1, "line_count": 10, "fingerprint": "fa"}],
    }


def _snapshot(root):
    atlas = root / "docs" / "architecture"
    return {p.name: p.read_bytes() for p in sorted(atlas.iterdir())}


def test_fixture_is_schema_valid():
    assert validate_structure(_structure()) == []


def test_two_renders_are_byte_identical(tmp_path):
    render.render_structure(tmp_path, _structure(), today=TODAY)
    first = _snapshot(tmp_path)
    render.render_structure(tmp_path, _structure(), today=TODAY)
    assert _snapshot(tmp_path) == first
    assert b"\r" not in b"".join(first.values())


def test_text_outside_markers_survives_and_block_replaced(tmp_path):
    atlas = tmp_path / "docs" / "architecture"
    atlas.mkdir(parents=True)
    old = f"head\n\n{render.BEGIN_MARKER}\nSTALE\n{render.END_MARKER}\n\ntail\n"
    (atlas / "file-index.md").write_text(old, encoding="utf-8", newline="\n")
    d = render.render_structure(tmp_path, _structure(), today=TODAY)
    text = (atlas / "file-index.md").read_text(encoding="utf-8")
    assert text.startswith("head\n\n") and text.endswith("\n\ntail\n")
    assert "STALE" not in text and "a/one.py -> a" in text
    assert "markers-added" not in d.evidence


def test_no_markers_gains_one_block_once(tmp_path):
    atlas = tmp_path / "docs" / "architecture"
    atlas.mkdir(parents=True)
    (atlas / "file-index.md").write_text("# File Index\n\nprose\n", encoding="utf-8", newline="\n")
    d = render.render_structure(tmp_path, _structure(), today=TODAY)
    assert "markers-added" in d.evidence
    d2 = render.render_structure(tmp_path, _structure(), today=TODAY)
    text = (atlas / "file-index.md").read_text(encoding="utf-8")
    assert text.count(render.BEGIN_MARKER) == 1 and text.startswith("# File Index\n\nprose\n")
    assert "markers-added" not in d2.evidence


def test_uncovered_file_index_writes_counts_and_no_rows(tmp_path):
    d = render.render_structure(tmp_path, _structure(file_index=False), today=TODAY)
    text = (tmp_path / "docs" / "architecture" / "file-index.md").read_text(encoding="utf-8")
    assert "| a | 1 | 10 |" in text
    assert " -> " not in text and "```" not in text
    assert "file-index uncovered" in d.evidence


def test_uncovered_edges_leaves_matrix_byte_identical(tmp_path):
    render.render_structure(tmp_path, _structure(), today=TODAY)
    before = (tmp_path / "docs" / "architecture" / "connectivity-matrix.md").read_bytes()
    d = render.render_structure(tmp_path, _structure(edges=False), today="2027-01-01")
    assert (tmp_path / "docs" / "architecture" / "connectivity-matrix.md").read_bytes() == before
    assert "edges uncovered" in d.evidence


def test_uncovered_edges_creates_no_matrix(tmp_path):
    render.render_structure(tmp_path, _structure(edges=False), today=TODAY)
    assert not (tmp_path / "docs" / "architecture" / "connectivity-matrix.md").exists()


def test_behind_line_carries_commit_and_count(tmp_path):
    render.render_structure(tmp_path, _structure(behind=12), today=TODAY)
    atlas = tmp_path / "docs" / "architecture"
    for name in ("connectivity-matrix.md", "file-index.md"):
        assert "Structure as of index commit abc1234, 12 commits behind HEAD" in (atlas / name).read_text(encoding="utf-8")
    render.render_structure(tmp_path, _structure(behind=0), today=TODAY)
    assert "Structure as of index commit abc1234, current" in (atlas / "file-index.md").read_text(encoding="utf-8")


def test_matrix_name_order_floor_and_fingerprints(tmp_path):
    s = _structure()
    s["systems"] = list(reversed(s["systems"]))
    render.render_structure(tmp_path, s, today=TODAY)
    atlas = tmp_path / "docs" / "architecture"
    matrix = (atlas / "connectivity-matrix.md").read_text(encoding="utf-8")
    assert matrix.startswith(f"---\nlast_mapped: {TODAY}\n---\n")
    assert "| a | . | 2 |" in matrix and "| b | 7 | . |" in matrix
    assert "`floor`: weight>=1" in matrix
    fp = json.loads((atlas / "structure-fingerprint.json").read_text(encoding="utf-8"))
    assert fp == {"index_commit": "abc1234", "systems": {"a": "fa", "b": "fb"}}


def test_structure_not_mutated(tmp_path):
    s = _structure()
    before = copy.deepcopy(s)
    render.render_structure(tmp_path, s, today=TODAY)
    assert s == before
