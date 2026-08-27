"""2026-08-21 rebuild-the-three-ceremony-assemblers plan, C13: the
`_scan_deliverable_collision` corpus walk stops reading whole handoff bodies
to check one frontmatter field.

Spec backlink: `docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md`
§ C13 ("baton-assemble -- the uniqueness check stops walking 170 files per
mint"). The scan itself is warn-only and stays a full `state/handoffs/`
walk by design (§ C13 body: doctrinally distinct from R1's link-discovery
target, not deleted) -- this chunk fixes ONLY the read cost per candidate,
via `_read_frontmatter_bounded`, not which files get scanned or what the
scan decides.

Negative-spec: does NOT test `_scan_deliverable_collision`'s collision
semantics (terminal-state boundary, ancestor-chain exclusion, roadmap-baton
skip, AC4 byte-identical write) -- those stay pinned by
`test_deliverable_collision_warn.py`, unchanged by this fix and deliberately
not re-asserted here.
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.baton_assemble as ba


def _write_handoff_with_body(
    root: Path, rel: str, deliverable_id: str, deployment_state: str, body_bytes: int
) -> Path:
    """A `state/handoffs/*.md` candidate whose BODY (after the closing `---`)
    is padded to `body_bytes` -- the bulk `_read_frontmatter`'s whole-file
    read used to pull off disk for nothing, since the scan only ever reads
    `deliverable_id`/`status`/`deployment_state`/`claimed_by` out of the
    frontmatter block itself."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f"deliverable_id: {deliverable_id}\n"
        "status: claimed\n"
        f"deployment_state: {deployment_state}\n"
        "claimed_by: some-session-id\n"
    )
    body = "x" * body_bytes
    path.write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return path


class TestBoundedReaderMatchesFullReader:
    """`_read_frontmatter_bounded` must return the SAME frontmatter text
    `_read_frontmatter` does, for both a large-bodied file and the edge
    cases the unbounded reader already handles (no file, no frontmatter)."""

    def test_bounded_read_matches_full_read_for_a_large_body(self, tmp_path):
        candidate = _write_handoff_with_body(
            tmp_path, "state/handoffs/large.md", "DEL-LARGE", "in_flight", 50_000
        )
        assert ba._read_frontmatter_bounded(candidate) == ba._read_frontmatter(candidate)

    def test_bounded_read_of_missing_file_is_empty(self, tmp_path):
        missing = tmp_path / "state" / "handoffs" / "absent.md"
        assert ba._read_frontmatter_bounded(missing) == ""

    def test_bounded_read_of_frontmatter_less_file_is_empty(self, tmp_path):
        path = tmp_path / "state" / "handoffs" / "no-fm.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# just a body, no delimiters\n" + ("y" * 20_000), encoding="utf-8")
        assert ba._read_frontmatter_bounded(path) == ""

    def test_bounded_read_of_delimiter_less_file_larger_than_one_chunk_is_empty(self, tmp_path):
        """No closing `---` anywhere -- the growing-buffer loop must fall
        through to EOF and return `""`, not hang or raise, even past the
        first `_FM_BOUNDED_READ_CHUNK`."""
        path = tmp_path / "state" / "handoffs" / "no-close.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ndeliverable_id: DEL-X\n" + ("z" * 20_000), encoding="utf-8")
        assert ba._read_frontmatter_bounded(path) == ""


class TestBoundedReadStaysUnderBudget:
    """The regression guard for the whole class (C14 extends this same
    axis on `builtins.open` count / process time across the three ops) --
    here, scoped to THIS scan: reading a large-bodied corpus must not pull
    the bodies off disk."""

    def test_scan_reads_far_fewer_bytes_than_the_corpus_body_size(self, tmp_path, monkeypatch):
        body_bytes = 50_000
        num_candidates = 20
        for i in range(num_candidates):
            _write_handoff_with_body(
                tmp_path,
                f"state/handoffs/candidate-{i}.md",
                "DEL-BUDGET-NO-HIT",
                "in_flight",
                body_bytes,
            )

        read_sizes: list[int] = []
        real_open = Path.open

        def _counting_open(self, *args, **kwargs):
            fh = real_open(self, *args, **kwargs)
            real_read = fh.read

            def _counting_read(size=-1):
                data = real_read(size)
                read_sizes.append(len(data))
                return data

            fh.read = _counting_read
            return fh

        monkeypatch.setattr(Path, "open", _counting_open)

        hit = ba._scan_deliverable_collision(
            "DEL-BUDGET-NO-HIT-ABSENT", tmp_path / "state" / "handoffs" / "exclude.md", tmp_path
        )
        assert hit is None

        total_read = sum(read_sizes)
        total_corpus_bytes = num_candidates * body_bytes
        assert total_read < total_corpus_bytes, (
            f"scan read {total_read} bytes across {num_candidates} candidates "
            f"each with a {body_bytes}-byte body ({total_corpus_bytes} bytes total) -- "
            "the bounded reader must stay well under the full-corpus-body size"
        )

    def test_scan_still_finds_a_collision_after_a_large_bodied_earlier_candidate(self, tmp_path):
        """The bounded reader must not short-circuit correctness: a large
        body on one candidate must not prevent the loop from reaching and
        reading a LATER candidate that actually collides."""
        _write_handoff_with_body(
            tmp_path, "state/handoffs/a-large-no-hit.md", "DEL-OTHER", "in_flight", 50_000
        )
        _write_handoff_with_body(
            tmp_path, "state/handoffs/z-large-hit.md", "DEL-COLLIDE", "in_flight", 50_000
        )
        hit = ba._scan_deliverable_collision(
            "DEL-COLLIDE", tmp_path / "state" / "handoffs" / "exclude.md", tmp_path
        )
        assert hit is not None
        assert hit["path"] == "state/handoffs/z-large-hit.md"
