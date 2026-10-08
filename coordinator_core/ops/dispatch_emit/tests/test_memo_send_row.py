"""A memo-send row is scheduled in wave 1 and closes only on its sender-side receipt."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.emit import emit_script
from coordinator_core.ops.dispatch_emit.spine_read import MemoRowNoReceiptError, read_spine

from .conftest import REVIEW_KW

_OUTBOX = ".coordinator-local/memo-outbox"
_MEMO_ROW = (
    "- id: M1\n  title: send the memo\n  change_kind: doc-edit\n"
    f"  surface: {_OUTBOX}/topic.md\n  writes: []\n  disposition: open\n"
    "  body: |\n    Send it.\n"
)
_CODE_ROW = (
    "- id: C1\n  title: code\n  change_kind: script-edit\n"
    "  surface: pkg/a.py\n  writes:\n    - pkg/a.py\n  disposition: open\n"
    "  body: |\n    Do it.\n"
)
_EMPTY_ROW = (
    "- id: E1\n  title: probe\n  change_kind: verification\n"
    "  surface: pkg/a.py\n  writes: []\n  disposition: open\n  body: |\n    Look.\n"
)


def _plan(tmp_path, *rows) -> object:
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "p.md"
    path.write_text(
        "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n"
        + "".join(rows)
        + "```\n",
        encoding="utf-8",
    )
    return path


def _receipt(tmp_path, slug="topic"):
    sent = tmp_path / _OUTBOX / "sent"
    sent.mkdir(parents=True, exist_ok=True)
    (sent / f"{slug}.md").write_text("sent\n", encoding="utf-8")


def test_memo_row_is_no_executor_row_and_surfaces_as_an_em_action(tmp_path):
    plan = _plan(tmp_path, _MEMO_ROW, _CODE_ROW)
    exclusions: list = []
    assert [r.id for r in read_spine(plan, exclusions=exclusions)] == ["C1"]
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-send")]
    script = emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
    assert not re.search(r"_rows\['M1'\] = _runRow\(", script)
    assert re.search(r"_rows\['C1'\] = _runRow\(", script)
    assert "EM: send topic" in script
    assert "cross-repo-memo send topic" in script


def test_memo_row_with_a_receipt_is_skipped_as_delivered(tmp_path):
    plan = _plan(tmp_path, _MEMO_ROW, _CODE_ROW)
    _receipt(tmp_path)
    exclusions: list = []
    assert [r.id for r in read_spine(plan, exclusions=exclusions)] == ["C1"]
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-delivered")]
    assert "EM: send" not in emit_script(plan, repo_root=tmp_path, **REVIEW_KW)


def test_declared_receipt_names_the_slug(tmp_path):
    row = _MEMO_ROW.replace(
        f"  surface: {_OUTBOX}/topic.md\n", f"  surface: docs/x.md\n  receipt: {_OUTBOX}/sent/other.md\n"
    ).replace("change_kind: doc-edit", "change_kind: doc-edit\n  kind: memo-send")
    plan = _plan(tmp_path, row)
    assert terminal_commit._memo_rows_without_receipt(tmp_path, _request("p.md")) == {"M1": "other"}
    _receipt(tmp_path, "other")
    assert terminal_commit._memo_rows_without_receipt(tmp_path, _request("p.md")) == {}


def test_memo_row_naming_no_slug_is_refused(tmp_path):
    row = _MEMO_ROW.replace(f"{_OUTBOX}/topic.md", "docs/x.md").replace(
        "change_kind: doc-edit", "change_kind: doc-edit\n  kind: memo-send"
    )
    with pytest.raises(MemoRowNoReceiptError):
        read_spine(_plan(tmp_path, row))


def test_non_memo_empty_writes_row_is_unchanged(tmp_path):
    plan = _plan(tmp_path, _EMPTY_ROW, _CODE_ROW)
    rows = read_spine(plan)
    assert [r.id for r in rows] == ["E1", "C1"]
    script = emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
    assert "memo-send row" not in script


def _request(plan_rel):
    return SimpleNamespace(plan_path=plan_rel)


def test_row_without_a_receipt_stays_open_and_names_the_slug(tmp_path):
    _plan(tmp_path, _MEMO_ROW, _CODE_ROW)
    assert terminal_commit._memo_rows_without_receipt(tmp_path, _request("p.md")) == {"M1": "topic"}


def test_row_with_a_receipt_closes(tmp_path):
    _plan(tmp_path, _MEMO_ROW, _CODE_ROW)
    _receipt(tmp_path)
    assert terminal_commit._memo_rows_without_receipt(tmp_path, _request("p.md")) == {}


def test_non_memo_rows_never_held_by_the_receipt_gate(tmp_path):
    _plan(tmp_path, _EMPTY_ROW, _CODE_ROW)
    assert terminal_commit._memo_rows_without_receipt(tmp_path, _request("p.md")) == {}


def test_terminal_commit_reply_names_the_unsent_memo(tmp_path):
    import subprocess

    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _plan(tmp_path, _MEMO_ROW, _CODE_ROW)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("pkg/a.py",)),), plan_path="p.md"
    )
    (tmp_path / "run.mjs").write_text("// e\n" + render_marker(request) + "\n", encoding="utf-8")
    params = {
        "script_path": "run.mjs",
        "incomplete_chunks": [],
        "inline_review": {"integration_stem": "s", "slices": 1, "fixes": 0},
    }
    out = terminal_commit._handler(params, repo_root=tmp_path / ".git")
    assert out["memo_unsent"] == {"M1": "topic"}
    _receipt(tmp_path)
    (tmp_path / "pkg" / "a.py").write_text("a2\n", encoding="utf-8")
    out = terminal_commit._handler(params, repo_root=tmp_path / ".git")
    assert "memo_unsent" not in out
