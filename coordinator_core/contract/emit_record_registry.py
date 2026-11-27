"""Generate record-registry.json from the record schemas plus the authored glob and join tables."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

_REPO = Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _REPO / "coordinator_core" / "frontmatter" / "schemas"
_MEMO_SCHEMAS = (
    _REPO / "coordinator_core" / "contract" / "cross-repo-memo.schema.json",
    _REPO / "coordinator_core" / "contract" / "archived-memo.schema.json",
)
OUTPUTS: Dict[str, Path] = {
    "record-registry.json": _REPO / "coordinator_core" / "contract" / "record-registry.json",
}
_GENERATOR = "python -m coordinator_core.contract.emit_record_registry"
REGISTRY_VERSION = 1
TYPE_VOCABULARY = [
    "array", "boolean", "date", "datetime", "integer", "number", "object", "string", "union",
]
MATCH_VOCABULARY = {
    "equals": "from_field value equals to_field value of the target record",
    "in_list": "an element of the from_field array equals to_field value of the target record",
    "path": "from_field holds the repo-relative path of the target record",
    "body_citation": (
        "the record body cites the target's to_field value, found by `pattern`; "
        "a hit absent from the target id set is dropped"
    ),
    "body_link": (
        "the record body holds a markdown link, found by `pattern` group 1, to the target's "
        "repo-relative path; the link resolves against the linking file and a miss is dropped"
    ),
}
JOIN_OPTION_VOCABULARY = {
    "item_field": "from_field is an array of objects; the match reads this key of each element",
    "skip_values": "from_field values equal to one of these are not joined",
}
GLOB_ROOT = "repo-root"
EXTENSION_HOME = ".coordinator/record-registry.extension.json"

# trap: applies_to is null or a placeholder for these; the globs below are authored, not read.
_AUTHORED_GLOBS: Dict[str, Tuple[List[str], str]] = {
    "decision": (
        ["docs/decisions/*.md", "coordinator/docs/decisions/*.md"],
        "schema applies_to plus authored: coordinator/docs/decisions holds DR-<AREA>-NNN records",
    ),
    "lesson-entry": (
        ["state/lessons/*.yaml", "archive/lessons/*/*.yaml"],
        "schema applies_to plus authored: closed lessons live under archive/lessons/<bucket>/",
    ),
    "bug-backlog": (
        ["state/bug-backlog/*.yaml", "archive/bug-backlog/*/*.yaml"],
        "schema applies_to plus authored: closed entries live under archive/bug-backlog/<bucket>/",
    ),
    "debt-backlog": (
        ["state/debt-backlog/*.yaml", "archive/debt-backlog/*/*.yaml"],
        "schema applies_to plus authored: closed entries live under archive/debt-backlog/<bucket>/",
    ),
    "improvement-queue": (
        ["state/improvement-queue/*.yaml", "archive/improvement-queue/*/*.yaml"],
        "schema applies_to plus authored: closed entries live under archive/improvement-queue/<bucket>/",
    ),
    "research-synthesis": (
        ["docs/research/**/README.md", "docs/research/**/synthesis.md"],
        "authored: schema applies_to also types every supporting file in a run dir",
    ),
    "chatty-roster": (["**/roster.json"], "authored: schema applies_to is the placeholder <run-dir>/roster.json"),
    "percolate-store": (["**/percolate-store.yaml"], "authored: consumer-owned file, schema applies_to is null"),
    "plan-merge-delta": (["**/*.plan-merge-delta.yaml", "**/*.plan-merge-delta.json"], "authored: caller-chosen path, schema applies_to is null"),
    "plan-tasks": (["docs/plans/**/*.md"], "authored: a fenced block inside a plan, schema applies_to is null"),
}
_EMBEDDED_IN = {"plan-tasks": "plan"}

_DELIVERABLE_TYPES = (
    "handoff", "completion-entry", "completion-receipt", "roadmap",
    "sizing-object", "problem-set", "initiative", "goal",
)
_INITIATIVE_TYPES = (
    "plan", "handoff", "roadmap", "goal", "completion-entry",
    "bug-backlog", "debt-backlog", "improvement-queue",
)
_DECISION_CITERS = ("plan", "handoff", "handoff-archived", "completion-entry", "wiki-page", "tripwire")
_WIKI = "coordinator/docs/wiki"
_TRIPWIRES = f"{_WIKI}/coordinator-tripwires"

# trap: no schema backs these; a field a record lacks is derived by the consumer per `derive`.
_AUTHORED_TYPES: Dict[str, dict] = {
    "wiki-page": {
        "path_globs": [f"{_WIKI}/**/*.md", "docs/wiki/**/*.md"],
        "exclude_globs": [f"{_TRIPWIRES}/**", "**/_index.md"],
        "path_globs_source": "authored: no schema; ten of the pages carry frontmatter",
        "identity": {"from": "path"},
        "fields": {
            "title": {"type": "string", "derive": {"from": "first_h1"}},
            "created": {"type": "date"},
            "status": {"type": "string"},
            "summary": {"type": "string", "derive": {"from": "first_sentence_after_h1"}},
        },
    },
    "tripwire": {
        "path_globs": [f"{_TRIPWIRES}/*.md", f"{_TRIPWIRES}/tripwire-registry/*.md"],
        "exclude_globs": ["**/README.md", "**/_index.md"],
        "path_globs_source": "authored: no schema; no frontmatter",
        "identity": {"from": "field", "field": "token"},
        "fields": {
            "token": {"type": "string", "required": True, "derive": {"from": "h2"}},
            "tell": {"type": "string", "derive": {"from": "labelled_line", "label": "**Tell:**"}},
            "rule": {"type": "string", "derive": {"from": "labelled_line", "label": "**Rule:**"}},
            "source": {"type": "string", "derive": {"from": "labelled_line", "label": "Source:"}},
            "kind": {
                "type": "string",
                "enum": ["behavioural", "mechanism"],
                "derive": {
                    "from": "path_prefix",
                    "default": "behavioural",
                    "prefixes": {f"{_TRIPWIRES}/tripwire-registry/": "mechanism"},
                },
            },
        },
    },
    "audit": {
        "path_globs": ["state/audits/*.md", "state/audits/*/README.md"],
        "exclude_globs": [],
        "path_globs_source": "authored: no schema; searched by doc_type filter",
        "identity": {"from": "path"},
        "fields": {"title": {"type": "string"}},
    },
}
_AUTHORED_FIELDS: Dict[str, Dict[str, dict]] = {
    "decision": {"supersedes": {"type": "string"}},
}
_PLAN_PATH_REFS = (
    ("review-sidecar", "plan"), ("run-report", "plan"), ("plan-coverage-check", "plan"),
    ("prior-art-check", "plan"), ("seam-check", "plan"), ("sizing-object", "plan"),
    ("handoff", "governing_plan"), ("handoff-archived", "governing_plan"),
    ("completion-receipt", "plan_path"), ("archived-memo", "related_plan"),
    ("plan", "superseded_by"),
)
_DECISION_PATTERN = r"\bDR-[A-Za-z0-9][A-Za-z0-9-]*\b"
_LINK_PATTERN = r"\]\(([^)#\s]+\.md)(?:#[^)\s]*)?\)"
_BODY_PATTERNS = {"body_citation": _DECISION_PATTERN, "body_link": _LINK_PATTERN}
_SKIP_NONE = {"skip_values": ["none"]}

_EDGES: List[tuple] = (
    [("plan", "deliverable_id", t, "deliverable_id", "equals") for t in _DELIVERABLE_TYPES]
    + [("handoff", "deliverable_ids", "plan", "deliverable_id", "in_list")]
    + [(t, "initiative", "initiative", "id", "equals") for t in _INITIATIVE_TYPES]
    + [(t, f, "plan", None, "path") for t, f in _PLAN_PATH_REFS]
    + [(t, "$body", "decision", "id", "body_citation") for t in _DECISION_CITERS]
    + [
        ("decision", "superseded_by", "decision", "id", "equals", _SKIP_NONE),
        ("decision", "supersedes", "decision", "id", "equals", _SKIP_NONE),
        ("plan", "depends_on_plan", "plan", None, "path", {"item_field": "plan"}),
        ("wiki-page", "$body", "wiki-page", None, "body_link"),
        ("wiki-page", "$body", "tripwire", None, "body_link"),
        ("tripwire", "$body", "tripwire", None, "body_link"),
        ("tripwire", "$body", "wiki-page", None, "body_link"),
        ("plan", "sizing_object", "sizing-object", None, "path"),
        ("handoff", "sizing_object", "sizing-object", None, "path"),
        ("handoff", "sizing_objects", "sizing-object", None, "path"),
        ("sizing-object", "baton", "handoff", None, "path"),
        ("sizing-object", "goal_id", "goal", "id", "equals"),
        ("plan", "predecessor_handoff", "handoff", None, "path"),
        ("plan", "claimed_by_handoff", "handoff", None, "path"),
        ("plan", "source_memo", "cross-repo-memo", None, "path"),
        ("handoff", "predecessor_id", "handoff", "handoff_id", "equals"),
        ("handoff", "origin_handoff_id", "handoff", "handoff_id", "equals"),
        ("handoff", "origin_plan_id", "plan", "plan_id", "equals"),
        ("handoff", "plan_ids", "plan", "plan_id", "in_list"),
        ("handoff", "origin_goal_id", "goal", "id", "in_list"),
        ("handoff", "roadmap_id", "roadmap", "roadmap_id", "equals"),
        ("spine", "roadmap_id", "roadmap", "roadmap_id", "equals"),
        ("goal", "parent_goal_id", "goal", "id", "equals"),
        ("completion-entry", "chain_starting_handoff", "handoff-archived", None, "path"),
        ("completion-receipt", "baton_id", "handoff", "handoff_id", "equals"),
        ("completion-receipt", "workstream_id", "workstream", "workstream_id", "equals"),
        ("cross-repo-memo", "in_reply_to", "cross-repo-memo", None, "path"),
        ("cross-repo-memo", "supersedes", "cross-repo-memo", None, "path"),
        ("archived-memo", "in_reply_to", "cross-repo-memo", None, "path"),
    ]
)

_DATE_PATTERN = re.compile(r"^\^\\d\{4\}-\\d\{2\}-\\d\{2\}\$$")


def _inline(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _dump(obj: object, depth: int) -> str:
    """Pretty-print down to one line per field: dicts at depth >= 4 and scalar lists stay inline."""
    scalar_list = isinstance(obj, list) and not any(isinstance(v, (dict, list)) for v in obj)
    if not isinstance(obj, (dict, list)) or not obj or depth >= 4 or scalar_list:
        return _inline(obj)
    pad, inner = "  " * depth, "  " * (depth + 1)
    if isinstance(obj, list):
        rows = [f"{inner}{_dump(v, depth + 1)}" for v in obj]
        return "[\n" + ",\n".join(rows) + f"\n{pad}]"
    rows = [f"{inner}{_inline(k)}: {_dump(obj[k], depth + 1)}" for k in sorted(obj)]
    return "{\n" + ",\n".join(rows) + f"\n{pad}}}"


def _json(obj: object) -> str:
    return _dump(obj, 0) + "\n"


def _schema_files() -> List[Path]:
    return sorted(
        [*_SCHEMA_DIR.glob("*.schema.json"), *_MEMO_SCHEMAS],
        key=lambda f: _load(f)["x-schema-name"],
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8"))


def _scalar_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "null"


def _classify(schema: object, root: dict) -> Tuple[set, list]:
    """Return (type names, enum values) for a property schema; `null` stays in the set."""
    if not isinstance(schema, dict):
        return {"object"}, []
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return _classify(root.get("$defs", {}).get(ref[len("#/$defs/"):]), root)
    types: set = set()
    enum: list = list(schema.get("enum", []))
    if "const" in schema:
        types.add(_scalar_type(schema["const"]))
    if enum:
        types.update(_scalar_type(v) for v in enum)
    declared = schema.get("type")
    for t in ([declared] if isinstance(declared, str) else declared or []):
        types.add(t)
    if "string" in types:
        fmt = schema.get("format")
        pattern = schema.get("pattern", "")
        if fmt == "date" or _DATE_PATTERN.match(pattern):
            types.discard("string")
            types.add("date")
        elif fmt == "date-time":
            types.discard("string")
            types.add("datetime")
    for key in ("anyOf", "oneOf", "allOf"):
        for member in schema.get(key, []) or []:
            sub_types, sub_enum = _classify(member, root)
            types |= sub_types
            enum += [v for v in sub_enum if v not in enum]
    if not types:
        types.add("object" if "properties" in schema else "string")
    return types, enum


def _collapse(types: set) -> Tuple[str, List[str] | None, bool]:
    nullable = "null" in types
    rest = sorted(types - {"null"}) or ["string"]
    if len(rest) == 1:
        return rest[0], None, nullable
    return "union", rest, nullable


def _field(prop: object, root: dict, required: bool) -> dict:
    types, enum = _classify(prop, root)
    kind, variants, nullable = _collapse(types)
    out: dict = {"type": kind}
    if required:
        out["required"] = True
    if variants:
        out["variants"] = variants
    if nullable:
        out["nullable"] = True
    if enum:
        out["enum"] = enum
    if "array" in types and isinstance(prop, dict):
        item_types = set()
        for branch in [prop, *(prop.get("anyOf", []) or [])]:
            if isinstance(branch, dict) and "items" in branch:
                item_types |= _classify(branch["items"], root)[0]
        if item_types:
            out["items"] = _collapse(item_types)[0]
    return out


def _globs(name: str, schema: dict) -> Tuple[List[str], str]:
    if name in _AUTHORED_GLOBS:
        return _AUTHORED_GLOBS[name]
    applies = schema.get("applies_to")
    if not isinstance(applies, str) or "<" in applies:
        raise ValueError(
            f"doc_type {name!r} has no usable applies_to ({applies!r}); add it to _AUTHORED_GLOBS"
        )
    return [applies], "schema applies_to"


def _doc_type(name: str, path: Path, schema: dict) -> dict:
    required = set(schema.get("required", []))
    props = schema.get("properties", {})
    globs, basis = _globs(name, schema)
    fields = {k: _field(v, schema, k in required) for k, v in props.items()}
    authored = _AUTHORED_FIELDS.get(name, {})
    entry: dict = {
        "schema": path.relative_to(_REPO).as_posix(),
        "schema_version": schema.get("x-schema-version"),
        "path_globs": globs,
        "path_globs_source": basis,
        "fields": {**fields, **{k: v for k, v in authored.items() if k not in fields}},
        "field_order": [*props, *(k for k in authored if k not in fields)],
        "joins": [],
    }
    if name in _EMBEDDED_IN:
        entry["embedded_in"] = _EMBEDDED_IN[name]
    if "kinds" in schema:
        entry["kinds"] = schema["kinds"]
    if schema.get("match_mode"):
        entry["match_mode"] = schema["match_mode"]
    return entry


def _authored_entry(name: str, spec: dict) -> dict:
    """Build a schema-less doc_type entry from an authored spec."""
    fields = spec["fields"]
    for fname, f in fields.items():
        if f.get("type") not in TYPE_VOCABULARY:
            raise ValueError(f"doc_type {name!r} field {fname!r}: type not in the type vocabulary")
    return {
        "schema": None,
        "schema_version": None,
        "path_globs": list(spec["path_globs"]),
        "exclude_globs": list(spec.get("exclude_globs", [])),
        "path_globs_source": spec["path_globs_source"],
        "identity": spec["identity"],
        "fields": fields,
        "field_order": list(fields),
        "joins": [],
    }


def _merge_extension(doc_types: Dict[str, dict], ext: dict) -> List[tuple]:
    """Apply a per-repo extension to doc_types in place; return its join edges."""
    for name, globs in ext.get("extend_globs", {}).items():
        if name not in doc_types:
            raise ValueError(f"extension extends unknown doc_type {name!r}")
        doc_types[name]["path_globs"] += [g for g in globs if g not in doc_types[name]["path_globs"]]
    for name, spec in ext.get("doc_types", {}).items():
        if name in doc_types:
            raise ValueError(f"extension redeclares doc_type {name!r}")
        doc_types[name] = _authored_entry(name, spec)
    return [tuple(e) for e in ext.get("joins", [])]


def _attach_joins(doc_types: Dict[str, dict], edges: Iterable[tuple]) -> None:
    for src, from_field, dst, to_field, match, *extra in edges:
        if src not in doc_types or dst not in doc_types:
            raise ValueError(f"join {src}.{from_field} -> {dst}: unknown doc_type")
        if from_field != "$body" and from_field not in doc_types[src]["fields"]:
            raise ValueError(f"join {src}.{from_field} -> {dst}: no such field on {src}")
        if to_field is not None and to_field not in doc_types[dst]["fields"]:
            raise ValueError(f"join {src}.{from_field} -> {dst}.{to_field}: no such field on {dst}")
        join: dict = {"from_field": from_field, "to_doc_type": dst, "match": match}
        if to_field is not None:
            join["to_field"] = to_field
        if match in _BODY_PATTERNS:
            join["pattern"] = _BODY_PATTERNS[match]
        for options in extra:
            unknown = set(options) - set(JOIN_OPTION_VOCABULARY)
            if unknown:
                raise ValueError(f"join {src}.{from_field} -> {dst}: unknown option {sorted(unknown)}")
            join.update(options)
        doc_types[src]["joins"].append(join)
    for entry in doc_types.values():
        entry["joins"].sort(key=lambda j: (j["from_field"], j["to_doc_type"], j.get("to_field", "")))


def render_all(extension: dict | None = None) -> Dict[str, str]:
    """Return {output key: file text}; `extension` is a per-repo layer merged over the shared registry."""
    doc_types: Dict[str, dict] = {}
    for path in _schema_files():
        schema = _load(path)
        name = schema["x-schema-name"]
        if name in doc_types:
            raise ValueError(f"duplicate doc_type {name!r} at {path}")
        doc_types[name] = _doc_type(name, path, schema)
    for name, spec in _AUTHORED_TYPES.items():
        if name in doc_types:
            raise ValueError(f"duplicate doc_type {name!r}")
        doc_types[name] = _authored_entry(name, spec)
    ext_edges = _merge_extension(doc_types, extension) if extension else []
    _attach_joins(doc_types, [*_EDGES, *ext_edges])
    registry = {
        "generated_by": _GENERATOR,
        "registry_version": REGISTRY_VERSION,
        "glob_root": GLOB_ROOT,
        "field_defaults": {"required": False, "nullable": False},
        "type_vocabulary": TYPE_VOCABULARY,
        "match_vocabulary": MATCH_VOCABULARY,
        "join_option_vocabulary": JOIN_OPTION_VOCABULARY,
        "doc_types": doc_types,
    }
    return {"record-registry.json": _json(registry)}


def resolve_registry(repo_root: Path | str) -> dict:
    """Return the registry for a repo root: the shared registry with the root's EXTENSION_HOME file merged over it. Uncached: callers cache per root."""
    ext_path = Path(repo_root) / EXTENSION_HOME
    extension = _load(ext_path) if ext_path.is_file() else None
    return json.loads(render_all(extension)["record-registry.json"])


def _disk(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8")


def drifted(rendered: Dict[str, str], outputs: Dict[str, Path] | None = None) -> List[str]:
    outputs = OUTPUTS if outputs is None else outputs
    return [k for k, text in rendered.items() if _disk(outputs[k]) != text]


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="emit_record_registry")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite the registry (default)")
    mode.add_argument("--check", action="store_true", help="exit 1 when the committed file differs")
    ap.add_argument("--extension", type=Path, help="per-repo extension JSON merged over the shared registry")
    ap.add_argument("--output", type=Path, help="registry path to write or check; required with --extension")
    args = ap.parse_args(argv)
    if args.extension and not args.output:
        ap.error("--extension requires --output so the shared registry is not overwritten")
    rendered = render_all(_load(args.extension) if args.extension else None)
    outputs = {**OUTPUTS, "record-registry.json": args.output} if args.output else OUTPUTS
    if args.check:
        bad = drifted(rendered, outputs)
        for key in bad:
            print(f"drift: {outputs[key]}", file=sys.stderr)
        return 1 if bad else 0
    for key, text in rendered.items():
        outputs[key].parent.mkdir(parents=True, exist_ok=True)
        with open(outputs[key], "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
