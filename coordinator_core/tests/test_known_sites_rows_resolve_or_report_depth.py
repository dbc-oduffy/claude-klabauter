"""The `_KNOWN_SITES` leg: measurement, not judgement, over the one-hop gate's frozen burn-down
inventory.

Spec backlink: `docs/plans/2026-08-26-every-register-either-derives-or-fails-on-its-dead-rows.md`,
chunk C3. Predecessor instrument this module oracles against, unmodified:
`docs/plans/2026-08-25-a-collector-that-sees-past-one-hop.md`.

ZERO WRITES INTO THE CAPPED FILE. This module only ever reads the two existing collectors'
public surface -- `test_no_unbatched_per_item_git_spawn.py`'s `_KNOWN_SITES`,
`find_unbatched_per_item_spawns`, and `_gate_scope_paths`, plus
`test_deep_per_item_spawn_worklist.py`'s `_deep_find_unbatched_per_item_spawns` -- exactly the
import shape `test_deep_per_item_spawn_worklist.py` itself already uses against the gate module.
Neither module is edited here. A DELETE-LEG WAS CONSIDERED AND REJECTED (per the plan's own
Anti-scope, carried from `docs/plans/2026-08-25-a-collector-that-sees-past-one-hop.md`): a row
dark to the one-hop gate is either genuinely fixed/gone (a closure candidate) or has simply moved
past the one-hop gate's own horizon while still being a live per-item spawn -- and those two
outcomes cannot be told apart without also consulting the depth-bounded deep oracle. A leg that
deletes on "dark to the one-hop gate" alone would silently erase the second class's only record.

CLASSIFICATION, per `_KNOWN_SITES` row `(path, enclosing, callee)`:
    - the path and the enclosing-function symbol still resolve in source, AND the one-hop gate
      still reports the site -> LIVE_DEBT: real per-item spawn debt, row stands unchanged.
    - resolves in source, dark to the one-hop gate, but the depth-bounded deep oracle
      (`_deep_find_unbatched_per_item_spawns`) reports it at some depth <= `_MAX_DEPTH` ->
      PAST_HORIZON: the site moved past the one-hop gate's horizon, it was never fixed. The row
      stands and this module records the depth the oracle first sees it at.
    - resolves in source, dark to BOTH the one-hop gate and the deep oracle at every depth through
      `_MAX_DEPTH` -> CLOSURE_CANDIDATE: reported as a candidate for removal from `_KNOWN_SITES`,
      never removed by this module.
    - the path or the enclosing-function symbol the row names no longer exists in source at all ->
      STALE: the row has drifted (moved, renamed, deleted); reported for re-pointing, never
      re-pointed here.

Only the row's OWN location -- the file and the enclosing function it is keyed on -- is checked
for existence. The callee half of the key is not independently resolved: an amplification site's
callee need not be defined in the same file (it may be imported), so callee existence is exactly
the question the two collectors already answer by finding (or not finding) the site as a
violation, not a question this module's path/symbol check re-derives.

The oracle (the deep collector) stays ADVISORY, exactly as its own plan requires. This module
consumes its existing public entry point (`_deep_find_unbatched_per_item_spawns`); it adds no new
discriminator and does not promote the oracle to gating. Nothing in this module asserts that any
particular row must be one classification or another -- the one exception is the two AC6 cases
below, which were located by hand this session specifically to prove the four-way split actually
discriminates, not to freeze every row's disposition.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from coordinator_core.tests.register_rows import (
    RegisterId,
    Row,
    SubjectClass,
    TrackedFileIndex,
    resolve_row,
)
from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
    _KNOWN_SITES,
    _gate_scope_paths,
    find_unbatched_per_item_spawns,
)
from coordinator_core.tests.test_deep_per_item_spawn_worklist import (
    _deep_find_unbatched_per_item_spawns,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The deep collector's own plan (docs/plans/2026-08-25-a-collector-that-sees-past-one-hop.md, C6)
# measures and publishes depths 2 through 4. There is no published depth-5+ figure to widen into,
# so the oracle is bounded here at the same ceiling that instrument itself is measured at.
_MAX_DEPTH = 4

KnownSiteKey = tuple[str, str, str]


class KnownSiteClassification:
    """The closed four-way split this leg reports. Never a fifth: a row's own location either
    resolves or it does not, and a resolving row is either seen by one of the two collectors, both,
    or neither."""

    LIVE_DEBT = "live-debt"
    PAST_HORIZON = "past-horizon"
    CLOSURE_CANDIDATE = "closure-candidate"
    STALE = "stale"


@dataclass(frozen=True)
class KnownSiteAssessment:
    """One `_KNOWN_SITES` row, measured rather than judged. `depth` is populated only for
    `PAST_HORIZON`: the first (lowest) depth at which the deep oracle reports the site."""

    site: KnownSiteKey
    classification: str
    depth: int | None
    detail: str


def _module_dotted(relpath: str) -> str:
    dotted = relpath[:-3] if relpath.endswith(".py") else relpath
    return dotted.replace("\\", "/").replace("/", ".")


def _site_resolves_in_source(
    site: KnownSiteKey, index: TrackedFileIndex, repo_root: Path
) -> tuple[bool, str]:
    """The path/symbol half of the measurement (AC's own phrasing): does the row's own file and
    enclosing-function symbol still exist? Reuses C1's AST row resolver
    (`register_rows.resolve_row`) rather than re-deriving path/AST resolution -- this module adds
    no second symbol-resolution mechanism."""
    path, enclosing, _callee = site
    dotted = f"{_module_dotted(path)}.{enclosing}"
    row = Row(
        register=RegisterId(path, enclosing),
        subject=dotted,
        declared_class=SubjectClass.SYMBOL,
    )
    resolution = resolve_row(row, index, repo_root)
    return resolution.resolved, resolution.detail


def classify_known_site(
    site: KnownSiteKey,
    index: TrackedFileIndex,
    one_hop_keys: frozenset[KnownSiteKey],
    deep_keys_by_depth: dict[int, frozenset[KnownSiteKey]],
    repo_root: Path,
) -> KnownSiteAssessment:
    """Measure one row against the four-way split. Delegates the path/symbol half to
    `register_rows.resolve_row` and the reachability half to the two existing collectors
    (unmodified, consulted read-only). Never deletes or re-points -- the four outcomes are
    reported, and only reported."""
    resolves, detail = _site_resolves_in_source(site, index, repo_root)
    if not resolves:
        return KnownSiteAssessment(
            site=site,
            classification=KnownSiteClassification.STALE,
            depth=None,
            detail=detail,
        )

    if site in one_hop_keys:
        return KnownSiteAssessment(
            site=site,
            classification=KnownSiteClassification.LIVE_DEBT,
            depth=None,
            detail="one-hop gate still reports this site",
        )

    for depth in sorted(deep_keys_by_depth):
        if site in deep_keys_by_depth[depth]:
            return KnownSiteAssessment(
                site=site,
                classification=KnownSiteClassification.PAST_HORIZON,
                depth=depth,
                detail=f"dark to the one-hop gate, seen by the deep oracle at depth {depth}",
            )

    return KnownSiteAssessment(
        site=site,
        classification=KnownSiteClassification.CLOSURE_CANDIDATE,
        depth=None,
        detail=f"dark to both the one-hop gate and the deep oracle through depth {_MAX_DEPTH}",
    )


# ---------------------------------------------------------------------------
# Fast unit coverage of the classifier itself, over synthetic fixtures -- no corpus walk, no
# collector invocation. These pin the four-way split's logic in isolation from the two expensive
# collectors exercised by the cadence test below.
# ---------------------------------------------------------------------------


def _index_for(repo_root: Path, *relpaths: str) -> TrackedFileIndex:
    return TrackedFileIndex(frozenset(relpaths))


def test_classify_known_site_stale_when_enclosing_symbol_missing(tmp_path):
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("def other():\n    pass\n", encoding="utf-8")
    index = _index_for(tmp_path, "pkg/mod.py")
    site = ("pkg/mod.py", "missing_fn", "some_callee")

    assessment = classify_known_site(site, index, frozenset(), {}, tmp_path)

    assert assessment.classification == KnownSiteClassification.STALE
    assert assessment.depth is None


def test_classify_known_site_stale_when_path_missing(tmp_path):
    index = _index_for(tmp_path)
    site = ("pkg/gone.py", "some_fn", "some_callee")

    assessment = classify_known_site(site, index, frozenset(), {}, tmp_path)

    assert assessment.classification == KnownSiteClassification.STALE


def test_classify_known_site_live_debt_when_one_hop_gate_reports(tmp_path):
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("def check():\n    pass\n", encoding="utf-8")
    index = _index_for(tmp_path, "pkg/mod.py")
    site = ("pkg/mod.py", "check", "spawner")

    assessment = classify_known_site(site, index, frozenset({site}), {}, tmp_path)

    assert assessment.classification == KnownSiteClassification.LIVE_DEBT
    assert assessment.depth is None


def test_classify_known_site_past_horizon_from_deep_oracle_records_lowest_depth(tmp_path):
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("def check():\n    pass\n", encoding="utf-8")
    index = _index_for(tmp_path, "pkg/mod.py")
    site = ("pkg/mod.py", "check", "spawner")
    deep_keys_by_depth = {2: frozenset(), 3: frozenset({site}), 4: frozenset({site})}

    assessment = classify_known_site(site, index, frozenset(), deep_keys_by_depth, tmp_path)

    assert assessment.classification == KnownSiteClassification.PAST_HORIZON
    assert assessment.depth == 3


def test_classify_known_site_closure_candidate_when_dark_to_both(tmp_path):
    module = tmp_path / "pkg" / "mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("def check():\n    pass\n", encoding="utf-8")
    index = _index_for(tmp_path, "pkg/mod.py")
    site = ("pkg/mod.py", "check", "spawner")
    deep_keys_by_depth = {2: frozenset(), 3: frozenset(), 4: frozenset()}

    assessment = classify_known_site(site, index, frozenset(), deep_keys_by_depth, tmp_path)

    assert assessment.classification == KnownSiteClassification.CLOSURE_CANDIDATE
    assert assessment.depth is None


# ---------------------------------------------------------------------------
# The real leg: every `_KNOWN_SITES` row, measured against the live corpus and both existing
# collectors. Never gates, never deletes, never re-points -- publishes what each row is.
# ---------------------------------------------------------------------------


# HORIZON (resolves-or-declares-horizon): `_KNOWN_SITES` is enrolled in
# `coordinator_core/tests/test_every_register_resolves_or_declares.py`'s core-45 sweep. A green
# run there establishes only that every `_KNOWN_SITES` row's path/enclosing-symbol subject exists
# on disk against its declared class -- it does NOT establish that `_KNOWN_SITES` is the right
# frozen burn-down population, that any future exemption taken here is legitimate, or that a
# resolving row is still LIVE_DEBT rather than a CLOSURE_CANDIDATE or STALE row this module's own
# classification below has not yet reported as such.


@pytest.mark.cadence
def test_known_sites_rows_resolve_or_report_depth():
    """AC6: the `_KNOWN_SITES` leg, oracled by the deep collector, with zero writes into the
    capped file (`test_no_unbatched_per_item_git_spawn.py`).

    Cadence-marked: this walks the same corpus the standing one-hop gate and the deep advisory
    module already each pay for separately (tens of seconds per collector run, per both modules'
    own docstrings), so it runs at cadence gates rather than per-commit -- matching its siblings in
    `test_deep_per_item_spawn_worklist.py`."""
    index = TrackedFileIndex.build(_REPO_ROOT)

    one_hop_violations = find_unbatched_per_item_spawns(_gate_scope_paths())
    one_hop_keys = frozenset(site.key for site in one_hop_violations)

    deep_keys_by_depth: dict[int, frozenset[KnownSiteKey]] = {}
    for depth in range(2, _MAX_DEPTH + 1):
        deep_violations = _deep_find_unbatched_per_item_spawns(
            _gate_scope_paths(), max_depth=depth
        )
        deep_keys_by_depth[depth] = frozenset(site.key for site in deep_violations)

    assessments = {
        site: classify_known_site(site, index, one_hop_keys, deep_keys_by_depth, _REPO_ROOT)
        for site in _KNOWN_SITES
    }

    # AC6's two first-run cases, both real and already located by hand this session (see module
    # docstring and the plan's C3 body): these prove the four-way split actually discriminates
    # PAST_HORIZON from STALE, not that every row is frozen to a particular classification.
    write_guards_site = (
        "coordinator_core/write_guards/validate_frontmatter_schema_advisory.py",
        "_reviewed_range_offer",
        "_resolve_ref_to_sha",
    )
    relocated_site = (
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        "_first_deliverable_commit_range_base",
        "_run_git",
    )

    write_guards_assessment = assessments[write_guards_site]
    assert write_guards_assessment.classification == KnownSiteClassification.PAST_HORIZON, (
        write_guards_assessment
    )
    assert write_guards_assessment.depth is not None

    relocated_assessment = assessments[relocated_site]
    assert relocated_assessment.classification == KnownSiteClassification.STALE, (
        relocated_assessment
    )
