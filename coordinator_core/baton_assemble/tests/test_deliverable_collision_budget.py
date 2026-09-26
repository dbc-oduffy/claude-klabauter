
from __future__ import annotations

from pathlib import Path

import coordinator_core.baton_assemble as ba


def _write_handoff_with_body(
    root: Path, rel: str, deliverable_id: str, deployment_state: str, body_bytes: int
) -> Path:
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
