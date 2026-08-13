"""
coordinator_core.distill.tests.test_harvest_debt

Unit tests for coordinator_core.distill.harvest_debt — the harvest-debt computation
logic (C4).

Coverage:
  compute_harvest_debt:
    (a) FAIL LOUD: absent log path raises DistillationLogMissingError (never silently
        treated as "harvest everything")
    (b) FAIL LOUD: a log path that existed and was renamed away also raises (same
        contract, exercised via an explicit rename to simulate the "log renamed away"
        scenario named in the plan's AC7)
    (c) golden un-harvested-set fixture: a canonical-format log fixture (run|path|
        disposition|fate, ASCII "->") plus a seeded archive/specs tree, asserting the
        exact harvest_debt basename list
    (d) DISTILLED and PROMOTE both count as harvested; EPHEMERAL/SKIP/PRESERVE do not
    (d2) action-table vocabulary (coordinator-claude schema-header `date | action | path | ...` format,
        lowercase `harvested`/`deleted` — the format sibling repos' live logs carry):
        counted as harvested; unrecognized actions (`skipped`, `consolidated`) do not
        count; uppercase variants do NOT match (exact-token, no case-folding); a mixed
        log (canonical-format rows + action-table rows) counts both vocabularies
    (e) comm -23 semantics: a basename present in a DISTILLED row is excluded from debt
        even if other rows in the same log mention other paths
    (f) warn=True when harvest_debt count is disproportionately larger than the logged
        harvested count; warn=False when the ratio is reasonable
    (g) nested archive/specs subdirectories (e.g. archive/specs/2026-07/) are scanned
        recursively, keyed specs_dir-relative (NOT bare basename)
    (g2) a basename collision across two different specs_dir subdirectories does NOT
        cause the harvested one to mask the other as debt (Finding 1 regression,
        2026-07-12 code review)
    (h) an absent specs_dir (not just an absent log) does not raise — total_specs/debt
        degrade to empty, only the log's absence is fail-loud
    (i) FROZEN-CORPUS golden (AC7) — compute_harvest_debt run against a FROZEN
        SNAPSHOT of this repo's real state/distillation-log.md +
        archive/specs/** filename manifest (captured 2026-07-21), rebuilt
        under tmp_path and asserted EXACT (not subset/superset) against a
        checked-in golden fixture under
        coordinator_core/distill/tests/goldens/harvest_debt_frozen_corpus_2026-07-21.json.
        CORRECTION (was: a live-corpus test reading the real, ever-mutating
        state/distillation-log.md + archive/specs/ tree directly) — see the
        test's own docstring for why that shape was a bug, not a feature.

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C4/C6
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.distill._common import SIDECAR_SUFFIXES
from coordinator_core.distill.harvest_debt import (
    DistillationLogMissingError,
    _specs_dir_relative_paths,
    compute_harvest_debt,
)

CANONICAL_LOG_FIXTURE = """\
## Run r-2026-07-12a
- archive/specs/2026-07/foo-plan.md -> DISTILLED, folded into wiki/foo.md (run: r-2026-07-12a)
- archive/specs/2026-07/bar-plan.md -> PROMOTE, promoted to decision (run: r-2026-07-12a)
- archive/specs/2026-07/baz-plan.md -> EPHEMERAL, scratch only, no lasting value (run: r-2026-07-12a)
- archive/specs/2026-07/qux-plan.md -> SKIP, superseded by later plan (run: r-2026-07-12a)
"""


def _seed_specs(tmp_path, names):
    specs_dir = tmp_path / "archive" / "specs" / "2026-07"
    specs_dir.mkdir(parents=True)
    for name in names:
        (specs_dir / name).write_text("dummy content\n")
    return tmp_path / "archive" / "specs"


def _write_log(tmp_path, text):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(text)
    return log_path


# ---------------------------------------------------------------------------
# FAIL LOUD
# ---------------------------------------------------------------------------

def test_absent_log_fails_loud(tmp_path):
    specs_dir = _seed_specs(tmp_path, ["foo-plan.md"])
    absent_log = tmp_path / "does-not-exist.md"
    with pytest.raises(DistillationLogMissingError):
        compute_harvest_debt(specs_dir, absent_log)


def test_renamed_away_log_fails_loud(tmp_path):
    # AC7: a test asserting FAIL-LOUD on a renamed-away log.
    specs_dir = _seed_specs(tmp_path, ["foo-plan.md"])
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)
    assert log_path.exists()

    renamed_path = tmp_path / "distillation-log.md.bak"
    log_path.rename(renamed_path)
    assert not log_path.exists()

    with pytest.raises(DistillationLogMissingError):
        compute_harvest_debt(specs_dir, log_path)


def test_absent_log_never_treated_as_harvest_everything(tmp_path):
    # Explicit regression guard for the #1 hazard: an absent log must not resolve
    # to an empty harvested-set that silently makes everything "debt-free" or
    # (worse) silently returns success with a full-tree debt list computed against
    # a phantom empty log. It must raise, full stop.
    specs_dir = _seed_specs(tmp_path, ["a.md", "b.md", "c.md"])
    absent_log = tmp_path / "nope.md"
    try:
        compute_harvest_debt(specs_dir, absent_log)
        assert False, "expected DistillationLogMissingError to be raised"
    except DistillationLogMissingError:
        # Expected -- this IS the assertion under test (see the docstring above).
        pass


# ---------------------------------------------------------------------------
# Golden un-harvested-set fixture
# ---------------------------------------------------------------------------

def test_golden_unharvested_set_fixture(tmp_path):
    specs_dir = _seed_specs(
        tmp_path,
        [
            "foo-plan.md",  # DISTILLED -> harvested
            "bar-plan.md",  # PROMOTE -> harvested
            "baz-plan.md",  # EPHEMERAL -> NOT harvested (counts as debt)
            "qux-plan.md",  # SKIP -> NOT harvested (counts as debt)
            "unlogged-plan.md",  # absent from log entirely -> debt
        ],
    )
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)

    result = compute_harvest_debt(specs_dir, log_path)

    assert result.harvest_debt == sorted(
        ["2026-07/baz-plan.md", "2026-07/qux-plan.md", "2026-07/unlogged-plan.md"]
    )
    assert result.harvested_count == 2
    assert result.total_specs == 5


def test_distilled_and_promote_both_count_as_harvested(tmp_path):
    specs_dir = _seed_specs(tmp_path, ["foo-plan.md", "bar-plan.md"])
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)
    result = compute_harvest_debt(specs_dir, log_path)
    assert "foo-plan.md" not in result.harvest_debt
    assert "bar-plan.md" not in result.harvest_debt


def test_ephemeral_skip_preserve_do_not_count_as_harvested(tmp_path):
    specs_dir = _seed_specs(tmp_path, ["baz-plan.md", "qux-plan.md"])
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)
    result = compute_harvest_debt(specs_dir, log_path)
    assert "2026-07/baz-plan.md" in result.harvest_debt
    assert "2026-07/qux-plan.md" in result.harvest_debt


def test_comm_minus_23_semantics_excludes_only_harvested_basenames(tmp_path):
    specs_dir = _seed_specs(tmp_path, ["foo-plan.md", "totally-unrelated.md"])
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)
    result = compute_harvest_debt(specs_dir, log_path)
    assert result.harvest_debt == ["2026-07/totally-unrelated.md"]


# ---------------------------------------------------------------------------
# action-table vocabulary (coordinator-claude schema-header format: date | action | path | ...)
# ---------------------------------------------------------------------------

ACTION_TABLE_LOG_FIXTURE = """\
# Distillation Log
# Append-only. Each row = one deleted scaffold OR one archived spec.
# Columns: date | action | path | last_sha | belongs_to_spec | reason

2026-07-01 | harvested | archive/specs/2026-07/foo-plan.md | 3b470e5 | foo-plan | Ripe plan harvested into docs/wiki/foo.md with claim derivation retained
2026-07-02 | deleted | archive/specs/2026-07/bar-plan.md | 9a1c2e0 | bar-plan | Review scaffolding deleted after knowledge folded into parent spec
2026-07-02 | skipped | archive/specs/2026-07/baz-plan.md | 1f4d8b3 | baz-plan | Still in active use by the ingest workstream, not ripe for harvest
2026-07-03 | consolidated | archive/specs/2026-07/qux-plan.md | 7e2a9c1 | qux-plan | Merged into the umbrella ingest spec rather than harvested directly
"""


def test_action_table_harvested_and_deleted_count_as_harvested(tmp_path):
    # The cockpit defect (2026-07-23): a real action-table log read as 0 harvested
    # because only canonical DISTILLED/PROMOTE rows were recognized, inflating
    # harvest-debt and emitting a spurious warn.
    specs_dir = _seed_specs(tmp_path, ["foo-plan.md", "bar-plan.md"])
    log_path = _write_log(tmp_path, ACTION_TABLE_LOG_FIXTURE)

    result = compute_harvest_debt(specs_dir, log_path)

    assert result.harvest_debt == []
    assert result.harvested_count == 2
    assert result.warn is False


def test_action_table_unrecognized_actions_do_not_count(tmp_path):
    specs_dir = _seed_specs(tmp_path, ["baz-plan.md", "qux-plan.md"])
    log_path = _write_log(tmp_path, ACTION_TABLE_LOG_FIXTURE)

    result = compute_harvest_debt(specs_dir, log_path)

    assert "2026-07/baz-plan.md" in result.harvest_debt  # skipped -> not harvested
    assert "2026-07/qux-plan.md" in result.harvest_debt  # consolidated -> not harvested


def test_action_table_tokens_are_exact_match_not_case_folded(tmp_path):
    # No mixed-case occurrence of harvested/deleted was found on disk (2026-07-23
    # fleet sweep), so matching is exact-token. Uppercase DELETED in an action-table
    # row must NOT count — it belongs to log_normalize's DR-053 legacy dialect
    # (DELETED -> EPHEMERAL), which is explicitly not-harvested semantics.
    specs_dir = _seed_specs(tmp_path, ["foo-plan.md"])
    log_path = _write_log(
        tmp_path,
        "2026-07-01 | HARVESTED | archive/specs/2026-07/foo-plan.md | abc1234 | foo | x\n"
        "2026-07-01 | DELETED | archive/specs/2026-07/foo-plan.md | abc1234 | foo | x\n",
    )

    result = compute_harvest_debt(specs_dir, log_path)

    assert result.harvest_debt == ["2026-07/foo-plan.md"]
    assert result.harvested_count == 0


def test_action_table_legacy_row_joins_on_basename_across_month_foldering(tmp_path):
    # Regression: cross-repo memo 2026-08-06-example-retrieval-repo-em-distill-fate-coverage-
    # and-legacy-log-reader.md, ask 2. A legacy action-table row's path predates a
    # later month-foldering rewrite (archive/specs/foo-plan.md logged, but the file
    # now lives at archive/specs/2026-07/foo-plan.md) — a path join silently fails
    # to match and manufactures false debt. Must join on basename instead.
    specs_dir = _seed_specs(tmp_path, ["foo-plan.md"])
    log_path = _write_log(
        tmp_path,
        "2026-07-01 | harvested | archive/specs/foo-plan.md | abc1234 | foo | pre-foldering row\n",
    )

    result = compute_harvest_debt(specs_dir, log_path)

    assert result.harvest_debt == []
    assert result.harvested_count == 1


def test_action_table_legacy_row_basename_collision_credits_neither(tmp_path):
    # Two different specs (different month folders) share a basename. A legacy
    # row for that basename can't disambiguate which one it meant — deliberately
    # credit neither rather than last-write-wins guessing (see harvest_debt's
    # _harvested_relative_paths docstring comment).
    specs_dir = tmp_path / "archive" / "specs"
    (specs_dir / "2026-06").mkdir(parents=True)
    (specs_dir / "2026-07").mkdir(parents=True)
    (specs_dir / "2026-06" / "dup-plan.md").write_text("dummy\n")
    (specs_dir / "2026-07" / "dup-plan.md").write_text("dummy\n")
    log_path = _write_log(
        tmp_path,
        "2026-07-01 | harvested | archive/specs/dup-plan.md | abc1234 | dup | ambiguous legacy row\n",
    )

    result = compute_harvest_debt(specs_dir, log_path)

    assert sorted(result.harvest_debt) == ["2026-06/dup-plan.md", "2026-07/dup-plan.md"]
    assert result.harvested_count == 0


def test_mixed_log_counts_both_vocabularies(tmp_path):
    specs_dir = _seed_specs(
        tmp_path,
        ["foo-plan.md", "bar-plan.md", "action-harvested.md", "action-deleted.md", "unlogged.md"],
    )
    mixed_log = (
        CANONICAL_LOG_FIXTURE
        + "\n"
        + "2026-07-01 | harvested | archive/specs/2026-07/action-harvested.md | 3b470e5 | ah | Plan harvested into wiki guide with derivation chain retained intact\n"
        + "2026-07-02 | deleted | archive/specs/2026-07/action-deleted.md | 9a1c2e0 | ad | Scaffolding deleted after fold into parent spec, git-recoverable\n"
    )
    log_path = _write_log(tmp_path, mixed_log)

    result = compute_harvest_debt(specs_dir, log_path)

    # Canonical vocabulary: foo (DISTILLED) + bar (PROMOTE); action-table
    # vocabulary: action-harvested + action-deleted. Only unlogged.md is debt.
    assert result.harvest_debt == ["2026-07/unlogged.md"]
    assert result.harvested_count == 4
    assert result.total_specs == 5


# ---------------------------------------------------------------------------
# warn ratio
# ---------------------------------------------------------------------------

def test_warn_true_when_debt_dwarfs_logged_harvested_rows(tmp_path):
    # 1 harvested row logged; 10 unlogged specs -> debt vastly outweighs the log.
    names = [f"unlogged-{i}.md" for i in range(10)] + ["foo-plan.md"]
    specs_dir = _seed_specs(tmp_path, names)
    log_path = _write_log(
        tmp_path,
        "## Run r-1\n- archive/specs/2026-07/foo-plan.md -> DISTILLED, ok (run: r-1)\n",
    )
    result = compute_harvest_debt(specs_dir, log_path)
    assert result.warn is True


def test_warn_false_when_ratio_reasonable(tmp_path):
    names = ["foo-plan.md", "bar-plan.md", "baz-plan.md", "qux-plan.md"]
    specs_dir = _seed_specs(tmp_path, names)
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)
    result = compute_harvest_debt(specs_dir, log_path)
    assert result.warn is False


def test_warn_true_when_zero_harvested_and_some_debt(tmp_path):
    specs_dir = _seed_specs(tmp_path, ["a.md", "b.md"])
    log_path = _write_log(tmp_path, "## Run r-1\n")
    result = compute_harvest_debt(specs_dir, log_path)
    assert result.harvested_count == 0
    assert result.warn is True


def test_warn_false_when_no_debt_at_all(tmp_path):
    specs_dir = _seed_specs(tmp_path, ["foo-plan.md", "bar-plan.md"])
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)
    result = compute_harvest_debt(specs_dir, log_path)
    assert result.harvest_debt == []
    assert result.warn is False


# ---------------------------------------------------------------------------
# recursive scan / absent specs_dir
# ---------------------------------------------------------------------------

def test_recursive_scan_across_nested_subdirs(tmp_path):
    specs_root = tmp_path / "archive" / "specs"
    (specs_root / "2026-06").mkdir(parents=True)
    (specs_root / "2026-07").mkdir(parents=True)
    (specs_root / "2026-06" / "old-plan.md").write_text("x\n")
    (specs_root / "2026-07" / "foo-plan.md").write_text("x\n")
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)

    result = compute_harvest_debt(specs_root, log_path)

    assert "2026-06/old-plan.md" in result.harvest_debt
    assert "2026-07/foo-plan.md" not in result.harvest_debt
    assert result.total_specs == 2


def test_basename_collision_across_subdirs_does_not_mask_debt(tmp_path):
    # Review: code-reviewer (Finding 1, 2026-07-12) — two DIFFERENT specs sharing a
    # basename across two different specs_dir subdirectories must not collide into
    # one debt-set entry. Logging one as DISTILLED must not silently drop the other
    # (same-named, different subdir) from the debt list.
    specs_root = tmp_path / "archive" / "specs"
    (specs_root / "2026-06").mkdir(parents=True)
    (specs_root / "2026-07").mkdir(parents=True)
    (specs_root / "2026-06" / "foo-plan.md").write_text("x\n")
    (specs_root / "2026-07" / "foo-plan.md").write_text("x\n")

    log_path = _write_log(
        tmp_path,
        "## Run r-1\n"
        "- archive/specs/2026-07/foo-plan.md -> DISTILLED, folded into wiki (run: r-1)\n",
    )

    result = compute_harvest_debt(specs_root, log_path)

    assert "2026-06/foo-plan.md" in result.harvest_debt, (
        "the 2026-06 foo-plan.md must still be reported as debt even though a "
        "same-named spec in 2026-07 was logged DISTILLED — basename must not be "
        "the sole collision key"
    )
    assert "2026-07/foo-plan.md" not in result.harvest_debt
    assert result.total_specs == 2
    assert result.harvested_count == 1


# ---------------------------------------------------------------------------
# C5: sidecar exclusion — sidecars never enter total_specs or the debt set
# ---------------------------------------------------------------------------


def test_sidecar_files_excluded_from_total_specs_and_debt(tmp_path):
    specs_dir = _seed_specs(
        tmp_path,
        [
            "unlogged-plan.md",  # a real, unlogged plan -> debt
            "orphan.review.md",  # sidecar -> excluded entirely
            "orphan.v3-divergence-check.md",  # sidecar -> excluded entirely
        ],
    )
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)

    result = compute_harvest_debt(specs_dir, log_path)

    assert result.harvest_debt == ["2026-07/unlogged-plan.md"]
    assert "2026-07/orphan.review.md" not in result.harvest_debt
    assert "2026-07/orphan.v3-divergence-check.md" not in result.harvest_debt
    assert result.total_specs == 1


def test_sidecar_suffixes_are_the_shared_common_vocabulary(tmp_path):
    # Every SIDECAR_SUFFIXES member is excluded, proving harvest_debt consumes
    # the C1 SSOT rather than its own local list.
    names = [f"orphan{suffix}" for suffix in SIDECAR_SUFFIXES if suffix.startswith(".")]
    names.append("foo-plan.md")
    specs_dir = _seed_specs(tmp_path, names)
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)

    result = compute_harvest_debt(specs_dir, log_path)

    assert result.total_specs == 1
    assert result.harvest_debt == []


def test_absent_specs_dir_does_not_raise_only_log_absence_is_fail_loud(tmp_path):
    specs_dir = tmp_path / "archive" / "specs"  # never created
    log_path = _write_log(tmp_path, CANONICAL_LOG_FIXTURE)

    result = compute_harvest_debt(specs_dir, log_path)

    assert result.total_specs == 0
    assert result.harvest_debt == []


# ---------------------------------------------------------------------------
# (i) FROZEN-CORPUS golden — snapshot of real state/distillation-log.md +
#     archive/specs/** filename shapes, rebuilt under tmp_path (AC7)
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_FROZEN_LOG_FIXTURE = _FIXTURES_DIR / "frozen_distillation_log_2026-07-21.md"
_FROZEN_MANIFEST_FIXTURE = _FIXTURES_DIR / "frozen_specs_manifest_2026-07-21.json"
_FROZEN_GOLDEN_PATH = (
    Path(__file__).resolve().parent / "goldens" / "harvest_debt_frozen_corpus_2026-07-21.json"
)


def _load_frozen_golden() -> dict:
    return json.loads(_FROZEN_GOLDEN_PATH.read_text(encoding="utf-8"))


def test_frozen_corpus_snapshot_harvest_debt_matches_golden(tmp_path):
    """Regression-pinning test (AC7): compute_harvest_debt run against a FROZEN
    SNAPSHOT of this repo's real state/distillation-log.md + archive/specs/**
    filename shapes (captured 2026-07-21, see the fixture files under
    coordinator_core/distill/tests/fixtures/), rebuilt fresh under tmp_path
    every run and asserted EXACT against a checked-in golden.

    CORRECTION (was: test_live_corpus_harvest_debt_matches_golden, which read
    this repo's REAL, live, ever-mutating state/distillation-log.md +
    archive/specs/ tree directly and pinned the result as a subset/superset
    against a golden). That shape was self-invalidating by design: this
    repo's own distillation ceremonies (bin/distill-log-append.py appending
    rows, specs being harvested or deleted-as-ephemeral) are the SYSTEM
    WORKING AS INTENDED, not a regression — yet every such ceremony run moved
    entries out of golden["known_debt"] without "a matching log row
    explaining the change" being legible to a pure superset-diff assertion,
    so ordinary forward progress tripped the test. Confirmed empirically
    2026-07-21: three specs previously pinned as known-debt had legitimately
    left debt (two were logged DISTILLED and are now harvested; one was
    logged DELETED as an ephemeral prior-art-check scaffold and no longer
    exists in archive/specs at all) — none of that is a parser/computation
    regression, but the old test failed anyway.

    Fixed by freezing a COPY of the real log text + the real archive/specs
    filename manifest as of 2026-07-21 into on-disk fixtures (preserving the
    AC7 intent of exercising the parser against real-world messy log format,
    not a hand-crafted synthetic log), and rebuilding them under `tmp_path`
    every run. The frozen snapshot never changes again, so the golden here IS
    an exact-equality pin (not subset/superset) — a genuine parser or
    computation regression is the ONLY thing that can flip this red, because
    the input corpus is now fixed forever, not live. Decision-boundary
    coverage (DISTILLED/PROMOTE vs EPHEMERAL/SKIP/PRESERVE, comm -23
    semantics, nested-subdir collision handling) stays with the synthetic
    CANONICAL_LOG_FIXTURE-based tests above; this test's unique job is
    proving the parser holds up against a real, once-live corpus snapshot.

    HOW TO RE-FREEZE the fixtures (only if a genuine parser/format change
    requires a new real-world snapshot — NOT a routine maintenance step):

        cp state/distillation-log.md \\
           coordinator_core/distill/tests/fixtures/frozen_distillation_log_<DATE>.md
        python3 -c "
        import json
        from pathlib import Path
        specs_dir = Path('archive/specs')
        paths = sorted(
            p.relative_to(specs_dir).as_posix()
            for p in specs_dir.rglob('*') if p.is_file()
        )
        Path('coordinator_core/distill/tests/fixtures/frozen_specs_manifest_<DATE>.json'
        ).write_text(json.dumps({'note': 'frozen snapshot <DATE>', 'paths': paths},
                                 indent=2, sort_keys=True) + chr(10))
        "

    then regenerate the golden the same way compute_harvest_debt is invoked
    in this test body, update the fixture/golden path constants above, and
    update this docstring's date. Never re-point this test at the LIVE
    corpus again — that is exactly the bug being fixed.
    """
    golden = _load_frozen_golden()
    manifest = json.loads(_FROZEN_MANIFEST_FIXTURE.read_text(encoding="utf-8"))
    log_text = _FROZEN_LOG_FIXTURE.read_text(encoding="utf-8")

    specs_dir = tmp_path / "archive" / "specs"
    for rel in manifest["paths"]:
        spec_file = specs_dir / rel
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text("frozen dummy content\n")
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(log_text, encoding="utf-8")

    result = compute_harvest_debt(specs_dir, log_path)

    assert result.harvest_debt == golden["harvest_debt"], (
        "harvest-debt drifted against the FROZEN 2026-07-21 corpus snapshot — "
        "since the input corpus is now fixed (not live), any difference here "
        "is a genuine parser/computation regression, not corpus churn."
    )
    assert result.harvested_count == golden["harvested_count"]
    assert result.total_specs == golden["total_specs"]
    assert result.warn == golden["warn"]
