"""coordinator_core.bash_guards.guard_multiprobe_banner -- BX-7, the
multi-probe banner-command guard (the 40.1% shape:
`docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md` § BX-7,
upstream `docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md` § C4).

Purpose: a single Bash call that echoes a `===`-delimited banner and then
runs several unrelated probe commands in the same call is, per the
1,389-transcript / 62,487-call fork-tax measurement this row cites, the
second-most common bash-spawn shape after grep-via-Bash. Most of what such
a chain re-collects (branch, HEAD sha, staged files, cwd, user, platform)
is already known or cheaply re-derived in one process -- session-fact
RE-DERIVATION rates were measured at 89% / 84% / 71% / 49%, i.e. agents
re-probe facts the session already holds, repeatedly, within one session.

Detection is entirely delegated to `_shape_classifier.classify_command`
(tokenizer-based, never regex-over-raw-command-text -- this module
contains no command-shape regex of its own). Rendering is delegated to
`_platform_verdict.platform_verdict_for_shape`, called with a fixed
``host_is_windows=False`` (DR-280, 2026-08-07: this guard's own Windows
deny leg retired as structurally unreachable -- see `check()`'s own
docstring) -- this guard now only ever advises, never denies.

REWRITE TARGET -- gate re-checked at authoring time, and it now EXISTS
------------------------------------------------------------------------
A guard may deny only toward a rewrite target the BX-16 seam can actually
emit. At the start of this task no sixth BX-16 check existed for this
shape; re-checked immediately before shipping this module, a concurrent
chunk had, in the interim, landed
`dispatch_checks.check_multiprobe_banner_rewrite` (registered in
`dispatch.py` as `"multiprobe-banner-rewrite"`, ahead of `offer-git-c`,
returning `_allow_rewrite(...)` for every probe segment it recognizes and
an advisory-with-skeleton fallback otherwise) -- a genuine, working
auto-rewrite into a single `python3 -c` one-liner that batches every probe
(`pwd`, `whoami`, `date`, `uname`, an echoed banner line, and every `git`
fact folded into ONE `git status --porcelain=v2 --branch` call) into one
process. This module's deny/advisory text below names that real mechanism
-- it does not invent one, and it does not merely gesture at "a Bash-to-
Bash equivalent" in the abstract.

THIS MODULE DOES NOT RUN THE REWRITE ITSELF. `check_multiprobe_banner_
rewrite` is registered separately in `dispatch.py`'s chain (a distinct
guard, `"multiprobe-banner-rewrite"`) and is the one that returns
`allow`+`updatedInput`; per its own docstring it "never denies -- BX-7 owns
the Windows deny policy for THAT residue, this row only owns the rewrite
target." This module is that Windows deny policy: it classifies the SAME
shape independently (via `_shape_classifier`, not by importing the rewrite
check's classification) and denies on Windows / advises on macOS,
describing the rewrite that the sibling guard performs.

2026-07-29 (duty-of-care promotion, C -> B, "one detected shape may deny
only toward a CONFIRMED outlet" gap fix): this module used to render its
deny/advisory around a FIXED, generic ``outlet_example`` regardless of
whether the sibling rewrite chain entry would actually produce a rewrite
for THIS exact command -- a caller-text-inventory finding
(`state/audits/2026-07-29-bash-guard-caller-text-inventory.md` § 2, DoE
repo) named this the cheapest available promotion in this row ("the
sibling `check_multiprobe_banner_rewrite` chain entry already computes the
literal per-command rewrite; this guard's own message could reuse that
string instead of a fixed generic example"). Independently, it was also a
correctness gap of the exact shape `guard_plumbing_and_loops.py` already
fixed for its own two shapes (see that module's docstring,
"GATED INDEPENDENTLY, PER SHAPE, AGAINST WHAT BX-16'S SEAM CAN ACTUALLY
EMIT"): denying on Windows toward a generic example when the seam has NOT
actually confirmed a rewrite for this exact command is denying toward a
target that may not exist, the precise deny-before-rewrite hazard the
plan's sequencing rule forbids -- `check_multiprobe_banner_rewrite` returns
a bare, no-`updatedInput` advisory (not a confirmed rewrite) whenever ANY
probe segment in the chain is unrecognized, which is common. This module
now mirrors `guard_plumbing_and_loops.py`'s already-reviewed seam-calling
architecture instead of re-deriving a parallel judgment of "is this
command actually rewritable": it CALLS `check_multiprobe_banner_rewrite`
(the same function the separate `"multiprobe-banner-rewrite"` chain entry
uses to perform the ACTUAL rewrite) purely to read its return value's
`updatedInput.command` when present, and renders the deny/advisory example
around that literal string. This module still never applies a rewrite
itself (no `updatedInput` in anything THIS module returns) -- see "THIS
MODULE DOES NOT RUN THE REWRITE ITSELF" above, unchanged -- it only reads
the sibling's confirmation before deciding whether to deny toward a
concrete target or (2026-08-06, see below) allow silently.

SUBAGENT-AWARE OUTLET, AND DROPPING THE UNDISCHARGEABLE GENERIC ADVISORY
-------------------------------------------------------------------------
2026-08-06 (`state/audits/2026-08-06-architecture-survey-analyst-friction-
digest.md` finding B2, seven of thirteen analysts hit it): two defects,
fixed together because both are about this guard recommending an outlet
the calling agent cannot actually use.

(1) The seam-confirmed leg used to name `python3 -c "..."` as THE outlet
    unconditionally. For a subagent caller that is a dead end:
    `block_subagent_destructive_action` classifies `python3 -c '<inline>'`
    as an "unparseable indirection wrapper" and denies it, with "no
    subagent-honored override for this guard" -- so a subagent following
    this guard's own advice walks straight into the sibling guard's deny.
    `check()` now resolves caller class via `_write_bump_message.
    resolve_agent_class` (the established EM-vs-subagent OR-resolver,
    already used by `bump_foreign_repo_write.py` for the identical
    question) -- skipped entirely (no `resolve_git_root` subprocess paid)
    when the payload carries neither `agent_id` nor `agent_type`, mirroring
    `block_subagent_destructive_action.check`'s identical no-agent-id
    short-circuit -- and, for a subagent, `_outlet_from_seam_result` renders
    `_subagent_script_outlet`'s form instead: write the seam's own script
    body (recovered from its `-c <shlex.quote(...)>` argv, not
    re-derived) to a file under the session scratchpad
    (`_sandbox_script_hint`, mirroring `bump_foreign_repo_write.
    _sandbox_root_hint` / `bump_outside_repo_write._sandbox_root`'s
    already-established always-allowed subagent write target) and invoke
    it as `python3 <path>` -- an outlet neither guard denies. Main-loop
    callers are unaffected: they still get the bare `python3 -c` rewrite.

(2) The unconfirmed-seam leg used to render a FIXED generic advisory
    (`_GENERIC_OUTLET_SUMMARY`/`_GENERIC_OUTLET_EXAMPLE`, a `pwd`/`whoami`/
    `git status` template) regardless of what the command actually
    contained -- an analyst hit this on a command with NONE of those three
    probes, receiving a message that misdescribed their own command and,
    for a subagent, an override note the note's own text says is "for a
    human operator, not this agent". A message that misdescribes the
    command AND offers no action the caller can take is the nag shape
    "design tooling as offers, not nags" forbids. `check()` now returns
    `None` (allow, silently) in this branch instead -- there is no
    per-command outlet to offer, so this guard says nothing rather than
    repeating a warning it cannot discharge. `_generic_advisory` and the
    two constants it rendered are removed; see this module's git history
    for the prior generic-advisory text if it is ever needed again.

Negative-spec
-------------
  - Does NOT reimplement `_shape_classifier`'s MULTI_PROBE_BANNER
    detection or `_platform_verdict`'s envelope shaping -- both are
    imported, never duplicated.
  - Does NOT fire when MULTI_PROBE_BANNER is present but NOT the
    precedence winner (`classification.primary`) -- a command that is
    simultaneously grep-via-Bash and a banner-probe is that OTHER guard's
    business first (AC-7: a deny/advisory message must never misdescribe
    what tripped it, and this module's own message specifically claims
    "multi-probe banner", so it must only speak when that is the correct
    diagnosis).
  - Does NOT reuse `check_multiprobe_banner_rewrite`'s per-segment
    classification (`_bt_probe_segment_kind`) directly -- only its already-
    computed RETURN VALUE (the rendered `updatedInput.command` or bare
    advisory), consumed exactly the way `guard_plumbing_and_loops.py`
    consumes `check_head_tail_plumbing_rewrite`/`check_find_exec_rewrite`.
    This is "consume, don't rebuild" (that module's own phrase): reading a
    sibling's already-computed answer is not re-implementing its
    computation.
  - Does NOT read any environment variable to key its own shape detection
    or platform verdict -- the only environment read is this guard's own
    escape hatch, `COORDINATOR_OVERRIDE_MULTIPROBE_BANNER`, read inline at
    `check()` call time per this package's established F2 discipline
    (never hoisted to module scope). This is a DIFFERENT variable from the
    rewrite guard's own `COORDINATOR_ALLOW_MULTIPROBE_BANNER` -- one
    bypasses this guard's deny/advise, the other bypasses the sibling
    rewrite guard's auto-rewrite; they gate different guards and are not
    interchangeable. Calling `check_multiprobe_banner_rewrite` for its
    return value does not change this: that function reads its OWN
    override inline, on its own call, and returns `None` when it fires --
    this module treats that `None` identically to "no confirmed outlet"
    (allows silently, per the 2026-08-06 change above), never as a reason
    to skip its own override check.

Spec backlink: DoE-claude:pln-windows-viability-stop-the-spa-b969d9 § BX-7
Spec backlink (shape/verdict primitives): DoE-claude:pln-fleet-wide-bash-spawn-fan-out--2f6552 § C2, C6
Spec backlink (rewrite target this guard denies toward): coordinator_core/bash_guards/dispatch_checks.py::check_multiprobe_banner_rewrite
Spec backlink (seam-calling architecture this module now mirrors): coordinator_core/bash_guards/guard_plumbing_and_loops.py
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from coordinator_core.bash_guards._platform_verdict import platform_verdict_for_shape
from coordinator_core.bash_guards._command_tokenizer import (
    exceeds_tokenizable_ceiling as _exceeds_tokenizable_ceiling,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
)
from coordinator_core.bash_guards._shape_classifier import (
    SHAPE_PRECEDENCE,
    Shape,
    ShapeClassification,
    classify_command,
)
from coordinator_core.bash_guards._shape_classifier import (
    _detect_find_exec_xargs,
    _detect_grep_via_bash,
    _detect_head_tail_plumbing,
    _detect_multi_probe_banner,
)
from coordinator_core.bash_guards.dispatch_checks import check_multiprobe_banner_rewrite
from coordinator_core.bash_guards._helpers import operator_override_note, resolve_git_root
from coordinator_core.bash_guards._write_bump_message import (
    AGENT_CLASS_SUBAGENT,
    resolve_agent_class,
)

_GUARD_NAME = "guard_multiprobe_banner"


def _classify_for_dialect(cmd: str, dialect: Optional[Dialect]) -> ShapeClassification:
    """Dialect-aware analogue of `_shape_classifier.classify_command` (row 9,
    `docs/reference/guard-dialect-coverage.md`). BASH is delegated UNCHANGED
    to `classify_command` -- zero behaviour change on the bash leg (AC4).

    POWERSHELL routes its tokenize call through `_dialect.
    resolve_segments_for_dialect` (the C2 wiring seam) instead of
    `classify_command`'s own `tokenize_full_command` call, then reuses the
    SAME per-shape detector functions `classify_command` itself calls, in
    the SAME `SHAPE_PRECEDENCE` order -- no new verb/flag table, per the
    triage row's own finding that detection here is structural
    (separator-count), not verb-specific.

    `Shape.FOR_LOOP` is skipped (never matches) on the POWERSHELL branch:
    its detector (`_detect_for_loop`) keys on the bash `for ... ; do ...
    done` structural grammar, which has no PowerShell analogue
    (`foreach ($x in ...) {...}` is a different statement grammar, not a
    verb/flag variant) -- guessing here is exactly what this guard's own
    dialect seam exists to avoid; guessing wrong risks a false clean on a
    shape this guard does not detect for PowerShell, not the multi-probe
    banner shape this row actually owns. `guard_multiprobe_banner` never
    reads `Shape.FOR_LOOP` itself (only `Shape.MULTI_PROBE_BANNER`'s
    precedence position relative to `Shape.GREP_VIA_BASH`, both above it in
    `SHAPE_PRECEDENCE`, matters here), so this omission cannot change this
    guard's own verdict either way.
    """
    if dialect is Dialect.BASH:
        return classify_command(cmd)

    segments = resolve_segments_for_dialect(cmd, dialect, guard_name=_GUARD_NAME)
    if segments is None:
        return ShapeClassification(tokens=None, matches=())

    matches = []
    for shape in SHAPE_PRECEDENCE:
        match = None
        if shape is Shape.GREP_VIA_BASH:
            match = _detect_grep_via_bash(segments)
        elif shape is Shape.MULTI_PROBE_BANNER:
            match = _detect_multi_probe_banner(segments)
        elif shape is Shape.HEAD_TAIL_PLUMBING:
            match = _detect_head_tail_plumbing(segments)
        elif shape is Shape.FOR_LOOP:
            match = None
        elif shape is Shape.FIND_EXEC_XARGS:
            match = _detect_find_exec_xargs(segments)
        if match is not None:
            matches.append(match)

    tokens = [tok for seg_tokens, _pipe in segments for tok in seg_tokens]
    return ShapeClassification(tokens=tokens, matches=tuple(matches))

#: Review: code-reviewer -- Finding 5 (nit): vestigial in `bash_guards` --
#: see `guard_grep_via_bash.py`'s identical comment above `CLASS`/
#: `MATCHERS`/`PRIORITY` for the full explanation. `dispatch.py` hardcodes
#: ordering explicitly; this `PRIORITY` governs nothing (it is not unique
#: either -- `block_worktree_creation` reuses `41`).
CLASS = "hard-deny"
#: HELD at Bash-only (C4, docs/plans/2026-08-07-command-guards-fire-under-
#: both-tool-names.md). CORRECTED 2026-08-07 -- the wave-map's original
#: reason for this hold (a fail-closed free-text tokenizer fallback that
#: misreads unparseable PowerShell input) does not apply to this file:
#: `_evaluate_legacy` does not appear here. That mechanism belongs to a
#: DIFFERENT guard family (the stash/worktree/subagent-destructive/suite-
#: invocation cohort), not this one. This guard's capability objection is
#: substantially discharged: `_dialect.py` already wires a real, lazily-
#: imported `tree-sitter-pwsh` parser, with `record_silent` on parse
#: failure or `has_error`, and `_classify_for_dialect` above already
#: dispatches on it -- this file already has a working PowerShell shape
#: match, not a guess.
#:
#: What actually holds this file: SEQUENCING, not capability. (1) A
#: concurrent workstream (DR-280, 2026-08-07) is mid-rewrite on this exact
#: file right now, retiring its Windows deny leg to advisory-only (two
#: `host_is_windows=False` call sites as of this writing) -- widening
#: `MATCHERS` while that rewrite is in flight risks landing a widening
#: onto a half-changed verdict shape. (2) Two known-red cells are open
#: against this file's own test suite under this plan's ownership
#: (`TestSubagentOutlet`, a native-vs-POSIX path-separator defect in
#: `_sandbox_script_hint`) -- widening a guard whose own tests are red is
#: a coverage claim, not a coverage gain. Next step here is re-evaluating
#: once DR-280 lands and the red cells clear, not a capability fix.
MATCHERS = ("Bash",)
PRIORITY = 41

#: Escape hatch, read inline at `check()` call time only (F2 discipline --
#: never hoisted to module scope). Distinct from the sibling rewrite
#: guard's own `COORDINATOR_ALLOW_MULTIPROBE_BANNER` -- see module
#: docstring negative-spec.
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_MULTIPROBE_BANNER"

_SHAPE_NAME = "multi-probe-banner"

#: Session-scratchpad script filename this guard recommends a SUBAGENT
#: caller write its batched probe to -- see `_sandbox_script_hint`.
_SCRATCH_SCRIPT_NAME = "multiprobe.py"


def _seam_confirmed_rewrite(result: Optional[Dict[str, Any]]) -> bool:
    """``True`` only when `check_multiprobe_banner_rewrite`'s return is a
    genuine ``_allow_rewrite`` (an ``updatedInput.command`` -- a concrete,
    executable single-process replacement for THIS exact command), never a
    bare ``_advisory``.

    Identical contract to `guard_plumbing_and_loops._seam_confirmed_
    rewrite` (see that function's docstring for the full "why" -- the
    non-``None``-but-not-a-rewrite case is common here too: ANY unrecognized
    probe segment in the chain makes `check_multiprobe_banner_rewrite`
    return a bare advisory, per its own docstring). Duplicated rather than
    imported: this is a five-line dict-shape check, not command parsing --
    importing it would couple two sibling guard modules over a "private"
    helper for no reduction in real duplication risk (the shape it checks,
    the harness hook-envelope contract, is itself the shared, stable
    surface both modules already depend on via `_hook_envelope`).
    """
    if not isinstance(result, dict):
        return False
    hso = result.get("hookSpecificOutput", {})
    if not isinstance(hso, dict):
        return False
    updated = hso.get("updatedInput")
    return isinstance(updated, dict) and bool(updated.get("command"))


def _sandbox_script_hint(git_root: Optional[str], session_id: str) -> str:
    """`state/subagent-share/<session_id>/multiprobe.py` under `git_root`
    -- the same session-scratchpad location `bump_foreign_repo_write.
    _sandbox_root_hint` / `bump_outside_repo_write._sandbox_root` already
    treat as an always-allowed subagent write target, restated here rather
    than imported (both are private to their own sibling modules -- a
    literal five-component path join carries none of the actual
    COMPUTATION "consume, don't rebuild" exists to protect against
    duplicating, unlike the rewrite seam this module calls elsewhere).
    Returns ``""`` when either input is empty -- callers degrade to a
    path-free instruction rather than fabricating one.
    """
    if not git_root or not session_id:
        return ""
    return str(Path(git_root) / "state" / "subagent-share" / session_id / _SCRATCH_SCRIPT_NAME)


def _subagent_script_outlet(
    rewrite_command: str, script_hint: str, bypass_note: str
) -> Tuple[str, str]:
    """Render the SUBAGENT-caller outlet for a seam-confirmed rewrite.

    `python3 -c` (the outlet the main-loop leg names) is a blocked
    indirection-wrapper shape for a subagent under
    `block_subagent_destructive_action` -- "there is NO subagent-honored
    override for this guard" -- so recommending it here would hand a
    subagent a command the very next guard in the chain rejects, the
    deadlock this rewrite closes (see module docstring). `python3 <path>`
    is not that shape and is not blocked for either caller class, so the
    outlet becomes: write the SAME script the seam already computed to a
    file under the session scratchpad, then invoke it by path.

    The script body is recovered from the seam's own
    ``<python3-invocation> -c <shlex.quote(script)>`` argv via
    `shlex.split` -- the exact reverse of `dispatch_checks.
    check_multiprobe_banner_rewrite`'s own `shlex.quote` at emission --
    rather than re-deriving the probe-to-script translation a second time;
    this is "consume, don't rebuild" applied to RENDERING the seam's answer,
    not to computing a new one.
    """
    script_body = ""
    argv: list = []
    # Every direct `shlex` site in this package inherits the tokenizable ceiling
    # (`test_command_tokenizer_length_ceiling.py`'s AST gate): `shlex.shlex.read_token`
    # is quadratic in the longest single token, and this input is a rewrite whose whole
    # shape is ONE very long `-c` argument. Degrading to the no-body outlet past the
    # ceiling costs a less specific message; skipping the check re-opens a multi-second
    # hang on the PreToolUse hot path.
    if not _exceeds_tokenizable_ceiling(rewrite_command):
        try:
            argv = shlex.split(rewrite_command)
        except ValueError:
            argv = []
    if len(argv) >= 3 and argv[-2] == "-c":
        script_body = argv[-1]

    path_display = script_hint or "<your session scratchpad>/%s" % _SCRATCH_SCRIPT_NAME
    # 2026-08-06 (C19a, guard-class census, byte-cap pass): trimmed from a
    # sentence that re-explained WHY `python3 -c` is blocked (the sibling
    # `block_subagent_destructive_action` guard already says that itself
    # when hit) -- the offer a subagent can act on is the path + invocation
    # below; the justification is not the actionable content.
    summary = "a script at `%s`, run as `python3 %s`. %s" % (
        path_display,
        path_display,
        bypass_note,
    )
    if script_body:
        example = (
            "write the script body below to `%s`, then run:\n\n"
            "  python3 %s\n\n"
            "Script body:\n%s"
            % (path_display, path_display, script_body)
        )
    else:
        example = "write the batched-probe script to `%s`, then run `python3 %s`" % (
            path_display,
            path_display,
        )
    return summary, example


def _outlet_from_seam_result(
    result: Dict[str, Any],
    *,
    is_subagent: bool,
    script_hint: str,
    payload: Optional[Dict[str, Any]],
    git_root: Optional[str] = None,
) -> Tuple[str, str]:
    """Render `(outlet_summary, outlet_example)` from `check_multiprobe_
    banner_rewrite`'s return, for a caller that has ALREADY confirmed (via
    `_seam_confirmed_rewrite`) that this is a genuine rewrite. Mirrors
    `guard_plumbing_and_loops._outlet_from_seam_result` for the main-loop
    leg -- see that function's docstring for why the bypass note is
    appended explicitly here rather than trusted to already be present in
    the seam result's own text (the ``updatedInput.command`` branch is a
    bare rewritten command string with no bypass mention at all).

    ``is_subagent``/``script_hint`` (subagent-aware outlet, this dispatch):
    a subagent caller gets `_subagent_script_outlet`'s scratch-script form
    instead of the main-loop's bare ``python3 -c`` rewrite -- see that
    function's docstring for why. Both callers still get the bypass note
    through this same function.
    """
    hso = result.get("hookSpecificOutput", {}) if isinstance(result, dict) else {}
    updated = hso.get("updatedInput")
    bypass_note = operator_override_note(_OVERRIDE_ENV, payload=payload, git_root=git_root)
    if isinstance(updated, dict) and updated.get("command"):
        rewrite = updated["command"]
        if is_subagent:
            return _subagent_script_outlet(rewrite, script_hint, bypass_note)
        return ("the rewrite below. %s" % bypass_note, rewrite)
    context = hso.get("additionalContext") or ""
    return ("the alternative below. %s" % bypass_note, context)


def check(
    payload: Dict[str, Any], host_is_windows: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    """Evaluate the multi-probe-banner advisory against a PreToolUse
    payload. Returns `None` (allow) or an `allow_advisory` envelope --
    never a deny (DR-280, 2026-08-07: the platform-conditioned deny branch
    was retired as structurally unreachable -- it gated on
    `_seam_confirmed_rewrite`, the same seam the earlier-registered
    `"multiprobe-banner-rewrite"` chain entry already consumes and returns
    on, so this guard's own deny leg could never be reached through the
    real dispatcher; see DR-280's Negative-spec for why the guard itself
    and its advisory leg stay). `host_is_windows` is still accepted (the
    chain-wide threading contract `dispatch.evaluate_payload_json` uses for
    every registered shape-guard) but no longer changes this guard's own
    verdict -- see the platform-verdict call at the end of this function.
    An unrecognized payload shape or unparseable command degrades to `None`
    via the explicit checks below -- this function does NOT catch-all internally (Review:
    code-reviewer -- Finding 3: this guard is registered in `dispatch.py`'s
    `guard_chain` with `fail_closed=True`, whose whole contract is that an
    internal bug propagates to `dispatch._crash_deny` rather than being
    swallowed as a silent allow; a blanket `except Exception: return None`
    here would defeat that registration exactly as it would for
    `guard_grep_via_bash`, which carries no such wrapper).

    ``host_is_windows``: accepted for the chain-wide threading contract
    every registered shape-guard honors (`_platform_verdict.py` module
    docstring; `dispatch.evaluate_payload_json` calls every entry with this
    same keyword regardless of whether the guard's own verdict still varies
    by platform) -- kept in this signature so `dispatch.py`'s registration
    lambda does not need a guard-specific special case, but this guard's
    own verdict no longer branches on it (DR-280: see this function's own
    docstring above).
    """
    if os.environ.get(_OVERRIDE_ENV, "0") == "1":
        return None

    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is None:
        return None

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    cmd = tool_input.get("command") or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    classification = _classify_for_dialect(cmd, dialect)
    if classification.tokens is None:
        return None

    primary = classification.primary
    if primary is None or primary.shape is not Shape.MULTI_PROBE_BANNER:
        return None

    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str):
        session_id = ""

    # 2026-07-29 duty-of-care promotion: consult the sibling rewrite chain
    # entry's OWN confirmation for this exact command before deciding
    # whether to point the deny/advisory at a concrete outlet. 2026-08-06
    # (B2 friction fix, see module docstring "SUBAGENT-AWARE OUTLET"): the
    # unconfirmed case used to fall back to a fixed generic advisory that
    # named a template unrelated to the segment actually present -- a nag
    # this guard cannot discharge (no outlet describes THIS command) -- so
    # it now allows silently instead of firing an advisory with no
    # actionable, command-accurate content.
    seam_result = check_multiprobe_banner_rewrite(cmd, session_id)
    if not _seam_confirmed_rewrite(seam_result):
        return None

    # No `agent_id`/`agent_type` at all -> top-level EM Bash call -> skip
    # identity resolution entirely (mirrors `block_subagent_destructive_
    # action.check`'s identical short-circuit) -- the common case pays no
    # `resolve_git_root` subprocess cost, and never resolves as a subagent
    # by construction.
    is_subagent = False
    script_hint = ""
    git_root: Optional[str] = None
    if payload.get("agent_id") or payload.get("agent_type"):
        git_root = resolve_git_root(payload.get("cwd"))
        is_subagent = resolve_agent_class(payload, git_root) == AGENT_CLASS_SUBAGENT
        if is_subagent:
            script_hint = _sandbox_script_hint(git_root, session_id)

    summary, example = _outlet_from_seam_result(
        seam_result, is_subagent=is_subagent, script_hint=script_hint, payload=payload, git_root=git_root
    )
    # 2026-08-06 (C19a, guard-class census): `primary.evidence` is only the
    # matched banner-marker SEGMENT (e.g. the bare `echo "=== facts ==="`),
    # not the caller's actual command -- rendering it in the "Command:"
    # field misdescribes what was denied whenever the banner-marker segment
    # is not the whole command (i.e. every real firing, since a lone banner
    # echo with nothing else is below `_MIN_BANNER_SEGMENTS` and never
    # reaches here). `platform_verdict_for_shape` already truncates its
    # `matched_cmd` argument to 200 chars, so passing the full `cmd` here
    # costs nothing extra for a pathological input.
    # DR-280: the deny leg is retired -- always render the advisory
    # envelope (never deny), regardless of the real or overridden host.
    # `platform_verdict_for_shape` is still the shared template so this
    # guard's message reads as one family with its still-live siblings;
    # only the platform BRANCH is forced here, not the rendering.
    return platform_verdict_for_shape(
        _SHAPE_NAME,
        cmd,
        summary,
        example,
        host_is_windows=False,
    )
