"""A memo-send row is scheduled in wave 1 and closes only on its sender-side receipt."""

from __future__ import annotations

import copy
import re
from types import SimpleNamespace

import pytest

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit.emit import emit_script
from coordinator_core.ops.dispatch_emit.spine_read import MemoRowNoReceiptError, MemoRowNoReceiverError, read_spine

from .conftest import REVIEW_KW

_OUTBOX = ".coordinator-local/memo-outbox"
_MEMO_ROW = (
    "- id: M1\n  title: send the memo\n  change_kind: doc-edit\n"
    f"  surface: {_OUTBOX}/topic.md\n  writes: []\n  memo_to: example-game-repo-em\n  disposition: open\n"
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
    assert _unsent(tmp_path, plan) == ["M1 (other)"]
    _receipt(tmp_path, "other")
    assert _unsent(tmp_path, plan) == []


def test_memo_row_naming_no_slug_is_refused(tmp_path):
    row = _MEMO_ROW.replace(f"{_OUTBOX}/topic.md", "docs/x.md").replace(
        "change_kind: doc-edit", "change_kind: doc-edit\n  kind: memo-send"
    )
    with pytest.raises(MemoRowNoReceiptError):
        read_spine(_plan(tmp_path, row))


def test_memo_row_without_memo_to_is_refused_naming_row_and_field(tmp_path):
    row = _MEMO_ROW.replace("  memo_to: example-game-repo-em\n", "")
    with pytest.raises(MemoRowNoReceiverError, match=r"'M1'.*memo_to"):
        read_spine(_plan(tmp_path, row, _CODE_ROW))


def test_memo_row_with_blank_memo_to_is_refused(tmp_path):
    row = _MEMO_ROW.replace("memo_to: example-game-repo-em", "memo_to: '  '")
    with pytest.raises(MemoRowNoReceiverError):
        read_spine(_plan(tmp_path, row))


def test_deferred_memo_row_without_memo_to_is_not_refused(tmp_path):
    row = _MEMO_ROW.replace("  memo_to: example-game-repo-em\n", "").replace(
        "disposition: open", "disposition: open\n  deferred: true"
    )
    assert [r.id for r in read_spine(_plan(tmp_path, row, _CODE_ROW))] == ["C1"]


def test_memo_row_with_memo_to_reads_and_stages(tmp_path):
    exclusions: list = []
    read_spine(_plan(tmp_path, _MEMO_ROW, _CODE_ROW), exclusions=exclusions)
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-send")]


def test_non_memo_empty_writes_row_is_unchanged(tmp_path):
    plan = _plan(tmp_path, _EMPTY_ROW, _CODE_ROW)
    rows = read_spine(plan)
    assert [r.id for r in rows] == ["E1", "C1"]
    script = emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
    assert "memo-send row" not in script


def _unsent(root, plan) -> list:
    return cb._unsent_memos(SimpleNamespace(root=root), plan.read_text(encoding="utf-8"))


def test_row_without_a_receipt_stays_open_and_names_the_slug(tmp_path):
    plan = _plan(tmp_path, _MEMO_ROW, _CODE_ROW)
    assert _unsent(tmp_path, plan) == ["M1 (topic)"]


def test_row_with_a_receipt_closes(tmp_path):
    plan = _plan(tmp_path, _MEMO_ROW, _CODE_ROW)
    _receipt(tmp_path)
    assert _unsent(tmp_path, plan) == []


def test_non_memo_rows_never_held_by_the_receipt_gate(tmp_path):
    plan = _plan(tmp_path, _EMPTY_ROW, _CODE_ROW)
    assert _unsent(tmp_path, plan) == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_implemented_flip_names_the_unsent_memo(tmp_path):
    from coordinator_core.ops.ceremony.tests.test_close_background_land_run_legs import (
        PLAN,
        _build,
        _fake_mint,
        _flip_ctx,
    )

    root = tmp_path / "repo"
    sha = _build(root, spine=True)
    _fake_mint(root / PLAN, root)
    plan = root / PLAN
    plan.write_text(plan.read_text(encoding="utf-8").replace("```\n", _MEMO_ROW + "```\n", 1), encoding="utf-8")
    refused = cb._plan_flip(_flip_ctx(root, sha))
    assert refused.status == "failed" and "memo not sent" in refused.detail and "M1 (topic)" in refused.detail
    _receipt(root)
    after = cb._plan_flip(_flip_ctx(root, sha))
    assert "memo not sent" not in after.detail


def _with_verifier_and_criterion(tmp_path, *rows):
    """The plan and roster under which both readers of the whole plan are emitted."""
    plan = _plan(tmp_path, *rows)
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            "title: p\n", "title: p\nprime_exit_criterion:\n  statement: the memo is sent and a.py changes\n", 1
        ),
        encoding="utf-8",
    )
    roster = copy.deepcopy(REVIEW_KW["review_roster_fragment"])
    wave = next(s for s in roster["execute_review"]["stages"] if s["kind"] == "review-wave")
    wave["agents"].append(
        {"agentType": "coordinator:delivery-verifier", "model": "sonnet", "effort": "low", "per": "whole-diff", "schema": "wave"}
    )
    return plan, {**REVIEW_KW, "review_roster_fragment": roster}


def test_verifier_and_judge_are_told_the_memo_row_is_em_owned(tmp_path):
    plan, kw = _with_verifier_and_criterion(tmp_path, _MEMO_ROW, _CODE_ROW)
    script = emit_script(plan, repo_root=tmp_path, **kw)
    assert script.count("EM-owned rows (never dispatched to an executor") >= 2
    assert "M1 (EM: send topic" in script


def test_no_em_owned_clause_when_every_row_is_dispatched(tmp_path):
    plan, kw = _with_verifier_and_criterion(tmp_path, _CODE_ROW)
    assert "EM-owned rows" not in emit_script(plan, repo_root=tmp_path, **kw)
