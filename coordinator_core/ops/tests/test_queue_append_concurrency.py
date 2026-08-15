"""
coordinator_core.ops.tests.test_queue_append_concurrency — collision-guard regression suite
for queue.append content-keyed filenames.

Purpose: Regression coverage for the DR-213 D2(i) amendment (2026-07-08,
2026-07-08-concurrency-safe-strangled-op-writes § C1): queue.append filenames now carry a
content digest (``<date>-<slug>-<digest12>.yaml``) so two DISTINCT entries sharing a
date+slug both survive (AC1), while a genuine re-run of an IDENTICAL entry still dedups to
one file (AC3). Also covers the digest normalization-order pin: the digest MUST be computed
from the post-``\\n``-normalization body, so a literal backslash-n and a real newline in the
otherwise-identical entry collapse to the same digest.

Spec backlink: pln-concurrency-safe-writes-for-th-c7ca9f § C1
DR authority: docs/decisions/DR-213-queue-write-substrate-carveout.md § D2(i) (amended)
Sibling harness: coordinator_core/ops/tests/test_queue_parity.py (byte-parity + write-always)
"""

from __future__ import annotations

import re

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY (queue.append side effects)

from coordinator_core.ipc import _REGISTRY

assert "queue.append" in _REGISTRY, (
    "import guard failed: 'queue.append' not in _REGISTRY — "
    "coordinator_core.ops.queue_append @register_op did not fire"
)

from coordinator_core.ops.queue_append import append_queue_entry  # noqa: E402

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# ---------------------------------------------------------------------------
# Fixed test constants (mirrors test_queue_parity.py's fixed-date convention)
# ---------------------------------------------------------------------------

_TEST_DATE = "2026-01-15"
_TEST_FROM_REPO = "claude-klabauter-em"

_SCHEMA_OUTPUT_DIRS: dict[str, str] = {
    "debt-backlog": "state/debt-backlog",
    "bug-backlog": "state/bug-backlog",
    "improvement-queue": "state/improvement-queue",
    "lessons": "state/lessons",
}


def _base_kwargs() -> dict:
    """Shared entry fields — same title+date across a test's calls (forces the collision)."""
    return dict(
        schema="debt-backlog",
        title="Concurrency collision-guard test entry",
        source="daily-review/2026-01-15",
        risk="Low.",
        proposed_action="None required.",
        status="open",
        from_repo=_TEST_FROM_REPO,
        created=_TEST_DATE,
        session_id="",
    )


# ---------------------------------------------------------------------------
# AC1 — distinct entries, same date+slug → both survive
# ---------------------------------------------------------------------------


class TestDistinctEntriesSurvive:
    """Two DISTINCT entries (different bodies), same forced date+slug → two files (AC1)."""

    def test_two_distinct_bodies_yield_two_files_both_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        kwargs = _base_kwargs()
        result_a = append_queue_entry(body="Entry A body — first writer.", **kwargs)
        result_b = append_queue_entry(body="Entry B body — second writer, distinct content.", **kwargs)

        assert result_a["out_path"] != result_b["out_path"], (
            "distinct-content entries sharing date+slug must resolve to distinct "
            "content-keyed filenames"
        )
        assert result_a["slug"] == result_b["slug"], (
            "slug is derived from title only and must be identical for both calls"
        )

        output_dir = tmp_path / _SCHEMA_OUTPUT_DIRS["debt-backlog"]
        yaml_files = list(output_dir.glob("*.yaml"))
        assert len(yaml_files) == 2, (
            f"expected exactly 2 files for 2 distinct-content same-date+slug entries "
            f"(no silent loss), got {len(yaml_files)}: {yaml_files}"
        )

        combined = "".join(f.read_text(encoding="utf-8") for f in yaml_files)
        assert "Entry A body — first writer." in combined, (
            f"entry A's body must survive in one of the two files:\n{combined}"
        )
        assert "Entry B body — second writer, distinct content." in combined, (
            f"entry B's body must survive in one of the two files:\n{combined}"
        )

        for f in yaml_files:
            # NOTE: the filename date prefix is always _today_iso() (wall-clock), not the
            # `created` param — this is pre-existing _output_path behavior, unchanged by
            # the content-keying amendment. Only the slug component is asserted here.
            assert result_a["slug"] in f.name, (
                f"filename {f.name!r} must retain the slug component"
            )
            # Review: code-reviewer — tighten the "both_present" assertion to also assert
            # the digest-suffix shape explicitly, mirroring test_queue_parity.py's
            # test_promote_filename_uses_ts_safe_slug digest-shape check (Finding 7).
            assert re.search(r"-[0-9a-f]{12}\.yaml$", f.name), (
                f"filename {f.name!r} must end with a 12-hex-char digest suffix"
            )


# ---------------------------------------------------------------------------
# AC3 — identical re-run dedups to one file
# ---------------------------------------------------------------------------


class TestIdenticalReRunDedups:
    """An IDENTICAL entry submitted twice → exactly ONE file (AC3, idempotency preserved)."""

    def test_identical_entry_twice_yields_one_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        kwargs = _base_kwargs()
        result_1 = append_queue_entry(body="Identical re-run body.", **kwargs)
        result_2 = append_queue_entry(body="Identical re-run body.", **kwargs)

        assert result_1["out_path"] == result_2["out_path"], (
            "an identical re-run (same title+date+body) must resolve to the same "
            "content-keyed filename"
        )

        output_dir = tmp_path / _SCHEMA_OUTPUT_DIRS["debt-backlog"]
        yaml_files = list(output_dir.glob("*.yaml"))
        assert len(yaml_files) == 1, (
            f"expected exactly 1 file for 2 identical-content writes (idempotency "
            f"preserved, DR-213 D2(i) amendment), got {len(yaml_files)}: {yaml_files}"
        )
        content = yaml_files[0].read_text(encoding="utf-8")
        assert "Identical re-run body." in content


# ---------------------------------------------------------------------------
# Digest normalization order — post-\\n-normalization, not pre-normalization
# ---------------------------------------------------------------------------


class TestDigestNormalizationOrder:
    """Digest is computed from the post-normalization body (DR-213 D2(i) amendment).

    A literal backslash-n string in the body and a real newline character, once both
    have gone through queue_append.py's ``body.replace("\\\\n", "\\n")`` normalization,
    must be byte-identical — hence the same content digest, hence one file, not two.
    """

    def test_literal_backslash_n_and_real_newline_normalize_to_same_digest(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        kwargs = _base_kwargs()
        # Literal two-character backslash-n sequence in the body string.
        result_literal = append_queue_entry(body="Line one.\\nLine two.", **kwargs)
        # Real newline character — the normalized form of the above.
        result_real = append_queue_entry(body="Line one.\nLine two.", **kwargs)

        assert result_literal["out_path"] == result_real["out_path"], (
            "a literal backslash-n body and a real-newline body that normalize to the "
            "same post-processing string must resolve to the same content digest "
            "(digest computed post-normalization, not pre-normalization)"
        )

        output_dir = tmp_path / _SCHEMA_OUTPUT_DIRS["debt-backlog"]
        yaml_files = list(output_dir.glob("*.yaml"))
        assert len(yaml_files) == 1, (
            f"expected exactly 1 file — literal-\\\\n and real-newline bodies normalize "
            f"identically, got {len(yaml_files)}: {yaml_files}"
        )
        content = yaml_files[0].read_text(encoding="utf-8")
        assert "Line one." in content and "Line two." in content
