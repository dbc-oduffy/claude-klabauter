"""
coordinator_core.hooks.cater_subagent_start -- engine-side catering
composer for a SubagentStart payload (C2,
docs/plans/2026-08-21-catering-rides-subagentstart.md).

DoE-claude's PreToolUse(Agent) hook (`enforce-agent-dispatch-mode.py`)
composes four legs for an `Agent`-tool child; a Workflow `agent()` spawn
never reaches that hook (see the gating plan's Problem section), so it
arrives uncatered. `docs/research/spike-verdicts/2026-08-21-subagentstart-
additionalcontext-caters-a-workflow-spawn.md` proved `SubagentStart`
`hookSpecificOutput.additionalContext` is a working per-child injection
channel on BOTH the `Agent` path and a Workflow `agent()` spawn, carrying
every input the existing assembler/provisioner already take (`agent_type`,
`cwd`, `session_id`).

This module ports the Agent-path composition ORDER verbatim from
`enforce-agent-dispatch-mode.py :: main` -- load-bearing for how a child
reads the result:

    sidecar offer (report_sidecar) OR miss notice
      -> injected contract blocks
      -> role framing (LAST, unconditional, outside any roster lookup)

Three legs, not four. Anti-scope (gating plan): the named-teammate clause
(`_compose_teammate_clause` on the Agent path) is deliberately NOT ported --
its gate field (`tool_input.name`) does not exist on a SubagentStart
payload, and its `SendMessage("main")` return channel does not exist for a
Workflow child.

In-process only -- no subprocess. `provision_report.py`'s own
`_PROVISION_TIMEOUT_SECONDS = 10` exceeds this registration's entire 5s
`SubagentStart` budget (gating plan § Cost), so a guarded-subprocess call
would be unreachable and a harness-killed hook could strand a sidecar on
disk no child was ever told about -- `_provision` and
`assemble_contract_blocks_for_payload` are called directly, reusing
`coordinator_core.subagent_sandbox.provision_report` and
`coordinator_core.subagent_sandbox.engine` rather than re-deriving
eligibility/assembly logic.

Fail-open on every arm (AC5), matching `provision_report.py`'s contract
verbatim: `compose_catering` never raises, and a failure in any one leg
never suppresses the other two -- each leg is independently wrapped. An
unenumerated type (not on `policy.report_sidecar`) stays silent for the
sidecar leg -- the same "expected miss, no diagnostic" posture
`coordinator_core.dispatch.provision._log_unenumerated_sidecar_miss`
already takes for the sibling subagent-sidecar seam (this module does not
duplicate that diagnostic; it is a defence-in-depth net for a governance
gap this plan does not own).

The `sidecar_path: <path>` and `sidecar_provisioning: missed` marker lines
are byte-identical to their Agent-path counterparts
(`_compose_sidecar_offer_text` / `_compose_sidecar_miss_text` in
`enforce-agent-dispatch-mode.py`) minus that hook's DoE-local
`_message_envelope.compose`/`render` prose-capping wrapper, which this repo
does not own or import -- the prose itself is carried verbatim so a
consuming agent's exact-line marker match (`coordinator/agents/
code-reviewer.md` § HARD RULE step 1) behaves identically on both paths.

Negative-spec: this module does NOT resolve `contract_blocks` out of
`subagent-sandbox-policy.yaml` itself -- that resolution is DoE-owned
(`enforce-agent-dispatch-mode.py :: _resolve_contract_blocks`, keyed off
their own policy file) and is expected to already be present on
`payload["contract_blocks"]` by the time this module sees the payload,
exactly as `assemble_contract_blocks_for_payload` already consumes it for
the Agent path (C3/AC7 wires DoE's `SubagentStart` shim to populate it).
Absent, this leg is simply empty -- fail-open, not re-derived here.

Negative-spec: this module does NOT call `coordinator_core.dispatch.
provision` (the subagent-sidecar DECISION-OBJECT provisioner) -- that is a
distinct seam (`{"subagent_sidecar": <path>}`, a different marker, a
different document shape) that `enforce-agent-dispatch-mode.py :: main`
never calls either; the three legs ported here are exactly the ones that
hook composes for the Agent path.

AC9 (payload sizing) -- MEASURED, not assumed: for `coordinator:staff-eng`
(the widest `contract_blocks` row on disk, 11 blocks), the assembled
`injected_prompt_blocks` alone measures ~31.9K characters -- roughly 3x the
documented ~10,000-character `additionalContext` cliff BEFORE the sidecar
offer or role framing are added. This is a real finding, not a hypothetical
one; it is carried up in this chunk's own report rather than silently
declared "done" against a payload nobody sized. See that report for the
full breakdown and the plan-level disposition this needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core._settings_home import claude_config_dir
from coordinator_core.subagent_sandbox.engine import (
    load_policy,
    resolve_effective_types,
    resolve_git_root,
)
from coordinator_core.subagent_sandbox.provision_report import (
    _provision,
    assemble_contract_blocks_for_payload,
)

#: Same machine-readable marker shape as DoE's Agent-path hook (Concern
#: B.3) -- consumer agents (`coordinator/agents/code-reviewer.md` § HARD
#: RULE step 1) key off this exact line, and AC2 asserts on it.
SIDECAR_PATH_MARKER_PREFIX = "sidecar_path: "

#: Same marker as `enforce-agent-dispatch-mode.py :: _compose_sidecar_miss_
#: text` -- AC5's partial-catering clause tests this exact line.
SIDECAR_MISS_MARKER = "sidecar_provisioning: missed"

#: Snippet path, relative to the coordinator-claude plugin root, in either
#: of its two known on-disk shapes (see `_resolve_role_append_snippet_path`).
_ROLE_APPEND_RELATIVE = ("snippets", "agent-role-dispatched.md")

#: Plugin directory name under `<claude_config_dir>/plugins/`.
_PLUGIN_DIR_NAME = "coordinator-claude"


def _compose_sidecar_offer_text(sidecar_path: str) -> str:
    """Port of `enforce-agent-dispatch-mode.py ::
    _compose_sidecar_offer_text` -- same DR-091 "unconditional, not an
    offer" framing, same trailing machine-readable marker line, own line,
    newline-preceded. Trimmed of the DoE-local `_message_envelope.compose`/
    `render` prose-capping wrapper (a plugin-resident mechanism this repo
    does not own or import) -- the prose itself is carried verbatim.
    """
    return (
        "\n\nYou have a run-report sidecar for this dispatch -- capture run "
        "notes and any divergence there; filling it in is expected, not "
        "optional.\n" + SIDECAR_PATH_MARKER_PREFIX + sidecar_path
    )


def _compose_sidecar_miss_text() -> str:
    """Port of `enforce-agent-dispatch-mode.py ::
    _compose_sidecar_miss_text` verbatim in substance -- an eligible type
    whose provisioning came back empty is TOLD, never left to read silence
    as ineligibility (AC5's partial-catering clause)."""
    return (
        "\n\nSidecar provisioning did not complete for this dispatch -- no "
        "scaffold exists on disk. Scaffold your own at the path your agent "
        "definition names, persist your findings there as normal, and say "
        "in them that provisioning missed.\n" + SIDECAR_MISS_MARKER
    )


def _resolve_role_append_snippet_path() -> Optional[Path]:
    """Locate `snippets/agent-role-dispatched.md` under the settings root --
    never a hardcoded plugin path. This module lives in claude-klabauter, a different
    on-disk checkout than the coordinator-claude plugin the snippet ships
    in, so the Agent-path hook's own `Path(__file__).resolve().parents[2]`
    colocated resolution (that hook lives INSIDE the plugin it reads from)
    has nothing to anchor against here.

    Probes both known plugin-root shapes for the SAME `<claude_config_dir>/
    plugins/coordinator-claude` root -- a DoE dev-clone shape (`.../
    coordinator-claude/coordinator/snippets/...`) and a marketplace/OSS-
    mirror shape (`.../coordinator-claude/snippets/...`), the same two
    shapes `coordinator_root._resolve_plugin_root_for_machine_local`
    already names for a DIFFERENT artifact (`templates/bin/
    _machine_local.py`) -- probing here for THIS function's own artifact
    directly rather than a different file standing in as a proxy (see that
    module's own docstring for why a proxy probe is the wrong shape).
    Returns ``None`` on no match; the caller fails open to "" (Concern D's
    role-framing leg simply does not fire).
    """
    plugin_base = claude_config_dir() / "plugins" / _PLUGIN_DIR_NAME
    for candidate_root in (plugin_base / "coordinator", plugin_base):
        candidate = candidate_root.joinpath(*_ROLE_APPEND_RELATIVE)
        if candidate.is_file():
            return candidate
    return None


def _load_role_append() -> str:
    """Fail-open snippet read -- matches every other leg's fail-open
    contract (missing file, unreadable, wrong shape all degrade to "")."""
    try:
        path = _resolve_role_append_snippet_path()
        if path is None:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _resolve_sidecar_leg(
    payload: Dict[str, Any], cwd: Optional[str]
) -> tuple[str, str]:
    """Resolve the sidecar-offer/miss-notice leg. Returns
    ``(sidecar_path, sidecar_text)`` -- ``sidecar_path`` is `""` when no
    sidecar was provisioned (whether because the type is ineligible, or
    because an eligible provision came back empty); ``sidecar_text`` is
    the composed offer/miss prose, or `""` when neither applies (the
    ineligible-type case -- stays silent, matching the Agent-path hook's
    own `_is_report_sidecar_eligible` gate on the miss notice).

    `policy_path=None` on both `load_policy` and `_provision` deliberately
    reuses `load_policy`'s own existing 3-rung fallback (explicit path ->
    `SUBAGENT_SANDBOX_POLICY` env var -> `CLAUDE_PLUGIN_ROOT`-relative
    default) rather than re-deriving a policy-path resolver here -- unlike
    the role-framing snippet, `load_policy` already has a working default
    leg (see that function's own docstring), so a second bespoke resolver
    would duplicate logic that already exists, exactly the shape this
    module's own docstring says to avoid.
    """
    git_root = resolve_git_root(cwd)
    policy = load_policy(None)
    agent_id, agent_type, subagent_type = resolve_effective_types(payload, git_root)

    sidecar_eligible = bool(
        (agent_type and agent_type in policy.report_sidecar)
        or (subagent_type and subagent_type in policy.report_sidecar)
    )
    if not sidecar_eligible:
        return "", ""

    sidecar_path = _provision(payload, None, cwd) or ""
    if sidecar_path:
        return sidecar_path, _compose_sidecar_offer_text(sidecar_path)
    return "", _compose_sidecar_miss_text()


def compose_catering(payload: Dict[str, Any], *, cwd: Optional[str] = None) -> str:
    """Compose the SubagentStart `hookSpecificOutput.additionalContext`
    catering string for `payload`, or `""` when there is nothing to cater.

    Ports the Agent-path composition ORDER verbatim (module docstring):
    sidecar offer OR miss notice -> injected contract blocks -> role
    framing, role framing LAST and unconditional. Fail-open on every arm
    (AC5): each leg is independently wrapped, so one leg's exception never
    suppresses another -- an unhandled exception anywhere in this function
    is still impossible by construction (every leg below is its own
    try/except), but the whole function is additionally defensive against
    a malformed `payload` (non-dict) up front.

    Never raises. The caller (the SubagentStart shim relaying this op,
    C3) must never see this function brick a spawn.
    """
    if not isinstance(payload, dict):
        return ""

    sidecar_path = ""
    sidecar_text = ""
    try:
        sidecar_path, sidecar_text = _resolve_sidecar_leg(payload, cwd)
    except Exception:
        sidecar_path, sidecar_text = "", ""

    injected_blocks = ""
    try:
        injected_blocks = (
            assemble_contract_blocks_for_payload(
                payload, cwd=cwd, report_sidecar_path=sidecar_path
            )
            or ""
        )
    except Exception:
        injected_blocks = ""

    role_append = ""
    try:
        role_append = _load_role_append()
    except Exception:
        role_append = ""

    parts: list[str] = []
    if sidecar_text:
        parts.append(sidecar_text)
    if injected_blocks:
        parts.append("\n\n" + injected_blocks)
    if role_append:
        parts.append("\n\n" + role_append)

    return "".join(parts)
