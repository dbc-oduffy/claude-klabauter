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

Contract (frozen, reviewed): example-doctrine-repo coordinator/docs/wiki/computed-skills.md
Branches computed against: example-doctrine-repo coordinator/skills/workstream-complete/SKILL.md
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
    coordinator_core.pickup_assemble.resolve_repo_root -> AC8's zero-spawn
        `.git` read-model repoint (see Negative-spec for what this replaces).

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
      resolve_repo_root` (AC8) — the zero-spawn in-process `.git` read-model,
      not a local `subprocess.run(["git", "rev-parse", ...])` call. Convert
      #2 shipped the plain-subprocess version and flagged this repoint as
      "out of scope for this chunk" in its own docstring; C3 closes it.
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
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, NamedTuple, Optional

from coordinator_core.contract.decision_object.envelope import build_envelope, emit
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
    build_untrusted_gate_judgment_point,
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
from coordinator_core.ops.deliverable_equivalence import (
    canonicalize as canonicalize_deliverable_id,
)
from coordinator_core.ops.deliverable_equivalence import load_equivalence_map
from coordinator_core.ops.fleet._common import handoff_archive_dest
from coordinator_core.pickup_assemble import resolve_repo_root  # AC8: zero-spawn `.git` read-model
from coordinator_core.resolution.facade import resolve_operator_config

from coordinator_core.workstream_complete import chain_partition_verdict_store
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
    "chain_partition_verdict",
    "classify_dispatch_plan_file",
    "code_loc",
    "commit_count",
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


def build_decisions_template(judgment_points: list[dict[str, Any]]) -> dict[str, Any]:
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
        never hand-copied here.

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
    for key in _all_free_value_keys():
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


def compute_session_shape_gate(repo_root: Path) -> SessionShapeGate:
    """Steps 0 of `/workstream-complete`: resolve this session's id and
    chain-terminal-vs-single-session disposition via the ported detector
    chain in `wsc-session-disposition.py`, folded into one gate fact."""
    mod = _load_session_disposition_module()
    sid = mod.resolve_session_id(repo_root)
    resolution = mod.resolve_disposition(repo_root, sid)
    disposition, consumed_handoff, diagnostics, consumed_handoff_paths = resolution
    return SessionShapeGate(
        sid=sid,
        disposition=disposition,
        consumed_handoff=consumed_handoff,
        diagnostics=diagnostics,
        consumed_handoff_paths=tuple(consumed_handoff_paths),
        # `.detection` is an ATTRIBUTE on that module's tuple subclass, not a
        # fifth element — the unpack above stays four-wide on purpose (see
        # `DispositionResolution`). `getattr` with a default so an older
        # copy of the bin script on a partially-updated tree degrades to
        # "no structured detection" rather than raising.
        detection=dict(getattr(resolution, "detection", None) or {}),
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
        id="jp-session-shape",
        question=(
            f"Session-shape resolved to {gate.disposition!r}, but the detector chain "
            "flagged an unresolved case below — is that resolution actually correct?"
        ),
        dispositions=[
            # AC2b: the canonical spelling and the legacy spelling are
            # emitted as TWO disposition entries with IDENTICAL resolves —
            # ceremony_common/apply_halt._disposition_resolves_directive's
            # ordinary value-match then clears d-coverage-gate for either
            # spelling an EM types, with zero change to that cross-family
            # shared predicate (see wsc_disposition module docstring).
            build_disposition(PREDECESSOR_CONSUMED, resolves=["d-coverage-gate"]),
            build_disposition(LEGACY_PREDECESSOR_CONSUMED, resolves=["d-coverage-gate"]),
            build_disposition(SINGLE_SESSION, resolves=[]),
            # AC4/plan Execution Notes: offered so an EM correcting a wrong
            # Detector C attribution has the true answer available whenever
            # this point fires at all — not conditioned on which leg
            # actually decided this resolution. `resolves=["d-coverage-
            # gate"]` is FORCED, not chosen: `_build_legacy_coverage_and_
            # trail_directives` only builds `d-coverage-gate` when
            # `canonicalize(disposition) == PREDECESSOR_CONSUMED` AND
            # `gate.consumed_handoff` is non-empty, and `consumed_handoff`
            # is empty on the memo leg — so "the gate runs" is not
            # implementable any other way (plan § Problem (2) PM ruling).
            build_disposition(MEMO_PREDECESSOR, resolves=["d-coverage-gate"]),
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
) -> dict[str, Any]:
    return {"id": id_, "cli": cli, "args": args, "depends_on": depends_on, "already_satisfied": already_satisfied}


def _build_legacy_coverage_and_trail_directives(
    gate: SessionShapeGate,
    decisions: dict[str, Any],
    plan_claim_directives: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The pre-existing (Convert #2) `d-coverage-gate` / `d-write-trail`
    pair, `depends_on` repointed onto `directives_lessons_plan.py`'s
    `d-claim-plan-execution-lock` where the original pointed at the now-
    superseded `d-claim-plan` (see module Negative-spec)."""
    directives: list[dict[str, Any]] = []
    claim_id = next((d["id"] for d in plan_claim_directives if d["id"] == "d-claim-plan-execution-lock"), None)

    if canonicalize(gate.disposition) == PREDECESSOR_CONSUMED and gate.consumed_handoff:
        directives.append(
            _directive(
                "d-coverage-gate",
                "wsc-coverage-gate-runner",
                ["coverage-gate", "--from-handoff", gate.consumed_handoff],
                depends_on=claim_id,
            )
        )

    review = decisions.get("review") or {}
    _REVIEW_REQUIRED = ("sha_range", "reviewer", "scope", "verdict", "diff_loc")
    if all(review.get(k) not in (None, "") for k in _REVIEW_REQUIRED):
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
        coverage_gate_id = next((d["id"] for d in directives if d["id"] == "d-coverage-gate"), None)
        directives.append(_directive("d-write-trail", "wsc-coverage-gate-runner", args, depends_on=coverage_gate_id))

    return directives


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
    directives.extend(_build_legacy_coverage_and_trail_directives(gate, decisions, plan_claim_directives))

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
    # example-doctrine-repo-em memo, `cross-repo/inbox/2026-08-03-example-doctrine-repo-em-wsc-
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
    if canonicalize(gate.disposition) != PREDECESSOR_CONSUMED or not gate.consumed_handoff:
        directives.append(directives_review.build_review_brightline_gate_directive(gate.sid))
    else:
        directives.append(directives_review.build_chain_plan_brightline_gate_directive(gate.consumed_handoff))
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
    directives.append(directives_commit_tail.build_wsc_tail_directive(gate.sid, effective_decisions))
    # `d-archive-session-claim` is DELIBERATELY NOT emitted here (2026-07-28).
    # This ceremony fires once per closed workstream, and a session can
    # close several workstreams before it ends — but `scope.archive()` moves
    # the whole live session claim dir, which is a once-per-SESSION-END
    # operation. Emitting it here archived a still-live session mid-session,
    # destroying once-per-session sentinels and the dispatch-evidence file.
    # Archival now belongs to session END, not workstream close — wired via
    # a SessionEnd hook (example-doctrine-repo repo) rather than this assembly. The
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


def build_coverage_judgment_point(gate: SessionShapeGate, directives: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """When this run is chain-terminal, `d-coverage-gate`'s VERDICT is only
    known at run time (COVERED/UNCOVERED/INDETERMINATE) — this assembler
    cannot pre-resolve it from disk. Surfaces the run-then-check obligation
    as a trusted recommendation (this module IS trusted to recommend
    running the named directive; it is not trusted to guess the verdict)."""
    if not any(d["id"] == "d-coverage-gate" for d in directives):
        return None
    # ADVISORY, not an enforced lock, by deliberate PM ruling (2026-07-27):
    # the commit tail (`d-run-wsc-tail`) carries no dependency edge on this
    # judgment point or on `d-coverage-gate`, and none of the dispositions
    # below resolve it. See `state/lessons/2026-07-27-verify-a-gate-
    # actually-enforces-before-s-a20579f1aa06.yaml` (evidence 87578a31 in
    # claude-klabauter, e5f7b47c in example-doctrine-repo) — do not re-derive this as a
    # bug or wire a dependency edge here without a fresh PM decision.
    return build_judgment_point(
        {
            "disposition": "uncovered-or-indeterminate-proceed-with-warning",
            "rationale": (
                "This gate is advisory: an UNCOVERED/INDETERMINATE verdict does not block "
                "the commit tail — it is the EM's judgment whether to stop and reconcile "
                "before proceeding."
            ),
        },
        id="jp-coverage-verdict",
        question="Has d-coverage-gate run, and if so did it return COVERED?",
        dispositions=[
            build_disposition("covered", resolves=["d-write-trail"]),
            build_disposition("uncovered-or-indeterminate-override", resolves=["d-write-trail"]),
            build_disposition("uncovered-or-indeterminate-proceed-with-warning", resolves=[]),
        ],
        evidence="directives[] entry with id == 'd-coverage-gate'",
        reason=(
            "The chain-end coverage gate's VERDICT (COVERED/UNCOVERED/INDETERMINATE) is a "
            "runtime fact review-coverage-gate.py computes, not something this read-only "
            "assembler can pre-resolve from disk."
        ),
    )


def build_review_scale_judgment_point(decision: directives_review.ReviewScaleDecision) -> Optional[dict[str, Any]]:
    """Surfaces `decide_review_scale`'s verdict — otherwise dead code with
    no call site (source memo 2026-08-03-example-doctrine-repo-em-wsc-chain-terminal-
    brightline-gate-never-fires.md). Sits BESIDE `review-partition-strategy`
    / `reviewer-count-on-oracle-disagreement` (the Staff Engineer finding 13): it does
    not feed their inputs and is not gated on `review_relevant` — read only
    via `gates['review_scale']`, independent of both.

    ADVISORY, not an enforced lock, by deliberate PM ruling (2026-07-27,
    the same ruling `build_coverage_judgment_point` carries): the commit
    tail (`d-run-wsc-tail`) carries no dependency edge on this judgment
    point. See DR-068 ("Commit-Time Coverage Gate — ... Advisory-Not-
    Blocking") and example-doctrine-repo coordinator/docs/wiki/workstream-complete-
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
    chain-terminal close typically means the brightline gate already
    computed a `PARTITION-MANDATORY` verdict that a prior call simply
    failed to carry forward (`decisions["chain_partition_verdict"]` never
    re-supplied) — this assembler recommending `proceed-unresolved` in that
    exact case routed the EM around its own mandatory-partition gate
    precisely because the gate's own verdict went missing. `decide_review_
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
    "Commit-Time Coverage Gate — ... Advisory-Not-Blocking") and example-doctrine-repo
    coordinator/docs/wiki/workstream-complete-review.md, section "The gate
    is an oracle, not a lock" — do not re-derive this as a bug or wire a
    dependency edge here without a fresh PM decision. Removing the
    recommendation changes what is *offered* on the unresolved path, never
    whether the point blocks.
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
        )
    return build_untrusted_gate_judgment_point(
        id="jp-review-scale",
        question="What review scale does decide_review_scale select for this close, and is it resolved?",
        dispositions=[
            build_disposition("proceed-unresolved", resolves=[]),
        ],
        evidence="gates['review_scale'] (decide_review_scale's ReviewScaleDecision)",
        reason=(
            f"review scale unresolved: {decision.reason} — an unresolved chain-terminal "
            "decision typically means the brightline gate already computed a "
            "PARTITION-MANDATORY verdict that was not carried forward; recommending "
            "proceed-unresolved here would route around that gate's own missing verdict "
            "(cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-mandatory-"
            "does-not-halt.md, mechanism 3)."
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
    entry_path: str, residue_fields: tuple[str, ...]
) -> dict[str, Any]:
    """Blocks `d-run-wsc-tail` until the `d-complete-entry` scaffold at
    `entry_path` has been hand-authored. SKILL.md's own "Resolving these
    two judgment points is not the last step" paragraph names this exact
    authoring window as mandatory before the commit-tail keystone may
    fire — a single `apply` pass previously fired `d-complete-entry` and
    `d-run-wsc-tail` back to back with no window between them for the EM
    to write anything."""
    fields = ", ".join(residue_fields)
    return build_untrusted_gate_judgment_point(
        id="jp-completion-entry-scaffold",
        question=(
            f"The completion entry at {entry_path!r} still carries placeholder "
            f"{fields} — has it been hand-authored yet?"
        ),
        dispositions=[build_disposition("not-yet-authored", resolves=[])],
        evidence=f"{entry_path}'s own frontmatter/body — still-placeholder: {fields}",
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
            "guaranteed exit-2 usage failure, never a legitimate tail-item soft-fail."
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
    `deliverable_id` matches it, canonicalizing both sides through the
    declared-fork equivalence map (`deliverable_equivalence.canonicalize`)
    — the SAME primary-key join `draft_plan_aging.resolve_plan_owner`
    performs in the opposite direction (plan -> owning handoff), so a
    plan/handoff pair split across a declared fork's winner/loser legs
    still joins here too.

    Returns `None` when no plan carries a matching `deliverable_id`, or
    when more than one does — an ambiguous join is not this function's
    call to arbitrate; it degrades to "unresolved" the same as no match at
    all. Never raises: an unreadable/non-UTF-8 plan file is skipped, not
    fatal to the scan."""
    plans_dir = root / "docs" / "plans"
    if not plans_dir.is_dir():
        return None
    equivalence_map = load_equivalence_map(root)
    canonical_target = canonicalize_deliverable_id(deliverable_id, equivalence_map)

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
        if canonicalize_deliverable_id(plan_deliverable_id, equivalence_map) == canonical_target:
            matches.append(plan_path)

    if len(matches) != 1:
        return None
    return matches[0]


def _evaluate_session_handoff_leg_a(root: Path, frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Leg A for `kind: session-handoff` batons only — that kind is not
    built to carry its own `## Acceptance criteria` (0/34 in example-doctrine-repo's
    corpus, 0/22 in claude-klabauter's; cross-repo/inbox/2026-08-03-example-doctrine-repo-em-
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

    # Step 2.7/2.7b — predecessor distill-fate, chain-terminal only.
    if predecessor_present:
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
    degrades to `None` (each caller's own not-applicable branch), matching
    every other absent-input case in this module's convention."""
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
    gate: SessionShapeGate, directives: list[dict[str, Any]], judgment_points: list[dict[str, Any]]
) -> tuple[str, str]:
    narration = (
        f"Session {gate.sid} resolved disposition={gate.disposition!r} "
        f"({len(directives)} directive(s) computed, {len(judgment_points)} judgment point(s) open)."
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


def brief(decisions: Optional[dict[str, Any]] = None, repo_root: Optional[Path] = None) -> Mapping[str, Any]:
    """Computes the `/workstream-complete` decision object for the current
    (or a caller-supplied) repo root. Read-only throughout; mutates nothing.
    Raises `TransportFailure` on a repo-root or bin-module resolution
    failure — callers (the CLI `main`) degrade that to `EXIT_TRANSPORT_FAIL`.
    """
    decisions = decisions or {}
    root = repo_root or resolve_repo_root()
    if root is None:
        raise TransportFailure("could not resolve a git worktree root")

    gate = compute_session_shape_gate(root)
    handoff_governing_plan_field = _governing_plan_field_from_consumed_handoff(root, gate)
    handoff_deliverable_id = _deliverable_id_from_consumed_handoff(root, gate)
    governing_plan, governing_plan_source = directives_lessons_plan.resolve_governing_plan_with_source(
        root, decisions, handoff_governing_plan_field, handoff_deliverable_id
    )
    directives = build_directives(gate, decisions, root, governing_plan=governing_plan)

    # AC1/AC2/AC4 — the seven row-4/5/6 inputs are caller-supplied `decisions`
    # facts, never assembler-computed (brief() is read-only and budgeted; see
    # this plan's Anti-scope on git subprocess calls). Absent means
    # unresolved, never defaulted to a value that changes the row selected
    # (C1's tri-state) — `decisions.get(...)` already returns `None` on
    # absence, which is exactly `decide_review_scale`'s "not yet resolved"
    # sentinel for every one of these seven params.
    #
    # `chain_partition_verdict` is the one exception to "caller-supplied
    # only": an explicit `decisions["chain_partition_verdict"]` ALWAYS wins
    # (never overridden by disk); when `decisions` omits it, fall back to the
    # persisted record `wsc-coverage-gate-runner.py::cmd_brightline_gate`
    # writes via `chain_partition_verdict_store` (root cause fix, cross-repo/
    # inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-mandatory-does-
    # not-halt.md "mechanism 2"). This is still a pure READ — `read_verdict_
    # record` never writes and fails closed (returns None, never a
    # fabricated verdict) on any absent/corrupt/foreign-session/mismatched-
    # provenance record; see that function's own docstring. The fallback is
    # skipped entirely when `decisions` already supplies a value, so every
    # existing test that passes `decisions=` keeps passing unchanged.
    chain_partition_verdict = decisions.get("chain_partition_verdict")
    if chain_partition_verdict is None:
        chain_partition_verdict = chain_partition_verdict_store.read_verdict_record(
            root,
            session_id=gate.sid,
            expected_from_handoff=gate.consumed_handoff or None,
        )

    review_scale_decision = directives_review.decide_review_scale(
        gross_loc=decisions.get("gross_loc"),
        code_loc=decisions.get("code_loc"),
        commit_count=decisions.get("commit_count"),
        surface_count=decisions.get("surface_count"),
        executor_dispatched=decisions.get("executor_dispatched"),
        shared_schema_touched=decisions.get("shared_schema_touched"),
        chain_disposition=gate.disposition,
        chain_partition_verdict=chain_partition_verdict,
    )

    judgment_points: list[dict[str, Any]] = []
    session_shape_jp = build_session_shape_judgment_point(gate)
    if session_shape_jp:
        judgment_points.append(session_shape_jp)
    coverage_jp = build_coverage_judgment_point(gate, directives)
    if coverage_jp:
        judgment_points.append(coverage_jp)
    review_scale_jp = build_review_scale_judgment_point(review_scale_decision)
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
    if any(d["id"] == "d-run-wsc-tail" for d in directives):
        effective_governing_plan_slug = (
            governing_plan.slug if governing_plan is not None else decisions.get("governing_plan_slug")
        )
        scaffold_fact = directives_completion.compute_completion_entry_scaffold_gate(
            root, gate.sid, effective_governing_plan_slug
        )
        if scaffold_fact is not None:
            judgment_points.append(
                build_completion_entry_scaffold_judgment_point(scaffold_fact.entry_path, scaffold_fact.residue_fields)
            )
            _append_directive_dependency(directives, "d-run-wsc-tail", "jp-completion-entry-scaffold")

        resolved_subject, _resolved_prose = _resolve_commit_message_authoring_fields(decisions)
        if not resolved_subject:
            judgment_points.append(build_commit_subject_missing_judgment_point())
            _append_directive_dependency(directives, "d-run-wsc-tail", "jp-commit-subject-missing")

        if not decisions.get("stage_paths"):
            session_start_time = directives_memo_lifecycle.resolve_session_start_time(root, gate.sid)
            known_concurrent_paths = directives_commit_tail.resolve_known_concurrent_paths(root, gate.sid)
            classified = directives_memo_lifecycle.classify_session_authored_files(
                root, session_start_time, known_concurrent_paths=known_concurrent_paths
            )
            session_authored_paths = [row["path"] for row in classified if row["session_authored"]]
            candidate_paths = directives_commit_tail.accumulate_session_paths(session_authored_paths)
            judgment_points.append(_judgments.build_stage_paths_missing_judgment_point(candidate_paths))
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

    # AC4 — advisory only, per docs/plans/2026-08-05-wsc-open-spine-row-
    # worklist.md: this gate contributes no judgment point and no directive
    # dependency edge anywhere in this module — its `warn_text`/
    # `summary_line` feed only `gates.open_spine_row_worklist` below.
    open_spine_row_gate = directives_spine_worklist.compute_open_spine_row_gate(
        governing_plan.slug if governing_plan else None,
        governing_plan.path if governing_plan else None,
        decisions=decisions,
    )

    narration, next_move = _narration_and_next_move(gate, directives, judgment_points)

    # `detection` defaults to `None` (no shared mutable default); the wire
    # shape for "no structured detection" stays `{}`, so consumers never have
    # to distinguish null from empty.
    session_shape_fact = {**gate._asdict(), "detection": dict(gate.detection or {})}

    envelope = build_envelope(
        artifact={"path": str(root), "classification": "workstream", "frontmatter": {}},
        preflight={
            "session_shape": session_shape_fact,
            "consumes_manifest": list(CONSUMES_MANIFEST),
            "governing_plan_resolution": {
                "source": governing_plan_source,
                "slug": governing_plan.slug if governing_plan else None,
            },
            "decisions_template": build_decisions_template(judgment_points),
        },
        gates={
            "session_shape": session_shape_fact,
            "completeness_checklist": completeness_gate._asdict(),
            "open_spine_row_worklist": open_spine_row_gate._asdict(),
            "consumed_handoff_completeness": consumed_handoff_completeness_gate._asdict(),
            "review_scale": review_scale_decision._asdict(),
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
    print(f"usage: {prog} brief [--decisions <json>]", file=sys.stderr)
    print(f"       {prog} apply [--decisions <json>]", file=sys.stderr)
    return EXIT_USAGE


def _main_brief(rest: list[str]) -> int:
    decisions: dict[str, Any] = {}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--decisions":
            if i + 1 >= len(rest):
                return _usage("workstream-complete-assemble")
            try:
                decisions = json.loads(rest[i + 1])
            except json.JSONDecodeError as exc:
                print(f"workstream-complete-assemble: malformed --decisions JSON: {exc}", file=sys.stderr)
                return EXIT_USAGE
            i += 2
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
    subcmd, rest = argv[0], argv[1:]

    if subcmd == "brief":
        return _main_brief(rest)
    if subcmd == "apply":
        return _main_apply(rest)

    print(f"workstream-complete-assemble: unknown subcommand {subcmd!r}", file=sys.stderr)
    return _usage("workstream-complete-assemble")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
