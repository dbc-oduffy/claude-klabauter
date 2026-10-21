"""coordinator_core.roadmap.mise_residue -- the residue contract `mise-prep-run` hands a workflow.

A plan that stays short after the gate is "residue": mechanical declarations (`capabilities:`,
row `appends:`) and census entries to rewrite into the read-only form `census.screen` accepts.
This module answers three questions and does nothing else:

  batch_plans      which plans share one residue agent (1..RESIDUE_BATCH_MAX, each plan once)
  classify_census  which unscreenable census entry no read-only rewrite can carry ("leftover")
  residue_args     what the workflow needs to run with no EM relay (`mise-prep-residue-args/2`)
  seam_ref_texts   the per-plan writes-collision findings files the args reference

Caller: `coordinator/bin/mise-prep-run.py :: _emit_residue` calls `residue_args`/`has_work` and passes
`RESIDUE_AGENT_TYPE`.

No model literal lives here: the caller passes models resolved by `emit.resolve_agent_model`.
Reading a plan's census costs one `census.tracked_targets` spawn for the whole set, never one per
entry; everything else is string work.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from coordinator_core.roadmap import census as _census  # uses its private _split_stages/_PY_HEADS

SCHEMA = "mise-prep-residue-args/2"
RESIDUE_BATCH_MAX = 8
APM_AGENT_TYPE = "coordinator:apm"
#: The DoE agent that authors residue; callers pass it as `residue_agent_type`.
RESIDUE_AGENT_TYPE = "coordinator:plan-author-residue"

#: Heads whose question cannot be asked read-only; the author has no rewrite to offer.
LEFTOVER_HEADS = frozenset({"node", "npx", "gh", "claude"})

#: Seam classes handed to the residue author. For `writes-collision` the author's only repair is
#: `appends:` on rows that only append; any other collision comes back in `escalate`, and
#: `seam_line` still prints it with the `coordinator:apm` route.
AUTHOR_SEAM_CLASSES = frozenset({"capabilities-undeclared", "writes-collision"})

_CERTIFIED_OUTCOMES = frozenset({"stamped", "already-certified", "would-stamp"})
_SEAM_FIELDS = ("capability", "missing_consumer", "counterpart_plan", "path", "row", "detail")
_REFUSED_HEAD = re.compile(r"^'([^']+)' is not on the read-only allowlist")
_UNTRACKED_PYTHON = ("is not a git-tracked file of this repo", "is not a path inside the repo")


def classify_census(command: str, refusal: str) -> str:
    """"leftover" when no read-only command can carry the entry's question, else "rewrite".

    Leftover: a `node`/`npx`/`gh`/`claude` stage, `python -c`, or a python target that is not a
    tracked file. The command is scanned as well as the refusal, because the screen reports only
    the FIRST refusing stage and a `$( )` in front would hide a `node` behind it.
    """
    for parts in _census._split_stages(command or "") or []:
        if not parts:
            continue
        head = Path(parts[0]).name
        if head in LEFTOVER_HEADS:
            return "leftover"
        if head in _census._PY_HEADS and "-c" in parts:
            return "leftover"
    refusal = refusal or ""
    match = _REFUSED_HEAD.match(refusal)
    if match and match.group(1) in LEFTOVER_HEADS:
        return "leftover"
    if refusal.startswith("python -c"):
        return "leftover"
    if refusal.startswith("python ") and any(tag in refusal for tag in _UNTRACKED_PYTHON):
        return "leftover"
    return "rewrite"


def _entries(plan_text: str) -> list:
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    fm = parse_frontmatter(plan_text).get("frontmatter") or {}
    entries = fm.get("census") if isinstance(fm, dict) else None
    return entries if isinstance(entries, list) else []


def census_entries(plan_text: str, repo_root: Path, tracked: Optional[frozenset] = None) -> list[dict]:
    """The plan's unscreenable census entries: `{index, question, command, refusal, class}`.

    `tracked` is the batch answer from `census.tracked_targets`; omitted, it is resolved for this
    plan alone (one spawn). Entries that screen clean, and entries that are not mappings with a
    command, are not residue and are omitted.
    """
    entries = _entries(plan_text)
    if tracked is None:
        tracked = _census.tracked_targets(
            Path(repo_root),
            [
                c
                for e in entries
                if isinstance(e, dict)
                for c in _census.python_candidates(str(e.get("command") or ""), Path(repo_root))
            ],
        )
    out: list[dict] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command") or "").strip()
        if not command:
            continue
        refusal = _census.screen(command, Path(repo_root), tracked)
        if refusal:
            out.append({
                "index": index,
                "question": str(entry.get("question") or ""),
                "command": command,
                "refusal": refusal,
                "class": classify_census(command, refusal),
            })
    return out


def batch_plans(rows: Iterable[dict]) -> list[dict]:
    """`[{"id": "b1", "plans": [<row>...]}, ...]`: input order kept, each plan once, 1..8 per batch.

    A plan named twice keeps its first row. Rows are passed through unchanged for the caller to
    shape; an empty input yields no batch.
    """
    seen: set = set()
    unique: list = []
    for row in rows:
        plan = row.get("plan")
        if plan in seen:
            continue
        seen.add(plan)
        unique.append(row)
    return [
        {"id": f"b{n + 1}", "plans": unique[i:i + RESIDUE_BATCH_MAX]}
        for n, i in enumerate(range(0, len(unique), RESIDUE_BATCH_MAX))
    ]


def seam_line(finding: dict) -> str:
    """One `seam:` line in the form `mise-prep-run` prints, route included."""
    parts = [str(finding.get("class"))]
    parts += [f"{k}={finding[k]}" for k in _SEAM_FIELDS if finding.get(k) not in (None, "")]
    if not finding.get("blocking"):
        parts.append("(not blocking)")
    route = (
        "route: plan-author, declare capabilities"
        if finding.get("class") == "capabilities-undeclared"
        else "route: coordinator:apm, scope"
    )
    return "seam: " + " ".join(parts) + f" — {route}"


def _author_seam(row: dict) -> list[dict]:
    return [f for f in row.get("seam") or [] if f.get("class") in AUTHOR_SEAM_CLASSES]


def _certified(row: dict) -> bool:
    """True for a row that is certified or PREPPED: it contributes nothing to the residue."""
    return row.get("outcome") in _CERTIFIED_OUTCOMES or row.get("verdict") == "PREPPED"


def _collision_refs(short_rows: Iterable[dict], run_id: str) -> dict[str, dict]:
    """`{plan: {"path": <repo-relative POSIX findings file>, "lines": [seam_line...]}}`.

    The one builder behind both the args reference and the file text. Only writes-collision
    findings the row owns (its own `plan`, or no `plan` key) count, one line per path; records
    sharing a path merge, keeping the first line and appending the others' counterparts. A row
    with no such line has no entry. Two plans sharing a stem raise ValueError.
    """
    out: dict[str, dict] = {}
    stems: dict[str, str] = {}
    for row in short_rows:
        plan = row["plan"]
        if plan in out:
            continue
        by_path: dict[str, list[dict]] = {}
        for f in row.get("seam") or []:
            if f.get("class") != "writes-collision" or f.get("plan") not in (None, plan):
                continue
            by_path.setdefault(str(f.get("path") or ""), []).append(f)
        if not by_path:
            continue
        lines = []
        for path in sorted(by_path):
            recs = by_path[path]
            line = seam_line(recs[0])
            others = [str(r["counterpart_plan"]) for r in recs[1:] if r.get("counterpart_plan")]
            if others:
                line += f"; also {', '.join(others)}"
            lines.append(line)
        stem = Path(plan).stem
        if stems.setdefault(stem, plan) != plan:
            raise ValueError(f"plan stem {stem!r} names both {stems[stem]} and {plan}")
        out[plan] = {"path": f"state/mise-workflows/{run_id}.seam/{stem}.txt", "lines": lines}
    return out


def seam_ref_texts(rows: Iterable[dict], run_id: str) -> dict[str, str]:
    """`{findings file path: text}` for the members `residue_args` binds by reference.

    Newline-joined lines with a trailing newline. Pure: it writes nothing; the caller writes.
    """
    refs = _collision_refs([r for r in rows if not _certified(r)], run_id)
    return {ref["path"]: "\n".join(ref["lines"]) + "\n" for ref in refs.values()}


def _read(repo_root: Path, plan: str) -> str:
    try:
        return (repo_root / plan).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def residue_args(
    rows: list[dict],
    seam_fix_reply: Optional[dict],
    *,
    repo_root: Path,
    run_id: str,
    gate_command: str,
    residue_agent_type: str,
    residue_model: str,
    apm_model: str,
    read_text: Optional[Callable[[Path, str], str]] = None,
) -> dict:
    """The `args` object for `workflow.bind_args`, exactly the pinned `mise-prep-residue-args/2`.

    `rows` are the dicts `mise-prep-run.main` builds (plan, verdict, failing, detail, seam,
    outcome). A certified or PREPPED row is skipped entirely: no batch member and no census
    apmItem (its cross-plan seams belong to seam_fix; a census refusal the gate does not
    reproduce is not residue). Any other row enters a batch when it has bar classes to author,
    an author-routed seam finding, or a rewritable census entry. A member's `seam` carries only
    its capabilities-undeclared lines; its owned writes-collision lines (one per path) live in
    the findings file named by `seamRef` (`seamRefLines` lines), whose text is `seam_ref_texts`. Leftover census entries of non-PREPPED rows and
    `seam_fix_reply["skipped_cycles"]` become `apmItems`. Both lists empty means no workflow.
    """
    root = Path(repo_root)
    reader = read_text or _read
    short = [r for r in rows if not _certified(r)]
    refs = _collision_refs(short, run_id)

    texts = {r["plan"]: reader(root, r["plan"]) for r in short}
    candidates = [
        c
        for text in texts.values()
        for e in _entries(text)
        if isinstance(e, dict)
        for c in _census.python_candidates(str(e.get("command") or ""), root)
    ]
    tracked = _census.tracked_targets(root, candidates)

    members: list[dict] = []
    apm_items: list[dict] = []
    for row in short:
        plan = row["plan"]
        entries = census_entries(texts[plan], root, tracked)
        rewrite = [e for e in entries if e["class"] == "rewrite"]
        for e in entries:
            if e["class"] == "leftover":
                apm_items.append({
                    "kind": "census-leftover", "plan": plan, "index": e["index"],
                    "question": e["question"], "command": e["command"], "refusal": e["refusal"],
                })
        author = _author_seam(row)
        ref = refs.get(plan)
        inline = [f for f in author if f.get("class") != "writes-collision"]
        if not (row.get("failing") or inline or ref or rewrite):
            continue
        bar = ([f"bar: {', '.join(row['failing'])}"] if row.get("failing") else [])
        bar += [f"said: {line}" for line in row.get("detail") or []]
        member = {
            "plan": plan,
            "bar": bar,
            "seam": [seam_line(f) for f in inline],
            "census": [
                {"index": e["index"], "command": e["command"], "refusal": e["refusal"]}
                for e in rewrite
            ],
        }
        if ref:
            member["seamRef"] = ref["path"]
            member["seamRefLines"] = len(ref["lines"])
        members.append(member)

    for cycle in (seam_fix_reply or {}).get("skipped_cycles") or []:
        apm_items.append({
            "kind": "seam-fix-cycle", "plans": list(cycle.get("plans") or []),
            "path": cycle.get("path") or "", "detail": cycle.get("detail") or "",
        })

    return {
        "schema": SCHEMA,
        "repoRoot": root.as_posix(),
        "runId": run_id,
        "gateCommand": gate_command,
        "residue": {"agentType": residue_agent_type, "model": residue_model},
        "apm": {"agentType": APM_AGENT_TYPE, "model": apm_model},
        "batches": batch_plans(members),
        "apmItems": apm_items,
    }


def has_work(args: dict[str, Any]) -> bool:
    """True when the args name any batch or APM item; false means emit no workflow."""
    return bool(args.get("batches") or args.get("apmItems"))
