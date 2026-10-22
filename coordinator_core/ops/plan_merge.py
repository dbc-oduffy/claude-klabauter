"""plan.merge: merge N plans plus an EM-authored delta, or refuse with no file written (MUTATING).

Wire params: `sources` (2 or more repo-relative plan paths), `delta` (repo-relative JSON or YAML
file valid against plan-merge-delta.schema.json), `out` (repo-relative), `force` (default false),
`schema_dir` (optional repo-relative dir holding plan.schema.json and plan-tasks.schema.json).
Result `{"ok": true, "out", "report", "summary"}` or `{"ok": false, "refusal"}`. Bad params raise
ValueError.

The whole output is built in memory and the six checks run on it; one atomic write follows a pass,
so a refusal never creates a file. Untouched rows are carried as original text, never re-serialised.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._plan_merge_contract import (
    CHECK_KEYS,
    MergeReport,
    Refusal,
    RowAccount,
    Section,
    SourceRow,
    normalize_newlines,
)

GENERATES: list = []

_FENCE_OPEN = "```yaml plan-tasks\n"
_FENCE_CLOSE = "\n```"
_PREAMBLE = "(preamble)"
_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_HEADING_RE = re.compile(r"^##(?!#)\s+\S")
_ROW_START_RE = re.compile(r"(?m)^- id:")
_ROW_ID_RE = re.compile(r"(?m)^- id:[ \t]*(\S+)")
_BLOCK_START_RE = re.compile(r"^(?:- |  )([A-Za-z_][\w-]*):")
_PLAN_SCHEMA = "plan.schema.json"
_TASKS_SCHEMA = "plan-tasks.schema.json"
_DELTA_SCHEMA = "plan-merge-delta.schema.json"


def _rel_path(name: str, value: object, root: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"plan.merge: {name} must be a non-empty repo-relative path")
    p = Path(value)
    if p.is_absolute() or p.drive or p.anchor:
        raise ValueError(f"plan.merge: {name} must be repo-relative: {value!r}")
    resolved = (root / p).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"plan.merge: {name} escapes the repo root: {value!r}") from None
    return resolved


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _refuse(refusal: Refusal) -> dict:
    return {"ok": False, "refusal": refusal.to_dict()}


class _Stop(Exception):
    """Carries a Refusal out of a deep helper."""

    def __init__(self, refusal: Refusal):
        super().__init__(refusal.message)
        self.refusal = refusal


def _read(path: Path, label: str) -> str:
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return normalize_newlines(fh.read())
    except OSError as exc:
        raise ValueError(f"plan.merge: cannot read {label}: {path.name}: {exc}") from exc


def _heading_text(line: str) -> str:
    return re.sub(r"^#{1,6}\s*", "", line.strip()).strip()


def _row_id(text: str) -> str:
    m = _ROW_ID_RE.search(text)
    return m.group(1).strip("'\"") if m else ""


def _split_rows(source: str, fence_body: str) -> list:
    """Split a spine fence body into rows at column-0 `- id:`; text before the first row rides with it."""
    starts = [m.start() for m in _ROW_START_RE.finditer(fence_body)]
    if not starts:
        raise _Stop(Refusal("SPINE_MALFORMED", f"plan.merge: {source}: spine holds no `- id:` rows", (source,)))
    bounds = [0] + starts[1:] + [len(fence_body)]
    return [
        SourceRow(source, _row_id(fence_body[a:b]), fence_body[a:b].rstrip("\n"))
        for a, b in zip(bounds, bounds[1:])
    ]


def _split_sections(source: str, body: str, fence: tuple) -> list:
    """Return [(Section, consumed, spine_prose)] for the preamble and every `## ` section of `body`.

    The section holding the spine fence has the fence removed; if only its heading remains it is
    consumed, otherwise its prose is a carried section flagged `spine_prose`.
    """
    heads = []
    in_fence = False
    pos = 0
    for line in body.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and _HEADING_RE.match(line):
            heads.append(pos)
        pos += len(line)
    spans = [(_PREAMBLE, 0, heads[0] if heads else len(body))]
    for i, a in enumerate(heads):
        eol = body.find("\n", a)
        spans.append((_heading_text(body[a:len(body) if eol < 0 else eol]), a, heads[i + 1] if i + 1 < len(heads) else len(body)))
    out = []
    for heading, a, b in spans:
        text = body[a:b]
        if a <= fence[0] < b:
            tail = text[fence[1] - a:]
            text = text[:fence[0] - a] + (tail[1:] if tail.startswith("\n") else tail)
            prose = text.split("\n", 1)[1] if "\n" in text else ""
            out.append((Section(source, heading, text), not prose.strip(), True))
        else:
            out.append((Section(source, heading, text), False, False))
    return out


def _load_delta(path: Path) -> object:
    text = _read(path, "delta")
    try:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        import yaml

        return yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a SCHEMA_INVALID refusal
        raise _Stop(Refusal("SCHEMA_INVALID", f"plan.merge: delta does not parse: {exc}")) from exc


def _norm_src(p: str) -> str:
    return Path(p).as_posix()


def _source_configs(delta: object, sources: list) -> dict:
    cfg = {}
    if isinstance(delta, dict):
        for ent in delta.get("sources") or []:
            if isinstance(ent, dict) and isinstance(ent.get("path"), str):
                cfg[_norm_src(ent["path"])] = ent
    return {s: cfg.get(_norm_src(s), {}) for s in sources}


def _key_blocks(text: str) -> dict:
    """Split a row into its top-level key blocks; text before `- id:` is the `#lead` block."""
    blocks: dict = {}
    key = "#lead"
    buf: list = []
    for ln in text.split("\n"):
        m = _BLOCK_START_RE.match(ln)
        if m and (ln.startswith("  ") or key == "#lead"):
            blocks[key] = "\n".join(buf).rstrip("\n")
            key, buf = m.group(1), [ln]
        else:
            buf.append(ln)
    blocks[key] = "\n".join(buf).rstrip("\n")
    return blocks


def _diff_keys(a: str, b: str) -> set:
    ka, kb = _key_blocks(a), _key_blocks(b)
    return {k for k in set(ka) | set(kb) if ka.get(k) != kb.get(k)}


def _yaml_scalar(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _frontmatter_text(fields: dict, lists: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        lines.append(f"{k}: {_yaml_scalar(v)}")
    for k, items in lists.items():
        lines.append(f"{k}:" if items else f"{k}: []")
        lines.extend(f"  - {_yaml_scalar(i)}" for i in items)
    lines.append("---")
    return "\n".join(lines) + "\n"


def _scalar_list(value: object) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def _merged_frontmatter(delta: dict, src_fms: list, sources: list, plan_schema: dict) -> str:
    """Scalars come from the delta; schema-required scalars the delta omits come from the sources
    when they agree; provenance goes on new list keys."""
    fields = dict(delta["frontmatter"])
    for k in fields:
        if not _KEY_RE.match(k):
            raise _Stop(Refusal("SCHEMA_INVALID", f"plan.merge: frontmatter key not a bare YAML key: {k!r}"))
    for key in plan_schema.get("required", []):
        if key in fields:
            continue
        seen: list = []
        for src, fm in zip(sources, src_fms):
            v = fm.get(key)
            if v is not None and all(v != s[1] for s in seen):
                seen.append((src, v))
        if len(seen) > 1:
            raise _Stop(Refusal(
                "SCHEMA_INVALID",
                "plan.merge: sources differ on required scalar "
                f"{key!r} and the delta supplies none: "
                + "; ".join(f"{s}={v!r}" for s, v in seen),
                tuple(sources),
            ))
        if seen:
            fields[key] = seen[0][1]
    lists = {"source_plans": [_norm_src(s) for s in sources]}
    ids = [i for fm in src_fms for i in _scalar_list(fm.get("plan_id"))]
    hand = [h for fm in src_fms for h in _scalar_list(fm.get("predecessor_handoff"))]
    if ids:
        lists["source_plan_ids"] = ids
    if hand:
        lists["source_predecessor_handoffs"] = hand
    lists = {k: v for k, v in lists.items() if k not in fields}
    return _frontmatter_text(fields, lists)


def _match_source(spec: str, source: str, label: Optional[str]) -> bool:
    p = Path(source)
    return spec in (_norm_src(source), label, p.name, p.stem)


def _rendered_heading(section: Section, label: Optional[str], spine_prose: bool) -> str:
    first, _, rest = section.text.partition("\n")
    name = _heading_text(first)
    if spine_prose:
        name += " (prose)"
    prefix = f"[{label}] " if label else ""
    return f"## {prefix}{name}" + ("\n" + rest if "\n" in section.text else "")


def _schema_errors_text(errors: list) -> str:
    from coordinator_core.frontmatter.schema_validate import format_validation_errors

    return format_validation_errors(errors)


def _validate_delta(delta: object, schema_path: Path) -> None:
    from coordinator_core.frontmatter.schema_validate import validate_frontmatter

    if not isinstance(delta, dict):
        raise _Stop(Refusal("SCHEMA_INVALID", "plan.merge: delta is not a mapping"))
    try:
        errors = validate_frontmatter(delta, schema_path)
    except Exception as exc:  # noqa: BLE001
        raise _Stop(Refusal("SCHEMA_INVALID", f"plan.merge: delta schema check failed: {exc}")) from exc
    if errors:
        raise _Stop(Refusal("SCHEMA_INVALID", "plan.merge: delta invalid: " + _schema_errors_text(errors)))


def _first_failure(checks: dict, details: dict) -> Optional[Refusal]:
    order = (
        ("single_fence", "SPINE_MALFORMED"),
        ("unique_ids", "ID_COLLISION"),
        ("row_partition", "FIDELITY_BREACH"),
        ("schema_valid", "SCHEMA_INVALID"),
        ("sections_accounted", "SECTION_UNACCOUNTED"),
        ("edges_resolve", "DANGLING_EDGE"),
    )
    failed = [(k, c) for k, c in order if not checks[k]]
    if not failed:
        return None
    key, code = failed[0]
    msg = "; ".join(f"{k}: {details[k]}" for k, _ in failed if details.get(k))
    return Refusal(
        code, f"plan.merge: check failed: {msg or key}", tuple(details.get("sources", ())),
        tuple(details.get(key + "_ids", ())),
    )


def _classify(accounts: list, base_by_key: dict, out_texts: list, rekeyed_sources: set) -> list:
    """Upgrade an `untouched` account to `amended`/`rekey` when only id/depends_on moved in a source
    that carries a rekey mapping. Everything else is left for the partition check."""
    fixed = []
    for acct, text in zip(accounts, out_texts):
        base = base_by_key.get((acct.source, acct.row_id))
        if (
            acct.status == "untouched" and base is not None and acct.source in rekeyed_sources
            and text.rstrip("\n") != base.text.rstrip("\n")
            and _diff_keys(base.text, text) <= {"id", "depends_on"}
        ):
            acct = RowAccount(acct.row_id, acct.source, "amended", tuple(acct.edit_classes) + ("rekey",))
        fixed.append(acct)
    return fixed


def _row_fidelity(acct: RowAccount, base: SourceRow, out_text: str, entry: dict) -> Optional[str]:
    """Why `out_text` is not a faithful rendering of `base` under `acct`, or None."""
    out_text = out_text.rstrip("\n")
    if acct.status == "untouched":
        return None if out_text == base.text.rstrip("\n") else f"untouched row {acct.row_id} differs from its source"
    allowed = set(entry.get("fields") or {}) | set(entry.get("drop_fields") or ())
    if entry.get("deferred") is True:
        allowed.add("disposition")
    if entry.get("note"):
        allowed.add("consolidation")
    if "rekey" in acct.edit_classes:
        allowed |= {"id", "depends_on"}
    sb, ob = _key_blocks(base.text), _key_blocks(out_text)
    for key in set(sb) | set(ob):
        if key in allowed:
            continue
        s, o = sb.get(key), ob.get(key)
        if key == "body" and s is not None and o is not None:
            if not o.startswith(s):
                return f"row {acct.row_id}: source body is not a prefix of the output body"
        elif s != o:
            return f"row {acct.row_id}: key {key!r} changed without a delta entry naming it"
    return None


def _verify(out_text, accounts, bases, base_by_key, delta_rows, sections_info, sources, schemas) -> tuple:
    """Run the six checks on the in-memory output. Returns (checks, details)."""
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.frontmatter.schema_validate import (
        is_governed_plan, parse_frontmatter, plan_tasks_row_errors, validate_frontmatter,
    )

    checks = {k: False for k in CHECK_KEYS}
    details: dict = {"sources": sources}
    parsed = parse_frontmatter(out_text)
    fm, body = parsed.get("frontmatter") or {}, parsed.get("body", out_text)

    located = locate_fenced_block(body)
    checks["single_fence"] = located.status is LocateStatus.LOCATED
    if not checks["single_fence"]:
        details["single_fence"] = f"spine locate status {located.status.value}"
        return checks, details
    out_rows = _split_rows("<output>", located.body)
    out_texts = [r.text for r in out_rows]
    out_ids = [r.row_id for r in out_rows]

    dups = sorted({i for i in out_ids if out_ids.count(i) > 1})
    checks["unique_ids"] = not dups
    if dups:
        details["unique_ids"] = "duplicate row ids " + ", ".join(dups)
        details["unique_ids_ids"] = dups

    problems = []
    if out_ids != [a.row_id for a in accounts]:
        problems.append("output row ids do not match the row accounts")
    else:
        seen = set()
        for acct, text in zip(accounts, out_texts):
            if acct.status == "new":
                continue
            base = base_by_key.get((acct.source, acct.row_id))
            if base is None:
                problems.append(f"row {acct.row_id}: account names no source row")
                continue
            seen.add(id(base))
            msg = _row_fidelity(acct, base, text, delta_rows.get(acct.row_id, {}))
            if msg:
                problems.append(msg)
        if len(seen) != len(bases):
            problems.append(f"{len(bases) - len(seen)} source row(s) unaccounted")
    checks["row_partition"] = not problems
    if problems:
        details["row_partition"] = "; ".join(problems[:3])
        details["row_partition_ids"] = [a.row_id for a in accounts]

    schema_problems = []
    try:
        fm_errors = validate_frontmatter(fm, schemas["plan_path"])
    except Exception as exc:  # noqa: BLE001
        fm_errors = [{"field": "frontmatter", "error": str(exc), "hint": ""}]
    if fm_errors:
        schema_problems.append("frontmatter: " + _schema_errors_text(fm_errors))
    try:
        rows = yaml.safe_load(located.body) or []
    except yaml.YAMLError as exc:
        rows = []
        schema_problems.append(f"spine does not parse: {exc}")
    if len(rows) != len(out_rows):
        schema_problems.append("spine yaml row count differs from its `- id:` rows")
    governed = is_governed_plan(fm)
    tasks_schema = schemas["tasks_governed"] if governed else schemas["tasks"]
    created = fm.get("created")
    for row in rows:
        if not isinstance(row, dict):
            schema_problems.append("a spine entry is not a mapping")
            continue
        errs = plan_tasks_row_errors(
            row, tasks_schema, governed=governed, plan_created=None if created is None else str(created),
        )
        if errs:
            schema_problems.append(f"row {row.get('id')!r}: " + _schema_errors_text(errs))
    checks["schema_valid"] = not schema_problems
    if schema_problems:
        details["schema_valid"] = "; ".join(schema_problems[:3])

    sec_problems = list(sections_info["problems"])
    if any(text not in out_text for text in sections_info["carried_bodies"]):
        sec_problems.append("a carried section is missing from the output")
    checks["sections_accounted"] = not sec_problems
    if sec_problems:
        details["sections_accounted"] = "; ".join(sec_problems[:3])

    id_set = set(out_ids)
    dangling = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for dep in row.get("depends_on") or []:
            target = dep.get("chunk") if isinstance(dep, dict) else dep
            if isinstance(target, str) and target not in id_set:
                dangling.append(f"{row.get('id')} -> {target}")
    checks["edges_resolve"] = not dangling
    if dangling:
        details["edges_resolve"] = "unresolved depends_on: " + ", ".join(dangling[:5])
        details["edges_resolve_ids"] = dangling
    return checks, details


def _summary(report: MergeReport) -> str:
    c = report.counts()
    head = (
        f"{c['source_rows']} source rows: {c['untouched']} untouched, {c['amended']} amended, "
        f"{c['unaccounted']} unaccounted; {c['new']} new; {c['out']} out; "
        f"{c['dropped_sections']} sections declared dropped"
    )
    return "\n".join([head] + [f"{k}: {'pass' if report.checks[k] else 'fail'}" for k in CHECK_KEYS])


def _load_sources(root: Path, source_names: list, cfg: dict) -> tuple:
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    bases: list = []
    src_fms: list = []
    src_sections: list = []
    for name in source_names:
        text = _read(_rel_path("sources", name, root), f"source {name}")
        parsed = parse_frontmatter(text)
        body = parsed.get("body", text)
        heading = cfg[name].get("spine_heading") or "Tasks"
        loc = locate_fenced_block(body, heading=heading)
        if loc.status is not LocateStatus.LOCATED:
            raise _Stop(Refusal(
                "SPINE_MALFORMED",
                f"plan.merge: {name}: spine under '## {heading}' is {loc.status.value}",
                (name,),
            ))
        rows = _split_rows(name, loc.body)
        try:
            parsed_rows = yaml.safe_load(loc.body) or []
        except yaml.YAMLError as exc:
            raise _Stop(Refusal("SPINE_MALFORMED", f"plan.merge: {name}: spine does not parse: {exc}", (name,))) from exc
        if not isinstance(parsed_rows, list) or len(parsed_rows) != len(rows):
            raise _Stop(Refusal("SPINE_MALFORMED", f"plan.merge: {name}: spine entries are not all `- id:` rows", (name,)))
        bases.extend(rows)
        src_fms.append(parsed.get("frontmatter") or {})
        fence = (loc.span[0] - len(_FENCE_OPEN), loc.span[1] + len(_FENCE_CLOSE))
        src_sections.append(_split_sections(name, body, fence))
    return bases, src_fms, src_sections


def _load_schemas(schema_dir: Optional[Path]) -> tuple:
    from coordinator_core.frontmatter import schema_validate as sv

    default_dir = Path(sv.__file__).parent / "schemas"
    sdir = schema_dir or default_dir

    def load(name: str) -> dict:
        try:
            with open(sdir / name, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as exc:
            raise ValueError(f"plan.merge: cannot read schema {name} in {sdir.name}: {exc}") from exc

    plan_schema = load(_PLAN_SCHEMA)
    if schema_dir:
        tasks_schema = load(_TASKS_SCHEMA)
        tasks_governed = sv._plan_tasks_schema_without_pm_approved_required(tasks_schema)
    else:
        tasks_schema, tasks_governed = sv._PLAN_TASKS_SCHEMA_DICT, sv._PLAN_TASKS_SCHEMA_GOVERNED_DICT
    schemas = {"plan_path": sdir / _PLAN_SCHEMA, "tasks": tasks_schema, "tasks_governed": tasks_governed}
    return default_dir / _DELTA_SCHEMA, plan_schema, schemas


def _account_prose(source_names: list, src_sections: list, cfg: dict, drops: list) -> tuple:
    """Partition every source section into carried, consumed or declared dropped."""
    used = [False] * len(drops)
    dropped: list = []
    problems: list = []
    blocks: list = []
    bodies: list = []
    for name, secs in zip(source_names, src_sections):
        label = cfg[name].get("origin_label")
        for sec, consumed, spine_prose in secs:
            declared = [
                i for i, d in enumerate(drops)
                if _heading_text(d["heading"]) == sec.heading and _match_source(d["source"], name, label)
            ]
            for i in declared:
                used[i] = True
            if declared:
                dropped.append((sec, drops[declared[0]]["reason"]))
            elif consumed:
                continue
            elif sec.heading == _PREAMBLE:
                if sec.text.strip():
                    problems.append(f"{name}: (preamble) is neither carried nor declared dropped")
            else:
                blocks.append(_rendered_heading(sec, label, spine_prose))
                bodies.append(sec.text.split("\n", 1)[1] if "\n" in sec.text else "")
    for i, d in enumerate(drops):
        if not used[i]:
            problems.append(f"declared drop matches no section: {d['source']}: {d['heading']}")
    return dropped, problems, blocks, bodies


def _merge(root: Path, params: dict, source_names: list, out: Path, delta_path: Path, schema_dir: Optional[Path]) -> dict:
    from coordinator_core.atomic_replace import atomic_write_bytes

    delta = _load_delta(delta_path)
    cfg = _source_configs(delta, source_names)
    bases, src_fms, src_sections = _load_sources(root, source_names, cfg)

    delta_schema_path, plan_schema, schemas = _load_schemas(schema_dir)
    _validate_delta(delta, delta_schema_path)

    from coordinator_core.ops._plan_merge_delta import apply_delta, render_delta_tables
    from coordinator_core.ops._plan_merge_rekey import apply_rekey, detect_collisions

    rekey: dict = {}
    for ent in delta.get("rekey") or []:
        match = [s for s in source_names if _match_source(ent["source"], s, cfg[s].get("origin_label"))]
        if len(match) != 1:
            raise _Stop(Refusal("REKEY_DANGLING", f"plan.merge: rekey source {ent['source']!r} matches no single source"))
        rekey.setdefault(match[0], {}).update(ent["ids"])

    rows = list(bases)
    if rekey:
        rows = apply_rekey(rows, rekey)
        if isinstance(rows, Refusal):
            raise _Stop(rows)
        if len(rows) != len(bases):
            raise _Stop(Refusal("FIDELITY_BREACH", "plan.merge: apply_rekey changed the row count"))
    collisions = detect_collisions(rows)
    if collisions:
        raise _Stop(Refusal(
            "ID_COLLISION",
            "plan.merge: id collision: " + "; ".join(f"{i} in {', '.join(p)}" for i, p in collisions),
            tuple(sorted({p for _, ps in collisions for p in ps})),
            tuple(i for i, _ in collisions),
        ))
    base_by_key = {(r.source, r.row_id): b for r, b in zip(rows, bases)}

    applied = apply_delta(rows, delta)
    if isinstance(applied, Refusal):
        raise _Stop(applied)
    texts, accounts = applied
    if len(texts) != len(accounts):
        raise _Stop(Refusal("FIDELITY_BREACH", "plan.merge: apply_delta returned unequal text and account counts"))
    accounts = _classify(list(accounts), base_by_key, texts, set(rekey))

    dropped, problems, blocks, bodies = _account_prose(
        source_names, src_sections, cfg, list(delta.get("dropped_sections") or []),
    )
    parts = [_merged_frontmatter(delta, src_fms, source_names, plan_schema)]
    if delta.get("preamble"):
        parts.append(normalize_newlines(delta["preamble"]).strip("\n") + "\n")
    for ns in delta.get("new_sections") or []:
        parts.append(ns["heading"].strip() + "\n\n" + normalize_newlines(ns["content"]).strip("\n") + "\n")
    parts.append("## Consolidation delta\n\n" + normalize_newlines(render_delta_tables(
        accounts,
        [(sec.heading, sec.source, reason) for sec, reason in dropped],
        {e["id"]: e["reason"] for e in delta.get("rows") or [] if e.get("reason")},
    )).strip("\n") + "\n")
    parts.extend(b.strip("\n") + "\n" for b in blocks)
    parts.append("## Tasks\n\n" + _FENCE_OPEN + "\n".join(t.rstrip("\n") for t in texts) + _FENCE_CLOSE + "\n")
    out_text = normalize_newlines("\n".join(parts))

    checks, details = _verify(
        out_text, accounts, bases, base_by_key, {e["id"]: e for e in delta.get("rows") or []},
        {"problems": problems, "carried_bodies": bodies}, source_names, schemas,
    )
    refusal = _first_failure(checks, details)
    if refusal:
        return _refuse(refusal)

    report = MergeReport(
        tuple(accounts), tuple(b.row_id for b in bases), tuple(s.heading for s, _ in dropped), checks,
    )
    atomic_write_bytes(out, out_text.encode("utf-8"))
    return {"ok": True, "out": params["out"], "report": report.to_dict(), "summary": _summary(report)}


@register_op("plan.merge")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.merge" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.merge requires a resolved repo_root")
    root = Path(main_worktree_root(Path(repo_root))).resolve()
    sources = params.get("sources")
    if not isinstance(sources, list) or len(sources) < 2:
        raise ValueError("plan.merge: sources must list 2 or more plan paths")
    source_paths = [_rel_path("sources", s, root) for s in sources]
    delta_path = _rel_path("delta", params.get("delta"), root)
    out = _rel_path("out", params.get("out"), root)
    schema_dir = _rel_path("schema_dir", params["schema_dir"], root) if params.get("schema_dir") else None
    force = bool(params.get("force", False))

    if any(_same(out, s) for s in source_paths):
        return _refuse(Refusal(
            "OUTPUT_IS_SOURCE", "plan.merge: out is one of the sources",
            sources=tuple(sources),
        ))
    if out.exists() and not force:
        return _refuse(Refusal(
            "OUTPUT_EXISTS", f"plan.merge: out exists: {params['out']}; pass force to replace",
        ))
    try:
        return _merge(root, params, sources, out, delta_path, schema_dir)
    except _Stop as stop:
        return _refuse(stop.refusal)
