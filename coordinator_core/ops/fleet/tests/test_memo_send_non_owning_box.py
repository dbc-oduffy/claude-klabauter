"""memo.send delivers into this box's clone of the receiver repo, whatever box its EM was last live on."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.fleet import _memo_push_pending
from coordinator_core.ops.fleet._memo_anchor import anchor_names
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, check=True, **no_console_creationflags()
    )


def _init_repo(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    return root


def _make_sender_git_repo(tmp_path: Path) -> Path:
    root = _init_repo(tmp_path / "sender-repo")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _make_receiver_git_repo(tmp_path: Path, name: str = "receiver-repo") -> Path:
    root = _init_repo(tmp_path / name)
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init receiver")
    bare = tmp_path / f"{name}-origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")
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
    (machine_local / "registry.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return claude_home


def _write_draft(sender_repo: Path, topic: str, *, to: str = "example-retrieval-repo-em", cc: str | None = None) -> Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    cc_line = f'cc: "{cc}"\n' if cc else ""
    draft_path.write_text(
        "---\n"
        'title: "A test memo"\n'
        'from: "claude-klabauter-engine"\n'
        f'to: "{to}"\n'
        f"{cc_line}"
        "created: 2026-08-25\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        'summary: "a one-line summary"\n'
        'kind: "fyi"\n'
        'sent_by: "d218a65c-2c5b-472e-879c-ae9ed1747030"\n'
        "---\n\n"
        "Body prose.\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(sender_repo, "add", "--", f"state/memo-outbox/{topic}.md")
    _git(sender_repo, "commit", "-m", f"stage draft {topic}")
    return draft_path


def _live_on_another_box(home: Path, name: str, cwd: Path) -> None:
    """A Unimatrix session entry for `cwd`'s EM on `otherbox`, where a per-EM box lookup would read it."""
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    entry = {
        "sessionId": name, "name": name, "pid": os.getpid(), "cwd": str(cwd),
        "unimatrixBox": "otherbox", "unimatrixRemote": "otherbox",
        "unimatrixFarSeen": int(time.time() * 1000),
    }
    (sessions / f"{name}.json").write_text(json.dumps(entry), encoding="utf-8")


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "fake-home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


def _delivered(receiver: Path) -> list:
    """Delivered memo names, read from the anchors: a delivery never writes the receiver's worktree."""
    return sorted(name for name, _commit, _blob in anchor_names(resolve_git_common_dir(receiver)))


class TestSendDeliversLocallyWhateverBoxTheEmIsOn:
    def test_to_whose_em_is_on_another_box_commits_into_this_clone(self, tmp_path, monkeypatch, home):
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _live_on_another_box(home, "rag-em-far", receiver)
        _write_draft(sender, "far-topic")

        result = _memo_send({"dry_run": False, "topic": "far-topic"}, repo_root=sender)

        assert result["exit_code"] == 0, result
        assert result["acted"][0]["committed"] is True
        assert len(_delivered(receiver)) == 1
        assert len(anchor_names(resolve_git_common_dir(receiver))) == 1
        assert len(_memo_push_pending.list_pending(sender)) == 1

    def test_dry_run_previews_without_refusing(self, tmp_path, monkeypatch, home):
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _live_on_another_box(home, "rag-em-far", receiver)
        _write_draft(sender, "far-dry")

        result = _memo_send({"dry_run": True, "topic": "far-dry"}, repo_root=sender)

        assert result["exit_code"] == 0, result
        assert _delivered(receiver) == []
        assert _memo_push_pending.list_pending(sender) == []

    def test_cc_leg_to_a_repo_whose_em_is_on_another_box_delivers_too(self, tmp_path, monkeypatch, home):
        sender = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        cc_repo = _make_receiver_git_repo(tmp_path, name="cc-repo")
        monkeypatch.setenv(
            "CLAUDE_HOME",
            str(_make_claude_home(tmp_path, {"project_rag": to_repo, "example_cockpit_repo": cc_repo})),
        )
        _live_on_another_box(home, "cockpit-em-far", cc_repo)
        _live_on_another_box(home, "rag-em-far", to_repo)
        _write_draft(sender, "far-cc", to="example-retrieval-repo-em", cc="example-cockpit-repo-em")

        result = _memo_send({"dry_run": False, "topic": "far-cc"}, repo_root=sender)

        assert result["exit_code"] == 0, result
        assert len(_delivered(to_repo)) == 1
        assert len(_delivered(cc_repo)) == 1
        assert len(_memo_push_pending.list_pending(sender)) >= 1

    def test_no_live_em_anywhere_delivers(self, tmp_path, monkeypatch, home):
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _write_draft(sender, "unknown-topic")

        result = _memo_send({"dry_run": False, "topic": "unknown-topic"}, repo_root=sender)

        assert result["exit_code"] == 0, result


class TestUnregisteredReceiverStillRefused:
    def test_no_clone_on_this_box_is_refused_and_writes_nothing(self, tmp_path, monkeypatch, home):
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _write_draft(sender, "nowhere", to="example-cockpit-repo-em")

        result = _memo_send({"dry_run": False, "topic": "nowhere"}, repo_root=sender)

        assert result["exit_code"] == 1, result
        assert "UNKNOWN RECEIVER" in result["reason"]
        assert _delivered(receiver) == []
        assert _memo_push_pending.list_pending(sender) == []
