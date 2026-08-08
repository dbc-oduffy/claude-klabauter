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
    (f) sidecar cohort routed via sweep_sidecars (active-reference-guard-cleared),
        never via ripe_filter's own raw sidecars field
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

Spec backlink: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C10
"""

from __future__ import annotations

import asyncio
import json
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
    render_summary,
    slugify_stem,
    wiki_slugs_as_dict,
    write_scope_manifest,
)


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


def test_compute_scope_wiki_inventory_adds_coordinator_dir_when_present(fixture_repo):
    _write(
        fixture_repo / "coordinator" / "docs" / "wiki" / "other-guide.md",
        "# Other\n",
    )
    result = compute_scope(fixture_repo, run_id="2026-07-23-01h00")
    assert result.manifest["wiki_dirs"] == ["docs/wiki", "coordinator/docs/wiki"]
    assert "other-guide" in wiki_slugs_as_dict(result.manifest)


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
    assert cohorts["week_changelog"] == ["state/week-changelog/2026-08-03.md"]
    assert modes["completed"] == "harvest"
    assert modes["week_changelog"] == "harvest"

    # memo-one now carries a promote-signal distill_fate and is included;
    # memo-two never got a distill_fate field and is excluded.
    assert cohorts["memos"] == ["cross-repo/archive/2026-07-01-memo-one.md"]

    # archived handoffs are check-only: still reported...
    assert cohorts["handoffs"] == [
        "archive/handoffs/2026-07-01-eligible.md",
        "archive/handoffs/2026-07-05-claimed.md",
    ]
    assert modes["handoffs"] == "check-only"
    # ...but never batched for harvest.
    flat_batched = {p for batch in result.manifest["batches"] for p in batch}
    assert "archive/handoffs/2026-07-01-eligible.md" not in flat_batched
    assert "archive/completed/2026-08/2026-08-01-done.md" in flat_batched
    assert "state/week-changelog/2026-08-03.md" in flat_batched
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

    subprocess.run(["git", "init", "-q"], cwd=fixture_repo, check=True)

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
# Review: review-integrator — render_summary previously hardcoded
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
