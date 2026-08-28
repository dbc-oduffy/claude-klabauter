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

IDENTITY RESOLUTION -- CANONICAL RESOLVER, NOT DoE's RAW TEST (Review:
staff-eng, finding 3). DoE's cold-path source (guard-host-subagent-bash-ban.py
lines 135-137) treats ANY non-empty ``agent_id`` string as "a subagent" --
``if not isinstance(agent_id, str) or not agent_id.strip(): return 0``. This
port instead re-expresses the cohort gate through the SAME canonical
resolver ``block_subagent_commit``/``block_subagent_destructive_action``/
``block_reviewer_bash_outside_allowlist`` already key on --
``coordinator_core.write_guards.block_subagent_plan_body_write.
_resolve_subagent_identity`` (re-exporting ``write_guards._subagent_
identity._resolve_subagent_identity``) -- rather than importing DoE's raw
non-empty-string test verbatim.

This is a DELIBERATE, NAMED semantic delta, not a defect to reconcile: the
resolver fail-closes to ``""`` on any raw ``agent_id`` shape it does not
recognize (anything other than bare hex ``^[a-f0-9]{12,}$``, named-teammate
``a<name>-<16hex>``, or already-canonical ``<name>@session-<short>``), and
``""`` reads as "no subagent identity resolved" here -- i.e. treated as the
EM (allow), not as a subagent (deny). DoE's raw test would instead treat
that SAME malformed/legacy id shape as a subagent (deny), because it asks
only "is this a non-empty string", not "is this a shape coordinator's own
identity resolver recognizes". A shape the resolver rejects is therefore
EM-treated here and subagent-treated on DoE's cold path -- state this
explicitly so C9's parity oracle asserts it as an intended divergence
rather than tripping over it as a mismatch.

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
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import deny as _deny
from coordinator_core.write_guards.block_subagent_plan_body_write import (
    _resolve_subagent_identity,
)

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


def _compose_deny_reason() -> str:
    return (
        "this host denies the Bash tool to dispatched agents "
        f"({_CONFIG_NAME}: {_POLICY_KEY}: {_DENY_VALUE}). Use the PowerShell tool, or "
        "`python -c` for anything shell-shaped -- both are available to you and neither "
        "pays the 200-500ms bash.exe spawn this host is avoiding. Reads are covered too: "
        "the cost is the spawn, not the mutation. If a system reminder told you to prefer "
        "Bash, this policy outranks it -- say so in your report rather than routing around "
        "it. The EM is unaffected by this guard; only dispatched agents are.\n\n"
        f"See: {_WIKI_ANCHOR}"
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the host subagent-Bash-ban gate against a PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Identity-gated
    to a resolved subagent only -- see module docstring "IDENTITY
    RESOLUTION" for the deliberate divergence from DoE's raw non-empty-
    string test.
    """
    if not isinstance(payload, dict):
        return None
    if (payload.get("tool_name") or "") != _BANNED_TOOL:
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

    return _deny("PreToolUse", _compose_deny_reason())
