"""
coordinator_core.ops.emit.envelope — top-level snapshot assembly + output write.

Purpose: port the bash emitter's Section-9 envelope composition plus the run-preconditions
it depends on: the schema-version / contract-desync guard and the pin-integrity assertion.
Assembles every section array + completion_rollups + backlogs + per-section malformed
buckets + narrative_views into one contract-shaped object and writes it to the canonical
(or --out override) path.

Section registry (spine): sections register a ``collect(ctx) -> (records, malformed)``
porter plus a placement rule that distributes those records into the envelope's arrays.
The registry is EMPTY at C2 landing — the 21 section porters land in W3 and C3 wires them.
An empty registry MUST still emit a fully-shaped envelope with every array present and empty
(the graceful-absent contract, bash AC4). C3 calls ``register_section`` per porter.

Version-guard scope (plan § D4, matches the C1 validate.py split):
    - PORTED: the content-based CONTRACT_VERSION-vs-bundle-.version guard + pin-integrity
      assertion (delegated to validate.assert_version_consistency / assert_pin_integrity).
    - NOT ported: the src/*.ts-newer-than-dist mtime freshness guard (non-portable across a
      fresh vendor copy).

Negative-spec: emit() does NOT gate on any producer-side hold/sentinel — no runtime check,
no on-disk sentinel, no EmitHeldError, anywhere in this module. This is true for MINOR/PATCH
unconditionally: reader-first forward-compatibility is a consumer responsibility
(example-retrieval-repo ingests defensively); producers emit unconditionally. See
docs/plans/2026-07-08-producer-emit-hold-removal-reader-first-consumer-owned.md § C1.

For a MAJOR cockpit-contract bump, a process-level hold exists ABOVE this layer: per a
2026-07-25 PM ruling (docs/decisions/DR-234-major-cockpit-bump-producer-side-hold-adopted.md),
a human/EM holds merge-to-main and production emit until consuming repos confirm re-vendor.
That hold is exercised before this function is ever called on a production emit path — it is
not, and must never become, a check inside emit() itself. See
docs/wiki/cockpit-contract-revendor.md § Reader-first for the full reconciliation.

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C2
Port of: emit-cockpit-snapshot.sh (coordinator-claude 07eedcfb, 2026-07-19) — setup/guards, Section 9 envelope.

Dual-purpose note (deliberate, not incidental): this module also hosts the
``check-shipped-on-main.sh`` port (``main()``, ``_CHECK_SHIPPED_HELP``, ``resolve_ref``,
``_commit_age_label`` below) because it reuses ``sha_on_origin_main`` /
``check_origin_main_reachable`` / ``fetch_origin_main`` — the same ancestor-check
helpers ``_stamp_shipped_sha`` already uses internally for envelope assembly. A reader
grepping this file for cockpit-emission logic will also find that unrelated git-ancestor
CLI; it lives here for helper reuse, not because it belongs to the section-registry
pipeline. Review: code-reviewer — flagged as a placement-clarity gap (Finding 2).

Public promotion (2026-07-25): the four ancestor-check helpers above were promoted from
private to public names so ``coordinator_core/ops/introspect/verify_shipped.py`` can import
them as a second, cross-module consumer, rather than reaching across the underscore
boundary. ``_check_origin_main_reachable``/``_fetch_origin_main``/``_sha_on_origin_main``
survive as private aliases (see just above ``_CHECK_SHIPPED_HELP``) purely because
``_stamp_node_shipped_sha`` and its test (``test_node_shipped_sha_stamp.py``) patch those
exact underscore paths; ``_resolve_ref`` had no such caller and was renamed outright.
Spec backlink: state/handoffs/2026-07-25_000823_shipped-state-verifier.md

Batch classification (2026-07-29): ``classify_shas_on_origin_main`` (private alias
``_classify_shas_on_origin_main``) replaces the per-SHA ``sha_on_origin_main`` spawn loop
inside ``_stamp_shipped_sha`` / ``_stamp_node_shipped_sha`` / ``_stamp_closure_reachability``
with two spawns total per stamp call, regardless of distinct-SHA count (measured 108 spawns
on a real corpus before this change).

CLI migration (2026-08-07): ``main()`` (the ``check-shipped-on-main.sh`` CLI port) now resolves
every argv ref first, then classifies every resolved sha in ONE ``classify_shas_on_origin_main``
call — the same many-commits-against-ONE-ref batchable shape, not a records-loop hot path but
the same tri-state contract. ``sha_on_origin_main`` itself is UNCHANGED and remains a public
helper (other in-repo callers may still classify a single sha), but ``main()`` no longer calls
it. Spec backlink: pln-kill-the-n-1-git-spawn-class-a-88897a § C32.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from coordinator_core._settings_home import machine_local_dir, normalize_native_path
from coordinator_core.ops.emit import validate
from coordinator_core.ops.emit import enrich as _enrich
from coordinator_core.ops.emit import deliverable_status as _deliverable_status
from coordinator_core.ops.emit import backlog_history as _backlog_history
from coordinator_core.ops.emit import skipped_stage as _skipped_stage
from coordinator_core.ops.emit.context import EmitContext

# ---------------------------------------------------------------------------
# Placement contract — section name -> where its collect() output lands in the
# envelope. Mirrors tests/fixtures/section_envelope_map.json and the bash Section-9
# assembly (:2876-2924). 'records' = envelope dot-paths, 'malformed' = malformed_records
# keys. Sections that split across multiple record dot-paths (backlogs -> bug/debt/
# improvement, rollups -> day/week) MUST supply an explicit place fn at register time.
# ---------------------------------------------------------------------------
SECMAP: dict[str, dict] = {
    "handoffs":          {"records": ["handoffs"],                                              "malformed": ["handoffs"]},
    "backlogs":          {"records": ["backlogs.bug", "backlogs.debt", "backlogs.improvement"], "malformed": ["backlogs"]},
    "review_trail":      {"records": ["review_trail"],                                          "malformed": ["review_trail"]},
    "routine_signals":   {"records": ["routine_signals"],                                       "malformed": []},
    "rollups":           {"records": ["completion_rollups.day", "completion_rollups.week"],     "malformed": []},
    "goals":             {"records": ["goals_current"],                                         "malformed": []},
    "coordinator_roots": {"records": ["coordinator_roots"],                                     "malformed": ["coordinator_roots"]},
    "branch":            {"records": ["branches"],                                              "malformed": []},
    "lessons":           {"records": ["lessons"],                                               "malformed": ["lessons"]},
    "plans":             {"records": ["plans"],                                                 "malformed": ["plans"]},
    "cross_repo_memos":  {"records": ["cross_repo_memos"],                                       "malformed": ["cross_repo_memos"]},
    "roadmaps":          {"records": ["roadmaps"],                                              "malformed": ["roadmaps"]},
    "trackers":          {"records": ["trackers"],                                              "malformed": ["trackers"]},
    "health":            {"records": ["health"],                                                "malformed": ["health"]},
    "decision_guides":   {"records": ["decision_guides"],                                        "malformed": ["decision_guides"]},
    "session_hierarchy": {"records": ["session_hierarchies"],                                    "malformed": ["session_hierarchies"]},
    "file_attribution":  {"records": ["file_attributions"],                                      "malformed": ["file_attributions"]},
    "initiatives":       {"records": ["initiatives"],                                           "malformed": ["initiatives"]},
    "exec_summary":      {"records": ["exec_summaries"],                                         "malformed": ["exec_summaries"]},
    "roadmap_dag":       {"records": ["roadmap_dag_nodes", "roadmap_dag_edges"],                 "malformed": ["roadmap_dag_nodes", "roadmap_dag_edges"]},
    "commit_closures":   {"records": ["commit_closures"],                                        "malformed": ["commit_closures"]},
}

# competitor_summaries / intelligence_signals (cockpit-contract v2.16.0 market-intel widen):
# EMPTY-BY-DESIGN, PERMANENTLY. Claude-klabauter is NOT the data path for example-market-data-repo — that
# data routes example-market-data-repo -> cockpit's `ingestEmission` plane DIRECTLY, never through
# artifact.emit. Do NOT add a section `collect()`, do NOT read any market-intel source, do NOT
# register these two arrays in _PLACEMENT. Wiring them present-but-empty (skeleton keys below
# + malformed_records buckets in _MALFORMED_KEYS) is the confirmed TERMINAL state, not a stub
# awaiting a future porter.
#
# Authority: seam-confirm consult actioned by claude-central-em 2026-07-14
# (cross-repo/inbox/2026-07-14-claude-klabauter-em-cockpit-v2160-source-seam.md, ## EM Response).
#
# malformed_records key order (bash:2907-2923). Every key present, empty by default.
_MALFORMED_KEYS = (
    "handoffs", "backlogs", "review_trail", "coordinator_roots", "plans", "lessons",
    "cross_repo_memos", "roadmaps", "trackers", "health", "decision_guides",
    "session_hierarchies", "file_attributions", "initiatives", "exec_summaries",
    "roadmap_dag_nodes", "roadmap_dag_edges", "competitor_summaries", "intelligence_signals",
    "commit_closures",
)

# Canonical output filename (bash:79). central_state_root / this = the default OUT_FILE.
DEFAULT_OUTPUT_NAME = "cockpit-emission.json"


# ---------------------------------------------------------------------------
# Section registry
# ---------------------------------------------------------------------------
SectionCollect = Callable[[EmitContext], "tuple[list, list]"]
# A place fn mutates the envelope in place, distributing (records, malformed).
SectionPlace = Callable[[dict, list, list], None]


@dataclass
class _RegisteredSection:
    name: str
    collect: SectionCollect
    place: SectionPlace


_REGISTRY: "dict[str, _RegisteredSection]" = {}


def _set_dotpath(envelope: dict, dotpath: str, value: list) -> None:
    """Assign ``value`` to a one- or two-level dot-path (e.g. 'backlogs.bug')."""
    parts = dotpath.split(".")
    node = envelope
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def _get_dotpath(envelope: dict, dotpath: str):
    """Read the value at a one- or two-level dot-path (e.g. 'backlogs.bug').

    Read-side counterpart to ``_set_dotpath``. Returns whatever is stored at the path
    (typically a list for record arrays). Raises ``KeyError`` if any segment is absent.

    # keep in sync with validate._get_dotpath_value
    """
    node = envelope
    for part in dotpath.split("."):
        node = node[part]
    return node


def _resolve_provenance_path(raw: str, repo_root: Path, cwd: Path) -> Optional[Path]:
    """Resolve a provenance.path string to a readable absolute regular-file Path, or None.

    Purpose: canonicalise the variously-shaped provenance.path values (absolute,
    repo-relative, or empty) for the content_hash stamping stage.

    Resolution order (first hit wins):
        1. ``normalize_native_path(raw).expanduser()`` — if absolute AND is_file().
        2. ``repo_root / raw`` — if is_file().
        3. ``cwd / raw`` — if is_file().

    Candidates 2 and 3 join the RAW string (not the expanded candidate 1 path) onto
    repo_root/cwd — ``Path("/a") / "/b"`` collapses to ``"/b"``, so joining an already-
    absolute expansion would silently discard repo_root/cwd for absolute inputs.

    Returns None if none of the candidates resolves to a regular file; the caller
    then omits content_hash for that record (graceful-degrade, never abort).

    Spec backlink: pln-tc-3-emission-stack-python-por-c9595b
    """
    p = normalize_native_path(raw).expanduser()
    if p.is_absolute() and p.is_file():
        return p
    candidate = repo_root / raw
    if candidate.is_file():
        return candidate
    candidate = cwd / raw
    if candidate.is_file():
        return candidate
    return None


def _stamp_content_hash(envelope: dict, ctx: EmitContext, schema_version: str) -> None:
    """Post-collect enrichment: stamp a content_hash change-signal onto each resolvable record.

    Purpose: attach the R5 content-hash change-signal sub-shape onto every work-state record
    whose provenance.path resolves to a readable regular file, so example-retrieval-repo can invalidate
    its CQRS projection cache on change without a full re-parse (cache key: source_path + hex).

    Version-gated: only activates when schema_version >= 2.5.0. Below that threshold the
    stage is a complete no-op — build() output is byte-identical and the existing parity
    test stays green. Review: code-reviewer — Finding 3: a prior revision of this docstring
    named "the current vendored bundle" as 2.4.0 (below the gate, hence dormant); the bundle
    has since moved past 2.5.0 and this stage is now LIVE on every build() call. Naming a
    bundle version here is inherently stale-prone (the docstring doesn't move when the pin
    does) — state the gate threshold only; check ``read_schema_version()`` for the live pin.

    Wire shape per producer contract § 3.3 + change-signal.schema.json:
        "content_hash": {"algo": "sha256", "hex": "<64-hex>", "source_path": "<provenance.path>"}

    Emit predicate: omit the field entirely when provenance.path is empty, or when it does not
    resolve to a readable regular file. Never emit a placeholder hex. The try/except around
    compute_stamp is belt-and-suspenders against a vanish between the is_file check and the read.

    Walks exactly the record dot-paths enumerated in SECMAP (all arrays including nested ones
    such as completion_rollups.day and backlogs.bug). Does NOT walk malformed_records,
    narrative_views, or scalar fields.

    Spec backlink: pln-tc-3-emission-stack-python-por-c9595b
    Producer contract: coordinator_core/contract/example-retrieval-repo-producer-contract.md § 3.3
    """
    # Version gate: parse the dotted version to an int tuple; treat malformed/short as below.
    try:
        ver_tuple = tuple(int(x) for x in schema_version.split("."))
        if len(ver_tuple) < 3:
            return
    except (ValueError, AttributeError):
        return
    if ver_tuple < (2, 5, 0):
        return

    from coordinator_core import cache  # local import to keep the module-level deps lean

    all_record_dotpaths = {dotpath for spec in SECMAP.values() for dotpath in spec["records"]}
    repo_root = ctx.repo_root
    cwd = Path.cwd()

    # Per-call dedup cache: many records across different sections/dotpaths resolve
    # provenance.path to the SAME on-disk file (measured 5864 records -> 2633 unique
    # abs_paths against a real corpus) — without this, compute_stamp's full-body
    # sha256 read runs once per RECORD instead of once per unique FILE. This is a
    # pure within-call dedup, not a cross-run cache: it does not touch cache.py's
    # own (path, stamp)-keyed module cache and carries none of that cache's
    # staleness surface (D2/R5) — the same file is hashed at most once across this
    # single build() pass, and every record still gets a freshly-computed value for
    # this run (never a value left over from a prior emit()).
    _hex_by_path: dict[Path, str] = {}

    for dotpath in all_record_dotpaths:
        try:
            records = _get_dotpath(envelope, dotpath)
        except KeyError:
            # Section absent from this envelope -- nothing to stamp, not an error.
            continue
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            provenance = record.get("provenance")
            if not isinstance(provenance, dict):
                continue
            raw = provenance.get("path", "")
            if not raw or not isinstance(raw, str):
                continue
            abs_path = _resolve_provenance_path(raw, repo_root, cwd)
            if abs_path is None:
                continue
            if abs_path in _hex_by_path:
                hex_digest = _hex_by_path[abs_path]
            else:
                try:
                    hex_digest = cache.compute_stamp(abs_path)
                except OSError as exc:
                    print(f"WARNING: _stamp_content_hash: could not stamp {abs_path}: {exc}", file=sys.stderr)
                    continue
                _hex_by_path[abs_path] = hex_digest
            record["content_hash"] = {
                "algo": "sha256",
                "hex": hex_digest,
                "source_path": raw,
            }


def _extract_prior_docs_staleness(prior: dict):
    """Read the last-emitted ``ExecSummary.docs_staleness`` out of *prior*.

    ``exec_summaries`` is a singleton-per-repo array, so the first record's value IS the
    repo's last-known value. Returns ``skipped_stage.absent()`` when the prior emission has
    no exec-summary record or that record never carried the key — either way there is no
    last-known value to reuse, and the caller's declared policy decides what happens next.
    """
    records = prior.get("exec_summaries")
    if not isinstance(records, list) or not records:
        return _skipped_stage.absent()
    first = records[0]
    if not isinstance(first, dict) or "docs_staleness" not in first:
        return _skipped_stage.absent()
    return first["docs_staleness"]


def _validate_prior_docs_staleness(value: Any) -> Optional[str]:
    """Return None if *value* is a usable last-known ``docs_staleness``, else a short reason.

    The shape contract, per ``ops/doc_staleness.py``'s two entry variants: a list of objects,
    each carrying a string ``path`` and a boolean ``stale``. ``commits_since`` is checked as an
    int only WHEN PRESENT — the ``no_content_modifying_history`` variant legitimately omits it
    (along with ``days_since``, ``last_touch_sha`` and the rest), so requiring it would reject
    valid prior values. ``None`` passes: it is this field's contract-sanctioned "not computed"
    state, which is exactly what the ``NULL_SANCTIONED`` fallback writes.

    Review: code-reviewer (Finding 1) — this call site reused its prior value with NO validator
    while the module docstring next door stated validated reuse as an unconditional rule, so a
    stale array would ship an old shape silently if ``DocStalenessEntry`` changes upstream.
    Deliberately a shape check and not a no-op: a no-op here would have been the same
    convention-only enforcement the required ``validate`` parameter exists to end.

    Not a whole-contract validator (schema conformance is ``validate.py``'s job) — the narrow
    question is whether the reused value is as good as a computed one.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return f"expected a list of doc-staleness entries or null, got {type(value).__name__}"
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            return f"entry {index} is not an object ({type(entry).__name__})"
        if not isinstance(entry.get("path"), str) or not entry.get("path"):
            return f"entry {index} has no usable string 'path'"
        if not isinstance(entry.get("stale"), bool):
            return (
                f"entry {index} ({entry.get('path')!r}) has a non-boolean 'stale' "
                f"({type(entry.get('stale')).__name__})"
            )
        if "commits_since" in entry and not isinstance(entry["commits_since"], int):
            return (
                f"entry {index} ({entry.get('path')!r}) has a non-int 'commits_since' "
                f"({type(entry['commits_since']).__name__})"
            )
    return None


def _stamp_docs_staleness(envelope: dict, ctx: EmitContext) -> None:
    """Post-collect enrichment: populate ``ExecSummary.docs_staleness`` (C6).

    ``sections/exec_summary.py::collect()`` never sets this key — the detector
    it feeds (``coordinator_core.ops.doc_staleness``) reasons over the whole
    repo (git log across every declared doc), not over the single markdown
    artifact ``collect()`` parses, so it cannot live inside that section's
    ``collect()`` without duplicating the cross-cutting repo_root/registry
    plumbing every other post-collect stamp already centralises here (mirrors
    ``_stamp_content_hash``'s placement rationale immediately above).

    REQUIRED-WITH-NULL (D9): every ``exec_summaries`` record gets this key
    written, one of two states —
        ``null`` — detector did NOT run: the repo declares no
            ``coordinator.local.md`` (no per-repo doc-health config exists to
            resolve from at all), OR the detector raised.
        ``[]``/populated — detector RAN (a ``coordinator.local.md`` exists,
            so a registry resolves, even if only to fleet defaults): maps
            each ``status: "ok"`` entry from
            ``build_doc_staleness_report_from_registry`` onto the contract's
            ``DocStalenessEntry`` shape. A non-"ok" per-doc status (e.g.
            ``"absent"``, ``"no_content_modifying_history"``) is dropped —
            mirrors ``DocStalenessEntry``'s own docstring ("the producer only
            emits an entry for a doc it could fully evaluate").

    Never fails emission (AC-parity with every other stamp in this file): a
    raising detector, a missing git binary, or an unreadable repo all degrade
    to ``null`` rather than propagating — this enrichment is best-effort, not
    load-bearing for the emission to succeed.

    ``last_touch_date`` passes straight through from
    ``compute_doc_staleness`` (git ``%cI`` format) without conversion — ``%cI``
    already emits an offset-bearing ISO-8601 string (``+HH:MM``/``Z``), which
    is exactly what the contract's ``IsoDateTime`` pattern requires; a naive
    (offset-less) timestamp would quarantine the whole record on ingest.

    Cost note: this runs at most once per envelope build (``exec_summaries``
    is a singleton-per-repo record per ``sections/exec_summary.py``'s own
    docstring) — not once per record — so the per-doc git shell-outs the
    detector performs are bounded by the repo's declared doc count, not by
    envelope size. Bounded is not cheap, though: measured ~0.37s of a 3.5s
    emit on the coordinator-claude corpus, which is why the detector now runs only on
    the full-enrichment tier (``ctx.full_enrichment``). On the cheap tier the
    field is satisfied from the previously-emitted value, falling back to the
    contract-sanctioned ``null`` — the SAME state the never-fail degrade path
    below already writes — via the one shared rule in ``skipped_stage``. Note
    what the gate does NOT do: it never writes ``[]``, which would claim the
    detector ran and found every declared doc fresh.

    That reuse IS validated (``_validate_prior_docs_staleness``): a prior value
    whose entries do not carry the ``DocStalenessEntry`` shape is rejected and
    the field falls back to ``null``, rather than shipping an old shape as
    though it were current. The VALIDATED REUSE rule in ``skipped_stage``'s
    module docstring therefore holds at BOTH gated stages, not just at
    ``file_attributions``.

    Spec backlink: docs/plans/2026-07-28-human-facing-doc-staleness-detector.md § C6
    Perf backlink: emit() cost-lever work, 2026-07-29 (Lever 2).
    """
    records = envelope.get("exec_summaries")
    if not records:
        return

    if not ctx.full_enrichment:
        satisfied, reused = _skipped_stage.skipped_stage_value(
            ctx,
            stage="docs_staleness",
            extract=_extract_prior_docs_staleness,
            absent_policy=_skipped_stage.NULL_SANCTIONED,
            validate=_validate_prior_docs_staleness,
        )
        if satisfied:
            for record in records:
                record["docs_staleness"] = reused
            return

    repo_root = ctx.repo_root
    local_md = repo_root / "coordinator.local.md"
    if not local_md.is_file():
        docs_staleness = None
    else:
        try:
            from coordinator_core.ops.doc_staleness import (
                build_doc_staleness_report_from_registry,
            )

            report = build_doc_staleness_report_from_registry(str(repo_root))
            docs_staleness = [
                {
                    "path": doc["path"],
                    "stale": doc["stale"],
                    "commits_since": doc["commits_since"],
                    "days_since": doc["days_since"],
                    "last_touch_sha": doc["last_touch_sha"],
                    "last_touch_date": doc["last_touch_date"],
                    "changed_areas": doc["changed_areas"],
                    "threshold_commits": doc["threshold_commits"],
                    "threshold_days": doc["threshold_days"],
                }
                for doc in report.get("docs", [])
                if doc.get("status") == "ok"
            ]
        except Exception as exc:  # noqa: BLE001 -- detector must never break emit()
            print(
                f"WARNING: _stamp_docs_staleness: detector failed for {repo_root}: {exc}",
                file=sys.stderr,
            )
            docs_staleness = None

    for record in records:
        record["docs_staleness"] = docs_staleness


def _default_place_for(name: str) -> SectionPlace:
    """Default single-target placer from SECMAP.

    Requires the section to map to exactly one record dot-path; split sections
    (backlogs, rollups) MUST pass an explicit place fn to ``register_section``.
    """
    spec = SECMAP[name]
    rec_paths = spec["records"]
    mal_keys = spec["malformed"]

    def place(envelope: dict, records: list, malformed: list) -> None:
        if len(rec_paths) != 1:
            raise ValueError(
                f"section {name!r} maps to {len(rec_paths)} record paths; "
                "register it with an explicit place fn that splits collect() output"
            )
        _set_dotpath(envelope, rec_paths[0], list(records))
        for key in mal_keys:
            envelope["malformed_records"][key] = list(malformed)

    return place


def register_section(
    name: str,
    collect: SectionCollect,
    place: Optional[SectionPlace] = None,
) -> None:
    """Register a section porter (C3 wiring seam).

    ``collect(ctx) -> (records, malformed)`` per the spine contract. ``place`` distributes
    that output into the envelope; when omitted, a single-target placer is derived from
    SECMAP (invalid for split sections, which must supply their own).
    """
    if name not in SECMAP:
        raise KeyError(f"unknown section {name!r} — add it to envelope.SECMAP first")
    _REGISTRY[name] = _RegisteredSection(name, collect, place or _default_place_for(name))


def clear_registry() -> None:
    """Drop all registered sections (test isolation seam)."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------
def _empty_skeleton(schema_version: str, emitted_at: str, emitted_by_machine: str) -> dict:
    """A fully-shaped envelope with every array present and empty (bash:2876-2924 shape).

    Negative-spec: emits NO top-level ``coordinator_root_path`` scalar. A 2026-07-07 AC12
    revision briefly added one (sibling of ``emitted_by_machine``, value ``str(ctx.repo_root)``),
    but it had ZERO readers and was removed 2026-07-21: example-retrieval-repo ingests the top-level
    ``emitted_at`` but never a top-level ``coordinator_root_path`` (rag/coordinator-claude only read the schema'd
    PER-RECORD ``coordinator_root_path`` composite-key component), it is absent from the vendored
    SnapshotEnvelope schema, and that schema's top-level ``additionalProperties: false`` makes the
    extra scalar a whole-envelope validation failure. The per-row ``coordinator_root_path`` ('.')
    literals in coordinator_roots.py / goal_append.py are a SEPARATE, schema'd field — untouched.
    """
    return {
        "schema_version": schema_version,
        "emitted_at": emitted_at,
        "emitted_by_machine": emitted_by_machine,
        "coordinator_roots": [],
        "branches": [],
        "handoffs": [],
        "completion_rollups": {"day": [], "week": []},
        "backlogs": {"bug": [], "debt": [], "improvement": []},
        "review_trail": [],
        "routine_signals": [],
        "goals_current": [],
        "lessons": [],
        "plans": [],
        "cross_repo_memos": [],
        "roadmaps": [],
        "trackers": [],
        "health": [],
        "decision_guides": [],
        "session_hierarchies": [],
        "file_attributions": [],
        "initiatives": [],
        "exec_summaries": [],
        "roadmap_dag_nodes": [],
        "roadmap_dag_edges": [],
        "commit_closures": [],
        # competitor_summaries / intelligence_signals (cockpit-contract v2.16.0):
        # EMPTY-BY-DESIGN, permanently — see the negative-spec block above _MALFORMED_KEYS
        # for why claude-klabauter never populates these.
        "competitor_summaries": [],
        "intelligence_signals": [],
        "backlog_history": None,
        "narrative_views": None,
        "malformed_records": {key: [] for key in _MALFORMED_KEYS},
    }


# ---------------------------------------------------------------------------
# Post-collect enrichment helpers
# ---------------------------------------------------------------------------

def _stamp_lma(record_groups: Sequence[list[dict]], repo_root: Path) -> None:
    """Stamp ``last_meaningful_activity`` in-place for MULTIPLE record groups via ONE shared
    git-log walk (bash §1 step 3.5, §8.6, §8.8).

    Port of the bash background-parallel ``git log -1 --format="%cI" -- <path>`` fan-out that
    runs for handoffs (bash:412-453), plans (bash:1695-1730), and roadmaps (bash:1840-1866).
    Uses ``enrich.batch_last_modified_at_grouped``, which walks the union of every group's
    provenance paths ONCE rather than spawning one ``git log`` per group (PERF 2026-07-29 —
    see that function's docstring for the measured sum-vs-max win). Each group's records keep
    their own input order; groups do not interact. Offline / bad-path → LMA=null (emit
    continues, never aborts).
    """
    path_groups = [
        [r.get("provenance", {}).get("path") or "" for r in records]
        for records in record_groups
    ]
    lma_groups = _enrich.batch_last_modified_at_grouped(repo_root, path_groups)
    for records, lmas in zip(record_groups, lma_groups):
        for record, lma in zip(records, lmas):
            record["last_meaningful_activity"] = lma


def check_origin_main_reachable(repo_root: Path) -> bool:
    """Return True when ``origin/main`` is locally reachable (no fetch; bash §1.5 amortised gate)."""
    # Review: code-reviewer — bare git subprocess.run lacked the Windows-safe
    # no-window/no-hang flag set the git_native._git() sibling module carries.
    try:
        rc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "origin/main"],
            capture_output=True,
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        ).returncode
        return rc == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def fetch_origin_main(repo_root: Path) -> None:
    """Try fetching origin/main; failure is silently ignored (bash §1.5 offline degradation)."""
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "origin", "main", "--quiet"],
            capture_output=True,
            check=False,
            timeout=120,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Offline degradation per the docstring contract above -- caller proceeds
        # against whatever origin/main state is already locally known.
        pass


def sha_on_origin_main(repo_root: Path, sha: str) -> Optional[bool]:
    """Check if ``sha`` is an ancestor of ``origin/main`` via ``git merge-base --is-ancestor``.

    Returns True (on main), False (not on main), or None (sha unreachable / exit 2 equivalent).
    Parity: bash §1.5 ``check-shipped-on-main.sh <sha>`` exit 0/1/2 semantics.
    """
    try:
        rc = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", sha, "origin/main"],
            capture_output=True,
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        ).returncode
        if rc == 0:
            return True
        if rc == 1:
            return False
        # Any other exit code (128 = bad object / unreachable) → treat as exit 2 (degrade all).
        return None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def classify_shas_on_origin_main(repo_root: Path, shas: list[str]) -> dict[str, Optional[bool]]:
    """Batch tri-state classification of ``shas`` against ``origin/main``, in O(1) git spawns.

    Per-SHA equivalent of calling ``sha_on_origin_main`` once per entry, but the whole set is
    resolved with exactly two subprocess spawns regardless of ``len(shas)`` — the 108-spawn
    hot spot this function replaces (one ``git merge-base --is-ancestor`` per record). Preserves
    ``sha_on_origin_main``'s tri-state contract element-for-element:

      - True  — sha is an ancestor of origin/main
      - False — sha is a valid, resolvable commit, but NOT an ancestor
      - None  — sha is unreachable / not a valid object (degrade-all signal to the caller)

    Mechanism: one ``git rev-list origin/main`` builds the ancestor set (anything printed there
    is, by definition, both a valid commit AND an ancestor); one ``git cat-file --batch-check``
    fed every REMAINING candidate sha on stdin classifies valid-commit-not-ancestor vs.
    not-a-valid-object — the distinction a naive ``rev-list`` set-membership test would collapse
    (silently reading "unreachable/bad object" as "definitely not shipped", a fail-open
    regression in a shipped-state signal). ``--batch-check`` emits exactly one output line per
    input line, in input order, even for objects that don't resolve — so line-for-line zip
    against the (deduplicated, ancestor-filtered) input list recovers per-sha classification
    without depending on ``%(objectname)`` matching an abbreviated or non-canonical input string.

    Any subprocess failure (git absent, timeout, OSError, or an output line count mismatch)
    degrades every entry to None, matching ``sha_on_origin_main``'s own except-clause policy.
    """
    if not shas:
        return {}
    unique_shas = list(dict.fromkeys(shas))

    try:
        rev_list = subprocess.run(
            ["git", "-C", str(repo_root), "rev-list", "origin/main"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {sha: None for sha in unique_shas}
    if rev_list.returncode != 0:
        return {sha: None for sha in unique_shas}
    ancestor_set = set(rev_list.stdout.split())

    # Only the SHAs NOT already proven ancestors need the validity check.
    candidates = [sha for sha in unique_shas if sha not in ancestor_set]
    valid_not_ancestor: set[str] = set()
    if candidates:
        stdin_payload = "\n".join(candidates) + "\n"
        try:
            batch_check = subprocess.run(
                ["git", "-C", str(repo_root), "cat-file", "--batch-check=%(objecttype)"],
                input=stdin_payload,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
                **no_console_creationflags(),
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {sha: None for sha in unique_shas}
        lines = batch_check.stdout.splitlines()
        if len(lines) != len(candidates):
            # Malformed/unexpected batch-check output shape — degrade everything rather
            # than risk misaligning a line to the wrong sha.
            return {sha: None for sha in unique_shas}
        for sha, line in zip(candidates, lines):
            if line.strip() == "commit":
                valid_not_ancestor.add(sha)

    result: dict[str, Optional[bool]] = {}
    for sha in unique_shas:
        if sha in ancestor_set:
            result[sha] = True
        elif sha in valid_not_ancestor:
            result[sha] = False
        else:
            result[sha] = None
    return result


# Back-compat private aliases — coordinator_core/ops/introspect/verify_shipped.py is a second,
# cross-module consumer of the four names above (promoted to public 2026-07-25, spec backlink
# state/handoffs/2026-07-25_000823_shipped-state-verifier.md). _stamp_node_shipped_sha below
# keeps calling these underscore-prefixed aliases rather than the public names: its own test
# (test_node_shipped_sha_stamp.py) patches these exact private paths via `unittest.mock.patch`,
# which rebinds only the attribute it names — a caller that referenced the public name instead
# would silently bypass the mock and spawn real git subprocesses. Do not rename this call site
# without also updating that test's patch targets.
_check_origin_main_reachable = check_origin_main_reachable
_fetch_origin_main = fetch_origin_main
_sha_on_origin_main = sha_on_origin_main
_classify_shas_on_origin_main = classify_shas_on_origin_main


_CHECK_SHIPPED_HELP = """\
Usage: check-shipped-on-main.sh [--verbose] <commit> [<commit>...]

Checks whether all given commits are ancestors of origin/main.

Arguments:
  commit       A commit SHA, branch tip, or symbolic ref (e.g. HEAD).
               Accepts one or more.

Options:
  --verbose    Print one line per commit: "{sha}: ON_MAIN" or "{sha}: NOT_ON_MAIN ({age})"
  --help       Show this help

Exit codes:
  0  All commits are on origin/main.
  1  At least one commit is NOT on origin/main.
  2  Not inside a git repository, or origin/main is unreachable.
"""


def resolve_ref(repo_root: Path, ref: str) -> Optional[str]:
    """``git rev-parse <ref>``, returning the resolved SHA or None if unresolvable."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", ref],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _commit_age_label(repo_root: Path, sha: str) -> str:
    """Human-readable age of ``sha``'s commit timestamp (bash §check-shipped-on-main age calc).

    Falls back to "now" (age 0) when the commit is unreachable — a faithfully-reproduced
    quirk of the original bash: an unresolvable-but-SHA-shaped ``sha`` still reaches this
    point when ``git rev-parse`` accepted it syntactically but ``git log`` can't find it,
    yielding a spurious "0s ago" rather than an error. Not "fixed" here — see port template
    § parity gate (faithfully reproduce pre-existing oracle quirks, don't repair mid-port).
    """
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%ct", sha],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
        commit_ts = int(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else now
    except (OSError, ValueError, subprocess.TimeoutExpired):
        commit_ts = now
    age_secs = now - commit_ts
    if age_secs < 3600:
        return f"{age_secs}s ago"
    if age_secs < 86400:
        return f"{age_secs // 3600}h ago"
    return f"{age_secs // 86400}d ago"


def main(argv: list[str]) -> int:
    """CLI entry — port of ``check-shipped-on-main.sh`` (git merge-base ancestor gate).

    Port of: check-shipped-on-main.sh (coordinator-claude b5a4192c, 2026-07-20).
    Reuses ``sha_on_origin_main`` / ``check_origin_main_reachable`` / ``fetch_origin_main``,
    the same amortised-fetch ancestor-check helpers ``_stamp_shipped_sha`` already exercises.

    Negative-spec: read-only. Never modifies the repo.
    """
    verbose = False
    commits: list[str] = []
    for arg in argv:
        if arg in ("--verbose", "-v"):
            verbose = True
        elif arg in ("--help", "-h"):
            print(_CHECK_SHIPPED_HELP, end="")
            return 0
        elif arg.startswith("-"):
            print(f"Unknown option: {arg}", file=sys.stderr)
            return 1
        else:
            commits.append(arg)

    repo_root = Path.cwd()

    try:
        inside = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        ).returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        inside = False
    if not inside:
        print("check-shipped-on-main: not inside a git repository", file=sys.stderr)
        return 2

    if not check_origin_main_reachable(repo_root):
        fetch_origin_main(repo_root)
        if not check_origin_main_reachable(repo_root):
            print("check-shipped-on-main: origin/main is not reachable", file=sys.stderr)
            return 2

    if not commits:
        print(
            "check-shipped-on-main: no commits specified. Pass at least one SHA, branch, or HEAD.",
            file=sys.stderr,
        )
        return 1

    # Resolve every ref first (one `git rev-parse` per ref, unchanged — resolution of
    # branch names / symbolic refs / HEAD~N is out of this migration's scope), then
    # classify every resolved sha in ONE batched call rather than one
    # `sha_on_origin_main` spawn per ref. Many-commits-against-ONE-ref is the shape
    # `classify_shas_on_origin_main` already batches (its own docstring: two spawns
    # total regardless of set size) — distinct from the many-independent-RANGES shape,
    # which never batches (rev-list set-algebra collapse).
    resolved: dict[str, Optional[str]] = {ref: resolve_ref(repo_root, ref) for ref in commits}
    shas_to_classify = [sha for sha in resolved.values() if sha is not None]
    classified = (
        classify_shas_on_origin_main(repo_root, shas_to_classify) if shas_to_classify else {}
    )

    any_not_on_main = 0
    for ref in commits:
        sha = resolved[ref]
        if sha is None:
            print(f"check-shipped-on-main: cannot resolve '{ref}' — skipping", file=sys.stderr)
            any_not_on_main = 1
            continue

        short = sha[:8]
        # Explicit reconciliation (§ Anti-scope 25): a sha absent from the returned
        # classified map is NEVER read as a resolved classification — it degrades to
        # the same None (unreachable/indeterminate) branch classify_shas_on_origin_main
        # itself uses. classify_shas_on_origin_main's own contract returns an entry for
        # every requested sha, so this ``in`` check is a belt-and-suspenders guard
        # against a future contract change, not a case exercised by that function today.
        result = classified[sha] if sha in classified else None
        if result:
            if verbose:
                print(f"{short}: ON_MAIN")
        else:
            any_not_on_main = 1
            if verbose:
                age = _commit_age_label(repo_root, sha)
                print(f"{short}: NOT_ON_MAIN (committed {age})")

    return any_not_on_main


def _stamp_shipped_sha(handoffs: list[dict], repo_root: Path) -> None:
    """Stamp ``shipped_sha`` in-place on handoffs (bash § SECTION 1.5).

    For all distinct non-null ``shipped_in.sha`` values, one batched
    ``classify_shas_on_origin_main`` call resolves ancestry against origin/main. Parity: one
    amortised fetch if origin/main is not locally reachable; offline degradation (exit 2
    equivalent → all shipped_sha stay null). Records with null ``shipped_in.sha`` receive null
    (no change from collect() stub).
    """
    if not handoffs:
        return

    # Collect distinct non-null shipped_in.sha values (jq unique[]).
    shas: list[str] = []
    seen: set[str] = set()
    for r in handoffs:
        si = r.get("shipped_in")
        if si and isinstance(si, dict):
            sha = si.get("sha")
            if sha and sha not in seen:
                shas.append(sha)
                seen.add(sha)
    if not shas:
        return

    # Amortised gate: check origin/main reachability; fetch once if needed (bash:503-514).
    origin_ok = check_origin_main_reachable(repo_root)
    if not origin_ok:
        fetch_origin_main(repo_root)
        origin_ok = check_origin_main_reachable(repo_root)
    if not origin_ok:
        # Offline degradation: all shipped_sha stay null (bash:515-518).
        return

    # Batch classify; degrade all on any exit-2 equivalent (bash:521-535). One O(1)-spawn
    # call replaces what was a per-SHA `git merge-base --is-ancestor` spawn (see
    # classify_shas_on_origin_main docstring — the 108-spawn hot spot).
    classified = classify_shas_on_origin_main(repo_root, shas)
    shipped_map: dict[str, str] = {}
    if not any(v is None for v in classified.values()):
        shipped_map = {sha: sha for sha, v in classified.items() if v is True}
    # else: any None → exit-2 equivalent; leave shipped_map empty (degrade all).

    # Stamp shipped_sha from the map (keyed by shipped_in.sha).
    for r in handoffs:
        si = r.get("shipped_in")
        if si and isinstance(si, dict):
            sha_key = si.get("sha") or ""
            r["shipped_sha"] = shipped_map.get(sha_key)
        # else shipped_sha stays null (collect() stub)


def _stamp_node_shipped_sha(nodes: list[dict], repo_root: Path) -> None:
    """Stamp ``shipped_sha`` in-place on RoadmapDagNode records (D4 — node-shaped variant).

    Reads/writes the bare-scalar ``shipped_sha`` field DIRECTLY — no transient ``shipped_in``
    dict (RoadmapDagNode has no ``shipped_in`` field; the assembler already exposes the SHA
    as a bare scalar, sourced from the ``shipped_in`` frontmatter key without the dict wrapper).

    Collects distinct non-null ``shipped_sha`` values, runs the same amortised
    origin/main fetch + merge-base ancestor-of check that ``_stamp_shipped_sha`` uses for
    handoffs, and writes the verified-or-null value back to ``node['shipped_sha']``.

    Offline degradation: if origin/main is unreachable after one fetch attempt, all
    ``shipped_sha`` values are set to null (same degradation policy as ``_stamp_shipped_sha``).
    On the handoff path, offline degradation leaves shipped_sha at null (already null in
    collect() stub); on the node path the initial value may be non-null (assembler carries
    the bare SHA), so the degradation must explicitly write null.

    Negative-spec:
      - Does NOT read or write a ``shipped_in`` dict on the node — RoadmapDagNode has no
        such field; no transient key is introduced (F3 — avoids stray non-contract field
        that Zod .object() would strip silently while disk-truth is wrong).
      - Does NOT call this on the live serve path (roadmap.serve) — emit-path only (F1).
      - Does NOT touch handoffs, plans, roadmaps, or edge records.

    Spec backlink: pln-emit-first-class-roadmap-dag-i-137a28 § C3
    """
    if not nodes:
        return

    # Collect distinct non-null bare shipped_sha values.
    shas: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        sha = node.get("shipped_sha")
        if sha and sha not in seen:
            shas.append(sha)
            seen.add(sha)
    if not shas:
        return

    # Amortised gate: check origin/main reachability; fetch once if needed.
    origin_ok = _check_origin_main_reachable(repo_root)
    if not origin_ok:
        _fetch_origin_main(repo_root)
        origin_ok = _check_origin_main_reachable(repo_root)
    if not origin_ok:
        # Offline degradation: all shipped_sha → null (mirrors _stamp_shipped_sha policy).
        for node in nodes:
            node["shipped_sha"] = None
        return

    # Batch classify; degrade all on any exit-2 equivalent (None result). One O(1)-spawn
    # call replaces what was a per-SHA `git merge-base --is-ancestor` spawn (see
    # classify_shas_on_origin_main docstring — the 108-spawn hot spot).
    classified = _classify_shas_on_origin_main(repo_root, shas)
    shipped_map: dict[str, str] = {}
    if not any(v is None for v in classified.values()):
        shipped_map = {sha: sha for sha, v in classified.items() if v is True}
    # else: any None → exit-2 equivalent; leave shipped_map empty (degrade all).

    # Write verified-or-null back to each node's bare-scalar shipped_sha field.
    for node in nodes:
        sha = node.get("shipped_sha") or ""
        node["shipped_sha"] = shipped_map.get(sha)


def _stamp_closure_reachability(records: list[dict], repo_root: Path) -> None:
    """Stamp ``reachable_on_default_branch`` in-place on commit_closures records (AC4).

    Mirrors ``_stamp_shipped_sha``'s discipline verbatim: dedupe distinct SHAs via a seen-set,
    run ONE amortised ``origin/main`` reachability gate (``check_origin_main_reachable`` /
    ``fetch_origin_main``), then ONE batched ``classify_shas_on_origin_main`` call (not a
    per-record spawn) and degrade ALL records to ``null`` on the offline / exit-2-equivalent
    case. Unlike ``_stamp_shipped_sha`` (whose field is a SHA-or-null, not a proper tri-state),
    this stamps a genuine ``true``/``false``/``null`` tri-state — an indeterminate result
    (``classify_shas_on_origin_main`` returning ``None`` for a given sha) is NEVER coerced to
    ``false`` (DECISION-1, AC4).
    """
    if not records:
        return

    # Collect distinct non-null sha values (mirrors bash:499-501 jq unique[] posture).
    shas: list[str] = []
    seen: set[str] = set()
    for r in records:
        sha = r.get("sha")
        if sha and sha not in seen:
            shas.append(sha)
            seen.add(sha)
    if not shas:
        return

    # Amortised gate: check origin/main reachability; fetch once if needed.
    origin_ok = check_origin_main_reachable(repo_root)
    if not origin_ok:
        fetch_origin_main(repo_root)
        origin_ok = check_origin_main_reachable(repo_root)
    if not origin_ok:
        # Offline degradation: every record's reachability stays indeterminate (null),
        # never coerced to false.
        for r in records:
            r["reachable_on_default_branch"] = None
        return

    # Batch classify; degrade ALL to null on any exit-2-equivalent (None) result. One
    # O(1)-spawn call replaces what was a per-SHA `git merge-base --is-ancestor` spawn (see
    # classify_shas_on_origin_main docstring — the 108-spawn hot spot).
    classified = classify_shas_on_origin_main(repo_root, shas)
    if any(v is None for v in classified.values()):
        # origin/main became unreachable / a bad object was seen — degrade all, no partial stamp.
        for r in records:
            r["reachable_on_default_branch"] = None
        return
    for r in records:
        sha = r.get("sha")
        r["reachable_on_default_branch"] = classified.get(sha) if sha else None


def _stamp_initiative_goals(initiatives: list[dict], goals_current: list[dict]) -> None:
    """Post-collect enricher: resolve each initiative's ``_goal_ids`` into full Goal records.

    Purpose: bridge the goals->initiatives->status ladder (D24, 2.13.0). The section spine
    contract (``collect(ctx) -> (records, malformed)``, ``ctx``-only input) gives
    ``sections/initiatives.py`` no access to ``sections/goals.py``'s output, so the id-list ->
    full-record join CANNOT live in either section's ``collect()`` — it runs here, after both
    sections have collected, mirroring the ``_deliverable_status.stamp`` cross-join pattern
    (:540 above).

    Resolution: each id in an initiative's staging ``_goal_ids`` list is matched against
    ``goals_current`` scoped to the SAME ``(repo, coordinator_root_path)`` as the initiative
    record (a goal id is only unique within that scope, not globally).

    Emit predicate: the record's ``goals`` key is populated with the resolved Goal list ONLY
    when ``_goal_ids`` is non-empty AND at least one id resolves; otherwise the key is
    OMITTED entirely (``goals`` is ``.optional()``, not ``.nullable()`` — D24, absent-when-
    absent). An unresolvable id (goal not found in scope) is silently dropped, not an error —
    resolution is best-effort so a stale/typo'd id in the initiative YAML never quarantines
    the whole InitiativeSummary record (only a malformed NESTED Goal payload does that, per
    the schema's all-or-nothing nested-parse-failure behavior — this join only ever emits
    already-schema-valid Goal records straight from ``goals_current``, so that failure mode
    cannot occur here).

    The staging ``_goal_ids`` key is POPPED from every record unconditionally (whether or
    not any ids resolved) — the InitiativeSummary schema is ``.strict()``
    (``additionalProperties: false``); a leaked staging key would reject the whole envelope
    at the consumer's Zod parse.

    Negative-spec: does NOT read ``goals-log.*.jsonl`` (that derivation lives exactly once,
    in the shared context-free reader ``coordinator_core/goals/wire_read.py``
    (``read_and_collapse``) — not duplicated as a second glob/parse/collapse loop here or
    anywhere else); does NOT invent an ``initiative`` field on the nested Goal (the
    Goal wire shape has none — the join is initiative-owns-the-FK, not goal-owns-a-backref).

    Spec backlink: pln-claude-klabauter-artifact-emit-2-13-0-go-e1f844 § C3
    """
    for record in initiatives:
        goal_ids = record.pop("_goal_ids", [])
        if not goal_ids:
            continue
        scope_repo = record.get("repo")
        scope_root = record.get("coordinator_root_path")
        resolved: list[dict] = []
        for goal_id in goal_ids:
            # Review: code-reviewer (Finding 5) — first-match-wins assumes goal_id is
            # unique within a (repo, coordinator_root_path) scope. goals.py's own
            # latest-wins dedup keys on (repo, coordinator_root_path, period,
            # period_value) — NOT goal_id — so nothing currently enforces that
            # assumption; a violation would silently resolve to whichever matching
            # record is encountered first in goals_current, with no signal.
            for goal in goals_current:
                if (
                    goal.get("goal_id") == goal_id
                    and goal.get("repo") == scope_repo
                    and goal.get("coordinator_root_path") == scope_root
                ):
                    resolved.append(goal)
                    break
        if resolved:
            record["goals"] = resolved


def build(ctx: EmitContext, *, sections: "Optional[dict[str, _RegisteredSection]]" = None) -> dict:
    """Assemble the full envelope object (bash Section 9, :2730-2924).

    Runs the ported preconditions first: pin-integrity assertion + content-based
    CONTRACT_VERSION-vs-bundle guard (delegated to validate.py), which also resolves the
    ``schema_version`` from the vendored bundle .version (bash:2751). Then scaffolds the
    empty skeleton and overlays each registered section's collect() output. An empty
    registry yields the empty-but-shaped envelope (graceful-absent contract).
    """
    validate.assert_pin_integrity()
    schema_version = validate.assert_version_consistency()

    # NOTE: distinct clock from ctx.observed_at (per-row); rag uses this top-level value as
    # emission-uniform; the two can differ by section-collection time if collection is slow.
    # Review: code-reviewer (Slice-1 F6) — latent-divergence trap documented for rag query-surface readers.
    emitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = _empty_skeleton(schema_version, emitted_at, ctx.hostname)

    registry = sections if sections is not None else _REGISTRY
    for section in registry.values():
        records, malformed = section.collect(ctx)
        section.place(envelope, records, malformed)

    # --- Post-collect enrichment (order matters) ---
    # 1. LMA stamp — handoffs, plans, roadmaps (bash §1 step 3.5, §8.6, §8.8). One shared
    #    git-log walk across all three groups (PERF 2026-07-29 — see _stamp_lma docstring).
    _stamp_lma(
        [envelope["handoffs"], envelope["plans"], envelope["roadmaps"]],
        ctx.repo_root,
    )

    # 2. shipped_sha stamp — handoffs (bash § SECTION 1.5).
    _stamp_shipped_sha(envelope["handoffs"], ctx.repo_root)

    # 2b. shipped_sha stamp — DAG nodes (bare-scalar node-shaped variant; D4 / C3).
    _stamp_node_shipped_sha(envelope["roadmap_dag_nodes"], ctx.repo_root)

    # 2c. reachable_on_default_branch stamp — commit_closures (AC4; mirrors _stamp_shipped_sha).
    _stamp_closure_reachability(envelope["commit_closures"], ctx.repo_root)

    # 3. deliverable_status cross-join — handoffs × plans × roadmaps (bash § SECTION 8.16).
    _deliverable_status.stamp(
        envelope["handoffs"],
        envelope["plans"],
        envelope["roadmaps"],
        worktree_root=ctx.repo_root,
    )

    _stamp_content_hash(envelope, ctx, schema_version)

    # 2d. docs_staleness stamp — exec_summaries (C6). Populates the
    #     required-with-null field sections/exec_summary.py::collect() never
    #     sets; see _stamp_docs_staleness docstring for the null-vs-[]
    #     contract and the never-fail-emission degrade path.
    _stamp_docs_staleness(envelope, ctx)

    # 3b. emission-scope projection-validation (D25 §6, introduced at 2.14.0) — never
    #     mutates envelope; fails loud if a per-repo record's ephemeral {scope,repo,
    #     provenance} projection does not validate. No-op below the 2.14.0 gate threshold;
    #     LIVE on every build() call once the vendored pin reaches or exceeds it (naming a
    #     "current vendored pin" number here is what went stale last time — Review:
    #     code-reviewer, Finding 3 — check validate.read_schema_version() for the live pin).
    validate._validate_emission_scope(envelope, ctx, schema_version)

    # 4b. initiative goals[] resolution — id-list (from initiatives.collect() staging
    #     _goal_ids) x goals_current cross-join (D24, 2.13.0). Must run after both
    #     `initiatives` and `goals` sections have collected (guaranteed — this is
    #     post-collect enrichment) and pops the staging key unconditionally.
    _stamp_initiative_goals(envelope["initiatives"], envelope["goals_current"])

    # 5. backlog_history block (C5 — snake_case per contract v2.7.0, coordinator-claude+PM convention call +
    #    the Director of Engineering review; 2026-07-06 cross-repo memo; D9 default until contract declares the block;
    #    gate is contract-presence not a version sentinel — amended D6 premise, plan § D6
    #    as amended by docs/plans/2026-07-05-backlog-history-emit-gate-decouple.md § Option C).
    # Always non-null provenance (cockpit provenance_fk NOT NULL); series empty in D9 path.
    envelope["backlog_history"] = _backlog_history.collect(ctx)

    return envelope


# ---------------------------------------------------------------------------
# Output write
# ---------------------------------------------------------------------------
def emit(ctx: EmitContext, out: Optional[str | Path] = None) -> dict:
    """Build and write the cockpit-emission artifact, unconditionally.

    ``out`` None => canonical path ``<central_state_root>/cockpit-emission.json``. An
    explicit ``out`` selects a non-default path. Returns a small result dict for the
    JSON-RPC handler.

    Malformed-quarantine visibility (Review: code-reviewer — Finding 1): a record failing
    contract validation is diverted into ``envelope["malformed_records"][<bucket>]`` on disk,
    but that diversion is otherwise invisible to whatever calls this JSON-RPC op — a green
    ``ok: True`` result reads identically whether 0 or 67 records were silently quarantined
    this run (the exact incident shape this cutover fixes elsewhere). The return dict now
    carries a per-bucket ``malformed_counts`` (non-empty buckets only) plus a summed
    ``malformed_total``, and a non-empty quarantine additionally prints a loud one-line
    stderr signal — so a monitoring layer reading only the return value, or an operator
    reading only stdout, still sees the drop.
    """
    if out is None:
        out_path = Path(ctx.central_state_root) / DEFAULT_OUTPUT_NAME
    else:
        out_path = Path(out)

    envelope = build(ctx)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    malformed_counts = {
        bucket: len(records)
        for bucket, records in envelope["malformed_records"].items()
        if records
    }
    malformed_total = sum(malformed_counts.values())
    if malformed_total:
        print(
            f"emit: WARNING — {malformed_total} record(s) quarantined as malformed "
            f"this run: {malformed_counts}. See malformed_records in {out_path}.",
            file=sys.stderr,
        )

    return {
        "ok": True,
        "out": str(out_path),
        "schema_version": envelope["schema_version"],
        "emitted_at": envelope["emitted_at"],
        "section_count": len(_REGISTRY),
        "malformed_counts": malformed_counts,
        "malformed_total": malformed_total,
    }


# ---------------------------------------------------------------------------
# Run-context resolution (engine wiring seam)
# ---------------------------------------------------------------------------
def _resolve_central_state_root(coordinator_root: Path, cwd: Path) -> Path:
    """Resolve ``coordinator_state_root --central`` natively (no bash spawn).

    Oracle: ``lib/coordinator-state-root.sh`` (coordinator-claude 6fb5fb37, 2026-07-22)'s
    ``coordinator_state_root --central`` with
    no ``--subject``/``--artifact`` — Rule 4 (backward-compat default): resolves to
    ``$(_csr_claude_klabauter_root)/state``. The bash lib's own ``_csr_claude_klabauter_root`` is itself
    documented as a native bridge onto ``coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root``
    (see that lib's "Native bridge" comment) — this calls the SAME native resolver directly,
    in-process, retiring the ``bash -c "source ... && coordinator_state_root --central"``
    spawn entirely. Falls back to the pre-migration computed path (~/.claude/state) only
    when the resolver fails, exactly as the previous bridge's except-clause did.

    ``coordinator_root`` and ``cwd`` are accepted for signature/call-site compatibility
    (the oracle's seam took both) but are not consulted: Rule 4's resolution depends only
    on the claude-klabauter-root resolver, never on the coordinator/cwd it's invoked from.

    Review: code-reviewer (F2/F3) — the except-clause is ``Exception`` (not the narrower
    ``(RuntimeError, ImportError)``) so it actually matches this docstring's "falls back
    ... only when the resolver fails" claim (``coordinator_claude_klabauter_root()``'s pointer-file
    read only catches ``OSError``, so a corrupt/non-UTF-8 pointer file raises
    ``UnicodeDecodeError`` — a ``ValueError`` subclass the narrower clause missed and would
    have let crash ``emit()`` instead of degrading). The fallback also now ``warnings.warn``s
    — this project has already had one break-class incident from a *silent* legacy-path
    fallback on a durable-settings-home-only machine (unset ``repos.claude_klabauter``); a
    resolver failure here must be observable, not swallowed, matching the sibling pattern in
    ``resolve_coordinator_root``'s own env-override fallback warning below.
    """
    try:
        from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root

        claude_klabauter_root = coordinator_claude_klabauter_root()
        if claude_klabauter_root:
            return Path(claude_klabauter_root) / "state"
    except Exception as exc:  # noqa: BLE001 — any resolver failure degrades, per docstring
        import warnings

        warnings.warn(
            f"coordinator_claude_klabauter_root() failed ({type(exc).__name__}: {exc}); falling back "
            "to the pre-migration CLAUDE_HOME/.claude/state path. This usually means "
            "repos.claude_klabauter is unset in the machine-local registry, or a "
            "partially-installed/durable-settings-home-only machine — verify "
            "`machine-local get repos.claude_klabauter` resolves before trusting emitted state.",
            stacklevel=2,
        )
    # pre-W4.2 assumption: coordinator_root was always 4 levels below ~/.claude; post-W4.2
    # it is the coordinator-claude clone at an arbitrary depth so .parent.parent.parent lands in the wrong
    # place. Derive from CLAUDE_HOME directly, same as resolve_context() does.
    return Path(os.environ.get("CLAUDE_HOME", str(Path.home()))) / ".claude" / "state"


def _registry_coordinator_root() -> Optional[Path]:
    """Registry rungs 3-4 of ``resolve_coordinator_root``, extracted for direct
    testability. Per-key precedence over the two machine-local registry files
    (``registry.local.toml`` wins, tracked ``registry.toml`` fills gaps —
    matches ``machine_resolver.registry_get``; the ``.local``-only read
    predated that pattern), key-major rung order preserved (``live_path``
    before ``repos.example_doctrine_repo``). Direct file reads only — bootstrap-safety
    invariant, no CLI/subprocess (see ``resolve_coordinator_root``'s
    docstring)."""
    reg_dir = machine_local_dir()
    # (parsed-dict-or-None, raw-text) per existing registry file, precedence-ordered.
    files: list = []
    for fname in ("registry.local.toml", "registry.toml"):
        path = reg_dir / fname
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        data = None
        try:
            try:
                import tomllib as _toml_mod  # stdlib 3.11+
            except ImportError:
                import tomli as _toml_mod  # type: ignore[no-redef]  # third-party fallback
            data = _toml_mod.loads(text)
        except Exception:
            import warnings
            warnings.warn(
                f"resolve_coordinator_root: tomllib/tomli unavailable or parse error on {fname}; "
                "falling back to quoted-key regex (may miss nested-table TOML forms). "
                "Install tomli or upgrade to Python 3.11+.",
                stacklevel=2,
            )
        files.append((data, text))

    for key_path, suffix in (
        (["plugin", "mirrors", "coordinator-claude", "live_path"], ""),
        (["repos", "example_doctrine_repo"], "/coordinator"),
    ):
        flat_key = ".".join(key_path)
        for data, text in files:
            if data is None:
                # Parse-failed file: quoted-key regex fallback on the raw text.
                ek = re.escape(flat_key)
                m = re.search(rf'"{ek}"\s*=\s*["\']([^"\']+)["\']', text)
                if m:
                    val = str(normalize_native_path(m.group(1).strip())) + suffix
                    candidate = Path(val).expanduser()
                    if (candidate / "bin" / "query-records.py").exists():
                        return candidate
                continue
            try:
                node: object = data
                for k in key_path:
                    node = node[k]  # type: ignore[index]
                candidate = Path(str(normalize_native_path(node)) + suffix).expanduser()
                if (candidate / "bin" / "query-records.py").exists():
                    return candidate
            except (KeyError, TypeError):
                # Nested-table form absent for this key_path -- fall through to
                # the flat quoted-compound-key form tried below.
                pass
            # Flat quoted-compound-key form: the machine-local registry CLI writes
            # "plugin.mirrors.coordinator-claude.live_path" = '...' as a single dotted
            # top-level key (tomllib parses it as-is, not as a nested structure).
            flat_val = data.get(flat_key)
            if flat_val is not None:
                try:
                    candidate = Path(str(normalize_native_path(flat_val)) + suffix).expanduser()
                    if (candidate / "bin" / "query-records.py").exists():
                        return candidate
                except (TypeError, ValueError):
                    # Flat-key value could not be normalized into a path -- try the
                    # next registry file / key_path candidate.
                    pass
    return None


def resolve_coordinator_root() -> Path:
    """Resolve the live post-W4.2-cutover coordinator script/lib root.

    Purpose: locate the coordinator clone whose ``bin/query-records.py`` exists so section
    porters that read from it find the real records reader.  The W4.2 cutover relocated the
    coordinator SOURCE out of ``~/.claude/plugins/coordinator/`` into the
    coordinator-claude clone at ``<doe-root>/coordinator``; the legacy plugin dir is now stale/empty.  The
    2026-07-22 de-node cutover then retired ``bin/query-records.js`` fleet-wide (claude-klabauter's own
    production dependency on it dropped to zero -- see
    ``cross-repo/archive/2026-07-22-claude-klabauter-em-query-records-positive-clearance-de-node-cutover-landed.md``
    -- and coordinator-claude/coordinator/bin now carries neither the ``.js`` oracle nor a ``.py``
    port), so ``bin/query-records.py`` (claude-klabauter's own native, de-node-durable reader) is now
    the sentinel, not the deleted ``.js`` file.  Section porters that read from
    ``ctx.coordinator_root / "bin" / "query-records.py"`` would silently fall back to ``[]``
    (hollow emission) without this resolver.  Mirrors the bash oracle
    (``emit-cockpit-snapshot.sh``) which derives ``COORDINATOR_ROOT`` from its own
    script-location for the same reason.

    Resolution order (rungs 1-4 are direct file reads only — never CLI, per Design pin 4
    doctrine; rung 5 is the sole CLI-shellout exception, see below):
      1. ENV ``COORDINATOR_ROOT`` — set in CI or by the coordinator plugin loader.
      2. Co-located ``<claude-klabauter-repo-root>/coordinator`` (2026-07-22 executable-surface
         migration, commits b644d5a9/8a28a6ca): the coordinator bin/lib/scripts tree now
         lives INSIDE this repo, not the coordinator-claude clone — coordinator-claude/coordinator/bin is
         empty post-migration (PM ruling: coordinator scripts must not execute out of
         ``~/.claude``). Checked first because it is now the canonical, common case and
         needs no registry/subprocess round-trip. Mirrors ``data_root.py``'s
         ``_colocated_root()`` (same repo-root-relative ``/ "coordinator"`` landing spot).
      3. Machine-local registry key ``plugin.mirrors.coordinator-claude.live_path``
         (pre-migration W4.2 cutover path; kept for machines/fixtures whose registry
         still points at a valid coordinator-claude mirror). Per-key file precedence:
         ``registry.local.toml`` wins, tracked ``registry.toml`` fills gaps
         (``machine_resolver.registry_get`` semantics; see ``_registry_coordinator_root``).
      4. Machine-local registry key ``repos.example_doctrine_repo`` + ``/coordinator`` suffix
         (same two-file per-key precedence).
      5. ``resolve-coordinator-clone --for-content`` subprocess (survivor CLI at
         ``~/.claude/bin/resolve-coordinator-clone``) — replaces the pre-repoint legacy
         plugin dir read, which was stale/empty post-W4.2 cutover.

    Each candidate is validated by ``(candidate / "bin" / "query-records.py").exists()``.
    Raises ``RuntimeError`` with a diagnostic when no candidate passes.

    Shared production helper — imported by the parity test suite (``test_emit_parity.py``)
    instead of maintaining a duplicate copy.

    Bootstrap-safety invariant (load-bearing, AC2): the machine-local registry read (rungs 3-4
    above) is a deliberate direct-TOML file read via ``_settings_home.machine_local_dir()``,
    NEVER the ``machine-local`` CLI. This resolver runs on the coordinator-root bootstrap path,
    before PATH/CLI availability is guaranteed — shelling out to the CLI here would break
    bootstrap on a fresh checkout. See ``docs/plans/2026-07-11-coordinator-core-home-claude-read-repoint.md``
    § "bootstrap-safety invariant" for the doctrine reconciliation against the general
    registry-key-read CLI-shellout preference (``machine-local-registry.md:184``), which does
    NOT apply to this bootstrap-path subset.

    Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C2
    Spec backlink (settings-home repoint): pln-repoint-coordinator-core-claud-56d805 § C2
    """
    env_val = os.environ.get("COORDINATOR_ROOT", "").strip()
    if env_val:
        p = Path(env_val).expanduser()
        if (p / "bin" / "query-records.py").exists():
            return p
        # Review: code-reviewer (F3) — warn when the explicit COORDINATOR_ROOT override fails
        # validation; silent drop-through masks typos and stale/partial checkouts.
        import warnings
        warnings.warn(
            f"COORDINATOR_ROOT is set to {str(p)!r} but "
            f"{str(p / 'bin' / 'query-records.py')!r} is absent; "
            "the explicit override was ignored. Update COORDINATOR_ROOT to the coordinator "
            "plugin root or add a machine-local registry entry pointing at the coordinator-claude clone.",
            stacklevel=2,
        )

    # Rung 2 (2026-07-22 executable-surface migration) — see docstring rung 2 above.
    # coordinator_core/ops/emit/envelope.py -> parent.parent.parent.parent == this
    # repo's root, then "/coordinator" to land on the co-located bin/lib/scripts tree.
    _colocated_root = Path(__file__).resolve().parent.parent.parent.parent / "coordinator"
    if (_colocated_root / "bin" / "query-records.py").exists():
        return _colocated_root

    # Direct TOML read (no CLI) — see bootstrap-safety invariant in the docstring above.
    registry_candidate = _registry_coordinator_root()
    if registry_candidate is not None:
        return registry_candidate

    # Legacy fallback: resolve the coordinator content root via the survivor CLI
    # (~/.claude/bin/resolve-coordinator-clone --for-content) rather than reading the
    # stale ~/.claude/plugins/coordinator-claude/coordinator plugin dir directly, which
    # is empty post-W4.2 cutover. Spec backlink:
    # docs/plans/2026-07-11-coordinator-core-home-claude-read-repoint.md § C4 (site 2).
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [str(Path.home() / ".claude" / "bin" / "resolve-coordinator-clone"), "--for-content"],
            capture_output=True,
            text=True,
            timeout=15,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        # Review: code-reviewer (Findings 2, 3) — added timeout=15 to match the
        # sibling C4 sites (liveness.py, and the OLD wsc_commit.py, retired
        # 2026-07-29 kill-list op removal) and resolve the absolute
        # survivor path instead of a bare PATH lookup for consistency.
        proc = None
    if proc is not None and proc.returncode == 0:
        legacy = Path(proc.stdout.strip())
        # Review: code-reviewer (F1) — validate query-records.py on the resolved path too;
        # returning on directory-existence alone re-introduces the hollow-emission bug if
        # the resolved clone is stale/partial.
        if (legacy / "bin" / "query-records.py").exists():
            return legacy
        raise RuntimeError(
            f"resolve-coordinator-clone --for-content returned {legacy} but "
            f"query-records.py not found at {legacy / 'bin' / 'query-records.py'}; "
            "this is a stale/partial checkout. Add a machine-local registry entry "
            "pointing at the coordinator-claude clone, or set COORDINATOR_ROOT to the coordinator plugin root."
        )
    raise RuntimeError(
        "coordinator root not found: checked COORDINATOR_ROOT env, the co-located "
        "<claude-klabauter-repo-root>/coordinator tree, machine-local registry, and "
        "resolve-coordinator-clone --for-content; "
        "set COORDINATOR_ROOT to the coordinator plugin root"
    )


def resolve_context(repo_root: Optional[Path] = None) -> EmitContext:
    """Build an EmitContext for the given repo root (or the meta-repo if none supplied).

    When ``repo_root`` is passed, the emission is rooted at that repo:
      - ``central_state_root`` is set to ``repo_root / "state"`` directly (no shell seam).
        # NOTE: holds a PER-REPO root post-2026-07-07 cutover, NOT central
      - Attribution (``repo_name``) is resolved from ``repo_root``'s own git remote.
    When ``repo_root`` is None (default), falls back to the pre-cutover behaviour:
    meta-repo (~/.claude) resolution via the shell seam.  This preserves backward
    compatibility for param-less callers (``artifact_emit.py:39``, ``goal_append.py:141``,
    ``recorder.py:173``) that are NOT wired until C4.

    ``coordinator_root`` = the LIVE post-W4.2-cutover coordinator script/lib clone,
    discovered via ``resolve_coordinator_root()`` regardless of which repo is emitting.

    Spec backlink: pln-per-repo-emission-cutover-un-h-03f05e § C2
    """
    coordinator_root = resolve_coordinator_root()
    if repo_root is not None:
        # Per-repo path: root state at repo_root/state directly, bypassing the --central seam.
        # NOTE: holds a PER-REPO root post-2026-07-07 cutover, NOT central
        central_state_root = repo_root / "state"
    else:
        # Legacy meta-repo path (pre-C4 callers; stays backward-compatible).
        claude_home = Path(os.environ.get("CLAUDE_HOME", str(Path.home())))
        repo_root = claude_home / ".claude"
        central_state_root = _resolve_central_state_root(coordinator_root, repo_root)
    return EmitContext.resolve(
        repo_root=repo_root,
        coordinator_root=coordinator_root,
        central_state_root=central_state_root,
    )


# ---------------------------------------------------------------------------
# C3 Section wiring — auto-wire all 21 section porters at import time.
# Lazy imports (inside the function body) avoid circular-import at the module
# level; the section modules import only from context.py, not from envelope.py.
# Split sections (backlogs, rollups) supply explicit place fns. All others use
# the default single-target placer derived from SECMAP.
# ---------------------------------------------------------------------------

def _wire_sections() -> None:
    """Register all 21 section porters into the envelope registry.

    Called once at module import (bottom of this file). Idempotent: a pre-populated
    registry (test double or re-import) is left unchanged by the early-return guard at the top of this function.
    Split sections (backlogs -> {bug,debt,improvement}; rollups -> {day,week}) supply
    explicit place fns so their combined collect() output is distributed correctly.

    Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C3
    """
    if _REGISTRY:
        # Already wired (re-import or test double installed first).
        return

    from coordinator_core.ops.emit.sections import (  # noqa: PLC0415
        handoffs,
        backlogs,
        review_trail,
        routine_signals,
        rollups,
        goals,
        coordinator_roots,
        branch,
        lessons,
        plans,
        cross_repo_memos,
        roadmaps,
        trackers,
        health,
        decision_guides,
        session_hierarchy,
        file_attribution,
        initiatives,
        exec_summary,
        roadmap_dag,
        commit_closures,
    )

    # --- Single-target sections (default placer from SECMAP) ---
    for _mod, _name in (
        (handoffs,          "handoffs"),
        (review_trail,      "review_trail"),
        (routine_signals,   "routine_signals"),
        (goals,             "goals"),
        (coordinator_roots, "coordinator_roots"),
        (branch,            "branch"),
        (lessons,           "lessons"),
        (plans,             "plans"),
        (cross_repo_memos,  "cross_repo_memos"),
        (roadmaps,          "roadmaps"),
        (trackers,          "trackers"),
        (health,            "health"),
        (decision_guides,   "decision_guides"),
        (session_hierarchy, "session_hierarchy"),
        (file_attribution,  "file_attribution"),
        (initiatives,       "initiatives"),
        (exec_summary,      "exec_summary"),
        (commit_closures,   "commit_closures"),
    ):
        register_section(_name, _mod.collect)

    # --- Split section: backlogs (bug / debt / improvement) ---
    def _place_backlogs(envelope: dict, records: list, malformed: list) -> None:
        """Distribute backlogs collect() output by type into backlogs.{bug,debt,improvement}."""
        by_type: dict[str, list] = {"bug": [], "debt": [], "improvement": []}
        for r in records:
            t = r.get("type")
            if t in by_type:
                by_type[t].append(r)
        envelope["backlogs"]["bug"] = by_type["bug"]
        envelope["backlogs"]["debt"] = by_type["debt"]
        envelope["backlogs"]["improvement"] = by_type["improvement"]
        envelope["malformed_records"]["backlogs"] = list(malformed)

    register_section("backlogs", backlogs.collect, _place_backlogs)

    # --- Split section: rollups (day / week grain) ---
    def _place_rollups(envelope: dict, records: list, malformed: list) -> None:
        """Distribute rollups collect() output by grain into completion_rollups.{day,week}."""
        day = [r for r in records if r.get("grain") == "day"]
        week = [r for r in records if r.get("grain") == "week"]
        envelope["completion_rollups"]["day"] = day
        envelope["completion_rollups"]["week"] = week
        # no malformed bucket for rollups

    register_section("rollups", rollups.collect, _place_rollups)

    # --- Split section: roadmap_dag (nodes / edges) ---
    def _place_roadmap_dag(envelope: dict, records: list, malformed: list) -> None:
        """Route roadmap DAG collect() output into roadmap_dag_nodes and roadmap_dag_edges.

        Records are tagged kind='node'|'edge' by collect(); this fn strips the tag (it is
        an internal routing token, not a contract field) and distributes into the two arrays.
        Malformed rows are similarly split by kind and routed into their respective buckets.
        Neither kind is duplicated into the other's bucket (F7 — no combined-list copy).

        Forward provision — mal_edges bucket: the mal_edges routing branch handles
        kind='edge' malformed rows, but collect() currently never generates them. Assembler
        exceptions always produce kind='node' malformed rows (a single per-roadmap_id quarantine
        entry). The edge-malformed bucket will always be empty on the current collect() path.
        This branch is kept for future collect() extensions that might produce edge-malformed
        rows without requiring a place fn change.
        Review: code-reviewer (F4) — dead-code status documented; regression catch in
        test_malformed_row_from_injected_assembler_failure asserts the bucket stays empty.
        """
        nodes = [{k: v for k, v in r.items() if k != "kind"} for r in records if r.get("kind") == "node"]
        edges = [{k: v for k, v in r.items() if k != "kind"} for r in records if r.get("kind") == "edge"]
        envelope["roadmap_dag_nodes"] = nodes
        envelope["roadmap_dag_edges"] = edges

        mal_nodes = [{k: v for k, v in r.items() if k != "kind"} for r in malformed if r.get("kind") == "node"]
        mal_edges = [{k: v for k, v in r.items() if k != "kind"} for r in malformed if r.get("kind") == "edge"]
        envelope["malformed_records"]["roadmap_dag_nodes"] = mal_nodes
        envelope["malformed_records"]["roadmap_dag_edges"] = mal_edges

    register_section("roadmap_dag", roadmap_dag.collect, _place_roadmap_dag)


_wire_sections()
