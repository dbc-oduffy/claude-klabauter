"""
Regression test — C3, docs/dispatch brief 2026-08-31-the-memo-channel-s-surviving-three.

Pins the wrong-body-shape guard `memo_send.py` § "Duplicate-body detector":
two drafts staged in one session's `state/memo-outbox/`, the second carrying
a body byte-identical to the first (frontmatter stripped, trailing
whitespace normalised) under a DIFFERENT topic. That is the documented
2026-08-21 incident shape — a stale draft resent under a new topic slug
while the original still sits in the outbox.

Spec backlink: coordinator_core/ops/fleet/memo_send.py module docstring,
negative-spec bullet "Does NOT allow a send whose staged body is
byte-identical ... to another `*.md` draft already sitting in the same
sender's `state/memo-outbox/` under a DIFFERENT topic".

Negative-spec (what this test file does NOT assert):
  - Does NOT test the detector in isolation via `_find_duplicate_draft_topic`
    directly — the AC is refusal at the OP layer (`_memo_send`), so the
    fixture drives real staged drafts through the real handler.
  - Does NOT assert anything about the CLI (`cross-repo-memo`) — this is the
    op-layer guard, which the module docstring notes runs "at the OP layer
    (not the CLI), so a direct `coordinator-invoke memo.send` is covered too."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_send import _memo_send

from .test_memo_send import _make_claude_home, _make_receiver_git_repo, _make_sender_git_repo, _write_draft

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _stage_two_drafts(tmp_path, monkeypatch, *, first_body: str, second_body: str):
    """Two drafts staged in one session — `earlier-topic` first, then
    `later-topic` carrying `second_body`. Returns (sender_repo, receiver_repo).
    """
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    _write_draft(sender_repo, "earlier-topic", body=first_body)
    _write_draft(sender_repo, "later-topic", body=second_body)
    return sender_repo, receiver_repo


class TestWrongBodyCollisionRefused:
    def test_second_send_with_identical_body_is_refused(self, tmp_path, monkeypatch, capsys):
        sender_repo, receiver_repo = _stage_two_drafts(
            tmp_path, monkeypatch,
            first_body="The bug is in memo_send.py today.\n",
            second_body="The bug is in memo_send.py today.\n",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "later-topic"}, repo_root=sender_repo,
        )

        assert result["exit_code"] == 1, result
        assert result["acted"] == []
        err = capsys.readouterr().err
        assert "earlier-topic" in err, err

        # Nothing was delivered into the receiver's inbox.
        inbox_files = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert inbox_files == []

        # Nothing sender-side moved either — the refusal is before any write.
        assert (sender_repo / "state" / "memo-outbox" / "later-topic.md").exists()
        assert not (sender_repo / "state" / "memo-outbox" / "sent").exists()

    def test_trailing_whitespace_and_blank_lines_still_collide(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The comparison normalises trailing whitespace per line and
        collapses trailing blank lines — a body differing only in that is
        still the same draft resent, per `_normalize_body`."""
        sender_repo, receiver_repo = _stage_two_drafts(
            tmp_path, monkeypatch,
            first_body="The bug is in memo_send.py today.\n",
            second_body="The bug is in memo_send.py today.   \n\n\n",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "later-topic"}, repo_root=sender_repo,
        )

        assert result["exit_code"] == 1, result
        assert "earlier-topic" in capsys.readouterr().err


class TestCorruptSiblingDraftIsSkipped:
    def test_non_utf8_sibling_draft_is_skipped_and_send_still_succeeds(
        self, tmp_path, monkeypatch,
    ):
        """A stray/corrupt sibling draft with non-UTF-8 bytes must be skipped
        by the duplicate-body scan, not raise out of it — module docstring
        `_find_duplicate_draft_topic`: "A candidate this cannot read or parse
        is skipped rather than treated as a match or a failure — a
        stray/corrupt sibling draft must not block an unrelated send."

        Review: coordinatorcode-reviewer Finding 1/2 — pins the fix for
        `except OSError:` (too narrow; `UnicodeDecodeError` is a `ValueError`
        subclass) missing this exact case. Fails against the pre-fix
        `except OSError:` and passes once widened to `(OSError, ValueError)`.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # The send's own draft — an unrelated body.
        _write_draft(
            sender_repo, "later-topic",
            body="An entirely unrelated body about something else.\n",
        )
        # A corrupt sibling draft directly in the outbox, non-UTF-8 bytes —
        # never committed/tracked, mirroring an on-disk-only stray file.
        outbox = sender_repo / "state" / "memo-outbox"
        (outbox / "corrupt-sibling.md").write_bytes(b"\xff\xfe\x00garbage not utf-8")

        result = _memo_send(
            {"dry_run": False, "topic": "later-topic"}, repo_root=sender_repo,
        )

        assert result["exit_code"] == 0, result
        assert len(result["acted"]) == 1
        inbox_files = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert len(inbox_files) == 1


class TestDifferentBodiesSendCleanly:
    def test_two_drafts_with_genuinely_different_bodies_both_send(
        self, tmp_path, monkeypatch,
    ):
        """Negative case pinning the detector's BOUNDARY, not merely its
        existence — two unrelated drafts staged in the same session must not
        collide."""
        sender_repo, receiver_repo = _stage_two_drafts(
            tmp_path, monkeypatch,
            first_body="Correction: ops/fleet/memo_send.py was already fixed.\n",
            second_body="An entirely unrelated second memo about something else.\n",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "later-topic"}, repo_root=sender_repo,
        )

        assert result["exit_code"] == 0, result
        assert len(result["acted"]) == 1
        inbox_files = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert len(inbox_files) == 1
        assert not (sender_repo / "state" / "memo-outbox" / "later-topic.md").exists()


class TestEmptyBodyIsNotACollision:
    def test_two_deliberately_body_less_drafts_are_not_a_collision(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Two deliberately body-less drafts staged under different topics
        are NOT flagged as colliding — the detector is skipped entirely when
        the normalised body is empty (module docstring negative-spec,
        `memo_send.py` § "Skipped only when the body is empty").

        `memo.send` itself still refuses an empty-body draft outright — via
        the UNRELATED `has_prose_body` guard a few lines further down
        (`_compose_delivered_content`), which fires on ANY empty body,
        colliding sibling or not. The boundary this test pins is which
        refusal fires: the ordinary "no prose" one, never a false collision
        naming `earlier-topic` — that distinction is only observable in the
        refusal reason, since both bodies being empty means the send fails
        either way.
        """
        sender_repo, receiver_repo = _stage_two_drafts(
            tmp_path, monkeypatch, first_body="\n", second_body="\n",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "later-topic"}, repo_root=sender_repo,
        )

        assert result["exit_code"] == 1, result
        assert result["acted"] == []
        err = capsys.readouterr().err
        assert "no prose" in err, err
        assert "earlier-topic" not in err, (
            "an empty-body draft must never be refused as a body-collision "
            f"with its empty-body sibling: {err}"
        )
