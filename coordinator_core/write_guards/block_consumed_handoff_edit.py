"""coordinator_core.write_guards.block_consumed_handoff_edit — hard-deny guard
(mixed-shape: the claim-holder continuation leg advises non-blocking, the
non-holder/close-intent legs still hard-deny).

Python engine-ification of example-doctrine-repo's retired
``coordinator/hooks/scripts/block-consumed-handoff-edit.sh`` PreToolUse
(Write|Edit|MultiEdit|NotebookEdit) hook (deleted 2026-07-16, example-doctrine-repo
``2f8b8450``), per the naked-Python hook migration (write_guards/INTERFACE.md).

Purpose (ported verbatim from the reference hook): a claimed handoff
(``state/handoffs/*.md`` with frontmatter ``status: claimed``, old-vocabulary
``status: consumed`` still recognized on read) is paper trail, not a live
progress journal. A picked-up handoff has been claimed
(``status: claimed``, ``claimed_by: <session>``) and the next session is
supposed to find a SUCCESSOR handoff via ``/handoff`` chain-archival.
Appending progress in-place to the consumed predecessor corrupts the audit
trail, bypasses the carry-forward machinery, and is invisible to the pickup
index. This guard DENIES the edit and OFFERS the available routes by naming
them in the deny message — it writes nothing to disk (design-as-offers — the
deny leads with the alternative, but the alternative is a named command, not
a pre-built artifact). A close-intent edit is routed to the ship op instead
of the continuation route (``_is_close_intent``; 2026-07-28 orphan-baton
fix, example-retrieval-repo-em memo). The deny text also offers a distinct terminal-close route
(``archive-stamp-cli ship-handoff`` — example-doctrine-repo's ergonomic veneer over
``handoff.archive_transition`` / ``mode: stamp_only``) alongside the
continuation route, so an operator CLOSING/shipping the workstream is not
funneled into the recovery-only override (2026-07-21 closure-friction fix,
memo Finding 1; deny repointed onto the ship-handoff verb once it landed
Example-doctrine-repo-side in b22f973d). A third named route, in the continuation branch
only, covers an author CORRECTING a factual error in a body they wrote:
``handoff.correct_body``, a narrow possession-gated (post-C2), prose-only op
invoked via the native ``coordinator_core.invoke`` form — never through the Edit
tool, so this guard never sees it and never needs to allow anything (same
guard-safe-node-write shape as the close route above). The override's
sanctioned-use list dropped "a one-off paper-trail correction" once this
route existed, since pointing that case at an in-session-unreachable env
var was the defect the op closes (2026-07-31 claimed-baton-body-correction
plan). The continuation-branch deny message is now ORDERED by applicability
to the calling session (2026-08-06 executing-session-can-discharge-criteria
plan, C4): ``claimed_by`` is read via the existing ``_extract_fm_field`` and
compared against the calling session's id — resolved via the same
three-variable precedence chain ``handoff_correct_body`` walks
(``COORDINATOR_SESSION_ID`` > ``CLAUDE_SESSION_ID`` >
``CLAUDE_CODE_SESSION_ID``) — to tell a claim holder from a non-holder. A
holder is led straight to ``handoff.correct_body``/``handoff.propagate``, a
non-holder is led to the liveness-gated pickup route (a sanctioned door onto
possession that will not steal a live peer's claim) before the recovery-only
``override_reason`` mention, never the reverse.

RESHAPE (2026-08-06 apply-guard-class-census plan, C18a): the holder
predicate above stopped being deny-TEXT-only. Five widenings, two friction
memos, a new op (``handoff.correct_body``) and a ratified DR were built to
route AROUND this guard's continuation-branch deny for the case where the
calling session IS the claim holder — while the predicate that actually
distinguishes that case from a genuine non-holder edit had been sitting
inside this module the whole time, used only to reorder deny prose. A
session editing its OWN claimed handoff is not the audit-trail-corruption
case this guard exists to stop; it is normal in-flight progress on a baton
the session legitimately holds. The holder leg now returns a non-blocking
``additionalContext`` envelope (no ``permissionDecision``) naming the same
chain-aware routes instead of denying — CLASS stays ``hard-deny`` (the
close-intent leg and the non-holder leg both still hard-deny; this module
mixes blocking and non-blocking outputs from one ``CLASS = "hard-deny"``
registration, the same shape ``validate_frontmatter_schema_deny.py`` uses
for its own advisory-shaped ``additionalContext`` returns). The non-holder
leg is UNCHANGED — genuine audit-trail corruption by a session that does
not hold the claim is still hard-denied.

A fourth named route, also continuation-branch-only, covers a `blocked_by`
edge on a roadmap baton: ``roadmap.link_stubs``, the first op that authors
that field (2026-08-05 roadmap-dependency-graph-enforcement-gap plan, C5).
Before this op existed, an operator wanting to record a dependency edge had
no writer to reach for and no route this guard could name — ``correct_body``
asserts frontmatter byte-identity and could never have written it, which is
exactly how the sender of the memo motivating this chunk lost time. Routing
to ``roadmap.link_stubs`` here is what actually opens an ungated path onto
the files this guard protects: an op invocation does not pass through
PreToolUse guards the way an Edit/Write does, so naming the op in this deny
text is the point at which the guard stops being the only gate on a
roadmap-baton frontmatter write. The substitute control is
``roadmap.link_stubs``'s own narrow two-field allowlist (``blocked_by``/
``blocks`` only, roadmap batons only, per that op's module docstring and
the C4 chunk body it was authored under) — the allowlist is what bounds the
path this routing opens, not this guard. Widening that op's field set would
reopen a guarded surface this routing change silently bypasses; that is the
reasoning behind the plan's Anti-scope bullet forbidding
``roadmap.link_stubs`` from becoming a general frontmatter writer.

This is a faithful engine-ification, not a redesign: it ports the reference
hook's candidate extraction (top-level ``file_path``, or ``edits[].file_path``
for ``MultiEdit`` — first match wins, tried in jq ``//`` order), backslash
normalization, ``..``-traversal rejection, git-root-relative containment
check against ``state/handoffs/``, the ``state/handoffs/<name>.md`` path
shape gate, and the awk-equivalent frontmatter ``status:`` extraction (first
frontmatter block only, inline-comment strip, quote strip) rather than
re-deriving them. One ported behavior was deliberately DROPPED, not carried
forward: the reference hook's successor-scaffold write. It ran unconditionally
on the deny path, before the operator had chosen among the message's named
exits — three of which (ship-handoff close, the recovery override,
abandoning) want no successor at all. The written artifact was itself
pickup-eligible (``status: open`` / ``deployment_state: ready_to_fire``)
next to a predecessor that could still have a live claim holder, and it
copied the predecessor's (possibly stale) ``title`` verbatim into the new
file. Removed 2026-07-28 (example-retrieval-repo-em memo,
``cross-repo/inbox/2026-07-28-example-retrieval-repo-em-claimed-handoff-guard-should-not-prescaffold-successor.md``)
— the deny message names the ``/handoff`` continuation route instead of
pre-building it.

Negative-spec:
  - Does NOT check ``tool_input.notebook_path`` for ``NotebookEdit`` — the
    reference hook's jq extraction only reads ``.tool_input.file_path`` /
    ``.tool_input.edits[].file_path``, which is empty for a bare
    ``NotebookEdit`` payload; that tool is matched (for hook-registration
    parity) but never produces a candidate in practice, exactly mirroring
    the reference hook's behavior.
  - Does NOT use ``coordinator_core.ops._path_guard.contained_path`` for the
    ``state/handoffs/`` containment check — that check operates on a
    normalized-but-UNRESOLVED string prefix match (mirroring the reference
    hook's ``cd+pwd -P`` canonicalization then literal ``case`` prefix test),
    not a symlink-resolved containment against a fixed root set; porting it
    through ``contained_path`` would change the semantics.
  - Does NOT fail closed on any error — every subprocess/file-read failure
    degrades gracefully (fewer candidates matched), matching the reference
    hook's ``set -uo pipefail`` (no ``-e``) plus pervasive ``|| true``
    discipline.
  - Does NOT write anything to disk, ever — the deny message IS the whole
    offer. Do not reintroduce a successor-scaffold write (see the dropped-
    behavior paragraph above); a test regression pin
    (``test_guard_writes_nothing``) exists specifically to catch this.

Spec backlink: coordinator/hooks/scripts/block-consumed-handoff-edit.sh (header doc,
  retired 2026-07-16, example-doctrine-repo ``2f8b8450``)
Ported from the retired example-doctrine-repo bash guard ``block-consumed-handoff-edit.sh``
  (deleted 2026-07-16, example-doctrine-repo ``2f8b8450``).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.session.core import SESSION_ENV_PRECEDENCE
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 50

#: Escape hatch (reference hook lines 34-39, 55-57) — recovery-only.
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT"

#: '..' as a full path component (reference hook line 99).
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")

#: Handoff-file path shape gate (reference hook line 120).
_HANDOFF_PATH_RE = re.compile(r"(^|/)state/handoffs/[^/]+\.md$")

#: A frontmatter ``<key>: <value>`` line, for close-intent shape detection.
_FM_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)$")

#: Frontmatter keys that exist ONLY to record a terminal disposition — their
#: presence in an edit's ``new_string`` is close-intent on its own, whatever
#: ``deployment_state`` the same edit carries.
_CLOSE_ONLY_KEYS = frozenset(
    {"shipped_in", "shipped_in_kind", "closed_reason", "continued_into"}
)

#: Terminal ``deployment_state`` values — the SSOT enum, imported not
#: redefined. Note this is the FOUR-value lifecycle set (``abandoned``
#: included): abandoning a workstream is a close, and wants no successor
#: either. Deliberately NOT
#: ``coordinator_core.ops.handoff_stamp._TERMINAL_DEPLOYMENT_STATES``, which
#: is that repair door's own narrower three-value set, and whose ``ops``
#: package costs ~140ms to import on a guard that runs on every Write/Edit.
#: ``lifecycle_constants`` has no imports of its own, so the marginal cost
#: here is the already-paid ``coordinator_core`` package init.
_TERMINAL_DEPLOYMENT_STATES = HANDOFF_TERMINAL_DEPLOYMENT

#: Frontmatter ``<field>: <value>`` line matcher, parameterized per field.
def _field_re(field: str) -> "re.Pattern[str]":
    return re.compile(r"^" + re.escape(field) + r":[ \t]*(.*)$")


def _collapse_slashes(value: str) -> str:
    """Backslash -> slash, collapse slash runs (reference hook lines 94-95)."""
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _resolve_git_root(cwd: Optional[str]) -> Optional[str]:
    """``git rev-parse --show-toplevel`` (reference hook line 107). Fails open.

    AC4 (docs/plans/2026-08-07-no-window-subprocess-primitive.md, chunk C3b):
    delegates to the shared, process-lifetime-memoized
    ``write_guards._repo_root.resolve_repo_root`` instead of hand-rolling its
    own spawn — same fail-open-to-``None`` contract as the prior inline
    ``subprocess.run``, so no verdict changes. The prior inline spawn also
    logged a forensic diagnostic on ``OSError`` ("treating as no git root");
    the shared resolver swallows all failures silently (never raises), so
    that diagnostic is restored here explicitly rather than lost -- verdict
    is still unaffected either way (``_normalize_and_gate`` already treats
    ``None`` the same as any other unresolved git root).
    """
    result = resolve_repo_root(cwd)
    if result is None:
        print(
            f"block_consumed_handoff_edit: no git root resolved for cwd="
            f"{cwd!r}, treating as no git root (decision unaffected)",
            file=sys.stderr,
        )
    return result


def _basename(path: str) -> str:
    return _collapse_slashes(path).rsplit("/", 1)[-1]


def _extract_candidates(payload: Dict[str, Any]) -> List[str]:
    """Port of the jq ``.tool_input.file_path // (.tool_input.edits[]?.file_path)``
    OR-resolver (reference hook lines 75-84): a truthy top-level ``file_path``
    yields a single candidate; otherwise every ``edits[].file_path`` (MultiEdit)
    is enumerated.
    """
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    fp = tool_input.get("file_path")
    if fp:
        return [fp]
    out: List[str] = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                efp = edit.get("file_path")
                if efp:
                    out.append(efp)
    return out


def _normalize_and_gate(cand: str, git_root: Optional[str]) -> Optional[str]:
    """Normalize + traversal-reject + containment-gate + path-shape-gate a
    single candidate (reference hook lines 91-120). Returns the normalized
    string, or ``None`` if this candidate should be skipped (``continue`` in
    the reference hook's while-loop).
    """
    cn = _collapse_slashes(cand)

    # Security: reject '..' traversal (reference hook lines 96-99).
    if _TRAVERSAL_RE.search(cn):
        return None

    # Security: resolve against git root and confirm containment under
    # <git-root>/state/handoffs/ (reference hook lines 100-118). Only applied
    # when a git root resolved — mirrors the reference hook's
    # `if [[ -n "$_git_root" ]]` gate.
    if git_root:
        if cn.startswith("/") or re.match(r"^[A-Za-z]:", cn):
            abs_cn = cn
        else:
            # `rstrip("/\\")`, never `rstrip("/")` -- a trailing BACKSLASH
            # (e.g. a drive-root `git_root` of `X:\`) survives the latter and
            # composes a double-slash prefix below (see the matching note on
            # `expected_prefix`).
            abs_cn = git_root.rstrip("/\\") + "/" + cn
        try:
            abs_cn_canon = casefold_path(str(Path(abs_cn).resolve(strict=False)))
        except OSError as exc:
            print(f"block_consumed_handoff_edit: path resolve failed for "
                  f"{abs_cn!r}, gating on unresolved form: {exc}", file=sys.stderr)
            abs_cn_canon = abs_cn
        # Case-folded per block_memo_status_hand_edit.py's own note: git_root
        # is real on-disk casing, but the candidate segment appended onto it
        # is caller-supplied and may differ only in case on a case-insensitive
        # filesystem — comparing un-folded would silently miss that bypass.
        # `rstrip("/\\")`, never `rstrip("/")` -- see block_memo_status_hand_edit.py's
        # `_normalize_and_gate` for the drive-root double-slash-inertness this avoids.
        expected_prefix = casefold_path(git_root.rstrip("/\\") + "/state/handoffs/")
        if not abs_cn_canon.startswith(expected_prefix):
            return None

    # Only handoff files under state/handoffs/ (reference hook lines 119-120).
    if not _HANDOFF_PATH_RE.search(cn):
        return None

    return cn


def _extract_fm_field(text: str, field: str) -> str:
    """Port of the reference hook's awk frontmatter field extraction
    (``_extract_fm`` / the STATUS-extraction awk block, lines 125-183):
    reads the FIRST frontmatter block (between the first two bare ``---``
    lines) and returns the value of the first ``<field>:`` line found, with
    an inline ``#`` comment and surrounding whitespace/quotes stripped.
    """
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return ""
    pattern = _field_re(field)
    for line in lines[1:]:
        if line.rstrip() == "---":
            break
        m = pattern.match(line)
        if m:
            val = m.group(1)
            val = re.sub(r"[ \t]*#.*$", "", val)
            val = val.strip()
            val = re.sub(r"^['\"]", "", val)
            val = re.sub(r"['\"]$", "", val)
            return val
    return ""


def _replacement_strings(payload: Dict[str, Any]) -> Optional[List[str]]:
    """The ``new_string`` payload(s) of an ``Edit``/``MultiEdit``.

    Returns ``None`` when the tool carries no such signal at all — ``Write``
    (whole-file ``content``) and ``NotebookEdit`` — so callers can distinguish
    "no signal available" from "signal present and empty".
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in ("Edit", "MultiEdit"):
        return None
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    edits = tool_input.get("edits")
    if isinstance(edits, list):
        return [
            e.get("new_string") or ""
            for e in edits
            if isinstance(e, dict)
        ]
    ns = tool_input.get("new_string")
    return [ns] if isinstance(ns, str) else None


def _is_close_intent(payload: Dict[str, Any]) -> bool:
    """Does this blocked edit read as CLOSING the handoff rather than
    continuing it?

    Close-intent edits are routed by the deny text onto ``archive-stamp-cli
    ship-handoff``, a path that needs no successor — scaffolding one there
    leaves an empty, pickup-index-visible baton on a workstream that already
    shipped (example-retrieval-repo-em memo, 2026-07-28).

    The signal is the replacement text, not the file's current state: an
    operator stamping a terminal disposition writes frontmatter keys and
    nothing else. Deliberately conservative — EVERY replacement must be
    frontmatter-shaped (so a progress append that happens to mention
    ``shipped_in`` in prose is a continuation, and still gets its scaffold),
    and at least one must assert a terminal disposition.

    Negative-spec:
      - Reading the file's CURRENT terminal state instead would not work: a
        close edit's whole purpose is that the file is not yet stamped.
      - ``Write`` and ``NotebookEdit`` carry no ``new_string``; they are never
        close-intent by this test and keep the scaffold.
    """
    replacements = _replacement_strings(payload)
    if not replacements:
        return False

    saw_terminal = False
    for replacement in replacements:
        for line in replacement.splitlines():
            if not line.strip() or line.strip() == "---":
                continue
            m = _FM_KV_RE.match(line)
            if m is None:
                # A body line — this is a progress append, not a close stamp.
                return False
            key = m.group(1)
            if key in _CLOSE_ONLY_KEYS:
                saw_terminal = True
            elif key == "deployment_state":
                value = re.sub(r"[ \t]*#.*$", "", m.group(2)).strip().strip("'\"")
                if value in _TERMINAL_DEPLOYMENT_STATES:
                    saw_terminal = True

    return saw_terminal


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        candidates = _extract_candidates(payload)
        if not candidates:
            return None

        cwd = payload.get("cwd") or None
        git_root = _resolve_git_root(cwd)
        base_dir = Path(cwd) if cwd else Path.cwd()

        # Walk candidates: find the first one that is a consumed handoff
        # (reference hook lines 89-147).
        file_path = ""
        disk_path: Optional[Path] = None
        for cand in candidates:
            cn = _normalize_and_gate(cand, git_root)
            if cn is None:
                continue
            candidate_disk = Path(cn) if Path(cn).is_absolute() else (base_dir / cn)
            if not candidate_disk.is_file():
                continue
            try:
                text = candidate_disk.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"block_consumed_handoff_edit: could not read candidate "
                      f"{candidate_disk}, skipping: {exc}", file=sys.stderr)
                continue
            status = _extract_fm_field(text, "status")
            if status in ("claimed", "consumed"):
                file_path = cand
                disk_path = candidate_disk
                break

        if not file_path or disk_path is None:
            return None

        # A close-intent edit is routed below onto `archive-stamp-cli
        # ship-handoff`, which needs no successor. Any other edit is routed
        # onto the `/handoff` continuation route (example-retrieval-repo-em memo,
        # 2026-07-28: the guard names the route, it does not pre-build it).
        close_intent = _is_close_intent(payload)

        # ─── Emit deny (reference hook lines 237-259) ────────────────────────
        # Prose kept under MESSAGE_PROSE_CAP_BYTES (docs/plans/2026-08-02-
        # guard-message-size-discipline.md, chunk C8): each named route gets
        # its own "<verb> instead:" cue so its backtick command lands inside
        # an exempt cue window (coordinator_core.bash_guards._message_size).
        if close_intent:
            _note = operator_override_note(_OVERRIDE_ENV, payload=payload, git_root=git_root)
            reason = (
                "Claimed-handoff close blocked: hand-editing the terminal "
                "stamp corrupts the audit trail. Ship instead: "
                f"`archive-stamp-cli ship-handoff {file_path} --sha <SHA>` "
                "(stamps shipped_in + deployment_state: shipped; add "
                "--archive to move now). Ops list: "
                "`docs/reference/em-callable-ops.md`."
                + ("\n\n" + _note if _note else "")
            )
        else:
            # Remedies are ordered by applicability to THIS calling session
            # (AC7, the Staff Engineer F4): possession is acquirable through a
            # sanctioned door (claim the baton), authorship was not — so a
            # non-holder is led to the claim route before the recovery-only
            # override, rather than to the override first. `claimed_by` is
            # read via the guard's own `_extract_fm_field` (no new import;
            # `pickup_assemble` measured ~138ms vs. this guard's ~8.4ms
            # full-import budget, a ~16x regression on this hot path).
            #
            # Session identity is resolved via the SAME three-variable
            # precedence chain `handoff_correct_body._resolve_session_id_
            # with_source` walks — both now consume the single canonical
            # `coordinator_core.session.core.SESSION_ENV_PRECEDENCE` rather
            # than each inlining/copying it (chain-review slice D, F1: this
            # guard previously read ONLY `COORDINATOR_SESSION_ID`, the op's
            # documented "explicit test override" tier, while the op it
            # routes to walked the full chain — a real, platform-injected
            # session with only `CLAUDE_CODE_SESSION_ID` set was classified
            # a non-holder). `session.core`'s import cost (~12ms, measured)
            # is well under the ~138ms `pickup_assemble`/`coordinator_core.
            # ops` cost this guard's hot-path budget was written to avoid —
            # see that module's own import-cost comment above.
            # Ledger-first: a desynced baton (live ledger claim, reverted/
            # empty frontmatter mirror) must not silently read as "no
            # holder" -- that used to let this guard stay silent on an
            # in-place edit to a claimed predecessor (the exact negative-
            # spec the pickup skill states). `resolve_claim_state` is the
            # one shared accessor (docs/plans/2026-08-07-claim-state-
            # ledger-first-authoritative-read.md, C1); a dead ledger holder
            # already degrades `source` to "mirror"/"none" inside it, so
            # this guard need not special-case liveness itself. Falls back
            # to the pre-migration mirror-only read (`_extract_fm_field`)
            # on any resolution failure -- never regresses below what this
            # guard already had.
            claimed_by = _extract_fm_field(text, "claimed_by")
            try:
                state = resolve_claim_state(disk_path, repo_root=git_root)
            except Exception:
                pass
            else:
                if state.holder is not None:
                    claimed_by = state.holder
            session_id = ""
            for var in SESSION_ENV_PRECEDENCE:
                session_id = os.environ.get(var, "")
                if session_id:
                    break
            is_holder = bool(session_id) and session_id == claimed_by
            if is_holder:
                # The predicate this guard already computed (the Staff Engineer F4/AC7)
                # is now load-bearing, not just prose-ordering: a session
                # that IS the claim holder is not the case this guard exists
                # to stop — appending progress to its own claimed baton is
                # exactly what the SUCCESSOR-chain mechanics assume happens
                # before `/handoff`, not the audit-trail corruption the
                # non-holder path denies. Five widenings, two friction
                # memos, a new op (`handoff.correct_body`) and a ratified DR
                # were built to route around this guard's deny for the
                # holder case; this leg is the actual fix (2026-08-06
                # apply-guard-class-census plan, C18a). Non-blocking:
                # `additionalContext`, no `permissionDecision` — the holder
                # can proceed, this is guidance, not a stop.
                reason = (
                    "Claimed handoff: you hold this claim. Prefer the "
                    "chain-aware routes over a hand-edit — correct/deliver: "
                    "`handoff.correct_body` (`possession-gated`) via "
                    "`coordinator_core.invoke`, or `handoff.propagate`. "
                    "Continue: `/handoff`. Close: "
                    f"`archive-stamp-cli ship-handoff {file_path}` (stamps "
                    "`shipped_in`+`deployment_state: shipped`; `recovery`-"
                    "only override; see "
                    "`docs/wiki/pretooluse-write-guards.md`). Roadmap "
                    "dependency edge: `roadmap.link_stubs` (writes "
                    "`blocked_by`/`blocks` on a roadmap baton only). Ops "
                    "list: `docs/reference/em-callable-ops.md`."
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": reason,
                    }
                }
            _note = operator_override_note(_OVERRIDE_ENV, payload=payload, git_root=git_root)
            reason = (
                "Claimed handoff: paper trail, not a live journal; edit "
                "blocked. Not your claim — claim instead via the "
                "liveness-gated pickup path: `/pickup` (or "
                "`pickup_assemble`, which refuses a takeover while the "
                f"current holder is live). Correct/deliver instead: "
                "`handoff.correct_body` (`possession-gated`; needs "
                "`override_reason` if you are neither holder nor "
                "author) via `coordinator_core.invoke`, or "
                "`handoff.propagate` (no authorship gate). Continue "
                "instead: `/handoff`. Close "
                f"instead: `archive-stamp-cli ship-handoff {file_path}` "
                "(stamps `shipped_in`+`deployment_state: shipped`; "
                "`recovery`-only override; see "
                "`docs/wiki/pretooluse-write-guards.md`). Roadmap "
                "dependency edge instead: `roadmap.link_stubs` (writes "
                "`blocked_by`/`blocks` on a roadmap baton only). Ops "
                "list: `docs/reference/em-callable-ops.md`."
                + ("\n\n" + _note if _note else "")
            )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:
        # Reference hook fails open on any unexpected condition (`set -uo
        # pipefail` without `-e`, pervasive `|| true`) — never fabricate a
        # deny on a guard crash (INTERFACE.md fidelity rule 6).
        return None
