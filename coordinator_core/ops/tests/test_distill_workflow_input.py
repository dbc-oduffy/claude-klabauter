
from __future__ import annotations

import asyncio

import pytest

from coordinator_core.distill.manifest_schema import make_scope_manifest
from coordinator_core.ops.distill_workflow_input import (
    CONSUMER_BATCH_FIELDS,
    CONSUMER_TOP_LEVEL_FIELDS,
    _handler,
    translate_to_workflow_input,
    validate_workflow_input,
)


def _run(coro):
    return asyncio.run(coro)


def _sample_manifest() -> dict:
    return make_scope_manifest(
        run_id="2026-08-06-15h20",
        batches=[["a.md", "b.md"], ["c.md"]],
        wiki_dirs=["docs/wiki"],
        wiki_slugs=[{"slug": "foo", "path": "docs/wiki/foo.md"}],
        cohorts={"harvest": ["a.md", "b.md", "c.md"]},
    )


def test_translate_top_level_field_renames():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert payload["runId"] == "2026-08-06-15h20"
    assert payload["repoRoot"] == "/repo"
    assert set(payload) == set(CONSUMER_TOP_LEVEL_FIELDS)


def test_translate_batches_shape_and_ids():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    batches = payload["batches"]
    assert [b["batchId"] for b in batches] == ["batch-1", "batch-2"]
    assert batches[0]["files"] == ["a.md", "b.md"]
    assert batches[1]["files"] == ["c.md"]
    for b in batches:
        assert set(b) == set(CONSUMER_BATCH_FIELDS)
        assert isinstance(b["description"], str) and b["description"]
        assert b["formatHints"] == {}


def test_translate_wiki_slugs_is_flat_object_map():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert payload["wikiSlugs"] == {"foo": "docs/wiki/foo.md"}
    assert isinstance(payload["wikiSlugs"], dict)


def test_translate_batch_count_and_total_file_count():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert payload["batch_count"] == 2
    assert payload["total_file_count"] == 3


def test_translate_format_hints_applied_uniformly():
    payload = translate_to_workflow_input(
        _sample_manifest(), repo_root="/repo", format_hints={"style": "verbose"}
    )
    assert all(b["formatHints"] == {"style": "verbose"} for b in payload["batches"])


def test_validate_well_formed_payload_clean():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    assert validate_workflow_input(payload) == []


def test_validate_catches_renamed_top_level_field():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["run_id"] = payload.pop("runId")
    errors = validate_workflow_input(payload)
    assert any("runId" in e for e in errors)


def test_validate_catches_missing_per_batch_field():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    del payload["batches"][0]["batchId"]
    errors = validate_workflow_input(payload)
    assert any("batchId" in e for e in errors)


def test_validate_catches_batch_count_integrity_mismatch():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["batch_count"] = 999
    errors = validate_workflow_input(payload)
    assert any("integrity mismatch" in e for e in errors)


def test_validate_catches_total_file_count_integrity_mismatch():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["total_file_count"] = 1
    errors = validate_workflow_input(payload)
    assert any("integrity mismatch" in e for e in errors)


def test_validate_catches_wiki_slugs_wrong_type():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/repo")
    payload["wikiSlugs"] = [{"slug": "foo", "path": "docs/wiki/foo.md"}]
    errors = validate_workflow_input(payload)
    assert any("wikiSlugs" in e for e in errors)


def test_handler_raises_on_missing_manifest():
    with pytest.raises(ValueError, match="manifest"):
        _handler({"repo_root": "/repo"}, repo_root=None)


def test_handler_raises_on_missing_repo_root():
    with pytest.raises(ValueError, match="repo_root"):
        _handler({"manifest": _sample_manifest()}, repo_root=None)


def test_dispatch_message_smoke():
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401 — triggers eager registration

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "distill.workflow_input",
        "params": {"manifest": _sample_manifest(), "repo_root": "/repo"},
    }
    d = _run(ipc.dispatch_message(msg))
    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    assert d["result"]["runId"] == "2026-08-06-15h20"
    assert d["result"]["batch_count"] == 2


def test_shard_splits_batches_and_slugs_with_matching_counts():
    import json

    from coordinator_core.ops.distill_workflow_input import SLUG_SHARD_MAX_BYTES, shard_workflow_input

    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/r")
    # ~180-byte entries, the real backlog's width: 100 of them would be ~18 KB.
    payload["wikiSlugs"] = {
        f"slug-{i:04d}-{'x' * 60}": f"docs/wiki/{'y' * 80}/s{i}.md" for i in range(250)
    }
    payload["wikiSlugCount"] = len(payload["wikiSlugs"])
    manifest, files = shard_workflow_input(payload)

    assert "batches" not in manifest and "wikiSlugs" not in manifest
    assert len(manifest["batchFiles"]) == manifest["batch_count"]
    assert sum(files[n]["file_count"] for n in manifest["batchFiles"]) == manifest["total_file_count"]
    assert all(files[n]["file_count"] == len(files[n]["files"]) for n in manifest["batchFiles"])
    shards = manifest["slugShardFiles"]
    assert len(shards) > 2 and shards[0] == "wikislugs-0001.json"
    sizes = [len(json.dumps(files[n], separators=(",", ":"), ensure_ascii=False).encode()) for n in shards]
    assert max(sizes) <= SLUG_SHARD_MAX_BYTES
    assert min(sizes[:-1]) > SLUG_SHARD_MAX_BYTES - 400  # greedy fill, not a fixed count
    merged = {k: v for n in shards for k, v in files[n]["entries"].items()}
    assert list(merged) == list(payload["wikiSlugs"])
    assert sum(files[n]["slug_count"] for n in manifest["slugShardFiles"]) == manifest["wikiSlugCount"]
    assert manifest["batchFiles"][0] == "batch-0001.json"


def test_oversize_slug_entry_gets_its_own_shard():
    from coordinator_core.ops.distill_workflow_input import SLUG_SHARD_MAX_BYTES, shard_workflow_input

    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/r")
    payload["wikiSlugs"] = {"a": "docs" + "/wiki/a.md", "huge": "z" * SLUG_SHARD_MAX_BYTES, "b": "docs" + "/wiki/b.md"}
    payload["wikiSlugCount"] = 3
    manifest, files = shard_workflow_input(payload)
    assert [list(files[n]["entries"]) for n in manifest["slugShardFiles"]] == [["a"], ["huge"], ["b"]]


def test_handler_emits_relevant_guides_per_batch_additively(tmp_path):
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "foo.md").write_text("# Quartz relay\nquartz relay shards\n", encoding="utf-8")
    (tmp_path / "docs" / "wiki" / "bar.md").write_text("# Other\nnothing\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("quartz relay notes", encoding="utf-8")
    manifest = make_scope_manifest(
        run_id="r",
        batches=[["a.md"], ["c.md"]],
        wiki_dirs=["docs/wiki"],
        wiki_slugs=[
            {"slug": "foo", "path": "docs/wiki/foo.md"},
            {"slug": "bar", "path": "docs/wiki/bar.md"},
        ],
        cohorts={"harvest": ["a.md", "c.md"]},
    )
    payload = _handler({"manifest": manifest, "repo_root": str(tmp_path)}, repo_root=None)
    assert payload["batches"][0]["relevantGuides"][0]["slug"] == "foo"
    assert payload["batches"][1]["relevantGuides"] == []
    assert payload["wikiSlugCount"] == 2 and payload["batch_count"] == 2
    assert validate_workflow_input(payload) == []


def test_relevant_guides_drop_outlines_before_the_batch_cap_refuses():
    from coordinator_core.ops.distill_workflow_input import SLUG_SHARD_MAX_BYTES, shard_workflow_input

    big = [{"slug": f"g{i}", "path": "p", "outline": ["# " + "x" * 400]} for i in range(60)]
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/r", relevant_guides=[big, []])
    kept = payload["batches"][0]["relevantGuides"]
    assert len(kept) == len(big) and all("outline" not in g for g in kept)
    shard_workflow_input(payload)  # does not raise: the hint yielded, the batch fit


def test_attach_relevant_guides_after_digests_degrades_to_paths_and_fits():
    from coordinator_core.ops.distill_workflow_input import (
        SLUG_SHARD_MAX_BYTES,
        attach_relevant_guides,
        shard_workflow_input,
    )

    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/r")
    batch = payload["batches"][0]
    batch["digests"] = {f: f"d/{'y' * 100}/{f}" for f in batch["files"]}
    guides = [{"slug": f"g{i}", "path": f"w/g{i}.md", "outline": ["# " + "x" * 300]} for i in range(60)]
    attach_relevant_guides(payload, [guides, []])
    kept = payload["batches"][0]["relevantGuides"]
    assert kept and all("outline" not in g for g in kept)
    assert [g["slug"] for g in kept] == [g["slug"] for g in guides[: len(kept)]]
    assert payload["batches"][1]["relevantGuides"] == []
    shard_workflow_input(payload)  # does not raise
    assert SLUG_SHARD_MAX_BYTES == 12_000


def test_relevant_guides_yield_entries_when_even_paths_do_not_fit():
    huge = [{"slug": f"g{i}", "path": "p" * 500, "outline": []} for i in range(60)]
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/r", relevant_guides=[huge, []])
    kept = payload["batches"][0]["relevantGuides"]
    assert 0 < len(kept) < len(huge) and kept == huge[: len(kept)]


def test_validate_catches_relevant_guides_wrong_type():
    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/r")
    payload["batches"][0]["relevantGuides"] = "foo"
    assert any("relevantGuides" in e for e in validate_workflow_input(payload))


def test_batch_over_relay_cap_refuses():
    import pytest

    from coordinator_core.ops.distill_workflow_input import SLUG_SHARD_MAX_BYTES, shard_workflow_input

    payload = translate_to_workflow_input(_sample_manifest(), repo_root="/r")
    payload["batches"][0]["files"] = ["f" * SLUG_SHARD_MAX_BYTES]
    with pytest.raises(ValueError, match="batch-0001.json"):
        shard_workflow_input(payload)


def _sized(root, rel, n):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)


def _tier_manifest(files_by_batch):
    flat = [f for b in files_by_batch for f in b]
    return make_scope_manifest(
        run_id="rt",
        batches=files_by_batch,
        wiki_dirs=["docs/wiki"],
        wiki_slugs=[{"slug": "g", "path": "docs/wiki/g.md"}],
        cohorts={"harvest": flat},
    )


def test_handler_digests_an_over_tier_batch_before_splitting(tmp_path):
    from coordinator_core.distill.run_cost import project_run_cost

    big = "# T\n" + ("prose line\n" * 20) + "```yaml plan-tasks\n" + "- id: C1\n  title: t\n  body: " + "y" * 450_000 + "\n```\n"
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "g.md").write_text("# g\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(big, encoding="utf-8")
    payload = _handler({"manifest": _tier_manifest([["a.md"]]), "repo_root": str(tmp_path)}, repo_root=None)
    assert payload["batch_count"] == 1
    assert "a.md" in payload["batches"][0]["digests"]
    cost = project_run_cost(payload, tmp_path)
    assert cost["max_scan_prompt_tokens"] <= 100_000


def test_handler_splits_when_digests_are_not_enough_and_renumbers(tmp_path):
    from coordinator_core.distill.run_cost import project_run_cost

    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "g.md").write_text("# g\n", encoding="utf-8")
    for name in ("a.md", "b.md", "c.md"):  # prose-only: the digest keeps every byte
        (tmp_path / name).write_text("prose line\n" * 15_000, encoding="utf-8")  # 165,000 B each
    payload = _handler({"manifest": _tier_manifest([["a.md", "b.md", "c.md"]]), "repo_root": str(tmp_path)}, repo_root=None)
    assert [b["batchId"] for b in payload["batches"]] == ["batch-1", "batch-2"]
    assert payload["batch_count"] == 2 and payload["total_file_count"] == 3
    assert [b["files"] for b in payload["batches"]] == [["a.md", "b.md"], ["c.md"]]
    assert sorted(f for b in payload["batches"] for f in b["files"]) == ["a.md", "b.md", "c.md"]
    assert payload["batches"][1]["description"] == "Batch 2 of 2 (1 files)"
    assert validate_workflow_input(payload) == []
    cost = project_run_cost(payload, tmp_path)
    assert max(cost["scan_prompt_tokens_by_batch"]) <= 100_000


def test_under_tier_batches_are_untouched(tmp_path):
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "g.md").write_text("# g\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("small", encoding="utf-8")
    payload = _handler({"manifest": _tier_manifest([["a.md"]]), "repo_root": str(tmp_path)}, repo_root=None)
    assert "digests" not in payload["batches"][0] and payload["batches"][0]["batchId"] == "batch-1"
    assert not (tmp_path / "state").exists()


def test_split_past_max_batches_drops_the_trailing_batches_and_never_raises(tmp_path):
    from coordinator_core.ops.distill_workflow_input import fit_scan_tier

    files = [f"f{i}.md" for i in range(5)]
    for name in files:
        _sized(tmp_path, name, 300_000)  # one per batch: each alone is under the tier
    payload = translate_to_workflow_input(_tier_manifest([files]), repo_root=str(tmp_path))
    fit_scan_tier(payload, max_batches=2)
    assert [b["files"] for b in payload["batches"]] == [files[:1], files[1:2]]
    assert payload["batch_count"] == 2 and payload["total_file_count"] == 2
    assert [b["batchId"] for b in payload["batches"]] == ["batch-1", "batch-2"]


def test_split_within_max_batches_keeps_every_file(tmp_path):
    from coordinator_core.ops.distill_workflow_input import fit_scan_tier

    files = [f"f{i}.md" for i in range(5)]
    for name in files:
        _sized(tmp_path, name, 300_000)
    for cap in (None, 5, 9):
        payload = translate_to_workflow_input(_tier_manifest([files]), repo_root=str(tmp_path))
        fit_scan_tier(payload, max_batches=cap)
        assert payload["total_file_count"] == 5 and payload["batch_count"] == 5


def test_a_manifest_already_over_the_cap_is_not_refused_when_nothing_splits(tmp_path):
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "g.md").write_text("# g\n", encoding="utf-8")
    batches = [[f"f{i}.md"] for i in range(8)]
    payload = _handler({"manifest": _tier_manifest(batches), "repo_root": str(tmp_path)}, repo_root=None)
    assert payload["batch_count"] == 8


def test_fit_scan_tier_prices_guides_into_every_split_batch(tmp_path):
    from coordinator_core.ops.distill_workflow_input import fit_scan_tier

    _sized(tmp_path, "a.md", 200_000)
    _sized(tmp_path, "b.md", 200_000)
    _sized(tmp_path, "docs/wiki/g.md", 150_000)
    payload = translate_to_workflow_input(_tier_manifest([["a.md", "b.md"]]), repo_root=str(tmp_path))
    guides = [[{"slug": "g", "path": "docs/wiki/g.md"}]]
    still_over = fit_scan_tier(payload, guides=guides)
    assert still_over == [] and payload["batch_count"] == 2
    # 350,000 B of file+guide is 87,500 tokens: each split batch carries the guide and fits.
    assert all(b["relevantGuides"] for b in payload["batches"])


def test_fit_scan_tier_lone_oversized_file_sheds_guides_and_is_reported(tmp_path):
    from coordinator_core.ops.distill_workflow_input import fit_scan_tier

    _sized(tmp_path, "huge.md", 500_000)
    _sized(tmp_path, "docs/wiki/g.md", 1_000)
    payload = translate_to_workflow_input(_tier_manifest([["huge.md"]]), repo_root=str(tmp_path))
    still_over = fit_scan_tier(payload, guides=[[{"slug": "g", "path": "docs/wiki/g.md"}]])
    assert still_over == ["batch-1"] and payload["batches"][0]["relevantGuides"] == []


def test_handler_names_a_batch_no_split_can_bring_under_the_tier(tmp_path):
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    (tmp_path / "docs" / "wiki" / "g.md").write_text("# g\n", encoding="utf-8")
    (tmp_path / "huge.md").write_text("prose line\n" * 40_000, encoding="utf-8")
    (tmp_path / "small.md").write_text("# s\n", encoding="utf-8")
    over = _handler({"manifest": _tier_manifest([["huge.md"]]), "repo_root": str(tmp_path)}, repo_root=None)
    assert over["over_tier_batches"] == ["batch-1"]
    fine = _handler({"manifest": _tier_manifest([["small.md"]]), "repo_root": str(tmp_path)}, repo_root=None)
    assert "over_tier_batches" not in fine
