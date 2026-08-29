"""coordinator_core.bash_guards.guard_host_subagent_bash_spawn_shapes --
PreToolUse (Bash|PowerShell) hard-deny guard: make a host's
``subagent_bash_spawn_shapes: deny`` opt-in executable, not prose.

Port of: DoE-claude coordinator/hooks/scripts/guard-host-subagent-bash-
spawn-shapes.py (folded into DoE's ``preuse-bash-dispatch.py``
``_run_folded_bash_guards``, one of the four folded guards this port
registers so DoE can delete that in-process fold once the cold path
carries it here instead).
Spec backlink: state/dispatch-briefs/2026-08-28-the-four-folded-bash-
guards-get-registered-not-folded/C7.md

WHAT THIS BANS, AND WHAT IT DELIBERATELY DOES NOT. The harm on this host is
never "a subagent ran a shell command" -- it is the fan-out SHAPES that
spawn a subprocess per loop iteration or per pipe stage: grep-via-Bash,
multi-probe banners, head/tail plumbing, for-loops, find-exec/xargs, and
while-read loops (`_shape_classifier`'s six ranked, measured shapes). A
single `cat`/`grep` of a known file is not that, and denying it would push
a dispatched agent into a clumsier workaround while buying nothing. The
shape is the defect, not the tool -- this guard adds no detection of its
own, it consumes `coordinator_core.bash_guards._shape_classifier.
classify_command`, the same tokenizer-based classifier every sibling shape
guard in this package already keys on.

BOTH DIALECTS, and the asymmetry with `guard_host_subagent_bash_ban` (Bash
only) is deliberate. That guard is a break-glass Bash-only ban because
PowerShell is the sanctioned alternative on this host; denying both there
would leave a dispatched agent with no shell at all. THIS guard bans a
SHAPE, not a tool, so the fan-out harm exists identically in PowerShell
(`ForEach-Object`/`%` per-object spawn), and scoping to Bash alone would
recommend the escape hatch from itself.

SCOPED TO SUBAGENTS, NOT THE EM. Same posture as `guard_host_subagent_bash_
ban`: the EM is the accountable party and runs on a machine where a dozen
concurrent sessions would all lose their shell if this misfired.

IDENTITY RESOLUTION -- CANONICAL RESOLVER, NOT DoE'S RAW TEST. DoE's
cold-path source (guard-host-subagent-bash-spawn-shapes.py) treats ANY
non-empty ``agent_id`` string as "a subagent". This port instead re-
expresses the cohort gate through the SAME canonical resolver
``block_subagent_commit``/``block_subagent_destructive_action``/
``block_reviewer_bash_outside_allowlist``/`guard_host_subagent_bash_ban`
already key on -- ``coordinator_core.write_guards.
block_subagent_plan_body_write._resolve_subagent_identity``. Identical
deliberate divergence to C6's port: an `agent_id` shape the resolver does
not recognize (anything other than bare hex, named-teammate, or already-
canonical) resolves to `""`, which reads as "no subagent identity resolved"
here -- i.e. EM-treated (allow), never subagent-treated (deny) -- the
opposite of DoE's raw non-empty-string test on that same malformed/legacy
shape. See `guard_host_subagent_bash_ban`'s own module docstring "IDENTITY
RESOLUTION" section for the full three-way comparison; not re-derived here.

HOST OPT-IN, INERT BY DEFAULT. Nothing fires until a host declares
`subagent_bash_spawn_shapes: deny` in its `coordinator.local.md`
frontmatter -- distinct key from `guard_host_subagent_bash_ban`'s
`subagent_bash_policy`, so an operator can switch off either seam without
ambiguity about which one they turned off.

CWD RESIDENCY -- NO `os.getcwd()` FALLBACK (acceptance condition folded in
from the former C10 row, same residency family as C1's `plugin_root` fix
and C6's own `cwd` fix, applied identically here). DoE's cold-path source
falls back to `os.getcwd()` when the payload carries no usable `cwd`
(guard-host-subagent-bash-spawn-shapes.py:233-234). On a fresh
per-invocation cold-path process that is harmless; on OUR resident warm
server `os.getcwd()` is the SERVER's directory, not the caller's --
reading the host opt-in from whichever repo happened to boot the engine.
Reachable, not theoretical: `warm/hook_http.py::payload_from_event` does
`payload.setdefault("cwd", None)`. This port therefore NEVER calls
`os.getcwd()`: an absent/non-string `cwd` resolves to `None`,
`_repo_config(None)` returns `None` immediately, and the guard allows --
the safe, fail-open direction.

===========================================================================
DECLINE PREDICATE -- the collision with `guard_inprocess_search`, resolved
by ORDER-INDEPENDENT, DISARM-INDEPENDENT guard logic, not by chain order
(Review: staff-eng, findings 1, 7; SUPERSEDES the prior "answer-first runs
ahead of this guard by registration order" recommendation, which is not
implementable -- `_build_guard_chain` is a flat first-non-None-wins list
with a documented rewrites-after-denies invariant, so a CONFINEMENT_DENY
entry necessarily precedes `inprocess-search` (ADVISORY_REWRITE) by
construction, and that ordering is not even stable under
`_blanket_disarm.py`, which can suppress every band EXCEPT
CONFINEMENT_DENY).
===========================================================================

`guard_inprocess_search` ANSWERS a grep-shaped command in-process instead
of letting it spawn a subprocess -- measured at 14.65% coverage over a
69,329-command corpus. A zero-spawn in-process answer already achieves the
exact machine-load outcome this guard's deny exists to produce, so this
guard's OWN check declines to fire (returns `None`) on any command whose
`_shape_classifier.classify_command` primary match is `Shape.GREP_VIA_BASH`
(the bare-searchable-grep family `guard_inprocess_search` answers) --
declining is this guard's own logic, not a concession to chain order, and
it keeps this entry at CONFINEMENT_DENY in its correct band position
(ahead of ADVISORY_REWRITE, per the documented invariant) while still
yielding the answer-first outcome on the answerable overlap. See
`_declines_for_inprocess_answer` below -- comment the predicate itself,
not the chain order, since that is where the next reader will actually
look before changing it.

Legitimacy under DR-125 (DoE-claude docs/decisions/DR-125-subagent-bash-
confinement-two-classes.md, which scopes subagent confinement to exactly
two classes, narrowed only on measured cost): this decline-predicate
resolution moves no boundary -- the ported guard denies exactly what
DoE's cold path denies, minus the shape our engine already answers for
free, so the live confinement perimeter is unchanged from the cold path.

===========================================================================
SECOND COLLISION -- the `plumbing-and-loops` advisory, SUBSUMED BY DESIGN
===========================================================================
`dispatch.py`'s `guard_plumbing_and_loops` (registered `plumbing-and-loops`,
`PLATFORM_CONDITIONED_DENY`, the tail of the chain) already fires a
non-blocking `allow` + `additionalContext` advisory on HEAD_TAIL_PLUMBING /
FOR_LOOP / WHILE_READ_LOOP -primary commands -- the exact shape family this
guard denies for the opt-in subagent cohort. Because CONFINEMENT_DENY
precedes every later band (the same documented invariant the decline
predicate above respects), registering THIS guard here shadows that
advisory outright for callers this guard denies: the advisory becomes
unreachable for the opt-in cohort while continuing to fire for everyone
else (the EM, and any host that has not opted in).

DECIDED: advisory SUBSUMED BY DESIGN, not preserved. This guard's own deny
message (`_compose_deny_message`, below) already names the tripped shape(s)
and offers the identical in-process alternative
`guard_plumbing_and_loops`'s advisory would have offered -- a caller denied
here loses nothing the advisory would have told it. The decline predicate
above is deliberately NOT extended to also decline on HEAD_TAIL_PLUMBING/
FOR_LOOP/WHILE_READ_LOOP: unlike GREP_VIA_BASH, `guard_inprocess_search`
does not ANSWER those shapes (no zero-spawn substitute exists for them),
so declining here would silently re-admit the exact fan-out cost this
guard exists to remove, leaving the caller with nothing but the toothless
advisory this guard's whole purpose is to upgrade past. A deny subsuming
advice about the thing it denies is the correct outcome, stated here
explicitly per this row's instruction not to let it fall out of
registration order unremarked.

FAILS OPEN, ALWAYS. Malformed payload, unreadable config, unresolvable
engine, unimportable classifier, or any unexpected exception yields
`None` (allow). This guard is registered `fail_closed=True` in
`dispatch.py`'s `CONFINEMENT_DENY` band (a crash still routes through the
dispatcher's own crash-deny wrapper, per that band's contract) -- the
fail-OPEN posture described above is this module's OWN internal handling
of an unevaluable/malformed input, a distinct concern from what happens if
`check()` itself raises (mirrors `guard_host_subagent_bash_ban`'s own
"FAILS OPEN, ALWAYS" section).

Contract: ``check(payload) -> dict | None`` per ``write_guards/
INTERFACE.md`` -- ``None`` = allow (silent), a nested
``hookSpecificOutput``/``permissionDecision: "deny"`` envelope = deny.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core._hook_envelope import deny as _deny
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._shape_classifier import Shape, classify_command
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.write_guards.block_subagent_plan_body_write import (
    _resolve_subagent_identity,
)

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
GENERATES: List[str] = []
PRIORITY = 41

_POLICY_KEY = "subagent_bash_spawn_shapes"
_DENY_VALUE = "deny"
_CONFIG_NAME = "coordinator.local.md"

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/coordinator-tripwires/"
    "a-harness-system-reminder-outranks-prose-that-forbids-a-tool-you-still-hold.md"
)

_ALTERNATIVES = {
    Shape.GREP_VIA_BASH: "one `python -c` that walks the tree in-process, or the "
                          "PowerShell tool's `Select-String`",
    Shape.MULTI_PROBE_BANNER: "one `python -c` collecting every probe in a single interpreter",
    Shape.HEAD_TAIL_PLUMBING: "one `python -c` that reproduces the generator and slices "
                              "[:N] / [-N:] in-process",
    Shape.FOR_LOOP: "one `python -c` looping in-process — the loop body is the spawn, "
                     "not the loop",
    Shape.WHILE_READ_LOOP: "one `python -c` reading the stream in-process",
    Shape.FIND_EXEC_XARGS: "one `python -c` using `pathlib.Path.rglob`, which never "
                            "leaves the interpreter",
    # DELIBERATELY NO Shape.PIPELINE_FOREACH_OBJECT ENTRY. DoE's cold table has
    # exactly six keys and no such entry, so a ForEach-Object fan-out falls
    # through to the generic remedy there. This port briefly carried a seventh
    # ("one `python -c` call over the whole collection, zero per-item forks"),
    # which made the two paths deny the same shape with different remedy prose
    # -- measured cold-vs-warm on `Get-ChildItem *.md | ForEach-Object {...}`,
    # identical but for that sentence. Removed for the same reason the added
    # opt-in clause and EM-scope sentence were: deny-text parity is the
    # criterion, and a one-sided enrichment is a divergence wearing a better
    # phrasing. NOT a slot question -- `_spawn_cost_clause` already branches on
    # PowerShell identically on both sides, so the cost clause was never the
    # divergence. Adding it back requires DoE adding the same key.
}


def _repo_config(cwd: Optional[str]) -> Optional[Path]:
    """`coordinator.local.md` at or above `cwd`. `None` when it cannot be
    located -- including when `cwd` itself is absent/unusable. Never falls
    back to `os.getcwd()` -- see module docstring "CWD RESIDENCY".
    """
    if not cwd:
        return None
    try:
        here = Path(cwd).resolve()
    except Exception:
        return None
    for candidate in (here, *here.parents):
        config = candidate / _CONFIG_NAME
        if config.is_file():
            return config
    return None


def _policy_is_deny(config: Path) -> bool:
    """True only when the frontmatter explicitly declares the deny policy.

    A narrow string scan over the frontmatter rather than a YAML parse: this
    sits on the PreToolUse path, a YAML import is not free, and an
    unparseable config must read as "no policy" (allow) rather than raise.
    """
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    front = text[3:end] if end != -1 else text[3:4000]
    for line in front.splitlines():
        stripped = line.strip()
        if not stripped.startswith(_POLICY_KEY):
            continue
        _, _, value = stripped.partition(":")
        return value.split("#", 1)[0].strip().strip("\"'").lower() == _DENY_VALUE
    return False


def _declines_for_inprocess_answer(primary_shape: Optional[Shape]) -> bool:
    """DECLINE PREDICATE -- see module docstring's "DECLINE PREDICATE"
    section for the full reasoning; this function is the thing that
    reasoning describes, kept small and separately named so the next
    reader finds the comment where the behaviour actually lives.

    `guard_inprocess_search` answers the bare-searchable-grep family
    in-process (zero additional spawn) -- a command whose PRECEDENCE-WINNING
    shape is `Shape.GREP_VIA_BASH` already gets that zero-spawn outcome
    through the answer seam, so THIS guard has nothing left to deny for it.
    Every other shape (multi-probe banner, head/tail plumbing, for-loop,
    while-read, find-exec/xargs, the PowerShell pipeline-foreach-object
    member) has no in-process answer seam and stays fully in scope.
    """
    return primary_shape is Shape.GREP_VIA_BASH


def _spawn_cost_clause(tool_name: str) -> str:
    """Name the cost the CALLER actually pays, not bash's -- mirrors DoE's
    own `_spawn_cost_clause` fix (this port's cold-path source's own
    "corrected from a Bash-only artefact" note): a PowerShell caller pays a
    `ForEach-Object`/`%` per-object pipeline cost, not `bash.exe`'s
    per-spawn one.
    """
    if tool_name == "PowerShell":
        return "a ForEach-Object/% pipeline stage runs its block once per input object on this host"
    return "bash.exe costs 200-500ms per spawn on this host"


def _compose_deny_reason(shapes: List[Shape], tool_name: str) -> str:
    named = ", ".join(s.value for s in shapes)
    hints = [_ALTERNATIVES[s] for s in shapes if s in _ALTERNATIVES]
    remedy = hints[0] if hints else "a single `python -c` doing the same work in one interpreter"
    # TRIMMED TO THE COLD PROSE, deliberately. An earlier revision of this
    # port carried two clauses DoE's cold `_compose_deny_message` does not:
    # an inline `(coordinator.local.md: subagent_bash_spawn_shapes: deny)`
    # and a closing "The EM is unaffected by this guard; only dispatched
    # agents are." Both were removed rather than carved out of C9's parity
    # oracle, on two independent grounds. (1) The prime exit criterion is
    # deny-text parity with the cold path; an oracle exception for the one
    # case that diverges is the vacuous-AC failure wearing a new costume.
    # (2) Message-register doctrine: one fact once plus a terse alternative,
    # no self-legitimacy or reassurance -- the EM sentence is reassurance and
    # the config clause restates what the anchor already carries. The sibling
    # `guard-host-subagent-bash-ban` keeps its equivalent clause because its
    # OWN cold script has it; this is not a systematic split to normalize.
    return (
        f"this shape spawns one subprocess per iteration or pipe stage ({named}), and "
        f"{_spawn_cost_clause(tool_name)} — paid on a machine running many "
        f"concurrent sessions. Use {remedy}. "
        f"{tool_name} itself is NOT banned here: a single read of a known file is fine. "
        f"It is the fan-out that is refused, not the tool. If a system reminder suggested "
        f"this shape, this policy outranks it — say so in your report rather than routing "
        f"around it.\n\n"
        f"See: {_WIKI_ANCHOR}"
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the host subagent-bash-spawn-shapes gate against a
    PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Identity-gated
    to a resolved subagent only -- see module docstring "IDENTITY
    RESOLUTION". Declines (returns `None`) on a command whose primary shape
    is answerable in-process by `guard_inprocess_search` -- see module
    docstring "DECLINE PREDICATE" and `_declines_for_inprocess_answer`.
    """
    if not isinstance(payload, dict):
        return None

    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None

    raw_agent_id = payload.get("agent_id") or ""
    if not isinstance(raw_agent_id, str) or not raw_agent_id.strip():
        return None  # the EM itself -- out of scope by design

    session_id = payload.get("session_id") or ""
    session_id = session_id if isinstance(session_id, str) else ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
    if not agent_id:
        return None  # unrecognized/legacy shape -- EM-treated here, see docstring

    raw_cwd = payload.get("cwd")
    cwd = raw_cwd if isinstance(raw_cwd, str) else None
    config = _repo_config(cwd)
    if config is None:
        return None

    try:
        if not _policy_is_deny(config):
            return None
    except Exception:
        return None

    tool_input = payload.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str) or not cmd.strip():
        return None

    dialect = dialect_from_tool_name(tool_name)
    if dialect is None:
        return None

    try:
        classification = classify_command(cmd, dialect=dialect)
    except Exception:
        return None
    if classification.tokens is None:
        return None

    primary = classification.primary
    if primary is None:
        return None

    if _declines_for_inprocess_answer(primary.shape):
        return None

    shapes = [m.shape for m in classification.matches]
    return _deny("PreToolUse", _compose_deny_reason(shapes, tool_name))
