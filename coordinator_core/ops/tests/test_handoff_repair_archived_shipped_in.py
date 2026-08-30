"""
coordinator_core.ops.tests.test_handoff_repair_archived_shipped_in

Tests for the archived-shipped_in provenance-repair verb,
``coordinator_core.ops.handoff_stamp._repair_archived_shipped_in_handler``.

This handler is deliberately NOT ``@register_op``-registered (see
handoff_stamp.py's module docstring) — it is a narrow, separate door onto
``archive/handoffs/`` that every other lifecycle verb (handoff.stamp included)
must keep refusing. No import-guard/_REGISTRY assertion applies here; that
absence is the point.

Coverage:
  (a) repair with an explicit sha on an archived record succeeds and reports
      the prior value.
  (b) unset on an archived record succeeds and clears the field.
  (c) the verb REFUSES without an explicit sha AND without unset=True —
      proving no resolution path exists.
  (d) sha and unset=True together is rejected (mutually exclusive).
  (e) reason is required; when supplied, it is echoed back in the response.
  (f) malformed sha shape is rejected.
  (g) a handoff_path outside archive/handoffs/ (e.g. state/handoffs/, or a
      traversal escape) is rejected.
  (h) file not found -> exit_code 1.
  (i) unset when shipped_in already absent -> byte-identical no-op.
  (j) sha repair to the value already present -> byte-identical no-op.
  (k) invariant-preservation: the EXISTING handoff.stamp op (_handler) still
      refuses an archive/handoffs/ path after this change — proving the
      archival freeze holds everywhere else. This is the most important test
      in this file.

Spec backlink: coordinator_core/ops/handoff_stamp.py
             ("_repair_archived_shipped_in_handler" docstring section)
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path

import pytest

# `_make_git_repo` spawns real `git init`/`config` because the handler under
# test performs real archived-handoff commits (repair, unset, no-op checks)
# that only a genuine object database can validate byte-identically; each
# test builds and mutates its own repo, so isolation cannot be hoisted to
# module scope.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

import coordinator_core.ops.handoff_stamp  # noqa: F401 — module under test

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ops.handoff_stamp import (
    _handler,
    _repair_archived_shipped_in_handler,
)
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with both state/handoffs/ and
    archive/handoffs/ skeletons and return its root (the main worktree root,
    NOT the .git dir)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
    **no_console_creationflags(),
)

    _git("init", "-b", "main")
    _git("config", "user.email", "repair-test@claude-klabauter.test")
    _git("config", "user.name", "Repair Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    (repo / "archive" / "handoffs" / "2026-07").mkdir(parents=True, exist_ok=True)
    (repo / "archive" / "handoffs" / "2026-07" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _seed_archived_handoff(repo: Path, name: str, extra_fm: str = "") -> Path:
    """Write archive/handoffs/2026-07/<name> with minimal YAML frontmatter.
    Does NOT commit — the repair handler only writes the frontmatter."""
    path = repo / "archive" / "handoffs" / "2026-07" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = textwrap.dedent(f"""\
        ---
        title: "Archived Handoff"
        status: superseded
        claimed_at: 2026-07-05T12:00:00Z
        {extra_fm.strip()}
        ---

        # Handoff body.
    """)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) repair with explicit sha — succeeds, reports prior value
# ---------------------------------------------------------------------------


def test_repair_explicit_sha_succeeds_and_reports_prior_value(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo, "2026-07-10-wrong.md", extra_fm="shipped_in: b4f10ccc"
    )

    result = _run(_repair_archived_shipped_in_handler(
        {
            "handoff_path": str(hpath),
            "reason": "incomplete-scope: session/claims.py absent from scope:, "
                      "witness commit 1c913689 named in ship commit message",
            "sha": "1c913689",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["prior_value"] == "b4f10ccc"
    assert result["new_value"] == "1c913689"
    assert "incomplete-scope" in result["reason"]

    text = hpath.read_text(encoding="utf-8")
    assert "shipped_in: 1c913689" in text
    assert "b4f10ccc" not in text


# ---------------------------------------------------------------------------
# (b) unset — succeeds and clears the field
# ---------------------------------------------------------------------------


def test_unset_clears_shipped_in(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo, "2026-07-10-unrecoverable.md", extra_fm="shipped_in: deadbeef"
    )

    result = _run(_repair_archived_shipped_in_handler(
        {
            "handoff_path": str(hpath),
            "reason": "peer-race: no recoverable correct sha for this row",
            "unset": True,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["unset"] is True
    assert result["prior_value"] == "deadbeef"
    assert result["new_value"] is None

    text = hpath.read_text(encoding="utf-8")
    assert "shipped_in" not in text


# ---------------------------------------------------------------------------
# (c) refuses without sha or unset — no resolution path
# ---------------------------------------------------------------------------


def test_refuses_without_sha_or_unset(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-noop.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "reason": "no sha or unset supplied"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "sha" in result.get("error", "")
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (d) sha and unset both supplied — rejected
# ---------------------------------------------------------------------------


def test_rejects_sha_and_unset_both_supplied(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-both.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_shipped_in_handler(
        {
            "handoff_path": str(hpath),
            "reason": "conflicting params",
            "sha": "abc1234",
            "unset": True,
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "mutually exclusive" in result.get("error", "")
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (e) reason required, and recorded in the response
# ---------------------------------------------------------------------------


def test_reason_required(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-no-reason.md")

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "sha": "abc1234"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "reason" in result.get("error", "")


def test_reason_is_recorded_on_success(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-record-reason.md")
    the_reason = "peer-race: concurrent session's commit touched shared scope: path"

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "reason": the_reason, "sha": "abc1234"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["reason"] == the_reason
    assert the_reason in result["message"]


# ---------------------------------------------------------------------------
# (f) malformed sha shape
# ---------------------------------------------------------------------------


def test_rejects_malformed_sha(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-bad-sha.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "reason": "bad sha shape", "sha": "not-a-sha!"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "malformed" in result.get("error", "")
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (g) path outside archive/handoffs/ — rejected
# ---------------------------------------------------------------------------


def test_rejects_state_handoffs_path(tmp_path):
    """The repair verb's own allowed root is archive/handoffs/ ONLY — a
    state/handoffs/ path (the live, non-archived tree) must be rejected too."""
    repo = _make_git_repo(tmp_path)
    path = repo / "state" / "handoffs" / "2026-07-10-live.md"
    path.write_text(
        "---\ntitle: \"Live\"\nclaimed_at: 2026-07-05T12:00:00Z\n---\n\nBody.\n",
        encoding="utf-8",
    )
    original = path.read_text(encoding="utf-8")

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(path), "reason": "wrong root", "sha": "abc1234"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert path.read_text(encoding="utf-8") == original


def test_rejects_traversal_path(tmp_path):
    repo = _make_git_repo(tmp_path)
    secret = repo / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_repair_archived_shipped_in_handler(
        {
            "handoff_path": "archive/handoffs/2026-07/../../../secret.md",
            "reason": "traversal escape",
            "sha": "abc1234",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert secret.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# (h) file not found
# ---------------------------------------------------------------------------


def test_file_not_found(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = _run(_repair_archived_shipped_in_handler(
        {
            "handoff_path": str(repo / "archive" / "handoffs" / "2026-07" / "missing.md"),
            "reason": "does not exist",
            "sha": "abc1234",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1, result


# ---------------------------------------------------------------------------
# (i) / (j) byte-identical no-ops
# ---------------------------------------------------------------------------


def test_unset_already_absent_is_noop(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-already-unset.md")
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "reason": "already absent", "unset": True},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


def test_sha_already_matches_is_noop(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(
        repo, "2026-07-10-already-correct.md", extra_fm="shipped_in: abc1234"
    )
    original = hpath.read_text(encoding="utf-8")

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "reason": "already correct", "sha": "abc1234"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    assert hpath.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Format contract — shipped_in is stored as an 8-char abbreviated sha
# everywhere else (archive_stamp.stamp_shipped_in stores resolved[:8]); a
# 40-char stored value was the exact divergence this workstream exists to
# clean up. Assertions are on STORED LENGTH, not mere substring presence.
# ---------------------------------------------------------------------------


def test_40_char_sha_is_stored_truncated_to_8(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-40char.md")
    full_sha = "1c913689abcdef0123456789abcdef012345678"

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "reason": "incomplete-scope: test", "sha": full_sha},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["new_value"] == "1c913689"
    assert len(result["new_value"]) == 8

    stored = read_fm_field(split_frontmatter(hpath.read_text(encoding="utf-8")).fm_text, "shipped_in")
    assert stored == "1c913689"
    assert len(stored) == 8, f"stored shipped_in must be 8 chars, got {stored!r} (len {len(stored)})"


def test_already_8_char_sha_round_trips_unchanged(tmp_path):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-8char.md")

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "reason": "peer-race: test", "sha": "00d94a8d"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["new_value"] == "00d94a8d"
    assert len(result["new_value"]) == 8

    stored = read_fm_field(split_frontmatter(hpath.read_text(encoding="utf-8")).fm_text, "shipped_in")
    assert stored == "00d94a8d"
    assert len(stored) == 8


def test_stored_truncated_sha_resolves_same_commit_via_git(tmp_path):
    """The 8-char stored form must still resolve (via git rev-parse) to the
    same commit as the full sha supplied by the caller."""
    repo = _make_git_repo(tmp_path)
    hpath = _seed_archived_handoff(repo, "2026-07-10-git-equiv.md")

    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "commit", "-m", "chore: witness commit"],
        cwd=str(repo), check=True, capture_output=True,
    **no_console_creationflags(),
)
    full_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
    **no_console_creationflags(),
).stdout.strip()

    result = _run(_repair_archived_shipped_in_handler(
        {"handoff_path": str(hpath), "reason": "incomplete-scope: test", "sha": full_sha},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    stored = result["new_value"]
    assert len(stored) == 8

    resolved = subprocess.run(
        ["git", "rev-parse", stored], cwd=str(repo), check=True, capture_output=True, text=True,
    **no_console_creationflags(),
).stdout.strip()
    assert resolved == full_sha, (
        f"8-char stored form {stored!r} must resolve to the same commit as "
        f"the full sha {full_sha!r}; git rev-parse resolved to {resolved!r}"
    )


# ---------------------------------------------------------------------------
# (k) Invariant-preservation — the EXISTING handoff.stamp op still refuses
# archive/handoffs/ after this change. THE MOST IMPORTANT TEST IN THIS FILE.
# ---------------------------------------------------------------------------


def test_invariant_handoff_stamp_still_refuses_archive_handoffs_path(tmp_path):
    """handoff.stamp's own allowed_roots (state/handoffs/ ONLY) must be
    byte-identical before and after adding the repair verb above — this new
    verb's archive/handoffs/ allowed_roots is a local variable scoped to
    _repair_archived_shipped_in_handler alone, never merged into _handler's."""
    repo = _make_git_repo(tmp_path)
    archived = repo / "archive" / "handoffs" / "2026-07" / "old.md"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(
        "---\ntitle: \"Old\"\nclaimed_at: 2026-07-01T00:00:00Z\n---\n\nBody.\n",
        encoding="utf-8",
    )
    original = archived.read_text(encoding="utf-8")

    result = _run(_handler(
        {"handoff_path": str(archived), "sha": "abc1234", "kind": "ship-commit"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, (
        f"handoff.stamp must still reject archive/handoffs/ paths after the "
        f"repair verb was added — the archival freeze must hold everywhere "
        f"else; got {result!r}"
    )
    assert result["applied"] is False
    assert archived.read_text(encoding="utf-8") == original
