"""
coordinator_core.workstream_complete — the `workstream-complete-assemble`
computed-skill engine.

Purpose: computes `/workstream-complete`'s full ceremony spine (Step 0
session-shape detection through Step 4 final summary) into one read-only
decision object per the frozen contract, and exposes a standalone MUTATING
`apply` half (see `coordinator_core.workstream_complete.apply`, C4) that
executes the computed `directives[]` against the resolved `judgment_points[]`
dispositions. This is the wiring seam (C3,
`docs/plans/2026-07-26-workstream-complete-computed-frontage.md`) that folds
the seven directive/judgment submodules (`directives_lessons_plan.py`,
`directives_completion.py`, `directives_memo_lifecycle.py`,
`directives_review.py`, `directives_commit_tail.py`,
`directives_session_hygiene.py`, `judgments.py`) into this module's `brief()`
— this package is the first multi-module computed-skill assembler in the
tree (D-4/F6 of the same plan); every submodule is a pure, `__init__`-
independent builder, and this module is the ONLY one that reads the
CONSUMES_MANIFEST, assembles the 8-key envelope, and exposes the CLI.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Branches computed against: DoE-claude coordinator/skills/workstream-complete/SKILL.md
Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md,
chunk C3 (wiring the manifest/submodules/apply-verb landed here). Original
compute-only convert: docs/plans/2026-07-21-canonical-resolution-engine.md,
chunk W2-B1.

Registration seam: this module ships no bash veneer and needs none — it is
consumed directly by the `coordinator/bin/workstream-complete-assemble`
trampoline, which delegates its entire `argv` to this module's `main()`
(direct-import template-variant #1, mirrors `pickup-assemble`; DR-088 ladder
discipline). `main()` dispatches TWO subcommands — `brief` (this module,
read-only) and `apply` (`coordinator_core.workstream_complete.apply`,
mutating) — through the SAME trampoline binary, matching
`workday-complete-assemble`'s one-trampoline-two-verbs precedent (AC9) rather
than a second generated shim pair.

READ-ONLY, by construction: every function in THIS module only reads
disk/git state. Mutating actions are returned as `directives[]` entries
naming an existing atomic CLI (see CONSUMES_MANIFEST below) — this module
never shells out to a mutating verb, never writes a file, and never runs
`git fetch`/`git commit`. The MUTATING half lives entirely in
`coordinator_core.workstream_complete.apply` (C4), never here.

Consumes manifest (orchestrates, reimplements none) — see CONSUMES_MANIFEST
for the literal tuple; grouped here by which submodule names each CLI:
    coordinator/bin/wsc-session-disposition.py (loaded by file path, see
        `_load_bin_module` — a hyphenated bin script, not a package)
        -> resolve_session_id / resolve_disposition, folded into
        gates.session_shape. NOT a CONSUMES_MANIFEST member (loaded
        in-process, never dispatched as a directive).
    wsc-coverage-gate-runner.py, check-workstream-complete-deletion-blocks.py,
        wsc-close.py, wsc-tail.py -> this module's own pre-existing Step
        2.4/2.9/2.67/3 directive spine (unchanged from Convert #2, save the
        two renames noted in the Negative-spec below).
    coordinator-lesson-add, coordinator-queue-append, archive-stamp-cli,
        coordinator-harvest-deferrals -> `directives_lessons_plan.py` (C2a).
    coordinator-complete-entry.py, reconcile-completion-commits.py,
        coordinator-fold-execution-record -> `directives_completion.py` (C2b).
    (archive-stamp-cli, wsc-close.py, both already listed above) ->
        `directives_memo_lifecycle.py` (C2c) contributes no CLI beyond those
        two already-manifested names.
    regenerate-orientation-cache, check-machine-local-regeneratability.py ->
        `directives_session_hygiene.py` (C2i). Step 2.96's completeness-
        checklist WARN gate has NO backing CLI (a pure read+render, per that
        module's own Design note) and is surfaced as `gates.completeness_
        checklist`, never a `directives[]` entry.
    review-brightline-gate.py, freeze-review-diff.py, fan-out-integrator.py,
        scan_unresolved_ubt_records.py, classify-dispatch-shape.py ->
        `directives_review.py` (C2d). `wsc-coverage-gate-runner.py`'s
        `coverage-gate`/`write-trail` subcommands are already manifested via
        the pre-existing `d-coverage-gate`/`d-write-trail` directives below —
        this module's OWN `build_chain_coverage_gate_directive`/
        `build_write_review_trail_directive` builders are deliberately NOT
        wired a second time here (see Negative-spec: no duplicate-CLI
        directive pairs).
    session-claim-cli, emit-cadence -> `directives_commit_tail.py` (C2e).
        (`wsc-close.py`/`wsc-tail.py` already manifested; this module's
        `build_close_tail_args_directive`/`build_wsc_tail_directive`
        SUPERSEDE this module's own pre-existing `d-close-tail-args`/`d-tail`
        inline builders — see Negative-spec.)
    coordinator_core.contract.decision_object.envelope.build_envelope / emit
        -> the 8-key envelope + fail-loud validation chokepoint.
    coordinator_core.contract.decision_object.judgment.build_judgment_point /
        build_untrusted_gate_judgment_point / build_disposition
        -> judgment_points[] construction (this module's own 2 pre-existing
        points; `judgments.py`'s 29 preserved points import the same
        constructors independently).
    coordinator_core.pickup_assemble.resolve_repo_root -> AC8's shared-resolver
        repoint (see Negative-spec for what this replaces, and for the
        "zero-spawn" claim that repoint did NOT earn).

Negative-spec:
    - Do NOT add a mutating code path here. A finding that "the assembler
      should just do X" for any X that writes to disk, stages a commit, or
      calls an op belongs in `directives[]`, not in a new function body.
    - NARROW READ-ONLY CARVE-OUT (C4, docs/plans/2026-08-01-wsc-completeness-
      gate-and-pickup-successor.md): this module DOES dispatch the
      `handoff.has_live_children` op in-process, by op name only — never
      "ops may be called" generally. That op writes nothing to disk, stages
      no commit, and mutates no state; it is a pure read used to build
      `gates.consumed_handoff_completeness` evidence (leg B of the pre-
      commit completeness judgment point below). This does not weaken the
      prohibition above: any op whose contract includes a write is still
      forbidden here and belongs in `directives[]`.
    - Do NOT re-implement `wsc-session-disposition.py`'s 3-detector chain
      here — it is loaded and called, not ported a second time.
    - `resolve_repo_root` is now `coordinator_core.pickup_assemble.
      resolve_repo_root` (AC8), not a local `subprocess.run(["git",
      "rev-parse", ...])` call. Convert #2 shipped the plain-subprocess
      version and flagged this repoint as "out of scope for this chunk" in
      its own docstring; C3 closes it.
      **CORRECTED 2026-08-11 (C5, docs/plans/2026-08-11-ceremony-closes-
      against-a-foreign-repo.md): this repoint is NOT "zero-spawn," and the
      text here claimed it was.** `pickup_assemble.resolve_repo_root` itself
      runs `git rev-parse --show-toplevel` through `_run_git` — the repoint
      relocated the one spawn into the shared resolver, it did not remove it.
      The AC8 spawn-budget accounting in `docs/plans/2026-08-01-wsc-
      completeness-gate-and-pickup-successor.md` § C3 was computed off this
      false premise and understates the true cost by one spawn per call, at
      both call sites (`apply.py::_lazy_repo_root` and this module's
      `root = repo_root or resolve_repo_root()`). Reported, not re-baselined:
      the resolver's behaviour is deliberately unchanged.
      Same public `Optional[Path] -> Optional[Path]` signature, so this is a
      straight import substitution — no caller-visible behavior change (no
      test asserted anything about the subprocess mechanics itself, only the
      black-box `Optional[Path]` contract).
    - `d-claim-plan` (Convert #2's original Step 2.4 predicate-scoping
      directive, `wsc-coverage-gate-runner.py claim-plan <slug>`) is
      SUPERSEDED by `directives_lessons_plan.py`'s `d-claim-plan-execution-
      lock` — identical underlying CLI call, richer sibling (adds
      `d-stamp-plan-implemented` alongside it per C2a). Emitting both would
      double-dispatch the same idempotent claim-plan call under two ids;
      C3 keeps only the new name. `d-coverage-gate`'s `depends_on` is
      repointed onto `d-claim-plan-execution-lock` accordingly.
    - `d-close-archive-session` (Convert #2's original, `wsc-close.py
      archive-session --sid <sid>`) was SUPERSEDED by `directives_commit_
      tail.py`'s `d-archive-session-claim` — byte-identical CLI/args,
      `depends_on` repointed onto the new tail directive. Same
      double-dispatch rationale as above. That superseding directive was
      itself later removed from the assembly (2026-07-28) — see the Step
      3/3.5/3.6 call site below for why, and `directives_commit_tail.py`'s
      Step 3.5 section for the builder's removal.
    - `d-close-tail-args` / `d-tail` (Convert #2's originals) are SUPERSEDED
      by `directives_commit_tail.py`'s `build_close_tail_args_directive`
      (same id, richer implementation) / `build_wsc_tail_directive` (id
      RENAMED to `d-run-wsc-tail` — the census's own name for the Step 3
      keystone call, carrying the 5-way exit ladder this module's docstring
      names). **Known consequence, flagged rather than silently absorbed:**
      the PRE-EXISTING `coordinator_core/workstream_complete/
      test_workstream_complete.py` (authored for Convert #2, not owned by
      any chunk in this plan's file-overlap table) hardcodes two
      `"d-tail" in ids` assertions and one closed-set `_KNOWN_CLIS` check
      that enumerates every directive `.cli` against `{wsc-coverage-gate-
      runner.py, check-workstream-complete-deletion-blocks.py, wsc-close.py,
      wsc-tail.py}` — no wiring of C2a-i's actual output can keep that
      specific test green, since `_KNOWN_CLIS` is a closed 4-entry set and
      ANY additional directive (even one gated correctly) fails it. This is
      a genuine plan gap (no chunk in the overlap table owns updating or
      retiring that file) surfaced to the EM in this chunk's return report,
      not something this module works around.
    - `directives_review.py`'s `build_chain_coverage_gate_directive` /
      `build_write_review_trail_directive` are deliberately NOT wired here —
      both would emit a byte-identical `wsc-coverage-gate-runner.py` call to
      this module's own pre-existing `d-coverage-gate`/`d-write-trail`
      under an alternate id, which is exactly the "reconcile the id here...
      C3's assembly-seam concern" case that module's own Negative-spec
      names. Keeping the pre-existing ids avoids a duplicate CLI dispatch
      pair.
    - Several CONSUMES_MANIFEST members genuinely cannot fire under
      `test_workstream_complete_contract.py`'s (C1) synthetic `tmp_path`
      sweep — `coordinator-lesson-add`/`coordinator-queue-append` need
      caller-supplied `decisions["lessons"]` the sweep never sets;
      `archive-stamp-cli`/`coordinator-harvest-deferrals` need a REAL
      `docs/plans/<slug>.md` file on disk (the submodule's own
      `resolve_governing_plan` deliberately verifies existence rather than
      trusting a bare slug — the module's own "do NOT invent a plan to
      reconcile against" negative-spec); `coordinator-complete-entry.py`/
      `reconcile-completion-commits.py`/`coordinator-fold-execution-record`
      need a real `archive/` or `docs/project-tracker.md` (or a real
      `state/subagent-share/` sidecar dir); `regenerate-orientation-cache`
      needs a caller-decided pinboard note; `freeze-review-diff.py`/
      `fan-out-integrator.py`/`scan_unresolved_ubt_records.py`/
      `classify-dispatch-shape.py` need caller-resolved review-partition/
      UBT/plan-file facts the sweep's fixed `_rich_decisions` payload does
      not carry. C1's own module docstring (its "Coverage caveat" section)
      names this exact class of gap as legitimate, expected residual red —
      "either widen the sweep... or add a named dispatched-worker-only
      exception" — neither of which is this chunk's file scope. This module
      does NOT fabricate synthetic decision values to force these dead
      under the sweep; that would be modeling a fact this engine cannot
      verify from real disk/caller state.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, NamedTuple, Optional

from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.ops import list_review_trail_records
from coordinator_core.ops.session_commits import (
    resolve_session_commits as _resolve_session_commits_primitive,
)
from coordinator_core.ops.review_brightline_gate import classify_surface
from coordinator_core.ops.review_brightline_gate import _is_noise_path  # C5: code_loc noise exclusion, same predicate as C1
from coordinator_core.ops.review_brightline_gate import _is_prose_bearing_path  # 2026-08-20: code_loc stops counting prose, same predicate as the brightline gate's mandate arms
from coordinator_core.coverage import _is_planning_artifact_path  # review finding P2: planning-artifact LOC de-weight
from coordinator_core.coverage import (  # 2026-08-12: numstat rename-row resolution, shared with review_brightline_gate.py (see this module's own site below)
    _REVIEW_SCALE_BARE_RENAME_RE,
    _REVIEW_SCALE_BRACED_RENAME_RE,
    _resolve_numstat_row_path,
)
from coordinator_core.ops.review_brightline_gate import _PLANNING_LOC_WEIGHT  # review finding P2: same de-weight, same constant

from coordinator_core.contract.decision_object.envelope import build_envelope, emit
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
    build_untrusted_gate_judgment_point,
    partition_reportable,
)
from coordinator_core.dag import CONTINUATION_EDGE_KINDS
from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.ceremony.wsc_disposition import (
    LEGACY_PREDECESSOR_CONSUMED,
    MEMO_PREDECESSOR,
    PREDECESSOR_CONSUMED,
    SINGLE_SESSION,
    canonicalize,
)
from coordinator_core.ops.fleet._common import handoff_archive_dest
from coordinator_core.pickup_assemble import compute_repo_identity_gate  # C2: foreign-repo gate
from coordinator_core.pickup_assemble import resolve_repo_root  # AC8: NOT zero-spawn — runs `git rev-parse --show-toplevel` via `_run_git`, one subprocess spawn per resolution
from coordinator_core.resolution.facade import resolve_operator_config

from coordinator_core.workstream_complete import completion_verdict as _completion_verdict
from coordinator_core.workstream_complete import directives_commit_tail
from coordinator_core.workstream_complete import directives_completion
from coordinator_core.workstream_complete import directives_lessons_plan
from coordinator_core.workstream_complete import directives_memo_lifecycle
from coordinator_core.workstream_complete import directives_review
from coordinator_core.workstream_complete import directives_session_hygiene
from coordinator_core.workstream_complete import directives_spine_worklist
from coordinator_core.workstream_complete import judgments as _judgments

EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3


class TransportFailure(Exception):
    """Raised for a resolution failure this module cannot compute past —
    no enclosing git worktree, or the sibling `wsc-session-disposition.py`
    bin script could not be located/loaded. Caught at the CLI boundary
    (`main`) and degraded to `EXIT_TRANSPORT_FAIL`, matching
    `pickup_assemble`'s own transport-failure contract."""


#: The full set of on-disk CLIs this module's `directives[]` may name
#: (AC2/AC15c's phantom-verb guard). See the module docstring's "Consumes
#: manifest" section for which submodule contributes each entry, and its
#: Negative-spec for why several members are legitimately unreachable under
#: `test_workstream_complete_contract.py`'s synthetic sweep today.
CONSUMES_MANIFEST: tuple[str, ...] = (
    "wsc-coverage-gate-runner",
    "check-workstream-complete-deletion-blocks",
    "wsc-close",
    "wsc-tail",
    "coordinator-lesson-add",
    "coordinator-queue-append",
    "archive-stamp-cli",
    "coordinator-harvest-deferrals",
    "coordinator-complete-entry",
    "reconcile-completion-commits",
    "coordinator-fold-execution-record",
    "regenerate-orientation-cache",
    "check-machine-local-regeneratability",
    "review-brightline-gate",
    "freeze-review-diff",
    "fan-out-integrator",
    "scan_unresolved_ubt_records",
    "classify-dispatch-shape",
    "session-claim-cli",
    "emit-cadence",
)


#: C2 (docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-being-
#: questions.md): the five wsc judgment points the plan's Problem/Anti-scope
#: sections name as NOT gate-nothing -- each builds `resolves` through a
#: resolver (`review_partition_resolves_ids`, `lesson_capture_resolves_ids`,
#: `memo_flip_resolves_ids`) that returns real directive ids once its
#: `decisions` slice is populated, and `[]` only when that slice is empty,
#: deliberately ("an honest 'this dispatches nothing', not a phantom id" --
#: each resolver's own docstring). `partition_reportable` cannot distinguish
#: "structurally gates nothing" from "this one call's decisions happened to
#: leave the resolver's slice empty" -- classifying these five by a single
#: observation would reintroduce the over-count the plan's Problem section
#: documents. Mirrors the identical exemption in
#: `contract/decision_object/tests/test_reportable_partition.py`'s own
#: `_RESOLVER_BACKED_OUT_OF_SCOPE_IDS` (C1's census harness) -- same five
#: ids, same rationale, not independently derived here.
_RESOLVER_BACKED_OUT_OF_SCOPE_IDS: frozenset[str] = frozenset(
    {
        "review-partition-strategy",
        "review-dispatch-vehicle-choice",
        "reviewer-count-on-oracle-disagreement",
        "lesson-worth-capturing",
        "memo-resolution-attribution",
    }
)


#: The free-value `decisions` keys THIS module's own `brief()` body reads
#: directly, rather than delegating to a `directives_*` builder. Same role as
#: a submodule's `FREE_VALUE_KEYS` and named identically so the AC3 one-oracle
#: rule covers this module too — a key read here but absent from this tuple is
#: a key no caller can discover from the template, which is the whole defect
#: `decisions_template` exists to close.
#:
#: `review_partition` is the load-bearing member: it carries the review slice
#: map, and its absence is exactly what left the `freeze-review-diff`
#: directives blocked and hand-invoked in the 2026-07-29 run this plan's
#: Problem section reconstructs. A template that omitted it would have
#: reproduced the motivating failure while claiming to fix it.
FREE_VALUE_KEYS: tuple[str, ...] = (
    "classify_dispatch_plan_file",
    "code_loc",
    "commit_count",
    "commit_count_scope",
    "executor_dispatched",
    "flags",
    "gross_loc",
    "msg_file",
    "orientation_cache_exists",
    "pinboard_note",
    "review_partition",
    "scratch_candidates",
    "shared_schema_touched",
    "surface_count",
    "ubt_check",
    "unattributable_files",
)


#: Union source for `build_decisions_template`'s free-value-key half (AC2) —
#: each `directives_*` submodule's OWN `FREE_VALUE_KEYS` constant (AC3),
#: imported here rather than hand-copied, plus this module's own (above).
#: `directives_review` contributes an empty tuple (see its own module comment
#: — no builder there reads a `decisions` mapping directly).
_FREE_VALUE_KEY_SOURCES: tuple[tuple[str, ...], ...] = (
    directives_completion.FREE_VALUE_KEYS,
    directives_lessons_plan.FREE_VALUE_KEYS,
    directives_memo_lifecycle.FREE_VALUE_KEYS,
    directives_commit_tail.FREE_VALUE_KEYS,
    directives_session_hygiene.FREE_VALUE_KEYS,
    directives_review.FREE_VALUE_KEYS,
    directives_spine_worklist.FREE_VALUE_KEYS,
    FREE_VALUE_KEYS,
)


def _all_free_value_keys() -> tuple[str, ...]:
    """Deduped union of every `directives_*` submodule's `FREE_VALUE_KEYS`,
    order-preserving (first-seen wins) — several keys are legitimately read
    by more than one submodule (e.g. `governing_plan_slug`), and a plain
    concatenation would emit the same template key twice."""
    seen: dict[str, None] = {}
    for keys in _FREE_VALUE_KEY_SOURCES:
        for key in keys:
            seen.setdefault(key, None)
    return tuple(seen.keys())


#: AC5 (docs/plans/2026-08-08-the-engine-asks-for-facts-it-already-holds.md,
#: chunk C4) — the declared template-key -> envelope-path mapping for every
#: free-value key `build_decisions_template` populates from data THIS SAME
#: `brief()` call already resolved, rather than leaving it `None`. A key not
#: listed here stays `None` unconditionally, even when some OTHER run might
#: resolve it in principle — the other ~26 free-value keys are an open PM
#: question, not this chunk (see this function's own Negative-spec).
DECISIONS_TEMPLATE_RESOLVED_KEY_ENVELOPE_PATHS: dict[str, str] = {
    "governing_plan_slug": "preflight.governing_plan_resolution.slug",
    "governing_plan_path": "preflight.governing_plan_resolution.path",
    "stage_paths": "gates.stage_paths_candidates",
}


def build_decisions_template(
    judgment_points: list[dict[str, Any]],
    resolved_free_values: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """AC1/AC2 (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md):
    `preflight.decisions_template` — the fillable `--decisions` skeleton a
    caller reads instead of reverse-engineering key names from this
    package's source.

    Pre-keyed two ways from data already in the SAME envelope, nothing
    computed that wasn't already known:
      - Every `judgment_points[].id` present in THIS call's `judgment_points`
        (AC1), valued `{"disposition": None, "options": [<dispositions[].
        value for that point>]}` — the exact set a caller must resolve to
        clear every open judgment point, with its legal answers enumerated.
      - Every free-value key the six `directives_*` submodules' builders
        read (AC2), valued `None` — the exact set `_all_free_value_keys`
        derives from each submodule's own `FREE_VALUE_KEYS` constant (AC3),
        never hand-copied here — UNLESS the key is one of
        `DECISIONS_TEMPLATE_RESOLVED_KEY_ENVELOPE_PATHS` (AC5) and
        `resolved_free_values` carries a non-`None` value for it, in which
        case the template pre-fills that ALREADY-COMPUTED value instead of
        `None`. This never forecloses a caller's own answer — the EM's
        subsequent `--decisions` call OVERRIDES whatever is pre-filled here;
        a pre-filled value only saves the caller from re-deriving a fact
        this same `brief()` run already resolved. `resolved_free_values` is
        deliberately narrow: only keys with a REAL resolver in this same run
        (not a guess) belong in it — see the module docstring's negative
        spec on this point.

    A free-value key never collides with a judgment-point id in this
    package's current vocabulary (free-value keys are `snake_case`;
    judgment-point ids are `kebab-case`, mostly `jp-`-prefixed) — this
    function does not defend against a collision beyond a plain `dict`
    assignment (last write wins), since none exists to defend against
    today.
    """
    template: dict[str, Any] = {}
    for jp in judgment_points:
        template[jp["id"]] = {
            "disposition": None,
            "options": [d["value"] for d in jp.get("dispositions", [])],
        }
    resolved_free_values = resolved_free_values or {}
    for key in _all_free_value_keys():
        if key in DECISIONS_TEMPLATE_RESOLVED_KEY_ENVELOPE_PATHS and resolved_free_values.get(key) is not None:
            template[key] = resolved_free_values[key]
        else:
            template[key] = None
    return template


_BIN_MODULE_CACHE: dict[str, ModuleType] = {}


def _load_bin_module(claude_klabauter_bin: str, filename: str, module_name: str) -> ModuleType:
    """Load a hyphenated `coordinator/bin/*.py` script as an importable
    module by file path — these scripts are not packages (hyphen in the
    filename bars a plain `import`), so `importlib.util` is the correct
    seam, not a `sys.path` insertion + `import` on a renamed copy.
    Cached per `(claude_klabauter_bin, filename)` — this module's `brief()` may be
    called repeatedly in one process (e.g. the conformance test), and
    re-executing the script module on every call would be wasteful and
    would break identity checks on any module-level singleton it defines.
    """
    cache_key = f"{claude_klabauter_bin}:{filename}"
    cached = _BIN_MODULE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    script_path = Path(claude_klabauter_bin) / filename
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise TransportFailure(f"could not build an import spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except FileNotFoundError as exc:
        raise TransportFailure(f"{script_path} not found: {exc}") from exc
    _BIN_MODULE_CACHE[cache_key] = module
    return module


def _resolve_claude_klabauter_bin() -> str:
    try:
        config = resolve_operator_config()
    except Exception as exc:  # noqa: BLE001 - any operator-config corruption is a transport failure here
        raise TransportFailure(f"could not resolve operator config: {exc}") from exc
    return config["claude_klabauter_bin"]


def _load_session_disposition_module() -> ModuleType:
    claude_klabauter_bin = _resolve_claude_klabauter_bin()
    return _load_bin_module(claude_klabauter_bin, "wsc-session-disposition.py", "wsc_session_disposition")


# ---------------------------------------------------------------------------
# gates.session_shape — Step 0 session-shape detection
# ---------------------------------------------------------------------------


class SessionShapeGate(NamedTuple):
    sid: str
    disposition: str
    consumed_handoff: str
    diagnostics: list[str]
    #: primary_consumed_handoff's own full sorted `matches` list from
    #: wsc-session-disposition.py — that bin script's OWN unanchored
    #: local-scan detector set (state/handoffs/, `(claimed_by|consumed_by):
    #: \s*<sid>`), NOT branch_resolution.py's anchored
    #: step0_evidence["consumed_handoff_paths"], a different computation.
    #: `consumed_handoff` (above) is kept as consumed_handoff_paths[0] (or
    #: "" when empty) for callers that have not migrated to the plural field.
    consumed_handoff_paths: tuple[str, ...]
    #: `wsc-session-disposition.py`'s structured detection record —
    #: `{"deciding_leg": <that module's DECIDING_LEGS member>,
    #: "detector_c_status": <"indeterminate"|"ambiguous"|"crash-recovery"|
    #: None>}`. ONE nested field rather than N scalars, matching
    #: `ConsumedHandoffCompletenessGate.elements`: "this leg has no such
    #: fact" is then a key-presence check, not a sentinel scalar, and the
    #: next fact the detector chain learns to report costs no further
    #: widening of this NamedTuple. Defaults to `None`, not `{}`, so every
    #: existing literal construction of this gate (tests, fixtures) keeps
    #: working WITHOUT every default-constructed gate in the process sharing
    #: one mutable dict object — an unpopulated record reads as "no
    #: structured detection", which `_session_shape_is_uncertain` treats as
    #: certain. Every read normalises with `dict(... or {})`, including the
    #: envelope emit, so the serialised shape is `{}` either way. When
    #: populated it is a plain `dict`, not a `MappingProxyType`: this gate is
    #: serialised into the envelope via `_asdict()` and must stay
    #: `json.dumps`-able. That constraint is on the STORED type, not on the
    #: default value.
    detection: Optional[dict[str, Any]] = None


def _detector_c_attribution_is_uncorroborated(detection: dict[str, Any]) -> bool:
    """True when a Detector C (crash-recovery) attribution rests on ZERO
    exact path matches — every matched `scope:` entry was a directory
    prefix.

    Narrower than `_session_shape_is_uncertain` on purpose, and the two are
    not interchangeable: that predicate decides whether to RAISE
    `jp-session-shape` (an alarm), this one decides whether to ADOPT the
    predecessor at all (a write). The `exact_match_count == 1 and
    scope_size >= 2` case is uncertain enough to flag but still carries a
    real path hit, so it keeps its attribution and only raises the point.
    Zero exact matches carries nothing: a `scope:` entry naming a bare
    package directory (`coordinator_core/`, `docs/decisions/`) matches any
    session that touched that tree at all, which is not evidence about
    WHOSE workstream this is.

    Spec backlink: state/bug-backlog/2026-08-19-jp-session-shape-resolution-
    is-inert-a-p-fe5b38e42795.yaml, which records why refusing the adoption
    is the correct shape of this fix rather than making `jp-session-shape`'s
    disposition load-bearing: the judgment point stays an alarm nobody has
    to answer correctly for the ceremony to be SAFE, and the damage path
    closes without a second way to resolve a shape.

    Negative-spec: this is NOT a liveness check and NOT a general
    predecessor validator. It reads one structured field on one detector
    leg. An `exact_match_count`-absent record (an older
    `wsc-session-disposition.py` that never computed it) returns False --
    presence-vs-absence selects the branch, exactly as
    `_session_shape_is_uncertain` documents for the same field, so a
    partially-updated tree degrades to today's behaviour rather than
    silently refusing every predecessor.
    """
    if detection.get("deciding_leg") != "detector-c":
        return False
    if detection.get("detector_c_status") != "crash-recovery":
        return False
    return detection.get("exact_match_count") == 0


def compute_session_shape_gate(repo_root: Path) -> SessionShapeGate:
    """Steps 0 of `/workstream-complete`: resolve this session's id and
    chain-terminal-vs-single-session disposition via the ported detector
    chain in `wsc-session-disposition.py`, folded into one gate fact."""
    mod = _load_session_disposition_module()
    sid = mod.resolve_session_id(repo_root)
    resolution = mod.resolve_disposition(repo_root, sid)
    disposition, consumed_handoff, diagnostics, consumed_handoff_paths = resolution
    detection = dict(getattr(resolution, "detection", None) or {})
    if consumed_handoff and _detector_c_attribution_is_uncorroborated(detection):
        diagnostics = list(diagnostics) + [
            "REFUSED: Detector C (crash-recovery) attributed this session to "
            f"{consumed_handoff} on {detection.get('matched_scope_entry_count')} of "
            f"{detection.get('scope_size')} scope entries with ZERO exact path matches "
            "-- every hit was a directory prefix. Falling back to single-session rather "
            "than adopting a predecessor on that evidence; nothing is stamped, "
            "ledger-appended, or filed against that handoff or its plan. Re-run after "
            "correcting the handoff's scope: if this session genuinely consumed it."
        ]
        disposition = SINGLE_SESSION
        consumed_handoff = ""
        consumed_handoff_paths = ()
    return SessionShapeGate(
        sid=sid,
        disposition=disposition,
        consumed_handoff=consumed_handoff,
        diagnostics=diagnostics,
        consumed_handoff_paths=tuple(consumed_handoff_paths),
        # `.detection` is an ATTRIBUTE on that module's tuple subclass, not a
        # fifth element — the unpack above stays four-wide on purpose (see
        # `DispositionResolution`). Bound once above, with a `getattr`
        # default, so an older copy of the bin script on a partially-updated
        # tree degrades to "no structured detection" rather than raising.
        # Emitted UNCHANGED even when the adoption above was refused: the
        # refusal moves `disposition`, never the evidence it was read from,
        # so `jp-session-shape` and the diagnostics still show what the
        # detector actually saw.
        detection=detection,
    )


def _session_shape_is_uncertain(detection: dict[str, Any]) -> bool:
    """True when the detector chain's own STRUCTURED verdict says this
    session's shape is not a settled fact.

    Reads `gates.session_shape.detection` — never `diagnostics`. The
    diagnostics are prose written for a human reader; keying a behavioural
    gate on their wording made this predicate silently stop firing the day
    Detector C's single-match NOTE was written without the word
    "indeterminate" in it, which is the whole reason this function no longer
    has a `diagnostics` parameter (DR-259's "notes never beat structure",
    applied one layer over).

    Two structurally uncertain cases:

      - the deciding leg reported `indeterminate` or `ambiguous` — a
        liveness-detection gap the assembler cannot settle from disk (the
        behaviour this predicate always INTENDED, now read from structure).
      - Detector C decided by crash-recovery scope intersection AND that
        attribution is coincidence-prone. Detector C's success path is the
        NORMAL crash-recovery route, so `deciding_leg == "detector-c"` alone
        over-fires on every ordinary resolution — this predicate is keyed
        ENTIRELY on `exact_match_count`, never on raw match COUNT
        (`matched_scope_entry_count`), because count is not a proxy for
        corroboration: two prefix hits are weaker evidence than one exact
        hit, not stronger, and — the 2026-08-06 second-pass fix — adding
        prefix hits alongside a lone exact hit must not SILENCE an already-
        weak attribution either. When `exact_match_count` is present:

          - `exact_match_count == 0` — every matched scope entry was a
            prefix hit, AT ANY COUNT. A `scope:` entry naming a package
            directory (`coordinator_core/`) matches any session that
            touched the engine at all; two or more such prefix hits do not
            corroborate each other, they compound the same weak signal (the
            example-market-data-repo live-false-positive this branch exists to
            catch: `matched_scope_entry_count=2`, both matches bare
            directory prefixes, zero exact hits).
          - `exact_match_count == 1` and `scope_size >= 2` — one exact hit
            against a multi-entry scope is weak WHETHER OR NOT prefix hits
            ride along with it (the near-neighbour miss this second pass
            closes: 1 exact + 2 prefix in a 7-entry scope is no more
            corroborated than 1 exact alone, and must flag identically).
          - `exact_match_count >= 2` — two or more exact path matches is
            real corroboration and stays quiet, regardless of accompanying
            prefix hits.
          - `exact_match_count == 1` and `scope_size == 1` — the narrowest,
            most specific attribution the detector can make (a `scope:`
            entry naming one file this session's own commit touched
            exactly) and stays quiet.

        `matched_scope_entry_count` plays NO role in this branch — it is
        read only in the stale-producer fallback below, a visibly separate
        branch, not folded into this one as a default.

        Negative-spec: raw `matched_scope_entry_count` is NEVER read as a
        proxy for corroboration on the live path — a gate of that shape
        (`!= 1` -> not uncertain, in either its `== 0` or `>= 2` guise) is
        exactly the bug this branch replaces twice over: first by letting
        an all-prefix multi-match read as settled, then by letting extra
        prefix hits silence an already-flagged single-exact match. Do not
        reintroduce a `matched_scope_entry_count` gate on this branch.

        `single_match_kind == "prefix"` is redundant on the live path above
        (a prefix-kind single match implies `exact_match_count == 0`,
        already caught by the first case) and is used only in the
        stale-producer fallback — it stays on the wire regardless, for its
        other existing consumers.

        shell-doc-ok: the backticked comparisons above are Python boolean
        expressions quoted from this module's own code, not shell version
        constraints.

    These four extra `detection` keys (`matched_scope_entry_count`,
    `scope_size`, `single_match_kind`, `exact_match_count`) are produced by
    `wsc-session-disposition.py`. When `exact_match_count` is present, it
    alone drives the branch above. When it is ABSENT — a stale copy of that
    bin script that still only reports `deciding_leg`/`detector_c_status`,
    or one that predates `exact_match_count` specifically — the branch
    falls back to the exact pre-`exact_match_count` logic
    (`matched_scope_entry_count == 1 and (scope_size >= 2 or
    single_match_kind == "prefix")`), replayed byte-for-byte via a visibly
    separate code path rather than a `.get`-with-default folded into the
    live rule. `exact_match_count` absent is NOT the same fact as
    `exact_match_count == 0`: presence-vs-absence, not value, selects the
    branch.

    This exact rule is DUPLICATED, not delegated, in
    `wsc-session-disposition.py`'s own `is_coincidence_prone_detection`
    (called from that module's `resolve_disposition` memo-preemption gate)
    — see that function's docstring for why a module-load delegation was
    tried and reverted. The two copies must be edited together.

    Negative-spec: does NOT read `gate.diagnostics`, and no substring match
    on detector prose exists anywhere in this module. The former
    `_UNCERTAIN_MARKERS` tuple was deleted rather than supplemented — left
    in place it would invite exactly the re-coupling this predicate exists
    to end.

    The first branch's `detector_c_status` reliance on `deciding_leg`
    being `"detector-c"` or `"none"` is now ENFORCED consumer-side, not
    merely trusted: `_detection()` in `wsc-session-disposition.py` only
    ever passes a non-`None` `detector_c_status` on those two legs (the
    `"none"` leg carries it whenever Detector C ran and returned
    `"indeterminate"`/`"ambiguous"`; the `"detector-c"` leg's own success
    path never carries those two values, since a status in
    `("indeterminate", "ambiguous")` never resolves to that leg — see
    `_detection`'s call sites). Do not simplify this gate back to a bare
    `detection.get("detector_c_status") in (...)` check: without the
    `deciding_leg` guard, a future producer change that leaks a stale
    `detector_c_status` onto `env-override`/`live-consume`/`archive` would
    silently mis-fire `jp-session-shape` on a resolution Detector C never
    touched.

    `deciding_leg == "memo-predecessor"` is a THIRD leg that legitimately
    carries Detector C's `detector_c_status`/match-facts fields onto this
    same `.detection` record — the memo leg rides Detector C's own
    diagnostics along as diagnostics-only even when the memo, not Detector
    C, decided the disposition (plan Execution Notes precedence contract).
    That is NOT the producer leak the `deciding_leg` guard above defends
    against: this predicate's first branch only matches `deciding_leg in
    ("detector-c", "none")`, so a `"memo-predecessor"` leg never enters
    either branch and this function always returns `False` for it, by
    construction, regardless of what `detector_c_status`/match-facts it
    carries. `memo-predecessor` is a settled fact once resolved — do not
    read the presence of `detector_c_status` on that leg as ambiguity."""
    if detection.get("deciding_leg") in ("detector-c", "none") and detection.get(
        "detector_c_status"
    ) in ("indeterminate", "ambiguous"):
        return True
    if detection.get("deciding_leg") != "detector-c":
        return False
    # SECOND COPY, deliberately not delegated: `wsc-session-disposition.py`'s
    # `is_coincidence_prone_detection` is the SAME rule, called from that
    # module's own `resolve_disposition` memo-preemption gate — see its
    # docstring. A module-load delegation here (`_load_session_disposition_
    # module()`) was tried and reverted: it forces `resolve_operator_config`
    # to run as a side effect of a pure predicate, which broke every test in
    # this file that constructs a `detection` dict by hand (no repo_root /
    # operator config in scope) and is a needless transport dependency for a
    # dict-in-bool-out function. If this rule ever changes, change BOTH
    # copies — this one, and `is_coincidence_prone_detection` in
    # `coordinator/bin/wsc-session-disposition.py`.
    #
    # `matched_scope_entry_count` is NOT a gate on this leg — that was the
    # 2026-08-06 miss: gating the second branch on `matched_count == 1`
    # let extra worthless prefix hits SILENCE an already-weak single-exact
    # attribution (1 exact + 2 prefix in a 7-entry scope fell through to
    # `matched_count != 1 -> False`, quiet, despite being no more corroborated
    # than the 1-exact-alone case that WAS flagged). The rule below is keyed
    # entirely on `exact_match_count`, which does not have that hole.
    if "exact_match_count" in detection:
        exact_count = detection.get("exact_match_count")
        if exact_count == 0:
            return True
        if exact_count == 1:
            return detection.get("scope_size", 0) >= 2
        # exact_count >= 2: two or more exact path matches is real
        # corroboration, regardless of how many prefix hits ride along.
        return False
    # Stale-producer fallback, visibly separate from the branch above, not
    # a default value folded into it: `exact_match_count` absent means an
    # older `wsc-session-disposition.py` that never computed it, so this
    # replays the exact pre-fix logic byte-for-byte rather than guessing.
    # `single_match_kind == "prefix"` earns its keep ONLY in this fallback
    # branch — on the live path above it is redundant (a prefix-kind single
    # match implies `exact_match_count == 0`, already caught) and was
    # dropped there; it stays on the wire for other consumers and for this
    # degradation path.
    if detection.get("matched_scope_entry_count") != 1:
        return False
    return (
        detection.get("scope_size", 0) >= 2
        or detection.get("single_match_kind") == "prefix"
    )


#: The judgment-point id both `build_session_shape_judgment_point` (the
#: construction site) and `_session_shape_disposition_from_decisions` (the
#: AC3 recompute) read/write — named once so a caller-supplied answer's key
#: and the point's own `id=` can never drift apart. Read through this NAME,
#: never a repeated string literal: `test_every_decisions_key_read_anywhere_
#: in_the_package_is_discoverable_from_the_template`'s AST scan flags a
#: literal `decisions.get("...")`/`decisions["..."]` constant as a
#: FREE_VALUE_KEYS candidate, which `jp-session-shape` is not — it is already
#: discoverable via `judgment_points[].id` in `preflight.decisions_template`
#: whenever the point is actually raised, the same "answer a judgment point"
#: contract `ceremony_common/apply_halt.py`'s own `decisions.get(dep)` (a
#: variable, not a literal) already reads under.
_JP_SESSION_SHAPE_ID = "jp-session-shape"


def _session_shape_disposition_from_decisions(decisions: Mapping[str, Any]) -> Optional[str]:
    """AC3 (docs/plans/2026-08-20-wsc-identity-gates-key-on-the-deliverable.md,
    item 2 / state/bug-backlog/2026-08-19-jp-session-shape-resolution-is-
    inert-a-p-fe5b38e42795.yaml): when the caller has already answered
    `jp-session-shape`, `gates.session_shape.disposition` should read that
    resolved value on a re-`brief`, not silently replay the detector chain's
    original verdict — today all four dispositions carry `resolves: []`, so
    the decision is honoured by `wsc-tail` but invisible here, leaving
    "accepted" and "discarded" indistinguishable to the operator deciding
    whether to retry.

    Reads `decisions["jp-session-shape"]["disposition"]` only — the same key
    `build_decisions_template` pre-keys for this judgment point — and
    canonicalizes it through `wsc_disposition.canonicalize` (both spellings
    permanently recognised, see that module). Returns `None` on anything
    absent, non-`dict`, non-`str`, empty, or unrecognised, so a malformed or
    legacy-typo'd value never corrupts the emitted gate: the detector's own
    verdict is kept unchanged in that case.

    Deliberately does NOT attach a `resolves` set to the judgment point's
    own dispositions (see this chunk's dispatch brief) — `d-coverage-gate`,
    the directive these dispositions used to resolve, was removed under
    K-001 (state/kill-ledger.md, LANDED `55e64be13`). This function only
    changes what `brief` REPORTS back through `gates.session_shape`, never
    what `directives[]` dispatches — the mutation path stays exactly where
    `wsc-tail` already reads the decision from, unchanged by this chunk.
    """
    entry = decisions.get(_JP_SESSION_SHAPE_ID)
    if not isinstance(entry, dict):
        return None
    value = entry.get("disposition")
    if not isinstance(value, str) or not value:
        return None
    try:
        return canonicalize(value)
    except ValueError:
        return None


def build_session_shape_judgment_point(gate: SessionShapeGate) -> Optional[dict[str, Any]]:
    """Surfaces a genuine session-shape uncertainty as an untrusted-gate
    judgment point — `wsc-session-disposition.py`'s structured detection
    record reports either an INDETERMINATE/AMBIGUOUS detector result, or a
    Detector C crash-recovery attribution whose scope-match corroboration is
    too thin to trust (zero exact matches at any match count — every hit
    was a directory-prefix match, not an exact one — or exactly one exact
    match in a scope of two or more entries) — both disk-level facts this
    assembler has no further evidence to resolve; recommending a
    disposition here would be exactly the false-confidence the
    untrusted-gate constructor exists to forbid (no `recommendation`
    parameter). Detector C's ordinary corroborated resolutions — two or
    more exact matches, or a single exact match in a single-entry scope —
    are NOT flagged — see `_session_shape_is_uncertain` for the authoritative
    rule."""
    if not _session_shape_is_uncertain(dict(gate.detection or {})):
        return None
    return build_untrusted_gate_judgment_point(
        id=_JP_SESSION_SHAPE_ID,
        question=(
            f"Session-shape resolved to {gate.disposition!r}, but the detector chain "
            "flagged an unresolved case below — is that resolution actually correct?"
        ),
        dispositions=[
            # AC2b (historical): the canonical and legacy spellings used to
            # carry IDENTICAL `resolves=["d-coverage-gate"]` lists —
            # ceremony_common/apply_halt._disposition_resolves_directive's
            # ordinary value-match cleared d-coverage-gate for either
            # spelling an EM typed. `d-coverage-gate` itself was removed
            # (K-001, state/kill-ledger.md), so every disposition here now
            # resolves nothing.
            build_disposition(PREDECESSOR_CONSUMED, resolves=[]),
            build_disposition(LEGACY_PREDECESSOR_CONSUMED, resolves=[]),
            build_disposition(SINGLE_SESSION, resolves=[]),
            build_disposition(MEMO_PREDECESSOR, resolves=[]),
        ],
        evidence="gates.session_shape.detection",
        reason=(
            "wsc-session-disposition.py's structured detection record reports either an "
            "INDETERMINATE/AMBIGUOUS detector result, or a Detector C (crash-recovery) "
            "attribution resolved from a single matched scope entry whose breadth is weak — "
            "the matched baton's scope has other, uncorroborated entries, or the single match "
            "itself was a prefix rather than an exact path. Neither is a fact this assembler "
            "can settle from disk; gates.session_shape.diagnostics carries the human-readable "
            "detail."
        ),
    )


# ---------------------------------------------------------------------------
# directives[] — the mechanical mutation spine (Convert #2's original
# Step 2.4/2.9/2.67/3 directives, minus the two SUPERSEDED ids — see
# module Negative-spec)
# ---------------------------------------------------------------------------


def _directive(
    id_: str,
    cli: str,
    args: list[str],
    depends_on: Any = None,
    already_satisfied: bool = False,
    advisory: bool = False,
) -> dict[str, Any]:
    """`advisory=True` marks this directive per `apply_base.execute_
    directives`'s own advisory-marker contract: a raising handler is
    recorded and surfaced, never taken to `APPLY_EXIT_PARTIAL_MUTATION`,
    and never reaches compensation. Omitting it (every pre-existing
    caller) reproduces today's byte-identical directive dict — no
    `"advisory"` key at all, not a `False`-valued one, so a caller doing
    its own `directive.keys()` shape check sees nothing new either."""
    directive = {
        "id": id_,
        "cli": cli,
        "args": args,
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
    }
    if advisory:
        directive["advisory"] = True
    return directive


def _build_legacy_coverage_and_trail_directives(
    gate: SessionShapeGate,
    decisions: dict[str, Any],
    plan_claim_directives: list[dict[str, Any]],
    repo_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """The pre-existing (Convert #2) `d-write-trail` directive builder.

    `d-coverage-gate` — REMOVED (K-001, state/kill-ledger.md), NOT live, and
    built by nothing in this repo. This function emits `d-write-trail` only.
    It formerly also emitted `d-coverage-gate` (`wsc-coverage-gate-runner
    coverage-gate --from-handoff <consumed_handoff>`, gated on
    `gate.disposition == PREDECESSOR_CONSUMED and gate.consumed_handoff`);
    the DAG walk behind that verdict cost ~150-180s per close.
    (Wording fixed 2026-08-19: this paragraph opened "the LIVE chain-end
    coverage-verdict directive" and only said "was removed" four lines
    later, which read as a live directive to anyone skimming the first
    line — it sent a peer session hunting a construction site that has not
    existed since K-001.) The gate-verdict memo machinery it used
    (`directives_review.gate_memo_hit`/`record_gate_verdict_if_passed`) was
    trimmed alongside it; only `d-run-review-brightline-gate`'s memo entry
    survives (brightline itself is unaffected by this cut and still mints
    chain-ancestry waivers via its own `--mint-chain-waivers` subprocess —
    see `coordinator/bin/wsc-coverage-gate-runner.py::cmd_brightline_gate`).
    `gate`/`decisions` are still accepted (`gate` was this function's own
    coverage-directive gating input; `decisions` is a write-trail-directives
    passthrough) so the signature stays stable for its one caller.

    `repo_root`, `plan_claim_directives` — kept for signature stability;
    `plan_claim_directives` was never read by the coverage-directive logic
    (see the ordering note below) and `d-write-trail` does not consult a
    verdict memo of its own.

    `d-write-trail`'s `wsc-coverage-gate-runner` subcommand (`write-trail`)
    has no positional slot for a `{<producer-id>.landed}`/
    `{<producer-id>.entry_path}` dependency token (both subparsers define
    ONLY `--flag`-form arguments — see
    `coordinator/bin/wsc-coverage-gate-runner.py::_build_parser`), so it
    never carried a real block-until-landed dependency on
    `plan_claim_directives` either; that was true before this cut and is
    unchanged by it."""
    directives: list[dict[str, Any]] = []

    directives.extend(
        build_write_trail_directives(decisions.get("review"), session_id=gate.sid, repo_root=repo_root)
    )

    return directives


_REVIEW_TRAIL_REQUIRED_FIELDS = ("sha_range", "reviewer", "scope", "verdict", "diff_loc")


def _build_write_trail_args(review: dict[str, Any]) -> list[str]:
    args = [
        "write-trail",
        "--sha-range", str(review["sha_range"]),
        "--reviewer", str(review["reviewer"]),
        "--scope", str(review["scope"]),
        "--verdict", str(review["verdict"]),
        "--diff-loc", str(review["diff_loc"]),
    ]
    if review.get("scope_kind"):
        args += ["--scope-kind", str(review["scope_kind"])]
    if review.get("reviewer_evidence"):
        args += ["--reviewer-evidence", str(review["reviewer_evidence"])]
    return args


def build_write_trail_directives(
    review: Any, *, session_id: str = "", repo_root: Optional[Path] = None
) -> list[dict[str, Any]]:
    """`decisions["review"]` -> zero, one, or many `d-write-trail*`
    directives, each a mechanical `wsc-coverage-gate-runner.py write-trail`
    call over `coordinator_core.ops.review_trail_write`'s single-record
    write path (`review_trail.write` writes exactly ONE record per
    invocation — see that module's own docstring; storage already supports
    N records, one per call, so N directives is the whole fix).

    Two accepted shapes, BOTH read from the SAME `decisions["review"]` key
    (docs backlink: a per-slice, brightline-mandated review can produce N
    distinct `(sha_range, reviewer, scope, verdict, diff_loc)` tuples that
    the pre-existing single-object shape could not express — see this
    module's own review-trail-partition fix):

    - a single `dict` (the pre-existing, still-fully-supported shape):
      identical to today, byte-for-byte — one `d-write-trail` directive
      when all five required fields are present and non-empty, none
      otherwise. Every existing caller/test that supplies a dict keeps
      working unchanged.
    - a `list[dict]` (additive): one `d-write-trail-<index>` directive per
      list entry whose own five required fields are all present and
      non-empty — `<index>` is the entry's position in the ORIGINAL list
      (not a count of qualifying entries), so an incomplete entry never
      shifts a later entry's id. An entry missing a required field
      contributes NO directive (mirrors the single-dict "name it, don't
      guess" convention) — it is silently dropped from `directives[]`,
      never dispatched with a partial/guessed value. This is a build-time
      decision only: at APPLY time each directive dispatches
      independently through `_execute_directives`'s per-directive halt
      contract, so one slice's dispatch failure (e.g. a foreign-session
      range refusal) never blocks or poisons any sibling slice's own
      write.

    `None`/`{}`/`[]`/any other falsy value: no directives (today's
    behavior for an absent/empty `review` key, preserved for both shapes).

    Calls `directives_commit_tail.validate_review_shape` first -- the SAME
    shared validator `build_close_tail_args_directive` calls, so the two
    independent reader sites cannot diverge (state/bug-backlog/2026-08-14-
    wsc-apply-accepts-an-unconsumed-decision-debea052f8c5.yaml). RAISES
    `ValueError` on a shape outside {falsy | dict of recognized keys | list
    of such dicts} -- a caller-supplied `review` nested one key deeper than
    either accepted shape now fails loud here instead of silently
    contributing zero directives.

    `session_id`/`repo_root` (C4, AC7, docs/plans/2026-08-15-the-ceremony-
    tail-stops-lying-about-why-it-failed.md): when BOTH are supplied,
    consults the gate verdict memo (READ-ONLY, via `directives_review.
    gate_memo_hit`) keyed on `(session_id, sha_range)` -- the SAME identity
    C3 gave the on-disk trail record itself -- and sets `already_satisfied
    =True` on a hit, so a reconcile-and-re-run whose trail record already
    exists no longer re-fires the write. Omitting either (every pre-C4
    caller) reproduces today's byte-identical directives -- no memo lookup,
    `already_satisfied` stays its `_directive` default of `False`. This
    function NEVER WRITES the memo itself, same division as the sibling
    `d-coverage-gate` memo above: the write happens exactly once, from
    `apply.py::_execute_directives`'s `directives_review.
    record_gate_verdict_if_passed`, after the directive actually dispatched
    and exited 0 this pass.
    """
    directives_commit_tail.validate_review_shape(review)
    if not review:
        return []
    if isinstance(review, dict):
        if not all(review.get(k) not in (None, "") for k in _REVIEW_TRAIL_REQUIRED_FIELDS):
            return []
        directive = _directive("d-write-trail", "wsc-coverage-gate-runner", _build_write_trail_args(review))
        _apply_write_trail_gate_memo(directive, session_id, review["sha_range"], repo_root)
        return [directive]
    directives: list[dict[str, Any]] = []
    for index, entry in enumerate(review):
        if not isinstance(entry, dict):
            continue
        if not all(entry.get(k) not in (None, "") for k in _REVIEW_TRAIL_REQUIRED_FIELDS):
            continue
        directive = _directive(
            f"d-write-trail-{index}", "wsc-coverage-gate-runner", _build_write_trail_args(entry)
        )
        _apply_write_trail_gate_memo(directive, session_id, entry["sha_range"], repo_root)
        directives.append(directive)
    return directives


def _apply_write_trail_gate_memo(
    directive: dict[str, Any], session_id: str, sha_range: Any, repo_root: Optional[Path]
) -> None:
    """C4 (AC7): shared opt-in for both `build_write_trail_directives`
    shapes. Stamps `_gate_memo_key_parts` on `directive` unconditionally
    when both inputs are present -- so `directives_review.
    record_gate_verdict_if_passed` can record under the SAME `(session_id,
    sha_range)` key this function just checked -- and additionally sets
    `already_satisfied=True` on a hit. The key is `(session_id, sha_range)`
    regardless of the directive's own id (`d-write-trail` vs the indexed
    `d-write-trail-<n>` shape): the underlying trail-record identity C3
    established does not vary with directive index, only the CLI dispatch
    does. No-op (directive left exactly as `_directive` built it) when
    either `session_id` or `repo_root` is falsy/`None` -- every pre-C4
    caller of `build_write_trail_directives` supplies neither."""
    if not session_id or repo_root is None:
        return
    key_parts = [session_id, str(sha_range)]
    directive["_gate_memo_key_parts"] = key_parts
    # Fixed gate-id tag (matches `directives_review._WRITE_TRAIL_DIRECTIVE_ID_PREFIX`);
    # see this function's docstring for why it is not this directive's own id.
    if directives_review.gate_memo_hit(repo_root, "d-write-trail", *key_parts):
        directive["already_satisfied"] = True


def build_deletion_blocks_check_directive(msg_file: Optional[str]) -> Optional[dict[str, Any]]:
    """Step 2.67's deletion-blocks check against the prepared commit-
    message file (`d-deletion-blocks`). The CLI's positional
    `<prepared-commit-msg-file>` is REQUIRED — its own usage line has no
    optional form — so an absent `msg_file` must contribute NO directive,
    never one with an empty `args` list that would fail with a usage
    error (exit 2) on every dispatch. Mirrors `directives_commit_tail.
    build_release_plan_claim_directive`'s "absent input, no directive"
    convention rather than inventing a placeholder path. The deletion-
    blocks step is optional — a plan-less/msg_file-less session simply
    skips it, exactly like a governing-plan-less session skips the
    release-plan-claim directive (2026-07-27 finding: this directive was
    previously emitted unconditionally with `args: []` whenever
    `decisions["msg_file"]` was absent, failing with a usage error on
    every real `apply` run that didn't happen to supply it)."""
    if not msg_file:
        return None
    return _directive(
        "d-deletion-blocks",
        "check-workstream-complete-deletion-blocks",
        [msg_file],
    )


def _resolve_commit_message_authoring_fields(decisions: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Single resolution of `d-run-wsc-tail`'s commit subject/prose text
    (state/bug-backlog/2026-07-28-workstream-complete-apply-re-scaffolds-t-
    e925d597e0af.yaml: this directive used to carry only `--sid` because
    nothing read the EM's actual answer to the `commit-message-authoring`
    judgment point). A flat `decisions["subject"]`/`["prose"]` key (the
    pre-existing, still-honored contract) takes precedence when the caller
    supplies it directly; otherwise falls back to the same text carried
    alongside `commit-message-authoring`'s own resolved disposition
    (`decisions["commit-message-authoring"]["subject"/"prose"]`) — the
    shape an EM naturally produces when resolving that judgment point.
    Used by BOTH `build_directives` (to backfill `effective_decisions`
    before building `d-run-wsc-tail`) and `brief()` (to decide whether the
    synthetic `jp-commit-subject-missing` halt below must fire) — the one
    place this precedence is decided."""
    jp_decision = decisions.get("commit-message-authoring")
    jp_fields = jp_decision if isinstance(jp_decision, dict) else {}
    subject = decisions.get("subject") or jp_fields.get("subject")
    prose = decisions.get("prose") or jp_fields.get("prose")
    return subject, prose


def build_directives(
    gate: SessionShapeGate,
    decisions: dict[str, Any],
    repo_root: Path,
    governing_plan: Optional[directives_lessons_plan.GoverningPlan] = None,
    session_start_time: Any = None,
    partition_mandatory: bool = False,
) -> list[dict[str, Any]]:
    """Assembles the FULL mechanical directive spine — Convert #2's
    original Step 2.4/2.9/2.67/3 core plus every submodule's contribution
    (C2a-C2i), superseding the two ids the Negative-spec names. `decisions`
    plays the same "caller-supplied input this module cannot read off disk"
    role every submodule already documents for its own slice — an absent
    key contributes no directive (or a partial one, per each builder's own
    "name it, don't drop it" convention), never a guessed value.

    `governing_plan` is resolved once by the caller (`brief()`, which also
    needs the resolution's `source` for `preflight.governing_plan_resolution`)
    and threaded through here rather than re-resolved — re-resolving it
    locally would re-read and re-parse the consumed handoff's frontmatter a
    second time per `brief()` call for a value that cannot differ, since
    both call sites pass identical arguments.

    `session_start_time` (2026-08-08, docs/plans/2026-08-08-the-second-
    close-re-measures-the-first-c.md): same "resolved once by the caller,
    threaded through" pattern as `governing_plan` — `brief()` already
    resolves it via `directives_memo_lifecycle.resolve_session_start_time`
    for `_measure_session_review_scale_inputs`, so this parameter reuses
    that SAME resolution rather than re-deriving it a second time. Used
    ONLY to floor the mid-chain review-brightline-gate directive's range at
    this session's own last-reviewed sha (`_resolve_review_brightline_
    floor_kwargs` below) — `None` (every existing caller, including every
    test that constructs `build_directives` directly) reproduces today's
    exact `["--session-id", sid]` call, unchanged.

    `partition_mandatory` (D, cross-repo/inbox/2026-08-15-example-retrieval-repo-em-
    wsc-review-trail-skips-silently.md): `brief()`'s own resolved
    `decide_review_scale(...).partition_mandatory`, threaded straight to
    `directives_commit_tail.build_wsc_tail_directive` below — a DEDICATED
    parameter, deliberately never a `decisions[...]` key (that would make
    it look caller-suppliable and decisions-template-discoverable; it is
    neither, it is this module's own resolved verdict). Defaults `False`,
    reproducing every existing caller's argv byte-identically.
    """
    directives: list[dict[str, Any]] = []

    # -- Step 2.4 (C2a): governing-plan claim + stamp + deferral harvest --
    plan_claim_directives = directives_lessons_plan.build_plan_claim_and_stamp_directives(governing_plan)
    directives.extend(plan_claim_directives)

    # Every OTHER consumer below that reads `decisions.get("governing_plan_
    # slug")` directly (directives_completion.build_directives's completion-
    # entry metadata + run-report-sidecar plan_slug gate,
    # directives_commit_tail.build_wsc_tail_directive's commit-tail arg,
    # directives_commit_tail.build_release_plan_claim_directive) would go
    # blind exactly like `d-release-plan-claim` did whenever the slug
    # resolved via the handoff-frontmatter or fixed-fallback legs rather
    # than a caller-supplied `decisions` key (2026-07-27 finding: the claim
    # was taken and never released under handoff-frontmatter resolution).
    # Backfilling ONE shared `effective_decisions` copy here — only when the
    # caller didn't already supply an explicit slug — means every
    # downstream consumer keeps reading a plain `decisions.get(...)` call
    # and automatically agrees with `governing_plan` without each one
    # re-deriving the resolution itself.
    effective_decisions = dict(decisions)
    if governing_plan is not None and not effective_decisions.get("governing_plan_slug"):
        effective_decisions["governing_plan_slug"] = governing_plan.slug

    # `d-run-wsc-tail`'s subject/prose text, same "backfill effective_
    # decisions, never each consumer re-deriving it" pattern as the
    # governing-plan-slug backfill just above — see
    # `_resolve_commit_message_authoring_fields`'s own docstring.
    resolved_subject, resolved_prose = _resolve_commit_message_authoring_fields(decisions)
    if resolved_subject and not effective_decisions.get("subject"):
        effective_decisions["subject"] = resolved_subject
    if resolved_prose and not effective_decisions.get("prose"):
        effective_decisions["prose"] = resolved_prose

    # -- Convert #2 original: d-coverage-gate / d-write-trail, repointed --
    directives.extend(
        _build_legacy_coverage_and_trail_directives(gate, decisions, plan_claim_directives, repo_root=repo_root)
    )

    # -- Step 2.4b (C2a): deferral-harvest sweep --
    harvest_targets = [governing_plan] if governing_plan else []
    for slug in decisions.get("additional_governing_plan_slugs", []) or []:
        for dirname in directives_lessons_plan._GOVERNING_PLAN_GLOB_DIRS:  # noqa: SLF001 - same-package sibling constant
            candidate = repo_root / dirname / f"{slug}.md"
            if candidate.is_file():
                harvest_targets.append(directives_lessons_plan.GoverningPlan(slug=slug, path=candidate))
                break
    directives.extend(directives_lessons_plan.build_deferral_harvest_directives(harvest_targets))

    # -- Step 1/1.2 (C2a): lesson capture --
    directives.extend(directives_lessons_plan.build_lesson_capture_directives(decisions))

    # -- Step 2.6/2.6.7/2.6.8/2.6b (C2b): completion-entry cluster --
    directives.extend(
        directives_completion.build_directives(
            sid=gate.sid,
            disposition=gate.disposition,
            consumed_handoff=gate.consumed_handoff,
            repo_root=repo_root,
            decisions=effective_decisions,
        )
    )

    # -- Step 2.65/2.66/2.67 (C2c): memo lifecycle + deletion blocks --
    directives.extend(directives_memo_lifecycle.build_directives(decisions))
    deletion_blocks_check_directive = build_deletion_blocks_check_directive(decisions.get("msg_file"))
    if deletion_blocks_check_directive is not None:
        directives.append(deletion_blocks_check_directive)

    # -- Step 2.8/2.95 (C2i): pinboard + machine-local regeneratability --
    pinboard_directive = directives_session_hygiene.build_pinboard_directive(
        orientation_cache_exists=bool(decisions.get("orientation_cache_exists")),
        pinboard_note=decisions.get("pinboard_note"),
    )
    if pinboard_directive is not None:
        directives.append(pinboard_directive)
    directives.append(directives_session_hygiene.build_machine_local_regeneratability_directive())

    # -- Step 2.9 (C2d): review-dispatch mechanical shell --
    # A chain terminal takes the CHAIN-scoped gate, not the session-scoped
    # one — skipping the session gate is right (wrong scope for the
    # disposition), but substituting nothing left the close that caps an
    # entire lineage's diff as the ONE close with no brightline gate at all,
    # strictly less gated than an ordinary mid-chain session (2026-08-03
    # doe-claude-em memo, `cross-repo/inbox/2026-08-03-doe-claude-em-wsc-
    # chain-terminal-brightline-gate-never-fires.md`). The chain gate's
    # machinery was already live — `wsc-coverage-gate-runner brightline-gate
    # --from-handoff` and its two-oracle plan/chain compute — only the call
    # site was missing. `d-coverage-gate` (advisory by design) is NOT that
    # substitute: its judgment point cannot block a complete.
    # The chain gate needs a resolved closing handoff (`--from-handoff` is
    # required by the runner's parser). When a chain terminal has none, the
    # fallback is the SESSION-scoped gate, never silence: a narrower-scoped
    # brightline gate is strictly more than none, and emitting nothing here
    # would reinstate the very hole this branch exists to close, just on a
    # rarer path. Wrong-scope-but-present beats absent.
    # Every close — chain-terminal or not — now takes the SESSION-scoped
    # brightline directive. The chain-terminal branch used to take
    # `d-run-chain-plan-brightline-gate` instead; that gate is removed
    # (state/kill-ledger.md K-007, 2026-08-19, PM ruling), so a chain
    # terminal falls back to the cheap session-scoped gate rather than to
    # no gate at all.
    floor_kwargs = _resolve_review_brightline_floor_kwargs(repo_root, gate.sid, session_start_time)
    if floor_kwargs is not None:
        directives.append(
            directives_review.build_review_brightline_gate_directive(
                gate.sid, repo_root=repo_root, **floor_kwargs
            )
        )
    else:
        directives.append(
            directives_review.build_review_brightline_gate_directive(gate.sid, repo_root=repo_root)
        )
    review_partition = decisions.get("review_partition") or {}
    if review_partition.get("range") and review_partition.get("slices"):
        slices = [
            directives_review.ReviewSlice(slice_id=str(s["slice_id"]), paths=tuple(str(p) for p in s["paths"]))
            for s in review_partition["slices"]
        ]
        directives.extend(
            directives_review.build_review_partition_freeze_directives(str(review_partition["range"]), slices)
        )
        if review_partition.get("integrator_spec_tsv"):
            directives.append(
                directives_review.build_review_partition_integrator_directive(str(review_partition["integrator_spec_tsv"]))
            )
    ubt_check = decisions.get("ubt_check") or {}
    if ubt_check.get("applies"):
        ubt_directive = directives_review.build_ubt_pending_check_directive(True, str(ubt_check.get("since_sha", "")))
        if ubt_directive is not None:
            directives.append(ubt_directive)
    classify_directive = directives_review.build_classify_dispatch_shape_directive(decisions.get("classify_dispatch_plan_file"))
    if classify_directive is not None:
        directives.append(classify_directive)

    # -- Step 2.67 (C2c): Deleted/Kept structured blocks --
    if decisions.get("deleted_paths") or decisions.get("kept_entries"):
        directives.append(
            directives_memo_lifecycle.build_deletion_blocks_directive(
                decisions.get("deleted_paths"), decisions.get("kept_entries")
            )
        )

    # -- Step 3/3.5/3.6 (C2e): commit-tail keystone through cadence --
    directives.append(directives_commit_tail.build_close_tail_args_directive(decisions))
    directives.append(
        directives_commit_tail.build_wsc_tail_directive(
            gate.sid, effective_decisions, partition_mandatory=partition_mandatory
        )
    )
    # `d-archive-session-claim` is DELIBERATELY NOT emitted here (2026-07-28).
    # This ceremony fires once per closed workstream, and a session can
    # close several workstreams before it ends — but `scope.archive()` moves
    # the whole live session claim dir, which is a once-per-SESSION-END
    # operation. Emitting it here archived a still-live session mid-session,
    # destroying once-per-session sentinels and the dispatch-evidence file.
    # Archival now belongs to session END, not workstream close — wired via
    # a SessionEnd hook (DoE-claude repo) rather than this assembly. The
    # `wsc-close archive-session` CLI subcommand remains in place for that
    # caller; the directive builder that used to construct this call
    # (`directives_commit_tail.build_archive_session_claim_directive`) has
    # been removed as unreferenced — only the CLI it wrapped survives.
    # Reads `effective_decisions` (backfilled above with the resolved
    # `governing_plan.slug` above) rather than raw `decisions` directly, so
    # claim and release agree regardless of which precedence leg
    # (decisions/handoff/fixed fallback) actually resolved the plan.
    # Reading the raw decisions key here left `d-release-plan-claim` absent
    # whenever the slug came from the handoff-frontmatter leg — a lock
    # taken and never released (2026-07-27, found in review of the
    # handoff-frontmatter-resolution fix).
    release_plan_claim = directives_commit_tail.build_release_plan_claim_directive(
        effective_decisions.get("governing_plan_slug")
    )
    if release_plan_claim is not None:
        directives.append(release_plan_claim)
    directives.append(directives_commit_tail.build_emit_cadence_directive())

    return directives


# `build_coverage_judgment_point` (jp-coverage-verdict) was removed here
# (K-001, state/kill-ledger.md) along with the `d-coverage-gate` directive
# it existed to surface a run-then-check obligation for -- the directive
# no longer exists, so the function would always return None.


def build_review_scale_judgment_point(
    decision: directives_review.ReviewScaleDecision,
    *,
    chain_terminal: bool = False,
) -> Optional[dict[str, Any]]:
    """Surfaces `decide_review_scale`'s verdict — otherwise dead code with
    no call site (source memo 2026-08-03-doe-claude-em-wsc-chain-terminal-
    brightline-gate-never-fires.md). Sits BESIDE `review-partition-strategy`
    / `reviewer-count-on-oracle-disagreement` (the Staff Engineer finding 13): it does
    not feed their inputs and is not gated on `review_relevant` — read only
    via `gates['review_scale']`, independent of both.

    ADVISORY, not an enforced lock, by deliberate PM ruling (2026-07-27,
    the same ruling `build_coverage_judgment_point` carries): the commit
    tail (`d-run-wsc-tail`) carries no dependency edge on this judgment
    point. See DR-068 ("Commit-Time Coverage Gate — ... Advisory-Not-
    Blocking") and DoE-claude coordinator/docs/wiki/workstream-complete-
    review.md, section "The gate is an oracle, not a lock" — do not
    re-derive this as a bug or wire a dependency edge here without a fresh
    PM decision. (The lesson file a sibling comment cites,
    `state/lessons/2026-07-27-verify-a-gate-actually-enforces-before-s-
    a20579f1aa06.yaml`, is absent from disk in both trees; DR-068 and the
    doc-wiki section above are the surviving, verified sources.)

    Gated (review-integrator finding 2, EM ruling 2026-08-03, mirroring
    `build_coverage_judgment_point`'s own `Optional[dict]` gate): fires only
    when the review scale actually implies review work, or when it could
    not be resolved. Rows 1/2 ("no review needed") are pure noise on every
    trivial close and would otherwise fire unconditionally, unlike this
    point's sibling. This changes ONLY when the point fires, never whether
    it blocks — the advisory posture and DR-068/2026-07-27 ruling above are
    untouched.

    UNRESOLVED branch carries no `recommendation` (example-retrieval-repo-em memo,
    cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-
    mandatory-does-not-halt.md, "mechanism 3"): an unresolved decision on a
    chain-terminal close meant the brightline gate had computed a
    `PARTITION-MANDATORY` verdict that a prior call failed to carry forward
    — this assembler recommending `proceed-unresolved` in that exact case
    routed the EM around its own mandatory-partition gate. That cause is
    now impossible (K-007 removed the gate), but the no-recommendation
    posture is retained on its own merits below. `decide_review_
    scale` returning unresolved is a genuine "this assembler has no further
    evidence to settle it" gap, structurally identical to the one
    `build_session_shape_judgment_point` already handles via
    `build_untrusted_gate_judgment_point` — mirrored here rather than
    inventing a second shape. The RESOLVED branch is untouched and keeps
    `build_judgment_point`'s `acknowledge-scale` recommendation: a resolved
    decision is a fact this assembler trusts itself to characterize, only
    the unresolved case is the false-confidence risk.

    Negative-spec: do not re-add a `recommendation` to the unresolved
    branch (e.g. reviving `proceed-unresolved` via `build_judgment_point`)
    — that is exactly the mechanism the memo above traces from "verdict
    computed but not carried" to "EM takes the tier-appropriate
    recommendation and closes a chain terminal on one reviewer." This is
    STILL advisory, not a new block: `d-run-wsc-tail` carries no dependency
    edge on `jp-review-scale` either way, per DR-068 (2026-07-27,
    "Commit-Time Coverage Gate — ... Advisory-Not-Blocking") and DoE-claude
    coordinator/docs/wiki/workstream-complete-review.md, section "The gate
    is an oracle, not a lock" — do not re-derive this as a bug or wire a
    dependency edge here without a fresh PM decision. Removing the
    recommendation changes what is *offered* on the unresolved path, never
    whether the point blocks.

    The unresolved branch's disposition enum is DELIBERATELY not a
    singleton (example-retrieval-repo-em memo, cross-repo/inbox/2026-08-10-example-retrieval-repo-
    em-jp-review-scale-null-is-blocked-computation.md, defect 2): dropping
    the recommendation above left `proceed-unresolved` as the sole
    selectable value, so the only recordable answer was the one this
    point's own `reason` calls routing around a missing verdict, and an EM
    who correctly determined the close IS partition-mandatory had no way to
    say so. A defect that offers exactly one exit and labels that exit
    unsafe in the same breath gets taken eventually. The enum now mirrors
    `backlog_grind_assemble.readers_mise._unresolved_range_judgment_point`
    — the same untrusted-gate shape, already carrying actionable
    dispositions for the same class of unmeasured-range problem — rather
    than inventing a second idiom:

      - `resolve-verdict-and-recompute` — the discharging exit. Supply the
        unresolved session-scoped input (`decisions["stage_paths"]`, a
        resolvable session id) and recompute. The chain-terminal arm of this
        disposition — run the gate, let it persist a verdict — is gone with
        the gate (K-007); nothing to run, nothing to carry forward.
      - `partition-review-by-hand` — the EM resolved the scale off SKILL.md's
        table and is running the partitioned review on that basis. This is
        the "resolved: partition-mandatory" answer the enum previously could
        not express.
      - `proceed-unresolved` — retained, unrecommended, and still described
        by `reason` as the route-around it is.

    Negative-spec on that enum: there is deliberately NO hand-declared
    `single-reviewer-ok` / "resolve-not-mandatory" counterpart. The
    asymmetry is a safety property, not an oversight — a permissive verdict
    nothing computed was the one outcome the removed verdict store's
    fail-closed contract existed to make impossible, and re-introducing it
    here as an EM-typed disposition would reopen it at a different seam.
    The store is gone (K-007); the asymmetry it justified is not. Over-reviewing on a
    hand call is safe; under-reviewing on one is the reported defect.

    THE UNRESOLVED CAUSE IS NO LONGER DISCRIMINATED FROM DISK (state/kill-
    ledger.md K-007, 2026-08-19, PM ruling). The prior three-way split
    (`absent` = producer pending / `unreadable` = break-class / residual =
    genuinely the EM's) was read off `chain_partition_verdict_store` via one
    stat. That store and the `d-run-chain-plan-brightline-gate` producer
    that wrote it are both removed, so there is no persisted chain verdict
    left to be pending or corrupt: a chain-terminal close now resolves on
    the session-scoped brightline alone and reaches this branch only when a
    session-scoped input is genuinely unresolved. That residual case is the
    one the generic unresolved text always described, so the two
    presence-keyed branches were deleted rather than left to test a fact
    nothing can produce.

    `chain_terminal` is retained and still defaults to `False`: it is INERT
    TODAY -- no branch in this function reads it -- but it stays on the
    signature because the replacement coverage the PM is specifying for the
    chain-wide question will need it, and callers already pass it.
    """
    if decision.resolved and decision.row in (1, 2):
        return None
    if decision.resolved:
        return build_judgment_point(
            {
                "disposition": "acknowledge-scale",
                "rationale": f"review scale row {decision.row} ({decision.scale}): {decision.reason}",
            },
            id="jp-review-scale",
            question="What review scale does decide_review_scale select for this close, and is it resolved?",
            dispositions=[
                build_disposition("acknowledge-scale", resolves=[]),
            ],
            evidence="gates['review_scale'] (decide_review_scale's ReviewScaleDecision)",
            reason=(
                "decide_review_scale's chain-terminal rows (5, 6) had zero call sites — this "
                "judgment point is the surfacing this plan adds, not new enforcement."
            ),
            # (docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-
            # being-questions.md) This branch is reached only once the scale
            # RESOLVES, its one disposition `acknowledge-scale` carries
            # `resolves=[]`, and DR-068 keeps `d-run-wsc-tail` free of any
            # dependency edge on this point — so it gates nothing on the
            # directive axis while carrying a recommendation, which is exactly
            # the shape `_emit`'s backstop refuses when unclassified. Without
            # this marker the envelope raises the moment an EM discharges the
            # review gate, i.e. complying with PARTITION-MANDATORY is what
            # breaks the ceremony. The unresolved branch below builds an
            # untrusted-gate point whose dispositions the EM must still act
            # on, and is correctly left unmarked.
            reportable=True,
        )
    return build_untrusted_gate_judgment_point(
        id="jp-review-scale",
        question=(
            "What review scale does decide_review_scale select for this close, and is it "
            "resolved? It is not resolved here — settle it rather than proceeding without "
            "it: supply the missing input named in `reason` and recompute, or resolve the "
            "scale by hand off SKILL.md's table and partition the review on that basis."
        ),
        dispositions=[
            build_disposition("resolve-input-and-recompute", resolves=[]),
            build_disposition("partition-review-by-hand", resolves=[]),
            build_disposition("proceed-unresolved", resolves=[]),
        ],
        evidence="gates['review_scale'] (decide_review_scale's ReviewScaleDecision)",
        reason=(
            f"review scale unresolved: {decision.reason}. `resolve-input-and-recompute` and "
            "`partition-review-by-hand` are the exits that settle it; proceed-unresolved is "
            "selectable, not endorsed."
        ),
    )


# ---------------------------------------------------------------------------
# jp-completion-entry-scaffold / jp-commit-subject-missing — mechanical,
# disk/decisions-computed halts in front of `d-run-wsc-tail` (state/bug-
# backlog/2026-07-28-workstream-complete-apply-re-scaffolds-t-
# e925d597e0af.yaml). Both mirror `build_session_shape_judgment_point`'s
# "only emitted while the underlying fact holds" shape: once the EM clears
# the fact (authors the entry; supplies a subject), the NEXT `brief()`
# recomputation simply stops emitting the point, and `_append_directive_
# dependency` below only ever ran for a call where the point WAS emitted —
# there is no stale dependency edge left dangling on a since-cleared pass.
# Both are structurally UNRESOLVABLE via a caller-supplied `decisions`
# entry (their one disposition's `resolves` list is deliberately empty) —
# the only way to clear either gate is to fix the underlying fact and
# re-run `apply`, never to fabricate a disposition.
# ---------------------------------------------------------------------------


def build_completion_entry_scaffold_judgment_point(
    entry_path: str, residue_fields: tuple[str, ...], entry_exists: bool = True
) -> dict[str, Any]:
    """Blocks `d-run-wsc-tail` until the `d-complete-entry` scaffold at
    `entry_path` has been hand-authored. SKILL.md's own "Resolving these
    two judgment points is not the last step" paragraph names this exact
    authoring window as mandatory before the commit-tail keystone may
    fire — a single `apply` pass previously fired `d-complete-entry` and
    `d-run-wsc-tail` back to back with no window between them for the EM
    to write anything.

    `entry_exists` distinguishes "not yet scaffolded at all" (no file at
    `entry_path` yet — `directives_completion.compute_completion_entry_
    scaffold_gate`'s own `_coordinator_complete_entry._read_existing_
    scaffold_state` computes this `exists` bit, but `scaffold_residue_
    fields` — the caller of that reader — only returns the missing-field
    LIST and discards it) from "still carries placeholder" (a real file
    exists on disk but one or more of title/nature/prose is still the
    scaffold's own placeholder value). Reporting the absent case as
    "still carries placeholder" claimed evidence this module never had —
    there was nothing on disk to carry anything, placeholder or otherwise.
    Computed here via a plain `Path(entry_path).is_file()` (this module's
    own read-only posture already tolerates a disk read at this layer —
    see `_read_consumed_handoff_text` — and duplicating a filesystem stat
    is cheaper and lower-risk than importing `directives_completion`'s
    private reader across a package boundary this chunk does not own)."""
    fields = ", ".join(residue_fields)
    if entry_exists:
        question = (
            f"The completion entry at {entry_path!r} still carries placeholder "
            f"{fields} — has it been hand-authored yet?"
        )
        evidence = f"{entry_path}'s own frontmatter/body — still-placeholder: {fields}"
    else:
        question = (
            f"The completion entry at {entry_path!r} has not yet been scaffolded at all "
            f"(no file on disk) — its {fields} still need authoring once it exists — "
            "has it been hand-authored yet?"
        )
        evidence = f"{entry_path} does not exist on disk yet — not yet scaffolded: {fields}"
    return build_untrusted_gate_judgment_point(
        id="jp-completion-entry-scaffold",
        question=question,
        dispositions=[build_disposition("not-yet-authored", resolves=[])],
        evidence=evidence,
        reason=(
            f"Author the resolved {fields} directly into {entry_path}, then re-run apply — "
            "there is no disposition that can clear this gate short of actually editing the "
            "file (SKILL.md § Resolve judgment points: 'not the last step')."
        ),
    )


def build_commit_subject_missing_judgment_point() -> dict[str, Any]:
    """Blocks `d-run-wsc-tail` when neither a flat `decisions['subject']`
    nor `decisions['commit-message-authoring']['subject']` resolved to a
    real value. `wsc-tail.py --subject` is HARD-required by the op
    (argparse `required=True`) — dispatching without it is a guaranteed
    exit-2 argparse failure, never a legitimate soft-fail tail item."""
    return build_untrusted_gate_judgment_point(
        id="jp-commit-subject-missing",
        question=(
            "d-run-wsc-tail needs a commit subject — supply it as "
            "decisions['subject'] or decisions['commit-message-authoring']['subject']."
        ),
        dispositions=[build_disposition("subject-not-yet-supplied", resolves=[])],
        evidence="decisions['subject'] / decisions['commit-message-authoring']['subject'], both absent",
        reason=(
            "wsc-tail.py's --subject is argparse required=True; dispatching without it is a "
            "guaranteed exit-2 usage failure, never a legitimate tail-item soft-fail. There is "
            "no disposition that can clear this gate (like jp-completion-entry-scaffold, its "
            "one disposition's resolves list is deliberately empty) — set decisions['subject'] "
            "(or decisions['commit-message-authoring']['subject']) to the commit subject text "
            "directly, then re-run apply; the next brief() recomputation simply stops emitting "
            "this judgment point once a real value resolves."
        ),
    )


def build_open_spine_rows_block_stamp_judgment_point(gate: "directives_spine_worklist.OpenSpineRowGate") -> dict[str, Any]:
    """Blocks `d-stamp-plan-implemented` when the governing plan's spine
    (`directives_spine_worklist.compute_open_spine_row_gate`) either still
    has one or more UNWAIVED `disposition: open` rows, or could not be
    resolved/read at all (`verdict: indeterminate`) — `status: implemented`
    is a terminal state
    (`coordinator_core/frontmatter/schemas/plan.schema.json`) and stamping
    it over unresolved or unverifiable spine work misrepresents the plan as
    done. Mirrors `build_commit_subject_missing_judgment_point`'s shape: a
    single disposition with an empty `resolves` — there is no EM pick that
    clears this gate short of actually resolving/waiving the named row(s)
    or fixing the spine so it can be read (SKILL.md § Resolve judgment
    points: "not the last step"), same as that builder's own docstring
    reasons for `jp-commit-subject-missing`. Only ever called once this
    module has already confirmed `gate.verdict == "indeterminate"` or
    (`gate.verdict == "applicable"` and `gate.unwaived_ids()` is non-empty)
    — see the call site's own comment for the incident this closes; raises
    rather than emitting a degenerate message naming rows it does not have
    if called on any other gate shape.

    Negative spec — the waiver asymmetry is deliberate, not an oversight.
    `decisions["waived_open_spine_row_ids"]` clears the `applicable` arm and
    deliberately does NOT clear the `indeterminate` one. Waiving a named row
    asserts a judgment about work whose state was successfully read; there is
    no equivalent assertion available when the spine could not be read at all,
    so a "proceed anyway" override here would reintroduce exactly the
    false-clean this gate exists to prevent. The cost is real and accepted: a
    governing plan that is permanently unreachable (deleted, moved, or
    unreadable) blocks this directive until the plan itself is repaired, and
    no `decisions[...]` key shortcuts that repair."""
    if gate.verdict == "indeterminate":
        return build_untrusted_gate_judgment_point(
            id="jp-open-spine-rows-block-stamp",
            question=(
                "The governing plan's spine could not be read "
                f"({gate.summary_line}) — stamping status: implemented (a terminal "
                "state) cannot be verified safe. Fix the spine, then re-run."
            ),
            dispositions=[build_disposition("rows-not-yet-resolved", resolves=[])],
            evidence=f"gates.open_spine_row_worklist.verdict: indeterminate — {gate.summary_line}",
            reason=(
                "The spine could not be resolved or read, so whether any row is still "
                "open is unknown; stamping implemented over that unknown risks the same "
                "false-clean this gate exists to prevent. Fix the governing plan or its "
                "spine fence, then re-run apply."
            ),
        )

    unwaived_ids = gate.unwaived_ids()
    if not unwaived_ids:
        raise ValueError(
            "build_open_spine_rows_block_stamp_judgment_point called on a gate with no "
            "unwaived open rows and verdict != 'indeterminate' — caller should not have "
            "promoted this gate to a judgment point"
        )
    unwaived_text = ", ".join(unwaived_ids)
    return build_untrusted_gate_judgment_point(
        id="jp-open-spine-rows-block-stamp",
        question=(
            f"The governing plan still has plan-spine row(s) at disposition: open "
            f"({unwaived_text}) — stamping status: implemented (a terminal state) would "
            "misrepresent that unresolved work as done. Resolve them first."
        ),
        dispositions=[build_disposition("rows-not-yet-resolved", resolves=[])],
        evidence=f"gates.open_spine_row_worklist.rows, still open and unwaived: {unwaived_text}",
        reason=(
            "Resolve via `python3 coordinator/bin/plan-tasks-resolve` (see "
            "gates.open_spine_row_worklist.warn_text), or add the id to "
            "decisions['waived_open_spine_row_ids'] and re-run apply."
        ),
    )


def build_landed_reconciliation_block_stamp_judgment_point(gate: "LandedReconciliationGate") -> dict[str, Any]:
    """Blocks `d-stamp-plan-implemented` when the session's own governing
    plan is `status: landed` with one or more `## Acceptance Criteria` rows
    still unticked (`gate.verdict == "applicable"`), or when that state
    could not be determined at all (`gate.verdict == "indeterminate"`) --
    mirrors `build_open_spine_rows_block_stamp_judgment_point`'s shape and
    carries the same three load-bearing rules that fix landed-instance #1
    (`state/review-sidecars/fourth-instance-hunt.md`): keyed on
    `gate.verdict`, never on whether `gate.warn_text` is set (`warn_text`
    is `None` on `indeterminate` too, so a message-keyed trigger would fail
    open exactly when the plan cannot be read); the `indeterminate` arm
    BLOCKS, and its message says the state could not be determined rather
    than naming an open/total split it does not have; and the open/total
    counts quoted in the `applicable` question below are read directly off
    `gate` -- the single derivation `compute_landed_reconciliation_gate`
    already computed, never recomputed here. Only ever called once the call
    site has confirmed `gate.verdict != "not-applicable"` -- raises rather
    than emitting a degenerate message naming counts it does not have if
    called on any other gate shape."""
    if gate.verdict == "indeterminate":
        return build_untrusted_gate_judgment_point(
            id="jp-landed-reconciliation-block-stamp",
            question=(
                "The governing plan's landed-reconciliation state could not be "
                f"determined ({gate.summary_line}) — stamping status: implemented (a "
                "terminal state) cannot be verified safe. Fix the plan, then re-run."
            ),
            dispositions=[build_disposition("acs-not-yet-reconciled", resolves=[])],
            evidence=f"gates.landed_reconciliation.verdict: indeterminate — {gate.summary_line}",
            reason=(
                "The plan's landed/AC state could not be resolved or read, so whether "
                "any acceptance criterion is still open is unknown; stamping implemented "
                "over that unknown risks the same false-clean this gate exists to "
                "prevent. Fix the governing plan, then re-run apply."
            ),
        )

    if gate.verdict != "applicable":
        raise ValueError(
            "build_landed_reconciliation_block_stamp_judgment_point called on a gate "
            "with verdict 'not-applicable' — caller should not have promoted this gate "
            "to a judgment point"
        )
    return build_untrusted_gate_judgment_point(
        id="jp-landed-reconciliation-block-stamp",
        question=(
            f"The governing plan is status: landed with {gate.open_count} of "
            f"{gate.total_count} acceptance criteria unticked — stamping status: "
            "implemented (a terminal state) would misrepresent that unreconciled work "
            "as done. Reconcile them first."
        ),
        dispositions=[build_disposition("acs-not-yet-reconciled", resolves=[])],
        evidence=f"gates.landed_reconciliation.open_count/total_count: {gate.open_count}/{gate.total_count}",
        reason=(
            "Tick each remaining acceptance criterion once its work is verified landed, "
            "or resolve the outstanding item via `python3 coordinator/bin/plan-tasks-"
            "resolve` (see gates.landed_reconciliation.warn_text), then re-run apply."
        ),
    )


def _append_directive_dependency(directives: list[dict[str, Any]], directive_id: str, dep_id: str) -> None:
    """Adds `dep_id` to the named directive's `depends_on`, normalizing
    `None`/a bare string into a list first — mirrors `ceremony_common.
    apply_halt._normalize_depends_on`'s own accepted shapes. A no-op if
    `directive_id` isn't present in `directives` (defensive; every call
    site here names an id `build_directives` always emits) or `dep_id`
    is already listed."""
    for directive in directives:
        if directive["id"] != directive_id:
            continue
        existing = directive.get("depends_on")
        if existing is None:
            directive["depends_on"] = [dep_id]
        elif isinstance(existing, str):
            directive["depends_on"] = [existing, dep_id] if existing != dep_id else [existing]
        elif isinstance(existing, list):
            if dep_id not in existing:
                existing.append(dep_id)
        return


# ---------------------------------------------------------------------------
# jp-consumed-handoff-completeness — C4's blocking pre-commit completeness
# gate. Keys on "a consumed handoff resolved on disk" (AC3), evaluated per
# element of the plural `gate.consumed_handoff_paths` set (AC6), never on
# `disposition == PREDECESSOR_CONSUMED` — a session that shipped a handoff
# straight from `awaiting_gate` (bypassing /pickup's consume transition)
# leaves no `consumed_by` stamp and can carry a single-session disposition
# despite a resolvable consumed handoff (state/lessons/2026-07-21-ship-a-
# chain-terminal-handoff-via-the-co-4dc2ff716f44.yaml) — gating on
# disposition would reproduce that exact blind spot.
# ---------------------------------------------------------------------------


# Leg B's edge set, deliberately NARROWER than `handoff.has_live_children`'s
# own default (`predecessor,additional_predecessors,forked_from`) — see
# `dag.ARCHIVAL_EDGE_KINDS` / `dag.CONTINUATION_EDGE_KINDS` for the full
# archival-vs-conclusion rationale (example-cockpit-repo-em, 2026-08-05,
# cross-repo/archive/2026-08-05-example-cockpit-repo-em-wsc-leg-b-counts-spinoffs-
# as-live-children.md). Same predicate, different question — the call site is
# where that distinction has to be recorded, because the shared op cannot know
# which one it is being asked.
#
# Imports `dag.CONTINUATION_EDGE_KINDS` directly rather than keeping a local
# literal duplicate — measured, not assumed: `coordinator_core.dag` is ALREADY
# resident in `sys.modules` by this point in `brief()`'s own cold-import
# chain, well before this line executes. `from coordinator_core.pickup_
# assemble import resolve_repo_root` (this module, top of file) triggers
# `coordinator_core.ops`'s eager op-registration sweep, which imports
# `ops/coverage_gate.py` -> `coverage.py` -> `dag.py` — confirmed via
# `python3 -X importtime -c "import coordinator_core.workstream_complete"`
# (`coordinator_core.dag` self-time 487us, nested under `coordinator_core.
# coverage`'s 17.8ms, itself reached via the ops eager-import sweep) and via
# `sys.modules` inspection after a bare `import coordinator_core.
# workstream_complete` (`'coordinator_core.dag' in sys.modules` == True,
# reproduced across repeated fresh-interpreter runs). This is NOT the same
# situation as `_dispatch_has_live_children`'s avoidance of importing
# `handoff_children` at module level just below — that module pulls IPC/
# liveness/session's heavier transitive closure onto the path; `dag.py`
# itself is stdlib-only and already unavoidably loaded here regardless of
# what this line does. Deleting the local duplicate removes the "two CSV
# strings must stay in sync" hazard for this site; see
# coordinator_core/tests/test_dag_edge_kind_ssot.py for the drift guard
# covering the sites that still keep their own representation.
_LEG_B_EDGE_KINDS = ",".join(sorted(CONTINUATION_EDGE_KINDS))


def _dispatch_has_live_children(root: Path, candidate: str) -> dict[str, Any]:
    """Dispatches the `handoff.has_live_children` op in-process (leg B).

    Passes `_LEG_B_EDGE_KINDS` explicitly rather than taking the op's default
    — see that constant's negative spec for why the spinoff edge is excluded.

    Reuses the PATTERN from `baton_assemble/apply.py::_invoke_op_in_process`
    — NOT a cross-package import of it. That function is a private,
    underscore-prefixed helper living in a subprocess-bearing module;
    importing it here would pull that module into `brief()`'s import path,
    against this repo's cold-invocation budget (see module docstring's
    narrow read-only carve-out for why calling the op itself, in-process,
    is fine). `handoff.has_live_children` is `"common_dir"`-scoped
    (`coordinator_core/op_scopes.py:71`), so `root` — the resolved WORKTREE
    root, never a `.git`/common-dir path — is converted to the git common
    dir before dispatch; the op itself converts back via
    `main_worktree_root()` internally. Handing this a common-dir/.git path
    instead of the worktree root is the footgun this mirrors
    `_invoke_op_in_process`'s own docstring to avoid.

    Never raises: a dispatch failure (unregistered op, `root` not inside a
    git worktree, or any other exception the op/its plumbing raises)
    degrades to the SAME `exit_code=2` indeterminate shape the op's own
    fail-closed ladder returns — AC5's non-blocking mapping applies
    uniformly whether the op itself declined or this dispatch could not
    reach it at all.

    Assumes a sync caller; revisit if `brief()` ever gains an async entry
    point.
    """
    import asyncio

    from coordinator_core.ipc import get_op_handler
    from coordinator_core.lifecycle import git_common_dir

    try:
        handler = get_op_handler("handoff.has_live_children")
        if handler is None:
            return {"exit_code": 2, "error": "handoff.has_live_children op not registered"}
        common_dir = git_common_dir(root)
        return asyncio.run(handler({"candidate": candidate, "edge_kinds": _LEG_B_EDGE_KINDS}, common_dir))
    except Exception as exc:  # noqa: BLE001 - degrade to leg B indeterminate (AC5), never raise out of brief()
        return {"exit_code": 2, "error": f"handoff.has_live_children dispatch failed: {exc}"}


# Plan `status:` values a joined plan can carry that leg A treats as closed
# business — a shipped/superseded/deferred/implemented/abandoned/complete
# plan's own repo has already declared it terminal, and re-opening it under
# the inversion (a non-terminal `status` now blocks) is not this gate's call
# to make. Deliberately its OWN partition, not reused from
# `lifecycle_constants` — this codebase's established convention for
# "terminal" sets is that each answers one question and the partitions are
# not expected to agree (see `lifecycle_constants.PLAN_ORPHAN_TERMINAL_
# STATUS`'s docstring); this one answers "does leg A still have standing to
# block on this plan's acceptance criteria," which none of the existing
# sets was built to answer.
#
# `shipped` and `complete` are not members of `plan.schema.json`'s `status`
# enum and are retained/added as deliberate defensive tolerance — the same
# tolerance `lifecycle_constants.PLAN_ORPHAN_TERMINAL_STATUS` already
# documents for `shipped`/`complete`/`executed`. Live corpus counts
# (docs/plans/2026-08-05-leg-a-divest-checkbox-invert-join.md, C2):
# `abandoned` 1 plan, `complete` 4 plans, `shipped` 1 plan. `complete` was
# not named by the source memo; it is added here because the inversion
# makes the omission load-bearing (those 4 plans would otherwise flip from
# correctly-terminal to falsely-`open`).
_LEG_A_TERMINAL_PLAN_STATUS = frozenset(
    {"implemented", "shipped", "superseded", "deferred", "abandoned", "complete"}
)


def _resolve_session_handoff_plan_by_deliverable_id(root: Path, deliverable_id: str) -> Optional[Path]:
    """Resolves a `kind: session-handoff` baton's `deliverable_id`
    frontmatter to the single `docs/plans/*.md` file whose own frontmatter
    `deliverable_id` matches it — the SAME primary-key join
    `draft_plan_aging.resolve_plan_owner` performs in the opposite direction
    (plan -> owning handoff).

    Returns `None` when no plan carries a matching `deliverable_id`, or
    when more than one does — an ambiguous join is not this function's
    call to arbitrate; it degrades to "unresolved" the same as no match at
    all. Never raises: an unreadable/non-UTF-8 plan file is skipped, not
    fatal to the scan."""
    plans_dir = root / "docs" / "plans"
    if not plans_dir.is_dir():
        return None

    matches: list[Path] = []
    for plan_path in sorted(plans_dir.glob("*.md")):
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        plan_frontmatter = parse_frontmatter(plan_text).get("frontmatter")
        plan_deliverable_id = (
            plan_frontmatter.get("deliverable_id") if isinstance(plan_frontmatter, dict) else None
        )
        if not plan_deliverable_id or not isinstance(plan_deliverable_id, str):
            continue
        if plan_deliverable_id == deliverable_id:
            matches.append(plan_path)

    if len(matches) != 1:
        return None
    return matches[0]


def _evaluate_session_handoff_leg_a(root: Path, frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Leg A for `kind: session-handoff` batons only — that kind is not
    built to carry its own `## Acceptance criteria` (0/34 in DoE-claude's
    corpus, 0/22 in claude-klabauter's; cross-repo/inbox/2026-08-03-doe-claude-em-
    wsc-leg-a-session-handoff-kind-blind.md): its acceptance criteria live
    in the PLAN it was executing. Joins on the handoff's own
    `deliverable_id` frontmatter to that plan's `deliverable_id`
    (`_resolve_session_handoff_plan_by_deliverable_id`) — replacing the
    retired `plan:` frontmatter pointer join, per PM ruling R2 (docs/plans/
    2026-08-04-terminal-state-propagation-join-keys.md § C12): `plan:` was
    undeclared and carried by roughly 1 of 80 live handoffs, `deliverable_id`
    resolves for nearly all of them.

    The AC-checkbox fallthrough this function used to fall back to never
    fired against the live corpus (docs/plans/2026-08-05-leg-a-divest-
    checkbox-invert-join.md's Problem section: 0/121 batons) and has been
    deleted. The joined plan's own `status:` is now the sole
    discriminator: a terminal `status` (`_LEG_A_TERMINAL_PLAN_STATUS`)
    resolves `not-applicable` — that repo has already closed the plan, and
    it is not this gate's call to re-open it. A resolved, NON-terminal
    `status` now resolves `open` — the inversion this plan exists to make:
    the joined plan is the consumed predecessor's governing plan, and an
    open (non-terminal) plan means that predecessor is not actually done.

    Falls back to `not-applicable` — a fourth verdict, deliberately
    distinct from `indeterminate` (see this function's caller's docstring)
    — for every case where there is nothing to hold this handoff to: no
    `deliverable_id`, a `deliverable_id` that resolves to no (or more than
    one) plan, an unreadable/non-UTF-8 plan, or a plan whose own `status:`
    is terminal. `detail` always names which of those fired, and — for
    `open` — which plan path and status it evaluated, so a reader can see
    where the verdict came from."""
    deliverable_id = frontmatter.get("deliverable_id")
    if not deliverable_id or not isinstance(deliverable_id, str):
        return {
            "verdict": "not-applicable",
            "detail": "kind: session-handoff carries no deliverable_id frontmatter",
            "open": None,
            "total": None,
        }
    resolved_plan = _resolve_session_handoff_plan_by_deliverable_id(root, deliverable_id)
    if resolved_plan is None:
        return {
            "verdict": "not-applicable",
            "detail": f"deliverable_id {deliverable_id!r} does not resolve to exactly one docs/plans/*.md",
            "open": None,
            "total": None,
        }
    try:
        plan_text = resolved_plan.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {
            "verdict": "not-applicable",
            "detail": f"plan {resolved_plan.name} unreadable",
            "open": None,
            "total": None,
        }
    plan_frontmatter = parse_frontmatter(plan_text).get("frontmatter")
    plan_status = plan_frontmatter.get("status") if isinstance(plan_frontmatter, dict) else None
    plan_display = f"docs/plans/{resolved_plan.name}"
    if isinstance(plan_status, str) and plan_status in _LEG_A_TERMINAL_PLAN_STATUS:
        close_out_last_partial = (
            plan_frontmatter.get("close_out_last_partial") if isinstance(plan_frontmatter, dict) else None
        )
        if close_out_last_partial:
            # C4 (2026-08-08): a terminal-status plan that STILL carries
            # close_out_last_partial: cannot be trusted the way an ordinary
            # terminal plan can -- the marker itself records that the last
            # close-out attempt found the plan not fully shipped/resolved
            # (see C1's `_clear_close_out_partial_marker`, which clears it
            # ONLY on the genuine `implemented` flip). A `status:` field
            # sitting next to an uncleared marker is exactly the
            # self-attestation gap this whole plan (C1-C4) exists to catch
            # -- `not-applicable` here would silently read that unverified
            # status as "correctly nothing to look at". `indeterminate` is
            # the honest verdict: the gate COULD have looked, and what it
            # found was itself suspect, so this must not read as verified.
            return {
                "verdict": "indeterminate",
                "detail": f"plan {plan_display}: status {plan_status!r} is terminal but still "
                f"carries close_out_last_partial: {close_out_last_partial!r}",
                "open": None,
                "total": None,
            }
        return {
            "verdict": "not-applicable",
            "detail": f"plan {plan_display}: status {plan_status!r} is terminal",
            "open": None,
            "total": None,
        }
    return {
        "verdict": "open",
        "detail": f"plan {plan_display}: status {plan_status!r} is not terminal — "
        "consumed predecessor's plan is not closed",
        "open": None,
        "total": None,
    }


# ---------------------------------------------------------------------------
# gates.landed_reconciliation — C3, pln-landed-fires-at-spine-resoluti-ac7e89:
# a session's OWN governing plan sitting at `status: landed` (this repo's
# only writer: `execute_plan_assemble.close_out_and_stamp._stamp_plan_
# landed`, per that plan's C1) with unreconciled `## Acceptance Criteria`
# checkboxes. Read-only, advisory-only, never a blocker (AC9 of that plan):
# leg A above already retains standing on a `landed` plan (`landed` is
# deliberately absent from `_LEG_A_TERMINAL_PLAN_STATUS`), so a landed-but-
# unreconciled predecessor already surfaces on THAT seam when it is a
# session-handoff's own consumed predecessor. This gate covers the
# complementary case — the CURRENT session's own governing plan — which leg
# A does not evaluate at all (leg A only ever looks at a *consumed*
# handoff's joined plan, never at `governing_plan` above).
#
# Deliberately NOT merged with `directives_spine_worklist.
# compute_open_spine_row_gate` just above: that gate fires on an OPEN
# plan-tasks spine row, and `landed` is stamped (this plan's own C1) only
# once every spine row has LEFT `open` — the two conditions are close to
# mutually exclusive on the same plan, not overlapping, so collapsing them
# would mostly render one gate permanently silent on any plan the other
# fires on rather than saving a nudge.
# ---------------------------------------------------------------------------

_LANDED_PLAN_STATUS = "landed"

_LANDED_RECONCILIATION_NOT_APPLICABLE_SUMMARY = (
    "Landed-plan reconciliation: not applicable — governing plan is not landed, "
    "or every acceptance criterion is ticked"
)

_LANDED_RECONCILIATION_INDETERMINATE_SUMMARY = (
    "Landed-plan reconciliation: INDETERMINATE — {reason}; this is not a clean-close "
    "signal, check by hand whether this session's governing plan is landed with open ACs"
)

_LANDED_RECONCILIATION_WARN_TEMPLATE = """WARN [landed-plan-reconciliation]: {plan_ref} is status: landed with {open} of {total} acceptance criteria unticked.
Reconcile and stamp now: tick each remaining AC once its work is verified landed, or resolve the outstanding item via `python3 coordinator/bin/plan-tasks-resolve`.

Reference: docs/plans/2026-08-14-landed-fires-at-spine-resolution-and-clo.md"""


class LandedReconciliationGate(NamedTuple):
    applies: bool
    open_count: int
    total_count: int
    warn_text: Optional[str]
    summary_line: str
    #: Same three-way split as `OpenSpineRowGate.verdict` — "applicable"
    #: (landed, at least one AC unticked), "not-applicable" (not landed, or
    #: landed with every AC ticked), "indeterminate" (no governing plan
    #: resolved, the plan file unreadable, or a landed plan with no
    #: parseable `## Acceptance Criteria` section to reconcile against).
    verdict: str = "not-applicable"


def _landed_reconciliation_indeterminate(reason: str) -> LandedReconciliationGate:
    return LandedReconciliationGate(
        applies=False,
        open_count=0,
        total_count=0,
        warn_text=None,
        summary_line=_LANDED_RECONCILIATION_INDETERMINATE_SUMMARY.format(reason=reason),
        verdict="indeterminate",
    )


def compute_landed_reconciliation_gate(
    governing_plan_slug: Optional[str],
    governing_plan_path: Optional[Path],
) -> LandedReconciliationGate:
    """AC9 (pln-landed-fires-at-spine-resoluti-ac7e89, C3) — surfaces "this
    session's governing plan is `landed` and its ACs are not reconciled" as
    a read-only OFFER, never a blocker: no judgment point, no directive
    dependency edge, no exit code. Mirrors `directives_spine_worklist.
    compute_open_spine_row_gate`'s degrade-never-raise shape and its
    `applies`/`verdict` split (see that function's own docstring for the
    rationale behind splitting `not-applicable` from `indeterminate`).

    Reads `status:` via the same `parse_frontmatter` this module already
    uses for leg A (`_evaluate_session_handoff_leg_a`) and reuses
    `directives_session_hygiene.parse_consumed_handoff_acceptance_criteria`
    for the checkbox count — no second frontmatter reader, no second
    checkbox parser.

    Degrades to `indeterminate`, never raises, on: no governing plan
    resolved, an unreadable/non-UTF-8 plan file, or a `landed` plan whose
    body carries no parseable `## Acceptance Criteria` section (no heading,
    or a heading with zero checkboxes) — there is nothing to reconcile
    against, and that is a fact worth flagging by hand, not a clean close.
    A NON-landed plan, or a landed plan with every AC ticked, is the
    ordinary `not-applicable` case."""
    if not governing_plan_slug or governing_plan_path is None:
        return _landed_reconciliation_indeterminate("no governing plan resolved for this session")

    try:
        source = governing_plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _landed_reconciliation_indeterminate(f"governing plan {governing_plan_slug} could not be read")

    frontmatter = parse_frontmatter(source).get("frontmatter")
    status = frontmatter.get("status") if isinstance(frontmatter, dict) else None
    if status != _LANDED_PLAN_STATUS:
        return LandedReconciliationGate(
            applies=False, open_count=0, total_count=0, warn_text=None,
            summary_line=_LANDED_RECONCILIATION_NOT_APPLICABLE_SUMMARY, verdict="not-applicable",
        )

    parsed = directives_session_hygiene.parse_consumed_handoff_acceptance_criteria(source)
    if parsed is None:
        return _landed_reconciliation_indeterminate(
            f"governing plan {governing_plan_slug} is status: landed but carries no "
            "## Acceptance Criteria heading"
        )
    if parsed["total"] == 0:
        return _landed_reconciliation_indeterminate(
            f"governing plan {governing_plan_slug} is status: landed but its Acceptance "
            "Criteria heading has no checkboxes"
        )
    if parsed["open"] == 0:
        return LandedReconciliationGate(
            applies=False, open_count=0, total_count=parsed["total"], warn_text=None,
            summary_line=_LANDED_RECONCILIATION_NOT_APPLICABLE_SUMMARY, verdict="not-applicable",
        )

    warn_text = _LANDED_RECONCILIATION_WARN_TEMPLATE.format(
        plan_ref=governing_plan_slug, open=parsed["open"], total=parsed["total"],
    )
    summary_line = (
        f"Landed-plan reconciliation: {parsed['open']} of {parsed['total']} ACs unticked on "
        f"{governing_plan_slug} (status: landed) — WARN emitted"
    )
    return LandedReconciliationGate(
        applies=True, open_count=parsed["open"], total_count=parsed["total"], warn_text=warn_text,
        summary_line=summary_line, verdict="applicable",
    )


def _evaluate_consumed_handoff_completeness_element(root: Path, raw_path: str) -> dict[str, Any]:
    """AC3/AC3b/AC4/AC5 — evaluates ONE element of `gate.
    consumed_handoff_paths`: leg A (acceptance-criteria checkbox parse) and
    leg B (`handoff.has_live_children`), independently. Leg B is dispatched
    even when leg A's own read fails — the op performs its own path
    resolution/containment under the worktree root, so a leg-A read
    failure does not imply leg B cannot look.

    leg_a["verdict"] is one of:
        "open"           — leg A FIRES (blocking): for every kind except
                            `session-handoff`, C3 parsed the handoff's own
                            heading with at least one unticked `- [ ]`
                            box; for `session-handoff`, the `deliverable_id`
                            join resolved to exactly one plan whose own
                            `status:` is NOT in `_LEG_A_TERMINAL_PLAN_
                            STATUS` — the consumed predecessor's governing
                            plan is not closed (see
                            _evaluate_session_handoff_leg_a).
        "clean"          — every kind EXCEPT `session-handoff`: heading
                            present, every box ticked (does not block).
                            `session-handoff` never resolves this verdict.
        "not-applicable" — `kind: session-handoff` ONLY: this baton kind
                            does not carry acceptance criteria of its own,
                            and no `deliverable_id` join led anywhere
                            still open — no `deliverable_id`, a
                            `deliverable_id` resolving to no (or more than
                            one) plan, an unreadable/non-UTF-8 plan, or a
                            joined plan whose own `status:` is terminal
                            (`_LEG_A_TERMINAL_PLAN_STATUS`) AND carries no
                            `close_out_last_partial:` marker — a terminal
                            plan that STILL carries that marker resolves
                            `indeterminate` instead (C4, 2026-08-08): the
                            marker itself records that the last close-out
                            attempt found the plan not fully shipped, so
                            its `status:` cannot be trusted the way an
                            ordinary terminal plan's can — see
                            _evaluate_session_handoff_leg_a. Deliberately
                            distinct from `indeterminate`: for the
                            no-`deliverable_id`/no-join/unreadable-plan
                            branches this is "there was nothing to look
                            at, and that is correct"; for the C4
                            uncleared-marker branch (which DOES read the
                            plan and the marker) it is "what the gate
                            found was affirmatively untrustworthy, not
                            merely absent" — either way the verdict
                            legitimately reads as verified-clear, unlike
                            `indeterminate` — see
                            _evaluate_session_handoff_leg_a. Never blocks.
        "indeterminate"  — for every kind EXCEPT `session-handoff`, one of
                            AC3b's three named leg-A holes: the handoff
                            could not be resolved/read at all, C3 found no
                            heading (`None`), or the heading was present
                            with zero checkboxes (`total == 0`) — a
                            heading with no checkboxes is not verifiable
                            completeness, so it does NOT count as a pass.
                            `session-handoff` ALSO resolves this verdict
                            (C4, 2026-08-08) when the `deliverable_id` join
                            led to a plan whose `status:` is terminal but
                            which still carries an uncleared
                            `close_out_last_partial:` marker — the marker
                            records that its own status cannot be trusted
                            as self-attestation, so a terminal-looking
                            `status:` next to it must not read as verified
                            (see _evaluate_session_handoff_leg_a).
    leg_b["verdict"] is one of "live-child" (FIRES, AC4), "no-children"
    (does not block), or "indeterminate" (AC5's non-blocking `exit_code=2`
    mapping — `leg_b["error"]` always carries the op's own `error` string
    in this case, never silently dropped, per AC3b)."""
    resolved = _resolve_handoff_path_str(root, raw_path)
    text: Optional[str] = None
    if resolved is not None:
        try:
            text = resolved.read_text(encoding="utf-8")
        # Review: coordinatorcode-reviewer-c13e4663 Finding 1 — UnicodeDecodeError is a
        # ValueError subclass, not an OSError; leg A must degrade to indeterminate for
        # a non-UTF-8 handoff too, never propagate out of brief() uncaught.
        except (OSError, UnicodeDecodeError):
            text = None

    if text is None:
        leg_a = {"verdict": "indeterminate", "detail": "handoff unreadable", "open": None, "total": None}
    else:
        frontmatter = parse_frontmatter(text).get("frontmatter")
        kind = frontmatter.get("kind") if isinstance(frontmatter, dict) else None
        if kind == "session-handoff":
            leg_a = _evaluate_session_handoff_leg_a(root, frontmatter)
        else:
            parsed = directives_session_hygiene.parse_consumed_handoff_acceptance_criteria(text)
            if parsed is None:
                leg_a = {
                    "verdict": "indeterminate",
                    "detail": "no ## Acceptance criteria heading",
                    "open": None,
                    "total": None,
                }
            elif parsed["total"] == 0:
                leg_a = {
                    "verdict": "indeterminate",
                    "detail": "## Acceptance criteria heading present, no checkboxes under it",
                    "open": 0,
                    "total": 0,
                }
            elif parsed["open"] > 0:
                leg_a = {
                    "verdict": "open",
                    "detail": f"{parsed['open']} of {parsed['total']} acceptance criteria unticked",
                    "open": parsed["open"],
                    "total": parsed["total"],
                }
            else:
                leg_a = {
                    "verdict": "clean",
                    "detail": f"all {parsed['total']} acceptance criteria ticked",
                    "open": 0,
                    "total": parsed["total"],
                }

    leg_b_result = _dispatch_has_live_children(root, raw_path)
    exit_code = leg_b_result.get("exit_code")
    if exit_code == 0:
        leg_b = {"verdict": "live-child", "detail": "has_live_children reports a live child", "exit_code": 0, "error": None}
    elif exit_code == 1:
        leg_b = {"verdict": "no-children", "detail": "no live handoff names this candidate as predecessor", "exit_code": 1, "error": None}
    else:
        error = leg_b_result.get("error") or "has_live_children returned an unexpected shape"
        leg_b = {"verdict": "indeterminate", "detail": error, "exit_code": exit_code, "error": error}

    blocks = leg_a["verdict"] == "open" or leg_b["verdict"] == "live-child"
    return {"handoff": raw_path, "blocks": blocks, "leg_a": leg_a, "leg_b": leg_b}


class ConsumedHandoffCompletenessGate(NamedTuple):
    applies: bool
    blocks: bool
    #: Plain dicts (not nested NamedTuples) — see `_evaluate_consumed_
    #: handoff_completeness_element`'s own return shape. Kept as plain
    #: dicts so `gates.consumed_handoff_completeness.elements[i]["handoff"]`
    #: is a direct key read, matching every other `gates.*` evidence shape
    #: in this envelope, rather than a NamedTuple a caller would have to
    #: know to `._asdict()` a second time.
    elements: tuple[dict[str, Any], ...]
    summary_line: str


def compute_consumed_handoff_completeness_gate(
    root: Path, consumed_handoff_paths: tuple[str, ...]
) -> ConsumedHandoffCompletenessGate:
    """AC6 — evaluates EVERY element of the plural consumed-handoff set;
    one in-flight element blocks without suppressing evaluation of the
    others. `applies=False` (no elements at all) is the common case for a
    single-session run with nothing consumed."""
    if not consumed_handoff_paths:
        return ConsumedHandoffCompletenessGate(
            applies=False,
            blocks=False,
            elements=(),
            summary_line="Consumed-handoff completeness: not applicable — no consumed handoff resolved",
        )

    elements = tuple(
        _evaluate_consumed_handoff_completeness_element(root, raw_path) for raw_path in consumed_handoff_paths
    )
    blocking = [e for e in elements if e["blocks"]]
    indeterminate_notes = [
        f"{e['handoff']} (leg A): {e['leg_a']['detail']}" for e in elements if e["leg_a"]["verdict"] == "indeterminate"
    ] + [
        f"{e['handoff']} (leg B): {e['leg_b']['detail']}" for e in elements if e["leg_b"]["verdict"] == "indeterminate"
    ]

    if blocking:
        names = ", ".join(e["handoff"] for e in blocking)
        summary_line = f"Consumed-handoff completeness: BLOCKING on {names}"
    else:
        summary_line = "Consumed-handoff completeness: all consumed handoffs clear"
    if indeterminate_notes:
        # AC3b: silence must never represent "not checked" — every
        # indeterminate leg (including every leg-B exit_code=2's own
        # `error` string) is named here too, never dropped just because
        # the gate itself did not end up blocking.
        summary_line += " | indeterminate: " + "; ".join(indeterminate_notes)

    return ConsumedHandoffCompletenessGate(
        applies=True, blocks=bool(blocking), elements=elements, summary_line=summary_line
    )


def build_consumed_handoff_completeness_judgment_point(gate: ConsumedHandoffCompletenessGate) -> dict[str, Any]:
    """AC3/AC4 — blocks all six attribution/tail directives when
    `gate.blocks` is True. Copies the `_append_directive_dependency`/
    `build_untrusted_gate_judgment_point` wiring from
    `build_commit_subject_missing_judgment_point`, but deliberately NOT its
    `resolves` shape: that builder's single disposition is unclearable by
    design (its own remedy is "edit a file and re-run"). This gate needs
    the opposite — an EM able to affirmatively override a known in-flight
    state (DR-502, docs/wiki/ceremony-wsc-hardening.md:372: a J-node stays
    formally unresolved until an affirmative EM pick) — so `override-known-
    in-flight` names all six of d-run-wsc-tail, d-claim-plan-execution-lock,
    d-stamp-plan-implemented, d-harvest-deferrals-1, d-complete-entry, and
    d-reconcile-completion-commits in `resolves` (the last of these carries
    a `depends_on="d-complete-entry"` edge plus a literal
    `{d-complete-entry.entry_path}` arg token — `_execute_directives`
    routes an unresolvable token to report["failed"], not
    report["blocked"], so leaving it ungated would strand it and flip a
    correct HALTED_AT_JUDGMENT into a spurious DIRECTIVE_FAILED/
    PARTIAL_MUTATION), and `stop-and-handoff` is the inert arm matching
    SKILL.md's own mutual-exclusion rule."""
    blocking = [e for e in gate.elements if e["blocks"]]
    lines = []
    for e in blocking:
        reasons = []
        if e["leg_a"]["verdict"] == "open":
            reasons.append(f"leg A: {e['leg_a']['detail']}")
        if e["leg_b"]["verdict"] == "live-child":
            reasons.append(f"leg B: {e['leg_b']['detail']}")
        lines.append(f"  - {e['handoff']}: {'; '.join(reasons)}")
    question = (
        "The following consumed handoff(s) are not verifiably complete — unticked acceptance "
        "criteria and/or a live successor handoff still names them as predecessor:\n"
        + "\n".join(lines)
        + "\nPer SKILL.md's mutual-exclusion rule ('/workstream-complete and /handoff are "
        "mutually exclusive. In-flight work → STOP and invoke /handoff instead'), the default "
        "is to stop. Proceed with /workstream-complete anyway?"
    )
    return build_untrusted_gate_judgment_point(
        id="jp-consumed-handoff-completeness",
        question=question,
        dispositions=[
            build_disposition(
                "override-known-in-flight",
                resolves=[
                    "d-run-wsc-tail",
                    "d-claim-plan-execution-lock",
                    "d-stamp-plan-implemented",
                    "d-harvest-deferrals-1",
                    "d-complete-entry",
                    "d-reconcile-completion-commits",
                ],
            ),
            build_disposition("stop-and-handoff", resolves=[]),
        ],
        evidence="gates.consumed_handoff_completeness.elements",
        reason=(
            "A consumed handoff resolved on disk with unticked acceptance criteria and/or a "
            "live successor — keyed on 'a consumed handoff resolved on disk', never on "
            "disposition == PREDECESSOR_CONSUMED (state/lessons/2026-07-21-ship-a-chain-"
            "terminal-handoff-via-the-co-4dc2ff716f44.yaml)."
        ),
    )


# ---------------------------------------------------------------------------
# judgment_points[] — the 29 preserved residue points (C2f/`judgments.py`),
# gated here per each point's own plausible firing condition. `judgments.py`
# itself is unconditional-by-design (its own Negative-spec: "does NOT decide
# whether a given builder's judgment_point should appear... an assembly-time
# concern") — this function is that assembly-time gate.
# ---------------------------------------------------------------------------


def _build_preserved_judgment_points(
    gate: SessionShapeGate,
    decisions: dict[str, Any],
    repo_root: Path,
    governing_plan_present: bool,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    completion_applies = directives_completion.completion_archive_predicate(repo_root)
    review_relevant = bool(decisions.get("review")) or canonicalize(gate.disposition) == PREDECESSOR_CONSUMED
    memo_dispositions_present = bool(decisions.get("memo_dispositions"))
    predecessor_present = canonicalize(gate.disposition) == PREDECESSOR_CONSUMED and bool(gate.consumed_handoff)

    # Step 1 / 1.2 — always relevant (every close evaluates it), scope
    # classification only once a lesson is actually in hand.
    points.append(
        _judgments.build_lesson_worth_capturing_judgment_point(
            directives_lessons_plan.lesson_capture_resolves_ids(decisions)
        )
    )
    if decisions.get("lessons"):
        points.append(_judgments.build_lesson_scope_classification_judgment_point())

    # Step 2/2.4/2.4b — governing-plan reconcile trio.
    if governing_plan_present:
        points.append(_judgments.build_plan_doc_content_update_judgment_point())
        points.append(_judgments.build_plan_vs_reality_reconcile_judgment_point())
        points.append(_judgments.build_enablement_vs_opportunistic_deferral_judgment_point())

    # Step 2.6/2.6.8/2.6b — completion-entry cluster (untrusted-gate: nature
    # classification reads another surface's already-authored prose).
    if completion_applies:
        points.append(_judgments.build_completion_nature_classification_judgment_point())
        points.append(_judgments.build_completion_entry_prose_judgment_point())

    # Step 2.6b/3 — commit-significance filter is relevant at every close.
    points.append(_judgments.build_commit_significance_filter_judgment_point())

    # Step 2.65 — memo lifecycle (both untrusted-gate: another session's
    # memo prose/activity).
    if memo_dispositions_present:
        points.append(
            _judgments.build_memo_resolution_attribution_judgment_point(
                directives_memo_lifecycle.memo_flip_resolves_ids(decisions.get("memo_dispositions") or []),
                directives_memo_lifecycle.compute_memo_resolution_attribution(repo_root, gate.sid),
            )
        )
        points.append(_judgments.build_do_now_memo_violation_check_judgment_point())

    # Step 2.66 — scratch self-clean disposition.
    if decisions.get("scratch_candidates"):
        points.append(_judgments.build_scratch_disposition_per_file_judgment_point())

    # Step 2.7/2.7b — predecessor distill-fate, chain-terminal only, and
    # only when the predecessor genuinely lacks a distill_fate: value to
    # reuse (state/handoffs/2026-08-10-a-commit-trailer-that-names-the-
    # session.md carrying `distill_fate: ephemeral` still tripped this
    # point before the gate below was added -- see this function's own
    # docstring precedent for untrusted-gate points reading another
    # surface's already-authored prose).
    if predecessor_present and _predecessor_lacks_distill_fate(repo_root, gate):
        points.append(_judgments.build_predecessor_distill_fate_judgment_point())

    # Step 2.8 — pinboard note content + orientation-doc row updates.
    if decisions.get("orientation_cache_exists"):
        points.append(_judgments.build_pinboard_note_content_judgment_point())
        points.append(_judgments.build_orientation_doc_row_updates_judgment_point())

    # Step 2.95 — cross-cutting question is asked at every close.
    points.append(_judgments.build_cross_cutting_check_judgment_point())

    # Step 2.96 — inline-waiver recognition, only when a completeness
    # checklist is actually in force against a consumed handoff.
    if predecessor_present:
        points.append(_judgments.build_inline_waiver_recognition_judgment_point())

    # Step 2.9/2.9b — the 8 review-side points, only when review is live.
    if review_relevant:
        # One id list shared by all three dispatching review points — each
        # names the SAME per-slice directives, so they must resolve the same
        # suffixed ids (`directives_review.review_partition_resolves_ids`).
        partition_resolves_ids = directives_review.review_partition_resolves_ids(
            decisions.get("review_partition") or {}
        )
        points.append(_judgments.build_review_partition_strategy_judgment_point(partition_resolves_ids))
        points.append(
            _judgments.build_reviewer_count_on_oracle_disagreement_judgment_point(partition_resolves_ids)
        )
        points.append(_judgments.build_shared_schema_touch_check_judgment_point())
        points.append(_judgments.build_governing_spec_identification_judgment_point())
        points.append(_judgments.build_finding_tradeoff_escalation_check_judgment_point())
        points.append(_judgments.build_shallow_row3_waive_check_judgment_point())
        points.append(_judgments.build_review_dispatch_vehicle_choice_judgment_point(partition_resolves_ids))
        points.append(_judgments.build_quota_retry_vs_escalate_judgment_point())

    # Step 3.0 — concurrent-peer / unattributable-file disposition, only
    # when the caller flags unattributable files at all.
    if decisions.get("unattributable_files"):
        points.append(_judgments.build_concurrent_peer_attribution_judgment_point())
        points.append(_judgments.build_unattributable_file_disposition_judgment_point())

    # Step 3/4 — commit-message authoring + session-work-summary are
    # relevant at every close.
    points.append(_judgments.build_commit_message_authoring_judgment_point())
    points.append(_judgments.build_session_work_summary_judgment_point())

    # Flag-severity classification, only when the caller has flags to
    # classify at all.
    if decisions.get("flags"):
        points.append(_judgments.build_flag_severity_classification_judgment_point())

    return points


# ---------------------------------------------------------------------------
# brief() — the single-shot decision-object computation
# ---------------------------------------------------------------------------


def _resolve_handoff_path_str(repo_root: Path, raw_path: str) -> Optional[Path]:
    """Resolves ONE consumed-handoff path string to a real on-disk file,
    following a concurrent boot sweep's archive of that same handoff when
    the live path no longer exists — this genuinely happens on this fleet
    (the handoff remains the correct provenance record even after
    `fleet.archive_handoffs` moves it under `archive/handoffs/YYYY-MM/`).
    Reuses the fleet-ops archival destination helper (`handoff_archive_
    dest`) rather than re-deriving the `YYYY-MM` placement convention here.
    Returns `None` if the handoff is absent from both the live and
    archived location, or if `raw_path` is falsy.

    Shared by `_resolve_consumed_handoff_path` (the pre-existing scalar
    caller) and C4's per-element completeness loop (`gate.
    consumed_handoff_paths`, plural) — same resolution rule for every
    element, not re-derived per call site."""
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    if candidate.is_file():
        return candidate
    archived = handoff_archive_dest(repo_root, candidate)
    if archived.is_file():
        return archived
    return None


def _predecessor_lacks_distill_fate(repo_root: Path, gate: SessionShapeGate) -> bool:
    """True when at least one element of the plural consumed-handoff set
    (`gate.consumed_handoff_paths`, falling back to the scalar `gate.
    consumed_handoff` if that tuple is somehow empty) genuinely lacks a
    usable `distill_fate:` value -- the gate for Step 2.7/2.7b's
    predecessor-distill-fate judgment point (`build_predecessor_distill_
    fate_judgment_point`), which must fire only when there is something to
    backfill. An unreadable handoff, absent/non-dict frontmatter, or an
    empty/whitespace `distill_fate:` value all count as LACKING -- failing
    open toward asking the question is correct here; failing closed would
    silently re-skip an author's declaration (the defect this predicate
    fixes). Never raises -- a read/parse error must not escape `brief()`."""
    raw_paths = gate.consumed_handoff_paths or (
        (gate.consumed_handoff,) if gate.consumed_handoff else ()
    )
    for raw_path in raw_paths:
        resolved = _resolve_handoff_path_str(repo_root, raw_path)
        if resolved is None:
            return True
        try:
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return True
        try:
            frontmatter = parse_frontmatter(text).get("frontmatter")
        except Exception:
            return True
        if not isinstance(frontmatter, dict):
            return True
        value = frontmatter.get("distill_fate")
        if not isinstance(value, str) or not value.strip():
            return True
    return False


def _resolve_consumed_handoff_path(repo_root: Path, gate: SessionShapeGate) -> Optional[Path]:
    """Resolves `gate.consumed_handoff` (the scalar) to a real on-disk
    file — see `_resolve_handoff_path_str` for the resolution rule itself."""
    return _resolve_handoff_path_str(repo_root, gate.consumed_handoff)


def _read_consumed_handoff_text(repo_root: Path, gate: SessionShapeGate) -> Optional[str]:
    """Best-effort read of the consumed handoff's raw text for Step 2.96's
    completeness-checklist gate (`directives_session_hygiene.
    compute_completeness_checklist_gate`) and for Step 2's governing-plan
    resolution (`_governing_plan_field_from_consumed_handoff` below) —
    read-only, never raises; an unreadable/missing/archived-away handoff
    degrades to `None`, matching every other absent-input case in this
    module's convention. Each caller reads that `None` its own way: the
    completeness-checklist gate's `verdict` is `indeterminate` when the
    close is chain-terminal (it should have had input and did not), the
    governing-plan resolution keeps its own not-applicable branch."""
    candidate = _resolve_consumed_handoff_path(repo_root, gate)
    if candidate is None:
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    # Review: coordinatorcode-reviewer-c13e4663 Finding 1 (sibling) — same
    # non-UTF-8-content gap as the plural-loop read site; this docstring's
    # own "never raises" contract already promised None here, so this was
    # not yet met either.
    except (OSError, UnicodeDecodeError):
        return None


def _governing_plan_field_from_consumed_handoff(repo_root: Path, gate: SessionShapeGate) -> Optional[str]:
    """Step 2's disk-resolvable third precedence leg (see
    `directives_lessons_plan.resolve_governing_plan_with_source`): the
    consumed handoff's own `governing_plan:` frontmatter field — the exact
    field the pre-conversion 769-line hand-walked ceremony body had the EM
    read to locate "the" governing plan, and the one this computed
    assembler regressed on until this fix. Best-effort: an unparsed or
    frontmatter-less handoff yields `None`, matching `resolve_governing_
    plan_with_source`'s own "absent, don't guess" convention rather than
    raising."""
    text = _read_consumed_handoff_text(repo_root, gate)
    if not text:
        return None
    parsed = parse_frontmatter(text)
    fm = parsed.get("frontmatter") or {}
    return fm.get("governing_plan")


def _deliverable_id_from_consumed_handoff(repo_root: Path, gate: SessionShapeGate) -> Optional[str]:
    """Feeds `resolve_governing_plan_with_source`'s deliverable-id join leg,
    which sits below the `governing_plan:` leg
    `_governing_plan_field_from_consumed_handoff` serves.

    That leg above is disk-resolvable in principle and empty in practice —
    no live handoff in the fleet carries a `governing_plan:` field, so the
    ceremony resolved no plan, stamped none implemented, and left every
    baton whose work had shipped advertising itself as in flight. The
    `deliverable_id` read here is the same fact by a key the corpus
    actually populates.

    NEGATIVE SPEC: this is a fallback beneath the EM-supplied and
    frontmatter legs, never a replacement for them — an explicit EM
    decision still wins. Best-effort by the same convention as its sibling:
    an unparsed or frontmatter-less handoff yields `None` rather than
    raising."""
    text = _read_consumed_handoff_text(repo_root, gate)
    if not text:
        return None
    parsed = parse_frontmatter(text)
    fm = parsed.get("frontmatter") or {}
    return fm.get("deliverable_id")


# Kept in sync BY HAND with the M2 "VERIFIED" re-entrant table in
# coordinator_core/workstream_complete/directives_session_hygiene.py's module
# docstring (C3, docs/plans/2026-08-08-wsc-judgment-directive-boundary.md) —
# update both together if a directive's re-entrancy verdict changes. Every
# other directive in the envelope is either M3 (hardcoded
# `already_satisfied: False`, re-fires on every pass) or an UNVERIFIED claim
# and must NOT be added here on convenience. `d-append-orientation-pinboard`
# is deliberately excluded despite having a real satisfaction check: no
# production call site threads `existing_pinboard_line` into
# `build_pinboard_directive` (see this module's own call site), so the check
# never runs and the directive still re-fires in practice.
_VERIFIED_REPLAY_SAFE_DIRECTIVE_IDS = frozenset(
    {
        "d-claim-plan-execution-lock",
        "d-complete-entry",
        "d-release-plan-claim",
    }
)

#: The deferral-harvest directive is emitted one-per-governing-plan with an
#: ordinal suffix (`build_deferral_harvest_directives` ->
#: `d-harvest-deferrals-1`, `-2`, ...), so it cannot be matched by the exact-id
#: set above. Matched on prefix instead; an exact-id entry silently never
#: matched, which under-reported the safe set rather than over-reporting it.
_VERIFIED_REPLAY_SAFE_ID_PREFIXES = ("d-harvest-deferrals-",)


def _is_verified_replay_safe(directive_id: Optional[str]) -> bool:
    """True when `directive_id` is a directive C3's audit verified as
    genuinely re-entrant at the CLI level, and so safe to replay.
    """
    if not directive_id:
        return False
    if directive_id in _VERIFIED_REPLAY_SAFE_DIRECTIVE_IDS:
        return True
    return directive_id.startswith(_VERIFIED_REPLAY_SAFE_ID_PREFIXES)


def _narration_and_next_move(
    gate: SessionShapeGate,
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    reported_judgment_points: Optional[list[dict[str, Any]]] = None,
) -> tuple[str, str]:
    reported_judgment_points = reported_judgment_points or []
    narration = (
        f"Session {gate.sid} resolved disposition={gate.disposition!r} "
        f"({len(directives)} directive(s) computed, {len(judgment_points)} judgment point(s) open)."
    )
    if reported_judgment_points:
        # C2 (docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-
        # being-questions.md): a point `partition_reportable` classified as
        # `reported` (gates no directive present on this envelope) is
        # demoted out of `judgment_points[]` but must not go silent -- its
        # question and its recommendation's rationale (when it carries one)
        # are folded into narration instead, so the EM still sees the fact
        # without being asked to answer a question that cannot change
        # anything. No envelope key is added for this -- `narration` is
        # already free-form.
        reported_bits = []
        for point in reported_judgment_points:
            recommendation = point.get("recommendation") or {}
            rationale = recommendation.get("rationale")
            bit = f"{point.get('id')} ({point.get('question')})"
            if rationale:
                bit += f" -- {rationale}"
            reported_bits.append(bit)
        narration += (
            f" {len(reported_judgment_points)} point(s) gate nothing on this run and are reported, "
            f"not asked: {'; '.join(reported_bits)}."
        )
    if judgment_points:
        replay_safe_ids = sorted(
            d.get("id") for d in directives if _is_verified_replay_safe(d.get("id"))
        )
        if replay_safe_ids:
            replay_note = (
                f" Only {len(replay_safe_ids)} of this run's directives ({', '.join(replay_safe_ids)}) "
                "are verified safe to replay — the rest re-fire on every apply pass, including the "
                "ceremony's own commit step (see directives_session_hygiene.py's module docstring for "
                "the full verdict table); resolve what you can, run apply, and repeat, but a re-run "
                "is not free."
            )
        else:
            replay_note = (
                " None of this run's directives are verified safe to replay — they re-fire on every "
                "apply pass, including the ceremony's own commit step (see "
                "directives_session_hygiene.py's module docstring for the full verdict table); resolve "
                "what you can, run apply, and repeat, but a re-run is not free."
            )
        next_move = (
            "Resolve the open judgment point(s) below, then work the directives in dependency "
            "order. A judgment point you leave open only blocks the directives that depend on "
            "it, not the rest of the run — resolve a subset and re-run to pick up the rest."
        ) + replay_note
    elif directives:
        next_move = "Work the directives in dependency order to finalize this workstream."
    else:
        next_move = "Nothing to do — no directives were computed."
    return narration, next_move


# ---------------------------------------------------------------------------
# gates.review_scale's three disk-derivable row-4 brightline inputs (AC6/AC9,
# docs/plans/2026-08-08-the-engine-asks-for-facts-it-already-holds.md, C5).
# Follows `backlog_grind_assemble.readers_mise`'s shipped shape (that
# module's `_run_git_read_only`/`_measure_range`) rather than inventing a
# new one — see `_measure_session_review_scale_inputs`'s own docstring for
# why the base-resolution differs (session-start-time-anchored here, vs.
# mise's named `start_sha` run record). This helper lives here, never in
# `directives_review.py`, which carries a no-subprocess negative-spec.
# ---------------------------------------------------------------------------

_REVIEW_SCALE_GIT_TIMEOUT = 15
_REVIEW_SCALE_NUMSTAT_ROW_RE = re.compile(r"^(-|\d+)\t(-|\d+)\t(.+)$")


def _run_git_read_only(args: list[str], cwd: Path) -> Optional[str]:
    """Run a READ-ONLY `git` command under `cwd`, returning stdout, or
    `None` on any failure (non-zero rc, missing binary, timeout) — never a
    substitute default for a range this helper could not measure. Mirrors
    `backlog_grind_assemble.readers_mise._run_git_read_only` verbatim in
    shape; not imported cross-package since that one is private to its own
    module."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_REVIEW_SCALE_GIT_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def _resolve_base_sha_after_session_start(
    root: Path, since_iso: str
) -> tuple[Optional[list[str]], Optional[str]]:
    """Base-sha resolution sub-step (Review: code-reviewer P3, 2026-08-08)
    — the `git log --since=<since_iso> --format=%H --reverse` then `git
    rev-parse <first-sha>~1` pipeline, now serving `_resolve_session_start_
    sha` alone.

    NEGATIVE-SPEC: this is a TIME window over the current branch, so on a
    shared worktree its `shas` include every concurrent peer's commits.
    `_measure_session_review_scale_inputs` was its second caller and no
    longer is — it attributes commits by `Session-Id` trailer instead (see
    `_session_owned_shas`). Do not re-point any session-scoped measurement
    at this helper.

    Returns `(shas, base)`: `shas` is `None` on a `git log` failure, else
    the ordered list of commit shas at/after `since_iso` on the current
    branch (possibly empty). `base` is the resolved parent-of-earliest-
    commit sha, or `None` when `shas` is empty/`None`/`git rev-parse`
    failed — deliberately NOT defaulted to `"HEAD"` or any other fallback
    here, since the two callers disagree on what an empty/failed
    resolution should fall back to (see each caller's own docstring)."""
    commits_out = _run_git_read_only(["log", f"--since={since_iso}", "--format=%H", "--reverse"], root)
    if commits_out is None:
        return None, None
    shas = [line.strip() for line in commits_out.splitlines() if line.strip()]
    if not shas:
        return shas, None
    base_out = _run_git_read_only(["rev-parse", f"{shas[0]}~1"], root)
    if base_out is None:
        return shas, None
    return shas, base_out.strip()


def _session_owned_shas(root: Path, session_id: str) -> Optional[list[str]]:
    """This session's OWN commits, oldest-first, selected by `Session-Id`
    commit trailer rather than by time window. `None` on any git failure or
    an absent `session_id` — never `[]`, which a caller would read as a
    truthful "this session committed nothing".

    Trailer attribution, not `--since`, is the only sound selector on a
    shared branch: this repo's load norm puts a dozen-plus concurrent EMs on
    one worktree (`docs/wiki/machine-load-norm.md`), so a time window over
    the current branch sweeps every peer that happened to commit during this
    session — measured live at 17 commits for one session's 1. It also drops
    this measurement's dependence on `resolve_session_start_time` being
    accurate, which it is not required to be: a start time resolving LATER
    than the session's own earliest commit silently excluded that commit
    before.

    DELIBERATELY UNANCHORED at the end. The sibling selector in
    `review_brightline_gate._compute_session_oracle_single` reads
    `--grep=^Session-Id: <sid>$`, and that trailing `$` silently drops any
    commit whose `Session-Id` line is not the message's final line —
    verified live: a commit carrying `Session-Id` followed by
    `Co-Authored-By`/`Commit-Token` did not match the anchored form and did
    match this one, while its message bytes were clean (no CR, no trailing
    space). That is an UNDER-count, the direction that quietly shrinks a
    review scale, so the anchor is not restored here. A same-prefix collision
    is not a real risk against fixed-length UUID session ids.

    KNOWN over-match, accepted: `git log --grep` matches per line of the FULL
    commit message, not the trailer block, so a body line quoting another
    session's trailer verbatim also matches. Over-inclusion is the safe
    direction for this function's callers.

    C5 (docs/plans/2026-08-18-a-session-always-has-a-baton.md § C5): derived
    from `ops.session_commits :: resolve_session_commits` — the same ONE
    `git log --numstat` primitive `branch_resolution.py`'s sibling walks
    were migrated onto, using its unanchored `^Session-Id: <sid>` form
    (this function's own pre-existing anchoring, unchanged — see the
    primitive's module docstring for why unanchored was chosen for every
    caller). This also closes the anchored/unanchored split against
    `review_brightline_gate._compute_session_oracle_single`, which reads
    this same primitive."""
    if not session_id:
        return None
    try:
        commits = _resolve_session_commits_primitive(root, session_id)
    except (ValueError, RuntimeError):
        return None
    return [c["sha"] for c in commits]


# `_resolve_numstat_row_path` (and its two rename regexes) now live in
# `coordinator_core.coverage` — see the top-of-file import block, which
# pulls them in alongside `_is_planning_artifact_path`.


def _accumulate_numstat(text: str, surfaces: set[str]) -> int:
    """Sums added+deleted over `git --numstat` rows, folding each row's path
    into `surfaces`. Binary rows (`-`/`-`) contribute 0 LOC but still count
    as a touched surface.

    Naming trap (review finding, 2026-08-11): the `gross_loc` this feeds is
    the RAW, unfiltered sum — `_is_noise_path` is not applied here;
    `code_loc` (`_accumulate_code_loc_numstat`) is this module's
    noise-excluded sibling. `backlog_grind_assemble/readers_mise.py`'s own
    `gross_loc` means the OPPOSITE — already noise-excluded — so the same
    name carries two different contracts across these two modules."""
    gross = 0
    for line in text.splitlines():
        match = _REVIEW_SCALE_NUMSTAT_ROW_RE.match(line)
        if not match:
            continue
        added, deleted, path = match.groups()
        if added != "-":
            gross += int(added)
        if deleted != "-":
            gross += int(deleted)
        surfaces.add(classify_surface(_resolve_numstat_row_path(path)))
    return gross


def _accumulate_code_loc_numstat(text: str) -> int:
    """Sums added+deleted over `git --numstat` rows, EXCLUDING noise paths
    (`_is_noise_path`) — the `code_loc` counterpart to `_accumulate_numstat`,
    which sums every row unconditionally into `gross_loc`. C5 (2026-08-11):
    reuses the SAME predicate C1 wired into `review_brightline_gate.
    _session_scoped`/`_compute_session_oracle_single` rather than a second
    definition of "code LOC" — a fully-noise row contributes 0 to `code_loc`
    while still counting toward `gross_loc` via `_accumulate_numstat`,
    matching this module's existing "gross_loc stays an accepted parameter,
    code_loc is the reviewable measure" split (see `decide_review_scale`'s
    own docstring).

    Review finding P2 (2026-08-11): a non-noise row also gets the same
    planning-artifact de-weight `_compute_chain_oracle` already applies to
    `chain_loc` (`_PLANNING_LOC_WEIGHT`) — a plan/research/problem-framing
    document is real review obligation, not noise, but is not the same
    review cost per line as code. Reuses the shared predicate and constant
    rather than defining a second weighting.

    2026-08-20 (cross-repo/inbox/2026-08-20-example-retrieval-repo-em-review-gate-doc-
    only-em-discretion.md): prose-bearing rows (`_is_prose_bearing_path` —
    markdown and YAML) are EXCLUDED outright, not de-weighted. This is the
    predicate `review_brightline_gate`'s own mandate arms already apply, and
    its absence here was the reported defect: the same closing session had
    `review-brightline-gate` print `loc=0` (prose filtered) while
    `gates.review_scale` reported `code_loc=2599` (prose counted) and
    mandated a partition off it. A counter named `code_loc` must not measure
    prose while the sibling oracle named the same fact deliberately excludes
    it — the two numbers disagreeing in one brief is the bug, and the fix is
    to make this one mean what it is called. Prose still reaches `gross_loc`
    unfiltered, and the doc-fragile gate (`compute_doc_fragile_gate`) is a
    separate arm that this exclusion does not touch.

    `_is_prose_bearing_path` is extension-only (its own docstring documents
    this as a deliberate JUDGMENT CALL, 2026-08-12 dispatch brief) — an
    executable `.yaml`/`.yml` under a code directory (CI workflow, a
    fixture a test loads at runtime) is excluded the same as narrative
    prose. That was already true of the predicate's other call site; this
    is the first call site where the effect can resolve `code_loc` to a
    genuine zero, which (see the 2026-08-20 note above `_decide_review_
    scale_core`'s `code_loc_resolved_zero`) also suppresses the row-4
    commit/surface brightline arms. Narrowing the predicate is out of this
    function's scope — it is shared with `review_brightline_gate`."""
    total = 0
    for line in text.splitlines():
        match = _REVIEW_SCALE_NUMSTAT_ROW_RE.match(line)
        if not match:
            continue
        added, deleted, path = match.groups()
        resolved_path = _resolve_numstat_row_path(path)
        if _is_noise_path(resolved_path) or _is_prose_bearing_path(resolved_path):
            continue
        row_loc = (int(added) if added != "-" else 0) + (int(deleted) if deleted != "-" else 0)
        if _is_planning_artifact_path(resolved_path):
            row_loc = int(row_loc * _PLANNING_LOC_WEIGHT)
        total += row_loc
    return total


_COMMIT_MARKER_LINE_RE = re.compile(r"^[0-9a-f]{40}$")


def _split_per_commit_numstat(text: str, shas: list[str]) -> dict[str, str]:
    """Splits ``text`` -- the output of a SINGLE `git show --numstat
    --format=%H <shas>` call -- into one numstat block per commit, keyed by
    sha. `%H` (rather than the empty `--format=` this module used before
    A, docs/plans/2026-08-08-the-engine-asks-for-facts-it-already-holds.md
    C-followup) puts a bare-sha marker line at the head of each commit's
    block; `git show` verified live to preserve the caller's own `shas`
    order rather than re-sorting.

    NO SECOND GIT SPAWN: this is a pure text split over the SAME stdout
    `_measure_session_review_scale_inputs`'s committed leg already fetches
    for `gross_loc`/`code_loc` — changing the format string from `""` to
    `"%H"` does not add a spawn, and `_accumulate_numstat`/
    `_accumulate_code_loc_numstat`'s numstat-row regex already ignores any
    line that fails to match `added\\tdeleted\\tpath`, so the marker lines
    this format now injects are silently skipped by both accumulators —
    their totals are unaffected by this change.

    `shas` bounds which marker lines are treated as commit boundaries
    (membership-checked against the caller's own known-good set) rather
    than trusting `_COMMIT_MARKER_LINE_RE` alone — a numstat PATH could
    theoretically also be exactly 40 lowercase hex characters, however
    unlikely; the membership check makes that ambiguity moot."""
    known = set(shas)
    blocks: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        if _COMMIT_MARKER_LINE_RE.match(line) and line in known:
            current = line
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return {sha: "\n".join(lines) for sha, lines in blocks.items()}


def _split_tracked(root: Path, paths: list[str]) -> Optional[tuple[list[str], list[str]]]:
    """Partitions `paths` into (tracked, untracked). `git diff` is blind to
    untracked files, so an unpartitioned diff silently scores a session whose
    whole output is new files — the common shape for `state/` artifacts — at
    zero LOC.

    `None` on a `git ls-files` failure — never `(list(paths), [])`, which
    would route every path (tracked and untracked alike) into the
    `git diff --numstat HEAD` leg. That leg is blind to untracked files, so a
    failed listing under that fallback silently scored the untracked share of
    `paths` at zero LOC while this function still returned a real
    `(list, list)` pair, exactly the too-low-triple-standing-in-for-a-failure
    shape `_measure_session_review_scale_inputs` forbids. The caller must
    treat `None` here the same as any other measurement failure."""
    if not paths:
        return [], []
    normalized_paths = [p.replace("\\", "/") for p in paths]
    listed = _run_git_read_only(["ls-files", "--", *normalized_paths], root)
    if listed is None:
        return None
    known = {line.strip() for line in listed.splitlines() if line.strip()}
    tracked = [p for p in paths if p.replace("\\", "/") in known]
    untracked = [p for p in paths if p.replace("\\", "/") not in known]
    return tracked, untracked


def _count_lines(path: Path) -> Optional[int]:
    """Line count of an untracked file, as its added-LOC contribution.
    `None` when unreadable (a path staged for deletion, a race with a peer's
    write) — the caller must treat this the same as any other measurement
    failure, not skip the file and score it zero."""
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


# Files an untracked-directory walk may enumerate before the measurement stops
# counting exactly and starts reporting a floor. Deliberately high: the only
# untracked directories that can reach here are ones `git status` reports, so
# gitignored trees (node_modules/, .venv/, build output) are already excluded
# upstream and a real work-product directory does not approach this.
_UNTRACKED_WALK_BUDGET = 5000


def _expand_untracked(
    root: Path, rel_path: str, budget: Optional[list[int]] = None
) -> Optional[list[str]]:
    """The untracked FILES a single untracked path contributes, relative to
    `root`. `[rel_path]` for an ordinary file; for a DIRECTORY, every file
    beneath it, because `git status --porcelain` collapses a wholly-untracked
    directory to one trailing-slash entry and the callers that feed this
    measurement pass those entries straight through. `None` when the walk
    itself fails, which the caller must propagate like any other measurement
    failure — `OSError` covers the reachable modes (permission denied on a
    subdirectory mid-walk, and the TOCTOU where the path is removed between
    the `is_dir()` test and the walk).

    Symlinked subdirectories are NOT followed (`os.walk(followlinks=False)`,
    stated rather than inherited: `Path.rglob`'s symlink behaviour is
    CPython-version-sensitive, and this function exists to survive unusual
    working-tree shapes). A symlink loop therefore terminates instead of
    walking forever.

    ``budget`` is a single-element mutable counter shared across one
    measurement's whole untracked list, so several large sibling directories
    compound against ONE bound rather than each getting a fresh one. When it
    is exhausted the walk stops early and returns what it found; the caller
    detects the shortfall off the counter and fails toward MORE review (see
    `_measure_session_review_scale_inputs`).

    Negative-spec: a directory must never reach `_count_lines`. Opening one
    raises `OSError` (`IsADirectoryError` on POSIX, `PermissionError` on
    Windows), which that helper converts to `None` — collapsing the WHOLE
    four-tuple to unresolved, so one peer's untracked scratch directory in the
    dirty set silently erases this session's own brightline and pushes the
    verdict toward reviewing LESS. That is the precise failure direction
    `_measure_session_review_scale_inputs`'s own negative-spec forbids.
    Observed live 2026-08-19 on a strang-03 close, against a peer's
    `state/dispatch-briefs/<brief>/`."""
    target = root / rel_path
    try:
        if not target.is_dir():
            return [rel_path]
        members: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(target, followlinks=False):
            for filename in sorted(filenames):
                if budget is not None and budget[0] <= 0:
                    return members
                members.append((Path(dirpath) / filename).relative_to(root).as_posix())
                if budget is not None:
                    budget[0] -= 1
        members.sort()
    except OSError:
        return None
    return members


def _measure_session_review_scale_inputs(
    root: Path,
    session_start_time: Any,
    session_id: str = "",
    uncommitted_paths: Optional[list[str]] = None,
    commit_slices_out: Optional[list[dict[str, Any]]] = None,
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Measures `(gross_loc, code_loc, commit_count, surface_count)` — the
    four disk-derivable row-4 brightline inputs `decide_review_scale` reads
    — over THIS session's own change set. Any element is `None` when
    genuinely unresolvable (no `session_id`, no git, an unreadable range) —
    never a zeroed quadruple standing in for a failed measurement, which
    would read as a resolved, trivially-small diff and move the verdict
    toward reviewing LESS (the same failure direction
    `backlog_grind_assemble.readers_mise._measure_range`'s own docstring
    guards against; this function mirrors its shape). A `(0, 0, 0, 0)`
    returned after a SUCCESSFUL measurement is a different thing and is
    honest: the session genuinely owns no commits and no dirty files.

    `code_loc` (C5, 2026-08-11) is `gross_loc`'s noise-excluded sibling —
    computed from the SAME already-fetched `git show`/`git diff --numstat`
    text via `_accumulate_code_loc_numstat`, never a second git spawn. This
    is the single production reachability point for `code_loc`: no other
    caller in this module measures it, and `decide_review_scale`'s row 4
    now reads it (C2) rather than `gross_loc`.

    Both halves are session-scoped, because Step 6's review-scale question
    is asked BEFORE `d-run-wsc-tail` commits and a measurement over landed
    commits alone would undercount the normal uncommitted close:

    - COMMITTED work is summed over `_session_owned_shas` via `git show
      --numstat`, per-commit. A `base..HEAD` range is wrong here for the
      same reason a time window is: on a shared branch it spans every peer
      commit interleaved between base and HEAD.
    - An untracked path that is a DIRECTORY is expanded to the files beneath
      it (`_expand_untracked`) before any line counting: `git status
      --porcelain` reports a wholly-untracked directory as one entry, and
      counting that entry as if it were a file collapses the whole
      measurement to unresolved.
    - UNCOMMITTED work is `git diff --numstat HEAD` restricted to the paths
      `directives_memo_lifecycle.classify_session_authored_files` marks
      session-authored (itself fed the Step 3.0 case-(b) exclusion set from
      `directives_commit_tail.resolve_known_concurrent_paths`). Unrestricted,
      this leg measured the entire shared dirty tree — 1775 LOC across 5
      surfaces of live peers' in-flight files attributed to a session whose
      real diff was 96 lines (bug `2026-08-10-workstream-complete-measures-
      review-scal-a52c3f9d55d2`).

    NEGATIVE-SPEC: never widen either leg back to a branch-scoped range or an
    unrestricted worktree diff. A brightline computed over work this session
    did not author does not merely over-review — it invites an EM to record a
    review attestation covering another session's changes, which is the one
    outcome the review trail exists to prevent.

    `surface_count` reuses `review_brightline_gate.classify_surface` — the
    same bucketing the `review-brightline-gate` CLI this verdict names.

    ``commit_slices_out`` (A, docs/plans/2026-08-08-the-engine-asks-for-
    facts-it-already-holds.md C-followup): when not ``None``, appended
    IN PLACE with one ``{"sha": ..., "sha_range": "<sha>^..<sha>", "diff_
    loc": <int>}`` dict per session-owned commit, oldest-first, derived
    from `_split_per_commit_numstat` over the SAME `git show` text this
    function already fetches for `gross_loc`/`code_loc` (no second spawn).
    This is a SIDE CHANNEL, not a return value — the four-tuple return
    stays byte-identical so every existing caller/test is unaffected.
    Left untouched (never appended to) when `shas` is `None`
    (unresolvable) or the committed-leg `git show` itself fails — a caller
    must read `commit_count is None` off this function's own return, not
    an empty `commit_slices_out`, to tell "unresolvable" apart from
    "resolved, zero commits" (mirrors the four-tuple's own None-vs-zero
    contract). Uncommitted work is DELIBERATELY never appended here — it
    has no sha and so cannot become a `sha_range` slice; a caller that
    needs to know uncommitted work contributed LOC not covered by any
    slice compares its own `code_loc` against the sum of `diff_loc` across
    the appended entries."""
    shas = _session_owned_shas(root, session_id)
    if shas is None:
        return None, None, None, None
    commit_count = len(shas)

    gross_loc = 0
    code_loc = 0
    surfaces: set[str] = set()

    if shas:
        # `--format=%H` (not the empty `--format=` this call used before A)
        # -- puts a marker line at each commit's head so `_split_per_commit_
        # numstat` can attribute LOC per commit from this SAME spawn; see
        # that helper's own docstring for why the accumulators below are
        # unaffected by the format change.
        committed = _run_git_read_only(["show", "--numstat", "--format=%H", *shas], root)
        if committed is None:
            return None, None, None, None
        gross_loc += _accumulate_numstat(committed, surfaces)
        code_loc += _accumulate_code_loc_numstat(committed)
        if commit_slices_out is not None:
            per_sha_text = _split_per_commit_numstat(committed, shas)
            for sha in shas:
                commit_slices_out.append(
                    {
                        "sha": sha,
                        # `~1`, never `^`: these slices are consumed by passing
                        # them back as `--sha-range` argv to the trail-write CLI,
                        # and cmd.exe eats a literal `^` in argv on Windows --
                        # collapsing `<sha>^..<sha>` to `<sha>..<sha>`, an empty
                        # range the op rejects with a bare `ValueError` that names
                        # neither the caret nor the shell. The two spellings are
                        # equivalent to git; only one survives the documented
                        # consumer path on a first-class platform.
                        "sha_range": f"{sha}~1..{sha}",
                        "diff_loc": _accumulate_code_loc_numstat(per_sha_text.get(sha, "")),
                    }
                )

    if uncommitted_paths is None:
        uncommitted_paths = [
            row["path"]
            for row in directives_memo_lifecycle.classify_session_authored_files(
                root,
                session_start_time,
                known_concurrent_paths=directives_commit_tail.resolve_known_concurrent_paths(
                    root, session_id
                ),
            )
            if row.get("session_authored")
        ]

    split = _split_tracked(root, uncommitted_paths)
    if split is None:
        return None, None, None, None
    tracked, untracked = split
    if tracked:
        # Review: code-reviewer — Finding (P1). `_split_tracked` returns
        # `tracked` built from the caller's original, unnormalized paths
        # (backslash-containing on Windows) -- normalizing it only for its
        # OWN internal `ls-files` pathspec, not for the caller. Feeding that
        # raw list into this second `git diff --numstat` pathspec reproduces
        # the exact defect this fix was meant to close, one call site
        # downstream. Normalize here, the same way `_split_tracked`
        # normalizes before its own `ls-files` call.
        normalized_tracked = [p.replace("\\", "/") for p in tracked]
        dirty = _run_git_read_only(
            ["diff", "--numstat", "HEAD", "--", *normalized_tracked], root
        )
        if dirty is None:
            return None, None, None, None
        gross_loc += _accumulate_numstat(dirty, surfaces)
        code_loc += _accumulate_code_loc_numstat(dirty)
    walk_budget = [_UNTRACKED_WALK_BUDGET]
    for rel_path in untracked:
        members = _expand_untracked(root, rel_path, budget=walk_budget)
        if members is None:
            return None, None, None, None
        for member in members:
            added = _count_lines(root / member)
            if added is None:
                return None, None, None, None
            gross_loc += added
            if not (_is_noise_path(member) or _is_prose_bearing_path(member)):
                code_loc += added
            surfaces.add(classify_surface(member))
    if walk_budget[0] <= 0:
        # An untracked tree too large to enumerate exactly IS a big diff, so
        # the exhausted budget resolves the brightline rather than defeating
        # it: the counted LOC is a floor, and it is raised to the row-4
        # threshold so `decide_review_scale` trips instead of reading the
        # truncation as a small diff. Returning `None` here would restore the
        # exact fails-toward-less-review collapse this walk exists to remove.
        gross_loc = max(gross_loc, directives_review._BRIGHTLINE_LOC)
        code_loc = max(code_loc, directives_review._BRIGHTLINE_LOC)

    return gross_loc, code_loc, commit_count, len(surfaces)


# ---------------------------------------------------------------------------
# The mid-chain review-brightline-gate range floor (2026-08-08,
# docs/plans/2026-08-08-the-second-close-re-measures-the-first-c.md) — the
# production caller-side supply for `directives_review.
# build_review_brightline_gate_directive`'s four dormant kwargs (landed
# 2026-08-08 as a builder-side capability with no caller; this is that
# caller). Lives here, not in `directives_review.py`, for the same reason
# `_run_git_read_only`/`_measure_session_review_scale_inputs` do: that
# module carries a no-subprocess, no-trail-record-fetch negative-spec.
# ---------------------------------------------------------------------------


def _git_is_ancestor(root: Path, ancestor_sha: str, descendant_sha: str) -> bool:
    """True iff `ancestor_sha` is an ancestor of (or identical to)
    `descendant_sha`, via `git merge-base --is-ancestor` under `root`.
    Mirrors `wsc-coverage-gate-runner.py::_git_is_ancestor` in shape —
    duplicated rather than imported, matching this module's own
    `_run_git_read_only` precedent of not reaching into `coordinator/bin/`.
    Any subprocess failure (including a non-zero "not an ancestor" exit)
    resolves to `False`: this predicate must never trust a floor it could
    not positively confirm."""
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_REVIEW_SCALE_GIT_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _resolve_head_sha(root: Path) -> Optional[str]:
    """Resolve `HEAD` to its current concrete full sha via `git rev-parse`,
    for `_resolve_review_brightline_floor_kwargs`'s `chain_tip_sha` — see
    that function's own docstring for why this call exists and what it
    trades away. Mirrors `_git_is_ancestor`'s subprocess shape (same
    timeout, same `CREATE_NO_WINDOW` Windows-popup guard) — duplicated
    rather than composed into a shared helper, matching this module's own
    `_run_git_read_only`/`_git_is_ancestor` precedent of small, independent
    git-subprocess call sites rather than a shared abstraction.

    Returns `None` on ANY failure (detached-HEAD edge case that still
    resolves fine in practice, a missing git binary, a non-repo root, a
    timeout, non-zero exit, or empty/malformed stdout) — never raises, and
    never fabricates a sha. The caller degrades to the literal `"HEAD"`
    string on a `None` here (a memo miss, not a build-path failure) — see
    `_resolve_review_brightline_floor_kwargs`'s own docstring for that
    contract."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_REVIEW_SCALE_GIT_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return None
    return sha


def _resolve_session_start_sha(root: Path, session_start_time: Any) -> Optional[str]:
    """This session's own `session_start_sha` fallback for `resolve_mid_
    chain_review_scope` — the same base-commit resolution idiom as
    `_measure_session_review_scale_inputs` above (this session's first
    commit at/after `session_start_time`, or that commit's parent when it
    exists), now sharing that sub-step's actual implementation via
    `_resolve_base_sha_after_session_start` (Review: code-reviewer P3,
    2026-08-08) rather than duplicating the git-log/rev-parse pipeline a
    second time — only the surrounding tuple/None-handling stays separate
    per caller, since `_measure_session_review_scale_inputs` returns a
    different (three-element) tuple shape and defaults `base` to `"HEAD"`
    on zero commits, which this function must never do (see below). `None`
    on any unresolvable step (`session_start_time` absent, a `git` failure,
    OR — Review: code-reviewer (P2 #2) — zero commits landed since session
    start)
    — never a guessed sha, and never the literal `"HEAD"` token: that value
    used to be returned here and could flow through to
    `resolve_mid_chain_review_scope`'s own `session_start_sha` fallback,
    which the caller (`_resolve_review_brightline_floor_kwargs`) then hands
    to the gate builder as a real floor — emitting a well-formed but EMPTY
    `HEAD..HEAD` range instead of the caller's byte-identical no-range
    fallback. Returning `None` here instead makes
    `_resolve_review_brightline_floor_kwargs` bail out via its own
    `if session_start_sha is None: return None`, which is the safe,
    already-tested no-range path."""
    if session_start_time is None:
        return None
    since_iso = session_start_time.astimezone(timezone.utc).isoformat()
    shas, resolved_base = _resolve_base_sha_after_session_start(root, since_iso)
    if shas is None:
        return None
    if not shas:
        # Review: code-reviewer (P2 #2) — returning the literal "HEAD" here
        # propagated into `resolve_mid_chain_review_scope`'s fallback and
        # could emit an empty `HEAD..HEAD` range instead of the caller's
        # byte-identical no-range fallback. `None` makes
        # `_resolve_review_brightline_floor_kwargs` bail out (its own
        # `if session_start_sha is None: return None`), so the caller emits
        # today's plain `["--session-id", sid]` call — the safe direction.
        return None
    return resolved_base


def _list_review_trail_paths_for_root(root: Path) -> list[str]:
    """`list_review_trail_records.list_paths()`-equivalent, but honouring
    THIS caller's explicit `root` instead of that function's own cwd-or-
    `COORDINATOR_ROOT` resolution.

    Review: code-reviewer (P2 #1) — the prior call site
    (`list_review_trail_records.list_paths(date_prefix="")`) has no
    `root`/`repo_root` parameter at all; it resolves the state root purely
    from process cwd (git-root-of-cwd) or the `COORDINATOR_ROOT` env var, so
    a `brief(repo_root=...)` call against a root that differs from cwd read
    (or failed to find) the WRONG tree's `state/review-trail/` — silently,
    since every existing test monkeypatches `list_paths` itself rather than
    exercising this path.

    Reuses that module's own `_collect` scanner (the actual `*.json`
    directory walk) rather than re-implementing it — only the two
    directory paths are computed here, and only for the direct-root case
    this caller always has (an explicit `root: Path`, never an env-override
    or meta-repo cwd): `_resolve_state_root()`'s own doc says a sibling-repo
    cwd resolves `state_root` as `<git-root>/state` directly (no meta-repo
    detection needed once the root is already known), so `state_root =
    root/"state"` and — mirroring that function's own basename-in("state")
    branch, which is always true here — `archive_dir =
    root/"archive"/"review-trail"`. This does NOT fork the UNION logic
    (which two directories get combined); it only supplies the one caller-
    known input (`root`) that function has no parameter for.

    Review: code-reviewer (P3) — cross-reference, not just prose: mirrors
    `list_review_trail_records.list_paths()`'s `live_dir`/`archive_dir`
    computation at `coordinator_core/ops/list_review_trail_records.py:275-280`
    verbatim (same two directories, same `_collect` scanner below).
    """
    state_root = root / "state"
    live_dir = str(state_root / "review-trail")
    archive_dir = str(root / "archive" / "review-trail")
    try:
        records = (
            list_review_trail_records._collect(live_dir)  # noqa: SLF001 - reusing the module's own scanner, not re-implementing it
            + list_review_trail_records._collect(archive_dir)  # noqa: SLF001
        )
    except OSError:
        # Mirrors `list_paths`'s own `ReviewTrailListError` degradation on a
        # directory-scan failure — the caller here treats an empty list
        # exactly like "no own records", falling through to `None`.
        return []
    records.sort(key=lambda r: r[0])
    return [str(Path(fullpath)) for _basename, fullpath in records]


def _resolve_review_brightline_floor_kwargs(
    root: Path, sid: str, session_start_time: Any
) -> Optional[dict[str, Any]]:
    """Builder-supply for `directives_review.build_review_brightline_gate_
    directive`'s four dormant kwargs: this session's own prior review-trail
    record(s), if any, floor the mid-chain brightline gate's range at the
    last-reviewed sha instead of the whole session (the defect this plan
    fixes — a session that closes twice had its second close scored over
    both).

    Returns `None` on the ordinary single-close path (AC2 — MUST stay
    byte-identical; this is nearly every close) and on ANY resolution
    failure — no `session_start_time`, an unreadable trail-record store, a
    `git` failure resolving `session_start_sha` — never a guessed floor.
    The caller falls back to today's plain `session_id`-only call in every
    `None` case. Zero own trail records is itself the byte-identical AC2
    path, not a failure — both reach `None` the same way, deliberately: a
    caller does not need to distinguish "nothing to floor with" from "could
    not resolve a floor," both mean "call the builder as before."

    Record selection: trail records whose own `session_id` field equals
    `sid` (exact string match — `sid` is the same value `gate.sid` resolves
    elsewhere in this module, and every on-disk record under
    `state/review-trail/` carries that same value verbatim in its own
    `session_id` field, verified against real records). A peer session's
    record is never included: including one would floor (or fail to floor)
    this session's range using a peer's own reviewed span, which could
    widen the emitted range over commits this session never touched —
    forbidden by the plan's Anti-scope.

    Review: code-reviewer (P2, 2026-08-08, scan cost) — this helper
    `json.load`s every `*.json` under `state/review-trail/` and
    `archive/review-trail/` (2,778 files measured on-disk today) on every
    mid-chain close where `session_start_time` resolved, filtering by
    `session_id` only AFTER the load. Accepted as-is, not optimised
    speculatively: measured at ~0.07s for a full load, not currently a
    budget breach for this ceremony op. No cheaper filter is available
    without forking the live+archive union `list_review_trail_records`
    already owns — `list_paths(date_prefix=...)` only trims by filename
    DATE prefix, not `session_id`, and this caller has no independent
    established filename<->session_id convention to filter on safely
    (nothing else in this module relies on one). Revisit if the corpus
    growth trend makes this scan measurably slow inside `brief()`'s
    invocation budget — the fix then is threading `date_prefix` through
    `_list_review_trail_paths_for_root`, not before.

    Field-shape adapter: `resolve_mid_chain_review_scope` reads a record's
    floor via the `sha_range_head`/`head` keys — a shape NO record on this
    fleet's disk actually carries. Every real record instead carries
    `sha_range` (format `"<start>..<tip>"`, occasionally `"<start>^..
    <tip>"`). `directives_review.resolve_trail_range_tip` (already public,
    left unmodified — AC3's ops-layer file stays untouched and this
    module's own Negative-spec forbids re-implementing that resolution)
    already extracts a record's trustworthy tip from that real shape; this
    function re-keys the result onto `sha_range_head` before handing the
    list to `resolve_mid_chain_review_scope`. A record whose tip cannot be
    trusted (an unterminated `..HEAD` range, an unparseable `sha_range`) is
    simply omitted from the re-keyed list — identical in effect to
    `resolve_mid_chain_review_scope`'s own `if not head: continue`, since
    an omitted record and one re-keyed to `sha_range_head=None` are
    indistinguishable to that loop.

    `chain_tip_sha` (mid-chain gate memo fix, 2026-08-11 — see
    `directives_review.record_gate_verdict_if_passed`'s own KEY-STALENESS
    restriction paragraph): FORMERLY the literal string `"HEAD"`,
    unconditionally, on the reasoning that a live symbolic tip keeps the
    emitted range current through the gate's own later read. That reasoning
    is still correct for the RANGE the gate walks — but it also meant the
    resolved argv's tip half was NEVER a concrete sha, so
    `record_gate_verdict_if_passed`'s `_is_concrete_sha` check never passed
    for the floor-resolved path, and the whole gate-verdict memo this
    module's own "Gate verdict memo" machinery exists to serve NEVER HIT on
    this path — the floor-resolution machinery ran, paid its own git-spawn
    cost, and built a directive whose memo could never fire.

    Now resolves `HEAD` to a CONCRETE, frozen sha via `_resolve_head_sha`
    — LAZILY, only here, at the point the floor path is actually confirmed
    taken (this session has own trail records AND a resolvable
    `session_start_sha`) — never at this function's own entry, and never
    unconditionally: `brief()` calls this on every mid-chain preview
    (including every read-only `brief()` call a caller makes before ever
    reaching `apply()`), so an unconditional git spawn here would tax the
    read-only preview path this whole module's docstring holds to an
    invocation budget. `_resolve_head_sha` failing (detached HEAD in a
    shape that still errors, a git failure, a non-repo root) degrades to
    the PRE-FIX literal `"HEAD"` string — never raises into the build path,
    never fabricates a sha — which reproduces today's exact behavior for
    that one resolution failure (a memo miss, not a build-path failure; see
    that helper's own docstring).

    Freezing the tip here is a DELIBERATE, DOCUMENTED behavior change, not
    an accidental narrowing: the range the gate walks is now anchored to
    whatever commit was HEAD at `brief()`/`build_directives()` BUILD time,
    not whatever HEAD happens to be when the gate CLI itself later runs.
    Those two differ whenever a commit lands between build and dispatch —
    on this fleet's shared, highly concurrent branches that is a real,
    not theoretical, gap. This is the INTENDED trade the mid-chain gate
    memo needs to ever hit at all (a symbolic tip can never be memoized,
    per `_is_concrete_sha`'s design) — a frozen anchor may omit a
    just-landed commit from THIS pass's brightline walk, but that commit is
    still covered by the SESSION's own trailer-scoped diff on the gate's
    OWN default range if this floor path is never reached again, and by a
    later close's own re-resolution otherwise. Not silently stumbled into:
    flagged here, at the one call site that changed, for the next reader
    who wonders why this differs from the read-at-gate-run-time default the
    non-floored two-element argv shape (`["--session-id", sid]`) still
    uses unchanged."""
    if session_start_time is None:
        return None
    paths = _list_review_trail_paths_for_root(root)
    own_records: list[dict[str, Any]] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("session_id") == sid:
            own_records.append(record)
    if not own_records:
        return None

    session_start_sha = _resolve_session_start_sha(root, session_start_time)
    if session_start_sha is None:
        return None

    trail_records: list[dict[str, Any]] = []
    for record in own_records:
        tip, _reason = directives_review.resolve_trail_range_tip(record)
        if tip is not None:
            trail_records.append({"sha_range_head": tip})

    # Lazy, floor-path-only resolution (see this function's own
    # `chain_tip_sha` docstring paragraph above) — reached only once every
    # earlier bail-out (`session_start_time is None`, no own trail records,
    # unresolvable `session_start_sha`) has already NOT fired.
    chain_tip_sha = _resolve_head_sha(root) or "HEAD"

    return {
        "trail_records": trail_records,
        "chain_tip_sha": chain_tip_sha,
        "is_ancestor": lambda a, b: _git_is_ancestor(root, a, b),
        "session_start_sha": session_start_sha,
    }


#: Bound on the caller-supplied `decisions["commit_count_scope"]` free-text
#: annotation (review-integrator finding, P2, 2026-08-12) — it reaches row
#: 4's `reason` string verbatim (`directives_review._row4_decision`'s
#: `scope_note`) with no upstream shape guarantee. Mirrors this module's own
#: `_QUOTA_WEAK_CORROBORATION_MAX_LEN` convention (`directives_review.py`) of
#: a plain length cap over a fixed enum — the docstring examples
#: (`"session-owned"`, "a franker label") are open-ended human labels, not a
#: closed vocabulary, so an allowlist regex would reject legitimate values.
_COMMIT_COUNT_SCOPE_MAX_LEN = 80
#: Strips control characters and the two literal characters (`)`  `=`) a
#: value could use to forge a second `commits=`/`surfaces=` clause or close
#: the `reason` string's parenthetical early — the exact spoofing vector the
#: same finding named. Newlines fall under `\x00-\x1f`.
_COMMIT_COUNT_SCOPE_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f()=]")


def _sanitize_commit_count_scope(raw: str) -> str:
    """Bound a caller-supplied `commit_count_scope` free-text value before
    it is threaded into `decide_review_scale` and, from there, row 4's
    `reason` string — see `_COMMIT_COUNT_SCOPE_MAX_LEN`/`_UNSAFE_RE` above.
    Unsafe characters are dropped rather than the whole value rejected: the
    attestation is already caller-asserted and unverified (see this call
    site's own `commit_count_scope` comment) — a shape defect in the label
    should not additionally raise or silently discard the override."""
    return _COMMIT_COUNT_SCOPE_UNSAFE_RE.sub("", raw)[:_COMMIT_COUNT_SCOPE_MAX_LEN]


def brief(decisions: Optional[dict[str, Any]] = None, repo_root: Optional[Path] = None) -> Mapping[str, Any]:
    """Computes the `/workstream-complete` decision object for the current
    (or a caller-supplied) repo root. Read-only throughout; mutates nothing.
    Raises `TransportFailure` on a repo-root or bin-module resolution
    failure — callers (the CLI `main`) degrade that to `EXIT_TRANSPORT_FAIL`.
    """
    decisions = decisions or {}
    repo_root_was_cwd_derived = repo_root is None
    root = repo_root or resolve_repo_root()
    if root is None:
        raise TransportFailure("could not resolve a git worktree root")

    gate = compute_session_shape_gate(root)

    # C2 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md):
    # the C1 foreign-repo gate needs `gate.sid`, itself resolved from THIS
    # same possibly-suspect `root` via `compute_session_shape_gate` above.
    # This is NOT a cycle to "fix" by reordering: `resolve_session_id` is a
    # pure env read that ignores the root it is passed, so `gate.sid` is
    # uncontaminated by whether `root` itself is the wrong repo.
    #
    # Gated ONLY when `root` was cwd-derived (`repo_root` not supplied) — an
    # explicitly-passed `repo_root` is an unambiguous statement of caller
    # intent that never touched cwd, so refusing it would regress a caller
    # with no defect (plan Anti-scope). An explicitly-supplied `repo_root`
    # still gets the gate CALLED and its verdict RECORDED below — just never
    # refused on.
    repo_identity_gate = compute_repo_identity_gate(root, gate.sid)
    if repo_root_was_cwd_derived and repo_identity_gate["verdict"] == "MISMATCH":
        raise TransportFailure(repo_identity_gate["message"])
    handoff_governing_plan_field = _governing_plan_field_from_consumed_handoff(root, gate)
    handoff_deliverable_id = _deliverable_id_from_consumed_handoff(root, gate)
    # `session_id` feeds precedence leg 2.5, the session's own commit-trailer
    # `Deliverable-Id` join. Without it the leg is unreachable in production:
    # it defaults to `None` and short-circuits, so the rung passes its unit
    # tests and never fires in the real ceremony — the inert-mechanism shape
    # AC4 exists to close, not to reproduce.
    governing_plan, governing_plan_source = directives_lessons_plan.resolve_governing_plan_with_source(
        root, decisions, handoff_governing_plan_field, handoff_deliverable_id, gate.sid
    )
    # Resolved once, here, and threaded into BOTH `build_directives` (the
    # mid-chain review-brightline-gate floor, 2026-08-08) and
    # `_measure_session_review_scale_inputs` below — see `build_directives`'s
    # own `session_start_time` docstring for why this replaces a second,
    # later resolution of the same fact.
    session_start_time = directives_memo_lifecycle.resolve_session_start_time(root, gate.sid)

    # AC1/AC2/AC4 — the seven row-4/5/6 inputs are caller-supplied `decisions`
    # facts, never assembler-computed (brief() is read-only and budgeted; see
    # this plan's Anti-scope on git subprocess calls). Absent means
    # unresolved, never defaulted to a value that changes the row selected
    # (C1's tri-state) — `decisions.get(...)` already returns `None` on
    # absence, which is exactly `decide_review_scale`'s "not yet resolved"
    # sentinel for every one of these seven params.
    #
    # AC6/AC9 (2026-08-08-the-engine-asks-for-facts-it-already-holds, C5):
    # `gross_loc`/`commit_count`/`surface_count` are the three disk-
    # derivable row-4 brightline inputs — see
    # `_measure_session_review_scale_inputs`. Backfilled ONLY when the
    # caller left them unresolved (`decisions.get(...)` already returning
    # `None`, the exact sentinel `decide_review_scale` reads as "not yet
    # resolved") — a caller-supplied value always wins. `code_loc` stays a
    # pure passthrough, the same posture `backlog_grind_assemble.
    # readers_mise._read_phase_6_review_scale` documents for its own
    # `code_loc=None`: row 3 short-circuits on `executor_dispatched is
    # True` before `code_loc` is ever consulted, and computing a
    # code-vs-noise LOC split would be a NEW predicate this chunk does not
    # own. `session_start_time` was already resolved once above (now also
    # threaded into `build_directives` for the review-brightline-gate
    # floor) and is reused here rather than re-resolved a second time.
    # A caller-supplied `stage_paths` is the EM's own reviewed-and-narrowed
    # set of this session's uncommitted files (jp-stage-paths-missing), so it
    # outranks re-deriving the same thing from the session-authored
    # predicate — which cannot see a MODIFIED pre-existing file at all.
    #
    # Classified ONCE here and threaded to both consumers (the measurement
    # below, and the `jp-stage-paths-missing` candidate set further down):
    # `classify_session_authored_files` spawns one `git log` per dirty path,
    # and on this repo's shared worktree the dirty set routinely runs to
    # dozens of files, so a second caller doubles this op's whole spawn
    # budget against its end-to-end invocation budget.
    # `stage_paths: []` is an ANSWER ("this session has no uncommitted
    # files"), not an absent one — distinguished by `is None`, never by
    # truthiness. Collapsing the two costs one `git log` spawn per dirty
    # path, and on this shared worktree the dirty set reaches five figures:
    # measured live at 18,555 entries, which wedged this op past a 7-minute
    # client timeout on a close whose own file set was already committed.
    known_concurrent_paths: "Optional[frozenset[str]]" = None
    classified_session_files: Optional[list[dict[str, Any]]] = None
    measurement_paths = decisions.get("stage_paths")
    if measurement_paths is None:
        known_concurrent_paths = directives_commit_tail.resolve_known_concurrent_paths(root, gate.sid)
        classified_session_files = directives_memo_lifecycle.classify_session_authored_files(
            root, session_start_time, known_concurrent_paths=known_concurrent_paths
        )
        measurement_paths = [
            row["path"] for row in classified_session_files if row["session_authored"]
        ]

    # A (docs/plans/2026-08-08-the-engine-asks-for-facts-it-already-holds.md
    # C-followup, cross-repo/inbox/2026-08-15-example-retrieval-repo-em-wsc-review-
    # trail-skips-silently.md): trail-ready per-commit slices, collected as
    # a side channel of the SAME measurement call below (no second git
    # spawn) — see `_measure_session_review_scale_inputs`'s own
    # `commit_slices_out` docstring paragraph. `None` (never `[]`) means
    # "unresolvable", read off `measured_commit_count is None` below, since
    # this list stays empty either way until that distinction is known.
    review_scale_commit_slices: list[dict[str, Any]] = []
    measured_gross_loc, measured_code_loc, measured_commit_count, measured_surface_count = (
        _measure_session_review_scale_inputs(
            root,
            session_start_time,
            gate.sid,
            uncommitted_paths=measurement_paths,
            commit_slices_out=review_scale_commit_slices,
        )
    )
    resolved_gross_loc = decisions.get("gross_loc")
    if resolved_gross_loc is None:
        resolved_gross_loc = measured_gross_loc
    # C7 (2026-08-12, docs/plans/2026-08-12-review-mandate-guides-the-split.md):
    # `commit_count_scope` is resolved ONLY on the override path (a caller
    # supplied `decisions["commit_count"]` directly) — never on the measured
    # path, where `_measure_session_review_scale_inputs` already guarantees
    # session-scoping and a scope note would be redundant noise on every
    # ordinary close. `"unspecified"` (not `None`) when the override is
    # supplied with no accompanying `decisions["commit_count_scope"]` — the
    # override itself stays ACCEPTED (this is the pre-authorized scope-
    # attestation variant, not a refusal), but the reported failure mode
    # (an EM reading `commits=` off the gate's own unfiltered range line and
    # passing it through with nothing on disk distinguishing that from a
    # real measurement) must now leave a visible, honest trace rather than
    # a silent trust. See `directives_review.decide_review_scale`'s own
    # `commit_count_scope` docstring paragraph for what this feeds.
    resolved_commit_count = decisions.get("commit_count")
    if resolved_commit_count is None:
        resolved_commit_count = measured_commit_count
        resolved_commit_count_scope = None
    else:
        # ACKNOWLEDGED AND DELIBERATE (review-integrator, 2026-08-12): this
        # value is RECORDED, never VERIFIED against `_session_owned_shas` —
        # the override still wins unconditionally regardless of what (or
        # whether) a scope is supplied. An inaccurate `commit_count_scope`
        # (dishonest, stale, or careless copy-paste) reproduces the original
        # peer-commit-inflation incident with the trail record now looking
        # self-diagnosing instead of silent. Gating the override on scope
        # verification is a PM call (changes a documented EM hand-supply
        # interface), not an oversight here.
        raw_commit_count_scope = decisions.get("commit_count_scope")
        if raw_commit_count_scope:
            resolved_commit_count_scope = _sanitize_commit_count_scope(raw_commit_count_scope) or "unspecified"
        else:
            resolved_commit_count_scope = "unspecified"
    resolved_surface_count = decisions.get("surface_count")
    if resolved_surface_count is None:
        resolved_surface_count = measured_surface_count
    # C5 (2026-08-11): `code_loc` is now PRODUCED here, mirroring the
    # gross_loc/commit_count/surface_count backfill above — a caller-
    # supplied `decisions["code_loc"]` still always wins. Before this,
    # `decide_review_scale`'s row 4 (pointed at `code_loc` by C2) received a
    # bare `decisions.get("code_loc")` with no producer at this call site,
    # so row 4 resolved `_row4_inputs_unresolved` in production instead of
    # ever tripping — a live regression C2 alone shipped (see this plan's
    # C5 stub).
    resolved_code_loc = decisions.get("code_loc")
    if resolved_code_loc is None:
        resolved_code_loc = measured_code_loc

    # 2026-08-20 (cross-repo/inbox/2026-08-20-example-retrieval-repo-em-review-gate-doc-
    # only-em-discretion.md, item 3): the count of this session's own commits
    # carrying a zero-line reviewable diff — `baton-assemble apply` scaffolds
    # and prose-only commits, both of which present nothing to a reviewer.
    # Read off the slices `_measure_session_review_scale_inputs` already
    # produced from the same `git show` text (no extra spawn). Left `None`
    # when the measurement itself was unresolvable (`measured_commit_count is
    # None` — the slices list stays empty either way, so it cannot be read as
    # "resolved, zero") and when the caller overrode `commit_count`, since the
    # slices then describe a different population than the override does.
    zero_diff_commit_count: Optional[int] = None
    if measured_commit_count is not None and decisions.get("commit_count") is None:
        zero_diff_commit_count = sum(
            1 for _slice in review_scale_commit_slices if _slice["diff_loc"] == 0
        )

    review_scale_decision = directives_review.decide_review_scale(
        gross_loc=resolved_gross_loc,
        code_loc=resolved_code_loc,
        commit_count=resolved_commit_count,
        zero_diff_commit_count=zero_diff_commit_count,
        surface_count=resolved_surface_count,
        executor_dispatched=decisions.get("executor_dispatched"),
        shared_schema_touched=decisions.get("shared_schema_touched"),
        chain_disposition=gate.disposition,
        commit_count_scope=resolved_commit_count_scope,
    )

    # D (same spec backlink as A above): `directives = build_directives(...)`
    # could not run any earlier than this point — it needs the RESOLVED
    # `review_scale_decision.partition_mandatory` verdict just computed
    # above, threaded through as a dedicated parameter (never a
    # `decisions[...]` key — see `build_directives`'s own `partition_
    # mandatory` docstring paragraph for why).
    directives = build_directives(
        gate,
        decisions,
        root,
        governing_plan=governing_plan,
        session_start_time=session_start_time,
        partition_mandatory=bool(review_scale_decision.partition_mandatory),
    )

    judgment_points: list[dict[str, Any]] = []
    session_shape_jp = build_session_shape_judgment_point(gate)
    if session_shape_jp:
        judgment_points.append(session_shape_jp)
    # `jp-coverage-verdict` (build_coverage_judgment_point) was removed here
    # (K-001, state/kill-ledger.md) along with `d-coverage-gate` itself.
    # `chain_terminal` no longer selects a branch inside the judgment point
    # (K-007, 2026-08-19 — the chain-scoped verdict and its store are gone),
    # but it is still computed and passed: the replacement coverage the PM
    # is specifying for the chain-wide question will need it, and the
    # consumed_handoff leg keeps the flag meaning what it has always meant.
    review_scale_chain_terminal = (
        canonicalize(gate.disposition) == PREDECESSOR_CONSUMED and bool(gate.consumed_handoff)
    )
    review_scale_jp = build_review_scale_judgment_point(
        review_scale_decision,
        chain_terminal=review_scale_chain_terminal,
    )
    if review_scale_jp:
        judgment_points.append(review_scale_jp)
    judgment_points.extend(
        _build_preserved_judgment_points(gate, decisions, root, governing_plan_present=governing_plan is not None)
    )

    # AC3/AC3b/AC4/AC5/AC6 — the plural, per-element consumed-handoff
    # completeness gate. Computed unconditionally (even when `directives`
    # carries no `d-run-wsc-tail` entry) so `gates.consumed_handoff_
    # completeness` always reflects what was evaluated — AC3b's "silence
    # must never represent 'not checked'" applies to the evidence surface
    # itself, not only to the blocking judgment point below.
    consumed_handoff_completeness_gate = compute_consumed_handoff_completeness_gate(
        root, gate.consumed_handoff_paths
    )

    # Authoring-window halts in front of d-run-wsc-tail (state/bug-backlog/
    # 2026-07-28-workstream-complete-apply-re-scaffolds-t-e925d597e0af.yaml)
    # — see the two builders' own docstrings. Both only ever append when
    # `directives` actually carries a `d-run-wsc-tail` entry (always true
    # today, but this mirrors `build_coverage_judgment_point`'s own
    # defensive `any(...)` check rather than assuming the id's presence).
    # AC5's `stage_paths` template pre-fill source (`gates.stage_paths_
    # candidates`) — only ever populated inside the branch below, when this
    # run actually computed a candidate set; stays `None` otherwise (caller
    # already supplied `decisions["stage_paths"]`, or `d-run-wsc-tail` is
    # absent this pass).
    stage_paths_candidates: Optional[list[str]] = None

    if any(d["id"] == "d-run-wsc-tail" for d in directives):
        effective_governing_plan_slug = (
            governing_plan.slug if governing_plan is not None else decisions.get("governing_plan_slug")
        )
        scaffold_fact = directives_completion.compute_completion_entry_scaffold_gate(
            root, gate.sid, effective_governing_plan_slug
        )
        if scaffold_fact is not None:
            judgment_points.append(
                build_completion_entry_scaffold_judgment_point(
                    scaffold_fact.entry_path,
                    scaffold_fact.residue_fields,
                    entry_exists=Path(scaffold_fact.entry_path).is_file(),
                )
            )
            _append_directive_dependency(directives, "d-run-wsc-tail", "jp-completion-entry-scaffold")

        resolved_subject, _resolved_prose = _resolve_commit_message_authoring_fields(decisions)
        if not resolved_subject:
            judgment_points.append(build_commit_subject_missing_judgment_point())
            _append_directive_dependency(directives, "d-run-wsc-tail", "jp-commit-subject-missing")

        if decisions.get("stage_paths") is None:
            # `session_start_time`, the known-concurrent set and the
            # session-authored classification were all resolved once above,
            # for the review-scale measurement — reused here rather than
            # recomputed, since the classification costs one `git log` spawn
            # per dirty path.
            if known_concurrent_paths is None or classified_session_files is None:
                known_concurrent_paths = directives_commit_tail.resolve_known_concurrent_paths(
                    root, gate.sid
                )
                classified_session_files = directives_memo_lifecycle.classify_session_authored_files(
                    root, session_start_time, known_concurrent_paths=known_concurrent_paths
                )
            session_authored_paths = [
                row["path"] for row in classified_session_files if row["session_authored"]
            ]
            candidate_paths = directives_commit_tail.accumulate_session_paths(session_authored_paths)
            stage_paths_candidates = candidate_paths
            judgment_points.append(
                _judgments.build_stage_paths_missing_judgment_point(candidate_paths, known_concurrent_paths)
            )
            _append_directive_dependency(directives, "d-run-wsc-tail", "jp-stage-paths-missing")

    # AC3/AC4 — either leg fires -> emit ONE judgment point, then depend
    # every attribution/tail directive on it. Keyed on `consumed_handoff_
    # completeness_gate.blocks`, which is itself keyed on "a consumed
    # handoff resolved on disk" (see the gate builder's own docstring),
    # never on `gate.disposition`. Lifted out of the `d-run-wsc-tail`
    # nesting above (2026-08-05-session-shape-attribution-structural-gate
    # C3) — the point must be reachable even on a run where d-run-wsc-tail
    # itself is absent, since `override-known-in-flight` now resolves six
    # directives, not just that one. Each `_append_directive_dependency`
    # call is defensively guarded, mirroring `build_coverage_judgment_
    # point`'s own pattern, because not every directive is present on
    # every run (e.g. no governing plan resolved).
    if consumed_handoff_completeness_gate.blocks:
        judgment_points.append(
            build_consumed_handoff_completeness_judgment_point(consumed_handoff_completeness_gate)
        )
        for _gated_directive_id in (
            "d-run-wsc-tail",
            "d-claim-plan-execution-lock",
            "d-stamp-plan-implemented",
            "d-harvest-deferrals-1",
            "d-complete-entry",
            "d-reconcile-completion-commits",
        ):
            if any(d["id"] == _gated_directive_id for d in directives):
                _append_directive_dependency(directives, _gated_directive_id, "jp-consumed-handoff-completeness")

    completeness_gate = directives_session_hygiene.compute_completeness_checklist_gate(
        gate.disposition,
        _read_consumed_handoff_text(root, gate),
        consumed_handoff_basename=Path(gate.consumed_handoff).name if gate.consumed_handoff else "",
        decisions=decisions,
    )

    # `directives_spine_worklist.compute_open_spine_row_gate` itself stays
    # purely advisory (its own docstring/Negative-spec: "Does NOT block" —
    # unchanged here, and this call site still feeds `warn_text`/
    # `summary_line` into `gates.open_spine_row_worklist` below exactly as
    # before). What changed: THIS assembly layer now promotes the gate's
    # unwaived-open-rows fact into a blocking judgment point gating
    # `d-stamp-plan-implemented` specifically (see
    # `build_open_spine_rows_block_stamp_judgment_point` below) — mirroring
    # `judgments.py`'s own division of labour (module computes the fact,
    # `__init__.py` decides which facts gate a directive). Closes a live
    # incident: `docs/plans/2026-08-15-composition-invocation-budgets.md`
    # was stamped `executing -> implemented` (commit `1ee373668`) with row
    # C2 still `disposition: open` (state/kill-ledger.md K-003) — the
    # `landed <-> implemented` schema distinction
    # (`coordinator_core/frontmatter/schemas/plan.schema.json`) exists
    # precisely to separate "code is on the branch" from "every spine row
    # reached a disposition", and the stamp directive reached past it
    # ungated. `open_spine_row_gate.warn_text` already excludes waived rows
    # (`decisions["waived_open_spine_row_ids"]`) — a PM-ruled, genuinely
    # carried-open row clears the block the same way it already clears the
    # advisory WARN, no new disposition needed. The trigger keys on
    # `verdict`, not `warn_text` alone: `warn_text` is also `None` on
    # `verdict: indeterminate` (plan unreadable, spine fence malformed),
    # and that case must still gate — an unreadable spine is the one
    # condition this module can least afford to wave the terminal stamp
    # through on.
    open_spine_row_gate = directives_spine_worklist.compute_open_spine_row_gate(
        governing_plan.slug if governing_plan else None,
        governing_plan.path if governing_plan else None,
        decisions=decisions,
    )
    _open_spine_row_gate_blocks = open_spine_row_gate.verdict == "indeterminate" or (
        open_spine_row_gate.verdict == "applicable" and open_spine_row_gate.warn_text is not None
    )
    if _open_spine_row_gate_blocks and any(
        d["id"] == "d-stamp-plan-implemented" for d in directives
    ):
        judgment_points.append(build_open_spine_rows_block_stamp_judgment_point(open_spine_row_gate))
        _append_directive_dependency(directives, "d-stamp-plan-implemented", "jp-open-spine-rows-block-stamp")

    # C3, pln-landed-fires-at-spine-resoluti-ac7e89 (AC9): `compute_landed_
    # reconciliation_gate` itself stays read-only/degrade-never-raise, same
    # as `open_spine_row_gate` above. What changed (fourth-instance-hunt
    # item 1, Layer A): THIS assembly layer now promotes a non-"not-
    # applicable" verdict into a blocking judgment point gating
    # `d-stamp-plan-implemented`, mirroring the open-spine wiring
    # immediately above it line for line -- see `build_landed_
    # reconciliation_block_stamp_judgment_point`'s own docstring for the
    # verdict-keyed/indeterminate-blocks/single-derivation rules this
    # carries forward. Closes the gap the previous comment here named: a
    # governing plan deliberately parked at `status: landed` with unticked
    # ACs used to reach `d-stamp-plan-implemented` ungated, because the
    # spine gate above is silent by construction once every row has left
    # `open` (the same state `landed` requires) and this was the only
    # remaining contradicting signal, advisory-only.
    landed_reconciliation_gate = compute_landed_reconciliation_gate(
        governing_plan.slug if governing_plan else None,
        governing_plan.path if governing_plan else None,
    )
    _landed_reconciliation_gate_blocks = landed_reconciliation_gate.verdict != "not-applicable"
    if _landed_reconciliation_gate_blocks and any(
        d["id"] == "d-stamp-plan-implemented" for d in directives
    ):
        judgment_points.append(
            build_landed_reconciliation_block_stamp_judgment_point(landed_reconciliation_gate)
        )
        _append_directive_dependency(
            directives, "d-stamp-plan-implemented", "jp-landed-reconciliation-block-stamp"
        )

    # C2 (docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-
    # being-questions.md): route every judgment point assembled above
    # through C1's shared predicate. Asked points (including every point
    # whose id is named in a directive's `depends_on`, and every point
    # whose disposition resolves a directive actually on THIS envelope)
    # stay in `judgment_points[]`, unchanged. Reported points (gate no
    # directive present here) are demoted out of `judgment_points[]` --
    # `decisions_template` and the envelope's own `judgment_points=` below
    # both read the post-partition (asked-only) list, since a reported
    # point is no longer a question a caller needs to answer -- and folded
    # into `narration` instead, via `_narration_and_next_move` below. This
    # is narration-demotion only: no `decisions=` pre-population, no 9th
    # envelope key -- see the plan's "Forked out of this plan" section.
    _asked_judgment_points, _reported_judgment_points = partition_reportable(judgment_points, directives)
    # `partition_reportable` classifies by directive-membership alone, with
    # no opinion on `recommendation` presence -- an untrusted-gate point
    # (`recommendation=None`, e.g. `jp-session-shape` and `jp-review-scale`'s
    # three untrusted branches) can land in `reported` there too, purely
    # because its dispositions' `resolves` happen to be empty. This plan's
    # premise (Anti-scope: "do NOT add a recommendation to jp-review-scale's
    # three unresolved/untrusted-gate branches... the three... are correctly
    # untouched, by construction") is narration-demotion for
    # RECOMMENDATION-carrying points only -- an untrusted-gate point has no
    # recommendation to fold into narration and stays a judgment point
    # regardless of what `partition_reportable` computed for it.
    #
    # `_RESOLVER_BACKED_OUT_OF_SCOPE_IDS` mirrors the identical exemption in
    # `contract/decision_object/tests/test_reportable_partition.py` (C1's
    # own census harness): these five points build `resolves` through a
    # resolver that returns real directive ids once its `decisions` slice is
    # populated, and `[]` only on an empty slice -- a single call's empty
    # `decisions` must not demote them (Anti-scope: "classifying them by a
    # single observation re-introduces the over-count this plan's Problem
    # section documents").
    reported_judgment_points = [
        jp
        for jp in _reported_judgment_points
        if jp.get("recommendation") is not None and jp["id"] not in _RESOLVER_BACKED_OUT_OF_SCOPE_IDS
    ]
    _reported_ids = {jp["id"] for jp in reported_judgment_points}
    judgment_points = [jp for jp in judgment_points if jp["id"] not in _reported_ids]

    narration, next_move = _narration_and_next_move(
        gate, directives, judgment_points, reported_judgment_points
    )

    # `detection` defaults to `None` (no shared mutable default); the wire
    # shape for "no structured detection" stays `{}`, so consumers never have
    # to distinguish null from empty.
    session_shape_fact = {**gate._asdict(), "detection": dict(gate.detection or {})}
    # AC3: a supplied `jp-session-shape` decision recomputes into the
    # emitted `disposition` field on this same re-`brief` — see
    # `_session_shape_disposition_from_decisions` for why (a resolved
    # judgment point must read resolved, not indistinguishable from a
    # discarded one). Only the reported fact changes; `gate` itself, and
    # every earlier `canonicalize(gate.disposition)` read above, are
    # untouched.
    _decided_session_shape_disposition = _session_shape_disposition_from_decisions(decisions)
    if _decided_session_shape_disposition is not None:
        session_shape_fact["disposition"] = _decided_session_shape_disposition

    # AC5 — the ONLY three free-value keys `build_decisions_template`
    # pre-fills from data this SAME run already resolved (`DECISIONS_
    # TEMPLATE_RESOLVED_KEY_ENVELOPE_PATHS`), threaded from the SAME local
    # variables the envelope below reads for `preflight.governing_plan_
    # resolution`/`gates.stage_paths_candidates` — never re-derived a second
    # time here.
    resolved_free_values = {
        "governing_plan_slug": governing_plan.slug if governing_plan else None,
        "governing_plan_path": str(governing_plan.path) if governing_plan else None,
        "stage_paths": stage_paths_candidates,
    }

    # A (spec backlink above): a trail-ready `review_scale.commit_slices`
    # payload, additive to `review_scale_decision._asdict()` — never a
    # replacement key, and never emitted when the underlying measurement
    # was unresolvable (`measured_commit_count is None`; see
    # `_measure_session_review_scale_inputs`'s own None-vs-zero contract).
    # A resolved-but-empty list (session owns zero commits) IS emitted —
    # that is an honest answer, not a failure. `scope_kind="diff"` on
    # every entry: each slice is a real code sha_range, never a plan/
    # integration record. The caller fills reviewer/scope/verdict per
    # entry and passes the list straight through as `decisions["review"]`
    # (`directives_commit_tail.build_close_tail_args_directive`'s existing
    # list branch already consumes this exact shape as `--review-slice`).
    review_scale_payload = review_scale_decision._asdict()
    if measured_commit_count is not None:
        for _slice in review_scale_commit_slices:
            _slice["scope_kind"] = "diff"
        review_scale_payload["commit_slices"] = review_scale_commit_slices
        # Uncommitted work has no sha and so can never become a slice — said
        # here rather than silently dropped: the gap between measured
        # `code_loc` and the sum of the slices' own `diff_loc` is exactly
        # the uncommitted contribution no slice covers.
        _sliced_code_loc = sum(s["diff_loc"] for s in review_scale_commit_slices)
        review_scale_payload["uncommitted_code_loc"] = (
            max(0, measured_code_loc - _sliced_code_loc) if measured_code_loc is not None else None
        )


    # C2, pln-one-completion-verdict-for-wor-ea96e2: `gates.completion_
    # verdict` — one rollup over the five gate payloads just built above,
    # via `completion_verdict.py`'s per-gate readers (AC2) and its
    # `compose_completion_verdict` (AC1). Every reader is handed that
    # gate's own SHALLOW `._asdict()` — the same dict already passed into
    # `gates={...}` below, never the raw NamedTuple — for consistency with
    # what the envelope itself emits; `_row_reference`/
    # `_completeness_item_field` in that module tolerate nested NamedTuples
    # either way. AC7: reads already-computed payloads only, calls no
    # gate-computation function.
    _completion_verdict_readings = {
        "completeness_checklist": _completion_verdict.completeness_checklist(completeness_gate._asdict()),
        "open_spine_row_worklist": _completion_verdict.open_spine_row_worklist(open_spine_row_gate._asdict()),
        "consumed_handoff_completeness": _completion_verdict.consumed_handoff_completeness(
            consumed_handoff_completeness_gate._asdict()
        ),
        "landed_reconciliation": _completion_verdict.landed_reconciliation(landed_reconciliation_gate._asdict()),
        "review_scale": _completion_verdict.review_scale(review_scale_payload),
    }
    completion_verdict_payload = _completion_verdict.compose_completion_verdict(_completion_verdict_readings)

    envelope = build_envelope(
        artifact={"path": str(root), "classification": "workstream", "frontmatter": {}},
        preflight={
            "session_shape": session_shape_fact,
            "consumes_manifest": list(CONSUMES_MANIFEST),
            "governing_plan_resolution": {
                "source": governing_plan_source,
                "slug": governing_plan.slug if governing_plan else None,
                "path": str(governing_plan.path) if governing_plan else None,
            },
            "decisions_template": build_decisions_template(judgment_points, resolved_free_values),
        },
        gates={
            "session_shape": session_shape_fact,
            "completeness_checklist": completeness_gate._asdict(),
            "open_spine_row_worklist": open_spine_row_gate._asdict(),
            "landed_reconciliation": landed_reconciliation_gate._asdict(),
            "consumed_handoff_completeness": consumed_handoff_completeness_gate._asdict(),
            "review_scale": review_scale_payload,
            "stage_paths_candidates": stage_paths_candidates,
            "repo_identity": repo_identity_gate,
            "completion_verdict": completion_verdict_payload,
        },
        directives=directives,
        judgment_points=judgment_points,
        decisions=decisions,
        narration=narration,
        next_move=next_move,
    )
    return emit(envelope)


# ---------------------------------------------------------------------------
# CLI entrypoint — two subcommands, one trampoline (AC9)
# ---------------------------------------------------------------------------


def _usage(prog: str) -> int:
    print(f"usage: {prog} brief [--decisions <json> | --decisions-file <path>]", file=sys.stderr)
    print(f"       {prog} apply [--decisions <json> | --decisions-file <path>]", file=sys.stderr)
    return EXIT_USAGE


def _main_brief(rest: list[str]) -> int:
    decisions: dict[str, Any] = {}
    conflict = detect_conflicting_payload_channels(rest)
    if conflict is not None:
        print(f"workstream-complete-assemble: {conflict}", file=sys.stderr)
        return EXIT_USAGE
    i = 0
    while i < len(rest):
        tok = rest[i]
        if (payload := resolve_json_payload_flag(rest, i)).consumed:
            if payload.error is not None:
                print(f"workstream-complete-assemble: {payload.error}", file=sys.stderr)
                return EXIT_USAGE
            decisions = payload.value
            i += payload.consumed
        else:
            print(f"workstream-complete-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return EXIT_USAGE

    try:
        decision_object = brief(decisions)
    except TransportFailure as exc:
        print(f"workstream-complete-assemble: transport failure: {exc}", file=sys.stderr)
        failure = emit(
            build_envelope(
                narration=f"Could not compute a brief: {exc}.",
                next_move="Confirm the command is run from inside a git worktree, then retry.",
            )
        )
        print(json.dumps(dict(failure)))
        return EXIT_TRANSPORT_FAIL
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors pickup_assemble Finding 4b
        print(f"workstream-complete-assemble: unexpected failure: {exc}", file=sys.stderr)
        failure = emit(
            build_envelope(
                narration=f"brief() raised an unexpected exception: {exc}.",
                next_move=(
                    "Re-run; if this repeats, report the traceback — this is a structural "
                    "backstop firing, not an enumerated failure mode."
                ),
            )
        )
        print(json.dumps(dict(failure)))
        return EXIT_TRANSPORT_FAIL

    try:
        print(json.dumps(dict(decision_object), indent=2, sort_keys=True))
    except (TypeError, ValueError) as exc:
        print(f"workstream-complete-assemble: could not serialize decision object: {exc}", file=sys.stderr)
        failure = emit(
            build_envelope(
                narration="The computed decision object could not be serialized to JSON.",
                next_move="Report this — a non-serializable decision object is a defect in the assembler.",
            )
        )
        print(json.dumps(dict(failure)))
        return EXIT_TRANSPORT_FAIL
    return EXIT_OK


def _main_apply(rest: list[str]) -> int:
    """Delegates to `coordinator_core.workstream_complete.apply.main` (C4).
    Imported lazily (not at module load time) so `brief` remains usable
    before `apply.py` lands — this chunk (C3) wires the `apply` subcommand
    shape per AC9; C4 authors the module it dispatches to."""
    try:
        from coordinator_core.workstream_complete.apply import main as apply_main
    except ImportError as exc:
        print(f"workstream-complete-assemble: apply half not available: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT_FAIL
    return apply_main(rest)


def main(argv: list[str]) -> int:
    if not argv:
        return _usage("workstream-complete-assemble")

    if argv[0] in ("--help", "-h"):
        print("usage: workstream-complete-assemble brief [--decisions <json> | --decisions-file <path>]")
        print("       workstream-complete-assemble apply [--decisions <json> | --decisions-file <path>]")
        return EXIT_OK

    subcmd, rest = argv[0], argv[1:]

    if subcmd == "brief":
        return _main_brief(rest)
    if subcmd == "apply":
        return _main_apply(rest)

    print(f"workstream-complete-assemble: unknown subcommand {subcmd!r}", file=sys.stderr)
    return _usage("workstream-complete-assemble")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
