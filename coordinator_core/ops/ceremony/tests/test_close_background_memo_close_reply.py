"""The memo-lifecycle leg replies before it resolves, only for a plan with an inbound-close row."""

from __future__ import annotations

from pathlib import Path

from coordinator_core import archive_stamp
from coordinator_core.ops.ceremony import close_background as cb

FIXTURE = (
    Path(__file__).resolve().parents[2] / "dispatch_emit" / "tests" / "fixtures" / "memo_close_only_12a81f.spine.md"
)
PLAN_REL = "docs/plans/pin.md"
MEMO_REL = "state/cross-repo/inbox/2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing.md"
SHA = "abcdef0123456789abcdef0123456789abcdef01"


def _setup(root: Path, monkeypatch, replies=None):
    (root / PLAN_REL).parent.mkdir(parents=True)
    (root / PLAN_REL).write_bytes(FIXTURE.read_bytes())
    inbox = root / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / Path(MEMO_REL).name).write_text("---\nfrom: coordinator-content-repo-em\ntitle: T\n---\nbody\n", encoding="utf-8")
    order = []
    replies = dict(replies or {})

    def dispatch(op, params, *, repo_root=None):
        order.append(op)
        return replies.get(op, {"exit_code": 0, "failed": []})

    def resolve(path, *args, return_result=False):
        order.append("resolve")
        return {"exit_code": 0}

    monkeypatch.setattr("coordinator_core.warm.entry_seam.reentrant_dispatch", dispatch)
    monkeypatch.setattr(archive_stamp, "cs_resolve_memo", resolve)
    return order


def _ctx(root: Path, *, plan_rel=PLAN_REL, memo=MEMO_REL):
    record = {"plan_rel": plan_rel} if plan_rel else {}
    return cb.JobContext(root, root / ".git", "run-1", (), memo, SHA, record=record)


def test_leg_sends_the_reply_then_resolves(tmp_path, monkeypatch):
    order = _setup(tmp_path, monkeypatch)

    result = cb._memo_lifecycle(_ctx(tmp_path))

    assert result.status == "ran" and "reply" in result.detail
    assert order == ["memo.draft", "memo.compose", "memo.send", "resolve"]


def test_refused_send_leaves_the_memo_unresolved(tmp_path, monkeypatch):
    order = _setup(tmp_path, monkeypatch, {"memo.send": {"exit_code": 1, "failed": []}})

    result = cb._memo_lifecycle(_ctx(tmp_path))

    assert result.status == "failed"
    assert "resolve" not in order


def test_plan_without_inbound_close_row_resolves_with_zero_dispatch(tmp_path, monkeypatch):
    order = _setup(tmp_path, monkeypatch)

    result = cb._memo_lifecycle(_ctx(tmp_path, memo="state/cross-repo/inbox/other.md"))
    assert result.status != "ran"  # other.md is absent from the inbox
    assert order == []

    (tmp_path / "state" / "cross-repo" / "inbox" / "other.md").write_text("---\nstatus: open\n---\n", encoding="utf-8")
    result = cb._memo_lifecycle(_ctx(tmp_path, memo="state/cross-repo/inbox/other.md"))

    assert result.status == "ran"
    assert order == ["resolve"]
