"""Tests for bug_blitz.mint: one row per file, severity, idempotence, reuse of an open tf row."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.bug_blitz.contract import FailingTest, run_tag
from coordinator_core.bug_blitz.mint import mint_tf_rows


@pytest.fixture(autouse=True)
def _no_output_override(monkeypatch):
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)


def _rows(root: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((root / "state" / "bug-backlog").glob("*.yaml")):
        out[p.stem] = yaml.safe_load(p.read_text(encoding="utf-8"))
    return out


def _tests():
    return (
        FailingTest(file="a/test_x.py", name="test_one", message="AssertionError: boom\nmore"),
        FailingTest(file="a/test_x.py", name="test_two", message="ValueError"),
        FailingTest(file="b/test_y.py", name="test_three", message="Segmentation fault: SIGSEGV"),
    )


def test_one_row_per_file_with_fields(tmp_path):
    ids = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="redset.txt")
    rows = _rows(tmp_path)
    assert len(ids) == 2 and set(ids) == set(rows)
    by_surface = {r["surface"]: r for r in rows.values()}
    a, b = by_surface["a/test_x.py"], by_surface["b/test_y.py"]
    assert a["title"] == "TF: a/test_x.py (2 failing)"
    assert a["severity"] == "P1" and b["severity"] == "P0"
    assert a["tags"] == ["tf", run_tag("r1")]
    assert a["evidence"] == "redset.txt"
    assert a["status"] == "open"
    assert "test_one :: AssertionError: boom" in a["body"]
    assert "more" not in a["body"]


def test_rerun_same_run_is_idempotent(tmp_path):
    first = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    before = _rows(tmp_path)
    second = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    assert second == first
    assert _rows(tmp_path) == before


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (root / "state" / "bug-backlog").glob("*.yaml")}


def test_new_run_reuses_open_row_in_place(tmp_path):
    first = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    before = _snapshot(tmp_path)
    second = mint_tf_rows(tmp_path, "r2", _tests(), target_ref="t")
    after = _snapshot(tmp_path)
    assert second == first
    assert set(after) == set(before)
    added = f"  - {run_tag('r2')}\n".encode()
    for name, data in before.items():
        assert b"system:" in after[name]
        assert after[name].replace(added, b"", 1) == data
        assert after[name].count(added) == 1


def test_same_run_recall_writes_and_declares_nothing(tmp_path, monkeypatch):
    mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    before = _snapshot(tmp_path)
    calls = []
    monkeypatch.setattr("coordinator_core.bug_blitz.mint.declare_write", calls.append)
    mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    assert _snapshot(tmp_path) == before and calls == []


def test_reuse_declares_the_edited_row(tmp_path, monkeypatch):
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    calls = []
    monkeypatch.setattr("coordinator_core.bug_blitz.mint.declare_write", calls.append)
    mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert calls == [f"state/bug-backlog/{ids[0]}.yaml"]


def test_flow_style_tags_raise_and_leave_file_unchanged(tmp_path):
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    path = tmp_path / "state" / "bug-backlog" / f"{ids[0]}.yaml"
    text = path.read_text(encoding="utf-8").replace(
        f"tags:\n  - tf\n  - {run_tag('r1')}\n", f"tags: [tf, {run_tag('r1')}]\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    before = path.read_bytes()
    with pytest.raises(ValueError, match=ids[0]):
        mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert path.read_bytes() == before


def _flow_style(root: Path, row_id: str, run: str) -> None:
    path = root / "state" / "bug-backlog" / f"{row_id}.yaml"
    text = path.read_text(encoding="utf-8").replace(
        f"tags:\n  - tf\n  - {run_tag(run)}\n", f"tags: [tf, {run_tag(run)}]\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _third():
    return (FailingTest(file="c/test_z.py", name="test_four", message="boom"),)


def test_bad_second_row_aborts_before_any_write(tmp_path):
    ids = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    _flow_style(tmp_path, ids[1], "r1")
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError, match=ids[1]):
        mint_tf_rows(tmp_path, "r2", _tests() + _third(), target_ref="t")
    assert _snapshot(tmp_path) == before


def test_two_flow_style_rows_one_error_names_both(tmp_path):
    ids = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    for row_id in ids:
        _flow_style(tmp_path, row_id, "r1")
    with pytest.raises(ValueError) as info:
        mint_tf_rows(tmp_path, "r2", _tests(), target_ref="t")
    assert ids[0] in str(info.value) and ids[1] in str(info.value)


def test_write_failure_leaves_no_tmp_and_row_unchanged(tmp_path, monkeypatch):
    mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    before = _snapshot(tmp_path)

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr("coordinator_core.bug_blitz.mint.os.replace", boom)
    with pytest.raises(OSError):
        mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert list((tmp_path / "state" / "bug-backlog").glob("*.tmp")) == []
    assert _snapshot(tmp_path) == before


def test_reused_row_still_loads_with_both_run_tags(tmp_path):
    from coordinator_core.ops.queue_family import load_family_records

    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    recs = load_family_records("bug-backlog", tmp_path, where="status=open")
    assert [r["path"].rsplit("/", 1)[-1] for r in recs] == [f"{ids[0]}.yaml"]
    tags = recs[0]["frontmatter"]["tags"]
    assert run_tag("r1") in tags and run_tag("r2") in tags


def test_closed_row_is_not_reused(tmp_path):
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    path = tmp_path / "state" / "bug-backlog" / f"{ids[0]}.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: open", "status: closed"), encoding="utf-8")
    again = mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert again != ids
    assert len(_rows(tmp_path)) == 2


def test_empty_tests_mints_nothing(tmp_path):
    assert mint_tf_rows(tmp_path, "r1", (), target_ref="t") == []
