"""
coordinator_core.hooks.cater_subagent_start -- engine-side catering
composer for a SubagentStart payload (C2,
docs/plans/2026-08-21-catering-rides-subagentstart.md).

DoE-claude's former PreToolUse(Agent) hook (`enforce-agent-dispatch-mode.py`)
used to compose its own catering legs for an `Agent`-tool child, but those
legs (including the named-teammate clause, `_compose_teammate_clause`) were
retired from that hook at `10cd4cda9` (2026-08-21, -1001 lines).
`SubagentStart` is now the sole catering path for every child, named or
unnamed, Agent-tool or Workflow `agent()` spawn alike. `docs/research/
spike-verdicts/2026-08-21-subagentstart-additionalcontext-caters-a-workflow-
spawn.md` proved `SubagentStart` `hookSpecificOutput.additionalContext` is a
working per-child injection channel on both shapes of spawn, carrying every
input the existing assembler/provisioner already take (`agent_type`, `cwd`,
`session_id`).

This module composes three legs, in an order load-bearing for how a child
reads the result:

    sidecar offer (report_sidecar) OR miss notice
      -> injected contract blocks
      -> role framing (LAST, unconditional, outside any roster lookup)

A named dispatch's teammate NAME is reachable on this path -- `payload
["agent_type"]` carries it directly, unlike the retired Agent-path hook's
`tool_input.name` gate, which never existed here. There is no fourth,
teammate-clause leg to port: the delivery instruction that clause used to
inject now lives resident in DoE's own agent definitions (`03d81fa41`)
rather than in any hook-injected prose.

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
never suppresses the other two -- each leg is independently wrapped. A
type that resolved and is genuinely not on `policy.report_sidecar` stays
silent for the sidecar leg -- the same "expected miss, no diagnostic"
posture `coordinator_core.dispatch.provision._log_unenumerated_sidecar_
miss` already takes for the sibling subagent-sidecar seam (this module
does not duplicate that diagnostic; it is a defence-in-depth net for a
governance gap this plan does not own). That silence is distinct from the
population that genuinely lost a sidecar, which gets the miss notice
instead (`_resolve_sidecar_leg`): a NAMED dispatch whose `subagent_type`
never resolved (its `agent_type` is always the teammate NAME, never a
policy key, so the roster lookup could never have succeeded for it
either way), or a dispatch for which no type resolved at all.

The `sidecar_path: <path>` and `sidecar_provisioning: missed` marker lines
are byte-identical to their Agent-path counterparts
(`_compose_sidecar_offer_text` / `_compose_sidecar_miss_text` in
`enforce-agent-dispatch-mode.py`) minus that hook's DoE-local
`_message_envelope.compose`/`render` prose-capping wrapper, which this repo
does not own or import -- the prose itself is carried verbatim so a
consuming agent's exact-line marker match (`coordinator/agents/
code-reviewer.md` § HARD RULE step 1) behaves identically on both paths.

CONTRACT CHANGE (2026-08-21, agreed with doe-claude-6d, bug-backlog
`2026-08-21-named-dispatch-catering-resolves-contrac-0755d38ec8ea.yaml`):
`payload["contract_blocks"]` now arrives in one of TWO shapes, and this
module resolves which shape it got before handing anything to
`assemble_contract_blocks_for_payload`:

  (a) a LIST of block-name strings -- an already-selected row, TODAY's
      shape, kept byte-identical for the pre-cutover compatibility window
      (DoE's shim has not cut over on every deploy at once). Passed
      through untouched.
  (b) a MAPPING of `agent_type -> [block names]` -- the new shape. DoE's
      shim still locates and parses `subagent-sandbox-policy.yaml` itself
      (`__file__`-anchored; that boundary does not move), but stops
      pre-selecting a row, because only THIS plane can resolve which type
      a dispatch actually is: a NAMED dispatch's `payload["agent_type"]`
      is the teammate NAME, not a policy key (`resolve_effective_types`
      below), so a caller selecting the row off the raw field silently
      picks nothing for that population. This module tries the raw
      `agent_type` key first (the common, unnamed-dispatch path, no
      back-pointer read needed), then the back-pointer-resolved
      `subagent_type` -- `_resolve_contract_blocks_payload` /
      `_select_contract_blocks_row`.

Anything else (`None`, a string, an int, any other malformed value) is
passed through unchanged -- `assemble_contract_blocks_for_payload`'s own
existing validation handles it exactly as before this change: `None`/falsy
degrades silently to no blocks, any other non-list is reported to stderr by
that function and also degrades to no blocks. This module does not
duplicate that check.

Negative-spec: this module does NOT resolve block NAMES out of
`subagent-sandbox-policy.yaml` itself (i.e. it never reads that YAML file)
-- that stays DoE-owned (`enforce-agent-dispatch-mode.py ::
_resolve_contract_blocks`, keyed off their own policy file). What moved to
this plane is only ROW SELECTION within the map DoE now hands over whole --
picking which `agent_type`'s list of names applies to this dispatch, not
deriving the list itself. `assemble_contract_blocks_for_payload` still owns
turning the selected list of names into rendered block text
(`_assemble_contract_blocks`), exactly as it always has for the list shape.

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

import hashlib
import os
import re
import secrets
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import blake2b
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core._settings_home import claude_config_dir, machine_local_dir
from coordinator_core.git.repo_root import show_toplevel as _show_toplevel_no_spawn
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op
from coordinator_core.session import scope as session_scope
from coordinator_core.subagent_sandbox.engine import (
    _NAMED_TEAMMATE_RE,
    load_policy,
    resolve_effective_types,
    resolve_git_root,
)

#: Permissive canonical-shape match for THIS predicate only -- deliberately
#: NOT `engine._TEAMMATE_CANONICAL_RE` (bug-backlog `2026-08-25-separator-
#: name-miss-marker-hole`). `engine`'s regex VALIDATES a well-formed
#: canonical id (`[A-Za-z0-9_.-]+@session-...`) for callers that need to
#: reject a malformed one; this predicate CLASSIFIES whether a dispatch is
#: a named one AT ALL, and a classifier that inherits a validator's name-
#: charset restriction goes silent on exactly the population it exists to
#: catch -- a teammate named with a separator the validator excludes
#: (`feature/auth-review@session-...`, `docs/api-check@session-...`) whose
#: `subagent_type` never resolved. Matching on the SEPARATOR shape alone
#: (`<non-empty name>@session-<suffix>`) subsumes the strict pattern, so
#: this predicate no longer needs `_TEAMMATE_CANONICAL_RE` at all -- it is
#: not imported here; `engine`'s own module keeps using it for its
#: distinct (validating) job.
_NAMED_TEAMMATE_CANONICAL_SHAPE_RE = re.compile(r"^.+@session-.+$")
from coordinator_core.subagent_sandbox.provision_report import (
    _exit_interview_section,
    _frontmatter,
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
#: text` -- AC5's partial-catering clause tests this exact line. Used ONLY
#: by the no-path miss body (`docs/plans/2026-08-25-a-missed-sidecar-
#: leaves-a-file-the-em-ca.md` AC3) -- the path-bearing miss body reuses
#: `SIDECAR_PATH_MARKER_PREFIX` instead, so a consumer keying off "sidecar_
#: path: " (the same key an ordinary offer uses) finds the sentinel too.
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

#: Prevalence counter (bug-backlog `2026-08-21-named-dispatch-catering-
#: resolves-contrac-0755d38ec8ea.yaml` deliverable 2) -- same machine-
#: readable "key: value" marker shape as `SIDECAR_PATH_MARKER_PREFIX` /
#: `SIDECAR_MISS_MARKER`, but this one is stderr-only diagnostic output,
#: never part of `additionalContext` (a spawned child must never see it).
#: Fires exactly once per dispatch where `payload["contract_blocks"]` is
#: the mapping shape AND row selection needed the back-pointer leg -- the
#: population that would have composed silently empty under the list-only
#: contract this module had before the fix.
NAMED_DISPATCH_ROW_RESOLVED_MARKER = "named_dispatch_contract_blocks_resolved: 1"


#: Snippet path, relative to the coordinator-claude plugin root, in either
#: of its two known on-disk shapes (see `_resolve_role_append_snippet_path`).
_ROLE_APPEND_RELATIVE = ("snippets", "agent-role-dispatched.md")

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


def _compose_sidecar_miss_text(sentinel_path: str = "", *, is_named: bool = False) -> str:
    """Parameterized over two authored bodies (`docs/plans/2026-08-25-a-
    missed-sidecar-leaves-a-file-the-em-ca.md` AC3), replacing the retired
    single-body `enforce-agent-dispatch-mode.py ::
    _compose_sidecar_miss_text` port -- an eligible type whose provisioning
    came back empty is TOLD, never left to read silence as ineligibility
    (AC5's partial-catering clause).

    `sentinel_path` non-empty (`_resolve_sidecar_leg` wrote a sentinel, or
    reused an existing one): tell the child the sentinel exists and name
    its literal path via `SIDECAR_PATH_MARKER_PREFIX`, same marker shape an
    ordinary sidecar offer uses. `sentinel_path` empty (no sentinel is
    possible or the write itself failed): the no-path body, which never
    tells the child to derive or scaffold its own path -- that instruction
    (`the path your agent definition names`) is retired for both bodies;
    the string is gone by construction (AC3)."""
    if sentinel_path:
        return (
            "\n\nSidecar provisioning did not complete for this dispatch, "
            "but a sentinel scaffold was written for you -- persist your "
            "findings there as normal, and say in them that provisioning "
            "missed.\n" + SIDECAR_PATH_MARKER_PREFIX + sentinel_path
        )
    if is_named:
        # A NAMED teammate's final assistant text is NOT returned to the
        # dispatcher -- delivery is an explicit `SendMessage`, per that
        # tool's own contract ("Your plain text output is NOT visible to
        # other agents"). The unnamed body below tells the child to report
        # "inline in your reply", which for this population names the one
        # channel that goes nowhere: six named `general-purpose` agents
        # dispatched 2026-08-26 each wrote a complete 7-13K report, ended
        # `stop_reason: end_turn`, surfaced to the EM as a bare idle notice,
        # and were recovered only by reading their transcripts off disk.
        # They followed this instruction exactly.
        return (
            "\n\nSidecar provisioning did not complete for this dispatch -- "
            "no scaffold exists on disk, and you are a NAMED teammate, whose "
            "final reply text is not returned to the dispatcher. Deliver your "
            "findings with SendMessage to the session that dispatched you, "
            "and say in them that provisioning missed. Ending your turn "
            "without sending delivers nothing.\n"
            + SIDECAR_MISS_MARKER
        )
    return (
        "\n\nSidecar provisioning did not complete for this dispatch -- no "
        "scaffold exists on disk. Report your findings inline in your "
        "reply and say in them that provisioning missed.\n"
        + SIDECAR_MISS_MARKER
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
    # `repo_root.show_toplevel` walks and never spawns -- the same repoint C2
    # made on the main compose path, applied here too. This leg was MISSED by
    # that pass and only fires on the over-cap companion spill, so a probe on
    # an under-cap agent type (code-reviewer) reads zero spawns while an
    # over-cap one (staff-eng) still pays a `git rev-parse`. Eligibility is the
    # same as C2's: this resolver feeding a lookup whose wrong answer is a
    # MISS, never a wrong verdict -- the function returns None and the caller
    # falls back to inline blocks.
    git_root = _show_toplevel_no_spawn(cwd)
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
    """Locate `snippets/agent-role-dispatched.md` under the coordinator-claude
    plugin's CONTENT root -- never a hardcoded plugin path. This module lives
    in claude-klabauter, a different on-disk checkout than the coordinator-claude
    plugin the snippet ships in, so the Agent-path hook's own
    `Path(__file__).resolve().parents[2]` colocated resolution (that hook
    lives INSIDE the plugin it reads from) has nothing to anchor against
    here.

    Mirrors `provision_report.resolve_plugin_root()`'s (C4) fleet-fixing
    THIRD rung ADDED to the two shapes this function already probed --
    `<claude_config_dir>/plugins/coordinator-claude` in both known shapes
    (DoE dev-clone nested under `coordinator/`, marketplace/OSS-mirror at
    that root directly), then the fleet's own `.doe-root` pointer file (the
    rung this defect's fleet-box case needs: a plugin root whose live clone
    sits OUTSIDE `.claude` entirely, where `<claude_config_dir>/plugins/
    coordinator-claude` holds only `coordinator/bin`). Does NOT add
    `resolve_plugin_root`'s own leading `CLAUDE_PLUGIN_ROOT` env-var rung --
    that rung is scoped to the contract-blocks leg's own test fixtures
    (`git_repo`, `coordinator_core/hooks/tests/test_cater_subagent_start_
    named_dispatch.py`), which legitimately point it at a snippets root
    that carries contract-block snippets but not this artifact; consulting
    it here would wrongly prefer that root over one that actually has
    `agent-role-dispatched.md`. AC6 ("0 additional process spawns", this
    module's own budget test) additionally treats a bare `import os` as a
    spawn-shaped signature regardless of use, ruling that rung out on this
    leg on process-budget grounds too.

    Does NOT delegate to `resolve_plugin_root()` itself, and does not
    duplicate its `_has_content` probe (`snippets/` existing as a
    directory): that function is shared with the contract-blocks leg, whose
    own test fixtures legitimately point `CLAUDE_PLUGIN_ROOT` at a
    snippets directory that holds contract-block snippets but NOT
    `agent-role-dispatched.md` -- stopping at the first rung with a
    `snippets/` dir (as `resolve_plugin_root` does) would wrongly short-
    circuit before reaching the rung that actually has this artifact. Kept
    (module's own docstring, and this function's own prior version):
    probe for THIS function's own artifact directly at every rung, not a
    directory's mere existence standing in as a proxy for it.

    Returns ``None`` on no match at any rung; the caller fails open to ""
    (Concern D's role-framing leg simply does not fire).
    """

    def _artifact_at(root: Path) -> Optional[Path]:
        candidate = root.joinpath(*_ROLE_APPEND_RELATIVE)
        return candidate if candidate.is_file() else None

    plugin_base = claude_config_dir() / "plugins" / "coordinator-claude"
    for candidate_root in (plugin_base / "coordinator", plugin_base):
        found = _artifact_at(candidate_root)
        if found is not None:
            return found

    try:
        pointer = machine_local_dir() / ".doe-root"
        doe_root = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        doe_root = ""
    if doe_root:
        found = _artifact_at(Path(doe_root) / "coordinator")
        if found is not None:
            return found

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


def _select_contract_blocks_row(
    contract_blocks: Mapping, agent_type: str, subagent_type: str
) -> Tuple[Optional[List[str]], Optional[str], bool]:
    """Select `agent_type`'s (or, failing that, `subagent_type`'s) row out
    of a mapping-shaped `payload["contract_blocks"]`. Returns
    ``(selected_or_None, matched_key_or_None, via_backpointer)``.

    Order matters (module docstring): `agent_type` first -- the common,
    unnamed-dispatch path, no back-pointer semantics involved -- then
    `subagent_type`, the back-pointer-resolved leg that only a NAMED
    dispatch needs. `via_backpointer` is True only when the SECOND leg is
    what matched -- exactly the population the prevalence counter
    (`NAMED_DISPATCH_ROW_RESOLVED_MARKER`) exists to measure. Neither key
    present is a lookup-miss, not an error: several catered types
    (`coordinator:git-commit-agent`, `coordinator:atlas-clarity-reviewer`)
    legitimately carry no `contract_blocks` row at all.
    """
    if agent_type and agent_type in contract_blocks:
        return contract_blocks.get(agent_type), agent_type, False
    if subagent_type and subagent_type in contract_blocks:
        return contract_blocks.get(subagent_type), subagent_type, True
    return None, None, False


def _resolve_contract_blocks_payload(
    payload: Dict[str, Any], agent_type: str, subagent_type: str
) -> Dict[str, Any]:
    """Return `payload`, or a shallow-copied patched version of it, ready
    for `assemble_contract_blocks_for_payload` to consume.

    Only the MAPPING shape is patched (module docstring, deliverable 1):
    `payload["contract_blocks"]` is replaced with the selected row's list
    of block names, so the downstream assembler sees exactly the shape it
    has always consumed. The LIST shape and every other value (`None`, a
    string, an int, ...) pass through byte-identical -- this function
    never touches a payload it did not itself recognize as a mapping.

    Emits the prevalence counter (deliverable 4) when row selection needed
    the back-pointer leg, and a stderr diagnostic (deliverable 3) ONLY for
    the genuinely-anomalous case: a row that IS present in the map but
    resolves to an empty/falsy list. A type absent from the map entirely,
    with no back-pointer type either, is the legitimate "no row for this
    type" case and stays silent, matching the sidecar leg's own posture
    toward an unenumerated type.
    """
    raw = payload.get("contract_blocks")
    if not isinstance(raw, Mapping):
        return payload

    selected, matched_key, via_backpointer = _select_contract_blocks_row(
        raw, agent_type, subagent_type
    )
    if via_backpointer:
        print(NAMED_DISPATCH_ROW_RESOLVED_MARKER, file=sys.stderr)
    if matched_key is not None and not selected:
        print(
            "cater_subagent_start: contract_blocks row present but empty "
            f"for {matched_key!r}",
            file=sys.stderr,
        )

    patched = dict(payload)
    patched["contract_blocks"] = selected if selected else []
    return patched


def _is_named_teammate_agent_id(agent_id: str) -> bool:
    """True iff `agent_id` is a named-teammate id, in either shape
    `resolve_effective_types` can hand back -- the EM-side canonical
    `<name>@session-<short>` form, or the subagent-side raw
    `a<name>-<16hex>` form (the Staff Engineer F4 fallback, when `_canonical_
    agent_id` could not canonicalize because `session_id` was absent or
    too short).

    AC2 of the governing plan says not to mint a third spelling of this
    predicate, and for the subagent-side shape that still holds --
    `_NAMED_TEAMMATE_RE` is reused verbatim below. The canonical-shape arm
    diverges deliberately (bug-backlog `2026-08-25-separator-name-miss-
    marker-hole`): `engine._TEAMMATE_CANONICAL_RE` VALIDATES a well-formed
    canonical id for callers that must reject a malformed one, but this
    predicate's job is to CLASSIFY whether a dispatch is named AT ALL --
    a classifier that inherits a validator's name-charset restriction goes
    silent on exactly the malformed-but-real inputs it most needs to flag.
    `_canonical_agent_id` genuinely mints ids like
    `feature/auth-review@session-11111111` for a teammate named with a
    `/`, and such a dispatch whose `subagent_type` never resolved must
    still hit the roster-miss branch below, not go silent. So this arm
    matches permissively on the SEPARATOR shape
    (`_NAMED_TEAMMATE_CANONICAL_SHAPE_RE`, `<non-empty name>@session-
    <suffix>`) rather than `engine`'s stricter charset -- that shape
    subsumes the strict pattern, so this is still only two arms, not a
    third spelling: an unnamed dispatch's bare-hex id (no `@`) matches
    neither."""
    return bool(
        _NAMED_TEAMMATE_CANONICAL_SHAPE_RE.fullmatch(agent_id)
        or _NAMED_TEAMMATE_RE.fullmatch(agent_id)
    )


def _compute_sentinel_leaf(agent_id: str) -> Optional[str]:
    """Collision-free-by-construction leaf for the miss-leg sentinel (AC4).

    `_sanitize_segment` MANGLES rather than rejects (retracted-premise
    section, governing plan): it strips `[^A-Za-z0-9._@-]` and returns
    `None` only for the degenerate empty/dot/dotdot results, so sanitizing
    ALONE is non-injective -- two distinct teammate names can mangle onto
    one sanitized stem, and an idempotent-hit write would then have the
    second dispatch silently reuse the first's sentinel (report
    misattribution). The digest is over the RAW `agent_id`, before
    sanitization, so two names that mangle identically still diverge in
    their digest and get distinct leaves. Deterministic and spawn-free --
    derivable by an EM from `(teammate name, session short id)` alone,
    with no on-disk lookup.

    Returns `None` when `agent_id` sanitizes to a degenerate result (should
    not occur for a well-formed canonical id, but the caller must not write
    a path-less sentinel either way)."""
    sanitized = _sanitize_segment(agent_id)
    if sanitized is None:
        return None
    digest = blake2b(agent_id.encode("utf-8"), digest_size=4).hexdigest()
    return f"{sanitized}-{digest}"


def _write_miss_sentinel(
    payload: Dict[str, Any], cwd: Optional[str], agent_id: str, agent_type: str
) -> str:
    """Write (or idempotently reuse) the miss-leg sentinel scaffold for a
    named dispatch that lost its report sidecar to the provisioning race
    (`docs/plans/2026-08-25-a-missed-sidecar-leaves-a-file-the-em-ca.md`
    AC1/AC4/AC5/AC7). Returns the sentinel's repo-relative path, or `""` on
    any failure or when no sentinel is derivable.

    The sentinel is never child-created (that plan's own anti-scope) --
    claude-klabauter writes it eagerly, before the child's first tool call, so an EM
    polling the derivable path never has to distinguish not-yet-written
    from never-provisioned.

    FAIL-OPEN (AC7): the try/except lives HERE, around the write, not in
    `compose_catering`'s outer wrap -- that wrap zeroes both `_resolve_
    sidecar_leg` return values on an exception, which would drop the miss
    marker text entirely, strictly worse than today.

    ATOMICITY (anti-scope): write to a `.tmp-<nonce>` sibling and
    `os.replace` onto the target, never bare `open(path, "x")` then a
    separate `write()` -- a hook killed between those two steps would leave
    a zero-byte sentinel with no frontmatter key, which an existence-only
    idempotent-hit check would then reuse forever. An existing file at the
    target path IS treated as an idempotent hit (no second write, no
    touch-claim) -- mirrors `_provision`'s own idempotent-hit contract for
    a re-fired dispatch against the same derived leaf.

    SCAFFOLD (AC5): stamps `provisioning: missed` and `scaffold_sha256:
    <hex over the body-below-frontmatter>` into the frontmatter emitted by
    `_frontmatter` (reused, not reimplemented) -- a file present but
    lacking the declared `scaffold_sha256` key is a corrupt/foreign
    scaffold, not a byte-identical-to-template baseline that would drift
    with the template.

    TOUCH-CLAIM: mirrors `_provision`/`_provision_plan_derivable_doc` --
    `session_scope.touch_written_path` fires only on the branch that
    actually wrote bytes, never on an idempotent hit.
    """
    try:
        leaf = _compute_sentinel_leaf(agent_id)
        if leaf is None:
            return ""

        # Non-spawning root read, matching this module's own contract
        # ("In-process only -- no subprocess", module docstring) and the
        # sibling legs at `compose_catering`/`assemble_contract_blocks_for_
        # payload`. `resolve_git_root` shells out to `git rev-parse
        # --show-toplevel`; the race arm never reached `_provision`, so it
        # carried NO spawn before this leg existed, and reintroducing one
        # here would break the plan's own zero-added-spawn criterion on the
        # SubagentStart hot path. A wrong answer here only means "this
        # lookup missed" -- the leg fails open to the no-path marker, never
        # to a wrong verdict -- which is exactly `resolve_git_root_cheap`'s
        # stated rule for who may use the cheap read.
        git_root = _show_toplevel_no_spawn(cwd)
        if not git_root:
            return ""

        session_id = payload.get("session_id") or None
        if not session_id:
            return ""
        sanitized_session_id = _sanitize_segment(str(session_id))
        if sanitized_session_id is None:
            return ""

        session_dir = Path(git_root) / "state" / "subagent-share" / sanitized_session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        doc_path = session_dir / f"{leaf}.md"
        rel_path = f"state/subagent-share/{sanitized_session_id}/{leaf}.md"

        if doc_path.exists():
            # Idempotent hit: a re-fired dispatch against the same derived
            # leaf reuses the existing sentinel rather than clobbering it.
            # No touch-claim here -- this call did not write the bytes.
            return rel_path

        body = "## Run notes\n\n## Observations\n\n" + _exit_interview_section()
        scaffold_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

        spawned_at = datetime.now(timezone.utc).isoformat()
        frontmatter = _frontmatter(agent_type, spawned_at, str(session_id))
        closing_marker = "---\n\n"
        assert frontmatter.endswith(closing_marker)
        frontmatter = (
            frontmatter[: -len(closing_marker)]
            + "provisioning: missed\n"
            + f"scaffold_sha256: {scaffold_sha256}\n"
            + closing_marker
        )
        doc_text = frontmatter + body

        tmp_path = session_dir / f"{leaf}.tmp-{secrets.token_hex(4)}.md"
        with open(tmp_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(doc_text)
        os.replace(tmp_path, doc_path)

        # SUBSUME: touch-claim (docs/plans/2026-08-05-in-process-writers-
        # declare-their-writes.md C2), same rationale as `_provision`'s own
        # create branch -- `session_id` here is the RAW payload["session_id"]
        # (the dispatching session).
        session_scope.touch_written_path(str(session_id), rel_path, git_root)

        return rel_path
    except Exception:
        return ""


def _resolve_sidecar_leg(
    payload: Dict[str, Any],
    cwd: Optional[str],
    agent_id: str,
    agent_type: str,
    subagent_type: str,
) -> tuple[str, str]:
    """Resolve the sidecar-offer/miss-notice leg. Returns
    ``(sidecar_path, sidecar_text)``.

    Separates two silences that a single "no type is on the roster" branch
    used to conflate (bug-backlog `2026-08-25-sidecar-provisioning-missed-
    never-fires-f49eb749c024.yaml`; plan `docs/plans/2026-08-25-a-named-
    dispatch-keeps-its-report.md` AC5):

      - a NAMED dispatch (`agent_id` in either named-teammate shape --
        `_is_named_teammate_agent_id`) whose `subagent_type` did NOT
        resolve (the back-pointer row was not yet written, or was never
        written) emits the miss notice. THIS is the population that
        genuinely lost a sidecar: for a named dispatch, `agent_type` is
        always the teammate NAME (never a `report_sidecar` policy key --
        `resolve_effective_types`'s own contract), so the roster lookup
        below is structurally incapable of finding it there; only the
        back-pointer-resolved `subagent_type` could ever have matched, and
        it did not. `agent_id` in the EM-derivable canonical `<name>@
        session-<short>` shape ALSO gets a sentinel written eagerly here
        (`_write_miss_sentinel`, `docs/plans/2026-08-25-a-missed-sidecar-
        leaves-a-file-the-em-ca.md`), and the miss notice names its literal
        path; the raw `a<name>-<16hex>` fallback shape gets no sentinel
        (its hex is not EM-derivable) and takes the no-path body instead.
      - NO type resolved at all (`agent_type` and `subagent_type` both
        falsy) ALSO emits the miss notice -- the resolver-exception arm
        (`compose_catering`'s own `except` catching a `resolve_effective_
        types` raise) and any other payload shape that yields empty legs
        for both. `agent_id` is empty in this arm, so no sentinel is
        possible; always the no-path body.
      - an UNNAMED dispatch (bare-hex or unresolvable `agent_id`) whose
        `agent_type` resolved but is genuinely absent from `policy.
        report_sidecar` stays SILENT (unchanged) -- `compose_catering`
        runs for every SubagentStart, so this is the majority population,
        and it matches the Agent-path hook's own `_is_report_sidecar_
        eligible` gate on the miss notice verbatim. Firing the marker here
        would broadcast a miss instruction to types whose definitions were
        never catered a sidecar at all.
      - an eligible type whose `_provision` came back empty also emits the
        miss notice (unchanged) -- an eligible dispatch is told, never
        left to read silence as ineligibility. Same canonical-shape gate
        as above applies: a sentinel is written and named only when
        `agent_id` is in the EM-derivable canonical shape.

    `agent_id`/`agent_type`/`subagent_type` arrive pre-resolved from the
    caller (`compose_catering`) rather than re-derived here --
    `resolve_effective_types`'s back-pointer read is in-process file I/O,
    not a process spawn, but this module still resolves it exactly once
    per dispatch (brightline: "add no second one") now that the
    contract-blocks leg needs `agent_type`/`subagent_type` too.

    `policy_path=None` on both `load_policy` and `_provision` deliberately
    reuses `load_policy`'s own existing 3-rung fallback (explicit path ->
    `SUBAGENT_SANDBOX_POLICY` env var -> `CLAUDE_PLUGIN_ROOT`-relative
    default) rather than re-deriving a policy-path resolver here -- unlike
    the role-framing snippet, `load_policy` already has a working default
    leg (see that function's own docstring), so a second bespoke resolver
    would duplicate logic that already exists, exactly the shape this
    module's own docstring says to avoid.
    """
    if not agent_type and not subagent_type:
        # Resolver-exception arm: `agent_id` is empty here (AC3), so no
        # sentinel is possible -- the no-path body, unconditionally.
        return "", _compose_sidecar_miss_text()

    if not subagent_type and agent_id and _is_named_teammate_agent_id(agent_id):
        # SHAPE GATE (AC2): sentinel keys ONLY on the canonical
        # `<name>@session-<short>` shape. The raw `a<name>-<16hex>`
        # fallback carries 16 hex digits no EM can derive, so a sentinel
        # there would be unpollable by construction -- write none, take
        # the no-path body.
        sentinel_path = ""
        if _NAMED_TEAMMATE_CANONICAL_SHAPE_RE.fullmatch(agent_id):
            sentinel_path = _write_miss_sentinel(payload, cwd, agent_id, agent_type)
        return sentinel_path, _compose_sidecar_miss_text(sentinel_path, is_named=True)

    policy = load_policy(None)

    sidecar_eligible = bool(
        (agent_type and agent_type in policy.report_sidecar)
        or (subagent_type and subagent_type in policy.report_sidecar)
    )
    if not sidecar_eligible:
        return "", ""

    sidecar_path = _provision(payload, None, cwd) or ""
    if sidecar_path:
        return sidecar_path, _compose_sidecar_offer_text(sidecar_path)

    # Same shape gate as above: an eligible type whose `_provision` came
    # back empty still only gets a sentinel when `agent_id` is in the
    # EM-derivable canonical shape.
    sentinel_path = ""
    if agent_id and _NAMED_TEAMMATE_CANONICAL_SHAPE_RE.fullmatch(agent_id):
        sentinel_path = _write_miss_sentinel(payload, cwd, agent_id, agent_type)
    return sentinel_path, _compose_sidecar_miss_text(
        sentinel_path, is_named=bool(agent_id and _is_named_teammate_agent_id(agent_id))
    )


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

    agent_id = ""
    agent_type = ""
    subagent_type = ""
    try:
        # Non-spawning root read (C2, state/dispatch-briefs/2026-08-21-
        # catering-costs-what-the-work-costs/C2.md): eligible per
        # `resolve_git_root_cheap`'s own stated rule -- every leg fed by
        # `agent_type`/`subagent_type` below fails open to "" on a miss, so a
        # wrong/absent root here only means "this lookup missed", never a
        # wrong VERDICT. `repo_root.show_toplevel` walks and never spawns.
        git_root = _show_toplevel_no_spawn(cwd)
        agent_id, agent_type, subagent_type = resolve_effective_types(payload, git_root)
    except Exception:
        agent_id, agent_type, subagent_type = "", "", ""

    sidecar_path = ""
    sidecar_text = ""
    try:
        sidecar_path, sidecar_text = _resolve_sidecar_leg(
            payload, cwd, agent_id, agent_type, subagent_type
        )
    except Exception:
        sidecar_path, sidecar_text = "", ""

    injected_blocks = ""
    try:
        resolved_payload = _resolve_contract_blocks_payload(
            payload, agent_type, subagent_type
        )
        injected_blocks = (
            assemble_contract_blocks_for_payload(
                resolved_payload, cwd=cwd, report_sidecar_path=sidecar_path
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
