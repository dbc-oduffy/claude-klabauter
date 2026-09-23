"""
test_memo_never_inbox_mirror — coordinator-claude / claude-klabauter never
get a cross-repo inbox, on any machine, regardless of registry state (PM
ruling 2026-09-23).

Live hole this closes: a `to: coordinator-claude-em` outbox item with
`delivery_mode: receiver-repo` got written+committed into a coordinator-claude
clone's `cross-repo/inbox/` and the local draft got marked `status: sent`,
because `_resolve_receiver_inbox`'s mirror-vs-ordinary-receiver classification
depends on `publish.mirrors.<key>.path`/`.owner` being declared on the SENDING
machine's registry — on a machine where that declaration is absent/partial,
the receiver resolves as an ordinary `repos.*` receiver instead.

This test registers each mirror as an ORDINARY `repos.*` receiver (the exact
under-registered shape that produced the live hole) so that, absent the
`never_inbox_mirror_refusal` chokepoint, delivery WOULD succeed — proving the
refusal fires independent of registry state, not merely when the mirror is
correctly classified.

Covers the `to:` leg (`_memo_send`) and the `cc:` leg (`_resolve_cc_targets`,
via `_memo_send`) — the two engine/"relay" delivery legs `memo.send` owns.
The CLI (`cross-repo-memo send`) is a bare forwarder onto this same op (see
`coordinator/bin/tests/test_cross_repo_memo_never_inbox_mirror.py`).

Run: python -m pytest coordinator_core/ops/fleet/tests/test_memo_never_inbox_mirror.py -p no:cacheprovider -q
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.fleet.memo_send import _MODE, _memo_send

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


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


def _make_mirror_git_repo(tmp_path: Path, name: str) -> Path:
    """A receiver repo shaped exactly like the mirror clone — its OWN
    `cross-repo/inbox/` exists, ready to receive a write if the chokepoint
    did not fire."""
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
    """Registers each mirror as an ORDINARY `repos.*` receiver — the
    under-registered shape (no `publish.mirrors.*` declaration at all) that
    produced the live hole. No `publish.mirrors.*` table is written here on
    purpose."""
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
    sender_repo: Path, topic: str, *, to: str, cc: str | None = None,
) -> Path:
    outbox = sender_repo / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    draft_path = outbox / f"{topic}.md"
    cc_line = f'cc: "{cc}"\n' if cc else ""
    content = (
        "---\n"
        'title: "A test memo"\n'
        'from: "example-retrieval-repo-em"\n'
        f'to: "{to}"\n'
        f"{cc_line}"
        "created: 2026-09-23\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        'summary: "a one-line summary"\n'
        'kind: "fyi"\n'
        'sent_by: "d218a65c-2c5b-472e-879c-ae9ed1747030"\n'
        "---\n\n"
        "Body prose.\n"
    )
    draft_path.write_text(content, encoding="utf-8", newline="\n")
    _git(sender_repo, "add", "--", f"state/memo-outbox/{topic}.md")
    _git(sender_repo, "commit", "-m", f"stage draft {topic}")
    return draft_path


@pytest.mark.parametrize(
    "receiver_key, receiver_dir, to_id",
    [
        ("coordinator_claude", "coordinator-claude", "coordinator-claude-em"),
        ("claude_klabauter", "claude-klabauter", "claude-klabauter-em"),
    ],
)
def test_to_leg_refuses_mirror_regardless_of_registry_state(
    tmp_path, monkeypatch, receiver_key, receiver_dir, to_id,
):
    sender_repo = _make_sender_git_repo(tmp_path)
    mirror_repo = _make_mirror_git_repo(tmp_path, receiver_dir)
    claude_home = _make_claude_home(tmp_path, {receiver_key: mirror_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    draft_path = _write_draft(sender_repo, "topic-to-mirror", to=to_id)

    result = _memo_send({"dry_run": False, "topic": "topic-to-mirror"}, repo_root=sender_repo)

    assert result["exit_code"] == 1
    assert result["mode"] == _MODE
    assert result.get("acted") in (None, [])

    # No byte moved into the mirror's inbox.
    inbox_entries = list((mirror_repo / "cross-repo" / "inbox").glob("*.md"))
    assert inbox_entries == []

    # Outbox item stays unsent.
    assert draft_path.exists()
    assert "status: draft" in draft_path.read_text(encoding="utf-8")
    sent_copy = sender_repo / ".coordinator-local" / "memo-outbox" / "sent" / "topic-to-mirror.md"
    assert not sent_copy.exists()


@pytest.mark.parametrize(
    "receiver_key, receiver_dir, cc_id",
    [
        ("coordinator_claude", "coordinator-claude", "coordinator-claude-em"),
        ("claude_klabauter", "claude-klabauter", "claude-klabauter-em"),
    ],
)
def test_cc_leg_refuses_mirror_regardless_of_registry_state(
    tmp_path, monkeypatch, receiver_key, receiver_dir, cc_id,
):
    sender_repo = _make_sender_git_repo(tmp_path)
    real_receiver = _make_mirror_git_repo(tmp_path, "real-receiver")
    mirror_repo = _make_mirror_git_repo(tmp_path, receiver_dir)
    claude_home = _make_claude_home(
        tmp_path, {"example_retrieval_repo": real_receiver, receiver_key: mirror_repo}
    )
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    draft_path = _write_draft(
        sender_repo, "topic-cc-mirror", to="example-retrieval-repo-em", cc=cc_id,
    )

    result = _memo_send({"dry_run": False, "topic": "topic-cc-mirror"}, repo_root=sender_repo)

    assert result["exit_code"] == 1
    assert result.get("acted") in (None, [])

    # Neither the `to:` receiver nor the `cc:` mirror received a write — the
    # whole send refuses before any byte moves (klabauter#46's own bar).
    assert list((real_receiver / "cross-repo" / "inbox").glob("*.md")) == []
    assert list((mirror_repo / "cross-repo" / "inbox").glob("*.md")) == []
    assert draft_path.exists()
    assert "status: draft" in draft_path.read_text(encoding="utf-8")
