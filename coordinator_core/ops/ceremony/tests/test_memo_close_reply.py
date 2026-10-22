"""send_close_reply over the 12a81f pin spine: draft, compose, send in order; each refusal path."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import memo_close_reply as mcr

FIXTURE = (
    Path(__file__).resolve().parents[2] / "dispatch_emit" / "tests" / "fixtures" / "memo_close_only_12a81f.spine.md"
)
PLAN_REL = "docs/plans/pin.md"
MEMO_REL = "state/cross-repo/inbox/2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing.md"
MEMO_NAME = Path(MEMO_REL).name
TOPIC = "re-2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing"
OK = {"exit_code": 0, "failed": []}


def _repo(root: Path, *, plan: bool = True, memo: str = "---\nfrom: coordinator-content-repo-em\ntitle: Refire halts\n---\nbody\n") -> Path:
    if plan:
        (root / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
        (root / PLAN_REL).write_bytes(FIXTURE.read_bytes())
    inbox = root / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / MEMO_NAME).write_text(memo, encoding="utf-8")
    return root


def _record(monkeypatch, replies=None):
    calls = []
    replies = dict(replies or {})

    def fake(op, params, *, repo_root=None):
        calls.append((op, params))
        return replies.get(op, OK)

    monkeypatch.setattr("coordinator_core.warm.entry_seam.reentrant_dispatch", fake)
    return calls


def test_cited_shas_over_fixture_row_in_order():
    row = mcr._plan_rows(FIXTURE.parent, FIXTURE.name)[0]
    assert mcr.cited_shas(f"{row['title']}\n{row['body']}") == ("dff8d697ef", "901de7c65f")


def test_cited_shas_ignores_letter_only_words_and_short_tokens():
    assert mcr.cited_shas("defaced abcdef0 abc123 2026-10-22") == ("abcdef0",)


def test_reply_is_drafted_composed_then_sent(tmp_path, monkeypatch):
    _repo(tmp_path)
    calls = _record(monkeypatch)

    result = mcr.send_close_reply(tmp_path, PLAN_REL, MEMO_REL, "run-1", None)

    assert result.status == "ran"
    assert [c[0] for c in calls] == ["memo.draft", "memo.compose", "memo.send"]
    draft, compose, send = (c[1] for c in calls)
    assert draft["to"] == "coordinator-content-repo-em" and draft["in_reply_to"] == MEMO_NAME and draft["topic"] == TOPIC
    assert "dff8d697ef" in compose["body"] and "901de7c65f" in compose["body"] and "run-1" in compose["body"]
    assert send == {"dry_run": False, "topic": TOPIC}


def test_existing_sender_receipt_dispatches_nothing(tmp_path, monkeypatch):
    from coordinator_core.ops.fleet.memo_wire import memo_send_performed_paths

    _repo(tmp_path)
    receipt = tmp_path / memo_send_performed_paths(TOPIC)[0]
    receipt.parent.mkdir(parents=True)
    receipt.write_text("sent\n", encoding="utf-8")
    calls = _record(monkeypatch)

    result = mcr.send_close_reply(tmp_path, PLAN_REL, MEMO_REL, "run-1", None)

    assert result.status == "ran" and "already sent" in result.detail
    assert calls == []


def test_refused_send_is_failed_with_the_reason(tmp_path, monkeypatch):
    _repo(tmp_path)
    calls = _record(monkeypatch, {"memo.send": {"exit_code": 2, "failed": [{"reason": "receiver unreachable"}]}})

    result = mcr.send_close_reply(tmp_path, PLAN_REL, MEMO_REL, "run-1", None)

    assert result.status == "failed" and "receiver unreachable" in result.detail
    assert [c[0] for c in calls][-1] == "memo.send"


def test_draft_collision_from_an_earlier_refire_still_composes_and_sends(tmp_path, monkeypatch):
    _repo(tmp_path)
    calls = _record(monkeypatch, {"memo.draft": {"exit_code": 2, "failed": [{"reason": "collision: draft exists"}]}})

    assert mcr.send_close_reply(tmp_path, PLAN_REL, MEMO_REL, "run-1", None).status == "ran"
    assert [c[0] for c in calls] == ["memo.draft", "memo.compose", "memo.send"]


def test_no_cited_sha_falls_back_to_terminal_sha(tmp_path, monkeypatch):
    _repo(tmp_path)
    plan = tmp_path / PLAN_REL
    plan.write_text(
        FIXTURE.read_text(encoding="utf-8").replace("dff8d697ef", "nosha").replace("901de7c65f", "nosha"),
        encoding="utf-8",
    )
    calls = _record(monkeypatch)

    assert mcr.send_close_reply(tmp_path, PLAN_REL, MEMO_REL, "run-1", "f" * 7 + "0123456789abcdef") .status == "ran"
    assert "fffffff012" in calls[1][1]["body"]


def test_no_cited_sha_and_no_terminal_sha_is_failed(tmp_path, monkeypatch):
    _repo(tmp_path)
    (tmp_path / PLAN_REL).write_text(
        FIXTURE.read_text(encoding="utf-8").replace("dff8d697ef", "nosha").replace("901de7c65f", "nosha"),
        encoding="utf-8",
    )
    calls = _record(monkeypatch)

    assert mcr.send_close_reply(tmp_path, PLAN_REL, MEMO_REL, "run-1", None).status == "failed"
    assert calls == []


@pytest.mark.parametrize("memo", ["---\ntitle: t\n---\nbody\n", "no frontmatter\n"])
def test_memo_without_from_is_failed(tmp_path, monkeypatch, memo):
    _repo(tmp_path, memo=memo)
    calls = _record(monkeypatch)

    assert mcr.send_close_reply(tmp_path, PLAN_REL, MEMO_REL, "run-1", None).status == "failed"
    assert calls == []


def test_plan_without_an_inbound_close_row_owes_nothing(tmp_path, monkeypatch):
    _repo(tmp_path)
    calls = _record(monkeypatch)

    assert mcr.send_close_reply(tmp_path, PLAN_REL, "state/cross-repo/inbox/other.md", "run-1", None) is None
    assert mcr.send_close_reply(tmp_path, None, MEMO_REL, "run-1", None) is None
    assert calls == []
