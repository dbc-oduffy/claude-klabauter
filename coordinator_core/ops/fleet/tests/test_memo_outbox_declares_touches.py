"""
Tests for `memo.send` and `memo.reconcile_outbox` declaring their written
paths through `ipc.py`'s `_SCOPE_TOUCH_PATHS_KEY` contract.

Both handlers write into `state/memo-outbox/` (sent/ copies, ledger rows,
and reconciled moves) without going through the commit-pipeline's normal
staged-write bookkeeping, so their outputs land unowned unless each handler
self-reports the paths it actually touched via `result["_scope_touch_paths"]`
(`ipc.py :: _SCOPE_TOUCH_PATHS_KEY`, consumed by
`cli_entry.py :: _record_self_reported_touches`).

Spec backlink: state/dispatch-briefs/2026-08-27-a-pathspec-is-not-a-scope/C8.md
  ("Check 5's own in-code comment already names memo.reconcile_outbox as one
  of these" — the census bucket this chunk closes).

Negative-spec:
  - Does NOT assert anything about `memo.send`'s receiver-inbox write —
    that path lives in a DIFFERENT repo (the registry-resolved receiver's
    tree), never this session's own scope, and is committed independently
    via `git_native.commit_authored_new_file`. Only the SENDER-side
    `sent/` copy and ledger append are this session's own writes.
  - Does NOT run on a dry_run call — dry_run performs no writes, so no
    `_scope_touch_paths` key is expected on that envelope.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_reconcile_outbox import _memo_reconcile_outbox
from coordinator_core.ops.fleet.memo_send import _SENT_LEDGER_FILENAME, _memo_send
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_OUTBOX = ("state", "memo-outbox")


# ---------------------------------------------------------------------------
# memo.send fixtures (mirrors test_memo_send.py's own factories)
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


def _write_draft(sender_repo: Path, topic: str, *, to: str = "example-retrieval-repo-em") -> Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    content = (
        "---\n"
        'title: "A test memo"\n'
        'from: "claude-klabauter-engine"\n'
        f'to: "{to}"\n'
        "created: 2026-08-25\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        'summary: "a one-line summary"\n'
        'kind: "fyi"\n'
        'sent_by: "d218a65c-2c5b-472e-879c-ae9ed1747030"\n'
        "---\n\nBody prose.\n"
    )
    draft_path.write_text(content, encoding="utf-8", newline="\n")
    _git(sender_repo, "add", "--", f"state/memo-outbox/{topic}.md")
    _git(sender_repo, "commit", "-m", f"stage draft {topic}")
    return draft_path


# ---------------------------------------------------------------------------
# memo.send
# ---------------------------------------------------------------------------

class TestMemoSendDeclaresTouches:
    def test_act_run_declares_sent_copy_and_ledger(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "declare-topic")

        result = _memo_send({"dry_run": False, "topic": "declare-topic"}, repo_root=sender_repo)

        assert result["exit_code"] == 0, result
        assert "_scope_touch_paths" in result
        touched = {Path(p) for p in result["_scope_touch_paths"]}

        sent_path = sender_repo / ".coordinator-local" / "memo-outbox" / "sent" / "declare-topic.md"
        ledger_path = sender_repo / ".coordinator-local" / "memo-outbox" / _SENT_LEDGER_FILENAME
        assert sent_path in touched
        assert ledger_path in touched
        # Never the receiver-side inbox path — a different repo's own write,
        # already committed independently via commit_authored_new_file.
        receiver_inbox = receiver_repo / "cross-repo" / "inbox"
        assert not any(receiver_inbox in p.parents for p in touched)

    def test_dry_run_declares_nothing(self, tmp_path, monkeypatch):
        sender_repo = _make_sender_git_repo(tmp_path)
        receiver_repo = _make_receiver_git_repo(tmp_path)
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_draft(sender_repo, "dry-declare-topic")

        result = _memo_send({"dry_run": True, "topic": "dry-declare-topic"}, repo_root=sender_repo)

        assert result["exit_code"] == 0, result
        assert "_scope_touch_paths" not in result


# ---------------------------------------------------------------------------
# memo.reconcile_outbox
# ---------------------------------------------------------------------------

@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _write_memo(root: Path, name: str, status: str) -> Path:
    outbox = root.joinpath(*_OUTBOX)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / name
    path.write_text(
        f'---\ntitle: "t"\nto: "example-retrieval-repo-em"\nstatus: {status}\n---\n\nbody\n',
        encoding="utf-8",
    )
    return path


class TestMemoReconcileOutboxDeclaresTouches:
    def test_act_run_declares_both_ends_of_each_move(self, worktree):
        _write_memo(worktree, "delivered.md", "sent")
        _write_memo(worktree, "live.md", "draft")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["exit_code"] == 0, result
        assert "_scope_touch_paths" in result
        touched = {Path(p) for p in result["_scope_touch_paths"]}

        source = worktree.joinpath(*_OUTBOX, "delivered.md")
        target = worktree / ".coordinator-local" / "memo-outbox" / "sent" / "delivered.md"
        assert source in touched, "the vacated source is a deletion this session owns too"
        assert target in touched
        # A draft that never moved contributes no touch entries.
        live = worktree.joinpath(*_OUTBOX, "live.md")
        assert live not in touched

    def test_no_op_run_declares_nothing(self, worktree):
        _write_memo(worktree, "live.md", "draft")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["exit_code"] == 0, result
        assert "_scope_touch_paths" not in result

    def test_dry_run_declares_nothing(self, worktree):
        _write_memo(worktree, "delivered.md", "sent")

        result = _memo_reconcile_outbox({"dry_run": True}, repo_root=worktree)

        assert result["exit_code"] == 0, result
        assert "_scope_touch_paths" not in result
