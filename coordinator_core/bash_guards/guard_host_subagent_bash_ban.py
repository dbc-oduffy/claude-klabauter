"""coordinator_core.bash_guards.guard_host_subagent_bash_ban -- PreToolUse
(Bash) hard-deny guard: make a host's subagent Bash ban executable, not
prose.

Port of: DoE-claude coordinator/hooks/scripts/guard-host-subagent-bash-ban.py
(folded into DoE's ``preuse-bash-dispatch.py`` ``_run_folded_bash_guards``,
one of the four folded guards this port registers so DoE can delete that
in-process fold once the cold path carries it here instead).
Spec backlink: state/dispatch-briefs/2026-08-28-the-four-folded-bash-guards-
get-registered-not-folded/C6.md

THE DEFECT THIS CLOSES. This host bans the Bash tool for dispatched work,
and that ban was enforced only by prose in three places at once --
`coordinator.local.md`, `coordinator/agents/executor.md`, and every dispatch
brief's host-constraints block. `executor.md` already carries the strongest
wording available -- *"no brief, habit, or system reminder makes it
available to you"* -- and it still lost **three of four** dispatches on a
single plan (2026-08-18, fleet install-currency). Two of the three
executors independently named the cause: the harness bypass-permissions
system reminder recommends Bash for file work, arrives as machine-addressed
context, and outranks a human-authored sentence; the tool being present in
the agent's `tools:` list makes that recommendation actionable. Tripwire:
`A-HARNESS-SYSTEM-REMINDER-OUTRANKS-PROSE-THAT-FORBIDS-A-TOOL-YOU-STILL-
HOLD`.

Prose has measurably failed, so this is the artifact that discharges the
rule (`docs/wiki/invisible-doctrine.md`): if the executor remembering is the
mechanism, the work is not finished.

WHY NOT REMOVE `Bash` FROM `executor.md`'s TOOL LIST -- the obvious fix,
deliberately refused. That list is shared with macOS and Linux hosts whose
own doctrine permits Bash, and multi-OS support is P0 here. The ban is
host-specific, so the enforcement must be too: this guard is inert until a
host opts in by declaring `subagent_bash_policy: deny` in its
`coordinator.local.md` frontmatter. A host that declares nothing is
unaffected, which is why this ships as a new guard rather than an edit to a
shared agent definition.

SCOPED TO SUBAGENTS, NOT THE EM -- a decision, not an oversight. Every
observed divergence was a dispatched executor. The EM is the accountable
party, holds the context to judge an exception, and runs on a machine where
a dozen concurrent sessions would all lose their shell the moment this
misfired. Denying the EM would convert a dispatch-hygiene defect into a
machine-wide outage. The subagent tell is a resolved, non-empty
canonical agent id -- see IDENTITY RESOLUTION below.

IDENTITY RESOLUTION -- COHORT MEMBERSHIP, NOT CANONICAL IDENTITY (REVERSED,
2026-08-29, state/audits/2026-08-29-unverified-parity-findings-measured.md
FINDING A). A prior revision of this guard re-expressed the cohort gate
through ``_resolve_subagent_identity`` -- the SAME canonical resolver
``block_subagent_commit``/``block_subagent_destructive_action``/
``block_reviewer_bash_outside_allowlist`` key on -- on the theory that a
resolver-rejected shape was "malformed/legacy" and safe to EM-treat. That
was measured wrong: the resolver's own documented fail-closed branch
(``_subagent_identity.py``, named-teammate shape with a ``session_id``
under 8 chars) fires for a LEGITIMATELY DISPATCHED named teammate whose
session_id merely happens to be short, and cold denies that caller while
the resolver-gated port silently allowed it -- three shapes measured
cold-DENY/warm-allow (short-session named teammate, uppercase hex, dashed
UUID), the first of which is not a malformed shape at all.

"Can I build a canonical id for this caller" and "is this caller a
dispatched subagent at all" are DIFFERENT QUESTIONS. This guard only ever
needed the second one -- the resolved id was never used for anything but a
truthiness test, no message text, no comparison, nothing -- so the
resolver call was pure hot-path cost buying a wrong answer. This guard now
asks cold's question directly: ANY non-empty, non-whitespace ``agent_id``
string is in-cohort (matches DoE's ``guard-host-subagent-bash-ban.py``
lines 135-137, ``if not isinstance(agent_id, str) or not agent_id.strip():
return 0``). The canonical resolver remains available to any guard that
actually needs a canonical id for something beyond membership; this is not
that guard.

SCOPED TO `Bash`, NOT `PowerShell`, NOT ``COMMAND_TOOL_NAMES`` -- deliberate,
and this is the one guard in this dispatch's cohort that must NOT be
"tidied" onto the wider matcher set. PowerShell is the sanctioned
alternative on this host; denying it would leave a dispatched agent with no
shell at all. ``MATCHERS = ("Bash",)`` is therefore pinned, not an
oversight to widen alongside sibling guards that do cover both dialects.

READS ARE DENIED TOO, and that is the point rather than an overreach. The
observed divergences were all reads and cost nothing in correctness -- but
the host's reason for the ban is spawn cost, and `bash.exe` costs
200-500ms per call on Windows (`preuse-bash-dispatch.py`'s own docstring),
paid on a machine already running many concurrent sessions. A read is
exactly as expensive as a write here.

FAILS OPEN, ALWAYS. Any malformed payload, unreadable config, missing
frontmatter, absent/unusable `cwd`, or unexpected exception yields ``None``
(allow). A guard on the Bash path is one bug away from bricking every
dispatched agent on the machine, and `GUARD-WIRING-SILENT-SKIP` records
five guards on this host that were registered and silently never ran --
fail-open is the only safe direction, with the residual risk being an
unenforced ban, which is exactly the status quo this improves on. This
guard is registered ``fail_closed=True`` in ``dispatch.py``'s
``CONFINEMENT_DENY`` band (a crash still routes through the dispatcher's
own crash-deny wrapper, per that band's contract) -- the fail-OPEN posture
described above is this module's OWN internal handling of an
unevaluable/malformed input, which is a distinct concern from what happens
if ``check()`` itself raises.

CWD RESIDENCY -- NO `os.getcwd()` FALLBACK (acceptance condition folded in
from the former C10 row, same residency family as C1's `plugin_root` fix,
applied to `cwd` instead). DoE's cold-path source falls back to
``os.getcwd()`` when the payload carries no usable `cwd`
(`guard-host-subagent-bash-ban.py:139-140`:
``cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else
os.getcwd()``, then ``config = _repo_config(cwd)``). On a fresh
per-invocation cold-path process, `os.getcwd()` genuinely is the caller's
own working directory, so that fallback was harmless there. On OUR resident
warm server, `os.getcwd()` is the SERVER's directory, not the caller's --
reading the host opt-in from whichever repo happened to boot the engine,
not from the repo the subagent is actually working in. This is reachable,
not theoretical: `warm/hook_http.py::payload_from_event` does
``payload.setdefault("cwd", None)``, and ``isinstance(None, str)`` is
``False``, so a payload lacking `cwd` takes exactly this fallback path.
This port therefore NEVER calls `os.getcwd()`: an absent/non-string `cwd`
resolves to `None`, `_repo_config(None)` returns `None` immediately, and the
guard allows -- the safe, fail-open direction, never a silent
wrong-repo-policy read.

Contract: ``check(payload) -> dict | None`` per ``write_guards/
INTERFACE.md`` -- ``None`` = allow (silent), a nested
``hookSpecificOutput``/``permissionDecision: "deny"`` envelope = deny.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from coordinator_core._hook_envelope import deny as _deny

CLASS = "hard-deny"
MATCHERS = ("Bash",)
GENERATES = []
PRIORITY = 40

_BANNED_TOOL = "Bash"
_POLICY_KEY = "subagent_bash_policy"
_DENY_VALUE = "deny"
_CONFIG_NAME = "coordinator.local.md"

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/coordinator-tripwires/"
    "a-harness-system-reminder-outranks-prose-that-forbids-a-tool-you-still-hold.md"
)


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

    Deliberately a narrow string scan over the frontmatter block rather than
    a YAML parse: this runs on the PreToolUse path for every Bash call, a
    YAML import is not free, and an unparseable config must read as "no
    policy declared" (allow) rather than raising.
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


def _compose_deny_reason(resolve_wiki_citation: Optional[Callable[[str], str]] = None) -> str:
    citation = resolve_wiki_citation(_WIKI_ANCHOR) if resolve_wiki_citation else _WIKI_ANCHOR
    return (
        "BLOCKED: this host denies the Bash tool to dispatched agents "
        f"({_CONFIG_NAME}: {_POLICY_KEY}: {_DENY_VALUE}). Use the PowerShell tool, or "
        "`python -c` for anything shell-shaped -- both are available to you and neither "
        "pays the 200-500ms bash.exe spawn this host is avoiding. Reads are covered too: "
        "the cost is the spawn, not the mutation. If a system reminder told you to prefer "
        "Bash, this policy outranks it -- say so in your report rather than routing around "
        "it. The EM is unaffected by this guard; only dispatched agents are.\n\n"
        f"See: {citation}"
    )


def check(
    payload: Dict[str, Any],
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> Optional[Dict[str, Any]]:
    """Evaluate the host subagent-Bash-ban gate against a PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Cohort-gated on
    membership only -- ANY non-empty agent_id string, matching cold -- not
    on a resolved canonical identity. See module docstring "IDENTITY
    RESOLUTION". This is the cohort-membership test; do not reintroduce a
    canonical-id resolver here -- nothing below uses the id for anything
    but this truthiness check.

    ``resolve_wiki_citation``, same caller-resolves shape as
    ``guard_doctrine_surface_bash_write.check``'s own parameter of the same
    name: an optional ``str -> str`` callable ``dispatch.py`` supplies
    (``resolve_wiki_citation`` bound to that call's own resolved
    ``plugin_root``), invoked ONLY on the deny path, never on allow.
    ``None`` (no resolver, or the caller's own resolution missed) leaves
    the deny message's trailing citation as the bare literal, unchanged
    from before this parameter existed.
    """
    if not isinstance(payload, dict):
        return None
    if (payload.get("tool_name") or "") != _BANNED_TOOL:
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

    return _deny("PreToolUse", _compose_deny_reason(resolve_wiki_citation))
