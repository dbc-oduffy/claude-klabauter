"""coordinator_core.bash_guards._write_bump_message -- the write-confinement
speed bump's shared deny copy, consumed by all three bump guards (Bash
cross-repo, Bash outside-repo, and the `Write`/`Edit`/`MultiEdit` tool
surface) so every surface speaks with one voice.

Spec backlink: docs/plans/2026-08-03-narrow-write-confinement-bump.md,
chunk C2, "Destination-class axis in the message module, owner-aware
publish copy". Ported deliverable of example-doctrine-repo's `{C2, C12}` group,
reduced here to `{C2}` (C12 shipped example-doctrine-repo-side, `6d0a8c4fa`).

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY. Under the plan's "Design
posture -- passable by construction", detection exists only to trigger this
message, and the message is the actual product: a correct verdict with an
unclear message has failed the plan even though every other chunk is right.

TWO AGENT CLASSES x TWO DESTINATION CLASSES = FOUR TEMPLATES. The agent
split (EM vs subagent -- see the 2026-08-02 executor-confinement lesson)
stays exactly two; this chunk adds an ORTHOGONAL destination-class axis
(`DESTINATION_FOREIGN` vs `DESTINATION_PUBLISH`), never a third agent class
(Anti-scope, this plan).

  - `DESTINATION_FOREIGN` -- today's copy: a sibling source repo, not a
    registered publish mirror. EM-class is told to check with its PM;
    subagent-class is told to stop and report to its dispatching EM and
    write into its sandbox instead.
  - `DESTINATION_PUBLISH` -- a registered `publish.mirrors.*.path` target.
    Drafted around DURABILITY, not ownership (this plan's PM ruling):
    committing into a mirror is not FORBIDDEN, it is WORSE than the
    alternative, because a change made in the source repo percolates out
    by script while the same change hand-made in the mirror is harder to
    manage and owned by nobody. This copy therefore names the mirror, its
    registered `owner` (CONTEXT ONLY -- the verdict is never gated on
    owner, see `_write_bump_applicability.target_is_publish_destination`),
    and the durable source-side alternative (a doctrine citation, never a
    bare directory -- see Anti-scope's "do not hardcode a mirror
    DESTINATION path"). It contains no "repos you don't own", no
    ownership-violation vocabulary, and no PM-checking / cross-repo-memo
    pointer -- publishing into a mirror is not forbidden, so none of that
    vocabulary belongs here (AC3, AC15).

Both classes get the same clear line -- `_write_bump_marker.clear_line()`,
never re-composed here -- because clearing the bump is a per-session action
available to whichever agent is running when assent is already in hand.
Phrased as standing the bump down FOR THAT TARGET: the sibling plan's C3
scopes the marker per-`(session, target)`, so the target-side file location
this line names is intentional scoping, not an odd place for a file.

SELF-ATTRIBUTION. Every template opens "Coordinator guard --" so the reader
identifies this as fleet infrastructure speaking, never mistakes it for
prompt injection lifted from the write target's own content -- the failure
mode this plan's C11 body names by hand ("a dispatched executor once read
this guard's own message as prompt injection and ignored it").

BUDGET -- every rendered template must fit
`_message_size.MESSAGE_PROSE_CAP_BYTES` (220 BYTES of prose, measured by
`_message_size.measure_envelope`), not 280 characters (this repo has no
`docs/wiki/guard-message-concision.md` -- that page is example-doctrine-repo-resident and
documents a DIFFERENT surface's cap in a different unit; do not cite it
here). Every template below leans on `_alternative_liveness`'s own
`_CUE_WINDOW_RE`/`_BACKTICK_RE`/`_cue_windows` exemption semantics, used as
designed rather than gamed:

  1. The bare word "instead" opens each template, right after the
     self-attribution clause, so a `_CUE_WINDOW_RE` cue window opens over
     the entire remainder of the message (no second cue is needed).
  2. Every variable-length fact (the target path, the mirror owner, the
     doctrine filename) is backticked -- `_BACKTICK_RE` spans inside a cue
     window are exempt from the prose count, so these facts cost nothing
     regardless of how long the real path is (verified: prose bytes stay
     stable across a short and a much longer synthetic path).
  3. The clear line is INDENTED (two leading spaces) and follows its
     introducing sentence with NO BLANK LINE in between -- `_cue_windows`
     cuts a window at the next blank line, and a blank line before the
     indented command would close the window before `_INDENTED_CMD_RE` ever
     gets a chance to exempt it. This is the trap the plan's own drift note
     names: the natural way to format the message (a blank line before an
     indented command) silently costs the whole exemption.

Residual coupling, named rather than hidden: these templates depend on
`_alternative_liveness`'s cue/exemption regexes to stay under cap. If those
regexes tighten, these templates could go over cap without any edit here --
AC7's firing-row/measure_envelope assertion is what would catch that. Margin
against the 220-byte cap is NOT uniform across the four templates
(`measure_envelope`, measured): foreign/em 169 bytes (margin 51),
foreign/subagent 192 bytes (margin 28 -- the tightest of the four),
publish/em 149 bytes (margin 71), publish/subagent 149 bytes (margin 71).
foreign/subagent is the one that breaks first if these regexes ever tighten.

CALLERS RESOLVE THE INPUTS; THIS MODULE ONLY COMPOSES. `target_repo`,
`session_repo`, `gitdir`, `session_id`, `sandbox_root`, and (for the publish
class) `destination_owner` are all taken as arguments, never re-derived
here -- this module does not import `_write_bump_applicability` (the
publish-owner resolver lives there, in `publish_destination_owner`) to avoid
an import-order hazard against a concurrently-authored sibling module; the
caller (C4/C5) resolves the owner and passes it through.

Negative-spec:
    - Does NOT decide whether to bump, or which destination class applies
      -- that is every calling guard's own job, using
      `_write_bump_applicability.target_is_publish_destination`.
    - Does NOT compose or resolve a gitdir/marker path itself -- delegates
      to `_write_bump_marker.clear_line()`.
    - Does NOT add a third agent class or a finer subagent taxonomy.
    - Does NOT name an env-var, config flag, or other out-of-band bypass.
    - Does NOT match a publish destination by path pattern, gate the
      verdict on `owner`, or hardcode a mirror DESTINATION path anywhere in
      this module's own literals (Anti-scope).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._write_bump_marker import clear_line
from coordinator_core.subagent_sandbox.engine import resolve_effective_types

#: The two agent classes this module's copy is drafted for (see module
#: docstring, "TWO AGENT CLASSES"). No third value is ever returned by
#: `resolve_agent_class()` -- deliberately a two-way split.
AGENT_CLASS_EM = "em"
AGENT_CLASS_SUBAGENT = "subagent"

#: The two destination classes this module's copy is drafted for (see
#: module docstring, "TWO AGENT CLASSES x TWO DESTINATION CLASSES"). An
#: orthogonal axis over the agent-class split above -- never a third agent
#: class (Anti-scope).
DESTINATION_FOREIGN = "foreign"
DESTINATION_PUBLISH = "publish"

#: The durable source-side alternative's doctrine citation (AC15) -- the
#: doctrine SECTION, never a bare directory, and never the mirror's own
#: destination path (Anti-scope: "do not hardcode a mirror DESTINATION path
#: anywhere"). This is a citation of a example-doctrine-repo-resident wiki page name,
#: not a filesystem path this guard could read or test.
_PUBLISH_DOCTRINE_CITATION = "plugin-extraction-and-distribution.md"
_PUBLISH_DOCTRINE_SECTION = "Publish-Repo Content Authoring"


def resolve_agent_class(payload: Dict[str, Any], git_root: Optional[str]) -> str:
    """Classify a session as EM-class or subagent-class for message
    selection (see module docstring, "TWO AGENT CLASSES").

    Uses `coordinator_core.subagent_sandbox.engine.resolve_effective_types`
    -- the fleet's existing OR-resolver -- rather than re-deriving agent
    identity here. Subagent-class the moment either resolved leg
    (`agent_id`, `subagent_type`) is non-empty; EM-class otherwise. Never
    raises: `resolve_effective_types` is itself fail-open, and an empty/
    missing `payload` or `git_root` simply resolves both legs empty, which
    this function reads as EM-class.
    """
    agent_id, _agent_type, subagent_type = resolve_effective_types(payload or {}, git_root)
    if agent_id or subagent_type:
        return AGENT_CLASS_SUBAGENT
    return AGENT_CLASS_EM


def _target_phrase(target_repo: str, raw_target: str = "") -> str:
    """Compose the display form of `target_repo`, optionally showing the
    PRE-translation raw token the operator actually typed alongside it.

    Spec backlink: docs/plans/2026-08-08-the-bump-message-never-showed-the-
    operat.md, chunk R1, AC1/AC2 -- the bump message must show BOTH the raw
    token and the resolved native path, distinguishably, but never print the
    same string twice under two labels (AC2's named failure mode).

    `raw_target` is CALLER-RESOLVED (see module docstring, "CALLERS RESOLVE
    THE INPUTS"): a caller passes `""` (the default) whenever it has already
    determined raw and resolved are the same value for its own candidate --
    this function does NOT itself compare `raw_target` to `target_repo`,
    because at several call sites `target_repo` is a DECORATED label (e.g.
    `bump_outside_repo_write.py`'s `"no git repo (%s)" % target_dir`), not
    the bare resolved path a straight string-equality check against
    `raw_target` could ever match -- that decision belongs to the caller,
    which knows its own plain resolved value.

    Both forms are backticked and placed inside the `_alternative_liveness`
    cue window every template already opens with the bare word "instead" --
    per module docstring, "BUDGET", a backticked span inside a cue window is
    exempt from the prose byte count, so a variable-length raw token costs
    nothing against `_message_size.MESSAGE_PROSE_CAP_BYTES` regardless of its
    own length. Only the literal connective prose ("(typed ...)") counts
    against the cap, and only when `raw_target` is non-empty.
    """
    if not raw_target:
        return f"`{target_repo}`"
    return f"`{target_repo}` (typed `{raw_target}`)"


def render_em_message(
    target_repo: str,
    session_repo: str,
    gitdir: Path,
    session_id: str,
    raw_target: str = "",
) -> str:
    """The FOREIGN-class, EM-class deny copy. Self-attributed, opens on the
    bare word "instead" to hold the whole remainder in one
    `_alternative_liveness` cue window (see module docstring, "BUDGET")."""
    line = clear_line(gitdir, session_id)
    return (
        "Coordinator guard — instead: check with your PM before writing into "
        f"{_target_phrase(target_repo, raw_target)} (not `{session_repo}`); once assented, clear this target:\n"
        f"  {line}\n"
        "Still unsure? cross-repo-memo is the sanctioned channel."
    )


def render_subagent_message(
    target_repo: str,
    session_repo: str,
    gitdir: Path,
    session_id: str,
    sandbox_root: str,
    raw_target: str = "",
) -> str:
    """The FOREIGN-class, subagent-class deny copy. `sandbox_root` is
    caller-resolved (see module docstring, "CALLERS RESOLVE THE INPUTS");
    this function only places it in the template."""
    line = clear_line(gitdir, session_id)
    return (
        "Coordinator guard — instead: you have no PM here, report to the EM that "
        f"dispatched you before writing into {_target_phrase(target_repo, raw_target)} (not `{session_repo}`); "
        f"your sandbox `{sandbox_root}` is the place to write, or once assented, "
        "clear this target:\n"
        f"  {line}"
    )


def render_publish_em_message(
    target_repo: str,
    destination_owner: str,
    gitdir: Path,
    session_id: str,
    raw_target: str = "",
) -> str:
    """The PUBLISH-class, EM-class deny copy -- drafted around durability,
    not ownership (see module docstring, "DESTINATION_PUBLISH"). Names the
    mirror, its registered owner (context, never gating), and the durable
    source-side alternative via a doctrine citation. Contains no
    "repos you don't own", no ownership-violation vocabulary, and no
    PM-checking / cross-repo-memo pointer (AC3, AC15)."""
    line = clear_line(gitdir, session_id)
    return (
        f"Coordinator guard — instead: {_target_phrase(target_repo, raw_target)} is publish mirror "
        f"(`{destination_owner}`) — durable fix belongs in source; see "
        f"`{_PUBLISH_DOCTRINE_CITATION}` § `{_PUBLISH_DOCTRINE_SECTION}`. Clear this "
        "target if you still mean to write here:\n"
        f"  {line}"
    )


def render_publish_subagent_message(
    target_repo: str,
    destination_owner: str,
    gitdir: Path,
    session_id: str,
    sandbox_root: str,
    raw_target: str = "",
) -> str:
    """The PUBLISH-class, subagent-class deny copy -- same durability
    framing as `render_publish_em_message`, routed to the dispatching EM
    rather than a PM (a dispatched agent has no PM of its own).
    `sandbox_root` is accepted for signature parity with the other
    subagent-class renderer but is not named in this copy -- the offer here
    is the durable source-side alternative, not this session's sandbox."""
    del sandbox_root
    line = clear_line(gitdir, session_id)
    return (
        f"Coordinator guard — instead: {_target_phrase(target_repo, raw_target)} is publish mirror "
        f"(`{destination_owner}`) — durable fix belongs in source; see "
        f"`{_PUBLISH_DOCTRINE_CITATION}` § `{_PUBLISH_DOCTRINE_SECTION}`. Report to "
        "your EM; if assented, clear this target:\n"
        f"  {line}"
    )


def render_bump_message(
    *,
    agent_class: str,
    target_repo: str,
    session_repo: str,
    gitdir: Path,
    session_id: str,
    sandbox_root: str = "",
    destination_class: str = DESTINATION_FOREIGN,
    destination_owner: str = "",
    raw_target: str = "",
) -> str:
    """Dispatch to the correct one of the four templates by
    `(destination_class, agent_class)`. The single entry point C4/C5/C7
    call so the three surfaces cannot drift onto different copy for the
    same class -- divergence between surfaces is the defect this plan's
    predecessor chunk (C6, `docs/plans/2026-08-02-write-confinement-guards.md`)
    exists to prevent.

    `destination_class` defaults to `DESTINATION_FOREIGN`. At authoring time
    only the two Bash guards (`bash_guards/bump_foreign_repo_write.py`,
    `bash_guards/bump_outside_repo_write.py`) still called with no
    `destination_class` keyword; the `Write`/`Edit`/`MultiEdit` tool surface
    (`write_guards/bump_out_of_repo_tool_write.py`) already resolves and
    passes `destination_class`/`destination_owner` -- wiring the
    publish-mirror carve-out through the two remaining Bash guards is this
    plan's C4/C5, not this chunk. Passing
    `destination_class=DESTINATION_PUBLISH` with no `destination_owner` is
    the caller's own contract to honour (C1's `publish_destination_owner`
    resolves it); this module renders whatever string it is given, even
    empty, rather than raising -- ordinary defensive composition, not a
    repeat of `_write_bump_applicability`'s detection-layer fail-open
    contract.

    `raw_target` (R1, docs/plans/2026-08-08-the-bump-message-never-showed-
    the-operat.md) defaults to `""`, the same "optional, safe default"
    contract `sandbox_root`/`destination_class`/`destination_owner` already
    carry (AC4) -- every caller that predates R1 keeps rendering byte-
    identical output. A caller passes a non-empty `raw_target` only when it
    has already determined (in its own terms -- see `_target_phrase`'s own
    docstring) that the PRE-translation token differs from `target_repo`;
    threaded straight through to whichever template below is selected.
    """
    if destination_class == DESTINATION_PUBLISH:
        if agent_class == AGENT_CLASS_SUBAGENT:
            return render_publish_subagent_message(
                target_repo, destination_owner, gitdir, session_id, sandbox_root, raw_target
            )
        return render_publish_em_message(target_repo, destination_owner, gitdir, session_id, raw_target)
    if agent_class == AGENT_CLASS_SUBAGENT:
        return render_subagent_message(
            target_repo, session_repo, gitdir, session_id, sandbox_root, raw_target
        )
    return render_em_message(target_repo, session_repo, gitdir, session_id, raw_target)
