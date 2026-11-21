"""memo_wire contract: receiver resolution and refusals, delivery target,
outbox-topic recognition, staged `to:`, performed paths, filename pattern.

Run: python -m pytest coordinator_core/ops/fleet/tests/test_memo_wire.py -p no:cacheprovider -q
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import memo_wire
from coordinator_core.ops.fleet._memo_compose import _memo_filename
from coordinator_core.ops.fleet._memo_resolver import (
    PR_COMMENT_FALLBACK,
    never_inbox_mirror_refusal,
)
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
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
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

def test_symbols_and_signatures() -> None:
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

def test_registered_receiver_resolves(registry) -> None:
    target = resolve_receiver("example-retrieval-repo-em")
    assert isinstance(target, ReceiverTarget)
    assert target.name == "example-retrieval-repo-em"
    assert target.receiver_repo_path == registry["receiver"]
    assert target.inbox_dir == registry["receiver"] / "cross-repo" / "inbox"
    assert str(registry["receiver"]) in target.all_repos.values()


def test_unknown_receiver_refused_with_suggestion(registry) -> None:
    refusal = resolve_receiver("project-rg-em")
    assert isinstance(refusal, WireRefusal)
    assert refusal.message.startswith("memo.send: UNKNOWN RECEIVER — 'project-rg-em'")
    assert "Did you mean 'example-retrieval-repo-em'?" in refusal.message
    assert "machine-local set repos.<name>" in refusal.message


def test_unknown_receiver_without_suggestion(registry) -> None:
    refusal = resolve_receiver("zzzzzzzz-qqqq-em")
    assert isinstance(refusal, WireRefusal)
    assert "UNKNOWN RECEIVER" in refusal.message
    assert "Did you mean" not in refusal.message


def test_mirror_refused(registry) -> None:
    refusal = resolve_receiver("claude-klabauter-em")
    assert isinstance(refusal, WireRefusal)
    assert "resolves to a publish mirror, which has no inbox." in refusal.message


def test_undeliverable_checkout_refused(registry) -> None:
    refusal = resolve_receiver("project-gone-em")
    assert isinstance(refusal, WireRefusal)
    assert "resolves to no checkout on this machine — refusing the send." in refusal.message


def test_unreadable_registry_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _make_claude_home(tmp_path, {}, local_text="this is = = not [valid toml\n")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    refusal = resolve_receiver("example-retrieval-repo-em")
    assert isinstance(refusal, WireRefusal)
    assert refusal.message.startswith("memo.send: machine-local registry could not be read: ")
    assert "no folder-scan fallback" in refusal.message


def test_ambiguous_receiver_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(name):
        raise memo_wire.AmbiguousReceiverError("central-em", ["a", "b"])

    monkeypatch.setattr(memo_wire, "resolve_receiver_inbox", _boom)
    refusal = resolve_receiver("central-em")
    assert isinstance(refusal, WireRefusal)
    assert refusal.message.startswith("memo.send: ")
    assert "central-em" in refusal.message


# AC3 -------------------------------------------------------------------

def test_delivery_target_filename_and_not_self_send(registry, tmp_path: Path) -> None:
    sender = _make_repo(tmp_path, "sender")
    target = resolve_delivery_target(
        "example-retrieval-repo-em", sender_worktree=sender, from_id="Sender EM",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(target, DeliveryTarget)
    assert target.filename == _memo_filename("2026-09-30", "Sender EM", "a-topic")
    assert target.self_send is False
    assert target.inbox_dir == registry["receiver"] / "cross-repo" / "inbox"


def test_delivery_target_self_send_exactly_when_receiver_is_sender(registry) -> None:
    target = resolve_delivery_target(
        "example-retrieval-repo-em", sender_worktree=registry["receiver"], from_id="example-retrieval-repo-em",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(target, DeliveryTarget)
    assert target.self_send is True


def test_empty_slug_from_is_refusal_not_raise(registry, tmp_path: Path) -> None:
    refusal = resolve_delivery_target(
        "example-retrieval-repo-em", sender_worktree=tmp_path, from_id="!!!",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(refusal, WireRefusal)
    assert "empty sender slug" in refusal.message


def test_filename_refusal_wins_over_unknown_receiver(registry, tmp_path: Path) -> None:
    refusal = resolve_delivery_target(
        "nobody-em", sender_worktree=tmp_path, from_id="!!!",
        topic="a-topic", today="2026-09-30",
    )
    assert isinstance(refusal, WireRefusal)
    assert "empty sender slug" in refusal.message


def test_unknown_receiver_surfaces_through_delivery_target(registry, tmp_path: Path) -> None:
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
def test_memo_outbox_topic(relpath, expected) -> None:
    assert memo_outbox_topic(relpath) == expected


# read_staged_to --------------------------------------------------------

def _stage(worktree: Path, reldir: str, topic: str, text: str) -> None:
    d = worktree / reldir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{topic}.md").write_text(text, encoding="utf-8", newline="\n")


def test_read_staged_to_canonical_and_legacy(tmp_path: Path) -> None:
    _stage(tmp_path, ".coordinator-local/memo-outbox", "t1", '---\nto: "example-retrieval-repo-em"\n---\n\nbody\n')
    _stage(tmp_path, "state/memo-outbox", "t2", "---\nto: other-em\n---\n\nbody\n")
    assert read_staged_to(tmp_path, "t1") == "example-retrieval-repo-em"
    assert read_staged_to(tmp_path, "t2") == "other-em"


def test_read_staged_to_none_cases(tmp_path: Path) -> None:
    assert read_staged_to(tmp_path, "absent") is None
    _stage(tmp_path, ".coordinator-local/memo-outbox", "no-to", "---\ntitle: x\n---\n\nbody\n")
    _stage(tmp_path, ".coordinator-local/memo-outbox", "list-to", "---\nto:\n  - a\n---\n\nbody\n")
    _stage(tmp_path, ".coordinator-local/memo-outbox", "no-fm", "just prose\n")
    for topic in ("no-to", "list-to", "no-fm"):
        assert read_staged_to(tmp_path, topic) is None


# performed paths / filename pattern -----------------------------------

def test_performed_paths_are_relative_forward_slash() -> None:
    assert memo_send_performed_paths("my-topic") == (
        ".coordinator-local/memo-outbox/sent/my-topic.md",
    )


def test_delivered_filename_pattern() -> None:
    assert delivered_filename_pattern("Sender EM", "a-topic") == "<YYYY-MM-DD>-sender-em-a-topic.md"
    assert delivered_filename_pattern("Sender EM", "a-topic") == _memo_filename(
        memo_wire.DATE_PLACEHOLDER, "Sender EM", "a-topic",
    )
    with pytest.raises(ValueError):
        delivered_filename_pattern("!!!", "a-topic")


# mirror-only box -------------------------------------------------------

_KLABAUTER_EM = "claude-klabauter-em"
_CONTENT_ROOT_EM = "content-root-em"
_MIRROR_OWNER_EM = "claude-klabauter-em"


def _mirror_only_box(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    home_root: Path | None = None,
    extra_repos: dict[str, Path] | None = None,
    marked: bool = True,
    declared: bool = True,
) -> dict[str, Path]:
    """A box whose only registered repo is the klabauter publish mirror; the manifest
    (never the box's installed one) names content-root-em central, and the G-EM home
    sits under tmp_path."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
    mirror = _make_repo(tmp_path, "claude-klabauter")
    if marked:
        (mirror / "PUBLISH_MIRROR").write_text("m\n", encoding="utf-8")
    gem = home_root if home_root is not None else _make_repo(tmp_path, "gem-box")
    repos = {"claude_klabauter": mirror, **(extra_repos or {})}
    claude_home = _make_claude_home(tmp_path, repos)
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    local = machine_local / "registry.local.toml"
    local.write_text(
        local.read_text(encoding="utf-8")
        + f'"coordinator.group_em_home" = {json.dumps(str(gem))}\n',
        encoding="utf-8",
    )
    if declared:
        (machine_local / "registry.toml").write_text(
            "schema = 1\n\n[publish.mirrors.claude_klabauter]\n"
            f'owner = "{_MIRROR_OWNER_EM}"\npath = {json.dumps(str(mirror))}\n',
            encoding="utf-8",
        )
    doctrine = tmp_path / "doctrine-root"
    schemas = doctrine / "coordinator" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "coordinator-registry.manifest.json").write_text(
        json.dumps({"identity": {
            "repoAliases": [], "redirectAliases": [],
            "centralReceiverIds": [_CONTENT_ROOT_EM],
        }}),
        encoding="utf-8",
    )
    (machine_local / ".coordinator-content-root").write_text(str(doctrine), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    return {"mirror": mirror, "gem": gem}


def _resolve(to: str, tmp_path: Path):
    return resolve_delivery_target(
        to=to, sender_worktree=tmp_path / "sender", from_id="sender-em",
        topic="a-topic", today="2026-11-20",
    )


@pytest.mark.parametrize("receiver", [_KLABAUTER_EM, _CONTENT_ROOT_EM])
def test_mirror_only_box_known_receivers_resolve_to_the_gem_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receiver: str,
) -> None:
    box = _mirror_only_box(tmp_path, monkeypatch)
    target = _resolve(receiver, tmp_path)
    assert isinstance(target, DeliveryTarget), target
    assert target.name == receiver
    assert target.receiver_repo_path == box["gem"]
    assert target.inbox_dir == box["gem"] / "state" / "cross-repo" / "inbox"
    assert never_inbox_mirror_refusal(receiver, target.receiver_repo_path) is None


def test_mirror_only_box_registered_owner_is_not_diverted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _make_repo(tmp_path, "claude-klabauter")
    box = _mirror_only_box(tmp_path, monkeypatch, extra_repos={"claude_klabauter": owner})
    for receiver in (_KLABAUTER_EM, _MIRROR_OWNER_EM):
        target = _resolve(receiver, tmp_path)
        assert isinstance(target, DeliveryTarget), receiver
        assert target.receiver_repo_path == owner, receiver
        assert target.receiver_repo_path != box["gem"], receiver


def test_mirror_only_box_unknown_receiver_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mirror_only_box(tmp_path, monkeypatch)
    refusal = _resolve("zzzzzzzz-qqqq-em", tmp_path)
    assert isinstance(refusal, WireRefusal)
    assert "UNKNOWN RECEIVER" in refusal.message
    assert PR_COMMENT_FALLBACK in refusal.message


@pytest.mark.parametrize("receiver", [_KLABAUTER_EM, _CONTENT_ROOT_EM])
def test_mirror_only_box_refused_home_keeps_the_unknown_receiver_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receiver: str,
) -> None:
    checkout = _make_repo(tmp_path, "checkout")
    _mirror_only_box(tmp_path, monkeypatch, home_root=checkout / "home")
    refusal = _resolve(receiver, tmp_path)
    assert isinstance(refusal, WireRefusal)
    assert "UNKNOWN RECEIVER" in refusal.message
    assert PR_COMMENT_FALLBACK in refusal.message


def test_mirror_only_box_undeclared_mirror_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _mirror_only_box(tmp_path, monkeypatch, marked=False, declared=False)
    refusal = resolve_receiver(_KLABAUTER_EM)
    assert isinstance(refusal, WireRefusal)
    assert "resolves to a publish mirror, which has no inbox." in refusal.message
    assert str(box["gem"]) not in refusal.message
