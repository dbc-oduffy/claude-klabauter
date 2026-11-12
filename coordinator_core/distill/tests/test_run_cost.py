"""Tests for the distill run cost model and the benchmark's fates fixture."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.benchmarks.distill_run_cost import build_fates, disposal_manifest_for
from coordinator_core.distill import manifest_schema as schema
from coordinator_core.distill.run_cost import project_run_cost, scoped_files_sha256


def _write(root: Path, rel: str, size: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def _payload(batches: list[list[str]], guides: dict[str, str], batch_extra: dict | None = None) -> dict:
    batch_extra = batch_extra or {}
    return {
        "runId": "r",
        "repoRoot": "/r",
        "wikiDirs": ["docs/wiki"],
        "wikiSlugs": guides,
        "wikiSlugCount": len(guides),
        "batch_count": len(batches),
        "total_file_count": sum(len(b) for b in batches),
        "batches": [
            {"batchId": f"batch-{i}", "files": b, "description": "d", "formatHints": {}, **batch_extra.get(i, {})}
            for i, b in enumerate(batches, start=1)
        ],
    }


def test_dispatches_and_bytes_default_lists_every_guide_to_every_agent(tmp_path):
    for name, size in (("a.md", 100), ("b.md", 200), ("c.md", 300)):
        _write(tmp_path, name, size)
    _write(tmp_path, "docs/wiki/g1.md", 1000)
    _write(tmp_path, "docs/wiki/g2.md", 2000)
    payload = _payload([["a.md", "b.md"], ["c.md"]], {"g1": "docs/wiki/g1.md", "g2": "docs/wiki/g2.md"})

    cost = project_run_cost(payload, tmp_path)

    assert cost["dispatches_by_phase"] == {"load": 1 + 2 + 1, "scan": 2}
    assert cost["scan_bytes_split"] == {"files": 600, "guides": 2 * 3000}
    assert cost["input_bytes_by_phase"]["scan"] == 600 + 6000
    assert cost["input_bytes_by_phase"]["load"] > 0
    assert cost["unreadable_files"] == []


def test_digests_and_relevant_guides_shrink_scan_bytes_but_not_the_scoped_set(tmp_path):
    _write(tmp_path, "a.md", 5000)
    _write(tmp_path, "d/a.digest", 50)
    _write(tmp_path, "docs/wiki/g1.md", 1000)
    _write(tmp_path, "docs/wiki/g2.md", 2000)
    guides = {"g1": "docs/wiki/g1.md", "g2": "docs/wiki/g2.md"}
    before = project_run_cost(_payload([["a.md"]], guides), tmp_path)
    after = project_run_cost(
        _payload([["a.md"]], guides, {1: {"digests": {"a.md": "d/a.digest"}, "relevantGuides": ["docs/wiki/g1.md"]}}),
        tmp_path,
    )

    assert before["input_bytes_by_phase"]["scan"] == 5000 + 3000
    assert after["input_bytes_by_phase"]["scan"] == 50 + 1000
    assert after["scoped_files_sha256"] == before["scoped_files_sha256"]


def test_relevant_guides_as_emitted_entry_dicts_are_costed_by_path(tmp_path):
    _write(tmp_path, "a.md", 10)
    _write(tmp_path, "docs/wiki/g1.md", 1000)
    guides = {"g1": "docs/wiki/g1.md"}
    entry = {"slug": "g1", "path": "docs/wiki/g1.md", "outline": ["# G"]}
    cost = project_run_cost(_payload([["a.md"]], guides, {1: {"relevantGuides": [entry]}}), tmp_path)
    assert cost["scan_bytes_split"]["guides"] == 1000


def test_slug_shards_add_load_dispatches(tmp_path):
    guides = {f"slug-{i:04d}": f"docs/wiki/{'p' * 200}-{i}.md" for i in range(200)}
    cost = project_run_cost(_payload([["a.md"]], guides), tmp_path)
    shards = cost["dispatches_by_phase"]["load"] - 1 - 1
    assert shards > 1


def test_missing_file_is_listed_not_silently_zero(tmp_path):
    cost = project_run_cost(_payload([["gone.md"]], {}), tmp_path)
    assert cost["unreadable_files"] == ["gone.md"]


def test_scoped_files_sha_ignores_batch_layout():
    one = _payload([["b.md", "a.md", "c.md"]], {})
    two = _payload([["c.md"], ["a.md"], ["b.md"]], {})
    assert scoped_files_sha256(one) == scoped_files_sha256(two)
    assert scoped_files_sha256(one) != scoped_files_sha256(_payload([["a.md", "b.md"]], {}))


def test_fates_fixture_gives_every_file_exactly_one_fate_and_is_deterministic():
    files = [f"archive/handoffs/h-{i}.md" for i in range(10)]
    fates = build_fates(files)
    assert sorted(fates) == sorted(files)
    assert fates == build_fates(list(reversed(files)))
    assert set(fates.values()) <= {"NEW_GUIDE", "ALREADY_CAPTURED", "EPHEMERAL"}


def test_disposal_manifest_is_schema_valid_and_byte_stable():
    fates = build_fates([f"archive/handoffs/h-{i}.md" for i in range(9)])
    manifest = disposal_manifest_for(fates)
    assert schema.validate_disposal_manifest(manifest) == []
    assert manifest["scan_stats"] == {"total_scanned": 9, "eligible_count": 3, "retained_count": 6}
    assert schema.canonical_manifest_bytes(manifest) == schema.canonical_manifest_bytes(disposal_manifest_for(dict(fates)))
