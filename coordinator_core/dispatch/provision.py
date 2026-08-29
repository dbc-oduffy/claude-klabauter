"""
coordinator_core.dispatch.provision -- agent-side subagent-sidecar
decision-object provisioner (W2-B3, canonical-resolution-engine Wave 2).

The dispatch brief is the decision-object contract seen from the INPUT end:
the engine provisions an EMPTY sidecar (a decision-object container), injects
its path + citizenship into the spawn payload, pre-populates the exit-
interview section, and the dispatched agent fills it in and returns a
pointer. This module is the spawn-time front-end that does that
provisioning -- the sibling seam to ``coordinator-doc-new --type
subagent-sidecar`` (the engine/dispatch-tooling-side scaffolder that emits
the SAME document shape for manual/test invocation).

Deliberately mirrors ``coordinator_core.subagent_sandbox.provision_report``
(the EXISTING spawn-time run-report provisioner) rather than re-deriving
session/path resolution: this module imports ``resolve_effective_types``,
``load_policy``, ``resolve_git_root`` verbatim from
``coordinator_core.subagent_sandbox.engine``, and reuses
``provision_report._sanitize_segment`` for the same single-path-segment
whitelist discipline (whitelist ``[A-Za-z0-9._-]``, reject the degenerate
``.``/``..``/empty results, never ``Path.resolve()``). Eligibility is
determined the same way ``provision_report`` determines run-report
eligibility -- via ``Policy.report_sidecar`` set membership -- because DR-058
left exactly one surviving policy field and this module does not invent a
second one; a dedicated ``subagent-sidecar``-specific policy key (if the
DoE-owned ``coordinator/subagent-sandbox-policy.yaml`` schema needs one) is a
DoE-side follow-up, not authored here (this repo is engine-only for that
policy file).

Sidecar shape (schema-of-record: DoE's ``schemas/decision-object.schema.json``
``$defs/subagent_sidecar``): dispatch frontmatter (plan/chunk/dispatched_at/
dispatched_by/status/agent_type/spawned_at/commits/sidecar_schema -- the same
shape ``coordinator-doc-new``'s ``_scaffold_subagent_sidecar`` emits) plus the
three decision-object fields:
  completion_status    -- durable, queryable "task done" marker; backlinks the
                           EXISTING query-completions records surface (claude-klabauter
                           work-state emission) -- NOT a fourth store.
  divergence_from_plan  -- block-style nested mapping (diverged/summary/detail).
                           Untrusted narrative -- never re-read as a directive
                           by any automated consumer.
  tell_the_EM           -- freeform exit-interview channel, a body section
                           (not frontmatter) so it can carry arbitrary prose.

Confinement preservation (AC-9, security-load-bearing): this module grants a
write TARGET, never a write CAPABILITY the calling agent lacked. It writes
exactly one file at spawn time, under its own sandboxed session directory,
via the same sanitize-before-path-touch discipline provision_report.py
uses -- it does not touch, read, or reference
``coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist`` (the
shipped code-reviewer Bash-command allowlist) or widen any tool grant. A
read-only-on-plan generic executor stays read-only-on-plan; the sidecar is
its ONLY structured write-back, exactly as it is for run-report today. See
``coordinator_core/dispatch/tests/test_provision.py`` for the assertion that
the confinement guard's allowlist constants are untouched by this module's
existence.

Class-asymmetric behavior (R7 Addendum) is a property of the DISPATCHING
side's tool grant, not of this module's write logic: generic executors are
read-only-on-plan (the sidecar is their ONLY structured write-back); named
Opus personas are offered-not-imposed (may also edit elsewhere, sidecar
offered for the durable record). This module provisions the identical
container shape for both classes -- the asymmetry is enforced upstream by
whichever tool-grant mechanism spawns the agent, never encoded as a field
here.

Fail-open everywhere: any parse failure, ineligible type, missing
session_id, or unexpected exception yields no sidecar path -- this module
must never brick a spawn. Mirrors provision_report.py's fail-open contract
verbatim.

AC7 (docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md
§ C4) -- `subagent-sandbox-policy.yaml` carries FOUR exact-string catering
maps keyed on `subagent_type`, all sharing this same fail-open lookup-miss
posture. This module closes the miss for exactly the one map with a
Claude-klabauter-side engine consumer it can reach directly:
  - `report_sidecar` (used above) -- a miss's SILENCE is closed by
    `_log_unenumerated_sidecar_miss`: an unenumerated-type miss now logs a
    stderr diagnostic (never flips the fail-open verdict), while an
    on-roster-but-not-opted-in miss (e.g. `Explore`) stays silent exactly as
    before.
  - `report_type_map` -- additive on top of `report_sidecar`; selects which
    `provision_report --type` template an eligible type gets. Moot for an
    unenumerated type once `report_sidecar` itself misses (no sidecar to
    template) -- no separate fix needed here.
  - `contract_blocks` -- resolves which snippet blocks (`do-not-commit`,
    `guard-encounter-preamble`, `quota-self-detect-preamble`) get injected
    into a dispatch prompt; a miss means NO injection at all, the sharpest
    instance of "less governed AND less instructed" this plan names. This
    map lives in DoE-owned `coordinator/subagent-sandbox-policy.yaml` and is
    read by a DoE-side doctrine hook on the Agent-tool spawn path -- outside
    this module's write surface. C1's `PreToolUse(Agent)` deny already
    blocks the spawn before this lookup runs, so with C1 in place this miss
    is unreachable in practice; the actual fix (closing the miss so an
    unenumerated type gets SOME contract-block set, or is denied before
    injection is consulted) stays DoE's to make.
    RULED 2026-08-10 (DoE `DR-151`, commit `8592f0592`): the fail-open miss
    STAYS -- these maps are catering, not governance, so failing closed
    would either veto an EM's dispatch or force catering nobody chose. The
    governance half closes at C1's roster deny guard instead. The same pass
    fixed a real DoE-side defect: `enforce-agent-dispatch-mode.py` resolved
    `contract_blocks` INSIDE its `report_sidecar`-eligibility branch,
    collapsing a decoupling their own policy header mandates -- so
    `coordinator:atlassian-worker` / `coordinator:drive-worker` silently got
    no blocks. This module's half already honours the split (see
    provision_report.py's independent `if`s + per-leg try/except); no
    claude-klabauter-side change is owed.
  - `dispatch_tier` -- classifies `exploration` vs `review-execution` tier.
    CORRECTED 2026-08-10 (DoE reply to the C6 memo): this map has NO runtime
    consumer at all -- not a documented-fallback gap, and not downstream of
    anything. Its only readers are `coordinator/tests/test_dispatch_tier.py`
    and prose, and those tests already enforce total coverage of every
    declared agent. There is no runtime lookup, so there is no runtime miss;
    it is out of the fail-open question entirely. Note also:
    `coordinator/agents/agent-effort-registry.yaml` (model/effort policy)
    has NO runtime consumer at all as of this plan, so there is no
    model/effort policy for an invented agent type by construction -- this
    bounds what this plan can fix.

Negative-spec: this module does NOT duplicate the harness's raw JSONL
transcript (AC-10) -- the container it writes is the CURATED decision-object
record (completion_status + divergence_from_plan + tell_the_EM), never a
replay log.
Negative-spec: does NOT re-implement DR-058's removed confined/exempt/
sanctioned_dirs enforcement grammar -- that grammar is dead (see
``coordinator_core/subagent_sandbox/CONTRACT.md``); this module builds only
against the SURVIVING resolver/policy-load halves.
Negative-spec: does NOT edit ``coordinator/subagent-sandbox-policy.yaml`` --
that policy file is DoE-owned; a policy opt-in entry for ``subagent-sidecar``
eligibility (if the shared ``report_sidecar`` key is judged insufficient) is
flagged as a DoE-side follow-up, not authored here.

Spec backlink: DoE-claude:pln-canonical-resolution-engine-6eea37 § W2-B3, R7 Addendum
Sibling seam: coordinator_core/subagent_sandbox/provision_report.py (run-report provisioner)
Schema-of-record: schemas/decision-object.schema.json $defs/subagent_sidecar (DoE clone)

Anonymous-provision_key disambiguation (2026-08-15 break-class fix): a caller
that omits ``provision_key`` used to get a fully-random nonce-named sidecar
(``<label>-sidecar-<nonce>.md``) -- fine for uniqueness, useless for
identity. Three concurrent same-``agent_type`` dispatches in one session
(the incident this closes) produced three indistinguishable-looking open
sidecars; a reviewer that found two identically-shaped empty siblings plus
one already filled by a peer had no principled way to tell which was its
own and correctly refused to write rather than guess. Two shapes were
weighed:

  1. Always derive a ``provision_key`` when the caller supplies none --
     CHOSEN. See ``provision_subagent_sidecar``'s ``elif agent_id:`` branch:
     the derived key is ``<effective_label>.<agent_id>``, where
     ``agent_id`` is the SAME already-canonicalized identity
     ``resolve_effective_types`` produces (unique per spawned agent within a
     session by construction -- it is the same value Bash-call attribution
     elsewhere in this tree relies on for uniqueness). Derivation is gated
     on ``agent_id`` surviving ``_sanitize_segment`` unchanged (the
     named-teammate form ``a.+-[a-f0-9]{16}`` is not filesystem-safe by
     construction -- only the bare-hex form is); an ``agent_id`` that does
     not falls through to the random-nonce path instead of being derived
     from, so no two accepted (sanitize-stable) ``agent_id``s can ever
     collapse onto the same key. This makes collision across concurrent
     same-type dispatches impossible within one session for every
     ``agent_id`` this branch actually derives from, without inventing a
     new identity source.
  2. Detect the ambiguity and fail loud at provisioning time (refuse a
     second anonymous same-``agent_type`` provision, name ``provision_key``
     as the fix) -- REJECTED. This only turns a silent hazard into a loud
     one; it still requires a human/dispatcher to remember to pass
     ``provision_key`` next time, which is the exact "the operator
     remembers" failure shape this repo's north star (see CLAUDE.md) rejects
     as a discharging artifact. It would also newly brick concurrent same-
     type dispatch outright until someone updates the dispatcher, which is a
     regression in availability for a purely a cosmetic-naming gap.

Idempotence tradeoff (named per the spec's ask): an explicit caller-supplied
``provision_key`` keeps its exact existing path and idempotent re-open
behavior -- this fix does not touch that branch at all. The DERIVED-key
branch trades idempotence across a genuine re-dispatch of "the same logical
unit" under a fresh spawn: a brand-new ``Task`` spawn always mints a fresh
``agent_id``, so a deliberate retry of the same logical unit lands a SECOND
sidecar rather than reopening the first. This is accepted because nothing on
this call's inputs distinguishes "retry the same logical unit" from
"dispatch a new, unrelated unit of the same type" without a caller-supplied
key -- guessing wrong there would silently collide two UNRELATED agents'
sidecars, which is the exact hazard this fix exists to close. A caller that
wants idempotent re-dispatch across spawns already has the tool: pass an
explicit ``provision_key`` (e.g. a plan/chunk slug), which is unaffected by
and unrelated to this change.

Brief surface (not built here): the derived/explicit path must still reach
the DISPATCHED agent's own prompt to close the incident's second half (an
agent that can't see its own sidecar path is still guessing). This module
only emits ``{"subagent_sidecar": <path>}`` to stdout at a PreToolUse-Agent
hook boundary; folding that value into the spawned agent's brief is done by
whichever dispatching skill/command constructs the ``Task`` prompt --
coordinator-claude's surface, not claude-klabauter's (see
``state/handoffs/2026-08-03-sidecar-emitter-contract.md``'s "DoE owns the
VALUES -- which dispatching skill/command passes ... " precedent for the
identical mechanism/values split). No half-bridge is built here; STOPPED per
the dispatch brief's own instruction.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from coordinator_core.session import scope as session_scope
from coordinator_core.subagent_sandbox.engine import (
    load_policy,
    resolve_effective_types,
    resolve_git_root,
)
from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment
from coordinator_core.subagent_sandbox.provision_report import (
    _read_sidecar_pointer,
    _recover_orphaned_sidecar,
    _write_sidecar_pointer,
)

#: Producer namespace for the sidecar pointer index (see
#: `provision_report._sidecar_pointer_path`). Distinct from that module's own
#: default because the two write different leaf suffixes for the same agent.
_POINTER_KIND = "dispatch"

# DEFERRED, NOT a module-level import (2026-08-13 hot-path import-budget fix,
# fourth site of the same defect shape as coordinator_core/bash_guards/
# _helpers.py, coordinator_core/bash_guards/block_subagent_plan_body_bash_
# write.py, and coordinator_core/write_guards/block_subagent_plan_body_write.py):
# a module-level `from coordinator_core.hooks.block_unenumerated_agent_type
# import resolve_roster` drags in `coordinator_core.hooks`'s package
# `__init__`'s full eager registration (18 submodules) on every import of this
# module. Confirmed harmless in practice (this module is not imported by
# `write_guards.engine` or `bash_guards.dispatch`, so no measured import-
# budget entrypoint is affected -- see
# state/improvement-queue/2026-08-13-dispatch-provision-py-147-carries-the-sa-
# 0fb4638944ec.yaml), fixed anyway for consistency with the other three sites.
# `_resolve_roster_accessor()` below imports lazily, ONLY when the roster is
# actually needed. Caches on this module's own `resolve_roster` attribute --
# same shape as `coordinator_core.session.core._psutil()` -- which is what
# keeps `monkeypatch.setattr(provision, "resolve_roster", ...)` working
# unmodified. DO NOT re-flatten this back to a module-level import.
_UNRESOLVED = object()
resolve_roster = _UNRESOLVED  # type: ignore[assignment]


def _resolve_roster_accessor():
    """Lazily import and cache ``resolve_roster`` on this module's own
    attribute (see the negative-spec comment above this cache's
    declaration). Returns the callable; never calls it.
    """
    global resolve_roster
    if resolve_roster is _UNRESOLVED:
        from coordinator_core.hooks.block_unenumerated_agent_type import (
            resolve_roster as _imported_resolve_roster,
        )

        resolve_roster = _imported_resolve_roster
    return resolve_roster


def _yaml_quote(value: str) -> str:
    """Minimal double-quote YAML scalar quoting -- escapes backslash and
    double-quote only. Mirrors ``coordinator-doc-new``'s ``_yaml_quote`` in
    scope (this module never imports across the claude-klabauter/CLI-script boundary;
    a plain markdown scaffolder script is not an importable Python module),
    kept intentionally small since frontmatter values here are session ids,
    plan paths, and chunk ids -- never arbitrary user prose.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _log_unenumerated_sidecar_miss(agent_type: str, subagent_type: str) -> None:
    """AC7 -- close the `report_sidecar` lookup-miss's SILENCE, not its
    fail-open verdict (defence in depth per C1: an unenumerated type cannot
    reach this spawn-time call at all once C1's `PreToolUse(Agent)` deny is
    wired, so this is a diagnostic net for the gap before that wiring lands
    / any bypass path, never a new decision branch). Distinguishes the two
    reasons a type can fail `report_sidecar` membership:

      - on the union-of-three roster (``resolve_roster``, C1) but simply not
        opted into ``report_sidecar`` -- an ordinary, EXPECTED miss (e.g.
        ``Explore``/``Plan`` are legitimate dispatch kinds with no sidecar
        need) -- stays silent, unchanged from before this chunk.
      - NOT on the roster at all -- the "less governed AND less instructed"
        shape this plan's Problem section names -- gets a stderr note so the
        miss is observable instead of indistinguishable from the expected
        case. Never flips the fail-open return value above; matches this
        module's "must never brick a spawn" contract verbatim.

    A `resolve_roster` load failure (peer-repo hiccup) degrades to today's
    silence, same posture C3 already took for the plan-body guards -- this
    diagnostic is not important enough to newly fail loud on a roster read
    error.
    """
    roster, roster_error = _resolve_roster_accessor()()
    if roster is None:
        return
    if agent_type in roster or subagent_type in roster:
        return
    print(
        "provision: subagent_sidecar miss for unenumerated type "
        f"agent_type={agent_type!r} subagent_type={subagent_type!r} -- not on "
        "the roster (docs/plans/2026-08-10-deny-unenumerated-agent-types-at-"
        "dispatch.md); C1's PreToolUse(Agent) deny is the primary fix, this "
        "is defence-in-depth visibility only, no sidecar provisioned",
        file=sys.stderr,
    )


def _build_sidecar_text(
    agent_type: str,
    spawned_at: str,
    plan_path: Optional[str],
    chunk_id: Optional[str],
    dispatched_by: Optional[str],
) -> str:
    """Emit the subagent-sidecar decision-object container's frontmatter +
    body -- the spawn-time counterpart to ``coordinator-doc-new``'s
    ``_scaffold_subagent_sidecar`` (same field set, same key order; the two
    MUST stay in lockstep -- see that function's docstring for the shape
    rationale and schema-of-record citation).

    ``plan_path``/``chunk_id``/``dispatched_by`` are all optional here
    (unlike the CLI, which enforces them as required args) because a live
    spawn payload may omit any of them -- this module fails open on missing
    eligibility/session_id upstream in ``provision_subagent_sidecar``, but a
    spawn that IS eligible should still get a sidecar even if the dispatcher
    forgot to thread ``plan``/``chunk`` through; the frontmatter simply
    carries ``null`` for the missing field rather than refusing to
    provision.
    """
    plan_field = _yaml_quote(plan_path) if plan_path else "null"
    chunk_field = _yaml_quote(chunk_id) if chunk_id else "null"
    dispatched_by_field = _yaml_quote(dispatched_by) if dispatched_by else "null"
    lines = [
        "---",
        f"plan: {plan_field}",
        f"chunk: {chunk_field}",
        f"dispatched_at: {_yaml_quote(spawned_at)}",
        f"dispatched_by: {dispatched_by_field}",
        "status: dispatched",
        f"agent_type: {_yaml_quote(agent_type)}",
        f"spawned_at: {_yaml_quote(spawned_at)}",
        "commits: []",
        "sidecar_schema: v1",
        "completion_status: pending",
        "divergence_from_plan:",
        "  diverged: false",
        '  summary: ""',
        '  detail: ""',
        "---",
        "",
        "<!-- Subagent-sidecar decision-object container (schemas/decision-",
        "     object.schema.json $defs/subagent_sidecar). This is a write",
        "     TARGET, not a write CAPABILITY -- it grants no tool access the",
        "     dispatched agent did not already have. See",
        "     docs/plans/2026-07-24-canonical-resolution-engine.md § W2-B3. -->",
        "",
        "## tell_the_EM",
        "",
        "<!-- Freeform exit-interview channel -- anything the agent wants",
        "     the dispatching EM to know that doesn't fit completion_status",
        "     or divergence_from_plan above. -->",
        "",
    ]
    return "\n".join(lines)


def provision_subagent_sidecar(
    payload: Dict[str, Any], policy_path: Optional[str] = None, cwd: Optional[str] = None
) -> Optional[str]:
    """Compute + write the subagent-sidecar decision-object doc; return its
    repo-relative path, or ``None``.

    Mirrors ``provision_report._provision``'s eligibility/session/path
    resolution verbatim (same imports, same fail-open branches, same
    single-segment sanitizer discipline) -- the ONLY differences are the
    document shape (``_build_sidecar_text`` vs ``_build_doc_text``) and the
    filename suffix (``-sidecar-<nonce>.md`` vs ``-<nonce>.md``) used to keep
    a run-report sidecar and a subagent-sidecar for the SAME session/label
    pair from colliding on disk when both are provisioned for one spawn.
    """
    git_root = resolve_git_root(cwd)
    policy = load_policy(policy_path)

    agent_id, agent_type, subagent_type = resolve_effective_types(payload, git_root)

    is_eligible = agent_type in policy.report_sidecar or subagent_type in policy.report_sidecar
    if not is_eligible:
        # A named teammate whose back-pointer moved out from under it is not
        # ineligible -- it is unrecognizable. Checked BEFORE the miss is
        # logged, so a recovered spawn is not also reported as an
        # unenumerated-type miss it never was.
        recovered = _recover_orphaned_sidecar(git_root, payload, subagent_type, _POINTER_KIND)
        if recovered is not None:
            return recovered
        _log_unenumerated_sidecar_miss(agent_type, subagent_type)
        return None

    effective_label = agent_type if agent_type in policy.report_sidecar else subagent_type

    if not git_root:
        return None

    session_id = payload.get("session_id") or None
    if not session_id:
        return None

    sanitized_session_id = _sanitize_segment(str(session_id))
    sanitized_label = _sanitize_segment(str(effective_label))
    if sanitized_session_id is None or sanitized_label is None:
        return None

    # Same sanitize-before-filesystem-mutation discipline as provision_report:
    # resolve the optional deterministic provision_key BEFORE any mkdir/open,
    # so a rejected key fails open cleanly with no stray directory left behind.
    provision_key = payload.get("provision_key") or None
    sanitized_provision_key: Optional[str] = None
    if provision_key is not None:
        sanitized_provision_key = _sanitize_segment(str(provision_key))
        if sanitized_provision_key is None:
            return None
    elif agent_id:
        # Concurrent same-agent_type dispatch disambiguation (2026-08-15
        # incident: three concurrent coordinator:code-reviewer agents got
        # nonce-named sidecars, so a reviewer that found two identically-
        # shaped open siblings had no way to identify its OWN file and
        # correctly refused to write). No caller-supplied provision_key ->
        # derive one deterministically from `effective_label` + the already-
        # resolved, already-canonicalized `agent_id` (see
        # `resolve_effective_types` above -- either a bare-hex id
        # `[a-f0-9]{12,}` or a named-teammate id `a.+-[a-f0-9]{16}`, where
        # the named form's `.+` matches ANY character short of newline, so
        # `agent_id` is NOT guaranteed `_sanitize_segment`-safe on its own --
        # the `_sanitize_segment(derived_key)` call below is load-bearing,
        # not redundant, and must not be dropped even though `derived_key`
        # already looks path-shaped).
        # `agent_id` is unique PER SPAWNED AGENT within a session by
        # construction (it is how this same resolver disambiguates Bash-call
        # attribution elsewhere in this tree), so two concurrent same-type
        # dispatches with well-formed agent_ids can never derive the same
        # key. That collision-proof guarantee only holds when `agent_id`
        # survives `_sanitize_segment` unchanged -- a named-teammate id that
        # does NOT (e.g. one containing a stripped character) would collapse
        # onto any other id differing only in the stripped characters, which
        # is the exact silent-overwrite failure this fix exists to close.
        # Guard against that: only derive a key when `agent_id` is already
        # sanitize-stable; otherwise fall through to the random-nonce path
        # below exactly as if no agent_id had resolved at all -- a malformed
        # id is precisely the case this derivation cannot safely cover.
        #
        # This trades away idempotence across a genuine re-dispatch of "the
        # same logical unit" under a NEW agent_id (a fresh Task-tool spawn
        # always gets a fresh id) -- deliberately: nothing on this call's
        # inputs distinguishes "re-dispatch the same logical unit" from
        # "dispatch a new, unrelated unit of the same type" without a
        # caller-supplied key, and guessing wrong there would silently
        # collide two UNRELATED agents' sidecars, which is the exact failure
        # this fix closes. A caller that wants idempotent re-dispatch across
        # spawns keeps passing an explicit `provision_key` (unchanged, this
        # branch is not reached when one is supplied) -- deriving one here
        # only replaces today's fully-random nonce with a deterministic,
        # collision-proof-within-session key sharing the SAME
        # `<key>.subagent-sidecar.md` naming as an explicit provision_key,
        # so a second call with the identical agent_id (e.g. a hook retry
        # for the same spawn attempt) still reopens the same file rather
        # than minting a second one.
        if _sanitize_segment(str(agent_id)) == agent_id:
            derived_key = f"{sanitized_label}.{agent_id}"
            sanitized_provision_key = _sanitize_segment(derived_key)

    session_dir = Path(git_root) / "state" / "subagent-share" / sanitized_session_id

    # CONTINUITY: adopt this agent's EXISTING sidecar when the session id has
    # moved out from under it. Same defect, same fix, same reasoning as
    # `subagent_sandbox.provision_report._provision` -- see the long comment
    # there. This module shares that module's home, its `(session_id,
    # agent_id)` key, and its FileExistsError idempotency, so it shared the
    # defect too: `/clear` mints a fresh session id without ending the
    # process, and a subagent that outlives one was scaffolded a second, EMPTY
    # sidecar while its populated one was orphaned under the old id.
    #
    # `_POINTER_KIND` keeps the two producers' indexes apart. They provision
    # for the same agent with different leaf suffixes (`<key>.md` there,
    # `<key>.subagent-sidecar.md` here), so a shared flat key would hand one
    # producer's spawn the other's document.
    raw_agent_id = str(payload.get("agent_id") or "")
    if sanitized_provision_key is not None and raw_agent_id:
        leaf = f"{sanitized_provision_key}.subagent-sidecar.md"
        if not (session_dir / leaf).exists():
            adopted = _read_sidecar_pointer(git_root, raw_agent_id, _POINTER_KIND)
            if adopted is not None and adopted != (
                f"state/subagent-share/{sanitized_session_id}/{leaf}"
            ):
                return adopted

    # sanitized_session_id is guaranteed separator-free by _sanitize_segment,
    # so this can only ever mkdir a direct child of subagent-share/
    # (confinement invariant -- do not relax the sanitizer without revisiting
    # this). Deferred until after the adoption check above so an adopting
    # spawn leaves no stray empty session dir behind.
    session_dir.mkdir(parents=True, exist_ok=True)

    spawned_at = datetime.now(timezone.utc).isoformat()
    plan_path = payload.get("plan") or None
    chunk_id = payload.get("chunk") or None
    doc_text = _build_sidecar_text(
        agent_type=effective_label,
        spawned_at=spawned_at,
        plan_path=plan_path,
        chunk_id=chunk_id,
        dispatched_by=str(session_id),
    )

    if sanitized_provision_key is not None:
        rel_path = f"state/subagent-share/{sanitized_session_id}/{sanitized_provision_key}.subagent-sidecar.md"
        doc_path = session_dir / f"{sanitized_provision_key}.subagent-sidecar.md"
        try:
            with open(doc_path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(doc_text)
            # Claim attribution: the DISPATCHING session (payload["session_id"],
            # raw -- never agent_id, never the sanitized directory leaf) owns
            # this claim -- see session_scope.touch_written_path's docstring
            # for the full rationale and the phantom-live-peer guard it
            # applies before recording.
            session_scope.touch_written_path(str(session_id), rel_path, cwd)
            # CONTINUITY: record where this agent's sidecar went, so the next
            # spawn under a DIFFERENT session id can find it. Best-effort -- a
            # pointer that cannot be written costs continuity, never the
            # sidecar.
            if raw_agent_id:
                _write_sidecar_pointer(git_root, raw_agent_id, rel_path, _POINTER_KIND)
        except FileExistsError:
            # Idempotent re-dispatch hit (same provision_key): preserve
            # existing content, just return its path -- matches
            # provision_report's chunk re-dispatch behavior.
            pass
        return rel_path

    nonce = secrets.token_hex(4)
    doc_path = session_dir / f"{sanitized_label}-sidecar-{nonce}.md"
    try:
        with open(doc_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(doc_text)
        session_scope.touch_written_path(
            str(session_id),
            f"state/subagent-share/{sanitized_session_id}/{sanitized_label}-sidecar-{nonce}.md",
            cwd,
        )
    except FileExistsError:
        # Second collision (astronomically unlikely at 32 bits) falls through
        # to main()'s blanket except -- fail-open, never brick the spawn.
        nonce = secrets.token_hex(4)
        doc_path = session_dir / f"{sanitized_label}-sidecar-{nonce}.md"
        with open(doc_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(doc_text)
        session_scope.touch_written_path(
            str(session_id),
            f"state/subagent-share/{sanitized_session_id}/{sanitized_label}-sidecar-{nonce}.md",
            cwd,
        )

    return f"state/subagent-share/{sanitized_session_id}/{sanitized_label}-sidecar-{nonce}.md"


def _read_stdin() -> str:
    return sys.stdin.read()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coordinator_core.dispatch.provision",
        description="Spawn-time subagent-sidecar decision-object provisioner.",
    )
    parser.add_argument(
        "--policy",
        dest="policy_path",
        default=None,
        help="Explicit path to the subagent-sandbox-policy.yaml file "
        "(overrides SUBAGENT_SANDBOX_POLICY env var).",
    )
    parser.add_argument(
        "--cwd",
        dest="cwd",
        default=None,
        help="Working directory to resolve the git root from (defaults to "
        "the process cwd).",
    )
    args = parser.parse_args(argv)

    try:
        payload_text = _read_stdin()
        try:
            payload = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"provision: malformed stdin payload, skipping subagent-sidecar: {exc}", file=sys.stderr)
            return 0
        if not isinstance(payload, dict):
            print("provision: stdin payload is not a JSON object, skipping subagent-sidecar", file=sys.stderr)
            return 0

        sidecar_path = provision_subagent_sidecar(payload, args.policy_path, args.cwd)
        if sidecar_path is not None:
            print(json.dumps({"subagent_sidecar": sidecar_path}))
    except Exception as exc:  # noqa: BLE001 -- spawn-time hook must never brick a spawn (module contract above)
        print(f"provision: unexpected error, skipping subagent-sidecar: {exc}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
