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
from coordinator_core.ops.review_mint.compose import _agent_call_literal
from coordinator_core.ops.workflow_scaffold import _js_string_literal

import json

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


def _resolve_schema_refs(schema, stage_schemas: Dict[str, dict], *, _seen=None):
    """Inline every ``{"$ref": "#/$defs/<name>"}`` in ``schema`` against
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
    return _js_string_literal(prompt)


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


#: Thrown by the emitted script when review prep yields nothing to review
#: over a diff that has product files. Fails the run closed before anything lands.
_NO_SLICES_REFUSAL = (
    "review prep returned no slices (it failed, refused, or froze nothing); "
    "refusing to continue unreviewed. Re-run the review prep, then resume."
)


def compose_execute_review(
    review: ExecuteReview,
    *,
    stage_schemas: Dict[str, dict],
    plan_path: str,
    run_base_sha: str,
    declared_paths: Optional[List[str]] = None,
    declared_paths_js: Optional[str] = None,
    prompt_head: str = "",
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
    prep_prompt = (
        f"{prompt_head}\n\n"
        f"Freeze and characterise this run's diff for review. Freeze it with the "
        f"`freeze-review-diff` launcher on PATH (the settings-home bin; the "
        f"`review.freeze_diff` op's entrypoint -- this repo need not carry "
        f"coordinator/bin, so never look for it here): `freeze-review-diff --worktree --range "
        f"{run_base_sha or 'run_base_sha'} --slice-id <a name unique to this run> "
        f"--paths <every declared path>`. --worktree is mandatory: the run's rows "
        f"land UNCOMMITTED in the working tree and a peer may commit meanwhile, so "
        f"`git diff base..HEAD` is never the run's diff. Return its single stdout "
        f"line as whole_diff_path, verbatim; never write or edit a diff yourself. Under "
        f"foreign_claims list ONLY declared paths that a commit in "
        f"{run_base_sha or 'run_base_sha'}..HEAD also changed: a peer landed inside this "
        f"run's footprint. Peers' work elsewhere in the shared tree is normal and is never "
        f"a foreign claim; an empty list is the usual answer.\n"
        f"plan_path: {plan_path}\n"
        f"run_base_sha: {run_base_sha}\n"
        f"declared_paths:{'' if declared_paths_js else ' ' + ', '.join(declared_paths)}"
    ).strip()
    prep_call = _agent_call_literal(
        review.prep.agent_type,
        prep_prompt,
        prep_phase,
        schema=True,
        as_arrow=False,
        agent_opts=_agent_opts_for(review.prep),
        schema_literal=_schema_literal(review.prep.schema, stage_schemas),
    )
    if declared_paths_js:
        prep_prefix = f"agent({_prompt_literal(prep_prompt)}, "
        if not prep_call.startswith(prep_prefix):
            raise ValueError("prep agent call does not open with its prompt literal")
        prep_call = (
            f"agent({_prompt_literal(prep_prompt)} + ' ' + "
            f"JSON.stringify({declared_paths_js}), " + prep_call[len(prep_prefix):]
        )
    phases.append(
        (
            prep_phase,
            f"  phase({_js_string_literal(prep_phase)});\n"
            f"  const _reviewPrep = await {_degrading(prep_call)};\n"
            # A failed or refused prep yields no slices -- a refusal reports
            # product_files 0, so the guard keys on the slices alone. The wave's
            # `?? []` would otherwise expand to no sliced reviewer and land the
            # run reviewed by the whole-diff tail alone. Declared paths are
            # non-empty here: the plan route always declares them, and the queue
            # route skips the wave before prep when no row committed.
            f"  if (!_reviewPrep || !(_reviewPrep.slices ?? []).length) {{ throw new Error("
            f"{_js_string_literal(_NO_SLICES_REFUSAL)}); }}",
        )
    )

    # -- 2. review-wave -----------------------------------------------------
    wave_phase = "Review wave"
    slice_agents = [a for a in review.review_wave if a.per == "slice"]
    whole_diff_agents = [a for a in review.review_wave if a.per != "slice"]

    item_lines: List[str] = []
    for agent in slice_agents:
        base_prompt = (
            f"{prompt_head}\n\n"
            f"Review your assigned slice of this run's diff.\n"
            f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
        ).strip()
        call = _agent_call_literal(
            agent.agent_type,
            base_prompt,
            wave_phase,
            schema=True,
            as_arrow=False,
            agent_opts=_agent_opts_for(agent),
            schema_literal=_schema_literal(agent.schema, stage_schemas),
        )
        # Splice the runtime slice object in via string concatenation
        # (never a template literal): `agent(<static> + '\n\nSlice: ' +
        # JSON.stringify(s), opts)`.
        prefix = f"agent({_prompt_literal(base_prompt)}, "
        assert call.startswith(prefix)
        spliced_call = (
            f"agent({_prompt_literal(base_prompt)} + "
            f"{_js_string_literal(chr(10) + chr(10) + 'Slice: ')} + "
            f"JSON.stringify(s), " + call[len(prefix):]
        )
        item_lines.append(
            f"    ...(_reviewPrep?.slices ?? []).map(s => () => {_degrading(spliced_call)})"
        )

    for agent in whole_diff_agents:
        is_delivery_verifier = agent.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE
        role_note = f"\n\n{_DELIVERY_VERIFIER_ROLE_PREAMBLE}" if is_delivery_verifier else ""
        wave_prompt = (
            f"{prompt_head}\n\n"
            f"Review this run's whole diff.\n"
            f"plan_path: {plan_path}\n"
            f"run_base_sha: {run_base_sha}"
            f"{role_note}"
        ).strip()
        emitted_agent_type = (
            _DELIVERY_VERIFIER_HOST_NATIVE_TYPE if is_delivery_verifier else agent.agent_type
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
        call = f"() => agent({_prompt_literal(wave_prompt)} + {frozen}, " + call[len(prefix):]
        item_lines.append(f"    {_degrading(call)}")

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
        integration_prompt = (
            f"{prompt_head}\n\n"
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
            review.integration.agent_type,
            integration_prompt,
            integration_phase,
            schema=True,
            as_arrow=False,
            agent_opts=_agent_opts_for(review.integration),
            schema_literal=_schema_literal(review.integration.schema, stage_schemas),
        )
        phases.append(
            (
                integration_phase,
                f"  phase({_js_string_literal(integration_phase)});\n"
                f"  const _reviewIntegration = await {_degrading(integration_call)};",
            )
        )

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


def compose_criterion_judge(
    review: ExecuteReview,
    *,
    stage_schemas: Dict[str, dict],
    plan_path: str,
    run_base_sha: str,
    falsifier: Optional[dict],
    prompt_head: str = "",
) -> Optional[str]:
    """The roster's ``judge`` agent as one ``agent(...)`` call EXPRESSION, or
    ``None`` when the roster declares no judge. Pointers only: the engine
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
    prompt = (
        f"{prompt_head}\n\n{_JUDGE_PREAMBLE}\n"
        f"plan_path: {plan_path} (its sizing_object field names the sizing)\n"
        f"run_base_sha: {run_base_sha}"
        f"{falsifier_clause}"
    ).strip()
    return _agent_call_literal(
        review.judge.agent_type,
        prompt,
        CRITERION_JUDGE_PHASE_TITLE,
        schema=True,
        as_arrow=False,
        agent_opts=_agent_opts_for(review.judge),
        schema_literal=_widen_judge_schema(_schema_literal(review.judge.schema, stage_schemas)),
    )

