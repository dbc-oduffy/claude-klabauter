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

from typing import Dict, List, Optional, Tuple

from coordinator_core.ops.review_mint.roster import (
    ExecuteReview,
    ReviewAgent,
    RosterFragmentError,
)
from coordinator_core.ops.review_mint.compose import _agent_call_literal, prompt_literal
from coordinator_core.ops.workflow_scaffold import _js_string_literal

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

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


def _size_gated(full_prompt: str, lean_prompt: str) -> str:
    """A JS expression choosing ``lean_prompt`` over ``full_prompt`` when prep froze a small diff."""
    return f"({_SMALL_DIFF_JS} ? {_prompt_literal(lean_prompt)} : {_prompt_literal(full_prompt)})"


def _degrading(call: str) -> str:
    """``call`` (``agent(...)`` or ``() => agent(...)``) rewritten so a
    rejection resolves to ``null`` instead of throwing the run past its
    terminal commit. Null, not an object: the wake digest's review and
    delivery legs treat any truthy result as a phase that ran, and null is
    already their not_run reading. Review-phase calls only."""
    body = call[len("() => "):] if call.startswith("() => ") else call
    wrapped = (
        "(async () => { try { return await " + body + "; } catch (e) { "
        "return null; } })()"
    )
    return f"() => {wrapped}" if call.startswith("() => ") else wrapped


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
    run_key: Optional[str] = None,
    host_degraded: bool = False,
    slice_key_js: Optional[str] = None,
    slice_identity_sha: Optional[str] = None,
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
        if precredited_rows
        else ""
    )
    prep_type, prep_role = _host_native(review.prep.agent_type, host_degraded)
    prep_prompt = (
        f"{prompt_head}\n\n{prep_role}"
        f"Freeze and characterise this run's diff for review. Freeze it with the "
        f"`freeze-review-diff` launcher on PATH (the settings-home bin; the "
        f"`review.freeze_diff` op's entrypoint -- this repo need not carry "
        f"coordinator/bin, so never look for it here): `freeze-review-diff --worktree --range "
        f"{run_base_sha or 'run_base_sha'} --slice-id {prep_slice_id} "
        f"--paths <every declared path>`. --worktree is mandatory: the run's rows "
        f"land UNCOMMITTED in the working tree and a peer may commit meanwhile, so "
        f"`git diff base..HEAD` is never the run's diff. Return its single stdout "
        f"line as whole_diff_path, verbatim; never write or edit a diff yourself. Report "
        f"product_files as the `product_files: N` line the launcher prints on stderr, verbatim; "
        f"never a count of your own. Under "
        f"foreign_claims list ONLY declared paths that a commit in "
        f"{run_base_sha or 'run_base_sha'}..HEAD also changed: a peer landed inside this "
        f"run's footprint. Peers' work elsewhere in the shared tree is normal and is never "
        f"a foreign claim, and so is any write by this run itself: its executors', reviewers' "
        f"and EM's own commits, and every `.coordinator-local/subagent-share/` sidecar. Count a "
        f"commit foreign only when a live peer session claims the path "
        f"(`session-claim-cli who-claims-path <path>`, on the settings-home bin); "
        f"an empty list is the usual answer. Take the verdict and the slices from ONE engine call, "
        f"never your own grouping: `\"${{COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}}/bin/coordinator-invoke\" "
        f"review.partition_slices '{{\"worktree\":true,\"base\":\"{run_base_sha or 'run_base_sha'}\","
        f"\"slice_prefix\":\"{prep_slice_id}\",\"paths\":[<every declared path>]}}'` -- it returns "
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
    phases.append(
        (
            prep_phase,
            f"  phase({_js_string_literal(prep_phase)});\n"
            f"  const _reviewPrep = await {_degrading(prep_call)};\n"
            # A single-reviewer-ok verdict over product files is one slice of the
            # whole diff; prep that omitted it is synthesised here, never refused.
            f"  if (_reviewPrep && _reviewPrep.verdict === 'single-reviewer-ok' && "
            f"!(_reviewPrep.slices ?? []).length && (_reviewPrep.product_files ?? 0) > 0) {{ "
            f"_reviewPrep.slices = [{{ id: 'whole-diff', files: [], "
            f"diff_path: _reviewPrep.whole_diff_path, "
            f"sidecar_path: (_reviewPrep.whole_diff_sidecars?.personas ?? [])[0] ?? "
            f"String(_reviewPrep.whole_diff_path).replace(/\\.diff$/, '') + '.whole-slice.md', "
            f"contract_blocks: 0 }}]; }}\n"
            # A clean prep over a run whose rows changed nothing froze an empty
            # diff: there is nothing to review or commit, which is an outcome,
            # not a failed prep.
            # A count of 0 that disagrees with the slices (one lists files), or with
            # a row this run landed, is not trusted: the run falls through to the
            # slices guard instead of halting, so landed rows never skip review.
            f"  if (_reviewPrep && _reviewPrep.verdict === 'single-reviewer-ok' && "
            f"(typeof _landed === 'undefined' || !Object.keys(_landed).length) && "
            f"(_reviewPrep.product_files ?? 0) === 0 && !(_reviewPrep.foreign_claims ?? []).length && "
            f"!(_reviewPrep.slices ?? []).some(s => (s?.files ?? []).length)) {{ "
            f"return {{ halted: 'no-op', reason: 'the run changed no product file; nothing to review or commit', "
            f"prep: _reviewPrep, wave: null, integration: null }}; }}\n"
            # Rows landed but changed no product file (a verification-only row):
            # the clean empty-diff prep is the one case with nothing to review.
            # The slice reviewers, other whole-diff reviewers and integration
            # are skipped; the delivery verifier and the terminal judge still run.
            # The same clean-prep predicate as the no-op halt, minus its
            # landed-rows condition; a refused prep fails `verdict`, and a prep
            # with product files fails `product_files`, so both still throw.
            f"  const _verifyOnly = !!(_reviewPrep && _reviewPrep.verdict === 'single-reviewer-ok' && "
            f"(_reviewPrep.product_files ?? 0) === 0 && !(_reviewPrep.foreign_claims ?? []).length && "
            f"!(_reviewPrep.slices ?? []).some(s => (s?.files ?? []).length));\n"
            # A failed or refused prep yields no slices -- a refusal reports
            # product_files 0, so the guard keys on the slices alone. The wave's
            # `?? []` would otherwise expand to no sliced reviewer and land the
            # run reviewed by the whole-diff tail alone. Declared paths are
            # non-empty here: the plan route always declares them, and the queue
            # route skips the wave before prep when no row committed.
            f"  if (!_reviewPrep || (!(_reviewPrep.slices ?? []).length && !_verifyOnly)) {{ "
            f"return {{ halted: {_js_string_literal(NO_SLICES_HALT)} + ' (prep verdict: ' + "
            f"String(_reviewPrep?.verdict ?? 'none') + ')', reason: {_js_string_literal(_NO_SLICES_REFUSAL)}, "
            f"prep: _reviewPrep ?? null, wave: null, integration: null }}; }}",
        )
    )

    # -- 2. review-wave -----------------------------------------------------
    wave_phase = "Review wave"
    slice_agents = [a for a in review.review_wave if a.per == "slice"]
    whole_diff_agents = [a for a in review.review_wave if a.per != "slice"]

    item_lines: List[str] = []
    for agent in slice_agents:
        slice_type, slice_role = _host_native(agent.agent_type, host_degraded)
        base_prompt = (
            f"{prompt_head}\n\n{slice_role}"
            f"Review your assigned slice of this run's diff.\n"
            f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
        ).strip()
        lean_prompt = (
            f"{prompt_head}\n\n{slice_role}"
            f"Review your assigned slice of this run's diff.\n{_LEAN_READ_CLAUSE}\n"
            f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
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
            f"agent({_size_gated(base_prompt, lean_prompt)} + "
            f"{_js_string_literal(chr(10) + chr(10) + 'Slice: ')} + "
            f"JSON.stringify(s), " + call[len(prefix):]
        )
        item_lines.append(
            f"    ...(_reviewPrep?.slices ?? []).map(s => () => {_degrading(spliced_call)})"
        )

    for agent in whole_diff_agents:
        is_delivery_verifier = agent.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE
        role_note = (
            f"\n\n{_DELIVERY_VERIFIER_ROLE_PREAMBLE}{delivery_supersession_clause(criterion)}"
            if is_delivery_verifier
            else ""
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
        )
        call = f"() => agent({_size_gated(wave_prompt, lean_wave_prompt)} + {frozen}, " + call[len(prefix):]
        degraded = _degrading(call)
        item_lines.append(
            f"    {degraded}" if is_delivery_verifier else f"    () => _verifyOnly ? null : ({degraded})()"
        )

    map_lines = ",\n".join(item_lines)

    delivery_index = next(
        (
            i
            for i, agent in enumerate(whole_diff_agents)
            if agent.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE
        ),
        None,
    )

    wave_lines = [
        f"  phase({_js_string_literal(wave_phase)});",
        f"  const _reviewWave = await parallel([\n{map_lines}\n  ]);",
    ]
    if delivery_index is not None:
        offset_from_end = len(whole_diff_agents) - delivery_index
        wave_lines.append(
            f"  const _deliveryVerdict = _reviewWave[_reviewWave.length - {offset_from_end}];"
        )
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
        phases.append(
            (
                integration_phase,
                f"  phase({_js_string_literal(integration_phase)});\n"
                f"  const _reviewIntegration = _verifyOnly ? null : await {_degrading(integration_call)};",
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
    "Judge whether this run met its plan's exit criteria. Inputs, all by path: the "
    "plan (its prime_exit_criterion, gated_exit_criteria and body are the spec), the "
    "PM's recorded words (the plan's execution_authorized_note, its sizing object's "
    "intent, any '## PM brief' section), and the working tree against run_base_sha. "
    "Do not read executor reports, review sidecars or their prose: the run does not "
    "certify itself. Every 'met' names the command you ran or the path you read. When "
    "the plan records a falsifier, run it as recorded; it is not yours to replace. "
    "Run Python as `python3`, falling back to `python` when `python3` is absent, rather "
    "than running a falsifier's bare `python` verbatim. "
    "Return 'indeterminate' when the evidence does not settle the criterion. Report "
    "the REQUIRED boolean `differs_from_baseline`: true only if what you observed "
    "differs from the baseline in the way the criterion describes, false otherwise "
    "(including when it matches the baseline), and copy the baseline you compared against as `baseline_output` "
    "(empty string if none). The run's verdict is computed from that boolean; "
    "`status` must agree with it."
)


def _widen_judge_schema(schema_literal: str) -> str:
    """The judge's roster schema plus the two fields the terminal verdict is
    computed from, matching ``dispatch_emit.emit._falsifier_schema_literal`` so
    both producers of ``_falsifierResult`` share one result shape."""
    schema = json.loads(schema_literal)
    schema.setdefault("properties", {}).update(
        {
            "differs_from_baseline": {"type": "boolean"},
            "baseline_output": {"type": "string", "maxLength": 300},
        }
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
) -> Optional[str]:
    """The roster's ``judge`` agent as one ``agent(...)`` call EXPRESSION, or
    ``None`` when the roster declares no judge. ``evidence_path`` names the
    plan's row-evidence sidecar, passed only when it holds entries. ``prompt_suffix_js`` is a JS
    string expression appended to the prompt at run time, for a path the
    script learns only then. Pointers only: the engine
    never inlines or summarises the PM's words, the judge reads them from the
    plan and sizing artifacts."""
    if review.judge is None:
        return None
    falsifier_clause = ""
    if falsifier:
        falsifier_clause = (
            f"\nrecorded falsifier: how={falsifier['how']!r}; "
            f"expected_when_true={falsifier['expected_when_true']!r}"
            + (f"; baseline_output={falsifier['baseline_output']!r}" if falsifier.get("baseline_output") else "")
        )
    judge_type, judge_role = _host_native(review.judge.agent_type, host_degraded)
    prompt = (
        f"{prompt_head}\n\n{judge_role}{_JUDGE_PREAMBLE}\n"
        f"plan_path: {plan_path} (its sizing_object field names the sizing)\n"
        f"run_base_sha: {run_base_sha}\n"
        f"verification_record: {plan_path} § Verification (EM-run legs, committed; "
        f"weigh one only for a leg you are denied, and mark it provenance em-recorded)"
        f"{_criterion_clause(criterion)}"
        f"{falsifier_clause}"
        + (f"\nrow_evidence: {evidence_path} (operator-recorded per-row evidence; weigh it, cite it)" if evidence_path else "")
    ).strip()
    call = _agent_call_literal(
        judge_type,
        prompt,
        CRITERION_JUDGE_PHASE_TITLE,
        schema=True,
        as_arrow=False,
        agent_opts=_agent_opts_for(review.judge, emitted_agent_type=judge_type),
        schema_literal=_widen_judge_schema(_schema_literal(review.judge.schema, stage_schemas)),
    )
    if prompt_suffix_js:
        prefix = f"agent({_prompt_literal(prompt)}"
        if not call.startswith(prefix):
            raise ValueError("judge agent call does not open with its prompt literal")
        call = f"{prefix} + {prompt_suffix_js}{call[len(prefix):]}"
    return call

