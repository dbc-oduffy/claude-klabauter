"""
coordinator_core.ops.tests.test_distill_scope

Unit tests for coordinator_core.ops.distill_scope — the "distill.scope" op (C10).

Coverage:
  dr146_normalize / slugify_stem (ported DR-146 stem-normalization constants):
    (a) strips a leading YYYY-MM-DD- date prefix
    (b) strips exactly one of -shape/-design/-v2 (first match, listed order)
    (c) de-pluralizes a trailing non-"ss" "s"
    (d) slugify_stem collapses non-alnum runs to a single hyphen, trims edges
  compute_scope, fixture-repo golden:
    (e) harvest cohort = ripe_filter.harvest ∩ harvest_debt.harvest_debt (a ripe spec
        already logged harvested is excluded; an un-ripe undebted spec is excluded)
    (f) sidecar cohort routed via sidecar_candidate_ids (no rg spawn; the guard is
        delete-time), never via ripe_filter's own raw sidecars field
    (g) handoff resolution gate: consumed/claimed + shipped_in + not-abandoned passes;
        missing shipped_in, wrong status, or deployment_state: abandoned all SKIP
    (h) memos cohort enumerates cross-repo/archive/*.md verbatim
    (i) wiki_dirs defaults to ["docs/wiki"]; adds "coordinator/docs/wiki" when present
    (j) wiki_slugs flat index keys on slugify_stem(filename stem)
    (k) batching: scannable cohort chunked into batch_size-sized batches, chronological
        by embedded date prefix then path
  determinism (AC1):
    (l) two compute_scope() calls over the same fixture tree with the same run_id
        produce byte-identical canonical_manifest_bytes() output
  write_scope_manifest:
    (m) writes state/scratch/artifact-distillation/<run-id>/input.json, valid JSON,
        schema_version present and first key
    (n) write-confined: a second run-id's write never touches the first run-id's file
  handler:
    (o) repo_root=None raises ValueError (no silent meta-repo fallback)
    (p) missing run_id param raises ValueError (no wallclock minting)
    (q) end-to-end dispatch_message smoke via the real registered wiring

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C10
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.distill.manifest_schema import canonical_manifest_bytes
from coordinator_core.ops import distill_scope as ds
from coordinator_core.ops.distill_scope import (
    DR146_DATE_PREFIX_RE,
    DR146_MIN_STEM_LEN,
    DR146_STRIP_SUFFIXES,
    PM_RULING_2026_08_06_COHORT_SPECS,
    STEM_PREFIX_LEN,
    CohortSpec,
    _date_sort_key,
    _default_cohort_specs,
    _handler,
    compute_scope,
    scannable_count,
    dr146_normalize,
    empty_wiki_warning,
    pack_batches,
    render_summary,
    slugify_stem,
    wiki_slugs_as_dict,
    workflow_input,
    write_scope_manifest,
)
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# DR-146 constants ported byte-for-byte from distill-harvest.workflow.js
# ---------------------------------------------------------------------------


def test_dr146_constants_match_js_port():
    assert DR146_STRIP_SUFFIXES == ("-shape", "-design", "-v2")
    assert DR146_MIN_STEM_LEN == 8
    assert STEM_PREFIX_LEN == 6
    assert DR146_DATE_PREFIX_RE.match("2026-07-23-foo")


def test_dr146_normalize_strips_date_prefix():
    assert dr146_normalize("2026-07-23-foo-bar") == "foo-bar"


def test_dr146_normalize_strips_one_suffix_first_match():
    assert dr146_normalize("percolation-engine-shape") == "percolation-engine"
    assert dr146_normalize("percolation-engine-design") == "percolation-engine"
    assert dr146_normalize("percolation-engine-v2") == "percolation-engine"


def test_dr146_normalize_depluralizes_non_ss_trailing_s():
    assert dr146_normalize("guides") == "guide"
    assert dr146_normalize("process") == "process"  # ends in "ss" -> untouched


def test_slugify_stem_collapses_non_alnum_and_trims():
    assert slugify_stem("Foo_Bar Baz!!") == "foo-bar-baz"
    assert slugify_stem("--leading-trailing--") == "leading-trailing"


# ---------------------------------------------------------------------------
# Fixture-repo golden
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"

    # archive/specs: one ripe-and-undebted, one ripe-but-already-harvested,
    # one not-ripe (draft), one sidecar-suffixed with implemented status.
    _write(
        root / "archive" / "specs" / "2026-07-01-alpha.md",
        "---\nstatus: implemented\n---\nbody\n",
    )
    _write(
        root / "archive" / "specs" / "2026-07-02-beta.md",
        "---\nstatus: shipped\n---\nbody\n",
    )
    _write(
        root / "archive" / "specs" / "2026-07-03-gamma.md",
        "---\nstatus: draft\n---\nbody\n",
    )
    _write(
        root / "archive" / "specs" / "2026-07-04-delta.review.md",
        "---\nstatus: implemented\n---\nbody\n",
    )

    # canonical log: beta already harvested (DISTILLED); alpha is not.
    _write(
        root / "state" / "distillation-log.md",
        "## Run 2026-07-05-00h00\n"
        "- archive/specs/2026-07-02-beta.md -> DISTILLED, promoted (run: 2026-07-05-00h00)\n",
    )

    # archive/handoffs: one eligible, three not.
    _write(
        root / "archive" / "handoffs" / "2026-07-01-eligible.md",
        "---\nstatus: consumed\nshipped_in: abc1234\n---\nbody\n",
    )
    _write(
        root / "archive" / "handoffs" / "2026-07-02-no-shipped-in.md",
        "---\nstatus: consumed\n---\nbody\n",
    )
    _write(
        root / "archive" / "handoffs" / "2026-07-03-abandoned.md",
        "---\nstatus: consumed\nshipped_in: abc1234\ndeployment_state: abandoned\n---\nbody\n",
    )
    _write(
        root / "archive" / "handoffs" / "2026-07-04-wrong-status.md",
        "---\nstatus: open\nshipped_in: abc1234\n---\nbody\n",
    )
    _write(
        root / "archive" / "handoffs" / "2026-07-05-claimed.md",
        "---\nstatus: claimed\nshipped_in: def5678\n---\nbody\n",
    )

    # cross-repo/archive: actioned memos.
    _write(root / "cross-repo" / "archive" / "2026-07-01-memo-one.md", "memo one\n")
    _write(root / "cross-repo" / "archive" / "2026-07-02-memo-two.md", "memo two\n")

    # docs/wiki: one existing guide.
    _write(root / "docs" / "wiki" / "percolation-engine.md", "# Percolation Engine\n")

    return root


def test_compute_scope_harvest_cohort_is_ripe_and_undebted(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["cohorts"]["harvest"] == ["archive/specs/2026-07-01-alpha.md"]
    assert "archive/specs/2026-07-02-beta.md" not in result.manifest["cohorts"]["harvest"]
    assert "archive/specs/2026-07-03-gamma.md" not in result.manifest["cohorts"]["harvest"]


def test_compute_scope_sidecar_cohort_via_sweep_not_ripe_filter(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["cohorts"]["sidecars"] == [
        "archive/specs/2026-07-04-delta.review.md"
    ]
    assert "archive/specs/2026-07-04-delta.review.md" not in result.manifest["cohorts"]["harvest"]


def test_compute_scope_reports_referenced_sidecar_without_spawning_rg(fixture_repo, monkeypatch):
    """The op reports candidates and spawns nothing; the reference decision is the
    delete-time guard's, which still blocks a referenced sidecar."""
    from coordinator_core.distill.delete_guard import check_active_reference

    rel = "archive/specs/2026-07-04-delta.review.md"
    _write(fixture_repo / "docs" / "cites.md", f"see {rel}\n")

    def _no_spawn(*args, **kwargs):
        raise AssertionError(f"distill.scope spawned: {args[:1]}")

    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", _no_spawn)
        m.setattr(subprocess, "Popen", _no_spawn)
        result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")

    assert result.manifest["cohorts"]["sidecars"] == [rel]
    if shutil.which("rg"):
        assert check_active_reference(rel, fixture_repo).passed is False


def test_compute_scope_handoff_resolution_gate(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    handoffs = result.manifest["cohorts"]["handoffs"]
    assert "archive/handoffs/2026-07-01-eligible.md" in handoffs
    assert "archive/handoffs/2026-07-05-claimed.md" in handoffs
    assert "archive/handoffs/2026-07-02-no-shipped-in.md" not in handoffs
    assert "archive/handoffs/2026-07-03-abandoned.md" not in handoffs
    assert "archive/handoffs/2026-07-04-wrong-status.md" not in handoffs
    assert len(handoffs) == 2


def test_compute_scope_memos_cohort(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["cohorts"]["memos"] == [
        "cross-repo/archive/2026-07-01-memo-one.md",
        "cross-repo/archive/2026-07-02-memo-two.md",
    ]


def test_compute_scope_wiki_inventory_default_dirs(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["wiki_dirs"] == ["docs/wiki"]
    assert isinstance(result.manifest["wiki_slugs"], list)
    assert wiki_slugs_as_dict(result.manifest) == {
        "percolation-engine": "docs/wiki/percolation-engine.md"
    }


def test_compute_scope_wiki_inventory_indexes_nested_pages(fixture_repo):
    """C3 (ANWH): the navigable-hierarchy wiki nests family pages under a
    family directory. wiki_slugs must index a nested page (rglob, not the
    prior top-level-only glob), keyed and pathed exactly as a top-level page,
    and must never index a generated _index.md."""
    _write(fixture_repo / "docs" / "wiki" / "fam.md", "# Fam\n")
    _write(fixture_repo / "docs" / "wiki" / "fam" / "child.md", "# Child\n")
    _write(fixture_repo / "docs" / "wiki" / "fam" / "_index.md", "# Index\n")

    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    slugs = wiki_slugs_as_dict(result.manifest)

    assert slugs["fam"] == "docs/wiki/fam.md"
    assert slugs["child"] == "docs/wiki/fam/child.md"
    assert "index" not in slugs
    assert "docs/wiki/fam/_index.md" not in slugs.values()


def test_compute_scope_wiki_inventory_adds_coordinator_dir_when_present(fixture_repo):
    _write(
        fixture_repo / "coordinator" / "docs" / "wiki" / "other-guide.md",
        "# Other\n",
    )
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["wiki_dirs"] == ["docs/wiki", "coordinator/docs/wiki"]
    assert "other-guide" in wiki_slugs_as_dict(result.manifest)


def test_compute_scope_default_wiki_dirs_include_guides_when_present(fixture_repo):
    _write(fixture_repo / "docs" / "guides" / "league-rules.md", "# League\n")
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["wiki_dirs"] == ["docs/wiki", "docs/guides"]
    assert "league-rules" in wiki_slugs_as_dict(result.manifest)


def test_empty_wiki_warning_names_dirs_when_slugs_zero_but_guides_exist(fixture_repo):
    _write(fixture_repo / "docs" / "guides" / "league-rules.md", "# League\n")
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00", wiki_dirs=["docs/empty"])
    assert result.manifest["wiki_slugs"] == []
    warning = empty_wiki_warning(fixture_repo, result.manifest)
    assert warning is not None
    assert "docs/guides" in warning and "docs/empty" in warning


def test_empty_wiki_warning_silent_when_slugs_present_or_no_candidate_files(fixture_repo, tmp_path):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert empty_wiki_warning(fixture_repo, result.manifest) is None
    bare = tmp_path / "bare"
    bare.mkdir()
    assert empty_wiki_warning(bare, {"wiki_slugs": [], "wiki_dirs": []}) is None


def test_compute_scope_batching_respects_batch_size(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00", batch_size=2)
    batches = result.manifest["batches"]
    total = sum(len(b) for b in batches)
    assert total == result.counts["scannable"]
    assert all(len(b) <= 2 for b in batches)


def test_compute_scope_batching_chronological_order(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00", batch_size=100)
    flat = [p for batch in result.manifest["batches"] for p in batch]
    assert flat == sorted(flat, key=_date_sort_key)


def test_pack_batches_closes_batch_at_byte_budget_and_keeps_order():
    sizes = {"a": 40, "b": 40, "c": 40, "d": 10}
    batches = pack_batches(list(sizes), size_of=sizes.__getitem__, byte_budget=100, max_files=50)
    assert batches == [["a", "b"], ["c", "d"]]


def test_pack_batches_oversize_file_gets_own_batch_never_dropped():
    sizes = {"a": 10, "huge": 1000, "b": 10}
    batches = pack_batches(list(sizes), size_of=sizes.__getitem__, byte_budget=100, max_files=50)
    assert batches == [["a"], ["huge"], ["b"]]


def test_pack_batches_max_files_is_hard_cap_under_budget():
    paths = [f"p{i}" for i in range(5)]
    batches = pack_batches(paths, size_of=lambda _p: 1, byte_budget=10**9, max_files=2)
    assert batches == [["p0", "p1"], ["p2", "p3"], ["p4"]]


def test_pack_batches_empty_input_yields_no_batches():
    assert pack_batches([], size_of=lambda _p: 1, byte_budget=10, max_files=3) == []


def test_compute_scope_byte_budget_splits_by_on_disk_size(fixture_repo):
    _write(fixture_repo / "cross-repo" / "archive" / "2026-07-01-memo-one.md", "x" * 500)
    _write(fixture_repo / "cross-repo" / "archive" / "2026-07-02-memo-two.md", "y" * 500)
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00", byte_budget=600)
    batches = result.manifest["batches"]
    assert result.counts["batches"] == len(batches)
    assert sum(len(b) for b in batches) == result.counts["scannable"]
    for b in batches:
        assert "cross-repo/archive/2026-07-01-memo-one.md" not in b or "cross-repo/archive/2026-07-02-memo-two.md" not in b


def test_compute_scope_size_of_hook_replaces_on_disk_size(fixture_repo):
    solo = compute_scope(fixture_repo, run_id="2026-07-23-01h00", size_of=lambda _p: 10**9)
    assert all(len(b) == 1 for b in solo.manifest["batches"])
    assert solo.counts["batches"] == solo.counts["scannable"]


def test_pack_batches_entry_budget_is_a_third_axis():
    batches = pack_batches(
        ["a", "b", "c"], size_of=lambda _p: 1, byte_budget=10**9, max_files=50,
        entry_cost_of=lambda _p: 60, entry_budget=100,
    )
    assert batches == [["a"], ["b"], ["c"]]


def test_compute_scope_write_digests_packs_by_digest_bytes_and_maps_paths(fixture_repo):
    big = "---\nstatus: draft\n---\nprose\n```yaml plan-tasks\n- id: C0\n  title: T\n" + "  body: x\n" * 500 + "```\n"
    _write(fixture_repo / "cross-repo" / "archive" / "2026-07-01-memo-one.md", big)
    plain = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert plain.digests == {}
    digested = compute_scope(fixture_repo, run_id="2026-07-23-01h00", write_digests=True)
    assert set(digested.digests) == {p for b in digested.manifest["batches"] for p in b}
    for rel, digest_rel in digested.digests.items():
        assert digest_rel.startswith("state/scratch/artifact-distillation/2026-07-23-01h00/digest/")
        assert (fixture_repo / digest_rel).stat().st_size <= (fixture_repo / rel).stat().st_size
    # the original path stays the join key: manifest batches are unchanged by digesting
    assert digested.manifest["batches"] == plain.manifest["batches"]


def test_compute_scope_size_of_hook_overrides_digest_sizes(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00", write_digests=True, size_of=lambda _p: 10**9)
    assert all(len(b) == 1 for b in result.manifest["batches"])


def test_handler_batch_files_carry_digests_and_stay_under_relay_cap(fixture_repo):
    from coordinator_core.ops.distill_workflow_input import SLUG_SHARD_MAX_BYTES

    for i in range(60):
        _write(
            fixture_repo / "cross-repo" / "archive" / f"2026-07-{i % 28 + 1:02d}-memo-{i:03d}-{'x' * 40}.md",
            "---\ndistill_fate: ratification\n---\nbody\n",
        )
    result = _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo / ".git")
    manifest = json.loads((fixture_repo / result["workflow_input_path"]).read_text(encoding="utf-8"))
    assert manifest["batchFiles"]
    for name in manifest["batchFiles"]:
        batch_path = (fixture_repo / result["workflow_input_path"]).parent / name
        raw = batch_path.read_text(encoding="utf-8")
        batch = json.loads(raw)
        assert len(json.dumps(batch, separators=(",", ":")).encode()) <= SLUG_SHARD_MAX_BYTES
        assert set(batch["digests"]) == set(batch["files"])
        for digest_rel in batch["digests"].values():
            assert (fixture_repo / digest_rel).is_file()


def test_handler_batch_files_carry_relevant_guides_within_cap(fixture_repo):
    from coordinator_core.ops.distill_workflow_input import SLUG_SHARD_MAX_BYTES

    _write(
        fixture_repo / "archive" / "specs" / "2026-07-06-percolation-engine-notes.md",
        "---\nstatus: implemented\n---\n" + "percolation engine notes\n" * 600,
    )
    _write(
        fixture_repo / "archive" / "daily-summaries" / "2026-07-06.md",
        "- percolation engine: archive/specs/2026-07-06-percolation-engine-notes.md\n",
    )
    _write(fixture_repo / "docs" / "wiki" / "unrelated-topic.md", "# Unrelated Topic\n")
    result = _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo / ".git")
    run_dir = (fixture_repo / result["workflow_input_path"]).parent
    manifest = json.loads((fixture_repo / result["workflow_input_path"]).read_text(encoding="utf-8"))
    assert manifest["batchFiles"]
    guide_paths: set[str] = set()
    for name in manifest["batchFiles"]:
        batch = json.loads((run_dir / name).read_text(encoding="utf-8"))
        assert len(json.dumps(batch, separators=(",", ":")).encode()) <= SLUG_SHARD_MAX_BYTES
        guide_paths |= {g["path"] for g in batch["relevantGuides"]}
    assert "docs/wiki/percolation-engine.md" in guide_paths


# ---------------------------------------------------------------------------
# Data-driven cohort_specs (2026-08-06 PM ruling)
# ---------------------------------------------------------------------------


def test_default_cohort_specs_reproduces_hardcoded_behavior(fixture_repo):
    """cohort_specs=None (the default) must reproduce the pre-ruling hardcoded
    handoffs+memos cohorts byte-for-byte — the sibling-repo compatibility
    contract this chunk must not break."""
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["cohorts"]["handoffs"] == [
        "archive/handoffs/2026-07-01-eligible.md",
        "archive/handoffs/2026-07-05-claimed.md",
    ]
    assert result.manifest["cohorts"]["memos"] == [
        "cross-repo/archive/2026-07-01-memo-one.md",
        "cross-repo/archive/2026-07-02-memo-two.md",
    ]
    assert result.manifest["cohort_modes"]["handoffs"] == "harvest"
    assert result.manifest["cohort_modes"]["memos"] == "harvest"


def test_cohort_specs_invalid_mode_rejected():
    with pytest.raises(ValueError, match="mode must be one of"):
        CohortSpec(name="bad", glob="foo/*.md", mode="delete")


def test_cohort_specs_unknown_filter_rejected():
    with pytest.raises(ValueError, match="unknown filter"):
        CohortSpec(name="bad", glob="foo/*.md", filter="not_a_real_filter")


def test_pm_ruling_cohort_specs_reproduces_ruling_as_config(fixture_repo):
    """AC: the 2026-08-06 ruling is reproducible purely as config — archive/
    completed/ and state/week-changelog/ are first-class harvest cohorts,
    memos narrow to the distill_fate-promote subset, and archived handoffs
    become check-only (never batched)."""
    _write(
        fixture_repo / "archive" / "completed" / "2026-08" / "2026-08-01-done.md",
        "done\n",
    )
    _write(
        fixture_repo / "state" / "week-changelog" / "2026-08-03.md",
        "changelog\n",
    )
    _write(
        fixture_repo / "cross-repo" / "archive" / "2026-07-01-memo-one.md",
        "---\ndistill_fate: ratification\n---\nmemo one\n",
    )

    result = compute_scope(
        fixture_repo,
        run_id="2026-07-23-01h00",
        cohort_specs=list(PM_RULING_2026_08_06_COHORT_SPECS),
    )
    cohorts = result.manifest["cohorts"]
    modes = result.manifest["cohort_modes"]

    assert cohorts["completed"] == [
        "archive/completed/2026-08/2026-08-01-done.md"
    ]
    assert modes["completed"] == "harvest"
    assert "week_changelog" not in cohorts

    # memo-one now carries a promote-signal distill_fate and is included;
    # memo-two never got a distill_fate field and is excluded.
    assert cohorts["memos"] == ["cross-repo/archive/2026-07-01-memo-one.md"]

    # archived handoffs are no cohort at all, and never batched for harvest.
    assert "handoffs" not in cohorts
    flat_batched = {p for batch in result.manifest["batches"] for p in batch}
    assert "archive/handoffs/2026-07-01-eligible.md" not in flat_batched
    assert "archive/completed/2026-08/2026-08-01-done.md" in flat_batched
    assert "state/week-changelog/2026-08-03.md" not in flat_batched
    assert "cross-repo/archive/2026-07-01-memo-one.md" in flat_batched


# ---------------------------------------------------------------------------
# Determinism (AC1)
# ---------------------------------------------------------------------------


def test_compute_scope_determinism(fixture_repo):
    a = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    b = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert canonical_manifest_bytes(a.manifest) == canonical_manifest_bytes(b.manifest)


# ---------------------------------------------------------------------------
# write_scope_manifest
# ---------------------------------------------------------------------------


def test_write_scope_manifest_writes_valid_json(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    written = write_scope_manifest(fixture_repo, result.manifest)
    assert written == fixture_repo / "state" / "scratch" / "artifact-distillation" / "2026-07-23-01h00" / "input.json"
    loaded = json.loads(written.read_text(encoding="utf-8"))
    assert next(iter(loaded)) == "schema_version"
    assert loaded["run_id"] == "2026-07-23-01h00"


def test_workflow_input_matches_distill_harvest_input_shape(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00", batch_size=2)
    wf = workflow_input(result.manifest, fixture_repo)
    assert wf["runId"] == "2026-07-23-01h00"
    assert wf["wikiDirs"] == result.manifest["wiki_dirs"]
    assert isinstance(wf["wikiSlugs"], dict)
    assert [b["files"] for b in wf["batches"]] == result.manifest["batches"]
    assert wf["wikiSlugCount"] == len(wf["wikiSlugs"])


def test_write_scope_manifest_write_confined_to_own_run_id(fixture_repo):
    result_a = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    result_b = compute_scope(fixture_repo, run_id="2026-07-23-02h00")
    written_a = write_scope_manifest(fixture_repo, result_a.manifest)
    written_b = write_scope_manifest(fixture_repo, result_b.manifest)
    assert written_a != written_b
    assert written_a.exists()
    assert written_b.exists()
    assert json.loads(written_a.read_text())["run_id"] == "2026-07-23-01h00"
    assert json.loads(written_b.read_text())["run_id"] == "2026-07-23-02h00"


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def test_handler_raises_on_repo_root_none():
    with pytest.raises(ValueError, match="_origin_worktree"):
        _handler({"run_id": "2026-07-23-01h00"}, repo_root=None)


def test_handler_raises_on_missing_run_id(fixture_repo):
    with pytest.raises(ValueError, match="run_id"):
        _handler({}, repo_root=fixture_repo / ".git")


def test_dispatch_message_smoke(fixture_repo, monkeypatch):
    """End-to-end command-type dispatch via the REAL registered wiring
    (ops/__init__.py eager import + _registry_map.py + op_scopes.py + the
    @register_op decorator) — proves distill.scope is reachable exactly as a
    caller would invoke it, not just as a directly-imported Python function."""
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401 — triggers eager registration

    subprocess.run(["git", "init", "-q"], cwd=fixture_repo, check=True, **no_console_passthrough_kwargs())

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "distill.scope",
        "params": {"run_id": "2026-07-23-01h00"},
        "_origin_worktree": str(fixture_repo),
    }
    d = _run(ipc.dispatch_message(msg))
    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    assert d["result"]["run_id"] == "2026-07-23-01h00"
    assert "summary" in d["result"]


# ---------------------------------------------------------------------------
# render_summary — data-driven cohort names, not hardcoded handoffs/memos
#
# render_summary previously hardcoded
# `handoffs=`/`memos=` (defended by `.get(..., 0)`), so a caller supplying a
# fully custom cohort_specs list with different cohort names got a summary
# that silently fabricated "handoffs=0 memos=0" and never showed the
# actually-configured cohorts at all.
# ---------------------------------------------------------------------------


def test_render_summary_shows_default_handoffs_and_memos_counts(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    summary = render_summary(result.manifest, result.counts)
    assert f"handoffs={result.counts['handoffs']}" in summary
    assert f"memos={result.counts['memos']}" in summary


def test_render_summary_reflects_custom_cohort_specs_names(fixture_repo):
    custom_specs = [
        CohortSpec(name="week_changelog", glob="state/week-changelog/**/*.md", filter=None, mode="harvest"),
    ]
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00", cohort_specs=custom_specs)
    summary = render_summary(result.manifest, result.counts)

    # the custom cohort name must actually appear, with its real count
    assert f"week_changelog={result.counts['week_changelog']}" in summary
    # the old hardcoded names must NOT be fabricated as "0" — they are simply
    # absent from this run's counts and must not be silently invented
    assert "handoffs=" not in summary
    assert "memos=" not in summary


# ---------------------------------------------------------------------------
# cohort_specs handler param — malformed rows raise ValueError, not a raw
# TypeError leaking a Python implementation detail to a JSON-RPC caller.
# ---------------------------------------------------------------------------


def test_handler_cohort_specs_malformed_row_raises_value_error_not_type_error(fixture_repo):
    with pytest.raises(ValueError, match="cohort_specs"):
        _handler(
            {
                "run_id": "2026-07-23-01h00",
                "cohort_specs": [{"name": "bad", "glob": "x/*.md", "typo_field": "oops"}],
            },
            repo_root=fixture_repo / ".git",
        )


def test_handler_cohort_specs_non_dict_row_raises_value_error(fixture_repo):
    with pytest.raises(ValueError, match="cohort_specs"):
        _handler(
            {"run_id": "2026-07-23-01h00", "cohort_specs": ["not-a-dict"]},
            repo_root=fixture_repo / ".git",
        )


def test_memo_cohorts_follow_a_state_rooted_corpus(fixture_repo):
    """A repo whose memo corpus lives under state/cross-repo/ is scanned there, for the default
    cohort and for the 2026-08-06 ruling's row, which names the legacy path."""
    (fixture_repo / "state").mkdir(exist_ok=True)
    (fixture_repo / "cross-repo").rename(fixture_repo / "state" / "cross-repo")
    _write(
        fixture_repo / "state" / "cross-repo" / "archive" / "2026-07-01-memo-one.md",
        "---\ndistill_fate: ratification\n---\nmemo one\n",
    )

    default = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert default.manifest["cohorts"]["memos"] == [
        "state/cross-repo/archive/2026-07-01-memo-one.md",
        "state/cross-repo/archive/2026-07-02-memo-two.md",
    ]

    ruling = compute_scope(
        fixture_repo,
        run_id="2026-07-23-01h00",
        cohort_specs=list(PM_RULING_2026_08_06_COHORT_SPECS),
    )
    assert ruling.manifest["cohorts"]["memos"] == ["state/cross-repo/archive/2026-07-01-memo-one.md"]


# ---------------------------------------------------------------------------
# mechanical_fate triage: frontmatter-decided rows leave the scan batches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\ndistill_fate: ephemeral\n---\nbody\n", "ephemeral"),
        ('---\ndistill_fate: "ephemeral"\n---\nbody\n', "ephemeral"),
        ("---\ndistill_fate: ratification\n---\nbody\n", None),
        ("---\nstatus: consumed\n---\nbody\n", None),
        ("no frontmatter\ndistill_fate: ephemeral\n", None),
        ("---\ndistill_fate: ephemeral\n", None),
    ],
)
def test_mechanical_fate_decides_from_frontmatter_alone(text, expected):
    from coordinator_core.ops.distill_scope import mechanical_fate

    assert mechanical_fate(text) == expected


def _add_fate_rows(root: Path) -> None:
    _write(root / "cross-repo" / "archive" / "2026-07-03-memo-eph.md", "---\ndistill_fate: ephemeral\n---\nm\n")
    _write(root / "cross-repo" / "archive" / "2026-07-04-memo-promote.md", "---\ndistill_fate: commitment\n---\nm\n")
    _write(
        root / "archive" / "handoffs" / "2026-07-06-eph-handoff.md",
        "---\nstatus: consumed\nshipped_in: abc1234\ndistill_fate: ephemeral\n---\nh\n",
    )


def test_compute_scope_tags_ephemeral_rows_out_of_batches(fixture_repo):
    _add_fate_rows(fixture_repo)
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    mech = {r["path"]: r for r in result.manifest["mechanical"]}
    assert set(mech) == {
        "cross-repo/archive/2026-07-03-memo-eph.md",
        "archive/handoffs/2026-07-06-eph-handoff.md",
    }
    assert mech["cross-repo/archive/2026-07-03-memo-eph.md"] == {
        "path": "cross-repo/archive/2026-07-03-memo-eph.md",
        "cohort": "memos",
        "mechanical_fate": "ephemeral",
    }
    assert mech["archive/handoffs/2026-07-06-eph-handoff.md"]["cohort"] == "handoffs"
    batched = {p for batch in result.manifest["batches"] for p in batch}
    assert batched.isdisjoint(mech)
    assert "cross-repo/archive/2026-07-04-memo-promote.md" in batched
    assert result.counts["mechanical"] == 2
    assert result.counts["scannable"] == len(batched)
    # the cohort listing is unchanged: tagging removes batch rows, not cohort membership
    assert "cross-repo/archive/2026-07-03-memo-eph.md" in result.manifest["cohorts"]["memos"]


def test_compute_scope_without_fate_rows_emits_empty_mechanical(fixture_repo):
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["mechanical"] == []
    assert result.counts["mechanical"] == 0


def test_handler_workflow_input_excludes_mechanical_rows(fixture_repo):
    _write(fixture_repo / "cross-repo" / "archive" / "2026-07-04-memo-promote.md", "---\ndistill_fate: commitment\n---\nm\n")
    before = _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo / ".git")
    before_total = json.loads((fixture_repo / before["workflow_input_path"]).read_text(encoding="utf-8"))[
        "total_file_count"
    ]
    assert before_total == 1
    _write(fixture_repo / "archive" / "completed" / "2026-07-07-eph.md", "---\ndistill_fate: ephemeral\n---\nc\n")
    _write(fixture_repo / "cross-repo" / "archive" / "2026-07-08-memo-promote-two.md", "---\ndistill_fate: commitment\n---\nm\n")
    after = _handler({"run_id": "2026-07-23-02h00"}, repo_root=fixture_repo / ".git")
    payload = json.loads((fixture_repo / after["workflow_input_path"]).read_text(encoding="utf-8"))
    assert after["counts"]["mechanical"] == 1
    # +1 promote memo batched; the ephemeral completed row is not
    assert payload["total_file_count"] == before_total + 1
    assert "mechanical" in after["summary"]


def _add_plans(root: Path) -> None:
    _write(root / "docs" / "plans" / "2026-11-01-a.md", "---\nstatus: implemented\n---\nbody\n")
    _write(root / "docs" / "plans" / "2026-11-02-b.md", "---\nstatus: draft\n---\nbody\n")
    _write(root / "docs" / "plans" / "2026-11-03-c.md", "---\nstatus: implemented\n---\nbody\n")
    _write(root / "docs" / "plans" / "2026-11" / "2026-11-04-d.md", "---\nstatus: implemented\n---\nbody\n")
    _write(root / "docs" / "plans" / "sidecars" / "x" / "run.md", "---\nstatus: implemented\n---\nbody\n")
    _write(root / "docs" / "plans" / "2026-11-05-e.review.md", "---\nstatus: implemented\n---\nbody\n")


def test_plans_cohort_holds_implemented_undebted_plans_only(fixture_repo):
    _add_plans(fixture_repo)
    log = fixture_repo / "state" / "distillation-log.md"
    log.write_text(
        log.read_text(encoding="utf-8")
        + "- docs/plans/2026-11-03-c.md -> DISTILLED, promoted (run: 2026-07-05-00h00)\n",
        encoding="utf-8",
    )
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["cohorts"]["plans"] == [
        "docs/plans/2026-11-01-a.md",
        "docs/plans/2026-11/2026-11-04-d.md",
    ]
    assert result.manifest["cohort_modes"]["plans"] == "harvest"
    assert result.counts["plans"] == 2
    batched = {p for batch in result.manifest["batches"] for p in batch}
    assert {"docs/plans/2026-11-01-a.md", "docs/plans/2026-11/2026-11-04-d.md"} <= batched
    assert not any(p.startswith("docs/plans/sidecars/") or p.endswith(".review.md") for p in batched)
    assert "docs/plans/2026-11-02-b.md" not in batched
    assert "docs/plans/2026-11-03-c.md" not in batched
    # the specs cohort stays specs-only, and plans' non-ripe rows never reach `skip`
    assert result.manifest["cohorts"]["harvest"] == ["archive/specs/2026-07-01-alpha.md"]
    assert not any(p.startswith("docs/plans/") for p in result.manifest["cohorts"]["skip"])


def test_plans_cohort_a_nested_log_row_credits_the_dated_subdir_plan(fixture_repo):
    _add_plans(fixture_repo)
    log = fixture_repo / "state" / "distillation-log.md"
    log.write_text(
        log.read_text(encoding="utf-8")
        + "- docs/plans/2026-11/2026-11-04-d.md -> DISTILLED, promoted (run: 2026-07-05-00h00)\n",
        encoding="utf-8",
    )
    cohort = compute_scope(fixture_repo, run_id="r").manifest["cohorts"]["plans"]
    assert "docs/plans/2026-11/2026-11-04-d.md" not in cohort
    assert "docs/plans/2026-11-01-a.md" in cohort


def test_plans_cohort_an_ephemeral_log_row_is_not_a_harvest(fixture_repo):
    _add_plans(fixture_repo)
    log = fixture_repo / "state" / "distillation-log.md"
    log.write_text(
        log.read_text(encoding="utf-8") + "- docs/plans/2026-11-01-a.md -> EPHEMERAL, no durable knowledge\n",
        encoding="utf-8",
    )
    assert "docs/plans/2026-11-01-a.md" in compute_scope(fixture_repo, run_id="r").manifest["cohorts"]["plans"]


def test_plans_cohort_is_empty_without_a_plans_dir(fixture_repo):
    result = compute_scope(fixture_repo, run_id="r")
    assert result.manifest["cohorts"]["plans"] == []
    assert result.counts["plans"] == 0


def test_scannable_count_equals_compute_scope_scannable_at_pm_ruling(fixture_repo):
    _add_plans(fixture_repo)
    _add_fate_rows(fixture_repo)
    _write(
        fixture_repo / "docs" / "plans" / "2026-11-06-eph.md",
        "---\nstatus: implemented\ndistill_fate: ephemeral\n---\nbody\n",
    )
    _write(
        fixture_repo / "archive" / "daily-summaries" / "2026-11-07.md",
        "- shipped docs/plans/2026-11-01-a.md and docs/plans/2026-11-06-eph.md\n",
    )
    scope = compute_scope(
        fixture_repo, run_id="r", cohort_specs=list(PM_RULING_2026_08_06_COHORT_SPECS), admit=True
    )
    assert scannable_count(fixture_repo) == scope.counts["scannable"] + scope.counts["deferred"]
    assert scope.counts["plans"] == 4
    assert scope.counts["mechanical"] == 1  # the ephemeral plan; the ephemeral memo/handoff are not in this cohort set
    assert "docs/plans/2026-11-06-eph.md" not in {p for b in scope.manifest["batches"] for p in b}


def _fresh_repo_with_log(fixture_repo: Path, log_text: str | None) -> None:
    log = fixture_repo / "state" / "distillation-log.md"
    if log_text is None:
        log.unlink()
    else:
        log.write_text(log_text, encoding="utf-8")


def test_an_absent_log_is_an_empty_baseline_not_a_refusal(fixture_repo):
    _fresh_repo_with_log(fixture_repo, None)
    scope = compute_scope(fixture_repo, run_id="r")
    assert "archive/specs/2026-07-01-alpha.md" in scope.manifest["cohorts"]["harvest"]
    # beta was credited only by the log that no longer exists: it is undebted again.
    assert "archive/specs/2026-07-02-beta.md" in scope.manifest["cohorts"]["harvest"]
    admitted = compute_scope(
        fixture_repo, run_id="r2", cohort_specs=list(PM_RULING_2026_08_06_COHORT_SPECS), admit=True
    )
    assert scannable_count(fixture_repo) == admitted.counts["scannable"] + admitted.counts["deferred"]
    assert not (fixture_repo / "state" / "distillation-log.md").exists()


def test_a_header_only_log_is_an_empty_baseline(fixture_repo):
    _fresh_repo_with_log(fixture_repo, "# Distillation Log\n\n## Run 2026-07-05-00h00\n")
    scope = compute_scope(fixture_repo, run_id="r")
    assert "archive/specs/2026-07-02-beta.md" in scope.manifest["cohorts"]["harvest"]


def test_a_log_with_content_and_no_parseable_row_still_refuses(fixture_repo):
    from coordinator_core.distill.harvest_debt import DistillationLogUnparseableError

    _fresh_repo_with_log(fixture_repo, "some prose that is no table and no row\n")
    with pytest.raises(DistillationLogUnparseableError):
        compute_scope(fixture_repo, run_id="r")
    with pytest.raises(DistillationLogUnparseableError):
        scannable_count(fixture_repo)


def test_the_handler_scopes_an_absent_log_repo(fixture_repo):
    _fresh_repo_with_log(fixture_repo, None)
    result = _handler({"run_id": "fresh"}, repo_root=fixture_repo / ".git")
    assert (fixture_repo / result["input_json_path"]).is_file()
    assert not (fixture_repo / "state" / "distillation-log.md").exists()


def test_the_handler_scopes_params_repo_root_not_the_injected_repo(fixture_repo, tmp_path):
    other = tmp_path / "other"
    (other / "archive" / "specs").mkdir(parents=True)
    result = _handler({"run_id": "r", "repo_root": str(fixture_repo / ".git")}, repo_root=other / ".git")
    assert (fixture_repo / result["input_json_path"]).is_file()
    assert not (other / "state").exists()


def test_the_handler_without_params_repo_root_scopes_the_injected_repo(fixture_repo, tmp_path):
    other = tmp_path / "other"
    (other / "archive" / "specs").mkdir(parents=True)
    result = _handler({"run_id": "r"}, repo_root=other / ".git")
    assert (other / result["input_json_path"]).is_file()
    assert not (fixture_repo / "state" / "scratch").exists()


def test_scannable_count_cap_stops_at_cap_and_is_exact_below_it(fixture_repo):
    for day in range(1, 5):
        _write(fixture_repo / "archive" / "daily-summaries" / f"2026-11-0{day}.md", "- a day\n")
    full = scannable_count(fixture_repo)
    assert full > 2
    assert scannable_count(fixture_repo, cap=2) == 2
    assert scannable_count(fixture_repo, cap=full) == full
    assert scannable_count(fixture_repo, cap=full + 10) == full


# ---------------------------------------------------------------------------
# a repeat scope call replaces the prior unconsumed scope dir
# ---------------------------------------------------------------------------


def _scope_dirs(repo: Path) -> list[str]:
    base = repo / "state" / "scratch" / "artifact-distillation"
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _dispatch_scope(repo: Path, run_id: str) -> dict:
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "distill.scope",
        "params": {"run_id": run_id},
        "_origin_worktree": str(repo),
    }
    d = _run(ipc.dispatch_message(msg))
    assert "result" in d, d.get("error")
    return d


def _age(repo: Path, run_id: str, seconds: float = 2 * 86400) -> None:
    d = repo / "state" / "scratch" / "artifact-distillation" / run_id
    old = d.stat().st_mtime - seconds
    for p in [d, *d.iterdir()]:
        os.utime(p, (old, old))


def test_two_scope_calls_through_dispatch_leave_one_run_dir(fixture_repo):
    _git_init(fixture_repo)
    _dispatch_scope(fixture_repo, "2026-07-23-01h00")
    _age(fixture_repo, "2026-07-23-01h00")
    _dispatch_scope(fixture_repo, "2026-07-23-02h00")
    assert _scope_dirs(fixture_repo) == ["2026-07-23-02h00"]


def test_back_to_back_scope_calls_leave_one_dir(fixture_repo):
    _git_init(fixture_repo)
    _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo)
    _age(fixture_repo, "2026-07-23-01h00")
    _handler({"run_id": "2026-07-23-02h00"}, repo_root=fixture_repo)
    assert _scope_dirs(fixture_repo) == ["2026-07-23-02h00"]


def test_scope_keeps_a_young_unconsumed_dir_a_live_run_may_still_read(fixture_repo):
    _git_init(fixture_repo)
    _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo)
    _handler({"run_id": "2026-07-23-02h00"}, repo_root=fixture_repo)
    assert _scope_dirs(fixture_repo) == ["2026-07-23-01h00", "2026-07-23-02h00"]


def test_rescoping_a_consumed_same_run_id_clears_stale_downstream_files(fixture_repo):
    _git_init(fixture_repo)
    _handler({"run_id": "autofire"}, repo_root=fixture_repo)
    d = fixture_repo / "state" / "scratch" / "artifact-distillation" / "autofire"
    (d / "disposal-manifest.json").write_text("{}", encoding="utf-8")
    _handler({"run_id": "autofire"}, repo_root=fixture_repo)
    assert not (d / "disposal-manifest.json").exists()
    assert (d / "input.json").is_file()


def test_scope_keeps_a_prior_dir_a_downstream_step_has_written_into(fixture_repo):
    _git_init(fixture_repo)
    _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo)
    consumed = fixture_repo / "state" / "scratch" / "artifact-distillation" / "2026-07-23-01h00"
    (consumed / "disposal-manifest.json").write_text("{}", encoding="utf-8")
    _handler({"run_id": "2026-07-23-02h00"}, repo_root=fixture_repo)
    assert _scope_dirs(fixture_repo) == ["2026-07-23-01h00", "2026-07-23-02h00"]


def test_scope_keeps_non_scope_siblings(fixture_repo):
    _git_init(fixture_repo)
    base = fixture_repo / "state" / "scratch" / "artifact-distillation"
    probe = base / "perf-probe"
    probe.mkdir(parents=True)
    (probe / "note.txt").write_text("x", encoding="utf-8")
    (base / "loose.params.json").write_text("{}", encoding="utf-8")
    _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo)
    assert _scope_dirs(fixture_repo) == ["2026-07-23-01h00", "perf-probe"]
    assert (base / "loose.params.json").exists()


def test_rescoping_the_same_run_id_keeps_its_dir(fixture_repo):
    _git_init(fixture_repo)
    _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo)
    _handler({"run_id": "2026-07-23-01h00"}, repo_root=fixture_repo)
    assert _scope_dirs(fixture_repo) == ["2026-07-23-01h00"]


# ---------------------------------------------------------------------------
# Incremental scope: the per-file stat cache serves the same scope a cache-less run computes
# ---------------------------------------------------------------------------

_HOUR_NS = 3600 * 10**9


@pytest.fixture(autouse=True)
def _hermetic_cache_root(monkeypatch):
    monkeypatch.setattr(ds, "registry_get", lambda _key: None)


@pytest.fixture()
def wide_budget(monkeypatch):
    """A run budget roomy enough that the default fit never narrows a small fixture."""
    from coordinator_core.contract.run_budget import RunBudget

    monkeypatch.setattr(ds._run_budget, "load", lambda _ceremony, path=None: RunBudget(1000, 100_000_000, 10_000))


def _set_mtime(path: Path, ago_ns: int) -> None:
    t = time.time_ns() - ago_ns
    os.utime(path, ns=(t, t))


def _age_tree(root: Path, ago_ns: int = _HOUR_NS) -> None:
    """Move every file's mtime out of the racy window, so its cache entry is storable."""
    for p in root.rglob("*"):
        if p.is_file():
            _set_mtime(p, ago_ns)


def _edit(path: Path, text: str, ago_ns: int = _HOUR_NS // 2) -> None:
    _write(path, text)
    _set_mtime(path, ago_ns)


def _cache_dir(repo: Path) -> Path:
    return repo / ".coordinator-local" / "cache" / "distill-scope"


def _drop_cache(repo: Path) -> None:
    shutil.rmtree(_cache_dir(repo), ignore_errors=True)


def _snapshot(result, run_id: str) -> dict:
    """The scope a run produced: manifest, counts and digest paths, run id normalised away."""
    raw = json.dumps(
        {"manifest": result.manifest, "counts": result.counts, "digests": result.digests}, sort_keys=True
    )
    return json.loads(raw.replace(run_id, "RUN"))


def _no_source_reads(monkeypatch) -> None:
    """Fail on any read of a source file or any digest build: only a stat may serve a warm scope."""

    def boom(*_a, **_k):
        raise AssertionError("a warm scope re-read a source file")

    monkeypatch.setattr(ds._ripe_filter, "_classify_one", boom)
    monkeypatch.setattr(ds, "_head_fate", boom)
    monkeypatch.setattr(ds._scan_digest, "build_digest", boom)
    monkeypatch.setattr(ds, "parse_distillation_log", boom)
    for name in list(ds._COHORT_FILTERS):
        monkeypatch.setitem(ds._COHORT_FILTERS, name, boom)


def test_cold_and_warm_scope_are_identical_and_the_warm_one_reads_no_source(fixture_repo, monkeypatch):
    _age_tree(fixture_repo)
    cold = compute_scope(fixture_repo, run_id="R", write_digests=True)
    assert (_cache_dir(fixture_repo)).is_dir()
    with monkeypatch.context() as m:
        _no_source_reads(m)
        warm = compute_scope(fixture_repo, run_id="R", write_digests=True)
    assert _snapshot(warm, "R") == _snapshot(cold, "R")
    _drop_cache(fixture_repo)
    assert _snapshot(compute_scope(fixture_repo, run_id="R", write_digests=True), "R") == _snapshot(cold, "R")


def test_stale_entries_serve_the_same_scope_a_cache_less_run_computes(fixture_repo):
    """Touch (same size, new mtime), delete, add, and edit the log: each change shows up in the
    cached scope exactly as in a scope computed with no cache at all."""
    _age_tree(fixture_repo)
    compute_scope(fixture_repo, run_id="R", write_digests=True)

    def assert_matches_cache_less(label: str) -> None:
        cached = compute_scope(fixture_repo, run_id="R", write_digests=True)
        _drop_cache(fixture_repo)
        fresh = compute_scope(fixture_repo, run_id="R", write_digests=True)
        assert _snapshot(cached, "R") == _snapshot(fresh, "R"), label

    specs = fixture_repo / "archive" / "specs"
    # same size ("shipped" -> "partial"), new mtime: only the stat key can tell
    _edit(specs / "2026-07-02-beta.md", "---\nstatus: partial\n---\nbody\n")
    assert_matches_cache_less("touched")
    compute_scope(fixture_repo, run_id="R", write_digests=True)
    (fixture_repo / "cross-repo" / "archive" / "2026-07-01-memo-one.md").unlink()
    assert_matches_cache_less("deleted")
    compute_scope(fixture_repo, run_id="R", write_digests=True)
    _edit(fixture_repo / "cross-repo" / "archive" / "2026-07-09-memo-new.md", "new memo\n")
    _edit(fixture_repo / "archive" / "handoffs" / "2026-07-09-new-eligible.md", "---\nstatus: claimed\nshipped_in: a1\n---\n")
    assert_matches_cache_less("added")
    compute_scope(fixture_repo, run_id="R", write_digests=True)
    log = fixture_repo / "state" / "distillation-log.md"
    _edit(
        log,
        log.read_text(encoding="utf-8")
        + "- archive/specs/2026-07-01-alpha.md -> DISTILLED, promoted (run: 2026-07-05-00h00)\n",
    )
    cached = compute_scope(fixture_repo, run_id="R", write_digests=True)
    assert "archive/specs/2026-07-01-alpha.md" not in cached.manifest["cohorts"]["harvest"]
    assert_matches_cache_less("log edited")


def test_a_run_dir_wiped_between_runs_gets_its_digests_back_without_rereading_sources(fixture_repo, monkeypatch):
    _age_tree(fixture_repo)
    first = compute_scope(fixture_repo, run_id="R", write_digests=True)
    expected = {rel: (fixture_repo / d).read_bytes() for rel, d in first.digests.items()}
    assert expected
    shutil.rmtree(fixture_repo / "state" / "scratch" / "artifact-distillation" / "R")
    with monkeypatch.context() as m:
        _no_source_reads(m)
        again = compute_scope(fixture_repo, run_id="R", write_digests=True)
        other = compute_scope(fixture_repo, run_id="S", write_digests=True)
    for result in (again, other):
        assert {rel: (fixture_repo / d).read_bytes() for rel, d in result.digests.items()} == expected


def test_an_unchanged_digest_is_not_rewritten_and_a_tampered_one_is(fixture_repo, monkeypatch):
    _age_tree(fixture_repo)
    first = compute_scope(fixture_repo, run_id="R", write_digests=True)
    rel, digest_rel = next(iter(first.digests.items()))
    target = fixture_repo / digest_rel
    written: list[str] = []
    real = ds._write_digest_file
    monkeypatch.setattr(ds, "_write_digest_file", lambda d, name, body: written.append(name) or real(d, name, body))
    compute_scope(fixture_repo, run_id="R", write_digests=True)
    assert written == []
    original = target.read_bytes()
    target.write_bytes(original + b"tampered")
    again = compute_scope(fixture_repo, run_id="R", write_digests=True)
    assert written == [target.name]
    assert target.read_bytes() == original
    assert again.digests == first.digests


def test_a_changed_fact_deriving_function_discards_the_cache(fixture_repo, monkeypatch):
    _age_tree(fixture_repo)
    compute_scope(fixture_repo, run_id="R")
    calls: list[str] = []
    real = ds._head_fate
    monkeypatch.setattr(ds, "_head_fate", lambda path: calls.append(str(path)) or real(path))
    compute_scope(fixture_repo, run_id="R")
    assert calls == []
    ds._code_fingerprint.cache_clear()
    monkeypatch.setattr(ds, "_handoff_eligible", lambda text: False)
    try:
        assert ds._code_fingerprint() != _fingerprint_of_original()
        compute_scope(fixture_repo, run_id="R")
        assert calls, "a changed _handoff_eligible must not be served from the old cache"
    finally:
        ds._code_fingerprint.cache_clear()


def _fingerprint_of_original() -> str:
    return _ORIGINAL_FINGERPRINT


_ORIGINAL_FINGERPRINT = ds._code_fingerprint()


def test_a_schema_mismatch_in_the_stored_cache_is_a_full_miss_not_an_error(fixture_repo):
    _age_tree(fixture_repo)
    cold = compute_scope(fixture_repo, run_id="R")
    path = next(_cache_dir(fixture_repo).glob("facts-*.json"))
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["files"]
    stored["schema"] = "0:stale"
    path.write_text(json.dumps(stored), encoding="utf-8")
    assert _snapshot(compute_scope(fixture_repo, run_id="R"), "R") == _snapshot(cold, "R")
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == ds._code_fingerprint()
    path.write_text("{not json", encoding="utf-8")
    assert _snapshot(compute_scope(fixture_repo, run_id="R"), "R") == _snapshot(cold, "R")


def test_an_entry_modified_inside_the_racy_window_is_never_stored(fixture_repo):
    """Files created just now sit inside the window where a same-size rewrite could keep the
    same stat; their facts are recomputed every run instead of cached."""
    compute_scope(fixture_repo, run_id="R")
    stored = json.loads(next(_cache_dir(fixture_repo).glob("facts-*.json")).read_text(encoding="utf-8"))
    assert stored["files"] == {}


def test_the_log_entry_obeys_the_racy_window_like_file_entries(fixture_repo):
    """A log modified inside the window is never stored; once aged out it is."""
    _age_tree(fixture_repo)
    log = fixture_repo / "state" / "distillation-log.md"
    _set_mtime(log, 0)
    compute_scope(fixture_repo, run_id="R")
    path = next(_cache_dir(fixture_repo).glob("facts-*.json"))
    assert json.loads(path.read_text(encoding="utf-8"))["log"] == {}
    _set_mtime(log, _HOUR_NS)
    compute_scope(fixture_repo, run_id="R")
    assert json.loads(path.read_text(encoding="utf-8"))["log"]["canonical"]


def test_a_failed_cache_store_is_not_an_error(fixture_repo):
    _age_tree(fixture_repo)
    expected = _snapshot(compute_scope(fixture_repo, run_id="R", write_digests=True), "R")
    shutil.rmtree(_cache_dir(fixture_repo).parent)
    _cache_dir(fixture_repo).parent.write_text("a file where the cache dir belongs", encoding="utf-8")
    assert _snapshot(compute_scope(fixture_repo, run_id="R", write_digests=True), "R") == expected


def test_the_cache_root_follows_the_settings_registry_key(fixture_repo, tmp_path, monkeypatch):
    base = tmp_path / "elsewhere"
    monkeypatch.setattr(ds, "registry_get", lambda key: str(base) if key == "paths.distill_scope_cache" else None)
    _age_tree(fixture_repo)
    compute_scope(fixture_repo, run_id="R", write_digests=True)
    assert list((base / "distill-scope").glob("facts-*.json"))
    assert not (fixture_repo / ".coordinator-local").exists()


def test_a_warm_scope_spawns_nothing(fixture_repo, monkeypatch):
    _age_tree(fixture_repo)
    compute_scope(fixture_repo, run_id="R", write_digests=True)

    def no_spawn(*args, **kwargs):
        raise AssertionError(f"distill.scope spawned: {args[:1]}")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    compute_scope(fixture_repo, run_id="R", write_digests=True)


def test_scandir_cohorts_equal_the_library_oracle_the_scope_used_before(fixture_repo):
    """harvest / skip / sidecars equal ripe_filter + harvest_debt + sidecar_sweep over a tree with
    nested month dirs, a non-markdown file, a legacy action-table row and an ambiguous basename."""
    from coordinator_core.distill.harvest_debt import compute_harvest_debt
    from coordinator_core.distill.ripe_filter import scan_spec_dir
    from coordinator_core.distill.sidecar_sweep import sidecar_candidate_ids

    specs = fixture_repo / "archive" / "specs"
    _write(specs / "2026-06" / "2026-06-01-nested.md", "---\nstatus: implemented\n---\nx\n")
    _write(specs / "2026-06" / "2026-06-02-dupe.md", "---\nstatus: implemented\n---\nx\n")
    _write(specs / "2026-07" / "2026-06-02-dupe.md", "---\nstatus: implemented\n---\nx\n")
    _write(specs / "2026-06" / "2026-06-03-legacy.md", "---\nstatus: implemented\n---\nx\n")
    _write(specs / "2026-06" / "notes.txt", "not markdown\n")
    _write(specs / "2026-06" / "2026-06-04-nofm.md", "no frontmatter\n")
    _write(specs / "2026-06" / "2026-06-05-x.plan-coverage-check.md", "---\nstatus: implemented\n---\n")
    log = fixture_repo / "state" / "distillation-log.md"
    log.write_text(
        log.read_text(encoding="utf-8")
        + "# Columns: date | action | path | last_sha | belongs_to_spec | reason\n"
        + "2026-06-10 | harvested | archive/specs/old-place/2026-06-03-legacy.md | abc | - | r\n"
        + "2026-06-10 | harvested | archive/specs/old-place/2026-06-02-dupe.md | abc | - | r\n",
        encoding="utf-8",
    )
    result = compute_scope(fixture_repo, run_id="R")
    ripe = scan_spec_dir(specs)
    debt = compute_harvest_debt(specs, log)
    assert result.manifest["cohorts"]["harvest"] == sorted(
        f"archive/specs/{p}" for p in set(ripe.harvest) & set(debt.harvest_debt)
    )
    assert result.manifest["cohorts"]["skip"] == sorted(f"archive/specs/{r.path}" for r in ripe.skip)
    assert result.manifest["cohorts"]["sidecars"] == sorted(sidecar_candidate_ids(specs, fixture_repo))
    assert result.counts["harvest_debt_total"] == debt.total_specs
    assert result.counts["harvest_debt_warn"] == int(debt.warn)
    assert "archive/specs/2026-06/2026-06-03-legacy.md" not in result.manifest["cohorts"]["harvest"]
    assert "archive/specs/2026-06/2026-06-02-dupe.md" in result.manifest["cohorts"]["harvest"]


def test_handler_serves_guides_from_cache_and_ranks_only_the_batches_that_changed(
    fixture_repo, monkeypatch, wide_budget
):
    from coordinator_core.distill import guide_index

    for i in range(6):
        _write(
            fixture_repo / "cross-repo" / "archive" / f"2026-07-{10 + i:02d}-memo-{i}.md",
            f"---\ndistill_fate: ratification\n---\npercolation engine memo {i}\n",
        )
    _age_tree(fixture_repo)
    params = {"run_id": "R", "batch_size": 2}
    first = _handler(dict(params), repo_root=fixture_repo / ".git")
    run_dir = (fixture_repo / first["workflow_input_path"]).parent

    def batch_files() -> dict[str, str]:
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(run_dir.glob("batch-*.json"))}

    expected = batch_files()
    assert len(expected) > 2
    real = guide_index.relevant_guides_for_batches
    ranked: list[list[list[str]]] = []
    monkeypatch.setattr(
        guide_index,
        "relevant_guides_for_batches",
        lambda root, batches, slugs, **kw: ranked.append(batches) or real(root, batches, slugs, **kw),
    )
    _handler(dict(params), repo_root=fixture_repo / ".git")
    assert ranked == []
    assert batch_files() == expected

    victim = fixture_repo / "cross-repo" / "archive" / "2026-07-12-memo-2.md"
    _edit(victim, "---\ndistill_fate: ratification\n---\npercolation engine memo two, rewritten\n")
    _handler(dict(params), repo_root=fixture_repo / ".git")
    (reranked,) = ranked
    assert all(len(batch) <= 2 for batch in reranked)
    assert len(reranked) == 1 and "cross-repo/archive/2026-07-12-memo-2.md" in reranked[0]

    ranked.clear()
    _edit(fixture_repo / "docs" / "wiki" / "percolation-engine.md", "# Percolation Engine\n\nchanged\n")
    _handler(dict(params), repo_root=fixture_repo / ".git")
    (all_batches,) = ranked
    assert len(all_batches) == len(expected)


# ---------------------------------------------------------------------------
# Admission: logs first, cited and heavy plans only, a batch budget
# ---------------------------------------------------------------------------

_HEAVY = "weighty plan body\n" * 800
_PM_SPECS = list(PM_RULING_2026_08_06_COHORT_SPECS)


def _admit_scope(root: Path, **kw):
    return compute_scope(root, run_id="r", cohort_specs=_PM_SPECS, admit=True, **kw)


def _heavy_plan(root: Path, name: str, extra: str = "") -> str:
    rel = f"docs/plans/{name}.md"
    _write(root / rel, f"---\nstatus: implemented\n---\n{extra}{_HEAVY}")
    return rel


def _log(root: Path, day: str, *cites: str) -> str:
    rel = f"archive/daily-summaries/{day}.md"
    _write(root / rel, "".join(f"- moved the needle: {c}\n" for c in cites) or "- a quiet day\n")
    return rel


def _not_admitted(scope) -> dict[str, str]:
    return {row["path"]: row["reason"] for row in scope.manifest["not_admitted"]}


def _batched(scope) -> list[str]:
    return [p for batch in scope.manifest["batches"] for p in batch]


def test_cite_keys_name_plans_by_dated_stem_or_path():
    text = (
        "see docs/plans/2026-11/2026-11-13-Big-Refactor.md and 2026-11-14-new-system-ab12cd, "
        "also archive/specs/old_spec.md; ignore docs/wiki/guide.md"
    )
    assert ds._cite_keys(text) == [
        "2026-11-13-big-refactor",
        "2026-11-14-new-system-ab12cd",
        "old-spec",
    ]


def test_admission_takes_logs_first_newest_first_then_cited_plans(fixture_repo):
    cited = _heavy_plan(fixture_repo, "2026-11-01-big")
    _log(fixture_repo, "2026-11-02", cited)
    _log(fixture_repo, "2026-11-03", "nothing of note")
    scope = _admit_scope(fixture_repo)
    assert _batched(scope) == [
        "archive/daily-summaries/2026-11-03.md",
        "archive/daily-summaries/2026-11-02.md",
        cited,
    ]
    assert scope.manifest["cohorts"]["logs"] == [
        "archive/daily-summaries/2026-11-02.md",
        "archive/daily-summaries/2026-11-03.md",
    ]
    assert scope.counts["log_cited_plan_count"] == 1


def test_admission_refuses_uncited_plans_and_light_plans_and_names_why(fixture_repo):
    uncited = _heavy_plan(fixture_repo, "2026-11-01-uncited")
    light = "docs/plans/2026-11-02-light.md"
    _write(fixture_repo / light, "---\nstatus: implemented\n---\nsmall\n")
    _log(fixture_repo, "2026-11-03", light)
    scope = _admit_scope(fixture_repo)
    reasons = _not_admitted(scope)
    assert reasons[uncited] == "not_log_cited"
    assert reasons[light] == "below_admission"
    assert reasons["archive/specs/2026-07-01-alpha.md"] == "not_log_cited"
    assert uncited not in _batched(scope) and light not in _batched(scope)
    assert scope.counts["not_admitted"] == len(reasons)


def test_admission_refuses_what_a_guide_or_decision_already_cites(fixture_repo):
    in_guide = _heavy_plan(fixture_repo, "2026-11-01-in-guide")
    in_decision = _heavy_plan(fixture_repo, "2026-11-02-in-decision")
    fresh = _heavy_plan(fixture_repo, "2026-11-03-fresh")
    _log(fixture_repo, "2026-11-04", in_guide, in_decision, fresh)
    _write(fixture_repo / "docs" / "wiki" / "guide.md", "Distilled from docs/plans/2026-11-01-in-guide.md\n")
    _write(fixture_repo / "docs" / "decisions" / "DR-1.md", "Ratifies 2026-11-02-in-decision\n")
    scope = _admit_scope(fixture_repo)
    reasons = _not_admitted(scope)
    assert reasons[in_guide] == "already_captured"
    assert reasons[in_decision] == "already_captured"
    assert fresh in _batched(scope)


def test_a_harvested_log_is_no_target_and_its_citations_admit_nothing(fixture_repo):
    plan = _heavy_plan(fixture_repo, "2026-11-01-big")
    done = _log(fixture_repo, "2026-11-02", plan)
    log = fixture_repo / "state" / "distillation-log.md"
    log.write_text(
        log.read_text(encoding="utf-8") + f"- {done} -> DISTILLED, promoted (run: 2026-07-05-00h00)\n",
        encoding="utf-8",
    )
    scope = _admit_scope(fixture_repo)
    assert done not in scope.manifest["cohorts"]["logs"]
    assert done not in _batched(scope)
    assert _not_admitted(scope)[plan] == "not_log_cited"


def test_log_headers_are_not_log_entries(fixture_repo):
    _write(fixture_repo / "state" / "week-changelog" / "HEADER.md", "# Week Changelog\n")
    _write(fixture_repo / "state" / "week-changelog" / "HEADER.priorities.abc.md", "priorities\n")
    _write(fixture_repo / "state" / "week-changelog" / "2026-11-09.md", "- a day\n")
    _write(fixture_repo / "archive" / "week-changelogs" / "2026-06-14" / "2026-07-01-machine-a.md", "- a day\n")
    scope = _admit_scope(fixture_repo)
    assert scope.manifest["cohorts"]["logs"] == [
        "archive/week-changelogs/2026-06-14/2026-07-01-machine-a.md",
        "state/week-changelog/2026-11-09.md",
    ]


def test_cited_plans_rank_by_citation_count_then_bytes(fixture_repo):
    once = _heavy_plan(fixture_repo, "2026-11-01-once")
    twice = _heavy_plan(fixture_repo, "2026-11-02-twice")
    fatter = _heavy_plan(fixture_repo, "2026-11-03-fatter", extra="more\n" * 2000)
    _log(fixture_repo, "2026-11-05", once, twice, fatter)
    _log(fixture_repo, "2026-11-06", twice, fatter)
    plans = [p for p in _batched(_admit_scope(fixture_repo)) if p.startswith("docs/plans/")]
    assert plans == [fatter, twice, once]


def test_an_ephemeral_fate_stays_mechanical_never_not_admitted(fixture_repo):
    eph = "docs/plans/2026-11-01-eph.md"
    _write(fixture_repo / eph, "---\nstatus: implemented\ndistill_fate: ephemeral\n---\nx\n")
    scope = _admit_scope(fixture_repo)
    assert eph in {row["path"] for row in scope.manifest["mechanical"]}
    assert eph not in _not_admitted(scope)


def test_admission_adds_the_logs_cohort_to_a_caller_set_without_one(fixture_repo):
    _log(fixture_repo, "2026-11-02", "a day")
    spec = CohortSpec(name="completed", glob="archive/completed/**/*.md")
    scope = compute_scope(fixture_repo, run_id="r", cohort_specs=[spec], admit=True)
    assert scope.manifest["cohorts"]["logs"] == ["archive/daily-summaries/2026-11-02.md"]


def test_the_batch_budget_defers_overflow_and_keeps_it_out_of_mechanical(fixture_repo):
    for day in range(1, 6):
        _log(fixture_repo, f"2026-11-0{day}", "a day")
    scope = _admit_scope(fixture_repo, size_of=lambda rel: 600, byte_budget=1000, max_scan_batches=2)
    assert scope.manifest["batches"] == [
        ["archive/daily-summaries/2026-11-05.md"],
        ["archive/daily-summaries/2026-11-04.md"],
    ]
    deferred = [row["path"] for row in scope.manifest["deferred"]]
    assert deferred == [f"archive/daily-summaries/2026-11-0{day}.md" for day in (3, 2, 1)]
    assert {row["cohort"] for row in scope.manifest["deferred"]} == {"logs"}
    assert scope.counts["scannable"] == 2 and scope.counts["deferred"] == 3
    assert not set(deferred) & {row["path"] for row in scope.manifest["mechanical"]}


def test_every_candidate_settles_exactly_once_and_scannable_count_agrees(fixture_repo):
    _add_plans(fixture_repo)
    _add_fate_rows(fixture_repo)
    cited = _heavy_plan(fixture_repo, "2026-11-10-cited")
    _heavy_plan(fixture_repo, "2026-11-11-uncited")
    for day in range(1, 5):
        _log(fixture_repo, f"2026-11-0{day}", cited)
    specs = ds._with_logs(_PM_SPECS)
    before = compute_scope(fixture_repo, run_id="r", cohort_specs=specs)
    candidates = set(_batched(before)) | {row["path"] for row in before.manifest["mechanical"]}
    after = compute_scope(
        fixture_repo, run_id="r", cohort_specs=specs, admit=True,
        size_of=lambda rel: 600, byte_budget=1000, max_scan_batches=2,
    )
    manifest = after.manifest
    assert after.counts["deferred"] > 0 and manifest["not_admitted"]
    settled = (
        _batched(after)
        + [row["path"] for row in manifest["mechanical"]]
        + [row["path"] for row in manifest["not_admitted"]]
        + [row["path"] for row in manifest["deferred"]]
    )
    assert len(settled) == len(set(settled))
    assert set(settled) == candidates
    from coordinator_core.distill import manifest_schema

    assert manifest_schema.validate_scope_manifest(manifest) == []
    assert scannable_count(fixture_repo, cohort_specs=_PM_SPECS) == after.counts["scannable"] + after.counts["deferred"]
    assert not {row["path"] for row in manifest["not_admitted"]} & {row["path"] for row in manifest["mechanical"]}


def test_cohort_excluded_counts_the_dropped_default_cohorts_and_lists_none(fixture_repo):
    scope = _admit_scope(fixture_repo)
    assert scope.counts["cohort_excluded"] == 7  # 5 handoffs, 2 memos with no distill_fate
    listed = {row["path"] for row in scope.manifest["not_admitted"]}
    assert not any(p.startswith(("archive/handoffs/", "cross-repo/archive/")) for p in listed)
    assert "handoffs" not in scope.manifest["cohorts"]


def test_the_op_default_admits_under_the_pm_ruling_set(fixture_repo):
    plan = _heavy_plan(fixture_repo, "2026-11-01-big")
    _log(fixture_repo, "2026-11-02", plan)
    result = _handler({"run_id": "r"}, repo_root=fixture_repo / ".git")
    counts = result["counts"]
    assert "handoffs" not in counts and counts["memos"] == 0
    assert counts["logs"] == 1 and counts["scannable"] == 2 and counts["deferred"] == 0
    assert counts["cohort_excluded"] == 7
    assert "not_admitted=" in result["summary"]
    manifest = json.loads((fixture_repo / result["input_json_path"]).read_text(encoding="utf-8"))
    assert manifest["not_admitted"] and manifest["deferred"] == []


def test_admission_is_deterministic_across_runs(fixture_repo):
    plan = _heavy_plan(fixture_repo, "2026-11-01-big")
    _log(fixture_repo, "2026-11-02", plan)
    first = _admit_scope(fixture_repo)
    second = _admit_scope(fixture_repo)
    assert canonical_manifest_bytes(first.manifest) == canonical_manifest_bytes(second.manifest)


# ---------------------------------------------------------------------------
# Coverage watermark: delta plus backlog slice, advanced only by a promote
# ---------------------------------------------------------------------------

_LOG_DAYS = ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]


def _watermarked_repo(tmp_path: Path, monkeypatch) -> Path:
    from coordinator_core import run_watermark

    monkeypatch.setattr(run_watermark, "registry_get", lambda _key: None)
    root = tmp_path / "wm-repo"
    _write(root / "state" / "distillation-log.md", "## Run seed\n")
    for day in _LOG_DAYS:
        _log(root, day)
    _git_init(root)
    for argv in (
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-q", "-m", "seed"],
    ):
        subprocess.run(["git", *argv], cwd=root, check=True, **no_console_passthrough_kwargs())
    return root


def _slice_scope(root: Path):
    return _admit_scope(root, batch_size=3, max_scan_batches=1)


def _commit_watermark(root: Path, result, run_id: str = "r"):
    from coordinator_core import run_watermark

    run_dir = root / "state" / "scratch" / "artifact-distillation" / run_id
    run_watermark.stage(run_dir, result.watermark)
    return run_watermark.promote(root, run_dir, "distill")


def _log_rel(day: str) -> str:
    return f"archive/daily-summaries/{day}.md"


def test_scope_after_promote_is_delta_plus_next_backlog_slice(tmp_path, monkeypatch):
    root = _watermarked_repo(tmp_path, monkeypatch)
    first = _slice_scope(root)
    assert sorted(_batched(first)) == [_log_rel(d) for d in _LOG_DAYS[:3]]
    assert first.counts["deferred"] == 2
    _commit_watermark(root, first)

    (root / _log_rel(_LOG_DAYS[0])).write_text("- edited after the first run\n", encoding="utf-8")
    second = _slice_scope(root)
    assert sorted(_batched(second)) == sorted(_log_rel(d) for d in [_LOG_DAYS[0], *_LOG_DAYS[3:]])
    assert second.counts["deferred"] == 2
    assert _log_rel(_LOG_DAYS[1]) not in _batched(second)


def test_completed_pass_scopes_only_changed_files(tmp_path, monkeypatch):
    root = _watermarked_repo(tmp_path, monkeypatch)
    for _ in range(2):
        _commit_watermark(root, _slice_scope(root))
    quiet = _slice_scope(root)
    assert _batched(quiet) == [] and quiet.counts["deferred"] == len(_LOG_DAYS)

    (root / _log_rel(_LOG_DAYS[3])).write_text("- changed\n", encoding="utf-8")
    delta = _slice_scope(root)
    assert _batched(delta) == [_log_rel(_LOG_DAYS[3])]
    assert delta.counts["deferred"] == len(_LOG_DAYS) - 1


def test_unpromoted_runs_leave_the_watermark_absent_and_rescope_identically(tmp_path, monkeypatch):
    root = _watermarked_repo(tmp_path, monkeypatch)
    first = _slice_scope(root)
    second = _slice_scope(root)
    assert _batched(first) == _batched(second)
    assert not (root / "state" / "run-watermarks").exists()


def test_the_handler_stages_the_proposal_and_never_the_state_file(tmp_path, monkeypatch, wide_budget):
    root = _watermarked_repo(tmp_path, monkeypatch)
    result = _handler({"run_id": "r", "batch_size": 2}, repo_root=root / ".git")
    pending = root / "state" / "scratch" / "artifact-distillation" / "r" / "watermark.pending.json"
    assert result["counts"]["scannable"] == 5 and pending.is_file()
    assert json.loads(pending.read_text(encoding="utf-8"))["pass_complete"] is True
    assert not (root / "state" / "run-watermarks").exists()


def test_no_head_means_no_watermark_and_the_full_admitted_scope(tmp_path, monkeypatch):
    from coordinator_core import run_watermark

    monkeypatch.setattr(run_watermark, "registry_get", lambda _key: None)
    root = tmp_path / "unborn"
    _write(root / "state" / "distillation-log.md", "## Run seed\n")
    for day in _LOG_DAYS:
        _log(root, day)
    _git_init(root)
    scope = _slice_scope(root)
    assert scope.watermark is None
    assert _batched(scope) == [_log_rel(d) for d in reversed(_LOG_DAYS[2:])] and scope.counts["deferred"] == 2


def test_byte_packing_overflow_holds_the_cursor_before_the_first_deferred_file(tmp_path, monkeypatch):
    root = _watermarked_repo(tmp_path, monkeypatch)
    scope = _admit_scope(root, batch_size=100, max_scan_batches=1, byte_budget=1, size_of=lambda _rel: 1)
    assert len(_batched(scope)) == 1
    assert scope.watermark.cursor == f"{_LOG_DAYS[0]} {_log_rel(_LOG_DAYS[0])}"
    assert scope.watermark.pass_complete is False


# ---------------------------------------------------------------------------
# Run budget: the default admission narrows to fit, defers and never refuses
# ---------------------------------------------------------------------------


def test_default_admission_narrows_to_the_run_budget_and_defers_the_rest(tmp_path, monkeypatch):
    from coordinator_core.contract import run_budget

    root = _watermarked_repo(tmp_path, monkeypatch)
    scope = _admit_scope(root, batch_size=1)
    cap = run_budget.load("distill")
    block = scope.manifest["budget"]
    assert len(scope.manifest["batches"]) == 1 and scope.counts["deferred"] == len(_LOG_DAYS) - 1
    assert all(block["within"].values()) and block["cap"]["agents"] == cap.agents
    assert block["projected"]["agents"] <= cap.agents and block["projected"]["tokens"] <= cap.tokens
    assert block["over_cap_fixed"] is False


def test_a_larger_budget_admits_more_batches_than_the_default(tmp_path, monkeypatch):
    from coordinator_core.contract.run_budget import RunBudget

    root = _watermarked_repo(tmp_path, monkeypatch)
    narrow = len(_admit_scope(root, batch_size=1).manifest["batches"])
    monkeypatch.setattr(ds._run_budget, "load", lambda _c, path=None: RunBudget(1000, 100_000_000, 10_000))
    wide = _admit_scope(root, batch_size=1)
    assert len(wide.manifest["batches"]) > narrow and wide.counts["deferred"] < len(_LOG_DAYS) - narrow + 1


def test_a_budget_the_fixed_cost_alone_exceeds_still_admits_one_batch(tmp_path, monkeypatch):
    from coordinator_core.contract.run_budget import RunBudget

    root = _watermarked_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(ds._run_budget, "load", lambda _c, path=None: RunBudget(1, 1, 1))
    scope = _admit_scope(root, batch_size=1)
    block = scope.manifest["budget"]
    assert len(scope.manifest["batches"]) == 1
    assert block["over_cap_fixed"] is True and block["within"]["agents"] is False


def test_a_run_narrowed_by_the_budget_is_followed_by_the_files_it_deferred(tmp_path, monkeypatch):
    root = _watermarked_repo(tmp_path, monkeypatch)
    seen: list[str] = []
    for _ in _LOG_DAYS:
        scope = _admit_scope(root, batch_size=1)
        assert len(_batched(scope)) == 1
        seen.extend(_batched(scope))
        _commit_watermark(root, scope)
    assert seen == [_log_rel(d) for d in _LOG_DAYS]


def test_the_handler_reports_the_budget_and_records_the_run_in_the_backlog_pass_ledger(tmp_path, monkeypatch):
    from coordinator_core.ops import backlog_pass_ledger

    root = _watermarked_repo(tmp_path, monkeypatch)
    result = _handler({"run_id": "r", "batch_size": 1}, repo_root=root / ".git")
    assert all(result["budget"]["within"].values()) and result["counts"]["deferred"] == len(_LOG_DAYS) - 1
    ledger = backlog_pass_ledger.summary(str(root), "distill")
    assert ledger["runs"] == 1 and ledger["open"] is True
    assert ledger["cumulative_tokens"] == result["budget"]["projected"]["tokens"]


def test_files_a_tier_split_drops_are_deferred_and_the_watermark_holds_before_them(tmp_path, monkeypatch, wide_budget):
    from coordinator_core.ops import distill_workflow_input as dwi

    real = dwi.attach_relevant_guides

    def drop_last(payload, guides, **kw):
        real(payload, guides, **kw)
        payload["batches"] = payload["batches"][:-1]

    monkeypatch.setattr(dwi, "attach_relevant_guides", drop_last)
    root = _watermarked_repo(tmp_path, monkeypatch)
    result = _handler({"run_id": "r", "batch_size": 1}, repo_root=root / ".git")
    manifest = json.loads((root / result["input_json_path"]).read_text(encoding="utf-8"))
    dropped = _log_rel(_LOG_DAYS[-1])
    assert result["counts"]["scannable"] == len(_LOG_DAYS) - 1 and result["counts"]["deferred"] == 1
    assert [row["path"] for row in manifest["deferred"]] == [dropped]
    assert dropped not in [p for batch in manifest["batches"] for p in batch]
    pending = root / "state" / "scratch" / "artifact-distillation" / "r" / "watermark.pending.json"
    assert json.loads(pending.read_text(encoding="utf-8"))["pass_complete"] is False
