"""
Regression test for bug-backlog 2026-09-01-memo-send-silently-drops-non-allow-liste-297a3a6012a8.

`_compose_delivered_content` composed the delivered memo through
`_compose_memo`, which declares no `distill_fate`/`in_repo_capture` kwarg —
so a draft carrying either field was silently delivered without it, no
warning, no refusal, nothing in the delivered file. That also disarmed
`schema_validate.py::_memo_cf_distill_fate`, since a cross-field guard
cannot refuse a field composition already discarded: a
`distill_fate: ratification` + `in_repo_capture: "~/.claude/memory/x.md"`
draft sent with rc 0 and reached the receiver's inbox carrying neither key.

Fixed by `_stamp_distill_fate` (memo_send.py), which carries both fields
through onto the already-composed content before `_memo_send`'s own
`validate_memo_cross_fields(delivered_fm)` call — re-arming the existing
guard rather than adding a new one.

Harness reused from `test_memo_send.py` / `test_memo_send_cc_delivery.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _git,
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _write_draft(
    sender_repo: Path, topic: str, *,
    to: str = "example-retrieval-repo-em", title: str = "A ratified memo",
    summary: str = "a one-line summary", kind: str = "fyi",
    sent_by: str = "d218a65c-2c5b-472e-879c-ae9ed1747030",
    body: str = "Body prose.\n",
    distill_fate: str | None = None,
    in_repo_capture: str | None = None,
) -> Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    extra = ""
    if distill_fate is not None:
        extra += f'distill_fate: "{distill_fate}"\n'
    if in_repo_capture is not None:
        extra += f'in_repo_capture: "{in_repo_capture}"\n'
    content = (
        "---\n"
        f'title: "{title}"\n'
        'from: "claude-klabauter-engine"\n'
        f'to: "{to}"\n'
        "created: 2026-09-01\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        f'summary: "{summary}"\n'
        f'kind: "{kind}"\n'
        f'sent_by: "{sent_by}"\n'
        f"{extra}"
        "---\n\n"
        f"{body}"
    )
    draft_path.write_text(content, encoding="utf-8", newline="\n")
    _git(sender_repo, "add", "--", f"state/memo-outbox/{topic}.md")
    _git(sender_repo, "commit", "-m", f"stage draft {topic}")
    return draft_path


def _inbox_files(receiver_repo: Path) -> list[Path]:
    return [
        p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
        if p.name != ".gitkeep"
    ]


class TestDistillFateSurvivesDelivery:
    def test_ephemeral_distill_fate_is_carried_into_the_delivered_memo(
        self, tmp_path, monkeypatch,
    ):
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": to_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(
            sender_repo, "distill-fate-topic",
            distill_fate="ephemeral",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "distill-fate-topic"}, repo_root=sender_repo,
        )

        assert result["exit_code"] == 0, result
        delivered = _inbox_files(to_repo)
        assert len(delivered) == 1
        content = delivered[0].read_text(encoding="utf-8")
        assert "distill_fate" in content, (
            "distill_fate must be carried into the delivered memo, not "
            "silently dropped by composition"
        )

    def test_ratification_without_in_repo_capture_refuses_loud_not_silent(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Before the fix, this draft was silently delivered with NEITHER
        field, disarming `_memo_cf_distill_fate` (the guard never saw
        `distill_fate: ratification`, so it never fired). After the fix,
        the field reaches `validate_memo_cross_fields` and the guard's own
        `in_repo_capture required` rule refuses the send loud.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": to_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(
            sender_repo, "distill-fate-ratify-topic",
            distill_fate="ratification",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "distill-fate-ratify-topic"}, repo_root=sender_repo,
        )

        assert result["exit_code"] == 1, result
        assert "in_repo_capture" in capsys.readouterr().err
        assert _inbox_files(to_repo) == [], (
            "a disarmed guard let this land before the fix -- must refuse "
            "before any write now"
        )

    def test_ratification_with_claude_memory_capture_refuses_loud(
        self, tmp_path, monkeypatch,
    ):
        """The exact reproduction from the backlog record: distill_fate=
        ratification + a ~/.claude-rooted in_repo_capture. Before the fix
        this sent with rc 0 and delivered neither field. After the fix both
        fields reach the validator and the ~/.claude-pointer rule refuses.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": to_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(
            sender_repo, "distill-fate-repro-topic",
            distill_fate="ratification",
            in_repo_capture="~/.claude/memory/nope.md",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "distill-fate-repro-topic"}, repo_root=sender_repo,
        )

        assert result["exit_code"] == 1, result
        assert _inbox_files(to_repo) == [], (
            "the reproduced defect: a junk probe with a memory-pointer "
            "in_repo_capture must never reach the receiver's inbox"
        )
