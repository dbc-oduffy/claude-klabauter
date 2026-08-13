"""Parity net for the cockpit-emission Python port (tc-3).

The bash emitter (Port of: emit-cockpit-snapshot.sh, coordinator-claude 07eedcfb 2026-07-19,
READ-ONLY oracle) was run once to capture `fixtures/golden-cockpit-emission.json`.
Every section porter under
`coordinator_core/ops/emit/sections/<name>.py` must reproduce, via `collect(ctx)`, the
slice of that golden that the section owns — after volatile + emit-derived fields are
normalized on BOTH sides using a context-appropriate strategy:

  Section parity (assert_section_parity):
    - Volatile fields (timestamps, git SHAs, machine identity) → typed sentinels
      (_TS_SENTINEL / _SHA_SENTINEL / _ID_SENTINEL per AC5-PROVENANCE oracle).
    - Derived fields (deliverable_status, shipped_sha) → None on BOTH sides.
      The golden was captured from the full post-enrichment envelope so it carries
      enriched values; collect() is pre-enrichment and returns None.  Nulling both
      sides verifies collect() doesn't accidentally return a non-null derived value
      (the Staff Engineer F0 — a pure sentinel-on-both-sides approach hides that failure).

  Full parity (assert_full_parity, C3):
    - Volatile fields → typed sentinels (same as section parity).
    - deliverable_status → compared directly; the cross-join result must match.
    - shipped_sha → <NORMALIZED> (git SHA; volatile between capture and re-run).

Exposed for the porter waves (C2) and the full-emission wave (C3):
    - normalize_record / normalize_records — canonicalize volatile fields (full context).
    - assert_section_parity(name)          — gate one section against its golden slice.
    - assert_full_parity(emission)         — gate a full Python emission against the golden.

A pytest parametrizes over discovered section modules so each porter is auto-tested via
`-k <name>` as it lands; zero sections present today → a single skip, never a failure.

Spec: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

# Whole-module opt-out: every test here compares emitted output against the
# LIVE coordinator tree, resolved via the machine-local registry. conftest.py's
# home quarantine turns that into 'coordinator root not found'.
pytestmark = pytest.mark.real_home

# --------------------------------------------------------------------------- paths
_TESTS_DIR = Path(__file__).resolve().parent
_FIXTURES = _TESTS_DIR / "fixtures"
GOLDEN_PATH = _FIXTURES / "golden-cockpit-emission.json"
MAP_PATH = _FIXTURES / "section_envelope_map.json"
_SECTIONS_DIR = _TESTS_DIR.parent / "sections"
_SECTIONS_PKG = "coordinator_core.ops.emit.sections"

# --------------------------------------------------------------------------- normalization
# Review: code-reviewer (F2) — sentinels, key-sets, and _normalize moved to the production
# normalizers module so both strang-01 and strang-02 test files import from a stable location
# rather than cross-importing between test leaves (breaks pytest collection isolation).
# All behavior is identical; only the home has moved.
from coordinator_core.ops.emit.normalizers import (  # noqa: E402
    _ALL_NORMALIZED_KEYS,
    _HANDOFF_PATH_RE,
    _ID_SENTINEL,
    _LIVE_STATE_ROUTINE_SIGNAL_KINDS,
    _MACHINE_KEYS,
    _REPO_KEYS,
    _SECTION_DERIVED_NULL_KEYS,
    _SECTION_DROP_KEYS,
    _SECTION_NORMALIZE_KEYS,
    _SHA_SENTINEL,
    _TS_SENTINEL,
    _VOLATILE_GIT_KEYS,
    _VOLATILE_GIT_OTHER_KEYS,
    _VOLATILE_SHA_KEYS,
    _VOLATILE_TIME_KEYS,
    _norm_handoff_path,
    _normalize,
    _relativize_abs_fixture_path,
)
from coordinator_core.ops.emit.envelope import resolve_coordinator_root  # noqa: E402


def normalize_record(record: dict) -> dict:
    """Canonicalize volatile + emit-derived fields on one record (recursively)."""
    return _normalize(record)


def normalize_records(records: list) -> list:
    """Normalize a list of records; comparison callers sort for order-insensitivity."""
    return [_normalize(r) for r in records]


def _sorted_key(record) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False)


def _normalize_and_sort(records: list) -> list:
    return sorted((_normalize(r) for r in records), key=_sorted_key)


def _normalize_section(value):
    """Section-parity normalizer: derived fields → None, volatile fields → typed sentinels.

    Applied to BOTH sides in assert_section_parity.  The golden carries post-enrichment
    values (deliverable_status, shipped_sha) while collect() is pre-enrichment and returns
    None.  Nulling both sides means the assertion correctly detects a porter that accidentally
    populates a derived field — compare to sentinel-on-both-sides which would silently
    pass any non-null value (the Staff Engineer F0 fix).

    Typed sentinels per AC5-PROVENANCE oracle (emission-conformance-contract.md § AC5-PROVENANCE):
    timestamps → _TS_SENTINEL, git SHAs → _SHA_SENTINEL, string IDs → _ID_SENTINEL.
    """
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if key in _SECTION_DROP_KEYS:
                continue
            if key in _SECTION_DERIVED_NULL_KEYS:
                out[key] = None
            # Review: code-reviewer — F1: per-type sentinel dispatch (mirrors _normalize).
            elif key in _VOLATILE_TIME_KEYS:
                out[key] = _TS_SENTINEL
            elif key in _VOLATILE_SHA_KEYS - frozenset({"shipped_sha"}):
                # shipped_sha is in _SECTION_DERIVED_NULL_KEYS; skip it here.
                out[key] = _SHA_SENTINEL
            elif key in _VOLATILE_GIT_OTHER_KEYS or key in _REPO_KEYS or key in _MACHINE_KEYS:
                out[key] = _ID_SENTINEL
            elif (
                key == "computed_state"
                and value.get("kind") in _LIVE_STATE_ROUTINE_SIGNAL_KINDS
            ):
                # weekly/distill-backlog routine_signals read REAL ambient coordinator
                # state (real_home marker, see normalizers.py docstring) — volatile
                # across days/machines, not fixture-scoped. Mirrors _normalize's
                # production handling so section- and full-parity agree.
                out[key] = _ID_SENTINEL
            elif key == "path" and isinstance(val, str):
                out[key] = _relativize_abs_fixture_path(_norm_handoff_path(val))
            elif key == "source_path" and isinstance(val, str):
                out[key] = _relativize_abs_fixture_path(val)
            else:
                out[key] = _normalize_section(val)
        return out
    if isinstance(value, list):
        return [_normalize_section(item) for item in value]
    return value


def _section_normalize_and_sort(records: list) -> list:
    return sorted((_normalize_section(r) for r in records), key=_sorted_key)


# --------------------------------------------------------------------------- live-coordinator-state escapes
# Some section porters read directly from ``ctx.coordinator_root`` — the REAL, mutable
# coordinator-claude checkout on this machine — rather than the frozen ``subprocess_root``
# fixture tree that ``build_emit_context()`` otherwise redirects every subprocess data read
# to. That is an architectural live-state escape distinct from ordinary golden drift: the
# golden was captured once against a real coordinator-claude checkout at a point in time,
# and these porters keep reading the LIVE, ever-changing checkout on every re-run, so the
# comparison rots independent of any producer regression. Confirmed pre-existing and
# explicitly tracked: commit ca650501 ("fix(strang-01 C6): finish C4a golden refresh —
# roadmaps live-state fragility remains backlog 2026-06-22").
#
# ``roadmaps`` — EVERY record is coordinator-claude-live-derived: ``_query_roadmap_records`` runs
# ``query-records.js`` with cwd resolved from ``ctx.central_state_root.parent`` for git
# root detection, but the fixture tree is deliberately not a git repo (see
# ``build_emit_context`` docstring), so resolution escapes to the real coordinator-claude checkout's own
# ``state/roadmap/*/OVERVIEW.md`` records — verified directly: this machine's current
# ``collect()`` output surfaces real coordinator-claude roadmap slugs (e.g. ``python-core-2026-07-01``,
# ``claude-klabauter-strangler-2026-07-04``) that do not exist anywhere in ``fixtures/root``.
# Excluded wholesale below rather than re-snapshotted — a re-snapshot buys days, not
# weeks, and re-arms the exact drift trap (same anti-goal the golden-drift fix for this
# whole test module exists to close).
_LIVE_COORDINATOR_STATE_SECTIONS = frozenset({"roadmaps"})

# ``routine_signals`` shares the SAME escape for two (of six) signal kinds only — the
# other four are either genuinely frozen (``docs``/``bug-sweep`` read ``ctx.repo_root``,
# the fixture tree, which is not a git repo so ``_commits_since_last`` always degrades to
# its 99-commit sentinel on both golden-capture and re-run) or a static placeholder
# (``dormant-repo``). ``weekly`` (native ``check_weekly_staleness``, cwd=
# ``ctx.coordinator_root/"bin"``) and ``distill-backlog`` (``_count_distill_backlog``
# scans ``ctx.coordinator_root``'s real archive/wiki tree) both read the live checkout.
# Neutralized per-kind (not section-excluded) so the OTHER four signals keep real parity
# coverage — see the perturbation proof in this module's own execution report.
_LIVE_COORDINATOR_STATE_ROUTINE_SIGNAL_KINDS = frozenset({"weekly", "distill-backlog"})
_LIVE_STATE_SENTINEL = "__LIVE_COORDINATOR_STATE_NORMALIZED__"

# ``exec_summary.docs_staleness`` (C6, envelope._stamp_docs_staleness) is a post-collect
# enrichment that reads LIVE git history on the emitting repo (commits_since/days_since
# move every day and every commit) — it cannot be frozen into a golden captured at a
# point in time, and has no bash-oracle equivalent at all (the golden predates the field
# entirely — see entities/exec_summary.py's C6 spec backlink). Same shape as
# ``content_hash`` (_SECTION_DROP_KEYS above) rather than the routine_signals sentinel
# shape: the golden slice carries NO key at all (not merely a different value), so this
# must be a symmetric POP on both sides, not a sentinel substitution (which would compare
# key-present-with-sentinel against key-absent and still fail).
_EXEC_SUMMARY_LIVE_KEYS = frozenset({"docs_staleness"})


def _neutralize_live_coordinator_state_exec_summary(records: list) -> list:
    """Drop ``docs_staleness`` from exec_summary records before golden comparison.

    Applied to BOTH the collect()-derived candidate and the golden slice, mirroring
    ``_neutralize_live_coordinator_state_routine_signals``'s per-key neutralization
    pattern one section up — but as a pop (golden has no key at all), not a sentinel swap.
    """
    out = []
    for record in records:
        if isinstance(record, dict):
            record = {k: v for k, v in record.items() if k not in _EXEC_SUMMARY_LIVE_KEYS}
        out.append(record)
    return out


# ``handoffs.baton_class`` (cockpit contract 3.9.0) postdates the golden capture and the bash
# oracle never emitted it, so the golden slice carries NO key at all — identical shape to
# ``exec_summary.docs_staleness`` above, and handled the same way: a symmetric pop on both
# sides, not a sentinel swap.
#
# Why this exists IN ADDITION to the ``baton_class`` entry in ``_SECTION_DROP_KEYS``: the two
# parity paths normalize through different functions. Section parity runs ``_normalize_section``
# (which honours ``_SECTION_DROP_KEYS``); full parity runs ``_normalize_and_sort``, which does
# not. Dropping the field in one place fixes exactly one of the two tests — the section test
# went green while ``test_full_parity`` kept failing on this single key, which is how the split
# was found. Both paths need it, and neither is redundant.
#
# No coverage is lost: ``baton_class`` is a pure function of ``kind``, which is itself already
# dropped from parity comparison, so this compares a derivative of an uncompared field. Its
# real coverage is its own unit tests plus the mapping-completeness assertion that every
# HandoffKind except ``spike-result`` resolves to a class.
#
# ``producer`` (cockpit contract 3.12.0 producer axis) joins it for the same reason and travels
# the same two paths — the split documented above is exactly why it appears here AND in
# ``_SECTION_DROP_KEYS``; adding it to one alone leaves ``test_full_parity`` red, which is how
# the ``baton_class`` split was originally found. Unlike ``baton_class``, ``producer`` is not a
# derivative of an already-dropped field: its coverage lives in
# ``coordinator_core/contract/cockpit_schema/tests/test_producer_axis_entity.py``, not in a derivation argument.
_HANDOFF_POST_GOLDEN_KEYS = frozenset({"baton_class", "producer"})


def _neutralize_post_golden_handoff_fields(records: list) -> list:
    """Drop ``baton_class`` and ``producer`` from handoff records before golden comparison.

    Applied to BOTH the emitted candidate and the golden slice, mirroring
    ``_neutralize_live_coordinator_state_exec_summary`` above.
    """
    out = []
    for record in records:
        if isinstance(record, dict):
            record = {k: v for k, v in record.items() if k not in _HANDOFF_POST_GOLDEN_KEYS}
        out.append(record)
    return out


def _neutralize_live_coordinator_state_routine_signals(records: list) -> list:
    """Sentinel-normalize the two live-coordinator-state routine_signals record kinds.

    Applied to BOTH the collect() candidate and the golden slice before comparison, so a
    real regression in ``computed_state``/``overdue`` derivation for the OTHER four signal
    kinds (``docs``, ``arch-audit``, ``bug-sweep``, ``dormant-repo``) is still caught.
    """
    out = []
    for record in records:
        if record.get("kind") in _LIVE_COORDINATOR_STATE_ROUTINE_SIGNAL_KINDS:
            record = dict(record)
            record["computed_state"] = _LIVE_STATE_SENTINEL
            record["overdue"] = _LIVE_STATE_SENTINEL
            record["inputs"] = _LIVE_STATE_SENTINEL
        out.append(record)
    return out


# --------------------------------------------------------------------------- golden access
def load_golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def load_map() -> dict:
    raw = json.loads(MAP_PATH.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("__")}


def _dig(envelope: dict, dotpath: str) -> list:
    """Resolve a dot-path (e.g. 'completion_rollups.day') to its array in the envelope."""
    node = envelope
    for part in dotpath.split("."):
        node = node[part]
    if not isinstance(node, list):
        raise TypeError(f"envelope path {dotpath!r} is {type(node).__name__}, expected list")
    return node


def golden_slice(name: str, envelope: dict | None = None) -> tuple[list, list]:
    """Return (records, malformed) for a section, as the union of its mapped golden arrays."""
    envelope = envelope if envelope is not None else load_golden()
    spec = load_map()[name]
    records: list = []
    for path in spec["records"]:
        records.extend(_dig(envelope, path))
    malformed: list = []
    for key in spec["malformed"]:
        malformed.extend(envelope["malformed_records"][key])
    return records, malformed


# --------------------------------------------------------------------------- EmitContext
def build_emit_context():
    """Construct an EmitContext pointed at the FROZEN FIXTURE TREE (frozen-fixture doctrine).

    All section subprocess calls (query-records.js, query-completions.sh,
    list-review-trail-records.sh, emit-lesson-summaries.py) are redirected to
    ``fixtures/root`` via ``subprocess_root`` so Python collect() reads the SAME
    immutable tree as the bash golden was captured from — no live-state drift, no
    repo_root divergence, no LMA-timing race.

    ``coordinator_root`` remains the real coordinator plugin dir (the scripts and
    validators that execute must be real; only the DATA root is frozen).

    Volatile fields (SHAs, observed_at, hostname) are normalized out of parity so
    their exact values here are non-load-bearing.

    Imported lazily so this module still collects (and the zero-section skip fires)
    before C1's context.py has landed.
    """
    from coordinator_core.ops.emit.context import EmitContext  # lazy: C1 spine

    fixture_root = _FIXTURES / "root"
    coordinator_root = resolve_coordinator_root()

    # Frozen, not real-clock: matches the golden's own captured `observed_at`
    # (every provenance envelope in golden-cockpit-emission.json carries this exact
    # value). observed_at is normalized to a sentinel for comparison purposes, so its
    # raw value looks "non-load-bearing" — but rollups.collect()'s completion-window
    # cutoff is anchored to it, and a real-clock value drifts the frozen fixture
    # completion (created 2026-07-01) out of the 30-day window as real time advances,
    # producing a flake keyed to wall-clock date rather than to the frozen tree.
    observed_at = "2026-07-06T15:54:47Z"

    return EmitContext(
        repo_root=fixture_root,
        coordinator_root=coordinator_root,
        central_state_root=fixture_root / "state",
        # Fixed git values — the fixture is not a git repo; volatile fields are
        # normalized out of parity so these sentinel values are non-load-bearing.
        git_branch="work/fixture/2026-07-01",
        git_sha="aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee",
        git_sha_short="aaaaaaaa",
        observed_at=observed_at,
        hostname="fixture.local",
        repo_name="fixture-owner/fixture-repo",
        # Frozen-fixture doctrine: redirect ALL subprocess data reads to the fixture
        # tree so collect() output is reproducible across repeated runs.
        subprocess_root=fixture_root,
        # The golden was captured before the cadence gate existed, i.e. with every
        # enrichment computing for real. Parity is therefore a gate on the FULL-enrichment
        # tier specifically — the tier whose output must stay byte-identical. The cheap
        # tier's reuse-vs-null behaviour is covered by its own tests, not here.
        full_enrichment=True,
    )


# --------------------------------------------------------------------------- assertions
def assert_section_parity(name: str, ctx=None) -> None:
    """Gate one section porter against its golden slice.

    Imports coordinator_core.ops.emit.sections.<name>, calls collect(ctx), normalizes both
    the candidate output and the golden slice, and asserts order-insensitive equality of
    records and of the malformed bucket.

    Uses _section_normalize_and_sort (the Staff Engineer F0 fix): derived fields (deliverable_status,
    shipped_sha) → None on BOTH sides, volatile fields → typed sentinels per AC5-PROVENANCE
    oracle.  The golden was captured from the full post-enrichment envelope so derived fields
    have real values; nulling both sides means a porter that accidentally returns a non-null
    derived field will cause a mismatch (vs sentinel-on-both-sides which silently passed it).
    """
    module = importlib.import_module(f"{_SECTIONS_PKG}.{name}")
    ctx = ctx if ctx is not None else build_emit_context()
    records, malformed = module.collect(ctx)

    exp_records, exp_malformed = golden_slice(name)

    if name == "routine_signals":
        records = _neutralize_live_coordinator_state_routine_signals(records)
        exp_records = _neutralize_live_coordinator_state_routine_signals(exp_records)

    got_r = _section_normalize_and_sort(records)
    exp_r = _section_normalize_and_sort(exp_records)
    assert got_r == exp_r, (
        f"section {name!r}: record parity mismatch "
        f"(got {len(got_r)} records, golden {len(exp_r)})"
    )

    got_m = _section_normalize_and_sort(malformed)
    exp_m = _section_normalize_and_sort(exp_malformed)
    assert got_m == exp_m, (
        f"section {name!r}: malformed-bucket parity mismatch "
        f"(got {len(got_m)}, golden {len(exp_m)})"
    )


def assert_full_parity(emission: dict) -> None:
    """Gate a full Python emission against the normalized golden (used by C3).

    Compares every section's record array and malformed bucket order-insensitively, then
    checks the scalar/derived envelope keys that are not volatile.
    """
    golden = load_golden()
    section_map = load_map()

    for name in section_map:
        if name in _NO_GOLDEN_ORACLE_SECTIONS:
            # Net-new record type, no bash-golden equivalent (see that set's docstring) —
            # covered by a bespoke fixture-commit test instead, not the golden-slice diff.
            continue
        if name in _LIVE_COORDINATOR_STATE_SECTIONS:
            # F7: roadmaps section compares against LIVE coordinator-claude state, not a frozen fixture
            # sub-tree — see _LIVE_COORDINATOR_STATE_SECTIONS docstring above. golden drift
            # here is not a producer regression; excluded rather than re-snapshotted (a
            # re-snapshot re-arms the same drift trap this module exists to close). The
            # definitive fix (frozen roadmap records under fixtures/root/state/roadmap/,
            # and the roadmaps porter honoring subprocess_root) is a production-code change
            # out of this test module's scope — tracked per commit ca650501's backlog note.
            continue
        exp_records, exp_malformed = golden_slice(name, golden)
        got_records, got_malformed = golden_slice(name, emission)
        if name == "routine_signals":
            exp_records = _neutralize_live_coordinator_state_routine_signals(exp_records)
            got_records = _neutralize_live_coordinator_state_routine_signals(got_records)
        if name == "exec_summary":
            exp_records = _neutralize_live_coordinator_state_exec_summary(exp_records)
            got_records = _neutralize_live_coordinator_state_exec_summary(got_records)
        if name == "handoffs":
            exp_records = _neutralize_post_golden_handoff_fields(exp_records)
            got_records = _neutralize_post_golden_handoff_fields(got_records)
        assert _normalize_and_sort(got_records) == _normalize_and_sort(exp_records), (
            f"full parity: section {name!r} record mismatch"
        )
        assert _normalize_and_sort(got_malformed) == _normalize_and_sort(exp_malformed), (
            f"full parity: section {name!r} malformed mismatch"
        )

    # Non-volatile scalar envelope invariants.
    # schema_version is NOT compared against the golden: it is a passthrough of the vendored
    # cockpit-contract pin, not emitter behaviour, so it legitimately moves on every contract
    # re-vendor while the golden stays frozen at the version it was captured under (2.20.0,
    # captured by a since-retired bash oracle — there is no regeneration path/UPDATE_GOLDEN
    # flag to re-capture it at the current pin). Diffing it against the frozen capture
    # manufactures a false failure on every bump — the same drift trap
    # `_LIVE_COORDINATOR_STATE_SECTIONS` above exists to close, and it fired for real on the
    # 2.20.0 -> 2.21.0 D33 re-vendor.
    #
    # Compared against CONTRACT_VERSION (cockpit_schema's independent literal source of
    # truth), NOT validate.read_schema_version() — build() itself derives schema_version via
    # validate.assert_version_consistency(), which internally calls read_schema_version() and
    # returns it unchanged (validate.py). Comparing emission["schema_version"] back against
    # read_schema_version() would be tautological (same function, same call, would pass even
    # if read_schema_version() itself started returning a stale/wrong value) — CONTRACT_VERSION
    # is a genuinely independent value, and test_vendor_pin_version_consistency.py is the
    # dedicated, obviously-named guard for CONTRACT_VERSION-vs-bundle desync; this assertion
    # only confirms build() didn't additionally corrupt/hardcode the value in transit.
    from coordinator_core.contract.cockpit_schema import CONTRACT_VERSION as _CONTRACT_VERSION

    assert emission.get("schema_version") == _CONTRACT_VERSION, (
        "schema_version drift — envelope did not stamp CONTRACT_VERSION"
    )
    assert emission.get("narrative_views") == golden["narrative_views"], "narrative_views drift"


# --------------------------------------------------------------------------- no-golden-oracle exemption
# Sections with "Parity oracle: none" in their own module docstring — net-new record types the
# bash emitter never produced, so there is no golden slice to diff against (the golden fixture
# was captured once, from the bash oracle, and is not retroactively extended per net-new section).
# Exempted from the discovery-driven golden-slice comparison (test_section_parity,
# test_map_covers_every_envelope_array_and_malformed_bucket, assert_full_parity) and covered
# instead by a bespoke fixture-commit test (see test_commit_closures_* below).
# Spec backlink: pln-commit-closure-emission-fact-e-c22b04 § C4.
#
# Review: code-reviewer (Finding 5) — nothing enforces that this set stays in sync with each
# module's own "Parity oracle: none" docstring line; a future section could be added here
# without the matching docstring, or vice versa, and silently diverge. NOT auto-derived from a
# grep of section docstrings: as of this writing, `sections/roadmap_dag.py` ALSO declares
# "Parity oracle: none" but is intentionally NOT in this set (it has real golden coverage via
# a different mechanism — see its own module docstring, D3) — so "declares no bash oracle" and
# "belongs in this frozenset" are related-but-distinct predicates, not a 1:1 grep-checkable
# convention. Keep this set manually in sync with `commit_closures`-shaped net-new sections
# (no bash-golden equivalent AND no bespoke-oracle coverage of their own) when adding new ones.
_NO_GOLDEN_ORACLE_SECTIONS = frozenset({"commit_closures"})

# Non-porter helper modules colocated under sections/ for import ergonomics — no `collect()`,
# never wired into envelope.py, and not a "section" under any of the predicates above (unlike
# _NO_GOLDEN_ORACLE_SECTIONS, this isn't a section lacking an oracle — it's not a section at
# all). Kept without the `_shared.py`-style leading-underscore convention because it is a named,
# public extraction point another op (handoff.columns, C3) imports directly — see
# sections/handoff_columns.py's own module docstring.
# Spec backlink: docs/plans/2026-08-11-pull-surface-for-cockpit-the-four-columns-and-the-archive.md § C1. [DEAD-CITATION: plan file never committed to this repo]
_NON_PORTER_HELPER_MODULES = frozenset({"handoff_columns"})


# --------------------------------------------------------------------------- discovery + params
def _discover_sections() -> list[str]:
    if not _SECTIONS_DIR.is_dir():
        return []
    return sorted(
        p.stem
        for p in _SECTIONS_DIR.glob("*.py")
        if not p.stem.startswith("_")
    )


# Discovery drives the golden-slice comparison, which is only meaningful for a section that
# has a golden slice to compare against — exclude _NO_GOLDEN_ORACLE_SECTIONS (net-new record
# types with no bash equivalent; see that set's docstring). They get a bespoke test instead.
# Also exclude _NON_PORTER_HELPER_MODULES — never section porters in the first place.
_DISCOVERED = [
    name for name in _discover_sections()
    if name not in _NO_GOLDEN_ORACLE_SECTIONS and name not in _NON_PORTER_HELPER_MODULES
]


# Collection-time corpus guards (house idiom — see test_verify_schema_registry_sync.py
# b7a56cec and test_schema_validate.py's _LEGACY_YAML_FIXTURE_NAMES assert): the two checks
# above (test_section_parity's discovered -> map membership, and
# test_map_covers_every_envelope_array_and_malformed_bucket's map -> golden coverage) only
# ever catch a section ADDED without a map entry or a map entry without golden coverage.
# Neither catches a section porter file being deleted or underscore-prefixed: _DISCOVERED
# just silently shrinks and that section's test_section_parity case vanishes from the
# parametrize list with zero signal — this is exactly the partial-narrowing hazard, not
# the already-handled total-empty case (see the `or [pytest.param(None, ...)]` fallback
# below). Close the reverse direction: every section in the committed
# section_envelope_map.json (minus _NO_GOLDEN_ORACLE_SECTIONS) must have a live discovered
# porter, and the discovered count itself is pinned so a narrowing shows up even if the
# map was (wrongly) edited to match.
_EXPECTED_DISCOVERED_COUNT = 20

assert len(_DISCOVERED) == _EXPECTED_DISCOVERED_COUNT, (
    f"discovered section porter count changed ({len(_DISCOVERED)} != "
    f"{_EXPECTED_DISCOVERED_COUNT}) under {_SECTIONS_DIR} — a section .py file was added, "
    "renamed, or deleted. If intended, update _EXPECTED_DISCOVERED_COUNT deliberately; if "
    "not, a section porter's parity test just silently vanished from test_section_parity."
)

_MAPPED_SECTIONS = set(load_map()) - _NO_GOLDEN_ORACLE_SECTIONS
_UNDISCOVERED_MAPPED_SECTIONS = _MAPPED_SECTIONS - set(_DISCOVERED)
assert not _UNDISCOVERED_MAPPED_SECTIONS, (
    f"section(s) {sorted(_UNDISCOVERED_MAPPED_SECTIONS)} have an entry in "
    f"section_envelope_map.json but no matching module under {_SECTIONS_DIR} — a section "
    "porter was deleted or renamed without updating the map, silently dropping its "
    "test_section_parity case."
)


_LIVE_STATE_SKIP_REASON = (
    "live-coordinator-state escape (F7): reads ctx.coordinator_root (the real, mutable "
    "coordinator-claude checkout) instead of the frozen fixture tree — golden drift here "
    "is not a producer regression. See _LIVE_COORDINATOR_STATE_SECTIONS docstring; "
    "tracked per commit ca650501's backlog note."
)


def _section_param(name: str):
    if name in _LIVE_COORDINATOR_STATE_SECTIONS:
        return pytest.param(name, marks=pytest.mark.skip(reason=_LIVE_STATE_SKIP_REASON))
    return name


@pytest.mark.parametrize(
    "name",
    [_section_param(name) for name in _DISCOVERED]
    or [pytest.param(None, marks=pytest.mark.skip(reason="no section porters present yet"))],
)
def test_section_parity(name):
    """Auto-gate every discovered section porter (with a bash-golden oracle) against the
    golden fixture. Sections with no oracle (_NO_GOLDEN_ORACLE_SECTIONS) are excluded — see
    that set's docstring — and covered by their own bespoke test instead. Sections with a
    known live-coordinator-state escape (_LIVE_COORDINATOR_STATE_SECTIONS) are skipped with
    a named reason rather than asserted — see that set's docstring."""
    section_map = load_map()
    assert name in section_map, (
        f"section module {name!r} has no entry in section_envelope_map.json — "
        f"add its envelope slice before the porter lands"
    )
    assert_section_parity(name)


# --------------------------------------------------------------------------- harness self-checks
def test_golden_fixture_is_present_and_shaped():
    golden = load_golden()
    assert golden["schema_version"], "golden missing schema_version"
    assert "malformed_records" in golden
    assert isinstance(golden["routine_signals"], list) and len(golden["routine_signals"]) == 6


def test_map_covers_every_envelope_array_and_malformed_bucket():
    """The section->envelope map must partition every emitted array + malformed bucket.

    Sections in _NO_GOLDEN_ORACLE_SECTIONS are excluded from the golden-coverage check: the
    golden fixture is a frozen one-time bash-oracle capture and is never retroactively extended
    for a net-new record type the bash emitter never produced (see that set's docstring).
    """
    golden = load_golden()
    section_map = {
        name: spec for name, spec in load_map().items() if name not in _NO_GOLDEN_ORACLE_SECTIONS
    }

    mapped_record_paths: set[str] = set()
    mapped_malformed: set[str] = set()
    for spec in section_map.values():
        mapped_record_paths.update(spec["records"])
        mapped_malformed.update(spec["malformed"])

    # Every dot-path resolves to a real array.
    for path in mapped_record_paths:
        _dig(golden, path)

    # Every malformed bucket in the golden is owned by exactly one section.
    golden_malformed = set(golden["malformed_records"].keys())
    assert mapped_malformed == golden_malformed, (
        f"malformed-bucket coverage gap: "
        f"unmapped={golden_malformed - mapped_malformed}, "
        f"phantom={mapped_malformed - golden_malformed}"
    )

    # Every top-level array key in the golden is covered by some record path's head.
    array_top_keys = {
        k for k, v in golden.items() if isinstance(v, list) and k != "malformed_records"
    }
    mapped_heads = {p.split(".")[0] for p in mapped_record_paths}
    assert array_top_keys <= mapped_heads, (
        f"uncovered top-level arrays: {array_top_keys - mapped_heads}"
    )


# --------------------------------------------------------------------------- C3: full parity
def test_full_parity(tmp_path):
    """Assert that the Python emitter produces the same output as the bash golden (C3).

    Builds the Python emission via envelope.build() with all sections wired (artifact_emit
    triggers the wiring at import), writes it to a temp file, then calls assert_full_parity()
    to compare every section's records + malformed bucket (order-insensitive, volatile fields
    normalized) and the scalar envelope invariants (schema_version, narrative_views).

    This test is the gate for C3 — it passes only when:
    (a) all 21 sections are registered and their collect() runs without error, AND
    (b) the Python emission matches the golden for every section, AND
    (c) the deliverable_status cross-join and shipped_sha/LMA enrichments produce the same
        values as the bash emitter's §8.16 + §1.5 + §1 step 3.5.
    """
    # Trigger section wiring side-effect (artifact_emit._wire_sections call at module import).
    import coordinator_core.ops.artifact_emit  # noqa: F401

    from coordinator_core.ops.emit import envelope as _envelope

    ctx = build_emit_context()

    # Use a temp out path so the sentinel gate does not fire during testing.
    out = tmp_path / "test-emission.json"
    _envelope.emit(ctx, out=out)
    emission = json.loads(out.read_text())

    assert_full_parity(emission)


# --------------------------------------------------------------------------- commit_closures
# Bespoke coverage for the `commit_closures` section (in _NO_GOLDEN_ORACLE_SECTIONS — see that
# set's docstring): a real, throwaway git repo rather than the frozen parity fixture tree
# (which is deliberately NOT a git repo, so a live `git log` scan against it always yields
# nothing to assert over). Spec backlink: pln-commit-closure-emission-fact-e-c22b04
# § C3/C4, AC3, AC4, AC5.

def _run_git_or_raise(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_closure_test_repo(repo_root: Path) -> None:
    """Init a throwaway git repo with a local identity (no reliance on global git config)."""
    _run_git_or_raise(repo_root, "init", "-q")
    _run_git_or_raise(repo_root, "config", "user.email", "test@example.com")
    _run_git_or_raise(repo_root, "config", "user.name", "Test User")
    _run_git_or_raise(repo_root, "config", "commit.gpgsign", "false")


def _commit_with_message(repo_root: Path, message: str, content: str) -> str:
    """Write unique ``content`` to a tracked file and commit ``message``; return the new SHA."""
    (repo_root / "file.txt").write_text(content)
    _run_git_or_raise(repo_root, "add", "-A")
    _run_git_or_raise(repo_root, "commit", "-q", "-m", message)
    return _run_git_or_raise(repo_root, "rev-parse", "HEAD")


def _closure_test_ctx(repo_root: Path):
    from coordinator_core.ops.emit.context import EmitContext

    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root / "state",
        git_branch="main",
        git_sha="a" * 40,
        git_sha_short="aaaaaaaa",
        observed_at="2026-07-17T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
    )


def test_commit_closures_fixture_commit_yields_record_with_reachability(tmp_path: Path) -> None:
    """A commit carrying a Closes: trailer yields a commit_closures record, with reachability
    stamped True once the enricher runs (AC3/AC4). collect() itself leaves
    reachable_on_default_branch null by construction (commit_closures.py docstring) — the
    stamp only appears after envelope._stamp_closure_reachability runs.
    """
    from coordinator_core.ops.emit.envelope import _stamp_closure_reachability
    from coordinator_core.ops.emit.sections import commit_closures

    _init_closure_test_repo(tmp_path)
    sha = _commit_with_message(
        tmp_path, "fix: close an item\n\nCloses: RECS-42\n", "fixture-content-1\n"
    )
    # No real remote in this throwaway repo — a bare local ref stands in for "origin/main"
    # so collect()'s `git log origin/main HEAD ...` and the reachability check both resolve.
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", sha)

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert len(records) == 1, f"expected exactly one Closes: record, got {records!r}"
    record = records[0]
    assert record["item_id"] == "RECS-42"
    assert record["sha"] == sha
    assert record["repo"] == "test/repo"
    assert record["reachable_on_default_branch"] is None, (
        "collect() must never resolve reachability itself (AC4) — left null by construction"
    )

    _stamp_closure_reachability(records, tmp_path)
    assert records[0]["reachable_on_default_branch"] is True, (
        "the commit is origin/main itself; reachability must stamp True"
    )


# Review: code-reviewer (Finding 2) — AC4's tri-state (true/false/null, "never silently
# coerced to false") is the load-bearing correctness property of _stamp_closure_reachability;
# the fixture-commit test above only exercised the True branch. These two siblings cover the
# False (not-yet-merged commit) and None (no origin/main ref at all — degrade-to-null) paths.
def test_commit_closures_reachability_false_when_not_on_origin_main(tmp_path: Path) -> None:
    """A Closes: commit that exists, but origin/main points at an EARLIER commit, must stamp
    reachable_on_default_branch False (AC4) — the single most common real-world case."""
    from coordinator_core.ops.emit.envelope import _stamp_closure_reachability

    _init_closure_test_repo(tmp_path)
    base_sha = _commit_with_message(tmp_path, "chore: base", "base\n")
    # origin/main stays pinned at base_sha — the closure commit below is never an ancestor.
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", base_sha)
    closure_sha = _commit_with_message(
        tmp_path, "fix: close an item\n\nCloses: RECS-42\n", "unmerged-content\n"
    )

    records = [{"sha": closure_sha, "reachable_on_default_branch": None}]
    _stamp_closure_reachability(records, tmp_path)

    assert records[0]["reachable_on_default_branch"] is False, (
        "closure_sha is not an ancestor of origin/main (which is pinned at an earlier "
        "commit); reachability must stamp False, not None or True"
    )


def test_commit_closures_reachability_none_when_no_origin_main_ref(tmp_path: Path) -> None:
    """No refs/remotes/origin/main ref at all must degrade to None (AC4) — never coerced
    to False. Exercises _check_origin_main_reachable's failure branch deterministically,
    since there's no real origin remote in the throwaway repo for _fetch_origin_main to
    reach."""
    from coordinator_core.ops.emit.envelope import _stamp_closure_reachability

    _init_closure_test_repo(tmp_path)
    sha = _commit_with_message(
        tmp_path, "fix: close an item\n\nCloses: RECS-42\n", "no-origin-content\n"
    )
    # Deliberately do NOT create refs/remotes/origin/main.

    records = [{"sha": sha, "reachable_on_default_branch": None}]
    _stamp_closure_reachability(records, tmp_path)

    assert records[0]["reachable_on_default_branch"] is None, (
        "no origin/main ref exists; reachability must degrade to None (indeterminate), "
        "never be coerced to False"
    )


def test_commit_closures_no_trailer_yields_no_records(tmp_path: Path) -> None:
    """A commit with no Closes: trailer yields zero records (F7 guard: prose false-positives
    structurally cannot occur since C1 only ever sees already-extracted trailer values)."""
    from coordinator_core.ops.emit.sections import commit_closures

    _init_closure_test_repo(tmp_path)
    sha = _commit_with_message(
        tmp_path, "docs: this commit closes the loop on nothing in particular", "body\n"
    )
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", sha)

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert records == []
    assert malformed == []


def test_commit_closures_collect_is_exactly_one_git_log_subprocess(tmp_path: Path) -> None:
    """collect() performs EXACTLY ONE subprocess call — the bounded git log (AC5).

    Scoped to collect() itself, not total git-call count across a full emit run:
    reachability's amortized per-SHA `git merge-base --is-ancestor` spawns (AC4) are legitimate
    and live in a separate function (_stamp_closure_reachability), never invoked here.
    """
    from coordinator_core.ops.emit.sections import commit_closures

    _init_closure_test_repo(tmp_path)
    sha = _commit_with_message(
        tmp_path, "fix: close an item\n\nCloses: RECS-7\n", "fixture-content-2\n"
    )
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", sha)

    ctx = _closure_test_ctx(tmp_path)

    with patch(
        "coordinator_core.ops.emit.sections.commit_closures.subprocess.run",
        wraps=subprocess.run,
    ) as spy:
        records, _malformed = commit_closures.collect(ctx)

    assert spy.call_count == 1, (
        f"collect() must spawn exactly one subprocess (the bounded git log, AC5); "
        f"got {spy.call_count} calls: {spy.call_args_list!r}"
    )
    (cmd,), _kwargs = spy.call_args
    assert cmd[:3] == ["git", "-C", str(tmp_path)]
    assert "log" in cmd, f"the one collect() subprocess must be a git log call, got {cmd!r}"
    assert len(records) == 1


# Review: code-reviewer (Finding 3) — AC3 names "malformed rows route to
# malformed_records.commit_closures" as an acceptance criterion, but no test reached
# _extract_closure_commits's SHA-validation quarantine branch: a well-formed `git log` run
# cannot itself produce a truncated/non-hex SHA (module docstring), so this needs a
# monkeypatched subprocess.run result rather than a real repo fixture.
def test_commit_closures_malformed_sha_routes_to_malformed_bucket(tmp_path: Path) -> None:
    """A git-log record whose SHA fails 40-char lowercase-hex validation must land in the
    malformed bucket with the documented reason, not be silently dropped or emitted as a
    record with a corrupt identity key (AC3)."""
    from coordinator_core.ops.emit.sections import commit_closures

    _init_closure_test_repo(tmp_path)
    ctx = _closure_test_ctx(tmp_path)

    # Fake NUL-delimited stdout matching commit_closures._LOG_FORMAT
    # ("%x00%H%x00%(trailers:key=Closes,valueonly)"): one malformed (non-40-hex) SHA record
    # and one well-formed record, so the quarantine branch and the happy path both fire in
    # the same subprocess result.
    fake_stdout = (
        "\x00" + "not-a-valid-sha" + "\x00" + "RECS-99\n"
        + "\x00" + ("b" * 40) + "\x00" + "RECS-1\n"
    )
    fake_result = subprocess.CompletedProcess(
        args=["git", "log"], returncode=0, stdout=fake_stdout, stderr=""
    )

    with patch(
        "coordinator_core.ops.emit.sections.commit_closures.subprocess.run",
        return_value=fake_result,
    ):
        records, malformed = commit_closures.collect(ctx)

    assert malformed == [
        {
            "sha": "not-a-valid-sha",
            "reason": "git-log record failed 40-char lowercase-hex SHA validation",
        }
    ], f"malformed bucket did not quarantine the invalid SHA as documented: {malformed!r}"
    assert len(records) == 1, f"the well-formed record must still be emitted: {records!r}"
    assert records[0]["sha"] == "b" * 40
    assert records[0]["item_id"] == "RECS-1"
