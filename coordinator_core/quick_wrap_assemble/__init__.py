"""
coordinator_core.quick_wrap_assemble — the `quick-wrap-assemble` computed-skill engine.

Purpose: emit `/quick-wrap`'s four-condition entry test as computed fields instead of
EM prose. Before this module, quick-wrap was the only session-terminating ceremony in
the corpus with no assembler at all, so 100% of its entry gate was re-derived by an
Opus-tier EM at every light close — and its skill body carried five `engine-gap`
markers naming fields no producer emitted.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: state/handoffs/2026-08-14-fact-layer-library-sweep.md § Specification
Fold-in ask: cross-repo/archive/2026-08-14-doe-claude-em-quick-wrap-has-no-assembler-at-all.md
Governing ruling: docs/decisions/DR-306-a-computed-fact-left-in-prose-is-break.md

Registration seam: consumed by the `coordinator/bin/quick-wrap-assemble` trampoline via
`entry_point_shim.ASSEMBLE_TARGETS`, mirroring `coordinator_core.orient_assemble`'s
template shape (build_envelope/_emit chokepoint, `main(argv) -> int`).

**All five close-gate facts are read off the session-fact facade**
(`coordinator_core.session.session_facts`, `fl-core-02` — DR-323's cutover, chunk C7).
This module previously carried its own one-fact-per-function readers as an INTERIM
seam; that lift has happened, and the readers are gone. `brief()` is now purely a
composition: call the five `session_<noun phrase>` facts, unwrap each DR-319 record's
`value` for `_entry_test`/`_judgment_points`/`_directives` (which still consume the
same bare-dict payload shape those interim readers produced — only the producer
changed), and emit the raw DR-319 record itself in `close_gate` so `source`/`collision`
travel to the ceremony's own consumers rather than being discarded at this seam.

  1. `pickup_kind`       — `session_facts.session_pickup_kind`
  2. `governing_plan`    — `session_facts.session_governing_plan`
  3. `diff`              — `session_facts.session_diff_brightline`
  4. `terminal_sizings`  — `session_facts.session_terminal_sizings`
  5. `fold_sidecars`     — `session_facts.session_fold_sidecars`

DEGRADED-FACT POSTURE (DR-319 § (c), never fail-open): a degraded record has no
`value` key, so unwrapping it with a bare `.get("value")` would silently read as
`None` at every downstream field access — the exact fail-open conflation DR-323 names
as the dangerous direction of a wrong answer. This module instead threads each
degraded fact into a CLOSED substitute (`_closed_pickup_kind`/`_closed_governing_plan`/
`_closed_diff` below) that fails its own entry-test condition and carries a visibly
degraded evidence string, or — for the two facts with no entry-test condition of their
own (`terminal_sizings`, `fold_sidecars`) — raises an explicit untrusted-gate judgment
point naming the degradation (`_degraded_probe_judgment_point`). Nothing here decides
what a degraded fact MEANS; it only ensures the ceremony cannot mistake "could not
read" for "read and clean."

Dirty-vs-clean (baton § Specification item 2): every scan that can collide with a live
peer emits its collision state. A terminal sizing carrying a peer's uncommitted edits is
reported `dirty: true` — detection is ours, the decision to skip rather than sweep stays
the EM's. `git mv` on a dirty record would drag in-flight work into `archive/`.

READ-ONLY, by construction, WITH ONE NAMED CARVE-OUT (C5, see Negative-spec below): this
module reads disk/git state, and `brief()` additionally calls C4's hardened
`auto_commit_session_async` in-process to commit this session's own claimed dirty paths
before returning. Every OTHER mutating action is still returned as a `directives[]` entry
naming an existing atomic CLI.

Negative-spec:
    - Do NOT add a mutating code path here, beyond the ONE narrow carve-out C5
      (docs/plans/2026-08-20-the-close-ceremony-commits-what-the-session-wrote.md § C5)
      already introduced: `_run_close_commit`'s in-process call to
      `coordinator_core.ops.session.safe_commit_offer.auto_commit_session_async`,
      replacing the former `safe-commit-offer` directive by explicit PM ruling (being
      asked whether to commit was itself the defect). That carve-out is closed — do not
      widen it into a general precedent for "the assembler should just do X" for any
      other X that writes to disk; every other mutating finding still belongs in a
      `directives[]` entry. Quick-wrap's own anti-scope forbids deletes, force-pushes, and
      history rewrites; this producer must not be the seam that reintroduces one.
    - Do NOT decide entry-test condition 4 ("work is finished"). It is genuine EM
      discretion — the carve-out the doctrine page explicitly preserves — and is emitted
      as a `judgment_points[]` entry, never as a computed verdict. Conditions 1-3 are
      computed; 4 is asked.
    - Do NOT re-derive `_emit`/`build_envelope` locally; import the shipped
      `contract/decision_object/envelope.py` chokepoint.
    - Do NOT reimplement session scoping or any of the five facts' own read logic. That
      logic lives on `session_facts` now; a size pass that "notices" this module has no
      scoping logic of its own is reading correct reuse as a gap, not forking a second
      copy into this ceremony.
    - Do NOT silently substitute `None`/`0`/an empty collection for a degraded fact at
      any of the five call sites below — DR-319 § (c)'s failure posture is binding here
      too, propagated via the closed-fallback/judgment-point mechanism this module owns.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from coordinator_core.contract.decision_object.envelope import (
    _emit,
    build_envelope,
    extend_exit_codes,
)
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_untrusted_gate_judgment_point,
)

#: Re-exported for the pre-lift characterization pin (`test_quick_wrap_assemble.py`
#: § "C1(d) — pre-lift characterization pin (fl-core-02)"), which references
#: `qwa.SCOPING_METHOD_AMBIGUOUS` as an input fixture. DR-323 § (d) forbids that pin's
#: assertions from changing shape across this cutover, so the symbol stays reachable at
#: this same attribute path even though no code below branches on it directly anymore
#: (`session_facts.session_diff_brightline` owns that comparison now).
from coordinator_core.ops.ceremony.branch_resolution import (  # noqa: F401
    SCOPING_METHOD_AMBIGUOUS,
)
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.ops.extract_scope_paths import _extract_scope_paths
from coordinator_core.ops.session.safe_commit_offer import auto_commit_session_async
from coordinator_core.ops.session_commits import resolve_session_commits
from coordinator_core.session import session_facts
from coordinator_core.session_baton.store import merge_baton

#: Locally scoped exit codes — NOT inherited from a sibling assembler. Built through the
#: shipped `extend_exit_codes` factory because `ExitCodeBase` is deliberately final;
#: subclassing it is a type error, not a style preference.
#:
#: SUCCESS is emitted whenever a decision object was computed, INCLUDING when the entry
#: test fails: a failing gate is a successful computation of a routing verdict, not a
#: transport error. A caller treating a non-zero exit as "the entry test passed" has the
#: polarity backwards — read `gates.entry_test.verdict`.
QuickWrapExitCode = extend_exit_codes("QuickWrapExitCode", USAGE=2, TRANSPORT=3)


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------


def _git_out(args: list[str], cwd: Path) -> str:
    """Read-only git invocation. Returns "" on any failure rather than raising —
    a fact this assembler cannot read degrades that fact to `null`/`unknown`, it never
    takes down the close ceremony. `--no-optional-locks` because this repo is a shared
    worktree with a dozen concurrent sessions and a read must never contend for
    `.git/index.lock`."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _resolve_session(worktree_root: Path | None = None) -> tuple[Path, Path, str]:
    """Return (worktree_root, common_dir, session_id).

    Raises `RuntimeError` when no enclosing git worktree or no session id resolves —
    the two genuine transport failures, distinct from a fact simply being absent.
    """
    if worktree_root is None:
        top = _git_out(["rev-parse", "--show-toplevel"], Path.cwd()).strip()
        if not top:
            raise RuntimeError("no enclosing git worktree")
        worktree_root = Path(top)

    common = _git_out(["rev-parse", "--git-common-dir"], worktree_root).strip()
    common_dir = Path(common) if common else worktree_root / ".git"
    if not common_dir.is_absolute():
        common_dir = (worktree_root / common_dir).resolve()

    from coordinator_core.ops.session_context import resolve_current_session_id

    sid = resolve_current_session_id(worktree_root=worktree_root)
    if not sid:
        raise RuntimeError(
            "could not resolve a session id — set COORDINATOR_SESSION_ID, "
            "CLAUDE_SESSION_ID, or CLAUDE_CODE_SESSION_ID in the environment"
        )
    return worktree_root, common_dir, sid


# ---------------------------------------------------------------------------
# Degraded-fact fallbacks (DR-319 § (c) propagated into this ceremony's own
# entry-test conditions and judgment points — see module docstring
# "DEGRADED-FACT POSTURE").
# ---------------------------------------------------------------------------


def _closed_pickup_kind() -> dict[str, Any]:
    """Substitute for `session_pickup_kind`'s `value` when that fact is degraded.

    Forces entry-test condition 3 to FAIL (`consumed_predecessor: True`): a session
    whose pickup kind could not be read is treated as having consumed a predecessor,
    routing to `/workstream-complete` rather than silently passing a light close on a
    fact this ceremony never actually confirmed. `classification: "degraded"` makes the
    substitution visible in condition 3's own evidence string rather than reading as an
    ordinary `"none"`.
    """
    return {
        "classification": "degraded",
        "artifact_path": None,
        "basename": None,
        "deliverable_id": None,
        "actioned_memos": [],
        "consumed_predecessor": True,
    }


def _closed_governing_plan() -> dict[str, Any]:
    """Substitute for `session_governing_plan`'s `value` when that fact is degraded.

    Forces entry-test condition 1 to FAIL: `present: True` with `scope_mode:
    "degraded"` (never `"spec-dispatch"`) means the deferred-obligation carve-out
    cannot apply, so a session whose governing-plan read could not complete is treated
    as plan-governed and routes out — the same "dangerous direction of a wrong answer"
    DR-323 names for the original `_read_governing_plan` bug, refused here rather than
    repeated.
    """
    return {
        "present": True,
        "path": None,
        "status": None,
        "scope_mode": "degraded",
        "slug": None,
    }


def _closed_diff() -> dict[str, Any]:
    """Substitute for `session_diff_brightline`'s `value` when that fact is degraded.

    Forces entry-test condition 2 to FAIL (`under_brightline: False`): a session whose
    diff brightline could not be computed is treated as breaching it, routing to a
    scoped review rather than silently passing. Numeric fields are `None`, never `0` —
    `0` reads as "genuinely clean," the exact fail-open conflation this substitute
    exists to avoid; `None` interpolated into condition 2's evidence string is visibly
    abnormal instead. `trustworthy: True` is deliberate: this substitute already makes
    the degradation visible via condition 2's own failure, so it does not also trip
    `_judgment_points`' separate `j-scoping-ambiguous` point, which exists for a
    different, non-degraded condition (an ambiguous but successfully-read trailer).
    """
    return {
        "scoping_method": "degraded",
        "trustworthy": True,
        "novel_loc": None,
        "gross_loc": None,
        "doc_only_loc": None,
        "novel_commit_count": None,
        "commit_count": None,
        "surface_count": None,
        "novel_surface_count": None,
        "breached": ["degraded"],
        "under_brightline": False,
    }


def _degraded_probe_judgment_point(id_: str, label: str, evidence: str) -> dict[str, Any]:
    """An untrusted-gate judgment point for a fact with no entry-test condition of its
    own (`terminal_sizings`, `fold_sidecars`) that came back degraded.

    Neither fact feeds `_entry_test`, so there is no condition to fail closed the way
    `_closed_pickup_kind`/`_closed_governing_plan`/`_closed_diff` do above — the only
    other place a degraded fact would otherwise go unmentioned is here, or silently
    into an empty scan indistinguishable from "nothing found." This raises it as a
    question instead: DETECTS that the probe could not run, and asks the EM to verify
    manually, never guessing what the missing scan would have found (DR-306's
    detect/decide split, the same discipline every other judgment point in this module
    already carries).
    """
    return build_untrusted_gate_judgment_point(
        id=id_,
        question=f"{label} could not be computed for this session — verify it manually?",
        dispositions=[
            build_disposition(
                "verified",
                guidance="Confirmed manually; proceed with the checklist.",
            ),
            build_disposition(
                "cannot-verify",
                guidance="Route to /workstream-complete rather than guess.",
            ),
        ],
        evidence=evidence,
        reason=(
            "The session fact facade reported this probe degraded, not computed — "
            "this assembler does not substitute a value for a fact it could not read."
        ),
    )


# ---------------------------------------------------------------------------
# Entry test
# ---------------------------------------------------------------------------


def _is_ancestry_null(raw: Any) -> bool:
    """True for the on-disk spellings DoE-claude `coordinator/skills/quick-wrap/
    SKILL.md`'s `‡` footnote treats as "no ancestor here" for a chain-root
    baton's `predecessor:`/`forked_from:`: absent (`None`), present-but-empty,
    or the literal `none`/`null` scalar (case-insensitive)."""
    if raw is None:
        return True
    stripped = str(raw).strip().lower()
    return stripped in ("", "none", "null")


def _read_ancestry_fields(worktree_root: Path, artifact_path: str) -> dict[str, Any] | None:
    """Read `predecessor`/`additional_predecessors`/`forked_from` off the
    session's claimed artifact — SKILL.md's `‡` footnote: "Until
    `artifact.ancestry` lands, read those three frontmatter fields off the
    same brief."

    Returns `None` when the file cannot be read or carries no parseable
    frontmatter (absent, archived mid-session, malformed) so condition 3's
    caller fails CLOSED rather than guessing a chain-root split from a file
    it never actually read. Never raises.

    Reuses `coordinator_core.frontmatter.primitives` (`split_frontmatter`,
    `read_fm_field_unquoted`) and the same nested-list-block scanner
    `pickup_assemble` already runs for this exact key
    (`coordinator_core.ops.extract_scope_paths._extract_scope_paths`,
    `key="additional_predecessors"`) — no new YAML parser.
    """
    abs_path = (worktree_root / artifact_path).resolve()
    try:
        text = abs_path.read_text(encoding="utf-8")
    except OSError:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    fm_text = split.fm_text
    return {
        "predecessor": read_fm_field_unquoted(fm_text, "predecessor"),
        "forked_from": read_fm_field_unquoted(fm_text, "forked_from"),
        "additional_predecessors": _extract_scope_paths(
            fm_text, key="additional_predecessors"
        ),
    }


def _c3_ancestry(pickup_kind: dict[str, Any], worktree_root: Path) -> tuple[bool, str]:
    """Condition 3: ANCESTRY, not pickup (SKILL.md `‡` — "A pickup is not
    itself the disqualifier — ancestry is").

    - Nothing claimed this session (`classification == "none"`) — nothing to
      read, passes.
    - `classification == "memo"` — passes, and NOT via the no-`artifact_path`
      branch below. A memo is not a baton: `memo.schema.json` has no
      `predecessor`, `additional_predecessors`, or `forked_from`, so the three
      fields this condition reads cannot exist on one, and actioning a memo
      inherits no prior session's scope — which is the whole rationale test 3
      encodes ("a chain that accumulated history owes a coverage gate").
      Structural, not a datum this could recover: `session_pickup_kind`'s memo
      branch records `actioned_memos[].basename` and never an `artifact_path`,
      so before this branch existed EVERY memo-only session failed c3 closed
      and was routed to `/workstream-complete` — a heavy close for a session
      that consumed no chain at all. Keyed on the classification rather than on
      resolving the basename back to a path deliberately: that resolution
      (inbox vs. a month-subfoldered archive) is the exact fragility the
      fail-closed branch below exists to distrust, and reading the file would
      buy nothing when the schema guarantees the fields are absent.
    - Something claimed but no `artifact_path` resolved — the file was
      already absent or archived to a location this session's pickup fact
      never found (e.g. a month-subfoldered `archive/handoffs/`); treated the
      same as an unreadable claimed artifact, fails CLOSED. This also covers
      `_closed_pickup_kind()`'s degraded-fact substitute (`classification:
      "degraded"`, `artifact_path: None`), which must keep failing condition
      3 — never fold "no artifact_path" into the pass branch above it.
    - An artifact_path resolved but the file cannot be read at close time
      (archived mid-session) — fails CLOSED, never guessed.
    - Read succeeds — passes iff the artifact is a chain root: `predecessor`
      and `forked_from` both null/absent/`"none"`, `additional_predecessors`
      empty. Any ancestor fails.
    """
    classification = pickup_kind.get("classification")
    artifact_path = pickup_kind.get("artifact_path")

    if classification == "none":
        return True, "classification='none' — nothing claimed this session, no ancestry to read"

    if classification == "memo":
        return True, (
            "classification='memo' — a memo is not a baton and carries no "
            "predecessor/additional_predecessors/forked_from, so it inherits no prior "
            "session's scope and owes no coverage gate"
        )

    if not artifact_path:
        return False, (
            f"classification={classification!r} claimed but no artifact_path resolved "
            "— treating as unreadable/absent, failing closed"
        )

    ancestry = _read_ancestry_fields(worktree_root, artifact_path)
    if ancestry is None:
        return False, (
            f"claimed artifact {artifact_path!r} could not be read (absent or archived "
            "mid-session) — failing closed"
        )

    is_root = (
        _is_ancestry_null(ancestry["predecessor"])
        and not ancestry["additional_predecessors"]
        and _is_ancestry_null(ancestry["forked_from"])
    )
    if is_root:
        return True, (
            f"claimed artifact {artifact_path!r} is a chain root "
            "(predecessor/additional_predecessors/forked_from all absent)"
        )

    carriers = []
    if not _is_ancestry_null(ancestry["predecessor"]):
        carriers.append(f"predecessor={ancestry['predecessor']!r}")
    if ancestry["additional_predecessors"]:
        carriers.append(f"additional_predecessors={ancestry['additional_predecessors']!r}")
    if not _is_ancestry_null(ancestry["forked_from"]):
        carriers.append(f"forked_from={ancestry['forked_from']!r}")
    return False, f"claimed artifact {artifact_path!r} carries ancestry: " + ", ".join(carriers)


def _entry_test(
    pickup_kind: dict[str, Any],
    governing_plan: dict[str, Any],
    diff: dict[str, Any],
    worktree_root: Path,
) -> dict[str, Any]:
    """Conditions 1-3 computed; condition 4 is EM discretion and is NOT decided here.

    Routing follows the skill body's own table verbatim: condition 2 failing routes to
    scoped-review-then-wrap (still quick-wrap), while 1, 3, or 4 failing routes out of
    the ceremony entirely. A `spec-dispatch` scope_mode passes condition 1 as a deferred
    obligation rather than failing it.

    Consumes the same bare-dict payload shape `session_pickup_kind`/
    `session_governing_plan`/`session_diff_brightline`'s `value` field carries (or one
    of this module's own closed substitutes when a fact degraded) — never a raw DR-319
    record. `brief()` is the seam that unwraps or substitutes before calling this.
    """
    scope_mode = (governing_plan.get("scope_mode") or "").strip().lower()
    plan_present = bool(governing_plan.get("present"))
    spec_dispatch = plan_present and scope_mode == "spec-dispatch"

    c1_pass = (not plan_present) or spec_dispatch
    c2_pass = bool(diff.get("under_brightline"))
    c3_pass, c3_evidence = _c3_ancestry(pickup_kind, worktree_root)

    conditions = [
        {
            "id": "c1",
            "condition": "No governing plan drove this session's work",
            "passed": c1_pass,
            "deferred_obligation": spec_dispatch,
            "evidence": (
                "no plan claim held by this session"
                if not plan_present
                else f"plan {governing_plan.get('path') or governing_plan.get('slug')!r} "
                f"scope_mode={scope_mode or 'null'}"
            ),
            "route_on_fail": "/workstream-complete",
        },
        {
            "id": "c2",
            "condition": "Diff under the review brightline",
            "passed": c2_pass,
            "evidence": (
                f"novel_loc={diff.get('novel_loc')} of {diff.get('gross_loc')} gross "
                f"({diff.get('doc_only_loc')} doc-only carved out), "
                f"novel_commits={diff.get('novel_commit_count')} of "
                f"{diff.get('commit_count')}, novel_surfaces="
                f"{diff.get('novel_surface_count')} of {diff.get('surface_count')}, "
                f"scoping={diff.get('scoping_method')}"
            ),
            "breached": diff.get("breached", []),
            "route_on_fail": "scoped review, then wrap (stays in quick-wrap)",
        },
        {
            "id": "c3",
            "condition": "What this session claimed has no ancestors",
            "passed": c3_pass,
            "evidence": c3_evidence,
            "route_on_fail": "/workstream-complete — a consumed handoff owes a coverage gate",
        },
        {
            "id": "c4",
            "condition": "Work is finished — nothing in-flight for a successor",
            "passed": None,
            "evidence": "EM judgment — deliberately not computed",
            "route_on_fail": "/handoff, only if context pressure genuinely forces the stop",
        },
    ]

    computed_failures = [c["id"] for c in conditions if c["passed"] is False]
    if not computed_failures:
        verdict = "computed-conditions-pass"
        route = "quick-wrap (pending condition 4)"
    elif computed_failures == ["c2"]:
        verdict = "review-owed"
        route = "scoped review, then wrap"
    else:
        verdict = "routed-out"
        route = "/workstream-complete"

    return {
        "conditions": conditions,
        "computed_failures": computed_failures,
        "verdict": verdict,
        "route": route,
    }


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _directives(fold: dict[str, Any], *, fold_degraded: bool = False) -> list[dict[str, Any]]:
    """The ceremony's unconditional CLI step(s), plus sidecar disposal when present.

    C5 (docs/plans/2026-08-20-the-close-ceremony-commits-what-the-session-wrote.md
    § C5): the former `safe-commit-offer` directive (step 1) is GONE from this list —
    being asked whether to commit was itself the defect (PM ruling), and an agent
    directive that may never actually run satisfies that ruling less completely than
    calling `auto_commit_session_async` in-process does. `brief()` now makes that call
    itself, before this function is invoked; see `_run_close_commit`. This function no
    longer emits any commit-shaped directive at all — do not re-add one.

    `fold_degraded` exists because `present: False` is NOT self-describing here. The
    caller's degraded fallback for Fact 5 carries that same literal, so gating `d2` on
    `fold["present"]` alone reads "the scan ran and found nothing" and "the scan could
    not run" identically — the absent-vs-clean conflation this package's lift exists to
    retire, surviving at one call site the record shape never reached. `d2` is withheld
    on a degraded read rather than emitted blind: `coordinator-fold-execution-record`
    takes `--plan`, so a path-less invocation synthesised from an empty fallback is not
    a runnable directive. The degradation surfaces instead as `j-fold-sidecars-degraded`
    (see `_degraded_probe_judgment_point`), which is the EM's signal to reconcile
    sidecars by hand. Negative-spec: do not "simplify" this parameter away by trusting
    `present` — the two states are only distinguishable here because it is passed.
    """
    directives: list[dict[str, Any]] = []
    if fold.get("present") and not fold_degraded:
        directives.append(
            {
                "id": "d2",
                "cli": "coordinator-fold-execution-record",
                "args": ["--read-divergence", *fold.get("paths", [])],
                "already_satisfied": False,
                "depends_on": None,
            }
        )
    directives.append(
        {
            "id": "d3",
            "cli": "regenerate-orientation-cache",
            "args": ["--invoker", "quick-wrap"],
            "already_satisfied": False,
            # Was `depends_on: "d1"` (the former `safe-commit-offer` directive).
            # C5 removed d1 in favor of an in-process call `brief()` makes
            # itself before this function runs — nothing left to depend on.
            "depends_on": None,
        }
    )
    return directives


def _judgment_points(
    sizings: dict[str, Any], diff: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every point routes through the shipped `judgment.py` builders.

    All three are UNTRUSTED-GATE points — they carry no `recommendation` by construction,
    because each asks for a fact this assembler genuinely cannot compute (is the work
    finished; is a peer's edit really theirs; is an ambiguous trailer trustworthy).
    Hand-building the dicts instead, as this module first did, produced a shape no other
    producer in the fleet emits — no `dispositions`, no `reason`, a dict `evidence` — and
    it slipped past `_emit` only because the one structural check there keys on
    `recommendation is not None`, which an absent key makes vacuously false. That is not
    conformance; it is bypassing the demotion mechanism every sibling goes through.

    Consumes the same bare-dict payload shape `session_terminal_sizings`/
    `session_diff_brightline`'s `value` field carries — never a raw DR-319 record; see
    `_entry_test`'s docstring for the same contract. A DEGRADED `terminal_sizings` or
    `fold_sidecars` fact is not this function's call site at all —
    `_degraded_probe_judgment_point` above and `brief()` below own that case, since
    neither fact has a slot in this function's fixed three points.
    """
    points: list[dict[str, Any]] = [
        build_untrusted_gate_judgment_point(
            id="j-work-finished",
            question="Is the work finished, with nothing in-flight for a successor?",
            dispositions=[
                build_disposition(
                    "finished",
                    guidance="Proceed with the checklist.",
                ),
                build_disposition(
                    "in-flight",
                    guidance=(
                        "Route to /handoff, only if context pressure genuinely forces "
                        "the stop."
                    ),
                ),
            ],
            evidence=(
                "Entry-test condition 4. No disk fact answers it: whether a successor "
                "is owed depends on intent this assembler cannot read."
            ),
            reason=(
                "Genuine EM discretion — the carve-out the computed-fact doctrine "
                "preserves. An assembler that guesses this has crossed from detecting "
                "into deciding."
            ),
        )
    ]

    dirty_terminal = [s for s in sizings.get("terminal", []) if s.get("dirty")]
    if dirty_terminal:
        points.append(
            build_untrusted_gate_judgment_point(
                id="j-dirty-terminal-sizing",
                question=(
                    "A terminal sizing-object carries uncommitted edits — archive it or "
                    "leave it?"
                ),
                dispositions=[
                    build_disposition(
                        "leave",
                        guidance=(
                            "Leave it. A git mv would sweep a live peer's in-flight "
                            "work into archive/."
                        ),
                    ),
                    build_disposition(
                        "archive",
                        guidance=(
                            "Only if the edits are this session's own and committed "
                            "first."
                        ),
                    ),
                ],
                evidence="; ".join(
                    f"{record['path']} ({record['status']})" for record in dirty_terminal
                ),
                reason=(
                    "Detecting the collision is mechanical; deciding whether the edits "
                    "are a live peer's is not."
                ),
            )
        )

    if not diff.get("trustworthy"):
        points.append(
            build_untrusted_gate_judgment_point(
                id="j-scoping-ambiguous",
                question=(
                    "Session-Id trailer scoping is ambiguous — are the diff counts this "
                    "session's own?"
                ),
                dispositions=[
                    build_disposition(
                        "supply-commit-set",
                        guidance=(
                            "Supply the commit set explicitly; the emitted counts may "
                            "include a peer's commits."
                        ),
                    ),
                    build_disposition(
                        "accept",
                        guidance=(
                            "Accept only if you can name why the trailer is reliable "
                            "here."
                        ),
                    ),
                ],
                evidence=f"scoping_method={diff.get('scoping_method')}",
                reason=(
                    "On a shared branch an ambiguous trailer means the counts may carry "
                    "a peer's commits; only the EM can say whether they do."
                ),
            )
        )
    return points


def _run_close_commit(root: Path, sid: str) -> dict[str, Any]:
    """C5 (docs/plans/2026-08-20-the-close-ceremony-commits-what-the-session-
    wrote.md § C5): call C4's hardened `auto_commit_session_async` in-process
    instead of emitting `safe-commit-offer` as an agent directive that may
    never actually run. Engine-side and deterministic — no prompt, no EM
    decision, no doctrine the operator has to remember.

    Fail-open by construction: any exception here is swallowed into a
    synthesized `"error"`-status `AutoCommitReport` rather than raised into
    `brief()`'s own envelope computation — a failure inside this call must
    never prevent the close ceremony's own decision-object computation from
    completing (AC9's own "does not prevent the ceremony from completing").

    Residue (whatever this call could not attribute to this session) is
    returned unchanged inside the report and stays dirty on disk — the next
    session's own workstream-start dirty-tree read surfaces it structurally,
    without this module writing anywhere new for it (AC9's second half).
    """
    try:
        report = asyncio.run(auto_commit_session_async(sid, str(root), invoker="attended"))
        return dict(report)
    except Exception as exc:  # noqa: BLE001 — must never block brief()'s own computation
        return {
            "session_id": sid,
            "groups": [],
            "excluded": [],
            "failed_groups": [],
            "dropped_groups": [],
            "residue": {},
            "outcome": {
                "status": "error",
                "detail": f"auto_commit_session_async raised {type(exc).__name__}: {exc}",
                "committed_paths": [],
                "conflicted_paths": [],
            },
        }


def brief(worktree_root: Path | None = None) -> dict[str, Any]:
    """Compute `/quick-wrap`'s decision object. READ-ONLY except for C5's named
    carve-out (`_run_close_commit`, see module docstring) — this call commits this
    session's own claimed dirty paths in-process before the envelope is emitted.

    Reads all five close-gate facts off `coordinator_core.session.session_facts`
    (DR-323's cutover, chunk C7 — see module docstring). Each DR-319 record's `value`
    is unwrapped for `_entry_test`/`_judgment_points`/`_directives`, which still consume
    the same bare-dict payload shape as before this cutover; a degraded record has no
    `value` to unwrap, so it is threaded through the closed-fallback/judgment-point
    mechanism instead (module docstring, "DEGRADED-FACT POSTURE") rather than read via
    a bare `.get("value")` that would silently resolve to `None`. `close_gate` itself
    carries the raw DR-319 record for each fact — `source`/`collision`/`degraded`
    travel to this ceremony's own consumers rather than being discarded here.
    """
    root, common_dir, sid = _resolve_session(worktree_root)

    pickup_kind_record = session_facts.session_pickup_kind(root, common_dir, sid)
    governing_plan_record = session_facts.session_governing_plan(root, common_dir, sid)
    diff_record = session_facts.session_diff_brightline(root, common_dir, sid)
    sizings_record = session_facts.session_terminal_sizings(root)
    fold_record = session_facts.session_fold_sidecars(root)

    pickup_kind = (
        pickup_kind_record["value"]
        if not pickup_kind_record["degraded"]
        else _closed_pickup_kind()
    )
    governing_plan = (
        governing_plan_record["value"]
        if not governing_plan_record["degraded"]
        else _closed_governing_plan()
    )
    diff = diff_record["value"] if not diff_record["degraded"] else _closed_diff()
    sizings = (
        sizings_record["value"]
        if not sizings_record["degraded"]
        else {"scanned": 0, "terminal": [], "non_terminal_count": 0}
    )
    fold = (
        fold_record["value"]
        if not fold_record["degraded"]
        else {"present": False, "paths": [], "count": 0}
    )

    entry_test = _entry_test(pickup_kind, governing_plan, diff, root)

    close_gate = {
        "pickup_kind": pickup_kind_record,
        "governing_plan": governing_plan_record,
        "diff": diff_record,
        "terminal_sizings": sizings_record,
        "fold_sidecars": fold_record,
    }

    judgment_points = _judgment_points(sizings, diff)
    if sizings_record["degraded"]:
        judgment_points.append(
            _degraded_probe_judgment_point(
                "j-terminal-sizings-degraded",
                "The terminal sizing-object scan",
                sizings_record["evidence"],
            )
        )
    if fold_record["degraded"]:
        judgment_points.append(
            _degraded_probe_judgment_point(
                "j-fold-sidecars-degraded",
                "The fold-execution-record sidecar scan",
                fold_record["evidence"],
            )
        )

    failures = entry_test["computed_failures"]
    if not failures:
        narration = (
            "Entry-test conditions 1-3 pass on computed fields; condition 4 is yours. "
            f"Session diff: {diff['novel_loc']} novel LOC across "
            f"{diff['novel_commit_count']} novel commit(s) "
            f"(of {diff['commit_count']} total), {diff['novel_surface_count']} "
            f"novel surface(s)."
        )
    else:
        narration = (
            f"Entry test fails on {', '.join(failures)} — {entry_test['route']}. "
            "This is a routing verdict, not an error."
        )

    commit_report = _run_close_commit(root, sid)
    commit_outcome = dict(commit_report.get("outcome") or {})
    commit_outcome["residue"] = commit_report.get("residue") or {}

    envelope = build_envelope(
        artifact={"session_id": sid, "worktree_root": str(root), "ceremony": "quick-wrap"},
        preflight={},
        gates={
            "close_gate": close_gate,
            "entry_test": entry_test,
            "commit_outcome": commit_outcome,
        },
        directives=_directives(fold, fold_degraded=fold_record["degraded"]),
        judgment_points=judgment_points,
        decisions={},
        narration=narration,
        next_move=entry_test["route"],
    )
    _print_commits_into_baton(root, sid)
    return dict(_emit(envelope))


def _print_commits_into_baton(root: Path, sid: str) -> None:
    """C5 (docs/plans/2026-08-18-a-session-always-has-a-baton.md § C5, part
    b): resolve this session's attributed commit shas via C4's
    ``session.commits`` primitive and merge them into the session's baton
    record (``session_baton.store.merge_baton`` — dedup-extends, never
    replaces).

    Compatible with ``brief()``'s own READ-ONLY contract: that docstring
    means "mutates no TRACKED-tree state" (handoffs, plans — the Tier-B
    stateless-computation guarantee); the session baton is the ephemeral,
    ``.git/coordinator-sessions/<sid>/`` -scoped advisory record C1
    establishes, already written unconditionally on every session's first
    prompt (``session_baton_mint``) — a second, best-effort merge here adds
    no new mutation SURFACE, only more complete data on an existing one.

    Fail-open: any resolution/write failure is swallowed — an advisory
    record must never block `/quick-wrap`'s own decision-object computation
    (mirrors `session_baton.store`'s own fail-open posture throughout).
    """
    if not sid:
        return
    try:
        commits = resolve_session_commits(root, sid)
    except (ValueError, RuntimeError):
        return
    shas = [c["sha"] for c in commits]
    if not shas:
        return
    try:
        merge_baton(sid, cwd=str(root), commits=shas)
    except Exception:  # noqa: BLE001 — advisory write must never raise into brief()
        pass


def _usage(stream: Any = None) -> int:
    print("usage: quick-wrap-assemble brief", file=stream or sys.stderr)
    return int(QuickWrapExitCode.USAGE)


def main(argv: list[str]) -> int:
    """`quick-wrap-assemble brief` CLI entrypoint."""
    if argv[:1] and argv[0] in ("--help", "-h"):
        _usage(stream=sys.stdout)
        return int(QuickWrapExitCode.SUCCESS)

    if not argv or argv[0] != "brief":
        return _usage()

    if argv[1:]:
        print(
            f"quick-wrap-assemble: unrecognized argument {argv[1]!r}",
            file=sys.stderr,
        )
        return _usage()

    try:
        decision_object = brief()
    except RuntimeError as exc:
        print(f"quick-wrap-assemble: {exc}", file=sys.stderr)
        return int(QuickWrapExitCode.TRANSPORT)

    print(json.dumps(decision_object, indent=2, sort_keys=True))
    return int(QuickWrapExitCode.SUCCESS)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
