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

# Typed sentinels per AC5-PROVENANCE oracle.
# Oracle: DoE emission-conformance-contract.md § AC5-PROVENANCE (three distinct sentinels).
_TS_SENTINEL = "1970-01-01T00:00:00Z"
_SHA_SENTINEL = "0000000000000000000000000000000000000000"
_ID_SENTINEL = "__NORMALIZED__"                             # string-ID sentinel: branch/repo/REPO_NAME

_VOLATILE_TIME_KEYS = frozenset({
    "observed_at",
    "emitted_at",
    "computed_as_of",
    "max_observed_at",
    "last_meaningful_activity",
    "last_modified_at",
    "last_commit_at",
    "last_commit_message",
    "last_activity_at",
})
# Git SHA fields — volatile as commits land.  Normalized to zero-SHA (_SHA_SENTINEL) per
# AC5-PROVENANCE oracle (provenance.ref.sha → "0000000000000000000000000000000000000000").
_VOLATILE_SHA_KEYS = frozenset({
    "git_sha", "git_sha_short",
    "tip_sha", "merge_base_sha",
    "max_commit_sha",
    "sha",
    "shipped_sha",
})
# Normalized to _ID_SENTINEL (string IDs and counts alike — both sides normalize the same way).
_VOLATILE_GIT_OTHER_KEYS = frozenset({
    "ahead_by", "behind_by",
    "commits_since_bug_sweep",
    "commits_since_update_docs",
    # AC5-PROVENANCE field 3 (emission-conformance-contract.md § AC5-PROVENANCE):
    # provenance.ref.branch varies between machines / sessions; normalize to _ID_SENTINEL.
    "branch",
})
# Union for downstream set operations (e.g. _SECTION_NORMALIZE_KEYS - shipped_sha).
_VOLATILE_GIT_KEYS = _VOLATILE_SHA_KEYS | _VOLATILE_GIT_OTHER_KEYS
# enriched value (the Staff Engineer F0: pure <NORMALIZED>-on-both-sides hides that failure).
# reproduce bash value); shipped_sha normalized via _VOLATILE_GIT_KEYS (git SHA, volatile).
_SECTION_DERIVED_NULL_KEYS = frozenset({"deliverable_status", "shipped_sha"})

# byte-parity COMPARISON only, never from the actual emitted envelope (sections/
# a THIRD same-chunk-family additive field but is DELIBERATELY NOT added here, unlike its
# The narrower reason it costs no coverage: `baton_class` is a PURE FUNCTION of `kind`, and
# a COMPUTED field, so the drop is only defensible because it has a dedicated oracle of its own
# byte-parity COMPARISON only — sections/cross_repo_memos.py::collect() still stamps it on
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
# (AC5-PROVENANCE field 7, emission-conformance-contract.md § AC5-PROVENANCE).
_REPO_KEYS = frozenset({"repo", "REPO_NAME"})
_MACHINE_KEYS = frozenset({"emitted_by_machine", "hostname"})

# etc. in _VOLATILE_GIT_OTHER_KEYS) rather than a fabricated fixture value.
_LIVE_STATE_ROUTINE_SIGNAL_KINDS = frozenset({"weekly", "distill-backlog", "arch-audit"})

_ALL_NORMALIZED_KEYS = (
    _VOLATILE_TIME_KEYS | _VOLATILE_GIT_KEYS | _REPO_KEYS | _MACHINE_KEYS
)

# section level and handled via _SECTION_DERIVED_NULL_KEYS → None, not <NORMALIZED>).
_SECTION_NORMALIZE_KEYS = (
    _VOLATILE_TIME_KEYS | (_VOLATILE_GIT_KEYS - frozenset({"shipped_sha"})) | _REPO_KEYS | _MACHINE_KEYS
)

_HANDOFF_PATH_RE = _re.compile(
    r"(?:(?:state|archive)/handoffs/(?:[^/]+/)?)([\w\-\.]+\.md)$"
)


def _norm_handoff_path(path: str) -> str:
    if isinstance(path, str):
        m = _HANDOFF_PATH_RE.match(path)
        if m:
            return f"handoffs/<basename>/{m.group(1)}"
    return path


# section porters emit an ABSOLUTE provenance path (e.g. review_trail/lessons entries
# anchor — INCLUDING the ``root/`` segment, matching the exact root
# Handoff-shaped paths are handled separately by _norm_handoff_path/_HANDOFF_PATH_RE
_FIXTURE_ROOT_ANCHOR = "coordinator_core/ops/emit/tests/fixtures/root/"


def _relativize_abs_fixture_path(value: str) -> str:
    if not isinstance(value, str):
        return value
    normalized = value.replace("\\", "/")
    idx = normalized.find(_FIXTURE_ROOT_ANCHOR)
    if idx == -1:
        return value
    return normalized[idx + len(_FIXTURE_ROOT_ANCHOR):]


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
                out[key] = _relativize_abs_fixture_path(val)
            else:
                out[key] = _normalize(val)
        return out
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value
