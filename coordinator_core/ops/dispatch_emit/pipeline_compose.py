"""Pure composer for the dispatch.emit pipeline route: a validated manifest and schedule to one Workflow script.

`compose_pipeline_script(manifest, inputs, schedule, *, run_id, agent_type_host)` does no I/O and
spawns nothing. Every stage becomes exactly one literal `agent(` call site inside its own
`phase()`, so the foreign-emission hook sees real agentType literals. Prompts and outputs are
filled at compose time, one `re.sub` pass per string, and a substituted value is never re-scanned.
Pre-loop and post-loop stages run once; per-subject stages run in a sequential `for` loop with a
per-subject try/catch. `{{subject}}` is filled per subject into the `subjects` const; a stage whose
element list is known at emit time (`over: subject.<field>` or `over: inputs.<list>`) gets one
pre-filled prompt per element, with `{{item}}` / `{{item.<field>}}` filled from that element. A stage
fanned over a prior return fills `{{item}}` at run time. Under `subjects_mode: sequential` every
stage runs per subject and `{{scratch_dir}}` is that subject's own directory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from coordinator_core.ops.dispatch_emit.emit import (
    _AGENT_MODELS,
    _EXECUTOR_AGENT_TYPE,
    _degrade_agent_type,
    resolve_agent_model,
)
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FAN_OUT_OVER,
    ITEM_MARK,
    PIPELINE_RELAY_CLAUSE,
    PLACEHOLDER_RE,
    SCOPE_POST,
    SCOPE_PRE,
    SCOPE_SUBJECT,
    VALIDATOR_TOKEN,
    Manifest,
    PipelineEmitRefused,
    PipelineInputs,
    RUN_ID_PREFIX,
    Schedule,
    Stage,
    WHEN_FLAG,
    WHEN_NONEMPTY,
    WORKFLOW_SCRIPT_CAP_BYTES,
    segment_name,
    stage_phase,
    subject_key,
    subject_slug,
)
from coordinator_core.ops.dispatch_emit.agent_sidecar import sidecar_clause_js
from coordinator_core.ops.dispatch_emit.fanout_cost import (
    FANOUT_GROUP_MODES,
    FanoutCost,
    Unit,
    group_prompt,
    propose_groups,
    with_fanout,
)
from coordinator_core.ops.dispatch_emit.pipeline_run import REVISIONS_MARKER, finished_rel, revisions_rel
from coordinator_core.ops.workflow_scaffold import _js_string_literal

__all__ = ["compose_pipeline_script", "compose_chain_script", "compose_chain_parts", "ChainPart"]

_FAN_TRAILER = (
    "const fanTrailer = (item, i, n, out) => "
    "`\\n\\nFan-out item ${i} of ${n}: ${JSON.stringify(item)}"
    "\\nWrite this item's output under ${out}/item-${i}/`;"
)

_FAN_OUT = """const fanOut = (items, run) => parallel(items.map((item, i) => () => run(item, i)));"""

_IN_CHUNKS = """async function inChunks(items, run, size) {
  const out = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(...await parallel(items.slice(i, i + size).map((item, j) => () => run(item, i + j))));
  }
  return out;
}"""

_WITH_ITEM = """const withItem = (text, item) => text.split(ITEM_MARK).join(String(item));"""

_PRODUCED = """const produced = async (label, call) => {
  const value = await call;
  const text = typeof value === 'string' ? value.trim() : value == null ? '' : JSON.stringify(value);
  if (text === '' || text === '{}' || text === '[]') throw new Error(`stage ${label} produced nothing`);
  return value;
};"""

_RELAY_DECL = f"const RELAY = {_js_string_literal(PIPELINE_RELAY_CLAUSE)};"


def _revisions_decl(name: str) -> str:
    """The script's one `REVISIONS` const: the stage-start read of the run's revisions file, spliced after `RELAY`."""
    revisions = f"\n\n{REVISIONS_MARKER} Revisions file: {revisions_rel(name)} (read it if it exists; a later revision governs)."
    # json.dumps, not _js_string_literal: the marker's apostrophe must stay verbatim for reads_revisions.
    return f"const REVISIONS = {json.dumps(revisions)};"


def _finish_decl(name: str) -> str:
    """The script's `FINISH` const: the final stage's instruction to write the run's terminal marker."""
    clause = f"\n\nOn finishing, write one line, this stage's label, to {finished_rel(name)}."
    return f"const FINISH = {json.dumps(clause)};"


def _final_stage(schedule: Schedule) -> tuple[str, str] | None:
    """(scope, stage id) of the stage a run executes last, or None when nothing runs.

    Trap: FINISH rides each fan-out item of that stage and the last subject only, so a sibling item or
    subject still running can see the run as finished early.
    """
    for scope in (SCOPE_POST, SCOPE_SUBJECT, SCOPE_PRE):
        for level in reversed(schedule.levels.get(scope, ())):
            if level:
                return scope, level[-1]
    return None


_RETURN_NAME = {SCOPE_PRE: "pre", SCOPE_SUBJECT: "ret", SCOPE_POST: "post"}


_ITEM_SLOT = "<item>"


def _fill(
    template: str,
    *,
    manifest: Manifest,
    inputs: PipelineInputs,
    stages: Mapping[str, Stage],
    subject: str | None,
    errors: list[str],
    item: Mapping | str | None = None,
    scratch: str | None = None,
    brief: str | None = None,
    _seen: tuple[str, ...] = (),
) -> str:
    """One `re.sub` pass; an unresolvable token is recorded in `errors` and left in place."""

    def value(match) -> str:
        token = match.group(1)
        if token == "brief":
            return inputs.brief if brief is None else brief
        if token == VALIDATOR_TOKEN:
            return inputs.validator
        if token == "scratch_dir":
            return scratch if scratch is not None else inputs.scratch_dir
        if token == "item":
            if not isinstance(item, str):
                errors.append("{{item}} used outside a list fan-out, or the element is an object (use {{item.<field>}})")
                return match.group(0)
            return item
        if token == "subject":
            if subject is None:
                errors.append(f"{{{{subject}}}} used outside a per-subject stage")
                return match.group(0)
            return subject
        if token.startswith("item."):
            name = token[len("item."):]
            if item == _ITEM_SLOT:
                return f"<{token}>"
            if not isinstance(item, Mapping) or name not in item:
                errors.append(f"{{{{{token}}}}} used outside a subject fan-out or missing from the item")
                return match.group(0)
            return str(item[name])
        if token.startswith("flags."):
            name = token[len("flags."):]
            if name in inputs.flags:
                raw = inputs.flags[name]
                return str(raw).lower() if isinstance(raw, bool) else str(raw)
            spec = manifest.flags.get(name)
            if spec is not None and spec.default is not None:
                return str(spec.default).lower() if isinstance(spec.default, bool) else str(spec.default)
            errors.append(f"flag {name!r} has no value and no default")
            return match.group(0)
        parts = token.split(".")
        if len(parts) == 3 and parts[0] == "stage" and parts[2] == "output" and parts[1] in stages:
            if parts[1] in _seen:
                errors.append(f"output of stage {parts[1]!r} refers to itself")
                return match.group(0)
            return _fill(
                stages[parts[1]].output,
                manifest=manifest,
                inputs=inputs,
                stages=stages,
                subject=subject,
                errors=errors,
                item=_ITEM_SLOT if stages[parts[1]].fan_out.kind == FAN_OUT_OVER else item,
                scratch=scratch,
                brief=brief,
                _seen=_seen + (parts[1],),
            )
        errors.append(f"unknown placeholder {{{{{token}}}}}")
        return match.group(0)

    return PLACEHOLDER_RE.sub(value, template)


def _returns(source_id: str, scope: str, stages: Mapping[str, Stage], schedule: Schedule) -> tuple[str, bool]:
    """JS expression for a stage's recorded return(s) as seen from `scope`, and whether it is a list of returns."""
    source = stages[source_id]
    source_scope = schedule.scope[source.id]
    key = _js_string_literal(source.id)
    if source_scope == SCOPE_SUBJECT and scope == SCOPE_POST:
        return f"subjRets.flatMap((r) => r[{key}] || [])", True
    return f"{_RETURN_NAME[source_scope]}[{key}]", source.fan_out.kind == FAN_OUT_OVER


def _source_list(stage: Stage, scope: str, stages: Mapping[str, Stage], schedule: Schedule) -> str:
    """JS expression for the de-duplicated list an `over` stage fans out across."""
    return _list_field(stage.fan_out.source, stage.fan_out.field, scope, stages, schedule)


def _list_field(source_id: str, name: str, scope: str, stages: Mapping[str, Stage], schedule: Schedule) -> str:
    returns, many = _returns(source_id, scope, stages, schedule)
    field = _js_string_literal(name)
    if many:
        return f"[...new Set({returns}.flatMap((r) => (r && r[{field}]) || []))]"
    return f"(({returns} && {returns}[{field}]) || [])"


def _when_condition(stage: Stage, scope: str, stages: Mapping[str, Stage], schedule: Schedule) -> str:
    """JS condition of a run-time `when` (return boolean or non-empty return list)."""
    when = stage.when
    if when.kind == WHEN_NONEMPTY:
        return f"{_list_field(when.stage, when.field, scope, stages, schedule)}.length > 0"
    returns, many = _returns(when.stage, scope, stages, schedule)
    field = _js_string_literal(when.field)
    want = json.dumps(when.equals)
    if many:
        return f"{returns}.some((r) => r && r[{field}] === {want})"
    return f"({returns} && {returns}[{field}]) === {want}"


def _stage_model(stage: Stage, agent_type: str) -> str:
    """Stage-declared model, else the agent table's pin; a type with no pin (general-purpose) takes the executor's pin. The Workflow model guard needs every agent() to set one."""
    if stage.model:
        return stage.model
    if agent_type in _AGENT_MODELS:
        return resolve_agent_model(agent_type)
    return resolve_agent_model(_EXECUTOR_AGENT_TYPE)


def _agent_options(stage: Stage, manifest: Manifest, agent_type: str, agent_type_host: str | None) -> str:
    options = [f"agentType: {_js_string_literal(_degrade_agent_type(agent_type, agent_type_host))}"]
    options.append(f"model: {_js_string_literal(_stage_model(stage, agent_type))}")
    if stage.effort is not None:
        options.append(f"effort: {_js_string_literal(stage.effort)}")
    if stage.schema is not None:
        options.append(f"schema: {json.dumps(manifest.schemas[stage.schema], sort_keys=True)}")
    return ", ".join(options)


def _agent_call(
    stage: Stage, manifest: Manifest, inputs: PipelineInputs, agent_type_host: str | None,
    prompt: str, label: str, slug: str, finish: str = "",
) -> str:
    """One `produced(` expression, which rejects an empty return; a roster-typed stage gets one literal call site per roster agent type. Every prompt resolves to the script's `RELAY` clause plus its template."""
    sidecar = sidecar_clause_js("pipeline", manifest.pipeline, label)
    prompt = f"(RELAY + REVISIONS + '\\n\\n' + {prompt} + {sidecar}{finish})"
    if stage.agent_type_from is None:
        call = f"agent({prompt}, {{ label: {label}, {_agent_options(stage, manifest, stage.agent_type, agent_type_host)} }})"
        return f"produced({label}, {call})"
    name = stage.agent_type_from.split(".", 1)[1]
    types = list(dict.fromkeys(entry["agent_type"] for entry in inputs.lists[name]))
    chain = "".join(
        f"t === {_js_string_literal(t)} ? agent(p, {{ label: l, {_agent_options(stage, manifest, t, agent_type_host)} }}) : "
        for t in types
    )
    chain += "Promise.reject(new Error(`no roster agent type for ${l}`))"
    return f"produced({label}, ((p, l, t) => {chain})({prompt}, {label}, rosterTypes[{_js_string_literal(name)}][{slug}]))"


def _roster_guard(stage: Stage) -> str:
    """Fails a runtime roster fan-out loud, naming each slug the roster lacks, before any agent spawns.

    The slugs arrive from an upstream return (mail addressed by name); an invented name would
    otherwise reject one agent at a time as an anonymous per-item null.
    """
    if stage.agent_type_from is None:
        return ""
    roster = f"rosterTypes[{_js_string_literal(stage.agent_type_from.split('.', 1)[1])}]"
    return (
        f"      const unknown = items.filter((x) => !Object.hasOwn({roster}, x));\n"
        f"      if (unknown.length) throw new Error({_js_string_literal(stage.id)} + "
        f"': slug(s) not on the roster: ' + unknown.join(', ') + "
        f"' (roster: ' + Object.keys({roster}).join(', ') + ')');\n"
    )


def _has_item(text: str) -> bool:
    return "item" in PLACEHOLDER_RE.findall(text)


def _uses_item(stage: Stage, manifest: Manifest) -> bool:
    return _has_item(manifest.templates.get(stage.template, "")) or _has_item(stage.output)


def _member_prompt_chain(prompt: str, sizes: Sequence[int]) -> str:
    """JS expression building a group item's prompt from `item.members`, one `group_prompt` per group size present; members are positional (M1, M2, ...) because the header is fixed per call site."""
    built = [
        group_prompt(
            [f"(item.members[{k}].prompt ?? withItem({prompt}, item.members[{k}].slug))" for k in range(n)],
            [f"M{k + 1}" for k in range(n)],
        )
        for n in sizes
    ]
    expr = f"({built[-1]})"
    for n, text in zip(reversed(sizes[:-1]), reversed(built[:-1])):
        expr = f"(item.members.length === {n} ? ({text}) : {expr})"
    return expr


def _stage_expression(
    stage: Stage,
    scope: str,
    manifest: Manifest,
    inputs: PipelineInputs,
    stages: Mapping[str, Stage],
    schedule: Schedule,
    agent_type_host: str | None,
    group_sizes: Sequence[int] = (),
    final: bool = False,
) -> str:
    finish = ""
    if final:
        finish = " + (s === subjects[subjects.length - 1] ? FINISH : '')" if scope == SCOPE_SUBJECT else " + FINISH"
    holder = "s" if scope == SCOPE_SUBJECT else "common"
    prompt = f"{holder}.prompts[{_js_string_literal(stage.id)}]"
    output = f"{holder}.outputs[{_js_string_literal(stage.id)}]"
    label = _js_string_literal(f":{stage.id}" if scope == SCOPE_SUBJECT else stage.id)
    if scope == SCOPE_SUBJECT:
        label = f"s.subject + {label}"
    fanned = stage.fan_out.kind == FAN_OUT_OVER
    size = f", {stage.max_concurrent}" if stage.max_concurrent is not None else ""
    fan = "inChunks" if stage.max_concurrent is not None else "fanOut"
    item_label = (
        f"s.subject + {_js_string_literal(f':{stage.id}:')} + (i + 1)"
        if scope == SCOPE_SUBJECT
        else f"{_js_string_literal(f'{stage.id}:')} + (i + 1)"
    )
    if not fanned:
        expr = _agent_call(stage, manifest, inputs, agent_type_host, prompt, label, "item", finish)
    elif stage.fan_out.emit_time:
        item_prompt = f"(item.prompt ?? withItem({prompt}, item.slug))"
        if group_sizes:
            item_prompt = f"(item.members ? {_member_prompt_chain(prompt, group_sizes)} : {item_prompt})"
        call = _agent_call(stage, manifest, inputs, agent_type_host, item_prompt, item_label, "item.slug", finish)
        expr = (
            "(async () => {\n"
            f"      const items = {holder}.items[{_js_string_literal(stage.id)}];\n"
            f"      return {fan}(items, (item, i) =>\n"
            f"        {call}{size});\n"
            "    })()"
        )
    else:
        items = _source_list(stage, scope, stages, schedule)
        fill = f"withItem({prompt}, item)" if _uses_item(stage, manifest) else prompt
        if not _has_item(stage.output):
            fill += f" + fanTrailer(item, i + 1, items.length, {output})"
        call = _agent_call(stage, manifest, inputs, agent_type_host, fill, item_label, "item", finish)
        expr = (
            "(async () => {\n"
            f"      const items = {items};\n"
            "      if (items.length === 0) return [];\n"
            f"{_roster_guard(stage)}"
            f"      return {fan}(items, (item, i) =>\n"
            f"        {call}{size});\n"
            "    })()"
        )
    empty = "[]" if fanned else "null"
    if stage.when is not None and stage.when.kind != WHEN_FLAG:
        condition = _when_condition(stage, scope, stages, schedule)
        expr = f"({condition} ? {expr} : Promise.resolve({empty}))"
    if stage.optional:
        message = _js_string_literal(f"optional stage {stage.id} failed: ")
        expr = (
            "(async () => {\n"
            f"      try {{ return await {expr}; }} catch (err) {{\n"
            f"        log({message} + String(err && err.message || err));\n"
            f"        return {empty};\n"
            "      }\n"
            "    })()"
        )
    return expr


def _level_lines(
    scope: str,
    manifest: Manifest,
    inputs: PipelineInputs,
    schedule: Schedule,
    stages: Mapping[str, Stage],
    agent_type_host: str | None,
    indent: str,
    tag: str = "",
    segment: tuple[str, str] | None = None,
    group_sizes: Mapping[str, Sequence[int]] | None = None,
    final_stage: tuple[str, str] | None = None,
) -> list[str]:
    target = _RETURN_NAME[scope]
    lines: list[str] = []
    for level in schedule.levels.get(scope, ()):
        for title in dict.fromkeys(stage_phase(stages[i]) for i in level):
            lines.append(f"{indent}phase({_js_string_literal(title)});")
        exprs = [
            _stage_expression(
                stages[i], scope, manifest, inputs, stages, schedule, agent_type_host, (group_sizes or {}).get(i, ()),
                final_stage == (scope, i),
            )
            for i in level
        ]
        if len(level) == 1:
            lines.append(f"{indent}{target}[{_js_string_literal(level[0])}] = await {exprs[0]};")
        else:
            thunks = ",\n".join(f"{indent}  () => {e}" for e in exprs)
            lines.append(f"{indent}{{")
            lines.append(f"{indent}  const level = await parallel([\n{thunks}\n{indent}  ]);")
            for k, stage_id in enumerate(level):
                lines.append(f"{indent}  {target}[{_js_string_literal(stage_id)}] = level[{k}];")
            lines.append(f"{indent}}}")
        for stage_id in level:
            lines += _halt_lines(
                stages[stage_id], scope, manifest.pipeline, inputs, indent, tag, segment, segment_name(manifest, inputs),
            )
        lines.append("")
    return lines


def _halt_lines(
    stage: Stage, scope: str, pipeline: str, inputs: PipelineInputs, indent: str, tag: str,
    segment: tuple[str, str] | None = None, name: str | None = None,
) -> list[str]:
    """The early exits of a stage's halts, each ending the run (or, given `segment`, its leg) before the next stage.

    `name` is the segment's name in `segment_halts` (default: the pipeline); the `halted` literal
    always carries the pipeline.

    `halts_unless` trusts the stage's own boolean; `halts_unless_nonempty` and `halts_unless_count`
    compare structured return fields against the emit-time roster, with a remedy composed here.
    `segment` is (block label, leg name): the halt records itself in `segment_halts` and breaks
    to the label instead of returning.
    """
    ref = f"{_RETURN_NAME[scope]}[{_js_string_literal(stage.id)}]"
    push = (
        f"results.push({{ subject: s.subject, returns: ret{tag} }});"
        if scope == SCOPE_SUBJECT
        else f"results.push({{ scope: {_js_string_literal(scope)}, returns: {_RETURN_NAME[scope]}{tag} }});"
    )
    halted = _js_string_literal(f"{pipeline}.{stage.id}")

    def block(condition: str, remedy: str) -> list[str]:
        if segment is not None:
            label, leg = segment
            return [
                f"{indent}if ({condition}) {{",
                f"{indent}  {push}",
                f"{indent}  segment_halts.push({{ segment: {_js_string_literal(name or pipeline)}, "
                f"leg: {_js_string_literal(leg)}, halted: {halted}, decision_required: {remedy} }});",
                f"{indent}  break {label};",
                f"{indent}}}",
            ]
        return [
            f"{indent}if ({condition}) {{",
            f"{indent}  {push}",
            f"{indent}  return {{ halted: {halted}, decision_required: {remedy}, results }};",
            f"{indent}}}",
        ]

    lines: list[str] = []
    if stage.halts_unless is not None:
        lines += block(
            f"{ref} && {ref}[{_js_string_literal(stage.halts_unless)}] === false",
            f"{ref}.remedy || []",
        )
    if stage.halts_unless_nonempty is not None:
        field = _js_string_literal(stage.halts_unless_nonempty)
        message = _js_string_literal(
            f"stage {stage.id} returned no {stage.halts_unless_nonempty}; "
            f"fix the stage's inputs and re-run, nothing downstream ran"
        )
        lines += block(
            f"!{ref} || typeof {ref} !== 'object' || !Array.isArray({ref}[{field}]) || {ref}[{field}].length === 0",
            f"[{message}]",
        )
    if stage.halts_unless_count is not None:
        count = stage.halts_unless_count
        size = len(inputs.lists[count.equals])

        def value(name: str) -> str:
            cell = f"{ref}[{_js_string_literal(name)}]"
            return f"(Array.isArray({cell}) ? {cell}.length : {cell})"

        mismatch = " || ".join(f"{value(f)} !== {size}" for f in count.fields)
        seen = " + ', ' + ".join(
            f"{_js_string_literal(f + '=')} + String({ref}[{_js_string_literal(f)}] === undefined ? 'missing' : {value(f)})"
            for f in count.fields
        )
        lead = _js_string_literal(f"stage {stage.id} disagrees with the {size}-entry {count.equals} roster (")
        tail = _js_string_literal("); reconcile them and re-run, nothing downstream ran")
        lines += block(f"!{ref} || {mismatch}", f"[{lead} + {seen} + {tail}]")
    return lines


def _element_value(element: object) -> object:
    """What `{{item}}` binds for one list element: a roster entry binds its slug."""
    return element["slug"] if isinstance(element, dict) and "slug" in element else element


def _unit_part(text: str) -> str:
    """`text` reduced to characters that cannot trip the meta pure-literal check."""
    return re.sub(r"[^\w./+-]", "_", text)


def _unit_ids(prefix: str, slugs: Sequence[str | None]) -> list[str]:
    """One unique unit id per element, `prefix` + its slug (its position when it has none)."""
    seen: set[str] = set()
    out: list[str] = []
    for i, slug in enumerate(slugs):
        uid = prefix + (_unit_part(slug) if slug else str(i + 1))
        if uid in seen:
            uid = f"{uid}#{i + 1}"
        seen.add(uid)
        out.append(uid)
    return out


def _groupable(stage: Stage, manifest: Manifest) -> bool:
    """An emit-time fan-out stage whose agents can share a prompt: no schema, no roster type, and
    nothing reads its returns (another stage's `when` or `over` source, its own halts)."""
    if stage.schema is not None or stage.agent_type_from is not None or stage.produces_brief:
        return False
    if stage.halts_unless or stage.halts_unless_nonempty or stage.halts_unless_count is not None:
        return False
    return not any(
        other.id != stage.id
        and ((other.when is not None and other.when.stage == stage.id) or other.fan_out.source == stage.id)
        for other in manifest.stages
    )


Segment = tuple[Manifest, PipelineInputs, Schedule]


def _segment(
    manifest: Manifest,
    inputs: PipelineInputs,
    schedule: Schedule,
    agent_type_host: str | None,
    errors: list[str],
    *,
    chained: bool,
    segment: tuple[str, str] | None = None,
    skip_guard: bool = False,
    fanout_group: str = "auto",
    tally: list | None = None,
    final: bool = False,
) -> tuple[list[str], list[str], list[Stage]]:
    """A segment's data consts, its body lines and its active stages; a chained segment leaves `results` to the chain and tags its result entries with the pipeline.

    `segment` is (block label, leg) for a scoped segment; `skip_guard` opens it with the check that
    skips it once an earlier segment of its leg has halted. `tally` collects one
    `(units, agents, run_time, groups)` entry per stage a script run reaches.
    """
    stages = {stage.id: stage for stage in manifest.stages}
    group_sizes: dict[str, tuple[int, ...]] = {}
    if (
        ITEM_MARK in inputs.brief
        or ITEM_MARK in (inputs.validator or "")
        or any(ITEM_MARK in json.dumps(sub) for sub in inputs.subjects)
    ):
        errors.append("the brief, the validator or a subject carries the reserved item marker")
    first = manifest.stages[0] if manifest.stages else None
    rebinds = first is not None and first.produces_brief and first.id not in schedule.skipped

    def fill_stage(stage: Stage, subject: str | None, item: Mapping | str | None = None) -> tuple[str, str]:
        template = manifest.templates[stage.template]
        scratch = (
            f"{inputs.scratch_dir.rstrip('/')}/{subject_slug(subject)}"
            if manifest.subjects_mode and subject is not None else None
        )
        kwargs = dict(
            manifest=manifest, inputs=inputs, stages=stages, subject=subject, errors=errors,
            item=item, scratch=scratch,
        )
        if rebinds and stage.id != first.id:
            kwargs["brief"] = _fill(first.output, **{**kwargs, "item": None})
        return _fill(template, **kwargs), _fill(stage.output, **kwargs)

    def scoped(scope: str, subject: str | dict | None) -> dict[str, dict]:
        name = None if subject is None else subject_key(subject)
        prompts, outputs, items = {}, {}, {}
        for stage in manifest.stages:
            if schedule.scope.get(stage.id) != scope:
                continue
            if stage.fan_out.emit_time:
                elements = subject[stage.fan_out.field] if stage.fan_out.over_subject else inputs.lists[stage.fan_out.inputs]
                items[stage.id] = []
                for element in elements:
                    value = _element_value(element) if stage.fan_out.over_inputs else element
                    prompt, output = fill_stage(stage, name, value)
                    items[stage.id].append(
                        {"prompt": prompt, "output": output, "slug": value if isinstance(value, str) else None}
                    )
                full = items[stage.id]
                # One shared prompt when every item's prompt differs only by its slug (a roster
                # entry's slug, or a string element itself): N copies of a multi-KB template
                # otherwise ride in the script.
                if elements and (
                    all(isinstance(e, dict) and "slug" in e for e in elements)
                    or all(isinstance(e, str) for e in elements)
                ):
                    seen = len(errors)
                    shared, _ = fill_stage(stage, name, ITEM_MARK)
                    probe_failed = len(errors) > seen
                    del errors[seen:]
                    if not probe_failed and all(e["prompt"] == shared.replace(ITEM_MARK, e["slug"]) for e in full):
                        prompts[stage.id] = shared
                        items[stage.id] = [{"slug": e["slug"]} for e in full]
                prefix = f"{_unit_part(name)}/{stage.id}:" if name is not None else f"{stage.id}:"
                ids = _unit_ids(prefix, [e["slug"] for e in full])
                if _groupable(stage, manifest):
                    groups = propose_groups(
                        [Unit(uid, stage.id, len(e["prompt"].encode("utf-8"))) for uid, e in zip(ids, full)],
                        fanout_group,
                    )
                else:
                    groups = tuple((uid,) for uid in ids)
                if any(len(g) > 1 for g in groups):
                    at = {uid: i for i, uid in enumerate(ids)}
                    base = items[stage.id]
                    items[stage.id] = [
                        base[at[g[0]]] if len(g) == 1 else {
                            "slug": "+".join(full[at[u]]["slug"] or u for u in g),
                            "members": [base[at[u]] if "prompt" not in base[at[u]] else
                                        {"prompt": base[at[u]]["prompt"], "slug": base[at[u]]["slug"]} for u in g],
                        }
                        for g in groups
                    ]
                    group_sizes[stage.id] = tuple(sorted({*group_sizes.get(stage.id, ()), *(len(g) for g in groups if len(g) > 1)}))
                if tally is not None:
                    tally.append((len(elements), len(groups), False, groups))
            else:
                runtime_item = ITEM_MARK if stage.fan_out.kind == FAN_OUT_OVER else None
                prompts[stage.id], outputs[stage.id] = fill_stage(stage, name, runtime_item)
                if tally is not None:
                    tally.append((1, 1, stage.fan_out.kind == FAN_OUT_OVER, ()))
        return {"prompts": prompts, "outputs": outputs, **({"items": items} if items else {})}

    has_subject_stage = any(v == SCOPE_SUBJECT for v in schedule.scope.values())
    subject_data = [
        {
            "subject": subject_key(sub),
            **(scoped(SCOPE_SUBJECT, sub) if has_subject_stage else {}),
        }
        for sub in inputs.subjects
    ]
    common = {
        scope: scoped(scope, None) for scope in (SCOPE_PRE, SCOPE_POST)
    }
    active = [stage for stage in manifest.stages if stage.id not in schedule.skipped]
    has_common = any(common[s]["prompts"] or common[s].get("items") for s in common)
    roster_names = sorted({
        s.agent_type_from.split(".", 1)[1] for s in active if s.agent_type_from is not None
    })
    data = []
    if skip_guard:
        label, leg = segment
        data.append(
            f"if (segment_halts.some((h) => h.leg === {_js_string_literal(leg)})) {{ "
            f"segment_halts.push({{ segment: {_js_string_literal(segment_name(manifest, inputs))}, "
            f"leg: {_js_string_literal(leg)}, skipped: true }}); break {label}; }}"
        )
    data.append(f"const subjects = {json.dumps(subject_data, ensure_ascii=True, indent=1)};")
    if has_common:
        merged = {
            "prompts": {**common[SCOPE_PRE]["prompts"], **common[SCOPE_POST]["prompts"]},
            "outputs": {**common[SCOPE_PRE]["outputs"], **common[SCOPE_POST]["outputs"]},
        }
        merged_items = {**common[SCOPE_PRE].get("items", {}), **common[SCOPE_POST].get("items", {})}
        if merged_items:
            merged["items"] = merged_items
        data.append(f"const common = {json.dumps(merged, ensure_ascii=True, indent=1)};")
    if roster_names:
        roster = {n: {e["slug"]: e["agent_type"] for e in inputs.lists[n]} for n in roster_names}
        data.append(f"const rosterTypes = {json.dumps(roster, ensure_ascii=True, indent=1)};")

    final_stage = _final_stage(schedule) if final else None
    tag = f", pipeline: {_js_string_literal(manifest.pipeline)}" if chained else ""
    if chained and inputs.segment_key:
        tag += f", segment: {_js_string_literal(inputs.segment_key)}"
    body = ["const pre = {};", "const post = {};", "const subjRets = [];"]
    if not chained:
        body.append("const results = [];")
    body.append("")
    body += _level_lines(SCOPE_PRE, manifest, inputs, schedule, stages, agent_type_host, "", tag, segment, group_sizes=group_sizes, final_stage=final_stage)
    body += ["for (const s of subjects) {", "  const ret = {};", "  subjRets.push(ret);", "  try {"]
    body += _level_lines(SCOPE_SUBJECT, manifest, inputs, schedule, stages, agent_type_host, "    ", tag, segment, group_sizes=group_sizes, final_stage=final_stage)
    body += [
        f"    results.push({{ subject: s.subject, returns: ret{tag} }});",
        "  } catch (err) {",
        f"    results.push({{ subject: s.subject, returns: ret, error: String(err && err.message || err){tag} }});",
        "  }",
        "}",
        "",
    ]
    body += _level_lines(SCOPE_POST, manifest, inputs, schedule, stages, agent_type_host, "", tag, segment, group_sizes=group_sizes, final_stage=final_stage)
    for scope in (SCOPE_PRE, SCOPE_POST):
        if schedule.levels.get(scope):
            body.append(f"results.push({{ scope: {_js_string_literal(scope)}, returns: {_RETURN_NAME[scope]}{tag} }});")
    return data, body, active


def compose_chain_script(
    segments: Sequence[Segment],
    *,
    run_id: str,
    agent_type_host: str | None,
    next_action: Mapping | None = None,
    legs: Mapping[str, str] | None = None,
    nest_scratch: bool = True,
    fanout_group: str = "auto",
) -> str:
    """One Workflow script running `segments` in order; each segment keeps its own stores in its own block, and every segment after the first, and every keyed segment, writes under `{{scratch_dir}}/<segment name>/`.

    Scoped means more than one segment or `legs` given: a halt then ends only its leg (the segment
    plus later segments sharing the leg name `legs` maps, default the segment's own name), recorded
    in `segment_halts`. A scoped run returns `{results, segment_halts}`, plus `next_action` while any
    leg did not halt; with halts its `params.halted_segments` names every segment of every halted
    leg. A one-segment script without `legs` returns at the halt, byte-identical to the plain script.
    `nest_scratch=False` takes each segment's `scratch_dir` as final (split parts).
    """
    if not segments:
        raise PipelineEmitRefused(["a chain needs at least one segment"])
    _check_fanout_group(fanout_group)
    chained = len(segments) > 1
    scoped = chained or legs is not None
    leg_of = [(legs or {}).get(segment_name(m, i), segment_name(m, i)) for m, i, _ in segments]
    errors: list[str] = []
    parts = []
    tally: list = []
    for index, (manifest, inputs, schedule) in enumerate(segments):
        if nest_scratch and ((chained and index > 0) or inputs.segment_key):
            inputs = replace(inputs, scratch_dir=f"{inputs.scratch_dir.rstrip('/')}/{segment_name(manifest, inputs)}")
        label = (f"segment{index + 1}", leg_of[index]) if scoped else None
        parts.append((manifest, inputs, _segment(
            manifest, inputs, schedule, agent_type_host, errors, chained=scoped,
            segment=label, skip_guard=scoped and leg_of[index] in leg_of[:index],
            fanout_group=fanout_group, tally=tally, final=index == len(segments) - 1,
        )))
    if errors:
        raise PipelineEmitRefused(sorted(set(errors)))

    manifest, inputs, _ = parts[0]
    prefix = f"{RUN_ID_PREFIX}{manifest.pipeline}-"
    name = run_id if run_id.startswith(prefix) else prefix + run_id
    active = [stage for _, _, (_, _, act) in parts for stage in act]
    phases = ", ".join(_js_string_literal(title) for title in dict.fromkeys(stage_phase(s) for s in active))
    description = manifest.description or f"Pipeline {manifest.pipeline} over {len(inputs.subjects)} subject(s), run sequentially"
    cost = FanoutCost(
        units=sum(t[0] for t in tally),
        agents=sum(t[1] for t in tally),
        basis="lower-bound" if any(t[2] for t in tally) else "exact",
        groups=tuple(g for t in tally for g in t[3]),
    )
    meta = "\n".join([
        "export const meta = {",
        f"  name: {_js_string_literal(name)},",
        f"  description: {_js_string_literal(description)},",
        f"  phases: [{phases}],",
        "};",
    ])
    out = [
        with_fanout(meta, cost),
        "",
        f"const ITEM_MARK = {json.dumps(ITEM_MARK)};",
        _RELAY_DECL,
        _revisions_decl(name),
        _finish_decl(name),
    ]
    if not scoped:
        out += parts[0][2][0]
    fanned = [(m, s) for m, _, (_, _, act) in parts for s in act if s.fan_out.kind == FAN_OUT_OVER]
    if any(s.max_concurrent is None for _, s in fanned):
        out += ["", _FAN_OUT]
    if any(s.max_concurrent is not None for _, s in fanned):
        out += ["", _IN_CHUNKS]
    if any(s.fan_out.emit_time or _uses_item(s, m) for m, s in fanned):
        out += ["", _WITH_ITEM]
    out += ["", _PRODUCED, "", _FAN_TRAILER, ""]
    if scoped:
        out += ["const results = [];", "const segment_halts = [];", ""]
        for index, (_, _, (data, body, _)) in enumerate(parts):
            out += [f"segment{index + 1}: {{"] + data + body + ["}", ""]
    else:
        out += parts[0][2][1]
    if not scoped:
        if next_action is None:
            out += ["return results;", ""]
        else:
            out += [f"return {{ results, next_action: {json.dumps(dict(next_action), sort_keys=True)} }};", ""]
    elif next_action is None:
        out += ["return { results, segment_halts };", ""]
    else:
        pairs = json.dumps([[segment_name(m, i), leg] for (m, i, _), leg in zip(segments, leg_of)])
        out += [
            f"const NEXT_ACTION = {json.dumps(dict(next_action), sort_keys=True)};",
            f"const SEGMENT_LEGS = {pairs};",
            "const haltedLegs = new Set(segment_halts.map((h) => h.leg));",
            "const delivered = SEGMENT_LEGS.some(([, leg]) => !haltedLegs.has(leg));",
            "const haltedSegments = SEGMENT_LEGS.filter(([, leg]) => haltedLegs.has(leg)).map(([name]) => name);",
            "return {",
            "  results,",
            "  segment_halts,",
            "  ...(delivered ? { next_action: haltedLegs.size === 0 ? NEXT_ACTION : "
            "{ ...NEXT_ACTION, params: { ...NEXT_ACTION.params, halted_segments: [...new Set(haltedSegments)] } } } : {}),",
            "};",
            "",
        ]
    text = "\n".join(out)
    size = len(text.encode("utf-8"))
    if size > WORKFLOW_SCRIPT_CAP_BYTES:
        sizes = {
            segment_name(m, i): len("\n".join(data + body).encode("utf-8")) for m, i, (data, body, _) in parts
        }
        largest = max(sizes, key=sizes.__getitem__)
        raise _ScriptCapRefused([
            f"the script is {size} bytes, over the Workflow cap of {WORKFLOW_SCRIPT_CAP_BYTES}; "
            f"largest segment: {largest} ({sizes[largest]} bytes)"
        ])
    return text


def scouts_at_depth(segments: Sequence[Segment], depth: str) -> list[Segment]:
    """`segments` with the scouts pipeline's question fan-out stage raised from Haiku to Sonnet when `depth` is not `standard`.

    Only a stage fanning out over `inputs.questions` that the manifest pins to haiku changes; probe
    and grounding stages keep their own model. Callers pair this with `fanout_group="split"`: a
    deeper scout owns one question, never a group of them.
    """
    if depth == "standard":
        return list(segments)
    out: list[Segment] = []
    for manifest, inputs, schedule in segments:
        if manifest.pipeline == "scouts":
            stages = tuple(
                replace(s, model="sonnet")
                if s.model == "haiku" and s.fan_out.inputs == "questions" else s
                for s in manifest.stages
            )
            manifest = replace(manifest, stages=stages)
        out.append((manifest, inputs, schedule))
    return out


def _check_fanout_group(fanout_group: str) -> None:
    if fanout_group not in FANOUT_GROUP_MODES:
        raise PipelineEmitRefused([f"fanout_group must be one of {list(FANOUT_GROUP_MODES)}, got {fanout_group!r}"])


class _ScriptCapRefused(PipelineEmitRefused):
    """The composed script exceeds the Workflow cap; the only refusal a per-pipeline split can cure."""


@dataclass(frozen=True)
class ChainPart:
    """One Workflow script of a research emit and the segment names it runs, in chain order (the pipeline name for an unkeyed segment)."""

    pipelines: tuple[str, ...]
    script: str


def compose_chain_parts(
    segments: Sequence[Segment],
    *,
    run_id: str,
    agent_type_host: str | None,
    next_action: Mapping | None = None,
    legs: Mapping[str, str] | None = None,
    fanout_group: str = "auto",
) -> tuple[ChainPart, ...]:
    """The chain as one script when it fits the cap, else one script per leg, ordered by each leg's first segment.

    A split segment after the first keeps the `<scratch>/<pipeline>` layout the chained script uses,
    set by its index in the full chain; every split part is scoped, so a halt ends only its leg and
    a part returns `segment_halts`. Only the last part carries `next_action`. A part still over the
    cap raises PipelineEmitRefused.
    """
    _check_fanout_group(fanout_group)
    try:
        script = compose_chain_script(
            segments, run_id=run_id, agent_type_host=agent_type_host, next_action=next_action, legs=legs,
            fanout_group=fanout_group,
        )
    except _ScriptCapRefused:
        if len(segments) < 2:
            raise
    else:
        return (ChainPart(tuple(segment_name(m, i) for m, i, _ in segments), script),)
    nested = [
        (manifest, replace(inputs, scratch_dir=f"{inputs.scratch_dir.rstrip('/')}/{segment_name(manifest, inputs)}")
         if index > 0 or inputs.segment_key else inputs, schedule)
        for index, (manifest, inputs, schedule) in enumerate(segments)
    ]
    by_leg: dict[str, list[Segment]] = {}
    for segment in nested:
        name = segment_name(segment[0], segment[1])
        by_leg.setdefault((legs or {}).get(name, name), []).append(segment)
    groups = list(by_leg.values())
    return tuple(
        ChainPart(tuple(segment_name(m, i) for m, i, _ in group), compose_chain_script(
            group, run_id=run_id, agent_type_host=agent_type_host,
            next_action=next_action if index == len(groups) - 1 else None,
            legs=legs or {}, nest_scratch=False, fanout_group=fanout_group,
        ))
        for index, group in enumerate(groups)
    )


def compose_pipeline_script(
    manifest: Manifest,
    inputs: PipelineInputs,
    schedule: Schedule,
    *,
    run_id: str,
    agent_type_host: str | None,
    fanout_group: str = "auto",
) -> str:
    """The Workflow script text for `manifest` run over `inputs` on `schedule`; deterministic for fixed arguments."""
    return compose_chain_script(
        [(manifest, inputs, schedule)], run_id=run_id, agent_type_host=agent_type_host,
        fanout_group=fanout_group,
    )
