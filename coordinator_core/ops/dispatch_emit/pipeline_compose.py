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
from typing import Mapping

from coordinator_core.ops.dispatch_emit.emit import _degrade_agent_type
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FAN_OUT_OVER,
    ITEM_MARK,
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
    stage_phase,
    subject_key,
    subject_slug,
)
from coordinator_core.ops.workflow_scaffold import _js_string_literal

__all__ = ["compose_pipeline_script"]

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
    _seen: tuple[str, ...] = (),
) -> str:
    """One `re.sub` pass; an unresolvable token is recorded in `errors` and left in place."""

    def value(match) -> str:
        token = match.group(1)
        if token == "brief":
            return inputs.brief
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


def _agent_options(stage: Stage, manifest: Manifest, agent_type: str, agent_type_host: str | None) -> str:
    options = [f"agentType: {_js_string_literal(_degrade_agent_type(agent_type, agent_type_host))}"]
    if stage.model is not None:
        options.append(f"model: {_js_string_literal(stage.model)}")
    if stage.effort is not None:
        options.append(f"effort: {_js_string_literal(stage.effort)}")
    if stage.schema is not None:
        options.append(f"schema: {json.dumps(manifest.schemas[stage.schema], sort_keys=True)}")
    return ", ".join(options)


def _agent_call(
    stage: Stage, manifest: Manifest, inputs: PipelineInputs, agent_type_host: str | None,
    prompt: str, label: str, slug: str,
) -> str:
    """One `produced(` expression, which rejects an empty return; a roster-typed stage gets one literal call site per roster agent type."""
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


def _has_item(text: str) -> bool:
    return "item" in PLACEHOLDER_RE.findall(text)


def _uses_item(stage: Stage, manifest: Manifest) -> bool:
    return _has_item(manifest.templates.get(stage.template, "")) or _has_item(stage.output)


def _stage_expression(
    stage: Stage,
    scope: str,
    manifest: Manifest,
    inputs: PipelineInputs,
    stages: Mapping[str, Stage],
    schedule: Schedule,
    agent_type_host: str | None,
) -> str:
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
        expr = _agent_call(stage, manifest, inputs, agent_type_host, prompt, label, "item")
    elif stage.fan_out.emit_time:
        call = _agent_call(stage, manifest, inputs, agent_type_host, "item.prompt", item_label, "item.slug")
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
        call = _agent_call(stage, manifest, inputs, agent_type_host, fill, item_label, "item")
        expr = (
            "(async () => {\n"
            f"      const items = {items};\n"
            "      if (items.length === 0) return [];\n"
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
) -> list[str]:
    target = _RETURN_NAME[scope]
    lines: list[str] = []
    for level in schedule.levels.get(scope, ()):
        for title in dict.fromkeys(stage_phase(stages[i]) for i in level):
            lines.append(f"{indent}phase({_js_string_literal(title)});")
        exprs = [
            _stage_expression(stages[i], scope, manifest, inputs, stages, schedule, agent_type_host)
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
        lines.append("")
    return lines


def _element_value(element: object) -> object:
    """What `{{item}}` binds for one list element: a roster entry binds its slug."""
    return element["slug"] if isinstance(element, dict) and "slug" in element else element


def compose_pipeline_script(
    manifest: Manifest,
    inputs: PipelineInputs,
    schedule: Schedule,
    *,
    run_id: str,
    agent_type_host: str | None,
) -> str:
    """The Workflow script text for `manifest` run over `inputs` on `schedule`; deterministic for fixed arguments."""
    stages = {stage.id: stage for stage in manifest.stages}
    errors: list[str] = []
    if (
        ITEM_MARK in inputs.brief
        or ITEM_MARK in (inputs.validator or "")
        or any(ITEM_MARK in json.dumps(sub) for sub in inputs.subjects)
    ):
        errors.append("the brief, the validator or a subject carries the reserved item marker")

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
            else:
                runtime_item = ITEM_MARK if stage.fan_out.kind == FAN_OUT_OVER else None
                prompts[stage.id], outputs[stage.id] = fill_stage(stage, name, runtime_item)
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
    if errors:
        raise PipelineEmitRefused(sorted(set(errors)))

    prefix = f"{RUN_ID_PREFIX}{manifest.pipeline}-"
    name = run_id if run_id.startswith(prefix) else prefix + run_id
    active = [stage for stage in manifest.stages if stage.id not in schedule.skipped]
    phases = ", ".join(_js_string_literal(title) for title in dict.fromkeys(stage_phase(s) for s in active))
    description = manifest.description or f"Pipeline {manifest.pipeline} over {len(inputs.subjects)} subject(s), run sequentially"
    has_common = any(common[s]["prompts"] or common[s].get("items") for s in common)
    roster_names = sorted({
        s.agent_type_from.split(".", 1)[1] for s in active if s.agent_type_from is not None
    })
    out = [
        "export const meta = {",
        f"  name: {_js_string_literal(name)},",
        f"  description: {_js_string_literal(description)},",
        f"  phases: [{phases}],",
        "};",
        "",
        f"const ITEM_MARK = {json.dumps(ITEM_MARK)};",
        f"const subjects = {json.dumps(subject_data, ensure_ascii=True, indent=1)};",
    ]
    if has_common:
        merged = {
            "prompts": {**common[SCOPE_PRE]["prompts"], **common[SCOPE_POST]["prompts"]},
            "outputs": {**common[SCOPE_PRE]["outputs"], **common[SCOPE_POST]["outputs"]},
        }
        merged_items = {**common[SCOPE_PRE].get("items", {}), **common[SCOPE_POST].get("items", {})}
        if merged_items:
            merged["items"] = merged_items
        out.append(f"const common = {json.dumps(merged, ensure_ascii=True, indent=1)};")
    if roster_names:
        roster = {n: {e["slug"]: e["agent_type"] for e in inputs.lists[n]} for n in roster_names}
        out.append(f"const rosterTypes = {json.dumps(roster, ensure_ascii=True, indent=1)};")
    fanned = [s for s in active if s.fan_out.kind == FAN_OUT_OVER]
    if any(s.max_concurrent is None for s in fanned):
        out += ["", _FAN_OUT]
    if any(s.max_concurrent is not None for s in fanned):
        out += ["", _IN_CHUNKS]
    if any(not s.fan_out.emit_time and _uses_item(s, manifest) for s in fanned):
        out += ["", _WITH_ITEM]
    out += ["", _PRODUCED, "", _FAN_TRAILER, ""]
    out += ["const pre = {};", "const post = {};", "const subjRets = [];", "const results = [];", ""]
    out += _level_lines(SCOPE_PRE, manifest, inputs, schedule, stages, agent_type_host, "")
    out += ["for (const s of subjects) {", "  const ret = {};", "  subjRets.push(ret);", "  try {"]
    out += _level_lines(SCOPE_SUBJECT, manifest, inputs, schedule, stages, agent_type_host, "    ")
    out += [
        "    results.push({ subject: s.subject, returns: ret });",
        "  } catch (err) {",
        "    results.push({ subject: s.subject, returns: ret, error: String(err && err.message || err) });",
        "  }",
        "}",
        "",
    ]
    out += _level_lines(SCOPE_POST, manifest, inputs, schedule, stages, agent_type_host, "")
    for scope in (SCOPE_PRE, SCOPE_POST):
        if schedule.levels.get(scope):
            out.append(f"results.push({{ scope: {_js_string_literal(scope)}, returns: {_RETURN_NAME[scope]} }});")
    out += ["return results;", ""]
    return "\n".join(out)
