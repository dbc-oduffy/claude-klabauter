"""
Tests for coordinator_core.ops.fleet.memo_reconcile_outbox — the
memo.reconcile_outbox MUTATING UDS op.

Test surface (state/bug-backlog/2026-08-25-the-memo-outbox-does-not-clean-
itself-up-after-a-send.yaml):
  - setup-error envelope on bad params (missing/wrong-typed dry_run, unknown key)
  - missing repo_root -> setup-error envelope
  - a non-draft entry moves to sent/; a draft entry does not
  - a frontmatter-less body fragment is REPORTED, never moved
  - an existing sent/<name> is never clobbered
  - dry_run previews every disposition and touches nothing
  - the op is idempotent: a second run over a swept outbox is a clean no-op

No `git init` is needed: the op resolves a worktree from repo_root and then
only reads/moves files, so a directory carrying a bare `.git/` entry (what
`main_worktree_root` keys on) is a faithful stand-in and keeps this module off
the spawns_process tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_reconcile_outbox import (
    _memo_reconcile_outbox,
    _validate_params,
)

#: Fixtures deliberately stage drafts at the RETIRED root -- the op's
#: canonical write root moved to `.coordinator-local/memo-outbox/`
#: (2026-09-03), and every test in this module doubles as a read-compat
#: proof: a draft staged at the old root before the move must still be
#: found and reconciled, landing in the NEW `sent/` dir (`_NEW_SENT`).
_OUTBOX = ("state", "memo-outbox")
_NEW_OUTBOX = (".coordinator-local", "memo-outbox")
_NEW_SENT = (".coordinator-local", "memo-outbox", "sent")


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A worktree root as `main_worktree_root` recognises one: a directory
    with a `.git` entry beneath it. No `git init` — nothing here runs git."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _outbox_dir(root: Path) -> Path:
    d = root.joinpath(*_OUTBOX)
    d.mkdir(parents=True, exist_ok=True)
    return d


# Review: coordinator:code-reviewer (Finding 3) — every fixture above stages
# only at the retired root, so `_reconcile`'s own new-root behavior was
# unproven at exactly the root the engine now writes to. This stages at the
# canonical `.coordinator-local/memo-outbox/` root instead.
def _new_outbox_dir(root: Path) -> Path:
    d = root.joinpath(*_NEW_OUTBOX)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_memo(root: Path, name: str, status: str | None) -> Path:
    """Write an outbox entry. `status=None` writes a frontmatter-less fragment."""
    path = _outbox_dir(root) / name
    if status is None:
        path.write_text("just a body fragment, no frontmatter\n", encoding="utf-8")
    else:
        path.write_text(
            f'---\ntitle: "t"\nto: "example-retrieval-repo-em"\nstatus: {status}\n---\n\nbody\n',
            encoding="utf-8",
        )
    return path


def _write_new_root_memo(root: Path, name: str, status: str | None) -> Path:
    """Same as `_write_memo`, staged at the NEW canonical root."""
    path = _new_outbox_dir(root) / name
    if status is None:
        path.write_text("just a body fragment, no frontmatter\n", encoding="utf-8")
    else:
        path.write_text(
            f'---\ntitle: "t"\nto: "example-retrieval-repo-em"\nstatus: {status}\n---\n\nbody\n',
            encoding="utf-8",
        )
    return path


def _dispositions(result: dict) -> dict:
    return {c["filename"]: c["disposition"] for c in result["candidates"]}


class TestValidateParams:
    def test_missing_dry_run_is_setup_error(self):
        assert _validate_params({})["exit_code"] == 1

    def test_wrong_typed_dry_run_is_setup_error(self):
        assert _validate_params({"dry_run": "yes"})["exit_code"] == 1

    def test_unknown_param_is_setup_error(self):
        """This op reconciles the whole outbox; a per-entry selector would be
        silently dropped rather than honoured, so it fails loud instead."""
        assert _validate_params({"dry_run": True, "topic": "x"})["exit_code"] == 1

    def test_both_arms_of_dry_run_validate(self):
        assert _validate_params({"dry_run": True}) is True
        assert _validate_params({"dry_run": False}) is False


class TestRepoRoot:
    def test_missing_repo_root_is_setup_error(self):
        result = _memo_reconcile_outbox({"dry_run": True}, repo_root=None)
        assert result["exit_code"] == 1

    def test_absent_outbox_is_a_clean_empty_result(self, worktree):
        """Never drafted anything: an empty outbox is not an error."""
        result = _memo_reconcile_outbox({"dry_run": True}, repo_root=worktree)
        assert result["exit_code"] == 0
        assert result["candidates"] == []


class TestSweep:
    def test_delivered_entry_moves_and_draft_stays(self, worktree):
        _write_memo(worktree, "delivered.md", "sent")
        _write_memo(worktree, "live.md", "draft")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["exit_code"] == 0
        assert [a["filename"] for a in result["acted"]] == ["delivered.md"]
        outbox = worktree.joinpath(*_OUTBOX)
        assert not (outbox / "delivered.md").exists()
        assert (worktree.joinpath(*_NEW_SENT) / "delivered.md").is_file(), (
            "a legacy-root draft must still be reconciled, landing in the NEW sent/"
        )
        assert (outbox / "live.md").is_file(), "a draft's home IS the outbox"

    # Review: coordinator:code-reviewer (Finding 3) — the core "non-draft
    # moves to sent/" case, proven at the NEW canonical root with no legacy
    # dir present, so a regression isolating new-root behavior in
    # `_reconcile` itself has a test here to catch it.
    def test_delivered_entry_at_new_root_moves_and_draft_stays(self, worktree):
        _write_new_root_memo(worktree, "delivered.md", "sent")
        _write_new_root_memo(worktree, "live.md", "draft")
        assert not worktree.joinpath(*_OUTBOX).exists()

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["exit_code"] == 0
        assert [a["filename"] for a in result["acted"]] == ["delivered.md"]
        new_outbox = worktree.joinpath(*_NEW_OUTBOX)
        assert not (new_outbox / "delivered.md").exists()
        assert (worktree.joinpath(*_NEW_SENT) / "delivered.md").is_file()
        assert (new_outbox / "live.md").is_file(), "a draft's home IS the outbox"

    def test_every_non_draft_status_is_delivered_history(self, worktree):
        """`sent` is not the only terminal spelling — the population that
        motivated this op also carried delivered / delivered-out-of-band /
        resolved-before-send. The discriminator is `draft`, not a terminal
        allow-list, so a new terminal spelling can never silently re-inflate
        the count."""
        for i, status in enumerate(
            ("sent", "delivered", "delivered-out-of-band", "resolved-before-send")
        ):
            _write_memo(worktree, f"m{i}.md", status)

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert len(result["acted"]) == 4
        assert not list(worktree.joinpath(*_OUTBOX).glob("*.md"))

    def test_frontmatter_less_fragment_is_reported_never_moved(self, worktree):
        frag = _write_memo(worktree, "orphan.body.md", None)

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["acted"] == []
        assert frag.is_file(), "a body fragment was never a memo; sent/ is not its home"
        # Act mode has no `candidates` key — an orphan surfaces via `skipped`,
        # so it can never vanish silently from the result.
        assert [s["disposition"] for s in result["skipped"]] == ["report"]

    def test_statusless_frontmatter_is_left_in_place(self, worktree):
        path = _outbox_dir(worktree) / "no-status.md"
        path.write_text('---\ntitle: "t"\n---\n\nbody\n', encoding="utf-8")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["acted"] == []
        assert path.is_file()

    def test_existing_sent_copy_is_never_clobbered(self, worktree):
        _write_memo(worktree, "dupe.md", "sent")
        sent = worktree.joinpath(*_NEW_SENT)
        sent.mkdir(parents=True)
        (sent / "dupe.md").write_text("the authoritative archived copy\n", encoding="utf-8")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["acted"] == []
        assert [s["filename"] for s in result["skipped"]] == ["dupe.md"]
        assert (sent / "dupe.md").read_text(encoding="utf-8") == (
            "the authoritative archived copy\n"
        )
        assert worktree.joinpath(*_OUTBOX, "dupe.md").is_file()


class TestDryRun:
    def test_dry_run_previews_dispositions_and_moves_nothing(self, worktree):
        _write_memo(worktree, "delivered.md", "sent")
        _write_memo(worktree, "live.md", "draft")
        _write_memo(worktree, "orphan.body.md", None)

        result = _memo_reconcile_outbox({"dry_run": True}, repo_root=worktree)

        assert result["exit_code"] == 0
        assert _dispositions(result) == {
            "delivered.md": "move",
            "live.md": "keep",
            "orphan.body.md": "report",
        }
        outbox = worktree.joinpath(*_OUTBOX)
        assert sorted(p.name for p in outbox.glob("*.md")) == [
            "delivered.md",
            "live.md",
            "orphan.body.md",
        ]
        assert not (outbox / "sent").exists()


class TestIdempotence:
    def test_second_run_over_a_swept_outbox_is_a_clean_noop(self, worktree):
        _write_memo(worktree, "delivered.md", "sent")
        _write_memo(worktree, "live.md", "draft")

        first = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)
        second = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert len(first["acted"]) == 1
        assert second["acted"] == []
        assert second["skipped"] == []
        assert second["exit_code"] == 0
