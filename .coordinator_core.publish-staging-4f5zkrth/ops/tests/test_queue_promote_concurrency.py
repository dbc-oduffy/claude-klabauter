"""
coordinator_core.ops.tests.test_queue_promote_concurrency — collision-guard tests for
queue.promote's content-keyed filename (DR-213 D2(i) amendment).

Purpose: Confirm the ``<ISO-ts-safe>-<slug>-<digest12>.yaml`` filename derivation
(``queue_promote._content_digest`` + ``promote_lesson``) resolves the same-second+slug
collision without a disk read, matching the shape landed for ``queue.append`` in C1.

Coverage:
  AC2 — two DISTINCT lessons forced to the same ISO-ts-safe filename component + slug
        both survive as two separate files (no silent loss/overwrite).
  AC3 — a genuine re-run of an IDENTICAL lesson (same semantic content) dedups to one
        file (idempotent overwrite via os.replace). Because ``id`` is a fresh uuid4 on
        every ``promote_lesson`` call by default, true byte-identical output across two
        independent calls requires the SAME ``entry_id``/``created`` be supplied (as a
        caller re-running via a fixed correlation id would do) OR the test must assert
        digest-level idempotency directly rather than full-call idempotency. This test
        does both: a full-parameter re-run pinning entry_id+created (one file, byte
        content converges) AND a direct ``_content_digest`` equality assertion between
        two independently-constructed field dicts with differing id/created (documents
        that the digest itself ignores id/created, per the field-set decision in
        ``_content_digest``'s docstring).

Spec backlink: pln-concurrency-safe-writes-for-th-c7ca9f § C2
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY (import guard, mirrors sibling tests)

from coordinator_core.ops.queue_promote import _content_digest, promote_lesson

_TEST_FROM_REPO = "claude-klabauter-em"
_FIXED_TS = "2026-01-15T10:30:00+00:00"


def _isolate_promote_env(monkeypatch, outbox_dir):
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox_dir))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)


class TestQueuePromoteConcurrencyCollisionGuard:
    """queue.promote content-keyed filename resolves same-second+slug collisions."""

    def test_two_distinct_lessons_same_second_and_slug_both_survive(self, tmp_path, monkeypatch):
        """AC2: two DISTINCT lessons forced to the same created-ts + slug → two files."""
        _isolate_promote_env(monkeypatch, tmp_path)

        shared_title = "Concurrency collision test lesson"
        result_a = promote_lesson(
            title=shared_title,
            body="Body A — first distinct lesson content.",
            change_kind="doctrine-edit",
            target_wiki="docs/wiki/collision-test-a.md",
            from_repo=_TEST_FROM_REPO,
            entry_id="00000000-0000-0000-0000-0000000000a1",
            created=_FIXED_TS,
        )
        result_b = promote_lesson(
            title=shared_title,
            body="Body B — second distinct lesson content, different from A.",
            change_kind="wiki-new",
            target_wiki="docs/wiki/collision-test-b.md",
            from_repo=_TEST_FROM_REPO,
            entry_id="00000000-0000-0000-0000-0000000000b2",
            created=_FIXED_TS,
        )

        path_a = Path(result_a["out_path"])
        path_b = Path(result_b["out_path"])

        assert path_a != path_b, (
            "distinct-content lessons sharing created-ts + slug must produce distinct "
            f"filenames; got identical path {path_a}"
        )
        assert path_a.exists(), f"lesson A file missing: {path_a}"
        assert path_b.exists(), f"lesson B file missing: {path_b}"

        yaml_files = list(tmp_path.glob("*.yaml"))
        assert len(yaml_files) == 2, (
            f"expected exactly 2 files after 2 distinct-content same-second+slug writes, "
            f"got {len(yaml_files)}: {yaml_files}"
        )

        text_a = path_a.read_text(encoding="utf-8")
        text_b = path_b.read_text(encoding="utf-8")
        assert "Body A" in text_a and "Body A" not in text_b
        assert "Body B" in text_b and "Body B" not in text_a

    def test_identical_lesson_rerun_with_same_id_and_created_dedups_to_one_file(
        self, tmp_path, monkeypatch
    ):
        """AC3: re-running promote_lesson with identical content AND identical
        entry_id/created (the caller-supplied-correlation-id re-run shape) dedups to
        one file via same digest → same filename → os.replace overwrite.
        """
        _isolate_promote_env(monkeypatch, tmp_path)

        kwargs = dict(
            title="Idempotent rerun test lesson",
            body="Identical body content for idempotency check.",
            change_kind="doctrine-edit",
            target_wiki="docs/wiki/idempotent-rerun.md",
            from_repo=_TEST_FROM_REPO,
            entry_id="00000000-0000-0000-0000-000000000c33",
            created=_FIXED_TS,
        )

        result1 = promote_lesson(**kwargs)
        result2 = promote_lesson(**kwargs)

        assert result1["out_path"] == result2["out_path"], (
            "identical entry_id+created+content re-run must produce the same output path"
        )

        yaml_files = list(tmp_path.glob("*.yaml"))
        assert len(yaml_files) == 1, (
            f"expected exactly 1 file after 2 identical re-runs (idempotent overwrite), "
            f"got {len(yaml_files)}: {yaml_files}"
        )

    def test_content_digest_ignores_id_and_created(self):
        """_content_digest excludes uuid4 id + created timestamp (per-write-varying
        provenance) so two calls with the SAME semantic content but DIFFERENT
        id/created — the shape of an independent promote_lesson() call without a
        caller-supplied correlation id — still converge on the same digest, and thus
        the same filename, and thus dedup via os.replace.

        This directly documents/asserts the field-set decision in _content_digest's
        docstring: without this exclusion, two genuine re-runs of the same logical
        lesson (fresh uuid4 + fresh created each call) would NEVER dedup.
        """
        fields_call_1 = {
            "id": "11111111-1111-1111-1111-111111111111",
            "created": "2026-01-15T10:30:00+00:00",
            "from_repo": _TEST_FROM_REPO,
            "change_kind": "doctrine-edit",
            "target_wiki": "docs/wiki/digest-idempotency.md",
            "title": "Digest idempotency across id/created",
            "body": "Same semantic content, different provenance.",
        }
        fields_call_2 = {
            "id": "22222222-2222-2222-2222-222222222222",
            "created": "2026-06-01T00:00:00+00:00",
            "from_repo": _TEST_FROM_REPO,
            "change_kind": "doctrine-edit",
            "target_wiki": "docs/wiki/digest-idempotency.md",
            "title": "Digest idempotency across id/created",
            "body": "Same semantic content, different provenance.",
        }

        assert _content_digest(fields_call_1) == _content_digest(fields_call_2), (
            "digest must be identical across differing id/created for the same "
            "semantic content — id/created are per-write-varying provenance, "
            "excluded from the digest field-set by design"
        )

    def test_content_digest_diverges_on_semantic_content_change(self):
        """Sanity counterpart: changing a semantic field (body) DOES change the digest."""
        base_fields = {
            "id": "33333333-3333-3333-3333-333333333333",
            "created": "2026-01-15T10:30:00+00:00",
            "from_repo": _TEST_FROM_REPO,
            "change_kind": "doctrine-edit",
            "target_wiki": "docs/wiki/digest-divergence.md",
            "title": "Digest divergence test",
            "body": "Original body.",
        }
        changed_fields = dict(base_fields, body="Different body content.")

        assert _content_digest(base_fields) != _content_digest(changed_fields), (
            "digest must diverge when a semantic field (body) changes"
        )
