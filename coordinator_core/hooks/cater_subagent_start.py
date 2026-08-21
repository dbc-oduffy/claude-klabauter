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

import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from coordinator_core._settings_home import claude_config_dir
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op
from coordinator_core.subagent_sandbox.engine import (
    load_policy,
    resolve_effective_types,
    resolve_git_root,
)
from coordinator_core.subagent_sandbox.provision_report import (
    _provision,
    _sanitize_segment,
    assemble_contract_blocks_for_payload,
)

#: Op name the SubagentStart shim relays this leg under (C3). Registered here,
#: not re-derived by the shim -- "no second registration" (plan Anti-scope):
#: this rides the existing SubagentStart registration's dispatch_ops_from_hook
#: call, ALONGSIDE "hooks.track_dispatched_agents" (the bookkeeping op), never
#: as a standalone hooks.json entry of its own.
OP_NAME = "hooks.cater_subagent_start"

#: Same machine-readable marker shape as DoE's Agent-path hook (Concern
#: B.3) -- consumer agents (`coordinator/agents/code-reviewer.md` § HARD
#: RULE step 1) key off this exact line, and AC2 asserts on it.
SIDECAR_PATH_MARKER_PREFIX = "sidecar_path: "

#: Same marker as `enforce-agent-dispatch-mode.py :: _compose_sidecar_miss_
#: text` -- AC5's partial-catering clause tests this exact line.
SIDECAR_MISS_MARKER = "sidecar_provisioning: missed"

#: Same machine-readable "key: value" shape as `SIDECAR_PATH_MARKER_PREFIX`
#: (AC9 amendment) -- the pointer a spilled-blocks companion file leaves in
#: `additionalContext` in place of the inlined blocks text.
BLOCKS_COMPANION_MARKER_PREFIX = "blocks_path: "

#: Composed-TOTAL char cap (AC9 amendment), measured against the documented
#: ~10,000-char `additionalContext` cliff (spike-verdict
#: 2026-08-21-subagentstart-additionalcontext-caters-a-workflow-spawn.md).
#: `coordinator:staff-eng` (11 `contract_blocks`, the widest row on disk)
#: measured ~31,913 composed chars against this cap -- 10 of 33 catered
#: types were over it (module docstring § AC9).
ADDITIONAL_CONTEXT_CHAR_CAP = 10_000

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


def _compose_blocks_pointer_text(blocks_rel_path: str) -> str:
    """Short pointer for a spilled-blocks companion file (AC9 amendment) --
    same machine-readable marker shape as `_compose_sidecar_offer_text`, own
    line, newline-preceded. Deliberately terse: the whole point of spilling
    is a deterministically small `additionalContext`."""
    return (
        "These contract blocks are your dispatch contract -- reading the "
        "file below is expected, not optional.\n"
        + BLOCKS_COMPANION_MARKER_PREFIX
        + blocks_rel_path
    )


def _resolve_blocks_companion_path(
    payload: Dict[str, Any], cwd: Optional[str], sidecar_path: str
) -> Optional[Tuple[Path, str]]:
    """Resolve the on-disk path (and its repo-relative string) for a
    spilled contract-blocks companion file. Anchored in the SAME per-session
    share directory `_provision` already resolves (`state/subagent-share/
    <session_id>/`) -- never re-derives that directory by hand.

    When a sidecar was provisioned this dispatch, its own path already
    carries a collision-safe discriminator (either the caller's
    `provision_key` or `_provision`'s own random nonce) -- reuse that exact
    stem with a `.blocks.md` suffix so the companion file sits beside the
    sidecar under the identical discriminator, without minting a second one.
    When no sidecar was provisioned this dispatch (ineligible type, or a
    provisioning miss), derive the same session-keyed directory `_provision`
    uses and mint an independent nonce-suffixed name -- the 2026-08-15
    concurrent-same-type incident is exactly what a nonce exists to prevent.
    """
    git_root = resolve_git_root(cwd)
    if not git_root:
        return None
    session_id = payload.get("session_id") or None
    if not session_id:
        return None
    sanitized_session_id = _sanitize_segment(str(session_id))
    if sanitized_session_id is None:
        return None

    session_dir = Path(git_root) / "state" / "subagent-share" / sanitized_session_id

    if sidecar_path:
        sidecar_leaf = Path(sidecar_path).name
        stem = sidecar_leaf[:-3] if sidecar_leaf.endswith(".md") else sidecar_leaf
        leaf_name = f"{stem}.blocks.md"
    else:
        leaf_name = f"blocks-{secrets.token_hex(4)}.md"

    session_dir.mkdir(parents=True, exist_ok=True)
    companion_path = session_dir / leaf_name
    return companion_path, f"state/subagent-share/{sanitized_session_id}/{leaf_name}"


def _spill_blocks_to_companion(
    payload: Dict[str, Any], cwd: Optional[str], sidecar_path: str, injected_blocks: str
) -> Optional[str]:
    """Write `injected_blocks` to its companion file and return the
    resulting repo-relative path, or `None` on any failure. Callers wrap
    this in their own try/except (AC9's fail-open contract) -- this
    function itself does not swallow exceptions, so a caller can tell a
    resolution/write failure apart from "nothing to spill"."""
    resolved = _resolve_blocks_companion_path(payload, cwd, sidecar_path)
    if resolved is None:
        return None
    companion_path, rel_path = resolved
    with open(companion_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(injected_blocks)
    return rel_path


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

    blocks_leg_text = ("\n\n" + injected_blocks) if injected_blocks else ""

    parts: list[str] = []
    blocks_idx = -1
    if sidecar_text:
        parts.append(sidecar_text)
    if blocks_leg_text:
        blocks_idx = len(parts)
        parts.append(blocks_leg_text)
    if role_append:
        parts.append("\n\n" + role_append)

    # AC9 amendment -- threshold, not a switch: measure the composed TOTAL
    # (all three legs, the shape the harness actually sees) and spill the
    # blocks leg to a companion file only when that total would exceed the
    # cap. A type already under the cap keeps its blocks inline, byte-
    # identical to today (AC1).
    if injected_blocks and sum(len(p) for p in parts) > ADDITIONAL_CONTEXT_CHAR_CAP:
        try:
            blocks_rel_path = _spill_blocks_to_companion(
                payload, cwd, sidecar_path, injected_blocks
            )
        except Exception:
            blocks_rel_path = None
        if blocks_rel_path:
            parts[blocks_idx] = "\n\n" + _compose_blocks_pointer_text(blocks_rel_path)
        # A resolution/write failure falls back to the inline blocks leg
        # already sitting in `parts[blocks_idx]` -- today's behaviour, over
        # cap but never worse, never silent, never raised (AC9 fail-open).

    return "".join(parts)


@register_op(OP_NAME)
async def _handler(params: dict, repo_root=None) -> dict:
    """SubagentStart relay target (C3) -- registration only, no second hooks.json
    entry (module docstring, "no second registration"). The existing SubagentStart
    shim relays this op via `ipc.py :: dispatch_ops_from_hook` ALONGSIDE
    `hooks.track_dispatched_agents`, bookkeeping op FIRST.

    Order is load-bearing, not this handler's to enforce: `dispatch_ops_from_hook`
    is sequential by contract, and `resolve_effective_types` (called inside
    `compose_catering` -> `_resolve_sidecar_leg`) resolves a named dispatch's
    `subagent_type` through a back-pointer into `dispatched-agents.txt` -- the
    file the bookkeeping leg writes on this SAME event. Called before that write
    lands, a named teammate's back-pointer read is a lookup-miss (empty string,
    fail-open per `_read_backpointer_subagent_type`'s own contract) -- never an
    error, just a silently worse catering result. This handler assumes its
    caller places the bookkeeping op first, exactly as the module docstring and
    the owning plan's chunk ordering direct; it does not (and cannot, from
    inside a single relayed op) verify that its caller obeyed that ordering.

    `params` IS the SubagentStart payload verbatim -- the same shape
    `compose_catering` and `resolve_effective_types` already consume
    (`agent_type`, `agent_id`, `cwd`, `session_id`), not a re-shaped subset.

    Runs `compose_catering` off the event loop (`asyncio.to_thread`) --
    `_provision`'s sidecar write and the role-append snippet read are both
    synchronous file I/O, and this module's own async-handler-discipline peers
    (`hooks.track_dispatched_agents`) hold the same rule for write-shaped work.

    Returns a `context_only("SubagentStart", ...)` envelope when
    `compose_catering` has something to say, `no_advisory()` (empty dict) when
    it does not -- fail-open on every arm (AC5): `compose_catering` itself never
    raises, so there is nothing for this handler to catch.
    """
    import asyncio

    cwd = field(params, "cwd") or None
    text = await asyncio.to_thread(compose_catering, params, cwd=cwd)
    if not text:
        return no_advisory()
    return context_only("SubagentStart", text)
