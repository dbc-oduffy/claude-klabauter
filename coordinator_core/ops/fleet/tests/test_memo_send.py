"""
Tests for coordinator_core.ops.fleet.memo_send — memo.send MUTATING op
(rebuilt 2026-08-25, three-write shape).

C2 test surface (docs/plans/2026-08-25-memo-send-three-writes-and-one-commit-th.md § C2):
  - param validation: dry_run type, unknown params rejected, topic slug
  - dry_run vs act envelope shape
  - missing/malformed staged draft -> setup error
  - end-to-end delivery: receiver-inbox write+commit, sent/ stamp, ledger row,
    original draft removed, sender-side commit — AC1, AC2
  - AC3: receiver-side commit runs no hook, exactly one git spawn (update-index)
  - AC4: a declining commit_authored_new_file fails loud, no sender-side
    receipt written (never falls back to a spawning/hook-running commit)
  - AC6: inbox collision refused on both legs (pre-check AND O_EXCL) independently
  - AC8: op reachable through _registry_map / ipc._REGISTRY

Harness: asyncio-free — `_memo_send` is a plain sync function (unlike the
killed original's async handler); called directly.
Pattern: real temp git repos for sender + receiver (git plumbing under test);
CLAUDE_HOME monkeypatched for machine-local registry isolation.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path

from types import SimpleNamespace

import pytest

from coordinator_core.frontmatter.primitives import split_frontmatter, read_fm_field_unquoted
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet import memo_send as memo_send_module
from coordinator_core.ops.fleet.memo_send import (
    _KNOWN_PARAM_KEYS,
    _MODE,
    _SENT_LEDGER_FILENAME,
    _SENT_LEDGER_MAX_AGE_DAYS,
    _SENT_LEDGER_MAX_ROWS,
    _memo_send,
    _validate_send_params,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Git repo factories
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=str(repo), capture_output=True, check=check,
        **no_console_creationflags(),
    )


def _make_sender_git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sender-repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _make_receiver_git_repo(tmp_path: Path, name: str = "receiver-repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init receiver")
    return root


# ---------------------------------------------------------------------------
# Registry fixture (mirrors sibling memo.* test files' _make_claude_home)
# ---------------------------------------------------------------------------

def _make_claude_home(tmp_path: Path, receiver_repos: dict[str, Path]) -> Path:
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return claude_home


def _write_draft(
    sender_repo: Path, topic: str, *,
    to: str = "example-retrieval-repo-em", title: str = "A test memo",
    summary: str = "a one-line summary", kind: str = "fyi",
    sent_by: str = "d218a65c-2c5b-472e-879c-ae9ed1747030",
    body: str = "Body prose.\n",
    track: bool = True,
) -> Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    content = (
        "---\n"
        f'title: "{title}"\n'
        'from: "claude-klabauter-engine"\n'
        f'to: "{to}"\n'
        "created: 2026-08-25\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        f'summary: "{summary}"\n'
        f'kind: "{kind}"\n'
        f'sent_by: "{sent_by}"\n'
        "---\n\n"
        f"{body}"
    )
    draft_path.write_text(content, encoding="utf-8", newline="\n")
    # A staged outbox draft is tracked at rest (memo.draft's own commit
    # happens elsewhere in the real workflow) — commit_scoped's deletion
    # leg needs `state/memo-outbox/<topic>.md` to already be a KNOWN path
    # for `git add -- <deleted-path>` to recognize its removal.
    if track:
        _git(sender_repo, "add", "--", f"state/memo-outbox/{topic}.md")
        _git(sender_repo, "commit", "-m", f"stage draft {topic}")
    return draft_path


def _base_params(**overrides) -> dict:
    params = {"dry_run": True, "topic": "some-topic"}
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------

class TestValidateSendParams:
    def test_dry_run_must_be_bool(self):
        result = _validate_send_params({"dry_run": "yes", "topic": "x"})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_unknown_param_rejected(self):
        result = _validate_send_params({"dry_run": True, "topic": "x", "body": "nope"})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_known_param_keys_is_exactly_dry_run_and_topic(self):
        assert _KNOWN_PARAM_KEYS == frozenset({"dry_run", "topic"})

    def test_invalid_topic_slug_rejected(self):
        result = _validate_send_params({"dry_run": True, "topic": "NOT-A-SLUG!!"})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_missing_topic_rejected(self):
        result = _validate_send_params({"dry_run": True, "topic": ""})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_valid_params_pass_through(self):
        result = _validate_send_params({"dry_run": False, "topic": "a-topic"})
        assert result == (False, "a-topic")


# ---------------------------------------------------------------------------
# Missing / malformed draft
# ---------------------------------------------------------------------------

class TestMissingDraft:
    def test_no_repo_root_is_setup_error(self):
        result = _memo_send({"dry_run": True, "topic": "x"}, repo_root=None)
        assert result["exit_code"] == 1
        assert result["mode"] == _MODE

    def test_no_staged_draft_is_setup_error(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        result = _memo_send({"dry_run": True, "topic": "never-drafted"}, repo_root=sender_repo)
        assert result["exit_code"] == 1

    def test_draft_missing_required_field_is_setup_error(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        outbox = sender_repo / "state" / "memo-outbox"
        outbox.mkdir(parents=True)
        (outbox / "bad-topic.md").write_text(
            '---\ntitle: "x"\nto: "example-retrieval-repo-em"\n---\n\nbody\n',
            encoding="utf-8",
        )
        result = _memo_send({"dry_run": False, "topic": "bad-topic"}, repo_root=sender_repo)
        assert result["exit_code"] == 1

    def test_draft_with_no_prose_body_is_refused(self, tmp_path, monkeypatch, capsys):
        # Regression, DoE-claude 2026-08-19: a scaffold composed and never
        # written back was delivered as frontmatter plus empty comment blocks,
        # its title advertising four items the body did not carry. Refuse at
        # the last step before the write into someone else's repo.
        sender_repo = _make_sender_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        outbox = sender_repo / "state" / "memo-outbox"
        outbox.mkdir(parents=True)
        (outbox / "bodyless.md").write_text(
            '---\n'
            'title: "Four items from one incident"\n'
            'from: "claude-klabauter-em"\n'
            'to: "example-retrieval-repo-em"\n'
            'kind: "ask"\n'
            'summary: "Four items, ranked by cost."\n'
            '---\n\n'
            "<!-- Compose your memo body here (memo.compose), then deliver it\n"
            "     via memo.send. -->\n"
            "<!-- a second block, also spanning lines,\n"
            "     with nothing but guidance inside it -->\n",
            encoding="utf-8",
        )
        result = _memo_send({"dry_run": False, "topic": "bodyless"}, repo_root=sender_repo)
        assert result["exit_code"] == 1
        assert "no prose" in capsys.readouterr().err
        # Nothing was written into the receiver's tree.
        assert result["acted"] == []


# ---------------------------------------------------------------------------
# dry_run preview
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_previews_without_writing(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "dry-run-topic")

        result = _memo_send({"dry_run": True, "topic": "dry-run-topic"}, repo_root=sender_repo)

        assert result["exit_code"] == 0
        assert result["dry_run"] is True
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["collision"] is False
        assert list((receiver_repo / "cross-repo" / "inbox").glob("*.md")) == []
        assert not (sender_repo / ".coordinator-local" / "memo-outbox" / "sent").exists()

    def test_unknown_receiver_is_setup_error(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "unknown-receiver-topic", to="nobody-em")

        result = _memo_send({"dry_run": True, "topic": "unknown-receiver-topic"}, repo_root=sender_repo)

        assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# End-to-end delivery — AC1, AC2
# ---------------------------------------------------------------------------

class TestEndToEndDelivery:
    def test_happy_path_three_writes(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "happy-topic")

        result = _memo_send({"dry_run": False, "topic": "happy-topic"}, repo_root=sender_repo)

        assert result["exit_code"] == 0, result
        assert len(result["acted"]) == 1
        acted = result["acted"][0]
        assert acted["written"] is True
        assert acted["committed"] is True
        assert acted["sender_committed"] is True

        # Write 1: receiver inbox file, committed.
        inbox_files = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert len(inbox_files) == 1
        delivered = inbox_files[0]
        assert "claude-klabauter-engine" in delivered.name
        assert "happy-topic" in delivered.name
        show = _git(receiver_repo, "show", f"HEAD:cross-repo/inbox/{delivered.name}")
        assert show.stdout.decode("utf-8") == delivered.read_text(encoding="utf-8")
        assert "status: open" in delivered.read_text(encoding="utf-8")
        status = _git(receiver_repo, "status", "--porcelain")
        assert status.stdout.decode("utf-8").strip() == ""

        # Write 2: sender-side sent/ copy + ledger row (new root), original
        # draft gone from the legacy root it was staged at (`_write_draft`
        # stages at `state/memo-outbox/` -- read-compatibility proof: a
        # legacy-root draft is still found, sent, and its sent-side receipt
        # lands at the new `.coordinator-local/memo-outbox/` root).
        assert not (sender_repo / "state" / "memo-outbox" / "happy-topic.md").exists()
        sent_path = sender_repo / ".coordinator-local" / "memo-outbox" / "sent" / "happy-topic.md"
        assert sent_path.exists()
        sent_split = split_frontmatter(sent_path.read_text(encoding="utf-8"))
        assert read_fm_field_unquoted(sent_split.fm_text, "status") == "sent"
        assert read_fm_field_unquoted(sent_split.fm_text, "sent_at")
        assert read_fm_field_unquoted(sent_split.fm_text, "delivered_to")

        ledger_path = sender_repo / ".coordinator-local" / "memo-outbox" / _SENT_LEDGER_FILENAME
        rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["topic"] == "happy-topic"
        assert rows[0]["to"] == "example-retrieval-repo-em"
        assert rows[0]["kind"] == "fyi"
        assert rows[0]["delivery_commit_sha"]
        assert rows[0]["sent_by"] == "d218a65c-2c5b-472e-879c-ae9ed1747030"

        # Write 3: one sender-side commit covering all three paths.
        #
        # PROVEN FROM HEAD, NEVER FROM A CLEAN TREE. Both branches of
        # `git_native.commit_scoped` build their tree under a throwaway index
        # (GIT_INDEX_FILE redirected) and land it with `commit-tree` +
        # `update-ref`, deliberately leaving the SHARED index untouched --
        # mutating it would clobber the ~50 concurrent sessions this repo's
        # load norm assumes. So a correct receipt commit leaves the committed
        # paths looking staged-deleted to `git status` until something else
        # refreshes the index, and asserting a clean tree here asserts the
        # opposite of the design. HEAD's content is the actual contract.
        last_commit = _git(sender_repo, "show", "--stat", "--format=", "HEAD")
        stat_text = last_commit.stdout.decode("utf-8")
        assert "happy-topic.md" in stat_text
        assert "sent-ledger.jsonl" in stat_text
        # The three writes are IN the commit, not merely on disk beside it.
        _git(sender_repo, "cat-file", "-e", "HEAD:.coordinator-local/memo-outbox/sent/happy-topic.md")
        _git(sender_repo, "cat-file", "-e", "HEAD:.coordinator-local/memo-outbox/sent-ledger.jsonl")
        gone = _git(
            sender_repo, "cat-file", "-e", "HEAD:state/memo-outbox/happy-topic.md",
            check=False,
        )
        assert gone.returncode != 0, (
            "the outbox original must be DELETED in HEAD, not merely moved on disk"
        )

    def _send_draft_without_sent_by(self, tmp_path, monkeypatch):
        """Drive a full send of a draft carrying no `sent_by` — the ordinary
        field-authored shape, since memo.draft never writes the field and
        memo.compose strips one. Returns (ledger row, delivery commit message).
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        outbox = sender_repo / "state" / "memo-outbox"
        outbox.mkdir(parents=True)
        (outbox / "no-sentby-topic.md").write_text(
            "---\n"
            'title: "x"\n'
            'from: "claude-klabauter-engine"\n'
            'to: "example-retrieval-repo-em"\n'
            "created: 2026-08-25\n"
            "status: draft\n"
            "delivery_mode: receiver-repo\n"
            'summary: "s"\n'
            'kind: "fyi"\n'
            "---\n\nbody\n",
            encoding="utf-8", newline="\n",
        )
        _git(sender_repo, "add", "--", "state/memo-outbox/no-sentby-topic.md")
        _git(sender_repo, "commit", "-m", "stage draft no-sentby-topic")

        result = _memo_send({"dry_run": False, "topic": "no-sentby-topic"}, repo_root=sender_repo)

        assert result["exit_code"] == 0, result
        ledger_path = sender_repo / ".coordinator-local" / "memo-outbox" / _SENT_LEDGER_FILENAME
        row = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
        delivery_msg = _git(
            receiver_repo, "log", "-1", "--format=%B"
        ).stdout.decode("utf-8")
        return row, delivery_msg

    def test_sent_by_absent_on_draft_is_resolved_at_send_time(self, tmp_path, monkeypatch):
        """A draft with no `sent_by` gets one resolved HERE, at send time.

        That is the whole point of the field: memo.draft never authors it and
        memo.compose strips it, so a send that does not resolve it ships every
        field-authored memo unrepliable. Regression for the 2026-08-25 rebuild,
        which delegated the duty upward to a caller that never existed.
        """
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "6f1e0c9a-1111-4222-8333-444455556666")
        row, _ = self._send_draft_without_sent_by(tmp_path, monkeypatch)
        assert row["sent_by"] == "6f1e0c9a-1111-4222-8333-444455556666"

    def test_delivery_commit_carries_a_session_id_trailer(self, tmp_path, monkeypatch):
        """The delivery commit carries `Session-Id:` as a SECOND, independent
        carrier of sender identity.

        DoE's resolve-peer-address.py names a commit trailer as one of three
        inputs to the only sanctioned session-id -> peer-name join, and an
        inbound memo has no claim decision to consult instead. On 2026-08-25
        three memos whose frontmatter carrier had already failed still named
        their sender through this trailer alone.
        """
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "6f1e0c9a-1111-4222-8333-444455556666")
        _, delivery_msg = self._send_draft_without_sent_by(tmp_path, monkeypatch)
        # Its own final paragraph, or git's trailer parser does not see it.
        assert delivery_msg.rstrip("\n").endswith(
            "\n\nSession-Id: 6f1e0c9a-1111-4222-8333-444455556666"
        ), delivery_msg

    def test_delivery_commit_omits_the_trailer_when_unresolved(self, tmp_path, monkeypatch):
        """No trailer at all beats a `Session-Id: unresolved` one — a trailer
        exists to be joined to an address, and a sentinel value would be one
        every reader must learn to reject. The absence is recorded in the
        memo's `sent_by:` frontmatter, which is where it belongs."""
        for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)
        _, delivery_msg = self._send_draft_without_sent_by(tmp_path, monkeypatch)
        assert "Session-Id" not in delivery_msg, delivery_msg

    def test_unresolved_sender_is_reported_on_the_envelope(self, tmp_path, monkeypatch):
        """An un-nameable sender is DELIVERED, and SAID.

        The sentinel is write-only otherwise -- it reaches the delivered
        memo and the ledger row, and nothing reads either back, which is how
        67 unattributed memos shipped over three days in 2026-08 before the
        receiving repo reported it. The envelope flag is what makes the next
        one cost a line at send time.
        """
        for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "unattributed-topic", sent_by="")

        result = _memo_send(
            {"dry_run": False, "topic": "unattributed-topic"}, repo_root=sender_repo
        )

        assert result["exit_code"] == 0, result
        assert result["acted"][0]["sender_unattributed"] is True

    def test_named_sender_is_not_flagged_unattributed(self, tmp_path, monkeypatch):
        """The flag is not always-on: a resolved sender clears it."""
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "6f1e0c9a-1111-4222-8333-444455556666")
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "attributed-topic", sent_by="")

        result = _memo_send(
            {"dry_run": False, "topic": "attributed-topic"}, repo_root=sender_repo
        )

        assert result["exit_code"] == 0, result
        assert result["acted"][0]["sender_unattributed"] is False

    def test_sent_by_unresolvable_session_uses_the_sentinel(self, tmp_path, monkeypatch):
        """The sentinel is the resolution-FAILURE case, never the ordinary one:
        an un-nameable sender says so in the field, is never silently omitted,
        and never fails the delivery."""
        for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)
        row, _ = self._send_draft_without_sent_by(tmp_path, monkeypatch)
        assert row["sent_by"] == "unresolved"


# ---------------------------------------------------------------------------
# AC3: receiver-side commit runs no hook, exactly one spawn
# ---------------------------------------------------------------------------

_CANARY_HOOKS = ("pre-commit", "post-commit", "pre-push")


def _install_hook_canary(repo: Path) -> Path:
    witness_dir = repo.parent / "hook-witness"
    witness_dir.mkdir(exist_ok=True)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for hook in _CANARY_HOOKS:
        script = hooks_dir / hook
        script.write_text(
            f'#!/bin/sh\ntouch "{witness_dir.as_posix()}/{hook}"\nexit 0\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
    return witness_dir


class TestNoReceiverHooksFire:
    def test_failed_sender_receipt_carries_its_reason(self, tmp_path, monkeypatch):
        """A failed sender-side receipt must carry the git stderr on the result.

        Regression, 2026-08-25. The pathspec cause fixed the same day
        (569e39e1b) was diagnosed only after TWO real sends failed identically,
        because the op logged the deciding stderr to a WARNING the engine does
        not retain and the CLI printed a generic line. The very next real send
        hit a DIFFERENT cause and the reason was again unavailable -- the
        diagnostic gap outlived the bug it hid.

        `sender_commit_stderr` is present ONLY on the failure path, so a
        successful send stays exactly as it was.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "receipt-fails", track=False)

        def _failing_commit_paths(worktree, *a, **kw):
            # Fail ONLY the sender-side receipt. The receiver-side delivery
            # commit goes through a different function entirely
            # (`commit_authored_new_file`), which is the ordering this op
            # guarantees, so this needs no worktree discrimination.
            # `commit_paths` signals failure by RAISING `CommitRefused`, not
            # by returning a falsy outcome, so the simulation raises.
            raise memo_send_module.CommitRefused("fatal: simulated receipt failure")

        monkeypatch.setattr(memo_send_module, "commit_paths", _failing_commit_paths)

        result = _memo_send(
            {"dry_run": False, "topic": "receipt-fails"}, repo_root=sender_repo
        )
        acted = result["acted"][0]

        assert acted["sender_committed"] is False, acted
        assert acted["sender_commit_stderr"] == "fatal: simulated receipt failure", (
            "a failed receipt must carry the deciding stderr out of the op -- "
            "without it the operator sees only the CLI's generic line and "
            "cannot tell which failure they hit"
        )

    def test_sender_receipt_lands_when_the_draft_was_never_committed(
        self, tmp_path, monkeypatch
    ):
        """An UNTRACKED outbox draft must still produce a sender-side receipt.

        Regression, 2026-08-25. Every other test here stages the draft through
        `_write_draft`, which COMMITS it -- so the outbox path was always known
        to git and its deletion always stageable. The real canonical workflow
        does not do that: `cross-repo-memo draft <topic>` leaves the draft
        untracked, and `send` follows immediately.

        In that case the op passed the outbox path to `commit_scoped` anyway,
        to stage the move's deletion leg. After the move the path is gone from
        disk AND unknown to git, so git failed the WHOLE commit with
        "pathspec ... did not match any file(s) known to git": the delivery
        landed in the receiver's tree, and the sender's sent/ copy and ledger
        row were left staged-but-uncommitted. It fired on the first two real
        sends ever made through the rebuilt channel.

        The fixture's own comment asserted the opposite -- "a staged outbox
        draft is tracked at rest" -- which is why twenty passing tests never
        saw it. This test exists to keep that assumption from being re-encoded.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "untracked-topic", track=False)

        # Precondition: the draft really is unknown to git.
        tracked = _git(sender_repo, "ls-files", "--", "state/memo-outbox/untracked-topic.md")
        assert tracked.stdout.decode("utf-8").strip() == ""

        result = _memo_send(
            {"dry_run": False, "topic": "untracked-topic"}, repo_root=sender_repo
        )

        assert result["exit_code"] == 0, result
        acted = result["acted"][0]
        assert acted["committed"] is True
        assert acted["sender_committed"] is True, (
            "the sender-side receipt must land even though the draft was never "
            "committed -- this is the bug this test exists for"
        )

        # The receipt is COMMITTED, not merely staged -- and HEAD is the proof,
        # not a clean tree. `commit_scoped` commits through a private index and
        # never refreshes the shared one (see the note in
        # `test_happy_path_three_writes`), so the committed paths still read as
        # staged-deleted afterwards. That is the design, not a failed commit.
        sent_path = sender_repo / ".coordinator-local" / "memo-outbox" / "sent" / "untracked-topic.md"
        assert sent_path.exists()
        _git(sender_repo, "cat-file", "-e", "HEAD:.coordinator-local/memo-outbox/sent/untracked-topic.md")
        _git(sender_repo, "cat-file", "-e", "HEAD:.coordinator-local/memo-outbox/sent-ledger.jsonl")
        assert not (sender_repo / "state" / "memo-outbox" / "untracked-topic.md").exists()

    def test_receiver_side_hooks_never_fire(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        witness = _install_hook_canary(receiver_repo)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "no-hooks-topic")

        result = _memo_send({"dry_run": False, "topic": "no-hooks-topic"}, repo_root=sender_repo)

        assert result["exit_code"] == 0, result
        fired = sorted(p.name for p in witness.iterdir())
        assert fired == [], f"receiver hooks fired: {fired}"


# ---------------------------------------------------------------------------
# AC4: fail loud, never fall back to a spawning commit
# ---------------------------------------------------------------------------

class TestReceiverCommitDeclineFailsLoud:
    def test_declined_commit_fails_loud_no_sender_receipt(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "declined-topic")

        def _fake_decline(*args, **kwargs):
            return git_native.GitResult(returncode=1, stdout="", stderr="synthetic decline")

        monkeypatch.setattr(
            memo_send_module.git_native, "commit_authored_new_file", _fake_decline,
        )

        result = _memo_send({"dry_run": False, "topic": "declined-topic"}, repo_root=sender_repo)

        assert result["exit_code"] != 0
        assert result["acted"] == []
        assert len(result["failed"]) == 1
        # The receiver's tree is left EXACTLY as it was found: the worktree
        # write precedes the commit attempt, so a declined commit rolls its own
        # write back rather than orphaning an uncommitted memo in a repo we do
        # not own. Nothing sender-side is touched either.
        #
        # This asserts the opposite of what it once did. Leaving the file was a
        # dead end, not untidiness: the orphan is materially delivered while
        # every ledger says it is not, and AC6's no-clobber guard then refuses
        # the retry against the failed attempt's own file -- leaving hand-
        # editing a foreign tree as the only way out. Rolling back is not the
        # spawning-commit fallback AC4 forbids; nothing is committed either way.
        inbox_files = [
            p for p in (receiver_repo / "cross-repo" / "inbox").glob("*.md")
            if p.name != ".gitkeep"
        ]
        assert inbox_files == []
        assert (sender_repo / "state" / "memo-outbox" / "declined-topic.md").exists()
        assert not (sender_repo / ".coordinator-local" / "memo-outbox" / "sent").exists()
        ledger_path = sender_repo / ".coordinator-local" / "memo-outbox" / _SENT_LEDGER_FILENAME
        assert not ledger_path.exists()


    def test_retry_after_declined_commit_is_not_blocked_by_no_clobber(
        self, tmp_path, monkeypatch
    ):
        """The regression that made the rollback necessary.

        A declined commit used to leave its own file in the receiver's inbox,
        so the identical re-run was refused by the no-clobber guard -- a memo
        that could be neither finished nor withdrawn through this op. The retry
        must reach the commit leg again.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "retry-topic")

        def _fake_decline(*args, **kwargs):
            return git_native.GitResult(returncode=1, stdout="", stderr="synthetic decline")

        monkeypatch.setattr(
            memo_send_module.git_native, "commit_authored_new_file", _fake_decline,
        )
        first = _memo_send({"dry_run": False, "topic": "retry-topic"}, repo_root=sender_repo)
        assert first["exit_code"] != 0

        second = _memo_send({"dry_run": False, "topic": "retry-topic"}, repo_root=sender_repo)
        assert "no clobber" not in json.dumps(second["failed"]), second["failed"]


# ---------------------------------------------------------------------------
# AC6: collision refused on both legs, independently
# ---------------------------------------------------------------------------

class TestCollisionRefused:
    def test_precheck_refuses_existing_file(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "collision-topic")

        # Compute the exact target filename the same way the handler does,
        # then pre-seed a collision at it.
        from coordinator_core.ops.fleet._memo_compose import _memo_filename
        import datetime as _dt
        filename = _memo_filename(_dt.date.today().isoformat(), "claude-klabauter-engine", "collision-topic")
        target = receiver_repo / "cross-repo" / "inbox" / filename
        target.write_text("already here\n", encoding="utf-8")

        result = _memo_send({"dry_run": False, "topic": "collision-topic"}, repo_root=sender_repo)

        assert result["exit_code"] != 0
        assert result["acted"] == []
        assert len(result["failed"]) == 1
        assert "collision" in result["failed"][0]["reason"]
        assert target.read_text(encoding="utf-8") == "already here\n"

    def test_o_excl_refuses_a_race_the_precheck_misses(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "race-topic")

        from coordinator_core.ops.fleet._memo_compose import _memo_filename
        import datetime as _dt
        filename = _memo_filename(_dt.date.today().isoformat(), "claude-klabauter-engine", "race-topic")
        target = receiver_repo / "cross-repo" / "inbox" / filename

        real_exists = Path.exists

        def _fake_exists(self):
            if self == target:
                return False  # lie: precheck sees no collision
            return real_exists(self)

        # Seed the real file AFTER the (faked) precheck would have passed —
        # simulates the race O_EXCL is the second, independent leg for.
        target.write_text("raced in\n", encoding="utf-8")
        monkeypatch.setattr(Path, "exists", _fake_exists)

        result = _memo_send({"dry_run": False, "topic": "race-topic"}, repo_root=sender_repo)

        assert result["exit_code"] != 0
        assert result["acted"] == []
        assert len(result["failed"]) == 1
        assert "race" in result["failed"][0]["reason"]
        assert target.read_text(encoding="utf-8") == "raced in\n"


# ---------------------------------------------------------------------------
# AC8: reachable through the door
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_registered_in_registry_map(self):
        from coordinator_core.ops._registry_map import OP_MODULE_MAP
        assert OP_MODULE_MAP["memo.send"] == "coordinator_core.ops.fleet.memo_send"

    def test_registered_in_ipc_registry_after_import(self):
        import coordinator_core.ops.fleet.memo_send  # noqa: F401
        from coordinator_core.ipc import _REGISTRY
        assert "memo.send" in _REGISTRY
        assert _REGISTRY["memo.send"] is memo_send_module._memo_send

    def test_mutates_declaration_matches_deleted_originals_contract(self):
        assert memo_send_module.MUTATES == [
            ".coordinator-local/memo-outbox/sent-ledger.jsonl",
            "state/memo-outbox/*.md",
            "cross-repo/inbox/*.md",
            "state/cross-repo/inbox/*.md",
        ]


# ---------------------------------------------------------------------------
# The sent-ledger is fleet-shared and append-only: HEAD must gain the peer's
# rows, never replay a stale staged snapshot
# ---------------------------------------------------------------------------

class TestSharedLedgerCommitsTheWorktreeUnion:
    def test_a_stale_staged_ledger_blob_is_not_committed_over_the_worktree(
        self, tmp_path, monkeypatch
    ):
        """A stale INDEX entry for the sent-ledger must not decide what lands.

        `state/memo-outbox/sent-ledger.jsonl` is fleet-shared -- every
        sending session on this box appends its own row to the one file
        (a bounded ring, see `TestLedgerIsABoundedRing`), under
        `locked_rmw`'s cross-process lock, so the WORKTREE copy is always
        the union of every completed append. An index entry for it is
        the opposite: it freezes at whatever a peer staged and goes stale the
        instant anybody else appends.

        `commit_scoped`, which this op used until 2026-08-30, reads a staged
        blob DIFFERING FROM HEAD as a deliberate partial stage and preserves
        it. That is correct for a hand-staged hunk and wrong for this file:
        measured on the live tree that day, ten consecutive memo.send commits
        landed the byte-identical stale blob b2a5f7d9d (2376 rows) while every
        one of their parents held a richer one, so HEAD lost each row appended
        in between -- the sender's own included, since its append never reached
        the commit either.

        Asserting on HEAD's bytes rather than on which function was called is
        deliberate -- the defect is a content outcome, and a call-shape
        assertion would pass against any future route that reintroduced it.
        """
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "ledger-union", track=False)

        ledger = sender_repo / ".coordinator-local" / "memo-outbox" / _SENT_LEDGER_FILENAME
        ledger.parent.mkdir(parents=True, exist_ok=True)

        def _rows(*topics: str) -> str:
            return "".join(
                json.dumps({"topic": t, "to": "peer-em"}) + "\n" for t in topics
            )

        # HEAD carries two rows...
        ledger.write_text(_rows("first", "second"), encoding="utf-8", newline="\n")
        _git(sender_repo, "add", ".coordinator-local/memo-outbox/sent-ledger.jsonl")
        _git(sender_repo, "commit", "-m", "peer: two ledger rows")

        # ...and the INDEX is left pinned to an OLDER one-row blob. This is the
        # live shape, and the discriminator matters: `commit_scoped` reads a
        # staged blob that differs from HEAD as a DELIBERATE partial stage and
        # preserves it, which is right for a hand-staged hunk and catastrophic
        # for a shared append-only ledger. An index that merely differs from
        # the WORKTREE is an ordinary unstaged edit and takes the safe branch,
        # which is why a naive reproduction of this bug passes against the
        # broken code.
        ledger.write_text(_rows("first"), encoding="utf-8", newline="\n")
        _git(sender_repo, "add", ".coordinator-local/memo-outbox/sent-ledger.jsonl")

        # A concurrent peer appends to the worktree, as `locked_rmw` does --
        # so the worktree, and only the worktree, holds the union.
        ledger.write_text(
            _rows("first", "second", "peer-appended-meanwhile"),
            encoding="utf-8", newline="\n",
        )

        staged_blob = subprocess.run(
            ["git", "ls-files", "-s", ".coordinator-local/memo-outbox/sent-ledger.jsonl"],
            cwd=str(sender_repo), capture_output=True, text=True, check=True,
            **no_console_creationflags(),
        ).stdout.split()[1]

        result = _memo_send(
            {"dry_run": False, "topic": "ledger-union"}, repo_root=sender_repo
        )
        assert result["acted"][0]["sender_committed"] is True, result

        head_ledger = subprocess.run(
            ["git", "show", "HEAD:.coordinator-local/memo-outbox/sent-ledger.jsonl"],
            cwd=str(sender_repo), capture_output=True, text=True, check=True,
            **no_console_creationflags(),
        ).stdout
        head_topics = [
            json.loads(line)["topic"]
            for line in head_ledger.splitlines() if line.strip()
        ]

        assert "peer-appended-meanwhile" in head_topics, (
            "the peer's appended row was dropped from HEAD -- the commit took "
            f"the stale staged blob {staged_blob[:9]} instead of the worktree "
            "union, which is the arm that destroyed rows on the live tree"
        )
        assert "second" in head_topics, head_topics
        assert "ledger-union" in head_topics, (
            "this send's own row is missing -- the append landed in the "
            f"worktree but never reached HEAD: {head_topics}"
        )


class TestLedgerIsABoundedRing:
    """The sent-ledger retains `_SENT_LEDGER_MAX_ROWS` rows, newest wins."""

    def _send_with_ledger(self, tmp_path, monkeypatch, existing_rows: int):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "ring-topic")

        ledger = sender_repo / ".coordinator-local" / "memo-outbox" / _SENT_LEDGER_FILENAME
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            "".join(
                json.dumps({"topic": f"old-{i}", "to": "peer-em"}) + "\n"
                for i in range(existing_rows)
            ),
            encoding="utf-8",
            newline="\n",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "ring-topic"}, repo_root=sender_repo
        )
        assert result["exit_code"] == 0, result
        rows = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return rows

    def test_an_overfull_ledger_is_trimmed_to_the_cap(self, tmp_path, monkeypatch):
        """Oldest rows go, the cap holds, and this send's row is the newest.

        Asserted on WHICH rows survived, not just the count: a trim that
        kept the cap by dropping the newest rows would satisfy a length
        check and lose exactly the receipts anybody looks the file up for.
        """
        rows = self._send_with_ledger(
            tmp_path, monkeypatch, existing_rows=_SENT_LEDGER_MAX_ROWS + 40
        )

        assert len(rows) == _SENT_LEDGER_MAX_ROWS
        assert rows[-1]["topic"] == "ring-topic"
        # 40 surplus rows plus one evicted to make room for this send.
        assert rows[0]["topic"] == "old-41", rows[0]
        assert not any(r["topic"] == "old-0" for r in rows)

    def test_an_under_cap_ledger_keeps_every_row(self, tmp_path, monkeypatch):
        """Below the cap the ring evicts NOTHING -- the bound is a ceiling,
        not a target to shrink toward."""
        rows = self._send_with_ledger(tmp_path, monkeypatch, existing_rows=3)

        assert len(rows) == 4
        assert [r["topic"] for r in rows[:3]] == ["old-0", "old-1", "old-2"]
        assert rows[-1]["topic"] == "ring-topic"

    def test_a_missing_trailing_newline_does_not_glue_two_rows(
        self, tmp_path, monkeypatch
    ):
        """A truncated write or hand edit leaves no trailing newline; the
        next append must not land on the same line and corrupt both rows."""
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "no-newline-topic")

        ledger = sender_repo / ".coordinator-local" / "memo-outbox" / _SENT_LEDGER_FILENAME
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps({"topic": "truncated-row", "to": "peer-em"}),
            encoding="utf-8",
            newline="\n",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "no-newline-topic"}, repo_root=sender_repo
        )
        assert result["exit_code"] == 0, result

        lines = [
            line
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 2, lines
        assert [json.loads(line)["topic"] for line in lines] == [
            "truncated-row",
            "no-newline-topic",
        ]


class TestLedgerIsBoundedInTimeToo:
    """The row cap alone leaves a RARE sender's ledger unbounded in time.

    claude-klabauter and DoE-claude send constantly, so 250 rows is ~2.5 days here and
    the row cap is the bound that bites. Every other repo sends a handful a
    month, where 250 rows is a year or more -- these tests are that repo's,
    not ours.
    """

    def _send_over(self, tmp_path, monkeypatch, rows):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "age-topic")

        ledger = sender_repo / ".coordinator-local" / "memo-outbox" / _SENT_LEDGER_FILENAME
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            "".join(json.dumps(r) + "\n" for r in rows),
            encoding="utf-8",
            newline="\n",
        )

        result = _memo_send(
            {"dry_run": False, "topic": "age-topic"}, repo_root=sender_repo
        )
        assert result["exit_code"] == 0, result
        return [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _stamp(days_ago):
        moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=days_ago
        )
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_a_quiet_repos_stale_rows_age_out_well_under_the_row_cap(
        self, tmp_path, monkeypatch
    ):
        """Three rows, nowhere near 250, and the old ones still go."""
        rows = self._send_over(
            tmp_path,
            monkeypatch,
            [
                {"topic": "last-year", "sent_at": self._stamp(400)},
                {"topic": "last-quarter", "sent_at": self._stamp(90)},
                {"topic": "just-inside", "sent_at": self._stamp(_SENT_LEDGER_MAX_AGE_DAYS - 1)},
            ],
        )

        assert [r["topic"] for r in rows] == ["just-inside", "age-topic"], rows

    def test_a_row_that_cannot_prove_its_age_is_kept(self, tmp_path, monkeypatch):
        """Rows predating `sent_at`, and any hand-written line, are the
        OLDEST rows in a real ledger. Evicting them for failing to prove
        their age would empty the file on the first append after this
        shipped -- the row cap is what reaches them."""
        rows = self._send_over(
            tmp_path,
            monkeypatch,
            [
                {"topic": "no-sent-at-field", "to": "peer-em"},
                {"topic": "unparseable-stamp", "sent_at": "last Tuesday"},
                {"topic": "ancient", "sent_at": self._stamp(400)},
            ],
        )

        assert [r["topic"] for r in rows] == [
            "no-sent-at-field",
            "unparseable-stamp",
            "age-topic",
        ], rows


class TestIndexLockOnReceiptIsNotAFailedSend:
    """A peer holding `.git/index.lock` must not turn a DELIVERED memo into an
    internal error (2026-09-01).

    `commit_paths` raises `IndexWriteLockBusy` when a peer holds the index --
    routine, not exotic, at the ~50-session load norm this repo sizes for. It
    was absent from the sender-receipt `except` tuple, so it escaped `_memo_send`
    entirely and surfaced at the JSON-RPC boundary as a bare
    `-32603 Internal error: IndexWriteLockBusy`.

    By that line the receiver commit, the delivered file and the ledger row have
    all landed. So the caller reads total failure for a send that fully
    succeeded, and the honest response to an internal error -- retry -- DOUBLE
    DELIVERS. Example-retrieval-repo-em reported exactly this shape.

    The module's ordering guarantee already names the correct outcome: "an
    uncredited delivery is merely untidy". This asserts the op reports it that
    way instead of raising.
    """

    def test_index_lock_busy_reports_uncommitted_receipt_not_an_exception(
        self, tmp_path, monkeypatch
    ):
        from coordinator_core.git.index_write import IndexWriteLockBusy

        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "peer-holds-index", track=False)

        def _locked_commit_paths(worktree, *a, **kw):
            raise IndexWriteLockBusy(
                f"{worktree}/.git/index.lock exists -- a peer holds the index"
            )

        monkeypatch.setattr(memo_send_module, "commit_paths", _locked_commit_paths)

        # The contract under test is "returns an envelope", so an escaping
        # exception fails the test by propagating -- no pytest.raises here.
        result = _memo_send(
            {"dry_run": False, "topic": "peer-holds-index"}, repo_root=sender_repo
        )

        acted = result["acted"][0]
        assert acted["written"] is True
        assert acted["committed"] is True, (
            "the DELIVERY landed -- that is what makes a retry a double-delivery"
        )
        assert acted["sender_committed"] is False
        assert "index.lock" in acted["sender_commit_stderr"], (
            "the deciding reason must reach the caller, not the engine's log"
        )
        assert result["failed"] == [], "a delivered memo is not a failed item"

    def test_delivered_memo_is_actually_on_disk_in_the_receiver_inbox(
        self, tmp_path, monkeypatch
    ):
        """The claim above rests on the delivery having really landed, so it is
        asserted against the receiver's tree rather than the envelope alone."""
        from coordinator_core.git.index_write import IndexWriteLockBusy

        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "peer-holds-index-2", track=False)

        monkeypatch.setattr(
            memo_send_module,
            "commit_paths",
            lambda worktree, *a, **kw: (_ for _ in ()).throw(
                IndexWriteLockBusy("index.lock exists")
            ),
        )

        _memo_send(
            {"dry_run": False, "topic": "peer-holds-index-2"}, repo_root=sender_repo
        )

        inbox = receiver_repo / "cross-repo" / "inbox"
        delivered = list(inbox.glob("*peer-holds-index-2*"))
        assert delivered, (
            f"the memo must be in the receiver's inbox: {list(inbox.iterdir())}"
        )
