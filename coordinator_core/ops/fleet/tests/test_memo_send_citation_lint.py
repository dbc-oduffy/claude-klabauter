"""memo.send holds a cross-repo send once when its body cites a
docs/state/coordinator/archive/cross-repo path without a repo qualifier —
C3 (docs/plans/2026-09-11-memo-send-path-fail-loud.md).

Unit tests (no spawn marker) exercise `_repo_qualifier_names` /
`_unqualified_path_citations` directly. Op-level tests
(`cadence` + `spawns_process`) seed a sender/receiver pair the same way
`test_memo_send_duplicate_reply_warning.py` does and call `_memo_send`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_send import (
    _memo_send,
    _repo_qualifier_names,
    _send_ack_path,
    _unqualified_path_citations,
)
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _write_draft,
)
from coordinator_core.ops.fleet.tests.test_memo_send_duplicate_reply_warning import (
    _seed_prior_reply_row,
)

_QUALIFIERS = frozenset({"doe-claude", "example-retrieval-repo", "claude-klabauter", "receiver-repo"})


# ---------------------------------------------------------------------------
# _repo_qualifier_names
# ---------------------------------------------------------------------------

class TestRepoQualifierNames:
    def test_registry_key_lowercased_and_underscore_to_hyphen(self):
        names = _repo_qualifier_names({"example_retrieval_repo": "/some/path/receiver-repo"})
        assert "example-retrieval-repo" in names

    def test_basename_of_registry_path_included(self):
        names = _repo_qualifier_names({"example_retrieval_repo": "/some/path/receiver-repo"})
        assert "receiver-repo" in names

    def test_empty_registry_yields_empty_set(self):
        assert _repo_qualifier_names({}) == frozenset()

    def test_none_registry_yields_empty_set(self):
        assert _repo_qualifier_names(None) == frozenset()


# ---------------------------------------------------------------------------
# _unqualified_path_citations
# ---------------------------------------------------------------------------

class TestUnqualifiedPathCitations:
    def test_repo_colon_path_form_not_flagged(self):
        body = "See DoE-claude coordinator/docs/wiki/x.md for detail."
        hits = _unqualified_path_citations(body, _QUALIFIERS)
        assert hits == []

    def test_repo_colon_path_with_line_number_not_flagged(self):
        body = "example-retrieval-repo:mcp/x.py:12 has the bug."
        hits = _unqualified_path_citations(body, _QUALIFIERS)
        assert hits == []

    def test_repo_name_backticked_path_not_flagged(self):
        body = "claude-klabauter `coordinator/bin/x` is the file."
        hits = _unqualified_path_citations(body, _QUALIFIERS)
        assert hits == []

    def test_url_form_not_flagged(self):
        body = "https://github.com/o/r/blob/main/docs/x.md is the link."
        hits = _unqualified_path_citations(body, _QUALIFIERS)
        assert hits == []

    def test_bare_docs_path_flagged(self):
        body = "See docs/x.md for detail."
        hits = _unqualified_path_citations(body, _QUALIFIERS)
        assert hits == ["docs/x.md"]

    def test_bare_state_path_flagged(self):
        body = "state/lessons/y.md has the note."
        hits = _unqualified_path_citations(body, _QUALIFIERS)
        assert hits == ["state/lessons/y.md"]

    def test_unknown_preceding_name_does_not_qualify(self):
        body = "see docs/x.md for detail."
        hits = _unqualified_path_citations(body, _QUALIFIERS)
        assert hits == ["docs/x.md"]

    def test_repeated_path_reported_once(self):
        body = "docs/x.md is discussed. Also read docs/x.md again."
        hits = _unqualified_path_citations(body, _QUALIFIERS)
        assert hits == ["docs/x.md"]

    def test_does_not_read_files(self, tmp_path, monkeypatch):
        # Negative-spec smoke: a body naming a path that does not exist on
        # disk still runs to completion (no file read attempted).
        monkeypatch.chdir(tmp_path)
        hits = _unqualified_path_citations("docs/does-not-exist-anywhere.md", _QUALIFIERS)
        assert hits == ["docs/does-not-exist-anywhere.md"]


# ---------------------------------------------------------------------------
# Op-level
# ---------------------------------------------------------------------------

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture()
def sender_and_receiver(tmp_path, monkeypatch):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    return sender_repo, receiver_repo


class TestCitationLintHeldOnce:
    def test_bare_path_held_once_then_delivers(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _write_draft(
            sender_repo, "bare-path-topic",
            body="See docs/x.md for detail.\n",
        )

        first = _memo_send({"dry_run": False, "topic": "bare-path-topic"}, repo_root=sender_repo)
        assert first["exit_code"] == 1
        inbox = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert inbox == []
        ack = _send_ack_path(sender_repo, "citation-lint:bare-path-topic")
        assert ack.is_file()

        second = _memo_send({"dry_run": False, "topic": "bare-path-topic"}, repo_root=sender_repo)
        assert second["exit_code"] == 0, second
        inbox = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert len(inbox) == 1

    def test_qualified_path_delivers_first_call(self, sender_and_receiver):
        sender_repo, receiver_repo = sender_and_receiver
        _write_draft(
            sender_repo, "qualified-path-topic",
            body="See example-retrieval-repo:docs/x.md for detail.\n",
        )

        result = _memo_send({"dry_run": False, "topic": "qualified-path-topic"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result

    def test_self_send_bare_path_delivers_first_call(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        (sender_repo / "cross-repo" / "inbox").mkdir(parents=True)
        (sender_repo / "cross-repo" / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": sender_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(
            sender_repo, "self-send-topic",
            body="See docs/x.md for detail.\n",
        )

        result = _memo_send({"dry_run": False, "topic": "self-send-topic"}, repo_root=sender_repo)
        assert result["exit_code"] == 0, result

    def test_both_c2_and_c3_held_together_then_delivers(self, sender_and_receiver, caplog):
        # Both gates fire on ONE refusal and both acks land in that pass, so
        # the retry the refusal promises actually sends.
        sender_repo, receiver_repo = sender_and_receiver
        _seed_prior_reply_row(
            sender_repo,
            in_reply_to="orig.md",
            sent_by="session-a",
        )
        _write_draft(
            sender_repo, "both-warnings-topic",
            body="See docs/x.md for detail.\n",
        )
        # Splice an in_reply_to onto the staged draft so C2's predicate fires.
        draft_path = sender_repo / "state" / "memo-outbox" / "both-warnings-topic.md"
        text = draft_path.read_text(encoding="utf-8")
        text = text.replace(
            'sent_by: "d218a65c-2c5b-472e-879c-ae9ed1747030"\n',
            'sent_by: "d218a65c-2c5b-472e-879c-ae9ed1747030"\n'
            'in_reply_to: "orig.md"\n',
        )
        draft_path.write_text(text, encoding="utf-8", newline="\n")

        first = _memo_send({"dry_run": False, "topic": "both-warnings-topic"}, repo_root=sender_repo)
        assert first["exit_code"] == 1
        assert _send_ack_path(sender_repo, "duplicate-reply:both-warnings-topic").is_file()
        assert _send_ack_path(sender_repo, "citation-lint:both-warnings-topic").is_file()
        refusal = caplog.text
        assert "already answered" in refusal
        assert "not repo-qualified" in refusal
        assert refusal.count("Nothing was written") == 1

        second = _memo_send({"dry_run": False, "topic": "both-warnings-topic"}, repo_root=sender_repo)
        assert second["exit_code"] == 0, second
        inbox = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert len(inbox) == 1
