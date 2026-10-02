"""memo_wire contract: receiver resolution and refusals, delivery target,
outbox-topic recognition, staged `to:`, performed paths, filename pattern.

Run: python -m pytest coordinator_core/ops/fleet/tests/test_memo_wire.py -p no:cacheprovider -q
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import memo_wire
from coordinator_core.ops.fleet._memo_compose import _memo_filename
from coordinator_core.ops.fleet.memo_wire import (
    DeliveryTarget,
    ReceiverTarget,
    WireRefusal,
    delivered_filename_pattern,
    memo_outbox_topic,
    memo_send_performed_paths,
    read_staged_to,
    resolve_delivery_target,
    resolve_receiver,
)


def _make_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


def _make_claude_home(tmp_path: Path, repos: dict[str, Path], local_text: str | None = None) -> Path:
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    if local_text is None:
        lines = []
        for key, repo_path in repos.items():
            toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'"repos.{key}" = "{toml_val}"')
        local_text = "\n".join(lines) + "\n"
    (machine_local / "registry.local.toml").write_text(local_text, encoding="utf-8")
    return claude_home


@pytest.fixture
def registry(tmp_path, monkeypatch):
    receiver = _make_repo(tmp_path, "project-rag")
    mirror = _make_repo(tmp_path, "claude-klabauter")
    no_checkout = tmp_path / "project-gone"
    home = _make_claude_home(
        tmp_path,
        {"project_rag": receiver, "claude_klabauter": mirror, "project_gone": no_checkout},
    )
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    return {"receiver": receiver, "mirror": mirror, "tmp": tmp_path}


# AC1 -------------------------------------------------------------------

def test_symbols_and_signatures():
    assert WireRefusal._fields == ("message",)
    assert ReceiverTarget._fields == ("name", "inbox_dir", "receiver_repo_path", "all_repos")
    assert DeliveryTarget._fields == ReceiverTarget._fields + ("filename", "self_send")
    params = inspect.signature(resolve_delivery_target).parameters
    assert list(params) == ["to", "sender_worktree", "from_id", "topic", "today"]
    assert all(
        params[n].kind is inspect.Parameter.KEYWORD_ONLY
        for n in ("sender_worktree", "from_id", "topic", "today")
    )
    for name in (
        "resolve_receiver", "memo_outbox_topic", "read_staged_to",
        "memo_send_performed_paths", "delivered_filename_pattern",
    ):
        assert callable(getattr(memo_wire, name))


# AC2 -------------------------------------------------------------------

def test_registered_receiver_resolves(registry):
    target = resolve_receiver("example-retrieval-repo-em")
    assert isinstance(target, ReceiverTarget)
    assert target.name == "example-retrieval-repo-em"
    assert target.receiver_repo_path == registry["receiver"]
    assert target.inbox_dir == registry["receiver"] / "cross-repo" / "inbox"
    assert str(registry["receiver"]) in target.all_repos.values()


def test_unknown_receiver_refused_with_suggestion(registry):
    refusal = resolve_receiver("project-rg-em")
    assert isinstance(refusal, WireRefusal)
    assert refusal.message.startswith("memo.send: UNKNOWN RECEIVER — 'project-rg-em'")
    assert "Did you mean 'example-retrieval-repo-em'?" in refusal.message
    assert "machine-local set repos.<name>" in refusal.message


def test_unknown_receiver_without_suggestion(registry):
    refusal = resolve_receiver("zzzzzzzz-qqqq-em")
    assert isinstance(refusal, WireRefusal)
    assert "UNKNOWN RECEIVER" in refusal.message
    assert "Did you mean" not in refusal.message


def test_mirror_refused(registry):
    refusal = resolve_receiver("claude-klabauter-em")
    assert isinstance(refusal, WireRefusal)
    assert "resolves to a publish mirror, which has no inbox." in refusal.message


def test_undeliverable_checkout_refused(registry):
    refusal = resolve_receiver("project-gone-em")
    assert isinstance(refusal, WireRefusal)
    assert "resolves to no checkout on this machine — refusing the send." in refusal.message


def test_unreadable_registry_refused(tmp_path, monkeypatch):
    home = _make_claude_home(tmp_path, {}, local_text="this is = = not [valid toml\n")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    refusal = resolve_receiver("example-retrieval-repo-em")
    assert isinstance(refusal, WireRefusal)
    assert refusal.message.startswith("memo.send: machine-local registry could not be read: ")
    assert "no folder-scan fallback" in refusal.message


def test_ambiguous_receiver_refused(monkeypatch):
    def _boom(name):
        raise memo_wire.AmbiguousReceiverError("central-em", ["a", "b"])

    monkeypatch.setattr(memo_wire, "resolve_receiver_inbox", _boom)
    refusal = resolve_receiver("central-em")
    assert isinstance(refusal, WireRefusal)
    assert refusal.message.startswith("memo.send: ")
    assert "central-em" in refusal.message


# AC3 -------------------------------------------------------------------

def test_delivery_target_filename_and_not_self_send(registry, tmp_path):
    sender = _make_repo(tmp_path, "sender")
    target = resolve_delivery_target(
        "example-retrieval-repo-em", sender_worktree=sender, from_id="Sender EM",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(target, DeliveryTarget)
    assert target.filename == _memo_filename("2026-09-30", "Sender EM", "a-topic")
    assert target.self_send is False
    assert target.inbox_dir == registry["receiver"] / "cross-repo" / "inbox"


def test_delivery_target_self_send_exactly_when_receiver_is_sender(registry):
    target = resolve_delivery_target(
        "example-retrieval-repo-em", sender_worktree=registry["receiver"], from_id="example-retrieval-repo-em",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(target, DeliveryTarget)
    assert target.self_send is True


def test_empty_slug_from_is_refusal_not_raise(registry, tmp_path):
    refusal = resolve_delivery_target(
        "example-retrieval-repo-em", sender_worktree=tmp_path, from_id="!!!",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(refusal, WireRefusal)
    assert "empty sender slug" in refusal.message


def test_filename_refusal_wins_over_unknown_receiver(registry, tmp_path):
    refusal = resolve_delivery_target(
        "nobody-em", sender_worktree=tmp_path, from_id="!!!",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(refusal, WireRefusal)
    assert "empty sender slug" in refusal.message


def test_unknown_receiver_surfaces_through_delivery_target(registry, tmp_path):
    refusal = resolve_delivery_target(
        "nobody-em", sender_worktree=tmp_path, from_id="sender-em",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(refusal, WireRefusal)
    assert "UNKNOWN RECEIVER" in refusal.message


# AC4 -------------------------------------------------------------------

@pytest.mark.parametrize(
    "relpath, expected",
    [
        (".coordinator-local/memo-outbox/my-topic.md", "my-topic"),
        ("state/memo-outbox/my-topic.md", "my-topic"),
        (".coordinator-local\\memo-outbox\\my-topic.md", "my-topic"),
        ("state\\memo-outbox\\my-topic.md", "my-topic"),
        (".coordinator-local/memo-outbox/sent/my-topic.md", None),
        ("state/memo-outbox/sent/my-topic.md", None),
        (".coordinator-local/memo-outbox/sub/my-topic.md", None),
        (".coordinator-local/memo-outbox/my-topic.txt", None),
        (".coordinator-local/memo-outbox/my-topic", None),
        (".coordinator-local/memo-outbox/My_Topic.md", None),
        (".coordinator-local/memo-outbox/-bad.md", None),
        (".coordinator-local/memo-outbox/.md", None),
        ("pkg/other.py", None),
        ("other/memo-outbox/my-topic.md", None),
    ],
)
def test_memo_outbox_topic(relpath, expected):
    assert memo_outbox_topic(relpath) == expected


# read_staged_to --------------------------------------------------------

def _stage(worktree: Path, reldir: str, topic: str, text: str) -> None:
    d = worktree / reldir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{topic}.md").write_text(text, encoding="utf-8", newline="\n")


def test_read_staged_to_canonical_and_legacy(tmp_path):
    _stage(tmp_path, ".coordinator-local/memo-outbox", "t1", '---\nto: "example-retrieval-repo-em"\n---\n\nbody\n')
    _stage(tmp_path, "state/memo-outbox", "t2", "---\nto: other-em\n---\n\nbody\n")
    assert read_staged_to(tmp_path, "t1") == "example-retrieval-repo-em"
    assert read_staged_to(tmp_path, "t2") == "other-em"


def test_read_staged_to_none_cases(tmp_path):
    assert read_staged_to(tmp_path, "absent") is None
    _stage(tmp_path, ".coordinator-local/memo-outbox", "no-to", "---\ntitle: x\n---\n\nbody\n")
    _stage(tmp_path, ".coordinator-local/memo-outbox", "list-to", "---\nto:\n  - a\n---\n\nbody\n")
    _stage(tmp_path, ".coordinator-local/memo-outbox", "no-fm", "just prose\n")
    for topic in ("no-to", "list-to", "no-fm"):
        assert read_staged_to(tmp_path, topic) is None


# performed paths / filename pattern -----------------------------------

def test_performed_paths_are_relative_forward_slash():
    assert memo_send_performed_paths("my-topic") == (
        ".coordinator-local/memo-outbox/sent/my-topic.md",
        ".coordinator-local/memo-outbox/sent-ledger.jsonl",
    )


def test_delivered_filename_pattern():
    assert delivered_filename_pattern("Sender EM", "a-topic") == "<YYYY-MM-DD>-sender-em-a-topic.md"
    assert delivered_filename_pattern("Sender EM", "a-topic") == _memo_filename(
        memo_wire.DATE_PLACEHOLDER, "Sender EM", "a-topic",
    )
    with pytest.raises(ValueError):
        delivered_filename_pattern("!!!", "a-topic")
