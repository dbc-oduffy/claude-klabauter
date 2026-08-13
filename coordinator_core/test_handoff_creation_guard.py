"""
coordinator_core.test_handoff_creation_guard — unit tests for
``coordinator_core.handoff_creation_guard`` (the archived-twin creation guard).

Coverage:
  (a) find_archived_twin_by_filename — sharded (archive/handoffs/YYYY-MM/<f>),
      unsharded (archive/handoffs/<f>), and no-match cases.
  (b) find_archived_twin_by_handoff_id — frontmatter handoff_id match, no
      handoff_id supplied, no match found.
  (c) assert_no_archived_twin — raises HandoffArchivedTwinError naming the
      archived twin's path on a filename collision AND on a handoff_id
      collision; message names the corrective action (git mv / fresh id);
      no-op (returns None) when no twin exists.
  (d) no escape/force parameter exists on assert_no_archived_twin (absolute
      guard, per the negative-spec in the module docstring).

Spec backlink: state/subagent-share/41c1917d-53d5-49f9-9e70-cf281768cc5d/
coordinatorexecutor-953c07e8.md
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from coordinator_core.handoff_creation_guard import (
    HandoffArchivedTwinError,
    _read_frontmatter_block,
    assert_no_archived_twin,
    find_archived_twin_by_filename,
    find_archived_twin_by_handoff_id,
)


def _seed_archived(repo_root: Path, rel: str, *, handoff_id: str | None = None) -> Path:
    p = repo_root / "archive" / "handoffs" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", 'title: "Archived"', "status: closed"]
    if handoff_id is not None:
        lines.append(f"handoff_id: {handoff_id}")
    lines.extend(["---", "", "# Body"])
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestFindArchivedTwinByFilename:
    def test_sharded_match(self, tmp_path):
        archived = _seed_archived(tmp_path, "2026-07/2026-07-22-foo.md")
        target = tmp_path / "state" / "handoffs" / "2026-07-22-foo.md"
        found = find_archived_twin_by_filename(target, tmp_path)
        assert found == archived

    def test_unsharded_match(self, tmp_path):
        archived = _seed_archived(tmp_path, "2026-07-22-foo.md")
        target = tmp_path / "state" / "handoffs" / "2026-07-22-foo.md"
        found = find_archived_twin_by_filename(target, tmp_path)
        assert found == archived

    def test_no_match(self, tmp_path):
        _seed_archived(tmp_path, "2026-07/2026-07-22-other.md")
        target = tmp_path / "state" / "handoffs" / "2026-07-22-foo.md"
        assert find_archived_twin_by_filename(target, tmp_path) is None

    def test_no_archive_dir(self, tmp_path):
        target = tmp_path / "state" / "handoffs" / "2026-07-22-foo.md"
        assert find_archived_twin_by_filename(target, tmp_path) is None


class TestFindArchivedTwinByHandoffId:
    def test_match(self, tmp_path):
        archived = _seed_archived(tmp_path, "2026-07/2026-07-22-foo.md", handoff_id="hnd-foo-abc123")
        found = find_archived_twin_by_handoff_id("hnd-foo-abc123", tmp_path)
        assert found == archived

    def test_no_match(self, tmp_path):
        _seed_archived(tmp_path, "2026-07/2026-07-22-foo.md", handoff_id="hnd-foo-abc123")
        assert find_archived_twin_by_handoff_id("hnd-bar-zzz999", tmp_path) is None

    def test_none_handoff_id_returns_none(self, tmp_path):
        _seed_archived(tmp_path, "2026-07/2026-07-22-foo.md", handoff_id="hnd-foo-abc123")
        assert find_archived_twin_by_handoff_id(None, tmp_path) is None

    def test_empty_handoff_id_returns_none(self, tmp_path):
        assert find_archived_twin_by_handoff_id("", tmp_path) is None

    def test_id_in_body_not_frontmatter_is_ignored(self, tmp_path):
        """Narrowing must not turn a body-only occurrence into a false positive:
        the guard only ever matched the frontmatter scalar (extract_frontmatter_scalar
        already stopped scanning at the second fence before this narrowing), and the
        streamed head-read must preserve that exactly."""
        archived = tmp_path / "archive" / "handoffs" / "2026-07" / "2026-07-22-foo.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text(
            "\n".join(
                [
                    "---",
                    'title: "Archived"',
                    "status: closed",
                    "---",
                    "",
                    "# Body mentions hnd-body-only-999 but not as frontmatter handoff_id",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        assert find_archived_twin_by_handoff_id("hnd-body-only-999", tmp_path) is None

    def test_malformed_no_closing_fence_still_resolves(self, tmp_path):
        """Review: code-reviewer (slice 1, P2) — the malformed-fence fallback
        (no closing '---' found before EOF) falls through to returning
        whatever was read so far, matching the pre-narrowing full-file read.
        Pin that a handoff_id living in an unterminated frontmatter block
        still resolves, exactly as it did before this narrowing."""
        archived = tmp_path / "archive" / "handoffs" / "2026-07" / "2026-07-22-malformed.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text(
            "\n".join(
                [
                    "---",
                    'title: "Malformed"',
                    "handoff_id: hnd-malformed-1",
                    "status: closed",
                    # No closing '---' fence anywhere in the file.
                    "",
                    "# Body runs right in without a second fence",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        found = find_archived_twin_by_handoff_id("hnd-malformed-1", tmp_path)
        assert found == archived

    def test_large_corpus_narrowing_reads_only_frontmatter(self, tmp_path):
        """Pins the narrowing itself: a large body must not be read to find a match
        that lives in the frontmatter block near the top of the file."""
        huge_body = "\n".join(f"filler line {i} of an enormous archived body" for i in range(50_000))
        archived = tmp_path / "archive" / "handoffs" / "2026-07" / "2026-07-22-huge.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text(
            "\n".join(["---", "title: huge", "handoff_id: hnd-huge-match-1", "---", "", huge_body])
            + "\n",
            encoding="utf-8",
        )
        found = find_archived_twin_by_handoff_id("hnd-huge-match-1", tmp_path)
        assert found == archived
        header = _read_frontmatter_block(archived)
        assert header is not None
        assert "filler line" not in header
        assert len(header) < 200


class TestAssertNoArchivedTwin:
    def test_no_twin_is_noop(self, tmp_path):
        target = tmp_path / "state" / "handoffs" / "2026-07-22-fresh.md"
        assert assert_no_archived_twin(target, tmp_path) is None

    def test_filename_collision_raises(self, tmp_path):
        archived = _seed_archived(tmp_path, "2026-07/2026-07-22-foo.md")
        target = tmp_path / "state" / "handoffs" / "2026-07-22-foo.md"
        with pytest.raises(HandoffArchivedTwinError) as exc_info:
            assert_no_archived_twin(target, tmp_path)
        msg = str(exc_info.value)
        assert str(archived) in msg
        assert "git mv" in msg
        assert "predecessor" in msg

    def test_handoff_id_collision_raises(self, tmp_path):
        archived = _seed_archived(
            tmp_path, "2026-07/2026-07-22-differently-named.md", handoff_id="hnd-dup-abc123"
        )
        target = tmp_path / "state" / "handoffs" / "2026-07-22-fresh-name.md"
        with pytest.raises(HandoffArchivedTwinError) as exc_info:
            assert_no_archived_twin(target, tmp_path, handoff_id="hnd-dup-abc123")
        msg = str(exc_info.value)
        assert str(archived) in msg

    def test_filename_match_checked_before_handoff_id(self, tmp_path):
        """Filename basis alone is sufficient — no handoff_id needed to trip the guard."""
        _seed_archived(tmp_path, "2026-07/2026-07-22-foo.md")
        target = tmp_path / "state" / "handoffs" / "2026-07-22-foo.md"
        with pytest.raises(HandoffArchivedTwinError):
            assert_no_archived_twin(target, tmp_path, handoff_id=None)

    def test_no_escape_parameter(self):
        """Negative-spec: no force/escape kwarg on the public guard function."""
        sig = inspect.signature(assert_no_archived_twin)
        for forbidden in ("force", "escape", "override", "skip"):
            assert forbidden not in sig.parameters, (
                f"assert_no_archived_twin must not gain a {forbidden!r} escape parameter "
                "— see module docstring negative-spec"
            )
