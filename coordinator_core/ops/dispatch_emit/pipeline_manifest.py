"""Load, shape-check, scope and schedule a DoE pipeline manifest (schema v1) for dispatch.emit.

load_manifest answers: what stage graph does this pipeline declare? validate answers: is that
graph emittable for these inputs, and in what levels does it run? Both collect every refusal
into one PipelineEmitRefused before raising. No spawn, no glob: the manifest and the files its
stages name are the only reads.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FAN_OUT_NONE, FAN_OUT_OVER, FAN_OUT_PER_SUBJECT, MANIFEST_SUFFIX, PIPELINES_DIR,
    PLACEHOLDER_RE, SCHEMA_VERSION, SCOPE_POST, SCOPE_PRE,
    SCOPE_SUBJECT, SUBJECTS_MODE_SEQUENTIAL, WHEN_FLAG, WHEN_NONEMPTY, WHEN_RETURN, FanOut,
    FlagSpec, Manifest, PipelineEmitRefused, PipelineInputs, Schedule, Stage, VALIDATOR_TOKEN, When,
    subject_key, subject_slug, validator_bound,
)

__all__ = ["load_manifest", "validate"]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_STAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_OVER_RE = re.compile(r"^stage\.([a-z0-9][a-z0-9_-]*)\.return\.([A-Za-z_][A-Za-z0-9_]*)$")
_OVER_SUBJECT_RE = re.compile(r"^subject\.([A-Za-z_][A-Za-z0-9_]*)$")
_OVER_INPUTS_RE = re.compile(r"^inputs\.([a-z][a-z0-9_]*)$")
_RETURN_REF_RE = re.compile(r"^stage\.([a-z0-9][a-z0-9_-]*)\.return\.([A-Za-z_][A-Za-z0-9_]*)$")
_NAME_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ITEM_TOKEN_RE = re.compile(r"^item\.([A-Za-z_][A-Za-z0-9_]*)$")
_FLAG_TOKEN_RE = re.compile(r"^flags\.([A-Za-z0-9_-]+)$")
_STAGE_OUT_RE = re.compile(r"^stage\.([a-z0-9][a-z0-9_-]*)\.output$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_HALT_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

_SCHEMA = json.loads(
    (Path(__file__).parent / "schemas" / "pipeline-manifest.schema.json").read_text(encoding="utf-8")
)
_TOP_KEYS = set(_SCHEMA["properties"])
_INPUT_KEYS = set(_SCHEMA["properties"]["inputs"]["properties"])
_FLAG_KEYS = {"allowed", "default"}
_LIST_KINDS = ("strings", "roster")
_MODELS = ("haiku", "sonnet", "opus")
_EFFORTS = ("low", "medium", "high")
_WHEN_KEYS = {"flag", "return", "nonempty", "equals"}
_STAGE_KEYS = set(_SCHEMA["$defs"]["stage"]["properties"])
_STAGE_REQUIRED = ("id", "template", "output")
_SCOPE_RANK = {SCOPE_PRE: 0, SCOPE_SUBJECT: 1, SCOPE_POST: 2}


def load_manifest(content_root: Path, pipeline: str) -> Manifest:
    """Read `<content_root>/pipelines/<pipeline>/manifest.yaml` and the files its stages name."""
    if not _NAME_RE.match(pipeline or ""):
        raise PipelineEmitRefused([f"pipeline name {pipeline!r} does not match ^[a-z0-9][a-z0-9-]*$"])
    pattern = f"{PIPELINES_DIR}/**/{pipeline}{MANIFEST_SUFFIX}"
    matches = sorted(p for p in (content_root / PIPELINES_DIR).rglob(f"{pipeline}{MANIFEST_SUFFIX}") if p.is_file())
    if not matches:
        raise PipelineEmitRefused([f"no pipeline manifest matches {content_root.as_posix()}/{pattern}"])
    if len(matches) > 1:
        raise PipelineEmitRefused(
            [f"{len(matches)} pipeline manifests match {pattern}:"] + [m.as_posix() for m in matches]
        )
    path = matches[0]
    root = path.parent
    raw = path.read_bytes()
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PipelineEmitRefused([f"{path.as_posix()}: not valid YAML: {exc}"]) from exc

    reasons: list[str] = []
    if not isinstance(doc, dict):
        raise PipelineEmitRefused([f"{path.as_posix()}: top level must be a mapping"])
    if doc.get("schema_version") != SCHEMA_VERSION:
        reasons.append(f"schema_version {doc.get('schema_version')!r} is not {SCHEMA_VERSION}")
    reasons += [f"unknown manifest key {k!r}" for k in doc if k not in _TOP_KEYS]
    if doc.get("pipeline") != pipeline:
        reasons.append(
            f"manifest pipeline {doc.get('pipeline')!r} does not match file stem {pipeline!r}"
        )

    description = doc.get("description")
    if description is not None and not (isinstance(description, str) and description.strip()):
        reasons.append("description must be a non-empty string")
        description = None
    subjects_mode = doc.get("subjects_mode")
    if subjects_mode is not None and subjects_mode != SUBJECTS_MODE_SEQUENTIAL:
        reasons.append(f"subjects_mode {subjects_mode!r} is not {SUBJECTS_MODE_SEQUENTIAL!r}")
        subjects_mode = None

    flags = _load_flags(doc.get("inputs"), reasons)
    lists = _load_lists(doc.get("inputs"), reasons)
    stages: list[Stage] = []
    templates: dict[str, str] = {}
    schemas: dict[str, dict] = {}
    raw_stages = doc.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        reasons.append("stages must be a non-empty list")
        raw_stages = []
    for index, raw_stage in enumerate(raw_stages):
        stage = _load_stage(raw_stage, index, root, templates, schemas, reasons)
        if stage is not None:
            stages.append(stage)

    _check_graph_shape(stages, schemas, flags, lists, reasons)
    if reasons:
        raise PipelineEmitRefused(reasons)
    return Manifest(
        pipeline=pipeline, root=root, flags=flags, stages=tuple(stages), templates=templates,
        schemas=schemas, sha256=hashlib.sha256(raw).hexdigest(), description=description,
        subjects_mode=subjects_mode, lists=lists,
    )


def _load_flags(inputs: object, reasons: list[str]) -> dict[str, FlagSpec]:
    flags: dict[str, FlagSpec] = {}
    if inputs is None:
        return flags
    if not isinstance(inputs, dict):
        reasons.append("inputs must be a mapping")
        return flags
    reasons += [f"unknown inputs key {k!r}" for k in inputs if k not in _INPUT_KEYS]
    raw_flags = inputs.get("flags") or {}
    if not isinstance(raw_flags, dict):
        reasons.append("inputs.flags must be a mapping")
        return flags
    for name, spec in raw_flags.items():
        if isinstance(spec, list):
            values = tuple(spec)
            if not values or len(set(values)) != len(values) or not all(isinstance(v, (str, bool)) for v in values):
                reasons.append(f"flag {name!r}: allowed values must be a non-empty unique list of strings or booleans")
                continue
            flags[str(name)] = FlagSpec(allowed=values, default=None)
            continue
        if not isinstance(spec, dict):
            reasons.append(f"flag {name!r} must be a list of allowed values (or a mapping with allowed and default)")
            continue
        reasons += [f"flag {name!r}: unknown key {k!r}" for k in spec if k not in _FLAG_KEYS]
        allowed = spec.get("allowed")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(v, str) for v in allowed):
            reasons.append(f"flag {name!r}: allowed must be a non-empty list of strings")
            continue
        default = spec.get("default")
        if default is not None and default not in allowed:
            reasons.append(f"flag {name!r}: default {default!r} is not in allowed {allowed}")
            continue
        flags[str(name)] = FlagSpec(allowed=tuple(allowed), default=default)
    return flags


def _load_lists(inputs: object, reasons: list[str]) -> dict[str, str]:
    raw = inputs.get("lists") if isinstance(inputs, dict) else None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        reasons.append("inputs.lists must be a mapping")
        return {}
    lists: dict[str, str] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not _NAME_KEY_RE.match(name):
            reasons.append(f"list name {name!r} must match ^[a-z][a-z0-9_]*$")
        elif not isinstance(spec, dict) or set(spec) != {"kind"} or spec["kind"] not in _LIST_KINDS:
            reasons.append(f"list {name!r} must be a mapping with only kind: strings | roster")
        else:
            lists[name] = spec["kind"]
    return lists


def _load_when(raw: object, label: str, reasons: list[str]) -> When | None:
    if not isinstance(raw, dict):
        reasons.append(f"{label}: when must be a mapping")
        return None
    reasons_before = len(reasons)
    reasons += [f"{label}: when has unknown key {k!r}" for k in raw if k not in _WHEN_KEYS]
    forms = [k for k in (WHEN_FLAG, WHEN_RETURN, WHEN_NONEMPTY) if k in raw]
    if len(forms) != 1:
        reasons.append(f"{label}: when needs exactly one of flag, return, nonempty")
        return None
    kind = forms[0]
    ref, equals = raw[kind], raw.get("equals")
    if equals is not None and not isinstance(equals, (str, bool)):
        reasons.append(f"{label}: when.equals must be a string or a boolean")
    if kind == WHEN_FLAG:
        if not isinstance(ref, str) or not _NAME_KEY_RE.match(ref):
            reasons.append(f"{label}: when.flag must be a flag name")
        if equals is None:
            equals = True
    else:
        if not isinstance(ref, str) or not _RETURN_REF_RE.match(ref):
            reasons.append(f"{label}: when.{kind} must be stage.<id>.return.<field>")
        if kind == WHEN_NONEMPTY and equals is not None:
            reasons.append(f"{label}: when.nonempty takes no equals")
        if kind == WHEN_RETURN and equals is None:
            equals = True
    if len(reasons) > reasons_before:
        return None
    return When(kind=kind, ref=ref, equals=equals)


def _load_stage(
    raw: object, index: int, root: Path, templates: dict[str, str], schemas: dict[str, dict],
    reasons: list[str],
) -> Stage | None:
    if not isinstance(raw, dict):
        reasons.append(f"stages[{index}] must be a mapping")
        return None
    sid = raw.get("id")
    label = f"stage {sid!r}" if isinstance(sid, str) else f"stages[{index}]"
    before = len(reasons)
    reasons += [f"{label}: unknown key {k!r}" for k in raw if k not in _STAGE_KEYS]
    reasons += [f"{label}: missing key {k!r}" for k in _STAGE_REQUIRED if k not in raw]
    if isinstance(sid, str) and not _STAGE_ID_RE.match(sid):
        reasons.append(f"{label}: id must match ^[a-z0-9][a-z0-9_-]*$")
    for key in ("agent_type", "model", "template", "output", "phase", "agent_type_from", "effort"):
        if key in raw and not isinstance(raw[key], str):
            reasons.append(f"{label}: {key} must be a string")
    if "agent_type" not in raw and "agent_type_from" not in raw:
        reasons.append(f"{label}: needs agent_type or agent_type_from")
    if isinstance(raw.get("phase"), str) and not raw["phase"].strip():
        reasons.append(f"{label}: phase must be non-empty")
    if isinstance(raw.get("model"), str) and raw["model"] not in _MODELS:
        reasons.append(f"{label}: model {raw['model']!r} is not one of {', '.join(_MODELS)}")
    if isinstance(raw.get("effort"), str) and raw["effort"] not in _EFFORTS:
        reasons.append(f"{label}: effort {raw['effort']!r} is not one of {', '.join(_EFFORTS)}")
    from_ref = raw.get("agent_type_from")
    if isinstance(from_ref, str) and not _OVER_INPUTS_RE.match(from_ref):
        reasons.append(f"{label}: agent_type_from {from_ref!r} is not inputs.<roster>")
    max_concurrent = raw.get("max_concurrent")
    if max_concurrent is not None:
        if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) or max_concurrent < 1:
            reasons.append(f"{label}: max_concurrent must be an integer of at least 1")
    optional = raw.get("optional", False)
    if not isinstance(optional, bool):
        reasons.append(f"{label}: optional must be true or false")
        optional = False
    produces_brief = raw.get("produces_brief", False)
    if not isinstance(produces_brief, bool):
        reasons.append(f"{label}: produces_brief must be true or false")
        produces_brief = False
    halts_unless = raw.get("halts_unless")
    if halts_unless is not None and not (isinstance(halts_unless, str) and _HALT_FIELD_RE.match(halts_unless)):
        reasons.append(f"{label}: halts_unless must be a field name matching ^[A-Za-z][A-Za-z0-9_]*$")
        halts_unless = None
    when = _load_when(raw["when"], label, reasons) if "when" in raw else None

    depends = raw.get("depends_on") or []
    if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
        reasons.append(f"{label}: depends_on must be a list of stage ids")
        depends = []
    web_caller = raw.get("web_caller", False)
    if not isinstance(web_caller, bool):
        reasons.append(f"{label}: web_caller must be true or false")
        web_caller = False
    fan_out = _load_fan_out(raw.get("fan_out", FAN_OUT_NONE), label, reasons)

    template, schema = raw.get("template"), raw.get("schema")
    if isinstance(template, str):
        text = _read_named(root, template, f"{label} template", reasons)
        if text is not None:
            templates[template] = text
    if isinstance(schema, dict):
        if "type" not in schema:
            reasons.append(f"{label}: inline schema needs a type")
        else:
            schemas[f"<inline:{sid}>"] = schema
            schema = f"<inline:{sid}>"
    elif schema is not None:
        if not isinstance(schema, str):
            reasons.append(f"{label}: schema must be a path or an inline object")
        else:
            body = _read_named(root, schema, f"{label} schema", reasons)
            if body is not None:
                try:
                    parsed = json.loads(body)
                except ValueError as exc:
                    reasons.append(f"{label} schema {schema!r}: not valid JSON: {exc}")
                else:
                    if isinstance(parsed, dict):
                        schemas[schema] = parsed
                    else:
                        reasons.append(f"{label} schema {schema!r}: must be a JSON object")
    if len(reasons) > before or fan_out is None or not isinstance(sid, str):
        return None
    return Stage(
        id=sid, agent_type=raw.get("agent_type"), model=raw.get("model"), template=template,
        schema=schema, web_caller=web_caller, depends_on=tuple(depends), fan_out=fan_out,
        output=raw["output"], phase=raw.get("phase"), agent_type_from=from_ref,
        effort=raw.get("effort"), max_concurrent=max_concurrent, when=when, optional=optional,
        produces_brief=produces_brief, halts_unless=halts_unless,
    )


def _load_fan_out(raw: object, label: str, reasons: list[str]) -> FanOut | None:
    if raw in (FAN_OUT_NONE, FAN_OUT_PER_SUBJECT):
        return FanOut(kind=raw)
    if isinstance(raw, dict) and set(raw) == {"over"} and isinstance(raw["over"], str):
        match = _OVER_RE.match(raw["over"])
        if match:
            return FanOut(kind=FAN_OUT_OVER, source=match.group(1), field=match.group(2))
        match = _OVER_SUBJECT_RE.match(raw["over"])
        if match:
            return FanOut(kind=FAN_OUT_OVER, field=match.group(1))
        match = _OVER_INPUTS_RE.match(raw["over"])
        if match:
            return FanOut(kind=FAN_OUT_OVER, inputs=match.group(1))
        reasons.append(
            f"{label}: fan_out.over {raw['over']!r} is not stage.<id>.return.<field>, "
            "subject.<field> or inputs.<list>"
        )
        return None
    reasons.append(f"{label}: fan_out must be none, per_subject, or {{over: stage.<id>.return.<field> | subject.<field> | inputs.<list>}}")
    return None


def _read_named(root: Path, name: str, what: str, reasons: list[str]) -> str | None:
    if "\\" in name or _DRIVE_RE.match(name):
        reasons.append(f"{what} path {name!r} must be a POSIX path relative to the manifest directory")
        return None
    resolved = contained_path(root / name, [root])
    if resolved is None:
        reasons.append(f"{what} path {name!r} resolves outside {root.as_posix()}")
        return None
    if not resolved.is_file():
        reasons.append(f"{what} file not found: {(root / name).as_posix()}")
        return None
    return resolved.read_text(encoding="utf-8")


def _return_field_type(by_id: dict[str, Stage], schemas: dict[str, dict], stage_id: str, field: str) -> str | None:
    """The declared JSON type of `field` in the stage's return schema; None when undeclared."""
    source = by_id.get(stage_id)
    if source is None or source.schema is None or source.schema not in schemas:
        return None
    prop = (schemas[source.schema].get("properties") or {}).get(field)
    return prop.get("type") if isinstance(prop, dict) else None


def _check_halts_unless(stage: Stage, schemas: dict[str, dict], reasons: list[str]) -> None:
    name = stage.halts_unless
    if stage.fan_out.kind == FAN_OUT_OVER:
        reasons.append(f"stage {stage.id!r}: halts_unless is not legal on a stage fanned over a list")
    schema = schemas.get(stage.schema or "") if stage.schema and stage.schema.startswith("<inline:") else None
    if schema is None:
        reasons.append(f"stage {stage.id!r}: halts_unless needs an inline object schema")
        return
    props = schema.get("properties") or {}
    field = props.get(name)
    if name not in (schema.get("required") or []) or not isinstance(field, dict) or field.get("type") != "boolean":
        reasons.append(f"stage {stage.id!r}: halts_unless {name!r} is not a required boolean field of its schema")
    remedy = props.get("remedy")
    if not (
        isinstance(remedy, dict) and remedy.get("type") == "array"
        and isinstance(remedy.get("items"), dict) and remedy["items"].get("type") == "string"
    ):
        reasons.append(f"stage {stage.id!r}: halts_unless needs a remedy property typed array of string")


def _check_graph_shape(
    stages: list[Stage], schemas: dict[str, dict], flags: dict[str, FlagSpec],
    lists: dict[str, str], reasons: list[str],
) -> None:
    by_id: dict[str, Stage] = {}
    position: dict[str, int] = {}
    for index, stage in enumerate(stages):
        if stage.id in by_id:
            reasons.append(f"duplicate stage id {stage.id!r}")
        by_id[stage.id] = stage
        position.setdefault(stage.id, index)
    for stage in stages:
        for dep in stage.depends_on:
            if dep not in by_id:
                reasons.append(f"stage {stage.id!r}: depends_on unknown stage {dep!r}")
        if stage.agent_type_from is not None:
            name = _OVER_INPUTS_RE.match(stage.agent_type_from)
            if name and lists.get(name.group(1)) != "roster":
                reasons.append(f"stage {stage.id!r}: agent_type_from {stage.agent_type_from!r} is not a declared roster list")
            if stage.fan_out.kind != FAN_OUT_OVER:
                reasons.append(f"stage {stage.id!r}: agent_type_from needs a fan_out over a list")
        if stage.produces_brief and (position[stage.id] != 0 or stage.fan_out.kind != FAN_OUT_NONE):
            reasons.append(f"stage {stage.id!r}: produces_brief is legal only on stages[0] with fan_out none")
        if stage.halts_unless is not None:
            _check_halts_unless(stage, schemas, reasons)
        if stage.fan_out.over_inputs and stage.fan_out.inputs not in lists:
            reasons.append(f"stage {stage.id!r}: fan_out.over names undeclared list {stage.fan_out.inputs!r}")
        when = stage.when
        if when is not None and when.kind == WHEN_FLAG and when.ref not in flags:
            reasons.append(f"stage {stage.id!r}: when.flag names undeclared flag {when.ref!r}")
        if when is not None and when.kind != WHEN_FLAG:
            if when.stage not in by_id or position.get(when.stage, 0) >= position[stage.id]:
                reasons.append(f"stage {stage.id!r}: when.{when.kind} names stage {when.stage!r}, which is not declared earlier")
            else:
                want = "boolean" if when.kind == WHEN_RETURN else "array"
                if _return_field_type(by_id, schemas, when.stage, when.field) != want:
                    reasons.append(
                        f"stage {stage.id!r}: when.{when.kind} {when.ref!r}: stage {when.stage!r} "
                        f"schema has no {want} field {when.field!r}"
                    )
        if stage.fan_out.kind != FAN_OUT_OVER or stage.fan_out.emit_time:
            continue
        source = by_id.get(stage.fan_out.source or "")
        if source is None:
            reasons.append(f"stage {stage.id!r}: fan_out.over names unknown stage {stage.fan_out.source!r}")
            continue
        if source.schema is None or source.schema not in schemas:
            reasons.append(f"stage {stage.id!r}: over-source {source.id!r} declares no schema")
            continue
        if _return_field_type(by_id, schemas, source.id, stage.fan_out.field) != "array":
            reasons.append(
                f"stage {stage.id!r}: over-source {source.id!r} schema has no array field "
                f"{stage.fan_out.field!r}"
            )


_MISSING = object()


def validate(manifest: Manifest, inputs: PipelineInputs) -> Schedule:
    """Return the Schedule for these inputs, or raise PipelineEmitRefused with every refusal."""
    reasons: list[str] = []
    by_id = {s.id: s for s in manifest.stages}
    deps = {s.id: tuple(d for d in s.depends_on if d in by_id) for s in manifest.stages}

    _check_inputs(manifest, inputs, reasons)
    order, cyclic = _kahn(manifest.stages, deps)
    if cyclic:
        reasons.append(f"dependency cycle among stages: {', '.join(cyclic)}")
    skipped = _skipped(manifest, inputs, by_id, order, reasons)
    _check_lists(manifest, inputs, skipped, reasons)

    closure: dict[str, frozenset[str]] = {}
    effective: dict[str, tuple[str, ...]] = {}
    scope: dict[str, str] = {}
    for sid in order:
        stage = by_id[sid]
        closure[sid] = frozenset(deps[sid]).union(*(closure[d] for d in deps[sid]))
        effective[sid] = tuple(dict.fromkeys(
            x for d in deps[sid] for x in ((d,) if d not in skipped else effective[d])
        ))
        if sid in skipped:
            continue
        scope[sid] = _scope_of(manifest, stage, effective[sid], scope, reasons)
        sources = [stage.fan_out.source] if stage.fan_out.kind == FAN_OUT_OVER else []
        if stage.when is not None and stage.when.stage:
            sources.append(stage.when.stage)
        for source in sources:
            if source in by_id and source not in closure[sid]:
                reasons.append(f"stage {sid!r}: over-source {source!r} is not in its depends_on chain")
        if stage.fan_out.over_subject:
            _check_subject_lists(stage, manifest, inputs, reasons)
        if scope[sid] == SCOPE_SUBJECT and not inputs.subjects:
            reasons.append(f"stage {sid!r} runs per subject but no subjects were given")

    for stage in manifest.stages:
        _check_stage_tokens(stage, manifest, inputs, scope.get(stage.id), closure.get(stage.id), by_id, reasons)

    levels = _levels([sid for sid in order if sid not in skipped], effective, scope, by_id)
    if reasons:
        raise PipelineEmitRefused(reasons)
    return Schedule(scope=scope, levels=levels, skipped=frozenset(skipped))


def _coerce_flag(spec: FlagSpec, raw: object) -> object:
    """A boolean flag takes `true`/`false` text from a CLI; every other value passes through."""
    if isinstance(raw, str) and any(isinstance(a, bool) for a in spec.allowed) and raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw


def _flag_value(manifest: Manifest, inputs: PipelineInputs, name: str) -> object:
    """The flag's effective value: the caller's, else its default, else false for a boolean flag; else _MISSING."""
    spec = manifest.flags.get(name)
    if spec is None:
        return _MISSING
    if name in inputs.flags:
        return _coerce_flag(spec, inputs.flags[name])
    if spec.default is not None:
        return spec.default
    return False if any(a is False for a in spec.allowed) else _MISSING


def _skipped(
    manifest: Manifest, inputs: PipelineInputs, by_id: dict[str, Stage], order: list[str],
    reasons: list[str],
) -> set[str]:
    """Stages a flag condition (or a skipped over-source / when-source) removes from this emission."""
    skipped: set[str] = set()
    for sid in order:
        stage = by_id[sid]
        when = stage.when
        if inputs.validator is None and validator_bound(stage, manifest):
            skipped.add(sid)
        if when is not None and when.kind == WHEN_FLAG:
            value = _flag_value(manifest, inputs, when.ref)
            if value is _MISSING:
                allowed = ", ".join(str(a).lower() if isinstance(a, bool) else str(a) for a in manifest.flags[when.ref].allowed)
                reasons.append(
                    f"stage {sid!r}: flag {when.ref!r} has no value; pass --flag {when.ref}=<{allowed}>"
                )
            elif value != when.equals:
                skipped.add(sid)
        elif when is not None and when.stage in skipped:
            skipped.add(sid)
        if stage.fan_out.kind == FAN_OUT_OVER and stage.fan_out.source in skipped:
            skipped.add(sid)
    return skipped


def _check_inputs(manifest: Manifest, inputs: PipelineInputs, reasons: list[str]) -> None:
    for name, value in inputs.flags.items():
        spec = manifest.flags.get(name)
        if spec is None:
            reasons.append(f"unknown flag {name!r}; declared: {', '.join(sorted(manifest.flags)) or 'none'}")
        elif _coerce_flag(spec, value) not in spec.allowed:
            reasons.append(f"flag {name!r} value {value!r} is not in allowed {list(spec.allowed)}")
    if inputs.validator is not None:
        if not inputs.validator.strip() or "\n" in inputs.validator:
            reasons.append("validator must be a non-empty single-line command")
        elif not any(validator_bound(s, manifest) for s in manifest.stages):
            reasons.append(
                f"validator given, but no stage of {manifest.pipeline!r} names {{{{{VALIDATOR_TOKEN}}}}}"
            )
    for name in inputs.lists:
        if name not in manifest.lists:
            reasons.append(f"unknown list {name!r}; declared: {', '.join(sorted(manifest.lists)) or 'none'}")
    keys: list[str] = []
    for index, subject in enumerate(inputs.subjects):
        key = subject.get("subject") if isinstance(subject, dict) else subject
        if not isinstance(key, str) or not key.strip():
            reasons.append(f"subjects[{index}] must be a non-empty string or an object with a string 'subject' key")
        else:
            keys.append(key)
    if manifest.subjects_mode and len({subject_slug(k) for k in keys}) != len(keys):
        reasons.append("subjects have duplicate (or slug-colliding) keys; their scratch dirs would collide")
    scratch = inputs.scratch_dir
    if (
        not scratch or "\\" in scratch or scratch.startswith("/") or _DRIVE_RE.match(scratch)
        or ".." in scratch.split("/")
    ):
        reasons.append(f"scratch_dir {scratch!r} must be a repo-relative POSIX path without '..'")


def _check_lists(manifest: Manifest, inputs: PipelineInputs, skipped: set[str], reasons: list[str]) -> None:
    """Every list an active stage fans over or draws agent types from must be given and well formed."""
    needed: dict[str, str] = {}
    for stage in manifest.stages:
        if stage.id in skipped:
            continue
        if stage.fan_out.over_inputs:
            needed.setdefault(stage.fan_out.inputs, stage.id)
        if stage.agent_type_from:
            match = _OVER_INPUTS_RE.match(stage.agent_type_from)
            if match:
                needed.setdefault(match.group(1), stage.id)
    for name, sid in needed.items():
        kind = manifest.lists.get(name)
        values = inputs.lists.get(name)
        if kind is None:
            continue
        if not values:
            reasons.append(f"stage {sid!r} needs list {name!r}, which was not given (--list {name}=...)")
            continue
        if kind == "strings":
            names = [v for v in values if isinstance(v, str) and v.strip()]
            if len(names) != len(values):
                reasons.append(f"list {name!r} must hold non-empty strings")
        else:
            bad = [
                v for v in values
                if not (isinstance(v, dict) and all(isinstance(v.get(k), str) and v[k].strip() for k in ("slug", "agent_type")))
            ]
            if bad:
                reasons.append(f"list {name!r} must hold objects with a slug and an agent_type (slug=agent_type)")
            names = [v["slug"] for v in values if v not in bad]
        if len(set(names)) != len(names):
            reasons.append(f"list {name!r} has duplicate entries")


def _item_fields(stage: Stage, manifest: Manifest) -> set[str]:
    """Names of the `{{item.<field>}}` placeholders in the stage's template and output."""
    texts = (manifest.templates.get(stage.template, ""), stage.output)
    return {
        m.group(1)
        for text in texts for tok in PLACEHOLDER_RE.findall(text)
        if (m := _ITEM_TOKEN_RE.match(tok))
    }


def _check_subject_lists(
    stage: Stage, manifest: Manifest, inputs: PipelineInputs, reasons: list[str],
) -> None:
    """Every subject must carry the list field an over-subject stage fans out across."""
    field = stage.fan_out.field
    used = _item_fields(stage, manifest)
    for subject in inputs.subjects:
        name = subject_key(subject) if isinstance(subject, dict) and isinstance(subject.get("subject"), str) else subject
        items = subject.get(field) if isinstance(subject, dict) else None
        if not isinstance(items, list) or not items:
            reasons.append(f"stage {stage.id!r}: subject {name!r} has no non-empty list field {field!r}")
            continue
        if used:
            seen: dict[tuple, int] = {}
            for number, item in enumerate(items, 1):
                if isinstance(item, dict):
                    key = tuple(str(item.get(f)).lower() for f in sorted(used))
                    if key in seen:
                        reasons.append(
                            f"stage {stage.id!r}: subject {name!r} {field}[{number}] repeats {field}[{seen[key]}]; "
                            "their output and mailbox files would collide"
                        )
                    seen.setdefault(key, number)
        for number, item in enumerate(items, 1):
            if not used:
                continue
            if not isinstance(item, dict):
                reasons.append(f"stage {stage.id!r}: subject {name!r} {field}[{number}] must be an object")
                continue
            reasons += [
                f"stage {stage.id!r}: subject {name!r} {field}[{number}] has no scalar field {f!r}"
                for f in sorted(used) if not isinstance(item.get(f), (str, int, float, bool))
            ]


def _kahn(stages: tuple[Stage, ...], deps: dict[str, tuple[str, ...]]) -> tuple[list[str], list[str]]:
    remaining = {s.id: set(deps[s.id]) for s in stages}
    order: list[str] = []
    while True:
        ready = [sid for sid, pending in remaining.items() if not pending]
        if not ready:
            break
        for sid in ready:
            order.append(sid)
            del remaining[sid]
        for pending in remaining.values():
            pending.difference_update(ready)
    return order, list(remaining)


def _scope_of(
    manifest: Manifest, stage: Stage, stage_deps: tuple[str, ...], scope: dict[str, str],
    reasons: list[str],
) -> str:
    if manifest.subjects_mode:
        return SCOPE_SUBJECT
    dep_scopes = [scope[d] for d in stage_deps]
    if stage.fan_out.kind == FAN_OUT_PER_SUBJECT or stage.fan_out.over_subject:
        if SCOPE_POST in dep_scopes:
            late = [d for d in stage_deps if scope[d] == SCOPE_POST]
            reasons.append(
                f"stage {stage.id!r} runs per subject but depends on post-loop stage(s): {', '.join(late)}"
            )
        return SCOPE_SUBJECT
    if stage.fan_out.kind == FAN_OUT_OVER:
        if stage.fan_out.source in scope:
            dep_scopes.append(scope[stage.fan_out.source])
        return max(dep_scopes, key=_SCOPE_RANK.__getitem__, default=SCOPE_PRE)
    return SCOPE_PRE if all(s == SCOPE_PRE for s in dep_scopes) else SCOPE_POST


def _levels(
    order: list[str], deps: dict[str, tuple[str, ...]], scope: dict[str, str], by_id: dict[str, Stage],
) -> dict[str, tuple[tuple[str, ...], ...]]:
    depth: dict[str, int] = {}
    for sid in order:
        same = [depth[d] for d in deps[sid] if scope[d] == scope[sid]]
        depth[sid] = 1 + max(same) if same else 0
    out: dict[str, tuple[tuple[str, ...], ...]] = {}
    for scope_name in (SCOPE_PRE, SCOPE_SUBJECT, SCOPE_POST):
        members = [sid for sid in by_id if sid in depth and scope[sid] == scope_name]
        if not members:
            continue
        out[scope_name] = tuple(
            tuple(sid for sid in members if depth[sid] == d)
            for d in range(1 + max(depth[sid] for sid in members))
        )
    return out


def _check_stage_tokens(
    stage: Stage, manifest: Manifest, inputs: PipelineInputs, scope: str | None,
    closure: frozenset[str] | None, by_id: dict[str, Stage], reasons: list[str],
) -> None:
    """Collect a refusal for every placeholder in the stage's template and output that cannot fill."""
    own = None if closure is None else closure | {stage.id}

    def check(text: str, where: str) -> None:
        for token in PLACEHOLDER_RE.findall(text):
            problem = _token_problem(token, manifest, inputs, scope, own, by_id, stage.fan_out)
            if problem:
                reasons.append(f"stage {stage.id!r} {where}: {{{{{token}}}}} {problem}")

    check(manifest.templates.get(stage.template, ""), f"template {stage.template!r}")
    check(stage.output, "output")
    filled = _fill_for_containment(stage.output, inputs, by_id)
    prefix = inputs.scratch_dir.rstrip("/") + "/"
    if filled is not None and not filled.startswith(prefix):
        reasons.append(f"stage {stage.id!r} output {stage.output!r} is outside scratch_dir {inputs.scratch_dir!r}")


def _token_problem(
    token: str, manifest: Manifest, inputs: PipelineInputs, scope: str | None,
    closure: frozenset[str] | None, by_id: dict[str, Stage], fan_out: FanOut | None = None,
) -> str | None:
    if token in ("brief", "scratch_dir", VALIDATOR_TOKEN):
        return None
    if token == "item":
        return None if fan_out is not None and fan_out.kind == FAN_OUT_OVER else "is out of scope: the stage does not fan out over a list"
    if _ITEM_TOKEN_RE.match(token):
        return None if fan_out is not None and fan_out.over_subject else "is out of scope: the stage does not fan out over a subject field"
    if token == "subject":
        return None if scope in (None, SCOPE_SUBJECT) else "is out of scope: the stage does not run per subject"
    flag = _FLAG_TOKEN_RE.match(token)
    if flag:
        name = flag.group(1)
        spec = manifest.flags.get(name)
        if spec is None:
            return f"names undeclared flag {name!r}"
        if name not in inputs.flags and spec.default is None:
            return f"references flag {name!r}, which has no value and no default"
        return None
    out = _STAGE_OUT_RE.match(token)
    if out:
        target = out.group(1)
        if target not in by_id:
            return f"names unknown stage {target!r}"
        if closure is not None and target not in closure:
            return f"is out of scope: {target!r} is not a dependency of this stage"
        if any(_ITEM_TOKEN_RE.match(tok) for tok in PLACEHOLDER_RE.findall(by_id[target].output)):
            return f"is out of scope: the output of {target!r} varies per item"
        return None
    return "is not a known placeholder"


def _fill_for_containment(text: str, inputs: PipelineInputs, by_id: dict[str, Stage]) -> str | None:
    """Fill an output path with a dummy subject; None when it holds a token this check cannot fill."""
    unfillable = False

    def sub(match: re.Match) -> str:
        nonlocal unfillable
        token = match.group(1)
        if token == "scratch_dir":
            return inputs.scratch_dir
        if token == "subject":
            return "subject"
        if token in ("brief", "item", VALIDATOR_TOKEN) or _FLAG_TOKEN_RE.match(token) or _ITEM_TOKEN_RE.match(token):
            return "x"
        out = _STAGE_OUT_RE.match(token)
        if out and out.group(1) in by_id:
            return inputs.scratch_dir + "/x"
        unfillable = True
        return ""

    filled = PLACEHOLDER_RE.sub(sub, text)
    if unfillable:
        return None
    parts: list[str] = []
    for seg in filled.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    return "/".join(parts)
