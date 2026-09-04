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
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape,
    ShapeClassification,
    classify_command,
)
from coordinator_core.bash_guards.dispatch_checks import check_multiprobe_banner_rewrite
from coordinator_core.bash_guards._helpers import operator_override_note, resolve_git_root
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._write_bump_message import (
    AGENT_CLASS_SUBAGENT,
    resolve_agent_class,
)
from coordinator_core.session import machinery_paths

_GUARD_NAME = "guard_multiprobe_banner"


def _record_powershell_non_verdict(dialect: Optional[Dialect], reason: str) -> None:
    """Record SILENT when the PowerShell leg returns without a verdict.

    `MATCHERS` declaring `PowerShell` (C6/AC14) is a claim that this guard
    reaches a verdict on a PowerShell payload. `tests/test_no_false_clean_
    on_unparsed_dialect.py` holds every PowerShell-declaring guard to that
    claim by construction: a bare `None` with no SILENT recorded is a
    false clean, indistinguishable from "this guard was never invoked" --
    the exact confusion C6 exists to end.

    Deliberately BASH-inert: the bash leg's own `primary is None` return
    stays byte-for-byte what it was (Anti-scope -- the bash leg's
    behaviour is not changed to make the port symmetrical), and
    `record_silent` is itself a no-op outside a `_verdict.collecting()`
    context, so this adds an assertion surface and no production cost.
    """
    if dialect is Dialect.POWERSHELL:
        record_silent(_GUARD_NAME, reason)


def _classify_for_dialect(cmd: str, dialect: Optional[Dialect]) -> ShapeClassification:
    """Dialect-aware analogue of `_shape_classifier.classify_command` (row 9,
    `docs/reference/guard-dialect-coverage.md`).

    RETIRED AS A CLASSIFICATION PATH (C3, pln-the-shape-classifier-reaches-
    a-e743e5 § AC11): this used to hand-walk `SHAPE_PRECEDENCE` itself
    (a second, independently-reasoned classification loop that predated
    `classify_command` having a `dialect` parameter at all -- C1/C2 of this
    same plan). Now that `classify_command(cmd, dialect=dialect)` IS the
    dialect-aware entry point, re-deriving a parallel walk here is exactly
    the duplication AC11 forbids: this function is kept ONLY as a
    name-stable shim (this guard's own test module,
    `tests/test_guard_multiprobe_banner.py::TestPowerShellDialectWiring`,
    calls it directly and is not in this chunk's write set) -- it does no
    classification of its own any more, it forwards to the canonical
    classifier unconditionally, on every dialect including BASH.

    The old hand-rolled walk deliberately SKIPPED `Shape.FOR_LOOP` on the
    POWERSHELL branch (`_detect_for_loop` keys on bash's `for ... ; do ...
    done` grammar, no PowerShell analogue) -- D2's row-14 superseding note
    OVERTURNS that: `_DETECTOR_TABLE[Dialect.POWERSHELL]` now carries
    `_detect_for_loop_pwsh`, a real predicate over measured tree-sitter-pwsh
    tokens, so FOR_LOOP is a genuine match on the PowerShell leg via
    `classify_command` itself. This guard never reads `Shape.FOR_LOOP`
    (only `Shape.MULTI_PROBE_BANNER`'s precedence position relative to
    `Shape.GREP_VIA_BASH` matters here), so the reversal cannot change this
    guard's own verdict either way -- carried forward for the record, not
    because this function still needs to special-case it.
    """
    return classify_command(cmd, dialect=dialect)

#: Review: code-reviewer -- Finding 5 (nit): vestigial in `bash_guards` --
#: see `guard_grep_via_bash.py`'s identical comment above `CLASS`/
#: `MATCHERS`/`PRIORITY` for the full explanation. `dispatch.py` hardcodes
#: ordering explicitly; this `PRIORITY` governs nothing (it is not unique
#: either -- `block_worktree_creation` reuses `41`).
CLASS = "hard-deny"
#: WIDENED (C6, pln-the-shape-classifier-reaches-a-e743e5 § D6, PM ruling
#: 2026-08-18). The prior hold here named two conditions: DR-280's rewrite
#: landing, and `state/bash-guards/known-red.json`'s two
#: `TestSubagentOutlet` `pending_fix` cells clearing. DR-280 landed
#: (`b1e2bc932` / `62f66c01a`); the red cells have NOT cleared -- the PM
#: ruled to widen ahead of that second precondition anyway, accepting the
#: five-cell debt (across this file and `guard_plumbing_and_loops.py`)
#: explicitly (AC17) rather than leave the guard unreachable on a
#: PowerShell payload indefinitely. `guard_plumbing_and_loops.py` is
#: widened in the same change for the identical reason. Reference by
#: DIRECT IDENTITY, never a copy or re-wrap -- `test_tool_name_membership.py`
#: asserts `is`.
MATCHERS = COMMAND_TOOL_NAMES
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

#: POWERSHELL-LEG GENERIC ADVISORY (C6 callee-graph audit,
#: pln-the-shape-classifier-reaches-a-e743e5 § AC16). `check_multiprobe_
#: banner_rewrite` (`dispatch_checks.py`, out of this chunk's write scope)
#: classifies its OWN input with `_shape_classifier.classify_command(cmd)`
#: -- no `dialect=` argument, so it takes the `Dialect.BASH` default (D1)
#: and tokenizes via `shlex(posix=True)` regardless of what dialect this
#: guard was actually invoked under. Before C6 widened `MATCHERS`, `check()`
#: below only ever ran on a Bash payload, so that internal bash-default was
#: always correct for the caller's own dialect. Widening `MATCHERS` to
#: `COMMAND_TOOL_NAMES` makes `check()` reachable on a PowerShell payload
#: too, and this guard's own `primary.shape is Shape.MULTI_PROBE_BANNER`
#: gate above is now dialect-aware (`_classify_for_dialect`) -- so a
#: PowerShell command CAN reach the seam call below with PowerShell syntax
#: (a here-string, a backtick escape) as `cmd`, which is exactly the
#: Anti-scope violation ("never feed PowerShell text into the posix
#: tokenizer") this plan forbids. Probed live (here-string and backtick
#: cases): `_command_tokenizer.tokenize_full_command` already catches a
#: `shlex` `ValueError` internally and degrades to `tokens=None` rather
#: than raising, and no probed case produced a false-positive BASH-shape
#: match either (`Write-Host` is not `_bt_probe_segment_kind`'s echo/printf
#: vocabulary) -- so today's blast radius is narrow. It is not zero by
#: construction, though, and the seam has no PowerShell leg to consult in
#: the first place (unlike `check_head_tail_plumbing_rewrite`, which DOES
#: take a `dialect=` parameter): there is nothing this call could confirm
#: for a PowerShell command even if the tokenizer behaved. `check()` below
#: therefore gates this call to `Dialect.BASH` explicitly and renders this
#: fixed, every-platform advisory for the `Dialect.POWERSHELL` leg instead
#: -- the same "no seam to consult, generic advisory" shape
#: `guard_plumbing_and_loops.py` already uses for `PIPELINE_FOREACH_OBJECT`
#: and the bare-glob `FOR_LOOP` fallback, applied here for the identical
#: reason (AC9: the alternative must be PowerShell-valid; a `python3 -c`
#: invocation is a subprocess call, not shell syntax, so it runs the same
#: from a PowerShell prompt).
_POWERSHELL_BANNER_GENERIC_SUMMARY = (
    "a single in-process python3 call batching every probe, zero per-probe forks"
)
_POWERSHELL_BANNER_GENERIC_EXAMPLE = (
    "python3 -c 'import subprocess\\n"
    "print(subprocess.run([\"git\", \"status\", \"--porcelain=v2\", \"--branch\"], "
    "capture_output=True, text=True).stdout)'  "
    "# batch every probe into one process instead of one call per probe"
)


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
    """`<machinery_root>/subagent-share/<session_id>/multiprobe.py` under
    `git_root` -- the same session-scratchpad location `bump_foreign_repo_
    write._sandbox_root_hint` / `bump_outside_repo_write._sandbox_root`
    already treat as an always-allowed subagent write target, resolved
    through `machinery_paths.share_dir` (the sole owner of this path shape,
    per `docs/plans/2026-09-02-state-keeps-the-work-not-the-machinery.md`
    chunk C3) rather than composed here. Returns ``""`` when either input
    is empty -- callers degrade to a path-free instruction rather than
    fabricating one.
    """
    if not git_root or not session_id:
        return ""
    return str(Path(machinery_paths.share_dir(git_root, session_id)) / _SCRATCH_SCRIPT_NAME)


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
        _record_powershell_non_verdict(dialect, "unparseable command text")
        return None

    primary = classification.primary
    if primary is None or primary.shape is not Shape.MULTI_PROBE_BANNER:
        _record_powershell_non_verdict(
            dialect, "no MULTI_PROBE_BANNER match on the PowerShell leg"
        )
        return None

    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str):
        session_id = ""

    # AC16 callee-graph audit (see `_POWERSHELL_BANNER_GENERIC_SUMMARY`'s own
    # comment above): `check_multiprobe_banner_rewrite` classifies internally
    # with a BASH default and has no PowerShell leg to consult -- gate the
    # seam call to Bash explicitly rather than let a PowerShell `cmd` reach
    # its posix tokenizer. This branch is reachable ONLY after C6 widened
    # `MATCHERS`; before that, `check()` never ran on a PowerShell payload.
    if dialect is Dialect.POWERSHELL:
        bypass_note = operator_override_note(_OVERRIDE_ENV, payload=payload)
        return platform_verdict_for_shape(
            _SHAPE_NAME,
            cmd,
            _POWERSHELL_BANNER_GENERIC_SUMMARY,
            "%s\n  %s" % (_POWERSHELL_BANNER_GENERIC_EXAMPLE, bypass_note),
            host_is_windows=False,
        )

    # 2026-07-29 duty-of-care promotion: consult the sibling rewrite chain
    # entry's OWN confirmation for this exact command before deciding
    # whether to point the deny/advisory at a concrete outlet. 2026-08-06
    # (B2 friction fix, see module docstring "SUBAGENT-AWARE OUTLET"): the
    # unconfirmed case used to fall back to a fixed generic advisory that
    # named a template unrelated to the segment actually present -- a nag
    # this guard cannot discharge (no outlet describes THIS command) -- so
    # it now allows silently instead of firing an advisory with no
    # actionable, command-accurate content. BASH-only from this point on
    # (see the `dialect is Dialect.POWERSHELL` gate immediately above).
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
