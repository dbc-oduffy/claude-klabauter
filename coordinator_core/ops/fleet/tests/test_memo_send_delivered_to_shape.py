"""`delivered_to` is stamped `<repo-key>:<path>`, re-resolvable after a root moves."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet.memo_send import _portable_delivered_to_form


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


def test_basename_readers_are_unaffected() -> None:
    assert Path("repos.r:cross-repo/inbox/m.md").name == "m.md"
