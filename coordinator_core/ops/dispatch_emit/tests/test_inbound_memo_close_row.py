"""A slugless row surfacing this repo's own inbox memo is closed by the memo-lifecycle leg, never a memo-send."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.spine_read import (
    EM_OWNED_REASONS,
    _is_memo_send_row,
    inbound_close_path,
    read_spine,
)

_MEMO = "state/cross-repo/inbox/x.md"
_ROW = (
    "- id: C2\n  title: close the inbound memo\n  change_kind: doc-edit\n"
    f"  surface: {_MEMO}\n  writes: [{_MEMO}]\n  disposition: open\n"
    "  body: |\n    Close it.\n"
)


def _plan(tmp_path, frontmatter="", row=_ROW):
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "p.md"
    path.write_text(
        f"---\ntitle: p\n{frontmatter}---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{row}```\n",
        encoding="utf-8",
    )
    return path


def _read(path):
    out: list = []
    rows = read_spine(path, exclusions=out)
    return rows, out


def test_inbound_row_is_not_a_memo_send():
    raw = {"surface": _MEMO, "writes": [_MEMO]}
    assert inbound_close_path(raw) == _MEMO
    assert _is_memo_send_row(raw) is False


def test_backslash_spelling_is_normalised():
    assert inbound_close_path({"surface": _MEMO.replace("/", "\\")}) == _MEMO


def test_other_inbox_shapes_stay_memo_sends():
    for surface in ("coordinator-content-repo/state/cross-repo/inbox/x.md", "/abs/state/cross-repo/inbox/x.md", "state/cross-repo/inbox/"):
        raw = {"surface": surface}
        assert inbound_close_path(raw) is None
        assert _is_memo_send_row(raw) is True


def test_row_with_slug_stays_a_memo_send():
    raw = {"surface": _MEMO, "receipt": ".coordinator-local/memo-outbox/sent/t.md"}
    assert inbound_close_path(raw) is None
    assert _is_memo_send_row(raw) is True


def test_two_distinct_inbox_memos_are_not_an_inbound_close():
    raw = {"surface": _MEMO, "writes": ["state/cross-repo/inbox/y.md"]}
    assert inbound_close_path(raw) is None


def test_row_also_writing_code_is_not_an_inbound_close(tmp_path):
    raw = {"surface": "coordinator_core/group_em/gem_home.py", "writes": ["coordinator_core/group_em/gem_home.py", _MEMO]}
    assert inbound_close_path(raw) is None
    row = (
        "- id: C1\n  title: fix and close\n  change_kind: code-edit\n"
        f"  surface: coordinator_core/group_em/gem_home.py\n  writes: [coordinator_core/group_em/gem_home.py, {_MEMO}]\n"
        "  disposition: open\n  body: |\n    Fix it.\n"
    )
    rows, out = _read(_plan(tmp_path, f"source_memo: {_MEMO}\n", row))
    assert [e["reason"] for e in out if e["reason"] == "memo-lifecycle"] == []


def test_plan_naming_the_memo_excludes_row_as_memo_lifecycle(tmp_path):
    rows, out = _read(_plan(tmp_path, f"source_memo: {_MEMO}\n"))
    assert rows == []
    assert [(e["id"], e["reason"]) for e in out] == [("C2", "memo-lifecycle")]
    assert "memo-lifecycle leg" in out[0]["detail"]
    assert "memo-lifecycle" not in EM_OWNED_REASONS


def test_sizing_naming_the_memo_excludes_row_as_memo_lifecycle(tmp_path):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "s.yaml").write_text(
        "intent: close state/cross-repo/inbox/x.md on landing\n", encoding="utf-8"
    )
    rows, out = _read(_plan(tmp_path, "sizing_object: state/sizings/s.yaml\n"))
    assert rows == []
    assert [e["reason"] for e in out] == ["memo-lifecycle"]


def test_plan_naming_no_memo_gives_em_performed_with_source_memo_remedy(tmp_path):
    rows, out = _read(_plan(tmp_path))
    assert rows == []
    assert [e["reason"] for e in out] == ["em-performed"]
    assert "source_memo" in out[0]["detail"] and _MEMO in out[0]["detail"]


def test_plan_naming_a_different_memo_gives_em_performed(tmp_path):
    _, out = _read(_plan(tmp_path, "source_memo: state/cross-repo/inbox/other.md\n"))
    assert [e["reason"] for e in out] == ["em-performed"]


def test_never_owed_as_memo_send_without_a_slug(tmp_path):
    for fm in ("", f"source_memo: {_MEMO}\n"):
        _, out = _read(_plan(tmp_path, fm))
        assert all(e["reason"] != "memo-send" for e in out)
        assert all("names no memo slug" not in e["detail"] for e in out)


def test_memo_lifecycle_row_is_neither_owed_nor_dispatched(tmp_path):
    rows, out = _read(_plan(tmp_path, f"source_memo: {_MEMO}\n"))
    assert rows == []
    assert [e for e in out if e["reason"] in EM_OWNED_REASONS] == []
