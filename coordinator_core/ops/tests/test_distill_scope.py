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
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.distill.manifest_schema import canonical_manifest_bytes
from coordinator_core.ops.distill_scope import (
    DR146_DATE_PREFIX_RE,
    DR146_MIN_STEM_LEN,
    DR146_STRIP_SUFFIXES,
    PM_RULING_2026_08_06_COHORT_SPECS,
    STEM_PREFIX_LEN,
    CohortSpec,
    _date_sort_key,
    _handler,
    compute_scope,
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
            "---\nstatus: implemented\n---\nbody\n",
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
        "---\nstatus: implemented\n---\npercolation engine notes\n",
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
