"""Compose the roster-v5 ``execute_review`` wave (Design D6, task C11).

Spec: ``docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md``
task C11, against coordinator-content-repo
``docs/plans/2026-09-27-review-inside-execute-plan.md`` § Contract (ask
#1). Consumes ``roster.parse_execute_review``'s :class:`ExecuteReview` and
emits three ``(phase_title, block)`` entries through
``compose._agent_call_literal`` -- prep, ONE ``parallel([...])`` review
wave, integration -- exactly the shape ``compose_execute_review`` docstring
below pins. Never reads a fragment or resolves a cross-repo pointer: pure,
like ``roster.py`` and ``compose.py``.

Result bindings (C13 reads these names verbatim): ``_reviewPrep``,
``_reviewWave``, ``_deliveryVerdict``, ``_reviewIntegration``.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Tuple

from coordinator_core.ops.review_mint.roster import (
    _TSHIRT_ORDER,
    DELIVERY_NOT_ROSTERED,
    ExecuteReview,
    ReviewAgent,
    RosterFragmentError,
    VALID_TIERS,
)
from coordinator_core.ops.review_mint.compose import _agent_call_literal, prompt_literal, with_sidecar_clause
from coordinator_core.ops.review_mint.wave_not_run import (
    NO_REVIEW as _WAVE_NO_REVIEW,
    REJECTED as _WAVE_REJECTED,
    SKIPPED as _WAVE_SKIPPED,
    WAVE_NOT_RUN_VAR,
    not_run_entry_js as _not_run_entry_js,
)
from coordinator_core.ops.dispatch_emit.test_interpreter import interpreter_clause
from coordinator_core.ops.review_slice_limits import MAX_SLICES
from coordinator_core.ops.review_prep import REVIEW_PREP_BUSY_PREFIX
from coordinator_core.ops.workflow_scaffold import _js_string_literal

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from coordinator_core.ops import requirement_register as _rr
from coordinator_core.ops.requirement_register import JudgeEvidence
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.session import record_homes

_DELIVERY_VERIFIER_AGENT_TYPE = "coordinator:delivery-verifier"

#: `coordinator:delivery-verifier` is registered by a plugin file
#: (`coordinator/agents/delivery-verifier.md`) that a running host may not
#: have reloaded yet -- unlike the executor/enricher/test roster
#: (`dispatch_emit.emit._HOST_NATIVE_AGENT_TYPE_ROSTER`), there is no signal
#: this module can read to confirm the host's roster is current, so this
#: type degrades to the harness's universal `general-purpose` built-in
#: UNCONDITIONALLY -- never guessed live from `agent_type_host`, which only
#: tells us the host resolves `coordinator:*` at all, not that this
#: particular, newly-added one is loaded. The role that `agentType` would
#: have carried is folded into the prompt instead, so `general-purpose`
#: still knows what it is being asked to do. DELETE-WHEN: the host roster
#: exposes a readable currency signal -- then route this type through
#: `_HOST_NATIVE_AGENT_TYPE_ROSTER` like the rest and drop both constants.
_DELIVERY_VERIFIER_HOST_NATIVE_TYPE = "general-purpose"

#: Block-local JS slot the in-wave delivery paths write their null reason into; the block publishes
#: it as `_deliveryNotRun` after the wave. Never a `let _deliveryNotRun` in the block:
#: `emit._unconst` strips only `const `, so it would shadow the script-scope pre-declaration.
_DELIVERY_REASON_VAR = "_deliveryNotRunReason"
_DELIVERY_REJECTED_JS = (
    f"{_DELIVERY_REASON_VAR} = 'rejected: ' + String(e && e.message || e);"
)
_DELIVERY_VERIFIER_ROLE_PREAMBLE = (
    "You are acting as the delivery-verifier reviewer: confirm the diff "
    "actually delivers what the plan/brief promised (not just that it "
    "compiles or passes tests) before this wave's verdict is integrated."
)


_HOST_NATIVE_REVIEW_TYPE = "general-purpose"


def _host_native(agent_type: str, host_degraded: bool) -> Tuple[str, str]:
    """``(emitted agentType, role preamble)`` for a review-stage agent. On a
    host that cannot resolve ``coordinator:*`` the stage runs as
    ``general-purpose`` with its role folded into the prompt, so the review
    wave still runs; otherwise the type is emitted unchanged."""
    if not host_degraded or not agent_type.startswith("coordinator:"):
        return agent_type, ""
    return (
        _HOST_NATIVE_REVIEW_TYPE,
        f"You are acting as the {agent_type.split(':', 1)[1]} agent for this review stage: "
        "perform that role's duties and return the structured result below.\n\n",
    )


def _resolve_schema_refs(schema, stage_schemas: Dict[str, dict], *, _seen=None):
    """shell-doc-ok: the JSON-Schema keyword ``$ref``, not a shell variable.
    Inline every ``{"$ref": "#/$defs/<name>"}`` in ``schema`` against
    ``stage_schemas`` (DoE's ``review-stage.schema.json`` ``$defs``) so the
    result is self-contained: no ``$ref`` that resolves only against a
    sibling ``$defs`` entry the emitted script never receives (the per-agent
    schema handed to ``agent()`` is JSON-serialized standalone, with no
    surrounding document to hang a JSON-Pointer walk-up on).

    ``_seen`` guards a ``$ref`` cycle across the recursion (a self- or
    mutually-referential pair of ``$defs`` entries would otherwise recurse
    forever); it raises rather than silently truncating, since a cycle here
    is a schema-authoring defect DoE should fix, not paper over.
    """
    if _seen is None:
        _seen = frozenset()
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            def_name = ref[len("#/$defs/"):]
            if def_name in _seen:
                raise RosterFragmentError(
                    f"execute_review stage schema has a $ref cycle through "
                    f"{def_name!r} -- $defs must not be self- or mutually-"
                    f"referential"
                )
            target = stage_schemas.get(def_name)
            if target is None:
                raise RosterFragmentError(
                    f"execute_review stage schema $ref {ref!r} does not "
                    f"resolve -- {def_name!r} not found in the injected "
                    f"stage_schemas (known: {sorted(stage_schemas)!r})"
                )
            return _resolve_schema_refs(
                target, stage_schemas, _seen=_seen | {def_name}
            )
        return {
            key: _resolve_schema_refs(value, stage_schemas, _seen=_seen)
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [_resolve_schema_refs(item, stage_schemas, _seen=_seen) for item in schema]
    return schema


def _schema_literal(name: str, stage_schemas: Dict[str, dict]) -> str:
    schema = stage_schemas.get(name)
    if schema is None:
        raise RosterFragmentError(
            f"execute_review agent carries unknown schema name {name!r} -- "
            f"not found in the injected stage_schemas (known: {sorted(stage_schemas)!r})"
        )
    resolved = _resolve_schema_refs(schema, stage_schemas)
    return json.dumps(resolved, sort_keys=True)


def _agent_opts_for(agent: ReviewAgent, *, emitted_agent_type: str = None) -> Dict[str, Dict[str, str]]:
    """``agent_opts`` keyed by the LITERAL ``agentType`` the call will carry
    (``emitted_agent_type``, defaulting to ``agent.agent_type`` unchanged) --
    ``_agent_call_literal`` looks its ``model``/``effort`` entry up by that
    same literal, so a caller that substitutes the agentType (e.g. the
    delivery-verifier host-native degrade) must key this dict to match or
    the roster's model/effort silently vanish from the emitted call.
    """
    entry: Dict[str, str] = {}
    if agent.model is not None:
        entry["model"] = agent.model
    if agent.effort is not None:
        entry["effort"] = agent.effort
    key = emitted_agent_type if emitted_agent_type is not None else agent.agent_type
    return {key: entry} if entry else {}


def _prompt_literal(prompt: str) -> str:
    return prompt_literal(prompt)


#: A frozen diff touching at most this many product files is a small diff (APM ruling
#: 2026-10-08, file count not LOC); the count is prep's `product_files`, known only at run time.
SMALL_DIFF_PRODUCT_FILES = 3

#: Unknown or missing `product_files` reads as large, so the full brief is the fallback.
_SMALL_DIFF_JS = f"((_reviewPrep?.product_files ?? {SMALL_DIFF_PRODUCT_FILES + 1}) <= {SMALL_DIFF_PRODUCT_FILES})"

_LEAN_READ_CLAUSE = (
    "Small diff: read only the frozen diff named below and, where a hunk needs it, the touched "
    "files themselves. Do not read the plan body or any other file; the plan's own task rows "
    "for this run's rows are the only plan text you may open."
)


_BRIEF_CONFORMANCE_CLAUSE = (
    "Brief conformance: the plan's `## PM brief` section and its `prime_exit_criterion.statement` "
    "are the one plan text you must open, whatever else is off limits. For each verbatim item "
    "your diff bears on, add one row to your sidecar's frontmatter `brief_conformance:` list "
    "({brief_item, requirement, file_line, verified_how, status: met|partial|unmet}) and return "
    "the same rows as brief_conformance. An item with no evidence in your diff is `unmet`, never "
    "omitted."
)


def _brief_conformance_clause(criterion: Optional["OperativeCriterion"]) -> str:
    """The code-reviewer prompt line that hands it the brief items its ``brief_conformance`` rows
    quote; names the operative criterion statement when one is resolved."""
    named = f" Operative exit criterion: {criterion.statement}" if criterion is not None else ""
    return f"\n{_BRIEF_CONFORMANCE_CLAUSE}{named}"


DELIVERY_NOTES_LABEL = "Executor delivery notes in the brief"


def _delivery_notes_expr(delivery_notes_js: Optional[str], agent_type: str) -> str:
    """A JS string expression appending the executor rows' delivery notes to a code-reviewer slice
    prompt; empty for any other agent, for no source, and (at run time) for an empty source."""
    if not delivery_notes_js or not _is_code_reviewer(agent_type):
        return ""
    return f"(({delivery_notes_js}) ? {_js_string_literal(chr(10) + DELIVERY_NOTES_LABEL + ': ')} + ({delivery_notes_js}) : '')"


def _is_code_reviewer(agent_type: str) -> bool:
    return agent_type.split(":")[-1] == "code-reviewer"


def _size_gated(full_prompt: str, lean_prompt: str) -> str:
    """A JS expression choosing ``lean_prompt`` over ``full_prompt`` when prep froze a small diff."""
    return f"({_SMALL_DIFF_JS} ? {_prompt_literal(lean_prompt)} : {_prompt_literal(full_prompt)})"


def _degrading(call: str, on_error: str = "") -> str:
    """``call`` (``agent(...)`` or ``() => agent(...)``) rewritten so a
    rejection resolves to ``null`` instead of throwing the run past its
    terminal commit. Null, not an object: the wake digest's review and
    delivery legs treat any truthy result as a phase that ran, and null is
    already their not_run reading. ``on_error`` is a JS statement run in the
    catch, with the rejection bound to ``e``, before the null return.
    Review-phase calls only."""
    body = call[len("() => "):] if call.startswith("() => ") else call
    wrapped = (
        "(async () => { try { return await " + body + "; } catch (e) { "
        + (on_error + " " if on_error else "")
        + "return null; } })()"
    )
    return f"() => {wrapped}" if call.startswith("() => ") else wrapped


#: Emitted once per wave block that fans a ratio-bearing slice agent: splits `slices` into
#: `min(slices, ceil(rows / maxRows))` (at least one) contiguous groups, sizes differing by at most one.
_GROUP_SLICES_JS = (
    "  const _groupSlices = (slices, rows, minRows, maxRows) => {\n"
    "    const n = Math.max(1, Math.min(slices.length, Math.ceil(rows / maxRows), Math.floor(rows / minRows)));\n"
    "    const groups = [];\n"
    "    let at = 0;\n"
    "    for (let i = 0; i < n; i++) {\n"
    "      const size = Math.floor(slices.length / n) + (i < slices.length % n ? 1 : 0);\n"
    "      groups.push(slices.slice(at, at + size));\n"
    "      at += size;\n"
    "    }\n"
    "    return groups.filter(g => g.length);\n"
    "  };"
)

_GROUPED_SLICES_CLAUSE = (
    "\nYou own every slice listed above: apply findings only inside their files, write one sidecar "
    "at the first slice's sidecar_path, and return slice_id as the slice ids joined with `+`."
)

#: Prefix of the `halted` string the emitted script returns when review prep yields nothing
#: to review over a diff that has product files. Fails the run closed before anything lands.
NO_SLICES_HALT = "review prep returned no slices"

_NO_SLICES_REFUSAL = (
    "review prep returned no slices (it failed, refused, or froze nothing); "
    "refusing to continue unreviewed. Re-run the review prep, then resume."
)


#: Placeholder for a run-time slice key; survives `prep_slice_id_for`'s sanitising.
_SLICE_KEY_SENTINEL = "__SLICE_KEY__"


def prep_slice_id_for(
    plan_path: str, run_base_sha: Optional[str], run_key: Optional[str] = None
) -> str:
    """The frozen-diff slice id of one run's prep: plan stem, `run_key` (the run's own unique id)
    and run base, so concurrent runs never share `state/review-trail/diffs/<id>.diff`. Every
    caller passes its run key: a plan-less route (queue grind, ask manifest) has no distinguishing
    stem, so the run base alone would collide."""
    safe = lambda s: re.sub(r"[^A-Za-z0-9_.-]", "-", s)  # noqa: E731
    parts = [safe(Path(plan_path.replace("\\", "/")).stem), safe(run_key or ""), (run_base_sha or "nobase")[:12]]
    return "-".join(p for p in parts if p) + "-prep"


def em_owned_clause(em_owned_rows: Optional[List[str]]) -> str:
    """The prompt clause naming rows no executor was dispatched for. The verifier and the
    judge read the whole plan, so without it an EM-owned row reads as an unbacked claim."""
    if not em_owned_rows:
        return ""
    return (
        "\nEM-owned rows (never dispatched to an executor; the EM performs them outside this "
        "run): " + "; ".join(em_owned_rows) + ". This run's diff holds no hunk for them by "
        "construction. Never list them in claims_unbacked, and never hold the criterion unmet "
        "on a clause that depends only on them: report that clause as owed by the EM."
    )


WITHHELD_RULE = (
    "Withheld rows are not deliverable in this run: an external gate holds each. Grade the exit "
    "criterion WITHOUT the clauses only a withheld row delivers. Such a clause is never `not_met` "
    "and never counts as met: leave it out of `clauses` and return it in `withheld_clauses` as "
    "{clause, rows (the withheld row ids), register_rows (the register row ids they trace to)}. "
    "A clause a non-withheld row delivers is graded as usual; if it is unmet, the criterion is `not_met`."
)


def withheld_rows_clause(rows: Optional[List[dict]]) -> str:
    """The prompt clause naming withheld rows (``{id, title, register_rows}``) and the rule for
    grading the criterion without their clauses; empty when no row is withheld."""
    if not rows:
        return ""
    named = "; ".join(
        f"{r.get('id')} ({r.get('title') or 'untitled'})"
        + (f" [register {', '.join(r['register_rows'])}]" if r.get("register_rows") else "")
        for r in rows
    )
    return f"\nWithheld rows: {named}. {WITHHELD_RULE}"


_OWNER_HEAD = (
    "\nOwner-repo rows (delivered in another repo; this run's diff holds no hunk for them):"
)
_OWNER_OWNED = (
    "\n- {id}: owned by {owner_repo}, root {root}{closure}. Judge the row with "
    "`git -C {root}` at its HEAD (log and files), never from this repo's diff."
)
_OWNER_CLOSURE = (
    " (closure_key {closure_key}: for a deliverable key, also the commits whose Deliverable-Id "
    "trailer equals its id)"
)
_OWNER_ABSENT = (
    "\n- {id}: owner {owner_repo} is not on this machine: its repo is unreadable here. Judge "
    "nothing for the row and never report not_met for its absence."
)


_ENGINE_HEAD = (
    "\nEngine-plane rows (delivered in the engine; this run's diff may hold the cwd part):"
)
_ENGINE_OWNED = (
    "\n- {id}: engine-plane row, engine root {root} ({root_class}). Judge what it names in the "
    "engine with `git -C {root}` at its HEAD (log and files); any part naming this repo's own "
    "files stays judged here. Report every root you read as root_read (a ledger item) or in "
    "roots_read (the judge)."
)


def owner_repo_roots() -> Dict[str, str]:
    """Registry ``repos.*`` map as ``{shortname: posix root}`` (underscores in the key become
    dashes in the shortname). One registry read, no spawn; empty when no registry exists."""
    from coordinator_core.machine_resolver import merged_flat_registry

    return {
        key[len("repos."):].replace("_", "-"): val.replace("\\", "/")
        for key, val in merged_flat_registry().items()
        if key.startswith("repos.") and isinstance(val, str) and val
    }


def owner_repo_clause(rows: List[dict], roots: Dict[str, str]) -> str:
    """Prompt clause for rows owned by another repo (``{id, owner_repo, closure_key?}``): the
    verifier judges them at the owner's HEAD, and an owner absent from ``roots`` is judged
    nothing (never not_met). Mirrored text-for-text by ``owner_repo_clause_js``; the roots
    lookup differs (here by lowercased ``owner_repo``, there by the stage-filled ``owner_root``).
    A row carrying ``engine`` true is an engine-plane row (``owner_root``, ``root_class``) and
    renders under its own head; an empty ``owner_root`` renders the unreadable text."""
    if not rows:
        return ""
    engine = [r for r in rows if r.get("engine")]
    rows = [r for r in rows if not r.get("engine")]
    out = ""
    if rows:
        out = _OWNER_HEAD
    if engine:
        tail = _ENGINE_HEAD
        for row in engine:
            root = row.get("owner_root") or ""
            text = _ENGINE_OWNED if root else _OWNER_ABSENT
            tail += (
                text.replace("{id}", row["id"])
                .replace("{owner_repo}", row["owner_repo"])
                .replace("{root_class}", row.get("root_class") or "")
                .replace("{root}", root)
            )
    else:
        tail = ""
    for row in rows:
        owner = row["owner_repo"]
        root = roots.get(owner.lower())
        closure = (
            _OWNER_CLOSURE.replace("{closure_key}", row["closure_key"])
            if row.get("closure_key")
            else ""
        )
        text = _OWNER_OWNED if root else _OWNER_ABSENT
        out += (
            text.replace("{closure}", closure)
            .replace("{id}", row["id"])
            .replace("{owner_repo}", owner)
            .replace("{root}", root or "")
        )
    return out + tail


def owner_repo_clause_js(rows_js: str) -> str:
    """JS expression yielding ``owner_repo_clause`` for the runtime array ``rows_js`` of
    ``{id, owner_repo, owner_root, closure_key, engine?, root_class?}``; an empty ``owner_root``
    renders the unreadable text."""
    lit = _js_string_literal
    gated = (
        f"g.length ? {lit(_OWNER_HEAD)} + g.map(r => "
        f"(r.owner_root ? {lit(_OWNER_OWNED)} : {lit(_OWNER_ABSENT)})"
        f".split('{{closure}}').join(r.closure_key ? "
        f"{lit(_OWNER_CLOSURE)}.split('{{closure_key}}').join("
        f"typeof r.closure_key === 'string' ? r.closure_key : JSON.stringify(r.closure_key)) : '')"
        f".split('{{id}}').join(r.id).split('{{owner_repo}}').join(r.owner_repo)"
        f".split('{{root}}').join(r.owner_root ?? '')).join('') : ''"
    )
    engine = (
        f"e.length ? {lit(_ENGINE_HEAD)} + e.map(r => "
        f"(r.owner_root ? {lit(_ENGINE_OWNED)} : {lit(_OWNER_ABSENT)})"
        f".split('{{id}}').join(r.id).split('{{owner_repo}}').join(r.owner_repo)"
        f".split('{{root_class}}').join(r.root_class ?? '')"
        f".split('{{root}}').join(r.owner_root ?? '')).join('') : ''"
    )
    return (
        f"((rs) => {{ const g = rs.filter(r => !r.engine), e = rs.filter(r => r.engine); "
        f"return ({gated}) + ({engine}); }})"
        f"({rows_js})"
    )


_ANCESTRY_RULE = (
    " Decide ancestry with `git merge-base --is-ancestor <sha> <base>`: exit 0 is an ancestor, "
    "exit 1 is not, any other exit is unknown. Never infer non-ancestry from a sha's absence "
    "in a log range."
)


#: `_reviewPrep.slices` as an array whatever the agent returned: a summarised prep reply carries
#: a count there, and iterating it throws.
REVIEW_PREP_SLICES_JS = "(Array.isArray(_reviewPrep?.slices) ? _reviewPrep.slices : [])"

_PREP_GUARDS_JS = (
    # A single-reviewer-ok verdict over product files is one slice of the
    # whole diff; prep that omitted it is synthesised here, never refused.
    "  if (_reviewPrep && _reviewPrep.verdict === 'single-reviewer-ok' && "
    "!(Array.isArray(_reviewPrep.slices) ? _reviewPrep.slices : []).length && (_reviewPrep.product_files ?? 0) > 0) { "
    "_reviewPrep.slices = [{ id: 'whole-diff', files: [], "
    "diff_path: _reviewPrep.whole_diff_path, "
    "sidecar_path: (_reviewPrep.whole_diff_sidecars?.personas ?? [])[0] ?? "
    "String(_reviewPrep.whole_diff_path).replace(/\\.diff$/, '') + '.whole-slice.md', "
    "contract_blocks: 0 }]; }\n"
    # A clean prep over a run whose rows changed nothing froze an empty
    # diff: there is nothing to review or commit, which is an outcome,
    # not a failed prep.
    # A count of 0 that disagrees with the slices (one lists files), or with
    # a row this run landed, is not trusted: the run falls through to the
    # slices guard instead of halting, so landed rows never skip review.
    "  if (_reviewPrep && _reviewPrep.verdict === 'single-reviewer-ok' && "
    "(typeof _landed === 'undefined' || !Object.keys(_landed).length) && "
    "(typeof _finished === 'undefined' || !_finished.size) && "
    "(_reviewPrep.product_files ?? 0) === 0 && !(_reviewPrep.foreign_claims ?? []).length && "
    "!(Array.isArray(_reviewPrep.slices) ? _reviewPrep.slices : []).some(s => (s?.files ?? []).length)) { "
    "return { halted: 'no-op', reason: 'the run changed no product file; nothing to review or commit', "
    "prep: _reviewPrep, wave: null, integration: null }; }\n"
    # Rows landed but changed no product file (a verification-only row):
    # the clean empty-diff prep is the one case with nothing to review.
    # The slice reviewers, other whole-diff reviewers and integration
    # are skipped; the delivery verifier and the terminal judge still run.
    # The same clean-prep predicate as the no-op halt, minus its
    # landed-rows condition; a refused prep fails `verdict`, and a prep
    # with product files fails `product_files`, so both still throw.
    "  const _verifyOnly = !!(_reviewPrep && _reviewPrep.verdict === 'single-reviewer-ok' && "
    "(_reviewPrep.product_files ?? 0) === 0 && !(_reviewPrep.foreign_claims ?? []).length && "
    "!(Array.isArray(_reviewPrep.slices) ? _reviewPrep.slices : []).some(s => (s?.files ?? []).length));\n"
    # A failed or refused prep yields no slices -- a refusal reports
    # product_files 0, so the guard keys on the slices alone. The wave's
    # `?? []` would otherwise expand to no sliced reviewer and land the
    # run reviewed by the whole-diff tail alone. Declared paths are
    # non-empty here: the plan route always declares them, and the queue
    # route skips the wave before prep when no row committed.
    "  if (!_reviewPrep || (!(Array.isArray(_reviewPrep.slices) ? _reviewPrep.slices : []).length && !_verifyOnly)) { "
    f"return {{ halted: {_js_string_literal(NO_SLICES_HALT)} + ' (prep verdict: ' + "
    f"String(_reviewPrep?.verdict ?? 'none') + ')', reason: {_js_string_literal(_NO_SLICES_REFUSAL)}, "
    "prep: _reviewPrep ?? null, wave: null, integration: null }; }"
)

_PREP_BUSY_RETRIES = 5
PREP_BUSY_HALT = "review.prep stayed busy after retries"
PREP_MALFORMED_HALT = "review.prep reply carried a non-array slices"
REVIEW_MISSING_HALT = "review wave reply carried no review beside a successful prep"
_PREP_BUSY_REFUSAL = (
    "A review-wave agent's review.prep call stayed busy past its retries, so its slice group was "
    "not reviewed. Re-run the review wave."
)
SLICE_ID_UNKNOWN_HALT = "review wave reply named a slice_id that is not a real slice id"
_SLICE_ID_UNKNOWN_REFUSAL = (
    "A review-wave agent returned a slice_id that is not the '+'-joined ids of real slices (a range "
    "such as 'prep-1..5' is one), so coverage cannot be attributed. Re-run the review wave."
)
_PREP_MALFORMED_REFUSAL = (
    "A review-wave agent summarised its review.prep reply instead of returning it verbatim "
    "(slices was not a list), so its slice group was not reviewed. Re-run the review wave."
)
_REVIEW_MISSING_REFUSAL = (
    "A review-wave agent returned a prep reply with a null error and no review. Re-run the review wave."
)

#: With `prep_in_wave` the review-wave block binds these after the wave, from the replies the
#: agents returned beside their reviews. `_reviewPrep` is the first reply without an error with
#: every reply's slices merged by id (when the relayed slices are all empty, the slice ids the wave reviews name stand in -- the stored reply, not the relay, is authoritative); when every reply errored it is the first reply, else null.
_PREP_FROM_WAVE_JS = (
    "  const _prepReplies = _waveRaw.map(r => (r && r.prep) ? r.prep : null).filter(p => p);\n"
    f"  const _busyPrefix = {_js_string_literal(REVIEW_PREP_BUSY_PREFIX)};\n"
    "  const _busyReplies = _prepReplies.filter(p => typeof p.error === 'string' && p.error.startsWith(_busyPrefix));\n"
    "  if (_busyReplies.length) { "
    f"return {{ halted: {_js_string_literal(PREP_BUSY_HALT)} + ' (' + _busyReplies.length + ' of ' + "
    "_waveRaw.length + ' wave agents; reviewed slices: ' + "
    "_prepReplies.filter(p => !p.error).flatMap(p => (Array.isArray(p.slices) ? p.slices : []).map(s => s.id)).join(', ') + ')', "
    f"reason: {_js_string_literal(_PREP_BUSY_REFUSAL)}, "
    "prep: _busyReplies[0], wave: null, integration: null }; }\n"
    "  const _malformed = _prepReplies.filter(p => p.slices !== undefined && !Array.isArray(p.slices) && !p.error);\n"
    "  if (_malformed.length) { "
    f"return {{ halted: {_js_string_literal(PREP_MALFORMED_HALT)} + ' (' + _malformed.length + ' of ' + "
    "_waveRaw.length + ' wave agents)', "
    f"reason: {_js_string_literal(_PREP_MALFORMED_REFUSAL)}, "
    "prep: { ..._malformed[0], slices: [] }, wave: null, integration: null }; }\n"
    "  if (_waveRaw.some(r => r && r.prep && !r.prep.error && (Array.isArray(r.prep.slices) ? r.prep.slices : []).length && (r.review === undefined || r.review === null))) { "
    f"return {{ halted: {_js_string_literal(REVIEW_MISSING_HALT)}, "
    f"reason: {_js_string_literal(_REVIEW_MISSING_REFUSAL)}, "
    "prep: _prepReplies[0] ?? null, wave: null, integration: null }; }\n"
    "  const _reviewPrep = (() => {\n"
    "    const ok = _prepReplies.find(p => !p.error);\n"
    "    if (!ok) return _prepReplies[0] ?? null;\n"
    "    const seen = new Set();\n"
    "    const merged = [];\n"
    "    for (const p of _prepReplies) for (const s of (Array.isArray(p.slices) ? p.slices : [])) {\n"
    "      if (!seen.has(s.id)) { seen.add(s.id); merged.push(s); }\n"
    "    }\n"
    "    if (!merged.length) for (const r of _waveRaw) {\n"
    "      const id = r && r.review && typeof r.review.slice_id === 'string' ? r.review.slice_id : '';\n"
    "      for (const sid of id.split('+').filter(Boolean)) if (!seen.has(sid)) {\n"
    "        seen.add(sid);\n"
    "        merged.push({ id: sid, files: [], diff_path: null, sidecar_path: r.review.sidecar_path ?? null, contract_blocks: 0 });\n"
    "      }\n"
    "    }\n"
    "    return { ...ok, slices: merged };\n"
    "  })();\n"
    "  const _realSliceIds = new Set((_reviewPrep && Array.isArray(_reviewPrep.slices) ? _reviewPrep.slices : []).map(s => s.id));\n"
    "  const _badSliceIdAt = _waveRaw.findIndex(r => r && r.review && typeof r.review.slice_id === 'string' && "
    "r.review.slice_id.split('+').filter(Boolean).some(id => !_realSliceIds.has(id)));\n"
    "  if (_badSliceIdAt >= 0) { "
    f"return {{ halted: {_js_string_literal(SLICE_ID_UNKNOWN_HALT)} + ' (wave agent ' + _badSliceIdAt + "
    "' returned slice_id ' + JSON.stringify(_waveRaw[_badSliceIdAt].review.slice_id) + '; real slice ids: ' + "
    "[..._realSliceIds].join(', ') + ')', "
    f"reason: {_js_string_literal(_SLICE_ID_UNKNOWN_REFUSAL)}, "
    "prep: _reviewPrep, wave: null, integration: null }; }\n"
)


_WAVE_GROUP_CLAUSE = (
    "\nYou own every slice the reply lists: apply findings only inside their files, write one "
    "sidecar at the first slice's sidecar_path, and return slice_id as the `id` of each slice the reply "
    "lists, copied verbatim and joined with `+`. Never shorten, elide or range an id: the wave accepts "
    "only the ids the reply lists."
)


def _wave_prep_instruction(
    mode_args: str, base_ref: str, prep_slice_id: str, repo_root: Optional[str], *, grouped: bool
) -> str:
    """The first-act ``review.prep`` instruction every review-wave agent carries under
    ``prep_in_wave``; a grouped agent adds the ``group_index`` / ``group_count`` values its prompt
    ends with."""
    group_args = ',"group_index":<group_index>,"group_count":<group_count>' if grouped else ""
    group_note = " Pass the `group_index:` and `group_count:` values given below." if grouped else ""
    repo_pin = f" --repo '{repo_root}'" if repo_root else ""
    return (
        f"First, run this run's review prep as ONE engine call, never your own freeze, grouping or "
        f"claim check: `\"${{COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}}/bin/coordinator-invoke\" "
        f"review.prep '{{{mode_args},\"base\":\"{base_ref}\","
        f"\"slice_prefix\":\"{prep_slice_id}\",\"paths\":[<every declared path>],"
        f"\"plan_path\":\"<plan_path>\"{group_args}}}'{repo_pin}`, its command word exactly as written "
        f"(a guard denies the prefix moved into a shell variable). <plan_path> is the `plan:` value "
        f"below when it is given, else the `plan_path:` line. When a non-empty `session_id:` value is "
        f"given, add it as `\"session_id\"`.{group_note} The op computes once per run: concurrent "
        f"callers read one stored reply. Return the reply verbatim as `prep`, beside your review as "
        f"`review`. An error beginning `{REVIEW_PREP_BUSY_PREFIX}` is not a refusal: re-run the same "
        f"call, up to {_PREP_BUSY_RETRIES} times, and if it is still busy return `prep` alone. Any other "
        f"non-null error in the reply is a refusal: return `prep` alone."
    )


def _wave_schema_literal(schema_literal: str) -> str:
    """The review schema wrapped so an agent returns its prep reply beside its review.

    Only ``prep`` is schema-required: the runtime's validator is not known to honour ``if/then`` or
    ``oneOf``. The wave consumer (``_PREP_FROM_WAVE_JS``) enforces that ``review`` is present
    unless ``prep.error`` is non-null."""
    return (
        '{ "type": "object", "properties": { "prep": { "type": "object", "properties": { '
        '"slices": { "type": "array", "items": { "type": "object" } }, '
        '"error": { "type": ["string", "null"] } } }, '
        f'"review": {schema_literal} }}, "required": ["prep"] }}'
    )


REVERIFY_PHASE = "Review re-verify"
_REVERIFY_AGENT_TYPE = "coordinator:test-runner"

_REVERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "edited": {"type": "array", "items": {"type": "string"}},
        "failing": {"type": "array", "items": {"type": "string"}},
        "reverted": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "tests": {"type": "array", "items": {"type": "string"}},
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"sidecar": {"type": "string"}, "id": {"type": "string"}},
                        },
                    },
                },
                "required": ["path"],
            },
        },
        "status_before": {"type": "string"},
        "status_after": {"type": ["string", "null"]},
        "record": {"type": ["string", "null"]},
        "sidecar_path": {"type": "string"},
    },
    "required": ["edited", "failing", "reverted", "status_before"],
}

#: Run-time review sidecar paths of the wave, appended to the re-verify prompt for its `sidecars`.
_REVERIFY_SIDECARS_JS = (
    "'\\nreview_sidecars: ' + JSON.stringify((Array.isArray(_reviewWave) ? _reviewWave : [])"
    ".filter(w => w && typeof w.sidecar_path === 'string').map(w => w.sidecar_path))"
)

#: `unapplied` for one re-verify reply `r`: each reverted path joined to the prep slices whose
#: `files` hold it. A path no slice holds (a file review created) carries a null slice.
_REVERIFY_UNAPPLIED_JS = (
    "(r.reverted ?? []).flatMap(v => { "
    "const norm = (p) => String(p).split('\\\\').join('/'); "
    f"const owners = {REVIEW_PREP_SLICES_JS}.filter(s => (s.files ?? []).some(f => norm(f) === norm(v.path))); "
    "const ids = (sc) => (v.findings ?? []).filter(f => sc === null || f.sidecar === sc).map(f => f.id); "
    "if (!owners.length) return [{ path: v.path, slice_id: null, sidecar_path: null, finding_ids: ids(null) }]; "
    "return owners.map(s => { "
    "const w = (Array.isArray(_reviewWave) ? _reviewWave : []).find(x => x && typeof x.slice_id === 'string' && "
    "x.slice_id.split('+').includes(s.id)); "
    "const sc = (w && w.sidecar_path) ?? s.sidecar_path ?? null; "
    "return { path: v.path, slice_id: s.id, sidecar_path: sc, finding_ids: ids(sc) }; }); })"
)


def _reverify_phase(
    *,
    base_ref: str,
    prep_slice_id: str,
    repo_root: Optional[str],
    plan_path: str,
    run_base_sha: str,
    prompt_head: str,
    host_degraded: bool,
    sc,
) -> Tuple[str, str]:
    """The ``Review re-verify`` phase: a test-runner lists what review changed (``review.reverify``
    list mode), runs only the tests covering it, and reverts the implicated files in revert mode
    when any fail. ``unapplied`` is joined from the reply in the emitted JS, never by the agent."""
    agent_type, role = _host_native(_REVERIFY_AGENT_TYPE, host_degraded)
    repo_pin = f" --repo '{repo_root}'" if repo_root else ""
    invoke = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'
    base_args = f'"base":"{base_ref}","slice_prefix":"{prep_slice_id}"'
    prompt = (
        f"{prompt_head}\n\n{role}"
        "Re-verify the edits this run's review applied in place, and keep only the ones whose "
        "tests pass. Three steps, in order.\n"
        f"1. List what review changed with ONE engine call: `{invoke} review.reverify "
        f"'{{{base_args}}}'{repo_pin}`, its command word exactly as written (a guard denies the "
        "prefix moved into a shell variable). When a non-empty `session_id:` value is given in "
        'this brief, add it as `"session_id"`. Its `edited` and `created` lists are the files '
        "review changed. If both are empty, return edited [], failing [], reverted [], "
        "status_before pass.\n"
        "2. Run, and only run, the tests covering those files: the test files among them, plus "
        "the test files that import an edited module. Name each test file or node id; never a "
        "directory, a tier (the fast tier included) or the full suite. Report every failing "
        "node id as `failing` and set status_before to pass when none failed, else fail.\n"
        f"3. When any failed, call review.reverify ONCE more in revert mode: `{invoke} review.reverify "
        f"'{{{base_args},\"failing\":[<failing node ids>],\"sidecars\":[<the review_sidecars "
        f"paths below>]}}'{repo_pin}` (add `session_id` as in step 1). Return its `reverted` and "
        "`record` as given, re-run only the tests of the reverted paths, and set status_after to "
        "pass or fail from that re-run. When nothing failed, status_after is null.\n"
        "Revert only through review.reverify: never `git checkout`, `restore`, `stash` or "
        "`reset`, and never edit a file yourself. Return the sidecar path you wrote as "
        "sidecar_path.\n"
        f"plan_path: {plan_path}\n"
        f"run_base_sha: {run_base_sha}"
    ).strip()
    call = _agent_call_literal(
        agent_type,
        prompt,
        REVERIFY_PHASE,
        schema=True,
        as_arrow=False,
        agent_opts={agent_type: {"model": "sonnet"}},
        schema_literal=json.dumps(_REVERIFY_SCHEMA, sort_keys=True),
    )
    prefix = f"agent({_prompt_literal(prompt)}, "
    if not call.startswith(prefix):
        raise ValueError("re-verify agent call does not open with its prompt literal")
    call = f"agent({_prompt_literal(prompt)} + '\\n' + {_REVERIFY_SIDECARS_JS}, " + call[len(prefix):]
    call = sc(call, "reverify", agent_type)
    block = (
        f"  phase({_js_string_literal(REVERIFY_PHASE)});\n"
        f"  const _reviewReverify = _verifyOnly ? null : "
        f"((r) => r ? {{ ...r, unapplied: {_REVERIFY_UNAPPLIED_JS} }} : null)"
        f"(await {_degrading(call)});"
    )
    return REVERIFY_PHASE, block


def compose_execute_review(
    review: ExecuteReview,
    *,
    stage_schemas: Dict[str, dict],
    plan_path: str,
    run_base_sha: str,
    declared_paths: Optional[List[str]] = None,
    declared_paths_js: Optional[str] = None,
    prompt_head: str = "",
    prep_suffix_js: Optional[str] = None,
    criterion: Optional["OperativeCriterion"] = None,
    precredited_rows: Optional[List[str]] = None,
    em_owned_rows: Optional[List[str]] = None,
    run_key: Optional[str] = None,
    host_degraded: bool = False,
    slice_key_js: Optional[str] = None,
    slice_identity_sha: Optional[str] = None,
    owner_rows_js: Optional[str] = None,
    review_terminal_sha: Optional[str] = None,
    tier: Optional[str] = None,
    tier_js: Optional[str] = None,
    executor_rows: Optional[int] = None,
    executor_rows_js: Optional[str] = None,
    tshirt: Optional[str] = None,
    tshirt_js: Optional[str] = None,
    repo_root: Optional[str] = None,
    prep_in_wave: bool = False,
    reverify: bool = False,
    delivery_notes_js: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """Compose the roster-v5 ``execute_review`` wave into ``(phase_title,
    block)`` entries: prep, review-wave, and -- ONLY when ``review.integration``
    is not ``None`` -- integration.

    ``review.integration is None`` is the 2026-09-28 no-integration-pass path
    (step b'): no integration call is emitted at all. Each review-wave
    reviewer applies its own findings in place (the DoE
    ``2026-09-26-retire-review-integrator.md`` contract); the caller
    (``dispatch_emit/emit.py``) is responsible for the post-wave mechanical
    bookkeeping step (``review_mint.wave_bookkeeping``) on that path -- this
    module stays pure (no I/O, no bookkeeping) exactly as before.

    Every call carries its roster ``model``/``effort`` (``None`` for a
    signal-resolved persona -- it inherits its own frontmatter opts,
    Design D6) and a ``schema:`` resolved by name from ``stage_schemas``
    (DoE's ``review-stage.schema.json`` ``$defs``, injected by the caller
    exactly as the roster fragment is -- this module never reads either
    file). An unknown schema name raises ``RosterFragmentError``.

    ``prompt_head`` is the brief-precedence clause (``dispatch_emit/emit.py
    :: _prompt_head`` -- importing ``emit`` here would cycle, since
    ``emit`` imports ``compose``/this module; the caller passes the
    rendered text in), spliced ahead of every composed prompt.

    ``prep_suffix_js`` (only with ``declared_paths_js``) is a JS string
    expression concatenated onto the prep prompt after the declared paths.

    ``run_base_sha`` may carry a prompt marker (``compose.PROMPT_MARKER_DELIM``) naming a script
    variable resolved at fire. The slice id is an identity, not content, so it stays on
    ``slice_identity_sha`` (the emit-time sha) while every prompt reads the fire-time base.

    ``review_terminal_sha`` marks a review-only run: the prep prompt freezes and partitions the
    committed range ``run_base_sha..review_terminal_sha`` and never the working tree. ``None``
    keeps the worktree prompt byte-identical.

    ``tier`` (``lightweight``/``standard``/``full``) omits every review-wave agent whose roster
    ``tiers`` excludes it; ``tier_js`` is a JS expression for a tier known only at fire time,
    which gates those agents at run time instead. An agent without ``tiers`` always runs, and
    neither argument leaves the wave byte-identical. An omitted delivery verifier leaves
    ``_deliveryVerdict`` bound to ``null``. Prep, integration and the judge never filter.

    ``executor_rows`` / ``executor_rows_js`` (exactly one, required while a slice agent carries
    ``executor_rows_per_reviewer``) is the run's executor row count, literal or a fire-time JS
    expression. That agent then fans out ``ReviewAgent.reviewer_count`` reviewers, not one per
    slice: ``_groupSlices`` splits prep's slices into contiguous balanced groups, each reviewer
    owning its whole slice objects. A one-slice group's prompt equals the per-slice prompt.

    ``tshirt`` / ``tshirt_js`` (at most one) is the plan's t-shirt, literal or fire-time; it omits
    or run-time gates every agent whose ``min_tshirt`` exceeds it. An unknown size admits.

    When the roster names no delivery verifier the wave binds ``_deliveryVerdict`` to ``null`` and
    ``_deliveryNotRun`` to ``DELIVERY_NOT_ROSTERED``.

    ``repo_root`` pins the ``prep_in_wave`` prep command to the repo that minted ``run_base_sha``
    with ``--repo``, so a run driven from another repo's cwd resolves the base in the right object
    store. ``None`` leaves the command unpinned.

    ``prep_in_wave`` emits no prep agent and no "Review prep" phase: each review-wave agent runs
    one ``review.prep`` call first (slice agents with their ``group_index`` / ``group_count``,
    whole-diff agents with the whole run) and returns ``{prep, review}``. The op computes once per
    slice id, so the first call freezes and the rest read the stored reply. After the wave
    ``_reviewWave`` holds the unwrapped reviews, ``_reviewPrep`` is bound from the replies, and the
    no-op, no-slices and verify-only outcomes are decided from it with the same halt keys and
    texts. Slice agents fan out their ratio's reviewer count up front, so every one must carry
    ``executor_rows_per_reviewer``; a group with no slices returns no review. Prompts are the full
    briefs, since the diff size is unknown until the wave has run, and whole-diff reviewers are
    not skipped on a verify-only run. ``False`` keeps the output byte-identical.

    ``reverify`` appends a "Review re-verify" phase after the last applying stage, binding
    ``_reviewReverify`` (``null`` under ``_verifyOnly`` or when the agent call degrades): a
    ``coordinator:test-runner`` agent lists the review-edited files with ``review.reverify``, runs
    the tests covering them, and reverts the implicated files when any fail. ``False`` keeps the
    output byte-identical.

    ``slice_key_js`` is a JS expression joined into the frozen-diff slice id at run time, for a
    function composed once and called once per item (emit-wave-fire's per-baton
    ``executeReview``): without it every concurrent call freezes under one static id and all but
    the first collide and fail closed.

    1. **prep** -- one call bound to ``_reviewPrep``; prompt carries
       ``plan_path``, ``run_base_sha`` and ``declared_paths``.
    2. **review-wave** -- ONE ``parallel([...])`` holding
       ``...(_reviewPrep?.slices ?? []).map(s => () => agent(...))`` for
       each ``per: slice`` agent (the slice object appended via
       ``JSON.stringify(s)`` at run time, string-concatenated so no
       template literal is needed) plus one call per ``per: whole-diff``
       agent. The ``coordinator:delivery-verifier`` result is re-bound to
       ``_deliveryVerdict`` by its FIXED position counted from the array's
       END: the slice calls precede the whole-diff calls and expand to a
       runtime-variable count, so only an end-anchored offset is static at
       compose time.
    3. **integration** -- one call bound to ``_reviewIntegration``.
    """
    if (declared_paths is None) == (declared_paths_js is None):
        raise ValueError(
            "compose_execute_review takes exactly one of declared_paths / "
            "declared_paths_js"
        )
    phases: List[Tuple[str, str]] = []

    # -- 1. prep ----------------------------------------------------------
    prep_phase = "Review prep"
    if slice_key_js and run_key:
        raise ValueError("compose_execute_review takes at most one of run_key / slice_key_js")
    sidecar_key = run_key or Path(plan_path.replace("\\", "/")).stem or "run"

    def _sc(call: str, stage: str, agent_type: str, label_js: Optional[str] = None) -> str:
        """Append this run's sidecar clause to one finished agent call."""
        label = f"{stage}-{agent_type}"
        full_js = None if label_js is None else f"{_js_string_literal(label + '-')} + {label_js}"
        return with_sidecar_clause(call, "review", sidecar_key, label, label_js=full_js)

    prep_slice_id = prep_slice_id_for(
        plan_path,
        slice_identity_sha if slice_identity_sha is not None else run_base_sha,
        _SLICE_KEY_SENTINEL if slice_key_js else run_key,
    )
    credit_note = (
        "\nDelivered before this run's base (resume): rows "
        + ", ".join(precredited_rows)
        + " are `coded` at a commit that is an ancestor of run_base_sha; the diff holds no hunk "
        "for them by construction. Count them backed; never list them in claims_unbacked."
        + (_ANCESTRY_RULE if review_terminal_sha else "")
        if precredited_rows
        else ""
    ) + em_owned_clause(em_owned_rows)
    base_ref = run_base_sha or "run_base_sha"
    if review_terminal_sha:
        freeze_clause = (
            f"`freeze-review-diff --range {base_ref}..{review_terminal_sha} --slice-id "
            f"{prep_slice_id} --paths <every declared path>`. This run reviews committed work "
            f"only: the diff is {base_ref}..{review_terminal_sha}, never the working tree, so "
            f"a peer's uncommitted file is never in it."
        )
        foreign_range = f"{base_ref}..{review_terminal_sha}"
        partition_args = (
            f"{{\"base\":\"{base_ref}\",\"head\":\"{review_terminal_sha}\","
            f"\"slice_prefix\":\"{prep_slice_id}\",\"paths\":[<every declared path>]}}"
        )
    else:
        freeze_clause = (
            f"`freeze-review-diff --worktree --range {base_ref} --slice-id {prep_slice_id} "
            f"--paths <every declared path>`. --worktree is mandatory: the run's rows "
            f"land UNCOMMITTED in the working tree and a peer may commit meanwhile, so "
            f"`git diff base..HEAD` is never the run's diff."
        )
        foreign_range = f"{base_ref}..HEAD"
        partition_args = (
            f"{{\"worktree\":true,\"base\":\"{base_ref}\","
            f"\"slice_prefix\":\"{prep_slice_id}\",\"paths\":[<every declared path>]}}"
        )
    prep_type, prep_role = _host_native(review.prep.agent_type, host_degraded)
    prep_prompt = (
        f"{prompt_head}\n\n{prep_role}"
        f"Freeze and characterise this run's diff for review. Freeze it with the "
        f"`freeze-review-diff` launcher on PATH (the settings-home bin; the "
        f"`review.freeze_diff` op's entrypoint -- this repo need not carry "
        f"coordinator/bin, so never look for it here): "
        f"{freeze_clause} Return its single stdout "
        f"line as whole_diff_path, verbatim; never write or edit a diff yourself. Report "
        f"product_files as the `product_files: N` line the launcher prints on stderr, verbatim; "
        f"never a count of your own. Under "
        f"foreign_claims list ONLY declared paths that a commit in "
        f"{foreign_range} also changed: a peer landed inside this "
        f"run's footprint. Peers' work elsewhere in the shared tree is normal and is never "
        f"a foreign claim, and so is any write by this run itself: its executors', reviewers' "
        f"and EM's own commits, and every `.coordinator-local/subagent-share/` sidecar. Count a "
        f"commit foreign only when a live peer session claims the path "
        f"(`session-claim-cli who-claims-path <path>`, on the settings-home bin); "
        f"an empty list is the usual answer. Take the verdict and the slices from ONE engine call, "
        f"never your own grouping: `\"${{COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}}/bin/coordinator-invoke\" "
        f"review.partition_slices '{partition_args}'`, its "
        f"command word exactly as written (a guard denies the prefix moved into a shell variable) -- it returns "
        f"verdict, product_paths and slices already frozen as [{{slice_id, paths, diff_path}}]. "
        f"Report its verdict as verdict. When the verdict is single-reviewer-ok its slices is empty: "
        f"return exactly ONE slice spanning every product file, with diff_path equal to whole_diff_path. "
        f"When it is PARTITION-MANDATORY, return one slice per returned slice (id = slice_id, files = paths, "
        f"diff_path as returned, sidecar_path = whole_diff_sidecars.personas[0]); freeze nothing "
        f"further. A non-null error in its result is a refusal: put it verbatim in your result and return "
        f"no slices. Every launcher named here is on PATH; never report a missing binary in place of slices. "
        f"slices is never empty while product_files > 0.\n"
        f"plan_path: {plan_path}\n"
        f"run_base_sha: {run_base_sha}\n"
        f"declared_paths:{'' if declared_paths_js else ' ' + ', '.join(declared_paths)}"
    ).strip()
    prep_call = _agent_call_literal(
        prep_type,
        prep_prompt,
        prep_phase,
        schema=True,
        as_arrow=False,
        agent_opts=_agent_opts_for(review.prep, emitted_agent_type=prep_type),
        schema_literal=_schema_literal(review.prep.schema, stage_schemas),
    )
    if declared_paths_js:
        prep_prefix = f"agent({_prompt_literal(prep_prompt)}, "
        if not prep_call.startswith(prep_prefix):
            raise ValueError("prep agent call does not open with its prompt literal")
        prep_call = (
            f"agent({_prompt_literal(prep_prompt)} + ' ' + "
            f"JSON.stringify({declared_paths_js})"
            + (f" + {prep_suffix_js}" if prep_suffix_js else "")
            + ", "
            + prep_call[len(prep_prefix):]
        )
    prep_call = _sc(prep_call, "prep", prep_type)
    if not prep_in_wave:
        phases.append(
            (
                prep_phase,
                f"  phase({_js_string_literal(prep_phase)});\n"
                f"  const _reviewPrep = await {_degrading(prep_call)};\n" + _PREP_GUARDS_JS,
            )
        )

    # -- 2. review-wave -----------------------------------------------------
    wave_phase = "Review wave"
    if tier is not None and tier_js is not None:
        raise ValueError("compose_execute_review takes at most one of tier / tier_js")
    if tier is not None and tier not in VALID_TIERS:
        raise ValueError(f"tier {tier!r} is not one of {sorted(VALID_TIERS)!r}")

    def _tier_gate(agent: ReviewAgent) -> Optional[str]:
        """JS test that ``agent`` runs at the fire-time tier; ``None`` when ungated."""
        if tier_js is None or agent.tiers is None:
            return None
        return f"{json.dumps(list(agent.tiers))}.includes({tier_js})"

    if tshirt is not None and tshirt_js is not None:
        raise ValueError("compose_execute_review takes at most one of tshirt / tshirt_js")
    if (executor_rows is None) == (executor_rows_js is None) and any(
        a.per == "slice" and a.executor_rows_per_reviewer is not None for a in review.review_wave
    ):
        raise ValueError(
            "compose_execute_review takes exactly one of executor_rows / executor_rows_js "
            "when a review-wave slice agent carries executor_rows_per_reviewer"
        )
    rows_expr = str(executor_rows) if executor_rows is not None else f"({executor_rows_js})"

    def _tshirt_gate(agent: ReviewAgent) -> Optional[str]:
        """JS test that ``agent`` is admitted at the fire-time t-shirt; an unknown size excludes."""
        if tshirt_js is None or agent.min_tshirt is None:
            return None
        admitted = [t for t in _TSHIRT_ORDER if agent.admits_tshirt(t)]
        return f"{json.dumps(admitted)}.includes({tshirt_js})"

    def _agent_gate(agent: ReviewAgent) -> Optional[str]:
        gates = [g for g in (_tier_gate(agent), _tshirt_gate(agent)) if g]
        return " && ".join(gates) if gates else None

    def _runs_at_compile_tier(agent: ReviewAgent) -> bool:
        # A fire-time t-shirt is judged by `_tshirt_gate`, never at compose time.
        return agent.runs_at(tier) and (tshirt_js is not None or agent.admits_tshirt(tshirt))

    all_whole_diff_agents = [a for a in review.review_wave if a.per != "slice"]
    slice_agents = [a for a in review.review_wave if a.per == "slice" and _runs_at_compile_tier(a)]
    whole_diff_agents = [a for a in all_whole_diff_agents if _runs_at_compile_tier(a)]

    item_lines: List[str] = []
    wave_mode_args = f"\"head\":\"{review_terminal_sha}\"" if review_terminal_sha else "\"worktree\":true"
    wave_tail_js = (
        (f" + ' ' + JSON.stringify({declared_paths_js})" if declared_paths_js else "")
        + (f" + {prep_suffix_js}" if declared_paths_js and prep_suffix_js else "")
    )
    for agent in slice_agents:
        slice_type, slice_role = _host_native(agent.agent_type, host_degraded)
        slice_bc = _brief_conformance_clause(criterion) if _is_code_reviewer(agent.agent_type) else ""
        notes_expr = _delivery_notes_expr(delivery_notes_js, agent.agent_type)
        if prep_in_wave:
            if agent.executor_rows_per_reviewer is None:
                raise ValueError(
                    "compose_execute_review(prep_in_wave=True) needs executor_rows_per_reviewer on "
                    f"slice agent {agent.agent_type!r}: its reviewer count is fixed before prep runs"
                )
            min_rows, max_rows = agent.executor_rows_per_reviewer
            slice_prompt = (
                f"{prompt_head}\n\n{slice_role}"
                f"Review your assigned slice group of this run's diff.\n"
                + _wave_prep_instruction(wave_mode_args, base_ref, prep_slice_id, repo_root, grouped=True)
                + f" Review exactly the slices the reply lists, and nothing else. If it lists none, "
                f"there is nothing in your group: return `prep` alone.{_WAVE_GROUP_CLAUSE}{slice_bc}\n"
                f"plan_path: {plan_path}\n"
                f"run_base_sha: {run_base_sha}\n"
                f"declared_paths:{'' if declared_paths_js else ' ' + ', '.join(declared_paths)}"
            ).strip()
            slice_call = _agent_call_literal(
                slice_type,
                slice_prompt,
                wave_phase,
                schema=True,
                as_arrow=False,
                agent_opts=_agent_opts_for(agent, emitted_agent_type=slice_type),
                schema_literal=_wave_schema_literal(_schema_literal(agent.schema, stage_schemas)),
            )
            slice_prefix = f"agent({_prompt_literal(slice_prompt)}, "
            assert slice_call.startswith(slice_prefix)
            grouped_call = (
                f"() => agent({_prompt_literal(slice_prompt)}{wave_tail_js}{' + ' + notes_expr if notes_expr else ''} + "
                f"{_js_string_literal(chr(10) + 'group_index: ')} + i + "
                f"{_js_string_literal(chr(10) + 'group_count: ')} + n, " + slice_call[len(slice_prefix):]
            )
            grouped_call = _sc(grouped_call, "slice", slice_type, "i")
            count_js = (
                f"Math.max(1, Math.min({MAX_SLICES}, Math.ceil({rows_expr} / {max_rows}), "
                f"Math.floor({rows_expr} / {min_rows})))"
            )
            slice_items = (
                f"(() => {{ const n = {count_js}; "
                f"return Array.from({{ length: n }}, (_, i) => {_degrading(grouped_call)}); }})()"
            )
            gate = _agent_gate(agent)
            item_lines.append(f"    ...({gate} ? {slice_items} : [])" if gate else f"    ...{slice_items}")
            continue
        base_prompt = (
            f"{prompt_head}\n\n{slice_role}"
            f"Review your assigned slice of this run's diff.\n"
            f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
            f"{slice_bc}"
        ).strip()
        lean_prompt = (
            f"{prompt_head}\n\n{slice_role}"
            f"Review your assigned slice of this run's diff.\n{_LEAN_READ_CLAUSE}\n"
            f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
            f"{slice_bc}"
        ).strip()
        call = _agent_call_literal(
            slice_type,
            base_prompt,
            wave_phase,
            schema=True,
            as_arrow=False,
            agent_opts=_agent_opts_for(agent, emitted_agent_type=slice_type),
            schema_literal=_schema_literal(agent.schema, stage_schemas),
        )
        # Splice the runtime slice object in via string concatenation
        # (never a template literal): `agent(<static> + '\n\nSlice: ' +
        # JSON.stringify(s), opts)`.
        prefix = f"agent({_prompt_literal(base_prompt)}, "
        assert call.startswith(prefix)
        spliced_call = (
            f"agent({_size_gated(base_prompt, lean_prompt)}{' + ' + notes_expr if notes_expr else ''} + "
            f"{_js_string_literal(chr(10) + chr(10) + 'Slice: ')} + "
            f"JSON.stringify(s), " + call[len(prefix):]
        )
        if agent.executor_rows_per_reviewer is not None:
            suffix = (
                f"(g.length === 1 ? {_js_string_literal(chr(10) + chr(10) + 'Slice: ')} + "
                f"JSON.stringify(g[0]) : {_js_string_literal(chr(10) + chr(10) + 'Slices: ')} + "
                f"JSON.stringify(g) + {_js_string_literal(_GROUPED_SLICES_CLAUSE)})"
            )
            single_suffix = f"{_js_string_literal(chr(10) + chr(10) + 'Slice: ')} + JSON.stringify(s), "
            grouped_call = _sc(spliced_call.replace(single_suffix, f"{suffix}, ", 1), "slice", slice_type)
            slice_items = (
                f"_groupSlices({REVIEW_PREP_SLICES_JS}, {rows_expr}, "
                f"{agent.executor_rows_per_reviewer[0]}, {agent.executor_rows_per_reviewer[1]}).map(g => () => {_degrading(grouped_call)})"
            )
        else:
            single_call = _sc(spliced_call, "slice", slice_type, "s.slice_id")
            slice_items = f"{REVIEW_PREP_SLICES_JS}.map(s => () => {_degrading(single_call)})"
        gate = _agent_gate(agent)
        item_lines.append(f"    ...({gate} ? {slice_items} : [])" if gate else f"    ...{slice_items}")

    for agent_idx, agent in enumerate(whole_diff_agents):
        is_delivery_verifier = agent.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE
        # Slice count is runtime, so a slot is recorded as a negative offset from the end of
        # `_reviewWave`; the block rewrites it to an index after the wave.
        slot_js = str(agent_idx - len(whole_diff_agents))

        def _not_run_expr(reason_js: str, _agent_type: str = agent.agent_type, _slot: str = slot_js) -> str:
            return _not_run_entry_js(_slot, _agent_type, reason_js).rstrip(";")

        degrade_on_error = (
            _DELIVERY_REJECTED_JS
            if is_delivery_verifier
            else _not_run_expr(f"{_js_string_literal(_WAVE_REJECTED)} + String(e && e.message || e)") + ";"
        )
        role_note = (
            f"\n\n{_DELIVERY_VERIFIER_ROLE_PREAMBLE}{delivery_supersession_clause(criterion)}"
            if is_delivery_verifier
            else (_brief_conformance_clause(criterion) if _is_code_reviewer(agent.agent_type) else "")
        )
        whole_type, whole_role = (
            (agent.agent_type, "")
            if is_delivery_verifier
            else _host_native(agent.agent_type, host_degraded)
        )
        wave_prompt = (
            f"{prompt_head}\n\n{whole_role}"
            f"Review this run's whole diff.\n"
            + (
                f"Read only the frozen diff named by whole_diff_path below (this run's is "
                f"`{prep_slice_id}.diff`); never another plan's `-prep.diff`.\n"
                if is_delivery_verifier
                else ""
            )
            + f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
            f"{credit_note if is_delivery_verifier else ''}"
            f"{role_note}"
        ).strip()
        lean_wave_prompt = (
            f"{prompt_head}\n\n{whole_role}"
            f"Review this run's whole diff.\n{_LEAN_READ_CLAUSE}\n"
            f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
            f"{credit_note if is_delivery_verifier else ''}"
            f"{role_note}"
        ).strip()
        emitted_agent_type = (
            _DELIVERY_VERIFIER_HOST_NATIVE_TYPE if is_delivery_verifier else whole_type
        )
        if prep_in_wave:
            wave_prompt = (
                f"{prompt_head}\n\n{whole_role}"
                f"Review this run's whole diff.\n"
                + _wave_prep_instruction(wave_mode_args, base_ref, prep_slice_id, repo_root, grouped=False)
                + f" Read the frozen diff named by the reply's whole_diff_path (this run's is "
                f"`{prep_slice_id}.diff`); never another plan's `-prep.diff`."
                + (
                    " Your sidecar_path is the reply's whole_diff_sidecars.delivery."
                    if is_delivery_verifier
                    else " If the reply's product_files is 0 and no slice lists files, there is "
                    "nothing to review: return `prep` alone."
                )
                + f"\nplan_path: {plan_path}\n"
                f"run_base_sha: {run_base_sha}"
                f"{credit_note if is_delivery_verifier else ''}"
                f"{role_note}"
                f"\ndeclared_paths:{'' if declared_paths_js else ' ' + ', '.join(declared_paths)}"
            ).strip()
            call = _agent_call_literal(
                emitted_agent_type,
                wave_prompt,
                wave_phase,
                schema=True,
                as_arrow=True,
                agent_opts=_agent_opts_for(agent, emitted_agent_type=emitted_agent_type),
                schema_literal=_wave_schema_literal(_schema_literal(agent.schema, stage_schemas)),
            )
            prefix = f"() => agent({_prompt_literal(wave_prompt)}, "
            assert call.startswith(prefix)
            owner = (
                f" + {owner_repo_clause_js(owner_rows_js)}"
                if is_delivery_verifier and owner_rows_js
                else ""
            )
            call = f"() => agent({_prompt_literal(wave_prompt)}{wave_tail_js}{owner}, " + call[len(prefix):]
            call = _sc(call, "whole", emitted_agent_type)
            line = _degrading(call, degrade_on_error)
            gate = _agent_gate(agent)
            if gate:
                skipped = (
                    f"({_DELIVERY_REASON_VAR} = 'tier ' + String({tier_js}) + ' excludes {_DELIVERY_VERIFIER_AGENT_TYPE}', null)"
                    if is_delivery_verifier
                    else "("
                    + _not_run_expr(
                        f"{_js_string_literal(_WAVE_SKIPPED + 'tier ')} + String({tier_js}) + "
                        f"{_js_string_literal(' excludes ' + agent.agent_type)}"
                    )
                    + ", null)"
                )
                line = f"() => ({gate} ? ({line})() : {skipped})"
            item_lines.append(f"    {line}")
            continue
        call = _agent_call_literal(
            emitted_agent_type,
            wave_prompt,
            wave_phase,
            schema=True,
            as_arrow=True,
            agent_opts=_agent_opts_for(agent, emitted_agent_type=emitted_agent_type),
            schema_literal=_schema_literal(agent.schema, stage_schemas),
        )
        # A whole-diff agent is handed the frozen diff by path: the wave prompt
        # is composed before the prep runs, so the pointer is spliced at run
        # time (as the slice object is). Without it the delivery verifier has
        # only a base sha and no diff to check claims against, and FAILs.
        prefix = f"() => agent({_prompt_literal(wave_prompt)}, "
        assert call.startswith(prefix)
        frozen = (
            "'\\n\\nwhole_diff_path: ' + (_reviewPrep?.whole_diff_path ?? 'NONE -- prep froze no diff')"
            + (
                " + '\\nsidecar_path: ' + (_reviewPrep?.whole_diff_sidecars?.delivery ?? 'NONE')"
                if is_delivery_verifier
                else ""
            )
            + (
                f" + {owner_repo_clause_js(owner_rows_js)}"
                if is_delivery_verifier and owner_rows_js
                else ""
            )
        )
        call = f"() => agent({_size_gated(wave_prompt, lean_wave_prompt)} + {frozen}, " + call[len(prefix):]
        call = _sc(call, "whole", emitted_agent_type)
        degraded = _degrading(call, degrade_on_error)
        line = (
            degraded
            if is_delivery_verifier
            else f"() => _verifyOnly ? ({_not_run_expr(_js_string_literal(_WAVE_SKIPPED + 'verify-only'))}, null) : ({degraded})()"
        )
        gate = _agent_gate(agent)
        if gate:
            skipped = (
                f"({_DELIVERY_REASON_VAR} = 'tier ' + String({tier_js}) + ' excludes {_DELIVERY_VERIFIER_AGENT_TYPE}', null)"
                if is_delivery_verifier
                else "("
                + _not_run_expr(
                    f"{_js_string_literal(_WAVE_SKIPPED + 'tier ')} + String({tier_js}) + "
                    f"{_js_string_literal(' excludes ' + agent.agent_type)}"
                )
                + ", null)"
            )
            line = f"() => ({gate} ? ({line})() : {skipped})"
        item_lines.append(f"    {line}")

    map_lines = ",\n".join(item_lines)

    delivery_index = next(
        (
            i
            for i, agent in enumerate(whole_diff_agents)
            if agent.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE
        ),
        None,
    )

    wave_lines = [f"  phase({_js_string_literal(wave_phase)});"]
    if not prep_in_wave and any(a.executor_rows_per_reviewer is not None for a in slice_agents):
        wave_lines.append(_GROUP_SLICES_JS)
    if delivery_index is not None:
        wave_lines.append(f"  let {_DELIVERY_REASON_VAR} = null;")
    tracked = [
        (i - len(whole_diff_agents), a)
        for i, a in enumerate(whole_diff_agents)
        if a.agent_type != _DELIVERY_VERIFIER_AGENT_TYPE
    ]
    if tracked or slice_agents:
        wave_lines.append(f"  {WAVE_NOT_RUN_VAR} = [];")
    if prep_in_wave:
        wave_lines.append(f"  const _waveRaw = await parallel([\n{map_lines}\n  ]);")
        wave_lines.append("  const _reviewWave = _waveRaw.map(r => (r ? (r.review ?? null) : null));")
    else:
        wave_lines.append(f"  const _reviewWave = await parallel([\n{map_lines}\n  ]);")
    if tracked:
        for slot, a in tracked:
            wave_lines.append(
                f"  if (!_reviewWave[_reviewWave.length - {-slot}] && "
                f"!{WAVE_NOT_RUN_VAR}.some(n => n.slot === {slot})) {{ "
                + _not_run_entry_js(str(slot), a.agent_type, _js_string_literal(_WAVE_NO_REVIEW))
                + " }"
            )
        wave_lines.append(f"  {WAVE_NOT_RUN_VAR}.forEach(n => {{ n.slot += _reviewWave.length; }});")
    if slice_agents:
        slice_types = "+".join(dict.fromkeys(a.agent_type for a in slice_agents))
        empty_group = (
            "(_waveRaw[i] && _waveRaw[i].prep && !_waveRaw[i].prep.error && "
            "!(Array.isArray(_waveRaw[i].prep.slices) ? _waveRaw[i].prep.slices : [{}]).length)"
            if prep_in_wave
            else "false"
        )
        wave_lines.append(
            f"  for (let i = 0; i < _reviewWave.length - {len(whole_diff_agents)}; i++) {{ "
            f"if (!_reviewWave[i]) {{ "
            + _not_run_entry_js(
                "i",
                slice_types,
                f"({empty_group} ? {_js_string_literal(_WAVE_SKIPPED + 'no slices in group')} "
                f": {_js_string_literal(_WAVE_NO_REVIEW)})",
            )
            + " } }"
        )
    if prep_in_wave:
        wave_lines.append(_PREP_FROM_WAVE_JS + _PREP_GUARDS_JS)
    if delivery_index is not None:
        offset_from_end = len(whole_diff_agents) - delivery_index
        wave_lines.append(
            f"  const _deliveryVerdict = _reviewWave[_reviewWave.length - {offset_from_end}];"
        )
        wave_lines.append(
            f"  const _deliveryNotRun = _deliveryVerdict ? null : ({_DELIVERY_REASON_VAR} ?? 'returned no result');"
        )
    elif any(a.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE for a in all_whole_diff_agents):
        wave_lines.append("  const _deliveryVerdict = null;")
        wave_lines.append(
            "  const _deliveryNotRun = "
            + _js_string_literal(f"tier {tier} excludes {_DELIVERY_VERIFIER_AGENT_TYPE}")
            + ";"
        )
    else:
        wave_lines.append("  const _deliveryVerdict = null;")
        wave_lines.append(f"  const _deliveryNotRun = {_js_string_literal(DELIVERY_NOT_ROSTERED)};")
    phases.append((wave_phase, "\n".join(wave_lines)))

    # -- 3. integration (only when the fragment declares one stage) --------
    if review.integration is not None:
        integration_phase = "Review integration"
        integration_type, integration_role = _host_native(
            review.integration.agent_type, host_degraded
        )
        integration_prompt = (
            f"{prompt_head}\n\n{integration_role}"
            f"Integrate this run's review wave into one residue pass. This run's "
            f"declared paths are already registered as your session's confined-"
            f"reviewer review targets -- Edit on any of them is sanctioned, not "
            f"confined to your own sidecar. Read every review-wave sidecar's "
            f"Findings Ledger, apply every outstanding finding in place in the "
            f"named file (nits included; the sole exemption is a finding you "
            f"believe is wrong, said out loud and defended), then write and "
            f"verify your own residue ledger.\n"
            f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
        ).strip()
        integration_call = _agent_call_literal(
            integration_type,
            integration_prompt,
            integration_phase,
            schema=True,
            as_arrow=False,
            agent_opts=_agent_opts_for(review.integration, emitted_agent_type=integration_type),
            schema_literal=_schema_literal(review.integration.schema, stage_schemas),
        )
        integration_call = _sc(integration_call, "integration", integration_type)
        phases.append(
            (
                integration_phase,
                f"  phase({_js_string_literal(integration_phase)});\n"
                f"  const _reviewIntegration = _verifyOnly ? null : await {_degrading(integration_call)};",
            )
        )

    if reverify:
        phases.append(
            _reverify_phase(
                base_ref=base_ref,
                prep_slice_id=prep_slice_id,
                repo_root=repo_root,
                plan_path=plan_path,
                run_base_sha=run_base_sha,
                prompt_head=prompt_head,
                host_degraded=host_degraded,
                sc=_sc,
            )
        )

    if slice_key_js:
        # Every occurrence sits inside a single-quoted prompt literal, so splicing closes and
        # reopens that literal around the runtime key.
        spliced = (
            "' + String(" + slice_key_js + ").replace(/[^A-Za-z0-9_.-]/g, '-') + '"
        )
        phases = [(title, block.replace(_SLICE_KEY_SENTINEL, spliced)) for title, block in phases]
    return phases


#: The judge's independence contract: it adjudicates from the artifacts that
#: define "done", never from the run's own account of itself.
_JUDGE_PREAMBLE = (
    "Judge whether this run met its plan's exit criteria. Weigh evidence in this ranked "
    "order; a lower tier never outranks a higher one. "
    "TIER 1, the human's verbatims: the claimed requirement rows (verbatim source text, "
    "anchor, surface) and the PM's own words on the sizing (the plan's "
    "execution_authorized_note, its sizing object's intent, exit_criterion.accepted.pm_quote "
    "and amendments, any '## PM brief' section). An APM ruling is the APM's, never the PM's. "
    "TIER 2, the exit criteria: the operative prime_exit_criterion first, then "
    "gated_exit_criteria. "
    "TIER 3, the plan's body and the working tree against run_base_sha: read, but it never "
    "outranks tier 1 or 2. "
    "Inputs are by path unless inlined below. "
    "The run's product is uncommitted by design: dispatch.land_run commits the working "
    "tree after you return. Judge the working tree as the state about to be committed; a "
    "clause asking for something committed is met by it being present there, and an "
    "untracked deliverable is never a reason for not_met. A clause asking for the landed sha "
    "to be reported, or a memo naming it, is a post-commit act that dispatch.land_run "
    "discharges or reports as owed, and is never a reason for not_met. "
    "Do not read executor reports, review sidecars or their prose: the run does not "
    "certify itself. Every 'met' names the command you ran or the path you read. When "
    "the plan records a falsifier, run it as recorded; it is not yours to replace. "
    "Return 'indeterminate' when the evidence does not settle the criterion. Report "
    "the REQUIRED boolean `differs_from_baseline`: true only if what you observed "
    "differs from the baseline in the way the criterion describes, false otherwise "
    "(including when it matches the baseline), and copy the baseline you compared against as `baseline_output` "
    "(empty string if none). The run's verdict is computed from that boolean; "
    "`status` must agree with it. When claimed requirement rows are listed, also return "
    "`register_rows`: one {id, claim, status, wired, surface, observed_ref} per row you weighed, "
    "`claim` this-plan for a row this plan claims; `status` met only when the work is done, "
    "`wired` true only when a user-reachable entry point on the row's surface invokes it, and "
    "`observed_ref` naming that entry point. "
    "When `status` is met, also return `completion_summary` (at most three sentences: what "
    "shipped, why it matters, what the EM must still do), optional `lesson_candidates` (each "
    "{title, body, change_kind, target_wiki}, only for a lesson this run demonstrated), and "
    "`authored_by: terminal-judge`; omit all three when `status` is not met. "
    "A clause you cannot observe because this run's guard denies the leg (a Playwright/e2e "
    "suite, say) is not met on assertion: report it indeterminate and name the door "
    "`coordinator-invoke criterion_evidence.record '{\"plan_path\":..., \"clause\":..., "
    "\"command\":..., \"output_path\":...}'`, which the EM uses to record the command it ran "
    "and its raw output. When the sidecar holds `criterion_evidence` entries, weigh each as "
    "observed evidence (command plus output), never as the EM's say-so; the verdict stays yours."
)


def _tier1_clause(evidence: Optional[JudgeEvidence]) -> str:
    """Tier 1 evidence inlined verbatim; a pointer to the sizing register when none is given."""
    if not evidence:
        return (
            "\nTIER 1 (pointer): the plan's sizing object (its sizing_object field) holds the "
            "requirement_register rows this plan claims and the PM's quotes; read them there."
        )
    out = ["\nTIER 1 evidence, verbatim:"]
    for row in evidence.rows:
        out.append(
            f"claimed row id={row.get(_rr.ROW_ID)} surface={row.get(_rr.ROW_SURFACE)} "
            f"anchor={row.get(_rr.ROW_ANCHOR)}\ntext:\n{row.get(_rr.ROW_TEXT)}"
        )
    for source, row_id, quote in evidence.rulings:
        out.append(f"ruling by the {source.upper()} on row {row_id}:\n{quote}")
    for label, quote in evidence.pm_words:
        out.append(f"PM words ({label}):\n{quote}")
    if evidence.uncovered_turns:
        out.append("PM turns no register row covers:")
        out.extend(f"turn {turn}: {head}" for turn, head in evidence.uncovered_turns)
    return "\n".join(out)


#: The criterion-block key carrying clauses a withheld row delivers: open, never met.
WITHHELD_CLAUSES_KEY = "withheld_clauses"

ROOTS_READ_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "root"],
        "properties": {
            "id": {"type": "string", "maxLength": 100},
            "root": {"type": "string", "maxLength": 500},
        },
    },
}


def _widen_judge_schema(schema_literal: str) -> str:
    """The judge's roster schema plus the two fields the terminal verdict is
    computed from, matching ``dispatch_emit.emit._falsifier_schema_literal`` so
    both producers of ``_falsifierResult`` share one result shape. ``register_rows``
    is DoE's own (review-stage 1.6.0); it is filled in only for an older plugin
    whose schema predates it."""
    schema = json.loads(schema_literal)
    properties = schema.setdefault("properties", {})
    properties.update(
        {
            "differs_from_baseline": {"type": "boolean"},
            "baseline_output": {"type": "string", "maxLength": 300},
        }
    )
    properties.setdefault("roots_read", ROOTS_READ_SCHEMA)
    properties.setdefault(
        _rr.JUDGE_ROWS_KEY,
        {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_rr.JUDGE_ROW_KEYS),
                "properties": {
                    "id": {"type": "string", "maxLength": 100},
                    _rr.JUDGE_CLAIM: {"type": "string", "enum": list(_rr.CLAIMS)},
                    "status": {"type": "string", "enum": list(_rr.STATUSES)},
                    "wired": {"type": "boolean"},
                    "surface": {"type": "string", "enum": list(_rr.SURFACES)},
                    _rr.JUDGE_OBSERVED_REF: {"type": "string", "maxLength": 300},
                    _rr.JUDGE_RULING_REF: {"type": "string", "maxLength": 300},
                },
            },
        },
    )
    # DoE's terminal-judge-result shape (coordinator-content-repo e2cba4ba2a); filled in only for an older
    # plugin whose schema predates it, so the judge's fields are never a schema reject.
    _lesson_props = ("title", "body", "change_kind", "target_wiki")
    properties.setdefault("completion_summary", {"type": "string", "maxLength": 600})
    properties.setdefault(
        "lesson_candidates",
        {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_lesson_props),
                "properties": {k: {"type": "string"} for k in _lesson_props},
            },
        },
    )
    properties.setdefault("authored_by", {"type": "string", "enum": ["terminal-judge"]})
    properties.setdefault(
        WITHHELD_CLAUSES_KEY,
        {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["clause", "rows"],
                "properties": {
                    "clause": {"type": "string", "maxLength": 200},
                    "rows": {"type": "array", "items": {"type": "string", "maxLength": 100}},
                    "register_rows": {"type": "array", "items": {"type": "string", "maxLength": 100}},
                },
            },
        },
    )
    schema["required"] = sorted(
        {*schema.get("required", []), "differs_from_baseline", "baseline_output"}
    )
    return json.dumps(schema, sort_keys=True)


#: The judge agent's own phase label; the emitter lists it in meta.phases.
CRITERION_JUDGE_PHASE_TITLE = "Criterion judge"


@dataclass(frozen=True)
class OperativeCriterion:
    """The criterion statement a judge is held to. ``superseded`` is the plan's
    own statement when a sizing amendment replaced it, else ``None``."""

    statement: str
    superseded: Optional[str] = None


def _one_line(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def resolve_operative_criterion(
    plan_text: str, repo_root: Optional[Path]
) -> Optional[OperativeCriterion]:
    """The single resolver for the criterion text any judge reads.

    The plan's ``prime_exit_criterion.statement``, unless its ``derived_from``
    names a sizing whose ``exit_criterion.amendments`` is non-empty: then the
    latest amendment's statement is operative and the plan's is superseded.
    Fail-soft to ``None`` / the plan's statement on any unreadable input.
    """
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    try:
        doc = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    block = doc.get("prime_exit_criterion") if isinstance(doc, dict) else None
    if not isinstance(block, dict):
        return None
    original = _one_line(block.get("statement"))
    derived = block.get("derived_from")
    if repo_root is not None and isinstance(derived, str) and record_homes.home_pattern("sizings").match(derived):
        try:
            root = Path(repo_root).resolve()
            target = (root / derived.strip()).resolve()
            target.relative_to(root)
            sizing = yaml.safe_load(target.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, yaml.YAMLError):
            sizing = None
        crit = sizing.get("exit_criterion") if isinstance(sizing, dict) else None
        amendments = crit.get("amendments") if isinstance(crit, dict) else None
        if isinstance(amendments, list) and amendments:
            last = amendments[-1]
            latest = _one_line(last.get("statement")) if isinstance(last, dict) else None
            if latest:
                return OperativeCriterion(latest, original if original != latest else None)
    return OperativeCriterion(original) if original else None


def resolve_operative_criterion_for_plan(
    plan_path: str, repo_root: Optional[Path]
) -> Optional[OperativeCriterion]:
    """``resolve_operative_criterion`` over the plan file at ``plan_path``
    (relative paths resolve against ``repo_root``); ``None`` when unreadable."""
    path = Path(plan_path)
    if not path.is_absolute() and repo_root is not None:
        path = Path(repo_root) / path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return resolve_operative_criterion(text, repo_root)


def _criterion_clause(criterion: Optional[OperativeCriterion]) -> str:
    if criterion is None or criterion.superseded is None:
        return ""
    return (
        f"\noperative exit criterion (a sizing amendment; judge against THIS): {criterion.statement}"
        f"\nsuperseded, the plan's original prime_exit_criterion.statement (do not judge against it): {criterion.superseded}"
    )


def delivery_supersession_clause(criterion: Optional[OperativeCriterion]) -> str:
    """Delivery-verifier prompt text naming an amended operative statement as the
    delivery claims, in place of the plan body's numbered exit-criteria list;
    empty when no amendment supersedes the plan's own criterion."""
    if criterion is None or criterion.superseded is None:
        return ""
    return (
        "\nThe plan body's numbered exit-criteria list is SUPERSEDED for delivery claims: "
        "do not treat its items as claims. Check each clause of the operative exit criterion "
        f"(a sizing amendment) instead: {criterion.statement}"
    )


def _plan_repo_root(plan_path: str) -> Optional[Path]:
    """The nearest ancestor of the plan file holding `coordinator.local.md`; `None` when there is none."""
    path = Path(plan_path.replace("\\", "/"))
    if not path.is_absolute():
        path = Path.cwd() / path
    for parent in path.parents:
        if (parent / "coordinator.local.md").is_file():
            return parent
    return None


def _criterion_judge_prompt(
    review: ExecuteReview,
    *,
    plan_path: str,
    run_base_sha: str,
    falsifier: Optional[dict],
    prompt_head: str,
    criterion: Optional[OperativeCriterion],
    host_degraded: bool,
    evidence_path: Optional[str],
    evidence: Optional[JudgeEvidence],
    em_owned_rows: Optional[List[str]],
    ledger_charter: Optional[str],
    withheld_rows: Optional[List[dict]] = None,
) -> Tuple[str, str]:
    """``(emitted agent type, prompt)`` of the roster judge; ``review.judge`` must be set."""
    falsifier_clause = ""
    if falsifier:
        falsifier_clause = (
            f"\nrecorded falsifier: how={falsifier['how']!r}; "
            f"expected_when_true={falsifier['expected_when_true']!r}"
            + (f"; baseline_output={falsifier['baseline_output']!r}" if falsifier.get("baseline_output") else "")
        )
    judge_type, judge_role = _host_native(review.judge.agent_type, host_degraded)
    interpreter = (
        f"{interpreter_clause(_plan_repo_root(plan_path))} "
        "Run a falsifier under that rather than running its bare `python` verbatim. "
    )
    prompt = (
        f"{prompt_head}\n\n{judge_role}{_JUDGE_PREAMBLE}{interpreter}\n"
        f"{_tier1_clause(evidence)}\n"
        f"plan_path: {plan_path} (its sizing_object field names the sizing)\n"
        f"run_base_sha: {run_base_sha}\n"
        f"verification_record: {plan_path} § Verification (EM-run legs, committed; "
        f"weigh one only for a leg you are denied, and mark it provenance em-recorded)"
        f"{_criterion_clause(criterion)}"
        f"{falsifier_clause}"
        f"{em_owned_clause(em_owned_rows)}"
        + (f"\nrow_evidence: {evidence_path} (operator-recorded per-row evidence and `criterion_evidence` clause entries; weigh them, cite them)" if evidence_path else "")
        + (f"\n{ledger_charter}" if ledger_charter else "")
    ).strip()
    return judge_type, prompt


def criterion_judge_brief(
    review: ExecuteReview,
    *,
    stage_schemas: Dict[str, dict],
    plan_path: str,
    run_base_sha: str,
    prompt_head: str = "",
    ledger_charter: Optional[str] = None,
    falsifier: Optional[dict] = None,
    criterion: Optional[OperativeCriterion] = None,
    evidence_path: Optional[str] = None,
    evidence: Optional[JudgeEvidence] = None,
    em_owned_rows: Optional[List[str]] = None,
    withheld_rows: Optional[List[dict]] = None,
) -> Optional[Tuple[str, dict]]:
    """The roster judge's ``(prompt, widened result schema)`` for a caller that runs the judging
    inside another agent, or ``None`` when the roster declares no judge. The prompt is the judge's
    own brief with no role preamble, so the host agent keeps its own type. The optional keywords
    render as in ``compose_criterion_judge``; all ``None`` leaves the prompt unchanged."""
    if review.judge is None:
        return None
    _, prompt = _criterion_judge_prompt(
        review, plan_path=plan_path, run_base_sha=run_base_sha, falsifier=falsifier, prompt_head=prompt_head,
        criterion=criterion, host_degraded=False, evidence_path=evidence_path, evidence=evidence,
        em_owned_rows=em_owned_rows, ledger_charter=ledger_charter, withheld_rows=withheld_rows,
    )
    schema = json.loads(_widen_judge_schema(_schema_literal(review.judge.schema, stage_schemas)))
    return prompt, schema


class CriterionJudgeParts(NamedTuple):
    """The judge call as data: what ``compose_criterion_judge`` renders into an ``agent(...)``
    expression and what an Agent-tool dispatch needs. ``model``/``effort`` are ``None`` when the
    roster pins none; ``schema_literal`` is the exact JSON text the script embeds."""

    agent_type: str
    model: Optional[str]
    effort: Optional[str]
    prompt: str
    schema_literal: str

    @property
    def schema(self) -> dict:
        return json.loads(self.schema_literal)


def criterion_judge_parts(
    review: ExecuteReview,
    *,
    stage_schemas: Dict[str, dict],
    plan_path: str,
    run_base_sha: str,
    falsifier: Optional[dict] = None,
    prompt_head: str = "",
    criterion: Optional[OperativeCriterion] = None,
    host_degraded: bool = False,
    evidence_path: Optional[str] = None,
    evidence: Optional[JudgeEvidence] = None,
    em_owned_rows: Optional[List[str]] = None,
    ledger_charter: Optional[str] = None,
    ledger_schema: Optional[dict] = None,
    withheld_rows: Optional[List[dict]] = None,
) -> Optional[CriterionJudgeParts]:
    """The roster judge's call parts, or ``None`` when the roster declares no judge. The one
    source both ``compose_criterion_judge`` and an Agent-tool brief render from."""
    if review.judge is None:
        return None
    judge_type, prompt = _criterion_judge_prompt(
        review, plan_path=plan_path, run_base_sha=run_base_sha, falsifier=falsifier,
        prompt_head=prompt_head, criterion=criterion, host_degraded=host_degraded,
        evidence_path=evidence_path, evidence=evidence, em_owned_rows=em_owned_rows,
        ledger_charter=ledger_charter, withheld_rows=withheld_rows,
    )
    schema_literal = _widen_judge_schema(_schema_literal(review.judge.schema, stage_schemas))
    if ledger_schema is not None:
        merged = json.loads(schema_literal)
        merged.setdefault("properties", {}).update(ledger_schema.get("properties", {}))
        merged["required"] = sorted({*merged.get("required", []), *ledger_schema.get("required", [])})
        schema_literal = json.dumps(merged, sort_keys=True)
    return CriterionJudgeParts(judge_type, review.judge.model, review.judge.effort, prompt, schema_literal)


def compose_criterion_judge(
    review: ExecuteReview,
    *,
    stage_schemas: Dict[str, dict],
    plan_path: str,
    run_base_sha: str,
    falsifier: Optional[dict],
    prompt_head: str = "",
    criterion: Optional[OperativeCriterion] = None,
    host_degraded: bool = False,
    prompt_suffix_js: Optional[str] = None,
    evidence_path: Optional[str] = None,
    evidence: Optional[JudgeEvidence] = None,
    em_owned_rows: Optional[List[str]] = None,
    ledger_charter: Optional[str] = None,
    ledger_schema: Optional[dict] = None,
    sidecar_workflow: str = "review",
    sidecar_run_key: Optional[str] = None,
    withheld_rows: Optional[List[dict]] = None,
) -> Optional[str]:
    """The roster's ``judge`` agent as one ``agent(...)`` call EXPRESSION, or
    ``None`` when the roster declares no judge. ``evidence_path`` names the
    plan's row-evidence sidecar, passed only when it holds entries. ``prompt_suffix_js`` is a JS
    string expression appended to the prompt at run time, for a path the
    script learns only then. Pointers only, except the register tier: ``evidence``
    inlines claimed rows, row rulings and the PM's quotes verbatim and in full;
    without it tier 1 is a pointer to the sizing's register.

    ``ledger_charter`` folds the requirements-ledger check into this judge: its text is
    appended to the prompt, and ``ledger_schema`` (an object schema whose properties carry
    ``items``, ``all_of_pm_ask`` and ``conflicts``) is merged into the widened result schema,
    with its ``required`` fields required. Both ``None`` leaves the call byte-identical."""
    parts = criterion_judge_parts(
        review, stage_schemas=stage_schemas, plan_path=plan_path, run_base_sha=run_base_sha,
        falsifier=falsifier, prompt_head=prompt_head, criterion=criterion, host_degraded=host_degraded,
        evidence_path=evidence_path, evidence=evidence, em_owned_rows=em_owned_rows,
        ledger_charter=ledger_charter, ledger_schema=ledger_schema, withheld_rows=withheld_rows,
    )
    if parts is None:
        return None
    prompt = parts.prompt
    call = _agent_call_literal(
        parts.agent_type,
        prompt,
        CRITERION_JUDGE_PHASE_TITLE,
        schema=True,
        as_arrow=False,
        agent_opts=_agent_opts_for(review.judge, emitted_agent_type=parts.agent_type),
        schema_literal=parts.schema_literal,
    )
    if prompt_suffix_js:
        prefix = f"agent({_prompt_literal(prompt)}"
        if not call.startswith(prefix):
            raise ValueError("judge agent call does not open with its prompt literal")
        call = f"{prefix} + {prompt_suffix_js}{call[len(prefix):]}"
    return with_sidecar_clause(
        call,
        sidecar_workflow,
        sidecar_run_key or Path(plan_path.replace("\\", "/")).stem or "run",
        f"judge-{parts.agent_type}",
    )

