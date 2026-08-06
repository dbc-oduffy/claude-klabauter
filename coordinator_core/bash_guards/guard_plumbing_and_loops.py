"""coordinator_core.bash_guards.guard_plumbing_and_loops -- BX-8, the
head/tail-plumbing and for-loop guard (`docs/plans/2026-07-29-windows-
viability-stop-the-spawn-storms.md` § BX-8, upstream
`docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md` § C5).

Purpose: two of the five ranked fork-tax shapes, folded into one guard
because BX-8 covers both -- head/tail plumbing (`... | head` / `... | tail`,
25% of measured forks: truncating a subprocess's output via ANOTHER
subprocess) and for-loops (9.0%: forking one child process per loop
iteration). The founding incident's own flagship example
(`find ... -exec sh -c ...` piped through `head`, 293 `sh` + 293 `head` +
293 `grep`) is a for-loop-shaped `find -exec` wrapper, not this module's
head/tail half -- see `check_find_exec_rewrite`'s own docstring, which
already owns that shape.

Detection is entirely delegated to `_shape_classifier.classify_command`
(tokenizer-based, never regex-over-raw-command-text -- this module contains
no command-shape regex of its own). This module fires ONLY when a shape is
the PRECEDENCE WINNER (`classification.primary`), never on a residue match
-- a command that is simultaneously grep-via-Bash and head/tail-plumbing (or
a multi-probe banner and a for-loop) is that OTHER guard's business first
(AC-7: a deny/advisory message must never misdescribe what tripped it).

GATED INDEPENDENTLY, PER SHAPE, AGAINST WHAT BX-16'S SEAM CAN ACTUALLY EMIT
----------------------------------------------------------------------------
The plan's sequencing rule: a guard may deny only toward a rewrite target
the seam can ACTUALLY EMIT for the SPECIFIC command in front of it, never
toward a shape in the abstract. This module honors that by literally
CALLING the seam check for each shape and inspecting what it returns, rather
than re-deriving a parallel judgment of "is this translatable":

  - HEAD_TAIL_PLUMBING -> `dispatch_checks.check_head_tail_plumbing_rewrite`.
    As of this module's authoring that check IS landed (2026-07-29,
    concurrent BX-16 work). Critically, that check returns non-``None`` for
    almost EVERY `upstream | head-or-tail` two-segment pipeline the shape
    classifier recognizes -- but only a MINORITY of those non-``None``
    returns are an actual `_allow_rewrite` (`updatedInput.command`, a
    concrete single-process replacement); the rest are a bare `_advisory`
    (`additionalContext` only) whose own text says the rewrite is "not
    offered automatically" -- an unrecognized upstream generator
    (`docker ps | head`, `git log --oneline | head`), a pipeline longer than
    two segments, or an unrecognized head/tail count form. This module's
    `_seam_confirmed_rewrite` helper is the gate: only the genuine-rewrite
    outcome licenses `platform_verdict_for_shape` (deny on Windows / advise
    on macOS); a bare seam advisory is treated identically to a ``None``
    return -- `_generic_advisory` on EVERY platform. Collapsing that
    distinction (denying on any non-``None`` return) was tried and reverted
    during this row's adversarial review: it denied common benign commands
    on Windows toward an "Example" that was just the seam's own disclaimer
    text, the exact deny-toward-a-target-that-does-not-exist hazard the
    sequencing rule exists to prevent.

  - FOR_LOOP -> `dispatch_checks.check_find_exec_rewrite`. That check's own
    docstring is explicit that its FOR_LOOP handling is NARROW: it
    recognizes only a for-loop that wraps a literal `find ... -exec ... ;`
    (translating the exec'd verb when it is rm/cat/wc -l, else advising with
    a generic os.walk skeleton) -- a bare glob for-loop
    (`for f in *.txt; do rm "$f"; done`) is deliberately left untranslated
    and that check returns ``None`` for it. The same `_seam_confirmed_
    rewrite` gate applies here too: a for-loop wrapping a literal
    `find -exec <verb>` where `<verb>` is NOT rm/cat/wc -l gets a bare seam
    advisory (no `updatedInput`), which this module treats exactly like the
    bare-glob ``None`` case -- a GENERIC advisory on BOTH platforms. Only a
    for-loop wrapping a translatable `find -exec` verb gets the
    platform-conditioned deny/advise verdict.

    FOR_LOOP's leg of AC-5 is, ARCHITECTURALLY, advisory-only on every
    platform, full stop -- not merely "usually" (Review: code-reviewer --
    Finding 4, spec completion, traced across this module + `_shape_
    classifier._detect_for_loop` + `dispatch_checks.check_find_exec_
    rewrite`). `_detect_for_loop` only classifies FOR_LOOP when the
    command's OWN first token is the literal `for`; `check_find_exec_
    rewrite`'s only path that counts as "redirected" per `_seam_confirmed_
    rewrite` (`_allow_rewrite`) requires `len(segments) == 1` AND that lone
    segment's first token is `find`. A segment cannot start with both `for`
    and `find` at once, and a genuine `for ... do ... done` loop is
    inherently multi-segment from its own internal `;`s in every
    constructible case (including newline-delimited loops, whose own
    segment still starts with `for`). So no FOR_LOOP-classified command --
    translatable exec verb or not -- can ever reach `_allow_rewrite`; this
    leg cannot satisfy AC-5's "observed being redirected" requirement as
    this architecture is built. This is a currently-accepted, currently-
    honestly-tracked gap (AC-5's plan row is `pending`, not `closed`), not a
    defect in this diff -- stated explicitly here so a future reader does
    not have to re-derive it by re-tracing the same three files.

    The bare-glob FOR_LOOP fallback's OWN rung is likewise a deliberate,
    recorded stop, not an unnoticed gap -- see `_FOR_LOOP_GENERIC_SUMMARY`'s
    own comment block (worklist Row P4) for the full "why this stays at
    rung C" reasoning: it needs a `_shape_classifier` capability this
    package does not have (structural extraction of a for-loop's
    variable/items/body, not merely detecting one is present), and even
    with that capability a fully generic per-item body has no safe
    translation without re-implementing a shell interpreter.

Consume, don't rebuild: this module does not re-parse `find -exec` or
`head`/`tail` argument grammar itself. It classifies the shape, calls the
one seam check that shape's rewrite lives in, and only shapes the platform
verdict around whatever that check already decided.

Negative-spec
-------------
  - Does NOT reimplement `_shape_classifier`'s shape detection or
    `_platform_verdict`'s envelope shaping -- both are imported, never
    duplicated.
  - Does NOT fire when HEAD_TAIL_PLUMBING or FOR_LOOP is present but NOT the
    precedence winner -- see the module comment above (AC-7).
  - Does NOT deny toward a bare glob for-loop, toward any command for which
    the relevant BX-16 seam check returns ``None``, NOR toward one for which
    the seam returns a bare `_advisory` with no `updatedInput` (no confirmed
    rewrite either way -- see `_seam_confirmed_rewrite`) -- all three stay
    advisory-only on every platform, per the plan's sequencing rule.
  - Does NOT read any environment variable for its own shape detection --
    the only environment reads are this guard's own escape hatch
    (`COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS`, read inline at `check()`
    call time, F2 discipline) and the two underlying seam checks' OWN
    override reads (`COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING`,
    `COORDINATOR_ALLOW_FIND_EXEC`), which this module observes only as a
    side effect of calling those checks (a seam check returning ``None``
    because its own override fired is treated identically to "no confirmed
    outlet" -- this guard degrades to a generic advisory rather than
    denying toward nothing).

Spec backlink: docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md § BX-8
Spec backlink (shape/verdict primitives): docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md § C2, C5, C6
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any, Dict, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _pl_segments_from_tokens_with_pipe_flag,
    token_matches_binary as _pl_token_matches_binary,
)
from coordinator_core.bash_guards._platform_verdict import platform_verdict_for_shape
from coordinator_core.bash_guards._shape_classifier import Shape, classify_command
from coordinator_core.bash_guards.dispatch_checks import (
    _bt_python3_invocation as _pl_python3_invocation,
    check_find_exec_rewrite,
)
from coordinator_core.bash_guards.guard_head_tail_rewrite import (
    _bt_head_tail_count as _pl_head_tail_count,
    check_head_tail_plumbing_rewrite,
)
from coordinator_core._hook_envelope import allow_advisory
from coordinator_core.bash_guards._helpers import operator_override_note

#: Review: code-reviewer -- Finding 5 (nit): vestigial in `bash_guards` --
#: see `guard_grep_via_bash.py`'s identical comment above `CLASS`/
#: `MATCHERS`/`PRIORITY` for the full explanation. `dispatch.py` hardcodes
#: ordering explicitly; this `PRIORITY` governs nothing here.
CLASS = "hard-deny"
MATCHERS = ["Bash"]
PRIORITY = 100

#: This guard's OWN escape hatch -- suppresses BOTH shapes' policy outright,
#: read inline at `check()` call time only (F2 discipline -- never hoisted
#: to module scope). Distinct from the underlying seam checks' own
#: `COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING` / `COORDINATOR_ALLOW_FIND_EXEC`
#: (which this module also implicitly honors -- see module docstring).
_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS"

_EVENT_NAME = "PreToolUse"

#: FOR-LOOP GENERIC FALLBACK -- DECIDED EXPLICITLY, NOT AN OMISSION
#: (worklist Row P4, `state/audits/2026-07-29-guard-module-ladder-
#: worklist.md`, example-doctrine-repo repo; `docs/plans/2026-07-29-bash-guard-
#: consolidated-execution.md` row M10 item 2). This bare-glob for-loop
#: branch is ARCHITECTURALLY CAPPED at an every-platform advisory carrying
#: a generic (non-command-specific) skeleton -- a recorded stop, not a
#: promotable rung-C-to-B gap left unaddressed by accident:
#:
#:   1. `check_find_exec_rewrite`'s only confirmed-outlet path
#:      (`_seam_confirmed_rewrite`) requires the WHOLE command to be exactly
#:      one segment whose first token is literally `find` (see this
#:      module's own docstring, "FOR_LOOP's leg of AC-5 is, ARCHITECTURALLY,
#:      advisory-only on every platform"). A for-loop's own first token is
#:      always `for`, never `find`, and a genuine `for ... do ... done` body
#:      is inherently multi-segment from its own internal `;`s/newlines --
#:      so NO for-loop, translatable exec verb or not, can ever reach that
#:      seam's confirmed-rewrite branch. This is a structural fact about the
#:      seam this guard consumes, not something this guard's own message
#:      wording could fix.
#:   2. Promoting this branch to a genuine per-command rewrite (rather than
#:      a fixed skeleton) needs a NEW `_shape_classifier` capability this
#:      package does not have today: structurally extracting the loop
#:      variable, the item list, and the body from a `for ... do ... done`
#:      construct -- `_detect_for_loop` only detects that one is PRESENT
#:      (`do`/`done` tokens exist), it does not parse its parts. Building
#:      that classifier is real, separate work, not a message-text fix, and
#:      is out of this row's scope.
#:   3. Even with that classifier in hand, a GENERIC body's per-item work is
#:      arbitrary shell (unknown variable expansions, quoting, exit-status
#:      handling) -- unlike `check_find_exec_rewrite`'s narrow, ENUMERATED
#:      verb translation (rm/cat/wc -l only), there is no safe, general
#:      translation of "whatever the body does" into Python without
#:      re-implementing a shell interpreter. A "run the body verbatim per
#:      item via a subprocess" alternative (the same verbatim-reuse pattern
#:      `_verbatim_head_tail_alternative` uses for the head/tail branch)
#:      would still fork once per iteration -- exactly the cost this guard
#:      exists to remove -- so it would not be a real improvement over the
#:      status quo, only a different-looking non-improvement; it is
#:      deliberately NOT implemented here for that reason.
#:
#: DECISION: stays at rung C -- a generic, every-platform-advisory
#: skeleton -- until a `_shape_classifier` for-loop-structure capability
#: exists AND a translator for at least a small enumerated set of common
#: per-item verbs is built on top of it (the same shape
#: `check_find_exec_rewrite`'s own enumerated rm/cat/wc -l translation
#: already takes for its narrower shape). No such capability exists in this
#: package as of this decision, so no attempt is made to fake one here.
_FOR_LOOP_GENERIC_SUMMARY = "a single in-process python3 loop, zero per-item forks"
_FOR_LOOP_GENERIC_EXAMPLE = (
    "python3 -c 'import glob\\nfor f in glob.glob(\"*.txt\"):\\n    ...'  "
    "# do the per-item work in-process, zero per-iteration forks"
)


def _seam_confirmed_rewrite(result: Optional[Dict[str, Any]]) -> bool:
    """``True`` only when a BX-16 seam check's return is an ``_allow_rewrite``
    (a genuine ``updatedInput.command`` -- a concrete, executable single-
    process replacement for this exact command), never merely an
    ``_advisory``.

    This is the actual sequencing gate: both `check_head_tail_plumbing_
    rewrite` and `check_find_exec_rewrite` return NON-``None`` far more
    often than they return an actual rewrite -- an upstream stage the seam
    doesn't recognize (``docker ps | head``, ``git log --oneline | head``),
    a pipeline longer than two segments, an unrecognized head/tail count
    form, or a `find -exec` verb outside rm/cat/wc -l all come back as a
    bare seam ``_advisory`` (``additionalContext``, no ``updatedInput``)
    whose own text says the rewrite is "not offered automatically" / "has
    no known translation on file". Treating THAT as a confirmed outlet --
    as an earlier revision of this module did, before this check existed --
    denies common, benign commands on Windows (``docker ps | head -n 20``)
    toward an "Example" that is just the seam's own disclaimer prose, which
    is exactly the deny-toward-a-target-that-does-not-exist hazard the
    plan's sequencing rule exists to prevent. Only a real ``updatedInput``
    licenses a deny; everything else must fall back to this guard's own
    `_generic_advisory` (advisory-only on every platform), identically to
    a seam result of ``None``.
    """
    if not isinstance(result, dict):
        return False
    hso = result.get("hookSpecificOutput", {})
    if not isinstance(hso, dict):
        return False
    updated = hso.get("updatedInput")
    return isinstance(updated, dict) and bool(updated.get("command"))


def _outlet_from_seam_result(result: Dict[str, Any]) -> Tuple[str, str]:
    """Render `(outlet_summary, outlet_example)` from a BX-16 seam check's
    return, for a caller that has ALREADY confirmed (via
    `_seam_confirmed_rewrite`) that this is a genuine `_allow_rewrite`
    (allow + `updatedInput.command`) -- the only outcome this module treats
    as "the seam confirmed a concrete outlet for this exact command", per
    the module docstring's sequencing discipline. A bare seam `_advisory`
    (allow + `additionalContext`, no `updatedInput`) is NOT a confirmed
    outlet -- see `_seam_confirmed_rewrite`'s docstring -- and callers must
    route that case to `_generic_advisory` instead of calling this
    function. The ``additionalContext``-only fallback below is defensive
    only (unreachable in the normal `check()` flow, kept in case a future
    seam-result shape gap slips past `_seam_confirmed_rewrite`).

    `outlet_summary` is a self-contained phrase, never a placeholder like
    the prior "below." -- `_platform_verdict.platform_verdict_for_shape`
    splices this summary into TWO different sentence shapes (the deny
    path's "Use instead: %s", but also the advisory path's "consider %s
    here too so behavior stays consistent across the fleet"), and only the
    deny phrasing reads sensibly with a bare "below." pointing at the
    Example line beneath it -- the advisory phrasing turned that into
    "consider below. here too", which both misdescribes the outlet and
    (Review: guard-class census) drags the override note (below) out of
    the deny template's "Use instead:" cue window and into counted prose,
    since the advisory template's own cue window starts at "Example:", not
    at the sentence carrying `%s`.
    """
    hso = result.get("hookSpecificOutput", {}) if isinstance(result, dict) else {}
    updated = hso.get("updatedInput")
    # This guard's OWN escape hatch is appended to the EXAMPLE, not the
    # summary -- both the deny and advisory templates in
    # `_platform_verdict.platform_verdict_for_shape` open an "Example:"
    # cue window for `outlet_example` with no blank line before it, so an
    # indented line placed immediately after the example text lands inside
    # that window in EITHER verdict shape (mirrors `_generic_advisory`'s
    # own "  Example:  %s\n  %s\n" convention below in this same module) --
    # an indented line inside a cue window is a counted offer, not counted
    # prose.
    bypass_note = operator_override_note(_OVERRIDE_ENV)
    summary = "the seam-confirmed single-process rewrite"
    if isinstance(updated, dict) and updated.get("command"):
        return (summary, "%s\n  %s" % (updated["command"], bypass_note))
    context = hso.get("additionalContext") or ""
    return (summary, "%s\n  %s" % (context, bypass_note))


def _generic_advisory(shape_label: str, cmd: str, summary: str, example: str) -> Dict[str, Any]:
    """Advisory-only-on-every-platform envelope for a command whose shape
    matched but for which the relevant BX-16 seam check returned ``None``
    (no confirmed outlet -- a bare glob for-loop, or an underlying seam
    override suppressing that check). Never a deny -- see module docstring.
    """
    cmd_safe = cmd if len(cmd) <= 200 else cmd[:200] + "..."
    # `%s`'s own indented, no-blank-line-before-it placement mirrors
    # `_outlet_from_seam_result`'s cue-window trick -- see that function's
    # comment.
    #
    # Review: coordinator:code-reviewer (Finding 6, guard-message-size-
    # discipline) -- the override note now trails the Example line (the
    # concrete command a reader would copy) rather than sitting between the
    # alternative summary and the Example, matching every other guard's
    # message shape in this diff.
    context = (
        "BASH-SPAWN ADVISORY (non-blocking): `%s`-shaped command spawns a "
        "subprocess per iteration/pipe stage.\n\n"
        "  Command:  %s\n\n"
        "Use instead: %s\n"
        "  Example:  %s\n"
        "  %s\n"
        % (shape_label, cmd_safe, summary, example, operator_override_note(_OVERRIDE_ENV))
    )
    return allow_advisory(_EVENT_NAME, context)


def _verbatim_head_tail_alternative(cmd: str) -> Optional[str]:
    """Build a single-`python3 -c` alternative for a HEAD_TAIL_PLUMBING
    command by piping the upstream stage's command VERBATIM into an
    in-process head/tail slicer, instead of `check_head_tail_plumbing_
    rewrite`'s approach of RECOGNIZING the upstream generator (`find`/`ls`/
    a grep-family binary) and reproducing its output from scratch.

    This is strictly a fallback quality upgrade for the class of commands
    that check already leaves at a bare `_generic_advisory` -- an upstream
    stage it does not recognize (`docker ps | head`, `git log --oneline |
    head`, or any other binary). Whatever produced the stream, running that
    EXACT command as a subprocess and slicing its stdout in-process gives the
    caller a runnable alternative with zero need to understand what the
    upstream command means.

    WHAT THIS BUYS, STATED HONESTLY -- it is capability and portability, NOT
    a spawn reduction, and the caller-facing message says so rather than
    overclaiming. Accounting for `git log --oneline | head -n 3`: the original
    spawns 2 processes (`git log`, `head`); this alternative spawns 3 (python3,
    then `/bin/sh` via `shell=True`, then `git log`), one of which is an
    interpreter startup this package's own benchmark audit measured as ~43%
    site-module loading. So on a host that HAS head/tail this is strictly more
    forks, and offering it as a fork reduction would be a false capability
    claim of exactly the shape `6f3ae246` was landed to remove from this
    package. The real win is on the platform this guard is banded for: a
    `PLATFORM_CONDITIONED_DENY` guard's primary audience is the Windows caller
    with no `head`/`tail` on PATH, for whom this is not a slower route to the
    job but the only route -- which is the duty-of-care thesis exactly
    ("capable but slower" is fine, "incapable" is the expensive failure).

    Note the alternative-liveness gate cannot police this distinction: it
    proves a named alternative RUNS, never that it is cheaper than what it
    replaces. That is why the honesty has to live in the message text.

    Returns `None` (no alternative offered) whenever the shape isn't the
    plain two-segment `generator | head-or-tail` this module already
    requires for HEAD_TAIL_PLUMBING to fire at all, or the head/tail
    invocation's own arguments aren't one of `_bt_head_tail_count`'s
    recognized line-count forms -- this function never widens what the
    guard fires on, it only improves the offer for shapes already matched.
    """
    classification = classify_command(cmd)
    if classification.tokens is None:
        return None
    segments = _pl_segments_from_tokens_with_pipe_flag(classification.tokens)
    if len(segments) != 2:
        return None
    (up_tokens, up_pipe_before), (ht_tokens, ht_pipe_before) = segments
    if up_pipe_before or not ht_pipe_before or not up_tokens or not ht_tokens:
        return None
    is_head = _pl_token_matches_binary(ht_tokens[0], "head")
    is_tail = _pl_token_matches_binary(ht_tokens[0], "tail")
    if not (is_head or is_tail):
        return None
    n = _pl_head_tail_count(ht_tokens[1:])
    if n is None:
        return None

    # Re-quote the upstream segment's OWN tokens into a fresh, valid shell
    # command line -- this is what makes the replacement "verbatim": the
    # upstream runs exactly as it would have, as the ONE subprocess this
    # rewrite still needs (the thing being asked, not the fork being taxed),
    # with no re-derivation of what it produces.
    upstream_cmd = " ".join(shlex.quote(tok) for tok in up_tokens)

    if is_head:
        # `itertools.islice(p.stdout, n)` reads (and the walk deque below
        # buffers) at most `n` lines regardless of `n <= 0` -- no separate
        # zero-case branch needed, unlike the seam's own generator-specific
        # rewrite, because there is no generator body here to skip running.
        slicer_lines = [
            "import itertools",
            "for _l in itertools.islice(p.stdout, %d):" % n,
            "    print(_l, end='')",
        ]
    else:
        slicer_lines = [
            "import collections",
            "_buf = collections.deque(p.stdout, maxlen=%d)" % n,
            "for _l in _buf:",
            "    print(_l, end='')",
        ]

    script_lines = (
        ["import subprocess", "p = subprocess.Popen(%s, shell=True, stdout=subprocess.PIPE, text=True)"
         % json.dumps(upstream_cmd), "try:"]
        + ["    " + line for line in slicer_lines]
        + ["finally:", "    p.stdout.close()", "    p.terminate()", "    p.wait()"]
    )
    script = "\n".join(script_lines)
    return "%s -c %s" % (_pl_python3_invocation(), shlex.quote(script))


def _verdict_head_tail(
    cmd: str, session_id: str, host_is_windows: Optional[bool]
) -> Dict[str, Any]:
    """Platform-conditioned verdict for a HEAD_TAIL_PLUMBING-primary command,
    consuming `check_head_tail_plumbing_rewrite`'s own confirmation of an
    outlet for this exact command (rewrite or advisory-with-skeleton both
    count; ``None`` -- either a genuine shape-classifier/seam disagreement
    or the seam's own override having fired -- degrades to a generic
    advisory rather than a deny toward nothing).
    """
    seam_result = check_head_tail_plumbing_rewrite(cmd, session_id)
    if not _seam_confirmed_rewrite(seam_result):
        verbatim_alt = _verbatim_head_tail_alternative(cmd)
        if verbatim_alt is not None:
            return _generic_advisory(
                "head-tail-plumbing",
                cmd,
                "a single python3 process running the upstream command "
                "verbatim and slicing head/tail on its stdout in-process",
                verbatim_alt,
            )
        return _generic_advisory(
            "head-tail-plumbing",
            cmd,
            "a single python3 process collecting the same lines and "
            "slicing head/tail in-process",
            "python3 -c '...'  # reproduce the generator output and slice "
            "[:N] / [-N:] in-process",
        )
    summary, example = _outlet_from_seam_result(seam_result)
    return platform_verdict_for_shape(
        "head-tail-plumbing", cmd, summary, example, host_is_windows=host_is_windows
    )


def _verdict_for_loop(
    cmd: str, session_id: str, host_is_windows: Optional[bool]
) -> Dict[str, Any]:
    """Platform-conditioned verdict for a FOR_LOOP-primary command,
    consuming `check_find_exec_rewrite`'s own confirmation of an outlet.
    That check's FOR_LOOP handling is narrow (a loop wrapping a literal
    `find ... -exec ...`, translated or generically advised) -- a bare glob
    for-loop makes it return ``None``, and this function then falls back to
    `_generic_advisory` (advisory-only on EVERY platform) rather than
    denying toward a for-loop shape the seam cannot serve.
    """
    seam_result = check_find_exec_rewrite(cmd, session_id)
    if not _seam_confirmed_rewrite(seam_result):
        return _generic_advisory(
            "for-loop", cmd, _FOR_LOOP_GENERIC_SUMMARY, _FOR_LOOP_GENERIC_EXAMPLE
        )
    summary, example = _outlet_from_seam_result(seam_result)
    return platform_verdict_for_shape(
        "for-loop-wrapping-find-exec",
        cmd,
        summary,
        example,
        host_is_windows=host_is_windows,
    )


def check(
    payload: Dict[str, Any], host_is_windows: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    """Evaluate the head/tail-plumbing-and-for-loop guard against a
    PreToolUse payload. Returns ``None`` (allow, no advisory), an
    `allow_advisory` envelope, or a `deny` envelope (Windows only, and only
    when the relevant BX-16 seam confirms a concrete outlet for this exact
    command -- see module docstring). An unrecognized payload shape or
    unparseable command degrades to ``None`` via the explicit checks below
    -- this function does NOT catch-all internally (Review: code-reviewer
    -- Finding 3: this guard is registered in `dispatch.py`'s `guard_chain`
    with `fail_closed=True`, whose whole contract is that an internal bug
    propagates to `dispatch._crash_deny` rather than being swallowed as a
    silent allow; a blanket `except Exception: return None` here would
    defeat that registration exactly as it would for
    `guard_grep_via_bash`, which carries no such wrapper).

    ``host_is_windows``: platform override, threaded straight through to
    `_platform_verdict.platform_verdict_for_shape` -- see that module's
    docstring "Platform-override contract" and `dispatch.py`'s own
    docstring for the threading contract this signature satisfies.
    """
    if os.environ.get(_OVERRIDE_ENV, "0") == "1":
        return None

    tool_name = payload.get("tool_name") or ""
    if tool_name != "Bash":
        return None

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    cmd = tool_input.get("command") or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str):
        session_id = ""

    classification = classify_command(cmd)
    if classification.tokens is None:
        return None

    primary = classification.primary
    if primary is None:
        return None

    if primary.shape is Shape.HEAD_TAIL_PLUMBING:
        return _verdict_head_tail(cmd, session_id, host_is_windows)
    if primary.shape is Shape.FOR_LOOP:
        return _verdict_for_loop(cmd, session_id, host_is_windows)
    return None
