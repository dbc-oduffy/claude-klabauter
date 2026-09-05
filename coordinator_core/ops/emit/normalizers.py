"""
coordinator_core.ops.emit.normalizers — shared AC5-PROVENANCE normalization utilities.

Purpose: typed sentinels and the ``_normalize`` helper shared by the strang-01 parity
tests (``test_emit_parity``) and the strang-02 DoE-HEAD conformance drift-check
(``test_doe_drift``).  Extracted from the test leaf so both modules import from a
stable production location rather than cross-importing between test files — avoiding
pytest collection-isolation breakage and making the surface available for future
runtime callers.

A field is normalized when its value legitimately differs between the golden-capture
run and a later Python emission on the SAME machine.  We canonicalize (replace the
value) rather than delete so structural presence is still asserted.  Applied
recursively so provenance-nested and watermark-nested copies are hit.

Oracle: DoE emission-conformance-contract.md § AC5-PROVENANCE (three distinct sentinels).
strang-02 cross-repo conformance comparison normalizes to these same values on the DoE
side; using a single ``<NORMALIZED>`` for all field types would cause every
timestamp/SHA field to compare unequal when DoE normalizes to epoch-zero / zero-SHA.

Spec backlink: state/handoffs/2026-07-04_201949_roadmap-strang-02.md (strang-02)
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C1
"""
from __future__ import annotations

import re as _re

# ---------------------------------------------------------------------------
# Typed sentinels
# ---------------------------------------------------------------------------
# Review: code-reviewer — F1: typed sentinels per AC5-PROVENANCE oracle.
# Oracle: DoE emission-conformance-contract.md § AC5-PROVENANCE (three distinct sentinels).
_TS_SENTINEL = "1970-01-01T00:00:00Z"                      # epoch-zero for all timestamp fields
_SHA_SENTINEL = "0000000000000000000000000000000000000000"  # zero SHA for git commit SHA fields
_ID_SENTINEL = "__NORMALIZED__"                             # string-ID sentinel: branch/repo/REPO_NAME

# ---------------------------------------------------------------------------
# Volatile field key sets
# ---------------------------------------------------------------------------
# Timestamps that move with wall-clock / git-HEAD between two runs.
_VOLATILE_TIME_KEYS = frozenset({
    "observed_at",          # provenance + record-level capture time
    "emitted_at",           # envelope emit time
    "computed_as_of",       # routine-signal evaluation time
    "max_observed_at",      # rollup input watermark
    "last_meaningful_activity",  # LMA (git-log derived)
    "last_modified_at",     # LMA (git-log derived)
    "last_commit_at",       # branch tip commit time
    "last_commit_message",  # branch tip commit subject
    "last_activity_at",     # GitHub API — repo last-push timestamp, moves with each push
    # Review: overengineering-reviewer (Kira) — "period" removed. Both `_local_day` and
    # `_iso_week` now derive from `ctx.observed_at` (frozen in the parity fixture), not the
    # wall clock, so the field is deterministic and the golden should pin it, not normalize
    # it away.
})
# Git SHA fields — volatile as commits land.  Normalized to zero-SHA (_SHA_SENTINEL) per
# AC5-PROVENANCE oracle (provenance.ref.sha → "0000000000000000000000000000000000000000").
_VOLATILE_SHA_KEYS = frozenset({
    "git_sha", "git_sha_short",
    "tip_sha", "merge_base_sha",
    "max_commit_sha",
    "sha",        # handoff shipped_in.sha
    "shipped_sha",  # emit-DERIVED (null in collect())
})
# Non-SHA git-volatile fields — counts and string IDs that move as commits land.
# Normalized to _ID_SENTINEL (string IDs and counts alike — both sides normalize the same way).
_VOLATILE_GIT_OTHER_KEYS = frozenset({
    "ahead_by", "behind_by",
    # Commit-count inputs for routine_signals — advance with every commit to the meta-repo.
    "commits_since_bug_sweep",
    "commits_since_update_docs",
    # AC5-PROVENANCE field 3 (emission-conformance-contract.md § AC5-PROVENANCE):
    # provenance.ref.branch varies between machines / sessions; normalize to _ID_SENTINEL.
    "branch",
})
# Union for downstream set operations (e.g. _SECTION_NORMALIZE_KEYS - shipped_sha).
_VOLATILE_GIT_KEYS = _VOLATILE_SHA_KEYS | _VOLATILE_GIT_OTHER_KEYS
# Emit-DERIVED fields populated AFTER section collect() — null/absent in collect() output.
#
# Section parity (assert_section_parity): both sides → None so the comparison confirms
# collect() returns null for these fields and does NOT silently pass a wrong non-null
# enriched value (the Staff Engineer F0: pure <NORMALIZED>-on-both-sides hides that failure).
#
# Full parity (assert_full_parity): deliverable_status compared directly (cross-join must
# reproduce bash value); shipped_sha normalized via _VOLATILE_GIT_KEYS (git SHA, volatile).
_SECTION_DERIVED_NULL_KEYS = frozenset({"deliverable_status", "shipped_sha"})

# content_hash (R5 change-signal) is an emit-DERIVED enrichment attached in envelope.build's
# _stamp_content_hash pass — section collect() never produces it. So the golden slice carries
# it while collect() output does not; nulling would leave key-present-vs-absent asymmetry.
# Section parity therefore DROPS it on both sides. Full parity keeps it: against the frozen
# fixture tree its hash is deterministic (constant bytes), so both sides match exactly.
#
# kind (roadmap_dag routing token) is an internal routing tag produced by
# sections/roadmap_dag.py collect() and stripped by _place_roadmap_dag before records land
# in the envelope. The golden slice comes from the post-placement envelope (no kind field);
# section parity DROPS it on both sides so the raw collect() output compares correctly
# against the golden. kind is NEVER a contract field — it is a pure internal routing token.
#
# _goal_ids (InitiativeSummary goals[] staging, 2.13.0) is an internal staging key stamped by
# sections/initiatives.py collect() and popped by envelope._stamp_initiative_goals before the
# wire write (the InitiativeSummary schema is .strict()). Same shape as kind: the golden slice
# is post-enrichment (no _goal_ids), so section parity DROPS it on both sides so the raw
# collect() output compares correctly. _goal_ids is NEVER a contract field.
#
# _supersedes_raw was a PlanSummary superseded_by reverse-edge staging key (dead-join fix,
# 2026-07-21) stamped and popped entirely inside sections/plans.py::collect() (self-join —
# plans.py already has visibility into every plan record, so the reverse-edge derivation runs
# as a second in-collect() pass rather than a post-collect envelope enricher; see that
# module's docstring). Because the pop now happens before collect() returns, the key never
# reaches _normalize_section in the first place — no drop entry needed here.
#
# archived / decision_note (CrossRepoMemoSummary, 2026-07-24 C6 — plan
# docs/plans/2026-07-24-cross-repo-memo-ownership-and-redesign.md) are additive fields with
# no bash-oracle equivalent: emit-cockpit-snapshot.sh's cross-repo-memo section (§ 8.7) never
# emitted an archived-set bucket or a decision_note excerpt, so the frozen golden fixture
# carries neither key. Same shape as content_hash immediately above — dropped from the
# byte-parity COMPARISON only, never from the actual emitted envelope (sections/
# cross_repo_memos.py::collect() still stamps both on every real record).
#
# Review: code-reviewer (F5) — `body` (CrossRepoMemoSummary, 2026-07-24 C8, same plan) is
# a THIRD same-chunk-family additive field but is DELIBERATELY NOT added here, unlike its
# archived/decision_note siblings above. archived/decision_note are dropped from the golden
# comparison because their presence/value never needs to match byte-for-byte; `body` is
# the opposite case on purpose — it IS compared byte-exact against a hardcoded golden
# string in fixtures/golden-cockpit-emission.json, because that comparison is the load-
# bearing assertion that _read_memo_body correctly re-derives body content (frontmatter
# stripped, .strip()-ped) from the frozen fixture memo file. Adding "body" here would
# silently remove the one test that actually exercises C8's core behavior. If the fixture
# memo file's body ever changes, update the golden string to match — don't drop-key body.
# `baton_class` (HandoffSummary, cockpit contract 3.9.0) is dropped for the same reason
# `docs_staleness` is popped from exec_summary further down the parity module: the golden was
# captured once from the bash oracle, that oracle never emitted this field, and the golden
# therefore carries NO key at all — so the only honest options are a symmetric drop or
# recapturing a golden whose whole value is being a frozen pre-port artifact. Drop wins.
#
# The narrower reason it costs no coverage: `baton_class` is a PURE FUNCTION of `kind`, and
# `kind` is already in this same set — comparing a derivative of a field parity does not
# compare adds nothing. Real coverage lives where it can actually assert something: the
# derivation's own unit tests, the mapping-completeness test asserting every HandoffKind
# except `spike-result` has an `x-baton-class` entry, and a live `emit-cadence` run.
# Contrast `body` below — do NOT read this entry as licence to drop an additive field whose
# comparison IS the load-bearing assertion.
# `producer` (HandoffSummary, cockpit contract 3.12.0 producer axis) is the same shape as
# `baton_class` one paragraph up and is dropped for the same reason: it postdates the golden
# capture, the retired bash oracle never emitted it, and the golden carries NO key at all.
#
# Its no-coverage-lost argument is NOT `baton_class`'s, though, and must not be read as such —
# `producer` is not a derivative of any already-dropped field. It costs no coverage here because
# its assertions live in a dedicated home: `coordinator_core/contract/cockpit_schema/tests/test_producer_axis_
# entity.py`. Drop it from the frozen-golden diff, never from that.
#
# `roadmap_id` (HandoffSummary, cockpit contract 4.2.0, added at 9e91bb19d4) joins the set on
# `producer`'s reasoning, not `baton_class`'s: it is not a derivative of an already-dropped
# field. It postdates the 2.20.0 golden capture, so the frozen slice carries no key at all and
# the diff can assert nothing about it in either direction. Its real coverage is the contract
# schema tests (`cockpit_schema/tests/test_new_entity_schemas.py`,
# `contract/tests/test_human_axis_contract_fields.py`) plus the mint-time carry test
# (`baton_assemble/tests/test_roadmap_identity_carry.py`); the porter leg is a bare frontmatter
# passthrough (`sections/handoffs.py::collect`). Dropped from the frozen-golden diff only.
#
# `fact_window` (RollupSummary, added at 9fb69f530f so rollup rows name the window their facts
# were computed over) joins for the same reason and with the same limit: the retired bash
# oracle emitted no window, so the golden carries no key. Unlike the passthroughs above this is
# a COMPUTED field, so the drop is only defensible because it has a dedicated oracle of its own
# — `ops/emit/tests/test_rollups_fact_window.py`. Drop it from the frozen-golden diff, never
# from that test. Per the `body` contrast above, do not extend this entry to a computed field
# that has no such home.
#
# `actioned_at` (CrossRepoMemoSummary, cockpit contract 4.4.0) joins the same set for the
# same reason as `archived`/`decision_note` above: the retired bash oracle's § 8.7 never
# emitted a closure date, so the frozen golden carries no key at all. Dropped from the
# byte-parity COMPARISON only — sections/cross_repo_memos.py::collect() still stamps it on
# every record whose memo records a closure time. Its real coverage lives in that section's
# own unit tests over the precedence chain and the never-infer-from-picked_up_at rule,
# which a frozen pre-port golden could not assert in the first place.
_SECTION_DROP_KEYS = frozenset(
    {
        "content_hash",
        "kind",
        "baton_class",
        "producer",
        "_goal_ids",
        "archived",
        "decision_note",
        "actioned_at",
        "roadmap_id",
        "fact_window",
    }
)

# Attribution slug — normalized per contract (REPO_NAME) to tolerate remote/machine variance.
# REPO_NAME is a hardcoded string field in the bash emitter
# (Port of: emit-cockpit-snapshot.sh, DoE 07eedcfb, 2026-07-19); included here so
# assert_full_parity is deterministic when comparing Python vs bash output
# (AC5-PROVENANCE field 7, emission-conformance-contract.md § AC5-PROVENANCE).
_REPO_KEYS = frozenset({"repo", "REPO_NAME"})
# Machine-identity, top-level only, but canonicalize wherever seen.
_MACHINE_KEYS = frozenset({"emitted_by_machine", "hostname"})

# routine_signals kinds whose computed_state is derived by shelling out to a script that
# reads REAL AMBIENT coordinator/archive state (not the frozen fixture tree) — this
# module's parity tests carry pytest.mark.real_home for exactly this reason. "weekly"
# reads coordinator_root/state/week-changelog/HEADER.md (real calendar/commit state,
# resets weekly); "distill-backlog" reads the real archive/completed backlog count via
# count-distill-backlog.sh. Both drift day-to-day/machine-to-machine independent of the
# frozen fixture, so computed_state for these two kinds is volatile and normalized here —
# mirroring the existing precedent of normalizing volatile INPUTS (commits_since_bug_sweep
# etc. in _VOLATILE_GIT_OTHER_KEYS) rather than a fabricated fixture value.
#
# "docs"/"bug-sweep" are NOT included: their git-log lookups run against ctx.repo_root
# (the frozen, non-git fixture tree), which deterministically fails and yields the same
# "stale" sentinel-count fallback every run — no live-state coupling.
# "arch-audit" IS included: same live-coordinator-state shape as "weekly" (reads the real
# health-ledger.md, not the frozen fixture). Its golden slice matched by coincidence on the
# capture day, but that computed_state cannot be verified against a point-in-time golden any
# more than "weekly" can — a same-day coincidental match is not real coverage, it is a latent
# flake that breaks whenever the live ledger changes. Sentinel it for honest determinism.
_LIVE_STATE_ROUTINE_SIGNAL_KINDS = frozenset({"weekly", "distill-backlog", "arch-audit"})

# Full-parity normalization set: volatile fields only.
# deliverable_status intentionally excluded — compared directly in assert_full_parity so the
# cross-join result is actually verified.  shipped_sha stays: it is a git SHA and volatile
# between the golden-capture run and a later re-run.
_ALL_NORMALIZED_KEYS = (
    _VOLATILE_TIME_KEYS | _VOLATILE_GIT_KEYS | _REPO_KEYS | _MACHINE_KEYS
)

# Section-parity normalization set: volatile fields minus shipped_sha (which is derived at
# section level and handled via _SECTION_DERIVED_NULL_KEYS → None, not <NORMALIZED>).
_SECTION_NORMALIZE_KEYS = (
    _VOLATILE_TIME_KEYS | (_VOLATILE_GIT_KEYS - frozenset({"shipped_sha"})) | _REPO_KEYS | _MACHINE_KEYS
)

# ---------------------------------------------------------------------------
# Handoff path normalizer
# ---------------------------------------------------------------------------
# Handoff provenance paths change when a handoff is consumed+archived between the
# golden-capture run and the porter run.  The basename is stable; only the prefix
# changes (state/handoffs/X → archive/handoffs/YYYY-MM/X).  Normalize both forms to
# their common basename so the comparison is not defeated by concurrent archival.
_HANDOFF_PATH_RE = _re.compile(
    r"(?:(?:state|archive)/handoffs/(?:[^/]+/)?)([\w\-\.]+\.md)$"
)


def _norm_handoff_path(path: str) -> str:
    """Reduce a handoff provenance path to its stable basename."""
    if isinstance(path, str):
        m = _HANDOFF_PATH_RE.match(path)
        if m:
            return f"handoffs/<basename>/{m.group(1)}"
    return path


# ---------------------------------------------------------------------------
# Absolute-path portability normalizer (path / source_path, non-handoff-shaped)
# ---------------------------------------------------------------------------
# 2026-07-21 golden-emission fixture real-name + abs-path portability, DR-060: some
# section porters emit an ABSOLUTE provenance path (e.g. review_trail/lessons entries
# resolved against the frozen fixture tree, or a live checkout under /Users/<name>/...).
# A machine-locked absolute path compares unequal between two machines even when the
# underlying file is identical. Reduce to the SAME canonical repo-root-relative POSIX
# form the source-fixed porters emit (``state/...``) by finding the stable fixtures-tree
# anchor — INCLUDING the ``root/`` segment, matching the exact root
# ``review_trail.py::_relativize_path`` resolves against (``ctx.subprocess_root`` ==
# ``fixtures/root``) — and stripping THROUGH it, not merely from it (Finding 1: a prior
# anchor one segment shallower kept the ``root/`` segment in the output, desyncing
# comparison against a source-relativized value that never carries it).
#
# Review: code-reviewer — Finding 1 (anchor/root granularity mismatch) + Finding 5
# (Windows backslash-delimited paths silently no-op the forward-slash-only anchor).
#
# Canonicalizes three input forms to the same ``state/...`` result:
#   1. Absolute:        /Users/x/.../fixtures/root/state/review-trail/y.json
#   2. Anchor-relative:  coordinator_core/ops/emit/tests/fixtures/root/state/lessons/z.yaml
#   3. Bare-relative:    state/foo.json (no anchor present — passes through unchanged)
# is a pure string operation (the normalizer has no live root handy) and works whether
# the live emitter's absolute path or an already-relative golden value is passed in.
# Handoff-shaped paths are handled separately by _norm_handoff_path/_HANDOFF_PATH_RE
# above and are never absolute by the time they reach this function, so there is no
# overlap to arbitrate.
_FIXTURE_ROOT_ANCHOR = "coordinator_core/ops/emit/tests/fixtures/root/"


def _relativize_abs_fixture_path(value: str) -> str:
    """Strip THROUGH the fixtures-tree anchor (incl. ``root/``) to canonical ``state/...``.

    Canonicalizes all three forms the fixtures tree/live emitters can produce to the
    SAME output the source-fixed porters (``review_trail.py``, the lessons producer)
    emit: an absolute path (``/Users/x/.../fixtures/root/state/...``), an anchor-relative
    path (``coordinator_core/.../fixtures/root/state/...``), or a bare-relative path
    (``state/...``, no anchor present) — the last passes through unchanged since it is
    already canonical. Non-string values pass through unchanged.

    Windows-normalizes the input (backslash → forward-slash) before searching, since the
    lister this feeds can produce backslash-delimited absolute paths on Windows
    (``os.path.join``/``os.walk``) — a forward-slash-only anchor would silently no-op
    there (Finding 5).
    """
    if not isinstance(value, str):
        return value
    normalized = value.replace("\\", "/")
    idx = normalized.find(_FIXTURE_ROOT_ANCHOR)
    if idx == -1:
        return value
    return normalized[idx + len(_FIXTURE_ROOT_ANCHOR):]


# ---------------------------------------------------------------------------
# Core normalizer
# ---------------------------------------------------------------------------

def _normalize(value):
    """Recursively normalize volatile AC5-PROVENANCE fields to typed sentinels.

    Applies to any dict/list value in a cockpit emission or DoE conformance fixture.
    Typed sentinels: _TS_SENTINEL (timestamps), _SHA_SENTINEL (git SHAs), _ID_SENTINEL
    (branch / repo / REPO_NAME / machine-identity strings).

    Oracle: DoE emission-conformance-contract.md § AC5-PROVENANCE.
    """
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            # Review: code-reviewer — F1: apply the correct typed sentinel per
            # AC5-PROVENANCE field class (emission-conformance-contract.md § AC5-PROVENANCE).
            if key in _VOLATILE_TIME_KEYS:
                out[key] = _TS_SENTINEL
            elif key in _VOLATILE_SHA_KEYS:
                out[key] = _SHA_SENTINEL
            elif key in _VOLATILE_GIT_OTHER_KEYS or key in _REPO_KEYS or key in _MACHINE_KEYS:
                out[key] = _ID_SENTINEL
            elif (
                key == "computed_state"
                and value.get("kind") in _LIVE_STATE_ROUTINE_SIGNAL_KINDS
            ):
                out[key] = _ID_SENTINEL
            elif key == "path" and isinstance(val, str):
                out[key] = _relativize_abs_fixture_path(_norm_handoff_path(val))
            elif key == "source_path" and isinstance(val, str):
                # content_hash.source_path is an ABSOLUTE machine-specific path (the golden
                # carries the capture machine's /Users/... prefix, a Windows run carries X:/...);
                # reduce it to the fixture-root-relative form exactly like provenance.path so the
                # hash's identity is compared without checkout-location variance.
                out[key] = _relativize_abs_fixture_path(val)
            else:
                out[key] = _normalize(val)
        return out
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value
