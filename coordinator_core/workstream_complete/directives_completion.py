"""
coordinator_core.workstream_complete.directives_completion — directive and
read-only-gate builders for the completion-entry + execution-record-fold
cluster of `/workstream-complete` (SKILL.md Steps 2.6, 2.6.7, 2.6.8, 2.6b of
the pre-conversion spine — DoE-claude
coordinator/skills/workstream-complete/SKILL.md:180-278).

Purpose: computes the mechanical half of "archive uncaptured work" (Step
2.6's skip predicate + the `coordinator-complete-entry` invocation), the
post-summary reconcile (Step 2.6.8), and the run-report-sidecar fold-and-
delete cluster (Step 2.6b) for `coordinator_core.workstream_complete`'s
`brief()` assembly seam (C3). Step 2.6.7 ("Judgment filter" — the SKILL's
own name for it) and Step 2.6 Action (b)'s nature-infer dispatch / prose
composition are OUT of this module's scope — both are judgment_points
(`commit-significance-filter`, `completion-nature-classification`,
`completion-entry-prose`) owned by `judgments.py` (C2f); this module only
turns already-decided prose/nature values into directive-argument shape.

This module is one of seven siblings (directives_lessons_plan.py,
directives_memo_lifecycle.py, directives_session_hygiene.py,
directives_review.py, directives_commit_tail.py, judgments.py) built under
the new intra-claude-klabauter multi-module-assembler convention this plan sets:
`__init__.py` is retained as the assembly + CLI seam ONLY, and every
submodule exposes pure, `__init__`-independent builder functions — this is
the first multi-module assembler in the tree (D-4,
docs/plans/2026-07-26-workstream-complete-computed-frontage.md; the
convention is registered centrally at C10,
coordinator/docs/wiki/computed-skills-conversion-checklist.md, so the next
converter inherits it deliberately rather than by imitation).

Design note — directives[] vs read-only gates (three decisions this chunk
made that the plan body left open, flagged explicitly for C3's assembly
seam):

1. **d-completion-archive-predicate** and **d-detect-run-report-sidecars**
   are census-classified DIRECTIVE but are NOT emitted as `directives[]`
   dicts — both are pure existence/glob checks with no CLI of their own,
   exactly the shape `coordinator_core.workstream_complete.__init__`'s own
   `d-governing-plan-predicate` already takes (a plain Python gate that
   decides whether OTHER directives get built, never a list entry itself).
   Modeling either as a phantom `directives[].cli` value would fail AC2's
   own phantom-verb guard the moment C1's contract test runs it against
   `CONSUMES_MANIFEST` — there is no atomic CLI on disk for either check
   (grepped `coordinator/bin/`: no `*archive-predicate*`/`*sidecar-
   detect*` entry). Exposed instead as `completion_archive_predicate()`
   and `compute_run_report_sidecar_gate()`.

2. **d-delete-folded-sidecars** has no atomic CLI either (grepped
   `coordinator/bin/`: `coordinator-fold-execution-record` folds, it does
   not delete; `reap-stale-subagent-sidecars.py` is a liveness+age-gated
   `/distill`-cadence reaper, a different gate shape entirely — it would
   silently preserve a same-session sidecar this step must delete NOW).
   The SKILL's own Staging note (`SKILL.md:277`) says these deletions
   "stage as individual `git rm` paths ... in the same Step 3 scoped
   commit" — the SAME mutating path `d-close-tail-args`/`d-tail`'s
   existing `--deleted-paths` flag already carries (see
   `__init__.py:315-316,336`). So this is a compute-only contribution to
   an EXISTING directive's payload, not a new mutating directive of its
   own. Exposed as `compute_run_report_sidecar_gate(...).foldable` — the
   assembler (C3) must fold this into `decisions["deleted_paths"]` BEFORE
   building `d-close-tail-args` (see § Wiring notes below).

3. **`d-reconcile-completion-commits` — REMOVED (completion.
   reconcile_commits kill, 2026-08-23):** its CLI, `coordinator/bin/
   reconcile-completion-commits.py`, is deleted, and this module no
   longer builds the directive. What follows is retained as history, since
   `apply.py`'s generic `{<producer-id>.entry_path}`/`.landed`/`.argv`
   arg-token substitution machinery (`_resolve_arg_tokens`,
   `_execute_directives`'s directive-id `depends_on` routing) it motivated
   remains live infrastructure, now unused pending a fresh consumer.
   `entry_path` was only known once `d-complete-entry` actually ran — it
   depends on today's date and an idempotency guard against an existing
   chain-slug entry that may already live under a different date/session
   (`coordinator_core/ops/coordinator_complete_entry.py:676-708`,
   `_idempotency_guard`), and this module's own negative-spec (below)
   forbade re-deriving that guard a second time here. This directive's
   `args` therefore carried the literal placeholder token
   `RECONCILE_ENTRY_PATH_TOKEN` in place of the real path, with
   `depends_on: "d-complete-entry"` — the apply half (C4) substituted it
   from `d-complete-entry`'s captured stdout (first line, per that CLI's
   own `print(entry_path)` contract, `coordinator_complete_entry.py:726`)
   before invoking, the same shape workday_complete/workweek_complete's
   `apply.py` already use for directive-to-directive value threading
   (there: `stdin_from`, piping a producer's captured stdout into a
   consumer's stdin — not reusable verbatim here because `reconcile-
   completion-commits.py` took the path as a positional argument, not
   stdin content, so a plain arg-token substitution was the closer fit).
   **Ratified (C4 completion, 2026-07-27):** `workstream_complete.apply.
   _resolve_arg_tokens` implements the substitution; `_execute_directives`
   also had to special-case this directive's `depends_on` value, since
   `ceremony_common.apply_halt._directive_gate_open` only understands
   `depends_on` as a judgment_point id and fail-closes (permanently
   blocks) on any other value — a `depends_on` naming a sibling DIRECTIVE
   id (as this one did) was recognized as a producer-ordering
   dependency and routed around that gate instead, letting `_resolve_arg_
   tokens`'s own producer-landed check govern readiness. See `apply.py`'s
   module docstring, deviation 3, for the full mechanism.

Consumes (orchestrates, reimplements none):
    coordinator/bin/coordinator-complete-entry.py
        -> d-complete-entry's directives[].cli. Legacy-monolith migration,
           chain-slug idempotency guard, LoE block computation, and
           skeleton scaffold+fill all live inside this CLI — this module
           never re-derives any of it, only composes the flags Step 2.6
           Action (a) names.
    coordinator/bin/coordinator-fold-execution-record.py
        -> d-fold-execution-observations's directives[].cli (bareword, no
           `.py` suffix on disk — the apply half needs the same bareword-
           or-`.py` script resolution `workweek_complete/apply.py`'s
           `_resolve_script_path` already handles for its own manifest).
    coordinator_core.frontmatter.schema_validate.parse_frontmatter
        -> reads each candidate sidecar's `status:` frontmatter field to
           classify it foldable vs. preserved (blocked/thrashing).

Negative-spec:
    - Does NOT take the `SessionShapeGate` NamedTuple `__init__.py`
      defines as a parameter anywhere in this module (mirrors
      `directives_session_hygiene.py`'s own negative-spec) —
      `__init__.py` imports this module (assembly direction); importing
      back for a type would be circular. Callers pass the plain fields
      the gate already carries (`sid`, `disposition`, `consumed_handoff`).
    - Does NOT dispatch the Step 2.6 Action (b) nature-infer Sonnet
      sub-call, compose completion-entry TITLE/body prose, or apply
      Step 2.6.7's commit-significance judgment filter — all three are
      `judgments.py`'s (C2f). This module only wires an ALREADY-DECIDED
      `nature`/`fold_desc` value into directive-argument shape.
    - Does NOT invoke `coordinator-complete-entry.py` or
      `coordinator-fold-execution-record` in-process. Every mutating action this module names is an
      existing CLI for the apply half to invoke, never invoked here — this
      module only reads disk (existence checks, sidecar globs, sidecar
      frontmatter) and returns directive/gate shape.
    - Does NOT re-implement `coordinator-complete-entry.py`'s idempotency
      guard, LoE computation, or chain-slug filename derivation — see
      Design note 3 above for why `entry_path` is a runtime-only value
      here, not a pre-resolved one.
    - Does NOT delete, move, or `git rm` any sidecar file — `foldable`
      only NAMES candidates; the actual deletion is `d-tail`'s (via
      `--deleted-paths`), per Design note 2.

§ Wiring notes for the assembler (C3):
    1. Call `build_directives(...)` and concatenate its result with the
       other six submodules' directive lists.
    2. Call `compute_run_report_sidecar_gate(repo_root, sid, plan_slug)`
       SEPARATELY, and fold its `.foldable` paths into
       `decisions["deleted_paths"]` BEFORE `__init__.py`'s existing
       `d-close-tail-args`/`d-tail` directive builders run over
       `decisions` — those two directives already have a `--deleted-
       paths` flag (`__init__.py:315-316,336`); this is additive to
       whatever `decisions["deleted_paths"]` the caller already supplies,
       never a replacement of it.
    3. Was `d-reconcile-completion-commits`'s `RECONCILE_ENTRY_PATH_TOKEN`
       wiring — removed along with the directive (completion.
       reconcile_commits kill, 2026-08-23); see Design note 3 above.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.ceremony.wsc_disposition import PREDECESSOR_CONSUMED, canonicalize

# ---------------------------------------------------------------------------
# Manifest CLI names (must be registered verbatim in __init__.py's
# CONSUMES_MANIFEST by C3 — see test_workstream_complete_contract.py).
# ---------------------------------------------------------------------------

_COMPLETE_ENTRY_CLI = "coordinator-complete-entry"
_FOLD_EXECUTION_RECORD_CLI = "coordinator-fold-execution-record"

#: The `decisions` keys `build_directives` below reads — declared once so a
#: caller (`__init__.py`'s `preflight.decisions_template` composition) can
#: import and union this tuple rather than hand-copying the key list. See
#: AC3 (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md):
#: the arg-builder and the template read this SAME constant.
_KEY_GOVERNING_PLAN_SLUG = "governing_plan_slug"
_KEY_NATURE = "nature"
_KEY_PLAN_SLUG = "plan_slug"
_KEY_PLAN_PATH = "plan_path"
_KEY_FOLD_DESC = "fold_desc"

FREE_VALUE_KEYS: tuple[str, ...] = (
    _KEY_GOVERNING_PLAN_SLUG,
    _KEY_NATURE,
    _KEY_PLAN_SLUG,
    _KEY_PLAN_PATH,
    _KEY_FOLD_DESC,
)

#: `status:` values a run-report sidecar's frontmatter carries that mark it
#: terminal-but-unresolved — never folded/deleted regardless of session
#: liveness or fold outcome (mirrors `reap-stale-subagent-sidecars.py`'s
#: own carve-out and `docs/wiki/scratch-lifecycle.md` Pattern A).
_PRESERVED_SIDECAR_STATUSES = frozenset({"blocked", "thrashing"})


def _directive(
    id_: str,
    cli: str,
    args: list[str],
    depends_on: Any = None,
    already_satisfied: bool = False,
) -> dict[str, Any]:
    return {"id": id_, "cli": cli, "args": args, "depends_on": depends_on, "already_satisfied": already_satisfied}


# ---------------------------------------------------------------------------
# Step 2.6 — skip predicate (read-only gate, not a directives[] entry)
# ---------------------------------------------------------------------------


# compute_completion_entry_scaffold_gate / CompletionEntryScaffoldFact —
# REMOVED (ceremony.wsc_tail kill, 2026-08-23): the Step 2.6
# authoring-window fact existed solely to feed `jp-completion-entry-
# scaffold`, which gated `d-run-wsc-tail`. Neither the judgment point nor
# the directive it gated still exist — see `directives_commit_tail.py`'s
# module docstring and `__init__.py`'s `build_directives`.


def completion_archive_predicate(repo_root: Path) -> bool:
    """Step 2.6's own skip gate: `archive/` and `state/workstreams/`
    absent means the project never adopted unified tracking — skip the
    whole completion-entry cluster (Action (a)/(b), Step 2.6.7, Step
    2.6.8) entirely, zero ceremony tax. Pure existence check, no CLI
    (Design note 1). `state/workstreams/` mirrors the onboarding-currency
    signal `detect_onboarding_offer._is_onboarded` uses (the substrate the
    retired project-tracker render was rendered FROM), so the two gates
    cannot drift apart — see docs/plans/
    2026-08-14-retire-the-handoff-tracker-and-project-tracker-renders.md
    § C1."""
    return (repo_root / "archive").is_dir() or (repo_root / "state" / "workstreams").is_dir()


# ---------------------------------------------------------------------------
# Step 2.6 Action (a) — d-complete-entry
# ---------------------------------------------------------------------------


def build_complete_entry_directive(
    *,
    sid: str,
    disposition: str,
    consumed_handoff: str = "",
    governing_plan_slug: Optional[str] = None,
    nature: Optional[str] = None,
) -> dict[str, Any]:
    """Step 2.6 Action (a): the single `coordinator-complete-entry`
    invocation. Fires unconditionally once `completion_archive_predicate`
    already gated the cluster in — `--consumed-handoff` only when
    `disposition == "chain-terminal"` and a handoff was actually consumed,
    `--governing-plan-slug`/`--nature` only when their backing values are
    set, all per Step 2.6 Action (a)'s own flag-composition text
    (`SKILL.md:186`). Idempotency (exit 0 no-op on an existing chain-slug
    entry) and LoE-block computation are the CLI's own job, not
    replicated here — mirrors `d-stamp-plan-implemented`'s existing
    "status-matrix logic lives in the CLI itself" precedent."""
    args = ["--sid", sid, "--disposition", disposition]
    if canonicalize(disposition) == PREDECESSOR_CONSUMED and consumed_handoff:
        args += ["--consumed-handoff", consumed_handoff]
    if governing_plan_slug:
        args += ["--governing-plan-slug", governing_plan_slug]
    if nature:
        args += ["--nature", nature]
    return _directive("d-complete-entry", _COMPLETE_ENTRY_CLI, args)


# Step 2.6.8 — d-reconcile-completion-commits — REMOVED (completion.
# reconcile_commits kill, 2026-08-23): `coordinator/bin/reconcile-
# completion-commits.py` is deleted.


# ---------------------------------------------------------------------------
# Step 2.6b — run-report sidecar detect / fold / delete-candidate gate
# ---------------------------------------------------------------------------


class RunReportSidecar(NamedTuple):
    path: Path
    status: Optional[str]


class RunReportSidecarGate(NamedTuple):
    detected: tuple[RunReportSidecar, ...]
    foldable: tuple[Path, ...]
    preserved: tuple[Path, ...]


def _read_sidecar_status(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter = parse_frontmatter(text).get("frontmatter") or {}
    status = frontmatter.get("status")
    return str(status) if status is not None else None


def compute_run_report_sidecar_gate(
    repo_root: Path,
    sid: str,
    plan_slug: Optional[str],
) -> RunReportSidecarGate:
    """Step 2.6b sub-steps 1 and 3 in one read-only computation:
    `d-detect-run-report-sidecars` (glob
    `<machinery-root>/subagent-share/<sid>/<plan-slug>.<chunk-id>.md`) and
    `d-delete-folded-sidecars` (rule-based filter on each sidecar's own
    `status:` frontmatter — `blocked`/`thrashing` preserved, everything
    else foldable). Neither is a `directives[].cli` entry (Design notes
    1-2) — `foldable` is the input the assembler must fold into
    `decisions["deleted_paths"]` (see module docstring's Wiring notes).

    Returns an empty gate (no detected/foldable/preserved) when
    `plan_slug` is falsy or the session's sidecar directory doesn't
    exist — both are the SKILL's own "skip this step silently" cases
    (`SKILL.md:250`), not error conditions.
    """
    if not plan_slug:
        return RunReportSidecarGate(detected=(), foldable=(), preserved=())
    # The SEVENTEENTH relocation reader: the sweep that fixed "the sixteen
    # relocation readers the census never covered" (196fbbc71e) missed this one,
    # and it is the worst place to miss one. The skip-silently contract below is
    # correct for a session that genuinely has no sidecars, and it is
    # indistinguishable from this function pointing at a directory that no longer
    # exists -- so a stale root here does not fail, it folds nothing, quietly,
    # every run. Resolve through the owner rather than rebuilding the join.
    from coordinator_core.session import machinery_paths

    sidecar_dir = Path(machinery_paths.share_dir(str(repo_root), sid))
    if not sidecar_dir.is_dir():
        return RunReportSidecarGate(detected=(), foldable=(), preserved=())

    detected = tuple(
        RunReportSidecar(path=p, status=_read_sidecar_status(p))
        for p in sorted(sidecar_dir.glob(f"{plan_slug}.*.md"))
    )
    foldable = tuple(s.path for s in detected if s.status not in _PRESERVED_SIDECAR_STATUSES)
    preserved = tuple(s.path for s in detected if s.status in _PRESERVED_SIDECAR_STATUSES)
    return RunReportSidecarGate(detected=detected, foldable=foldable, preserved=preserved)


def build_fold_execution_observations_directive(
    *,
    plan_path: str,
    fold_desc: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Step 2.6b sub-step 2: the `coordinator-fold-execution-record`
    invocation whose Part A (`## Execution Observations`) and Part B
    (`## Completion Entry Prose`) outputs the EM appends verbatim /
    consumes as-is (`SKILL.md:263-268`) — this module names the call, it
    never composes the fold content itself. `fold_desc` is the ALREADY-
    DECIDED one-liner (part of the `completion-entry-prose` judgment_point,
    C2f/EM's own call, per Design note in `directives_session_hygiene.py`'s
    precedent for the same "wire a decided value, don't decide it" split)
    — `--desc` is optional on the CLI itself, so a directive is still
    emitted (with a partial `args`) when `fold_desc` hasn't been resolved
    yet, per `__init__.py`'s own "name it, don't drop it" convention for
    absent-input directives. Returns `None` only when `plan_path` itself
    is unresolvable (nothing to fold against)."""
    if not plan_path:
        return None
    args = ["--plan", plan_path]
    if fold_desc:
        args += ["--desc", fold_desc]
    return _directive("d-fold-execution-observations", _FOLD_EXECUTION_RECORD_CLI, args)


# ---------------------------------------------------------------------------
# Aggregate entrypoint
# ---------------------------------------------------------------------------


def build_directives(
    *,
    sid: str,
    disposition: str,
    consumed_handoff: str,
    repo_root: Path,
    decisions: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Assembles this cluster's `directives[]` contribution — the
    completion-archive-predicate and run-report-sidecar-detect gates
    (Design note 1) decide INCLUSION here; they never appear as list
    entries themselves. `decisions` reads the same keys the existing
    `__init__.py:build_directives` already establishes
    (`governing_plan_slug`) plus this cluster's own (`nature`, `plan_slug`,
    `plan_path`, `fold_desc`) — every key optional, per the parent
    module's "name it, don't drop it" convention for absent-input
    directives.
    """
    decisions = decisions or {}
    directives: list[dict[str, Any]] = []

    if completion_archive_predicate(repo_root):
        directives.append(
            build_complete_entry_directive(
                sid=sid,
                disposition=disposition,
                consumed_handoff=consumed_handoff,
                governing_plan_slug=decisions.get(_KEY_GOVERNING_PLAN_SLUG),
                nature=decisions.get(_KEY_NATURE),
            )
        )
        # `d-reconcile-completion-commits` — REMOVED (completion.
        # reconcile_commits kill, 2026-08-23): its CLI,
        # `coordinator/bin/reconcile-completion-commits.py`, is deleted.

    plan_slug = decisions.get(_KEY_PLAN_SLUG) or decisions.get(_KEY_GOVERNING_PLAN_SLUG)
    sidecar_gate = compute_run_report_sidecar_gate(repo_root, sid, plan_slug)
    if sidecar_gate.detected:
        fold_directive = build_fold_execution_observations_directive(
            plan_path=str(decisions.get(_KEY_PLAN_PATH) or ""),
            fold_desc=decisions.get(_KEY_FOLD_DESC),
        )
        if fold_directive is not None:
            directives.append(fold_directive)

    return directives
