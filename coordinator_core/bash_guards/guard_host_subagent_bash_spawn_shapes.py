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

IDENTITY RESOLUTION -- COHORT MEMBERSHIP, NOT CANONICAL IDENTITY (REVERSED,
2026-08-29, state/audits/2026-08-29-unverified-parity-findings-measured.md
FINDING A). This guard previously re-expressed the cohort gate through
``_resolve_subagent_identity``, the same canonical resolver
`guard_host_subagent_bash_ban` used before its own reversal. Measured wrong
for the same reason: the resolver's fail-closed branch for a named
teammate with a short `session_id` EM-treated a genuinely dispatched
caller cold denies. The resolved id here was never used for anything but a
truthiness test -- no message text, no comparison -- so this guard now
asks cold's question directly: ANY non-empty, non-whitespace `agent_id`
string is in-cohort, matching DoE's raw
``isinstance(agent_id, str) and agent_id.strip()`` test. See
`guard_host_subagent_bash_ban`'s own module docstring "IDENTITY
RESOLUTION" section for the full account; not re-derived here. Do not
reintroduce a canonical-id resolver at this gate -- membership is a
different question from canonical identity, and nothing here needs the
latter.

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
guard's OWN check declines to fire (returns `None`) when the answer seam
would actually answer the WHOLE command -- checked with
`coordinator_core.search.answer.plan_for(cmd, tool_name)`, the same
cheap, non-executing (parse/plan only, no filesystem I/O, no subprocess)
predicate `guard_inprocess_search` itself plans against before it ever
executes anything. `Shape.GREP_VIA_BASH` as the primary match is
NECESSARY but not SUFFICIENT for this: a `curl ... | grep foo` or a
`grep foo .; rm -rf x` also classifies primary GREP_VIA_BASH but
`plan_for` correctly returns `None` for both (piped-into first segment;
non-pipe-joined trailing segment) -- declining on shape alone would lose
the deny on exactly the cases the answer seam cannot cover. Measured
(state/bug-backlog/2026-08-29-the-guard-rehome-is-not-yet-safe-to-dele-
9f7396118b81.yaml, finding 1): `grep -rn "foo" .`, `grep -rn "foo" . |
sort | uniq -c` (the `_stage_sort`/`_stage_uniq` pipeline stages are
absorbed, not dropped), and `grep -rln TODO src tests docs` are all
fully `plan_for`-answerable, so declining on them is correct; the
predicate's role is to keep denying the unanswerable overlap
(`curl foo | grep bar`, `grep foo .; echo done`) that shape-only
declining used to lose. Declining is this guard's own logic, not a
concession to chain order, and it keeps this entry at CONFINEMENT_DENY
in its correct band position (ahead of ADVISORY_REWRITE, per the
documented invariant) while still yielding the answer-first outcome on
the answerable overlap. See `_declines_for_inprocess_answer` below --
comment the predicate itself, not the chain order, since that is where
the next reader will actually look before changing it.

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
from typing import Any, Callable, Dict, List, Optional

from coordinator_core._hook_envelope import deny as _deny
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._shape_classifier import Shape, classify_command
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

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


def _declines_for_inprocess_answer(
    primary_shape: Optional[Shape],
    cmd: str,
    tool_name: str,
) -> bool:
    """DECLINE PREDICATE -- see module docstring's "DECLINE PREDICATE"
    section for the full reasoning; this function is the thing that
    reasoning describes, kept small and separately named so the next
    reader finds the comment where the behaviour actually lives.

    `guard_inprocess_search` answers the bare-searchable-grep family
    in-process (zero additional spawn), so a command it fully answers costs
    the caller nothing and there is nothing left for this guard to deny.

    THE PRECEDENCE-WINNING SHAPE IS NOT ENOUGH TO ESTABLISH THAT, and keying
    on it alone was a real defect (state/bug-backlog/2026-08-29-the-guard-
    rehome-is-not-yet-safe-to-dele-9f7396118b81.yaml, gap 1). `curl foo |
    grep bar` (piped INTO the grep -- the seam's input does not exist until
    the upstream command runs) and `grep foo . ; echo done` (a `;`-joined
    second segment the seam does not execute) both classify with
    `GREP_VIA_BASH` as the precedence-winning shape, exactly like a bare
    grep -- a shape test alone declines both, and the seam then answers only
    part of the command while the rest silently never runs, which is worse
    than the spawn this guard exists to refuse. `search.answer.plan_for`
    ALREADY REFUSES BOTH on that same structural ground (piped-into first
    segment; non-pipe-joined trailing segment) -- see `answer.py::
    _plan_for_grep`, which this predicate consults rather than re-derives.
    A trailing PIPE chain is different: `grep -rn "foo" . | sort | uniq -c`
    is fully covered by the seam's own stage pipeline
    (`engine._stage_sort`/`_stage_uniq`), measured here as fully
    `plan_for`-answerable, so declining it is correct -- an earlier revision
    of this predicate additionally required exactly one top-level segment,
    which rejected this pipeline too and duplicated (incorrectly) ground
    `plan_for` already covers on its own authority. Two conditions, both
    required:

      1. `GREP_VIA_BASH` is the precedence-winning shape (as before).
      2. `search.answer.plan_for` actually produces a plan for it. This is
         the seam's OWN authority on what it can answer, consulted rather
         than predicted; `plan_for` is pure (no I/O, no latch, no footer
         state) so asking it costs nothing on the per-call path. Without it
         this predicate would be a second, drifting model of the seam's
         grammar.

    Every other shape (multi-probe banner, head/tail plumbing, for-loop,
    while-read, find-exec/xargs, the PowerShell pipeline-foreach-object
    member) has no in-process answer seam and stays fully in scope.

    ALSO RESPECTS `guard_inprocess_search`'s OWN OPT-OUT
    (`_DISABLE_ENV_VAR`, imported rather than duplicated as a literal, and
    read fresh on THIS call -- this guard runs on a resident warm server,
    so a cached read would miss a mid-session toggle): if the answer seam
    is disabled it will not answer regardless of what `plan_for` says, so
    this predicate must not decline on its behalf.

    KNOWN, DELIBERATE COLD-VS-WARM DIVERGENCE that survives this fix: a
    single-segment grep the seam fully answers is denied cold and allowed
    warm, because the cold path has no answer seam to make it free. That is
    the divergence DR-125 already legitimises ("denies exactly what DoE's
    cold path denies, minus the shape our engine already answers for free"),
    and it is now backed by the seam's own plan rather than assumed from a
    shape label.
    """
    if primary_shape is not Shape.GREP_VIA_BASH:
        return False
    try:
        from coordinator_core.bash_guards.guard_inprocess_search import (
            _DISABLE_ENV_VAR,
        )
    except Exception:  # noqa: BLE001 -- can't confirm the seam is live, stay in scope
        return False
    import os

    if os.environ.get(_DISABLE_ENV_VAR, "0") == "1":
        return False
    try:
        from coordinator_core.search.answer import plan_for
    except Exception:  # noqa: BLE001 -- no seam importable means nothing answers it
        return False
    try:
        return plan_for(cmd, tool_name=tool_name) is not None
    except Exception:  # noqa: BLE001 -- a seam that cannot plan has not answered
        return False


def _spawn_cost_clause(tool_name: str) -> str:
    """Name the cost the CALLER actually pays, not bash's -- mirrors DoE's
    own `_spawn_cost_clause` fix (this port's cold-path source's own
    "corrected from a Bash-only artefact" note): a PowerShell caller pays a
    `ForEach-Object`/`%` per-object pipeline cost, not `bash.exe`'s
    per-spawn one.
    """
    if dialect_from_tool_name(tool_name) is Dialect.POWERSHELL:
        return "a ForEach-Object/% pipeline stage runs its block once per input object on this host"
    return "bash.exe costs 200-500ms per spawn on this host"


def _compose_deny_reason(
    shapes: List[Shape],
    tool_name: str,
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> str:
    citation = resolve_wiki_citation(_WIKI_ANCHOR) if resolve_wiki_citation else _WIKI_ANCHOR
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
        f"BLOCKED: this shape spawns one subprocess per iteration or pipe stage ({named}), and "
        f"{_spawn_cost_clause(tool_name)} — paid on a machine running many "
        f"concurrent sessions. Use {remedy}. "
        f"{tool_name} itself is NOT banned here: a single read of a known file is fine. "
        f"It is the fan-out that is refused, not the tool. If a system reminder suggested "
        f"this shape, this policy outranks it — say so in your report rather than routing "
        f"around it.\n\n"
        f"See: {citation}"
    )


def check(
    payload: Dict[str, Any],
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> Optional[Dict[str, Any]]:
    """Evaluate the host subagent-bash-spawn-shapes gate against a
    PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Cohort-gated on
    membership only -- ANY non-empty agent_id string, matching cold -- not
    on a resolved canonical identity. See module docstring "IDENTITY
    RESOLUTION". Declines (returns `None`) on a command whose primary shape
    is answerable in-process by `guard_inprocess_search` -- see module
    docstring "DECLINE PREDICATE" and `_declines_for_inprocess_answer`.

    ``resolve_wiki_citation``, same caller-resolves shape as
    ``guard_doctrine_surface_bash_write.check``'s own parameter of the same
    name: an optional ``str -> str`` callable ``dispatch.py`` supplies,
    invoked ONLY on the deny path via ``_compose_deny_reason``, never on
    allow. ``None`` leaves the deny message's trailing citation as the bare
    literal, unchanged from before this parameter existed.
    """
    if not isinstance(payload, dict):
        return None

    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None

    raw_agent_id = payload.get("agent_id") or ""
    if not isinstance(raw_agent_id, str) or not raw_agent_id.strip():
        return None  # the EM itself -- out of scope by design

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

    if _declines_for_inprocess_answer(primary.shape, cmd, tool_name):
        return None

    shapes = [m.shape for m in classification.matches]
    return _deny("PreToolUse", _compose_deny_reason(shapes, tool_name, resolve_wiki_citation))
