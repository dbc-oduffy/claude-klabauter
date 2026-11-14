"""memo.send refuses a target repo whose live EM runs on another box, before any write."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.fleet import _memo_owning_box as mob
from coordinator_core.ops.fleet import _memo_push_pending, memo_wire
from coordinator_core.ops.fleet._memo_anchor import anchor_names
from coordinator_core.ops.fleet.memo_send import _MODE, _memo_send
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


def _session(sessions: Path, name: str, cwd: Path, **fields) -> None:
    entry = {"sessionId": name, "name": name, "pid": os.getpid(), "cwd": str(cwd)}
    entry.update(fields)
    (sessions / f"{name}.json").write_text(json.dumps(entry), encoding="utf-8")


def _other_box_session(sessions: Path, name: str, cwd: Path) -> None:
    _session(
        sessions, name, cwd, unimatrixBox="otherbox", unimatrixRemote="otherbox",
        unimatrixFarSeen=int(time.time() * 1000),
    )


@pytest.fixture()
def sessions(tmp_path, monkeypatch) -> Path:
    """An isolated sessions dir the real resolver reads, with this box named `thisbox`."""
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(mob._contract(), "local_box", lambda: "thisbox")
    real = mob.resolve_owning_box
    monkeypatch.setattr(
        memo_wire, "resolve_owning_box", lambda repo: real(repo, sessions_dir=str(d))
    )
    return d


def _pending_records(sender: Path) -> list:
    return _memo_push_pending.list_pending(sender)


def _repo_state(repo: Path) -> tuple:
    common = resolve_git_common_dir(repo)
    refs = _git(repo, "for-each-ref").stdout
    return refs, anchor_names(common)


def _assert_untouched(sender: Path, receiver: Path, before: tuple, topic: str) -> None:
    assert _repo_state(receiver) == before
    assert _pending_records(sender) == []
    assert (sender / "state" / "memo-outbox" / f"{topic}.md").exists()
    assert "status: draft" in (sender / "state" / "memo-outbox" / f"{topic}.md").read_text(
        encoding="utf-8"
    )
    assert not (sender / ".coordinator-local" / "memo-outbox" / "sent").exists()


class TestNonOwningBoxRefused:
    def test_to_on_another_box_is_refused_and_writes_nothing(self, tmp_path, monkeypatch, sessions):
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _other_box_session(sessions, "rag-em-far", receiver)
        _write_draft(sender, "far-topic")
        before = _repo_state(receiver)

        result = _memo_send({"dry_run": False, "topic": "far-topic"}, repo_root=sender)

        assert result["exit_code"] == 1, result
        assert result["mode"] == _MODE
        assert result["reason"].startswith("memo.send: NOT THE OWNING BOX")
        assert "otherbox" in result["reason"] and "rag-em-far" in result["reason"]
        assert result.get("acted", []) == []
        _assert_untouched(sender, receiver, before, "far-topic")

    def test_dry_run_surfaces_the_same_refusal(self, tmp_path, monkeypatch, sessions):
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _other_box_session(sessions, "rag-em-far", receiver)
        _write_draft(sender, "far-dry")

        result = _memo_send({"dry_run": True, "topic": "far-dry"}, repo_root=sender)

        assert result["exit_code"] == 1, result
        assert result["reason"].startswith("memo.send: NOT THE OWNING BOX")

    def test_cc_on_another_box_refuses_the_whole_send(self, tmp_path, monkeypatch, sessions):
        sender = _make_sender_git_repo(tmp_path)
        to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
        cc_repo = _make_receiver_git_repo(tmp_path, name="cc-repo")
        monkeypatch.setenv(
            "CLAUDE_HOME",
            str(_make_claude_home(tmp_path, {"project_rag": to_repo, "example_cockpit_repo": cc_repo})),
        )
        _other_box_session(sessions, "cockpit-em-far", cc_repo)
        _write_draft(sender, "far-cc", to="example-retrieval-repo-em", cc="example-cockpit-repo-em")
        to_before, cc_before = _repo_state(to_repo), _repo_state(cc_repo)

        result = _memo_send({"dry_run": False, "topic": "far-cc"}, repo_root=sender)

        assert result["exit_code"] == 1, result
        assert "NOT THE OWNING BOX" in result["reason"]
        assert "otherbox" in result["reason"]
        assert _repo_state(to_repo) == to_before
        assert _repo_state(cc_repo) == cc_before
        _assert_untouched(sender, to_repo, to_before, "far-cc")


class TestOlderPeerContractRow:
    def test_row_lacking_this_box_still_refuses_an_other_box_em(self, tmp_path, monkeypatch, sessions):
        contract = mob._contract()
        real_row = contract.agent_row
        monkeypatch.setattr(
            contract, "agent_row",
            lambda entry, local: {k: v for k, v in real_row(entry, local).items() if k != "this_box"},
        )
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _other_box_session(sessions, "rag-em-far", receiver)
        _write_draft(sender, "old-contract")

        result = _memo_send({"dry_run": False, "topic": "old-contract"}, repo_root=sender)

        assert result["exit_code"] == 1, result
        assert result["reason"].startswith("memo.send: NOT THE OWNING BOX")

    def test_row_lacking_this_box_and_box_delivers_without_raising(self, tmp_path, monkeypatch, sessions):
        monkeypatch.setattr(mob._contract(), "agent_row", lambda entry, local: {"name": "x"})
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _other_box_session(sessions, "rag-em-far", receiver)
        _write_draft(sender, "bad-row")

        assert mob.resolve_owning_box(receiver, sessions_dir=str(sessions)).state == "unknown"
        result = _memo_send({"dry_run": False, "topic": "bad-row"}, repo_root=sender)

        assert result["exit_code"] == 0, result


class TestOwnedHereOrUnknownStillDelivers:
    def test_live_em_on_this_box_delivers(self, tmp_path, monkeypatch, sessions):
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _session(sessions, "rag-em-here", receiver)
        _other_box_session(sessions, "rag-em-far", receiver)
        _write_draft(sender, "here-topic")

        result = _memo_send({"dry_run": False, "topic": "here-topic"}, repo_root=sender)

        assert result["exit_code"] == 0, result
        assert result["acted"][0]["committed"] is True

    def test_no_live_em_anywhere_delivers(self, tmp_path, monkeypatch, sessions):
        sender = _make_sender_git_repo(tmp_path)
        receiver = _make_receiver_git_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
        _write_draft(sender, "unknown-topic")

        result = _memo_send({"dry_run": False, "topic": "unknown-topic"}, repo_root=sender)

        assert result["exit_code"] == 0, result
