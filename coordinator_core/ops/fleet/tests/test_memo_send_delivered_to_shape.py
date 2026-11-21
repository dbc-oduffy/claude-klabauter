"""`delivered_to` is stamped `<repo-key>:<path>`, re-resolvable after a root moves."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet.memo_send import _portable_delivered_to_form, _receipt_subject


def test_receipt_is_repo_key_prefixed_repo_relative_path(tmp_path: Path) -> None:
    repo = tmp_path / "receiver"
    target = repo / "cross-repo" / "inbox" / "2026-10-02-a-b-topic.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    got = _portable_delivered_to_form(
        repo, target, {"repos.receiver_alias_z": str(repo), "repos.receiver": str(repo)}
    )

    assert got.endswith(":cross-repo/inbox/2026-10-02-a-b-topic.md")
    assert got.split(":", 1)[0] in {"repos.receiver", "repos.receiver_alias_z"}


def test_receipt_survives_a_root_move_by_key(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    for root in (old, new):
        (root / "cross-repo" / "inbox").mkdir(parents=True)
        (root / "cross-repo" / "inbox" / "m.md").write_text("x", encoding="utf-8")

    before = _portable_delivered_to_form(old, old / "cross-repo/inbox/m.md", {"repos.r": str(old)})
    after = _portable_delivered_to_form(new, new / "cross-repo/inbox/m.md", {"repos.r": str(new)})

    assert before == after == "repos.r:cross-repo/inbox/m.md"


def test_keyless_receiver_never_gets_a_guessed_key(tmp_path: Path) -> None:
    repo = tmp_path / "receiver"
    target = repo / "cross-repo" / "inbox" / "m.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    assert _portable_delivered_to_form(repo, target, {}) == "cross-repo/inbox/m.md"


def test_receipt_subject_says_delivered_only_when_on_branch() -> None:
    got = _receipt_subject(
        "t", "claude-klabauter-em", pinned=False, sha="abc", delivered_to="repos.claude_klabauter:x.md"
    )

    assert got == "memo.send: t delivered to claude-klabauter-em"


def test_receipt_subject_says_pinned_with_ref_and_heal_command_when_off_branch() -> None:
    got = _receipt_subject(
        "t", "claude-klabauter-em", pinned=True, sha="abc", delivered_to="repos.claude_klabauter:x.md"
    )

    assert got == (
        "memo.send: t pinned for claude-klabauter-em (refs/coordinator/inbox-pin/abc; "
        "heal: cross-repo-memo sweep --apply --repo claude_klabauter)"
    )
    assert "delivered" not in got


def test_pinned_subject_without_a_registry_key_names_a_placeholder() -> None:
    got = _receipt_subject("t", "em", pinned=True, sha=None, delivered_to="cross-repo/inbox/m.md")

    assert "--repo <registry-name>" in got and "inbox-pin/<sha>" in got


def test_basename_readers_are_unaffected() -> None:
    assert Path("repos.r:cross-repo/inbox/m.md").name == "m.md"
