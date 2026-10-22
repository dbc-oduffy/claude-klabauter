"""coordinator_core.roadmap.mise_residue -- the residue contract `mise-prep-run` hands a workflow.

A plan that stays short after the gate is "residue": mechanical declarations (`capabilities:`,
row `appends:`) and census entries to rewrite into the read-only form `census.screen` accepts.
This module answers five questions and does nothing else:

  batch_plans      which plans share one residue agent (1..RESIDUE_BATCH_MAX, each plan once)
  classify_census  which unscreenable census entry no read-only rewrite can carry ("leftover"),
                   and which is a check for a row's step 0 ("step0": `python -c` / pytest)
  spine_repairs    which failing bar classes are a residue-author repair, and which repair
  residue_args     what the workflow needs to run with no EM relay (`mise-prep-residue-args/4`)
  seam_ref_texts   the per-plan writes-collision findings files the args reference

It also pins the APM ruling interface the workflow, the op and `mise-prep-run` share:
`RULINGS_OP`, `ruling_item_id`, `subject_sha`, `render_pm_ruling` and `ruled_items`.

Caller: `coordinator/bin/mise-prep-run.py :: _emit_residue` calls `residue_args`/`has_work` and passes
`RESIDUE_AGENT_TYPE`.

No model literal lives here: the caller passes models resolved by `emit.resolve_agent_model`.
Reading a plan's census costs one `census.tracked_targets` spawn for the whole set, never one per
entry; everything else is string work.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from coordinator_core.session.record_homes import home_dir
from coordinator_core.roadmap import census as _census  # uses its private _split_stages/_PY_HEADS

SCHEMA = "mise-prep-residue-args/4"
#: The engine op that writes APM rulings into each plan's `pm_ruling:` list.
RULINGS_OP = "plan.record_residue_rulings"
RESIDUE_BATCH_MAX = 8
APM_AGENT_TYPE = "coordinator:apm"
#: The DoE agent that authors residue; callers pass it as `residue_agent_type`.
RESIDUE_AGENT_TYPE = "coordinator:residue-author"

#: Heads whose question cannot be asked read-only; the author has no rewrite to offer.
LEFTOVER_HEADS = frozenset({"node", "npx", "gh", "claude"})
_PYTEST_HEADS = frozenset({"pytest", "py.test"})

#: Gate bar class (`<CLASS>/<kind>`, the shape `mise-prep-run._failing_classes` builds) -> the
#: in-place repair the residue author applies. The unquoted-`on:` class is absent: the gate reports
#: no failing class for it, so that spine repair has no route (pinned by
#: test_unquoted_on_key_yields_no_failing_class).
SPINE_REPAIR_CLASSES: dict[str, str] = {
    "CENSUS/census-unscreenable": (
        "a census entry marked step0 (python -c / pytest) moves into the owning row's step 0 "
        "and the census is renumbered"
    ),
    "SPINE/MemoRowNoReceiptError": (
        "add the row's receipt from the plan's own text; absent there, return the row as a leftover"
    ),
    "SPINE/MemoRowNoReceiverError": (
        "add the row's memo_to from the plan's own text; absent there, return the row as a leftover"
    ),
    "SPINE/InvalidChangeKindError": (
        "re-tag change_kind to the schema enum value the row's writes imply"
    ),
    "TEST_RUNNER_OUT_OF_ROLE/test-runner-out-of-role-row": (
        "split into a performer: em verify row with the same command, leaving the original "
        "row's writes"
    ),
}

#: Seam classes handed to the residue author. For `writes-collision` the author's only repair is
#: `appends:` on rows that only append; any other collision comes back in `escalate`, and
#: `seam_line` still prints it with the `coordinator:apm` route.
AUTHOR_SEAM_CLASSES = frozenset({"capabilities-undeclared", "writes-collision"})

_CERTIFIED_OUTCOMES = frozenset({"stamped", "already-certified", "would-stamp"})
_SEAM_FIELDS = ("capability", "missing_consumer", "counterpart_plan", "path", "row", "detail")
_REFUSED_HEAD = re.compile(r"^'([^']+)' is not on the read-only allowlist")
_UNTRACKED_PYTHON = ("is not a git-tracked file of this repo", "is not a path inside the repo")


def _is_pytest_stage(parts: list[str], head: str) -> bool:
    if head in _PYTEST_HEADS:
        return True
    return head in _census._PY_HEADS and "-m" in parts and parts[parts.index("-m") + 1:][:1] == ["pytest"]


def classify_census(command: str, refusal: str) -> str:
    """"leftover", "step0" or "rewrite" for an unscreenable census entry.

    Leftover: a `node`/`npx`/`gh`/`claude` stage, or a non-pytest python target that is not a
    tracked file. Step0: a `python -c` or pytest-headed stage, a check the owning row runs in its
    step 0, not a census question. The command is scanned as well as the refusal, because the
    screen reports only the FIRST refusing stage and a `$( )` in front would hide a `node` behind it.
    """
    step0 = False
    for parts in _census._split_stages(command or "") or []:
        if not parts:
            continue
        head = Path(parts[0]).name
        if head in LEFTOVER_HEADS:
            return "leftover"
        if head in _census._PY_HEADS and "-c" in parts or _is_pytest_stage(parts, head):
            step0 = True
    if step0:
        return "step0"
    refusal = refusal or ""
    match = _REFUSED_HEAD.match(refusal)
    if match and match.group(1) in LEFTOVER_HEADS:
        return "leftover"
    if refusal.startswith("python -c"):
        return "step0"
    if refusal.startswith("python ") and any(tag in refusal for tag in _UNTRACKED_PYTHON):
        return "leftover"
    return "rewrite"


def spine_repairs(failing: list[str]) -> list[str]:
    """One `spine-repair: <class> — <repair>` line per failing class `SPINE_REPAIR_CLASSES` keys, input order."""
    return [
        f"spine-repair: {name} — {SPINE_REPAIR_CLASSES[name]}"
        for name in failing or []
        if name in SPINE_REPAIR_CLASSES
    ]


def _frontmatter(plan_text: str) -> dict:
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    fm = parse_frontmatter(plan_text).get("frontmatter")
    return fm if isinstance(fm, dict) else {}


def _entries(plan_text: str) -> list:
    entries = _frontmatter(plan_text).get("census")
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


def _owes_capabilities(row: dict) -> bool:
    return any(f.get("class") == "capabilities-undeclared" for f in row.get("seam") or [])


def _closed(row: dict) -> bool:
    return row.get("outcome") in _CERTIFIED_OUTCOMES or row.get("verdict") == "PREPPED"


def _certified(row: dict) -> bool:
    """True for a certified or PREPPED row that owes no capabilities declaration.

    A row with a capabilities-undeclared seam finding (blocking or not) is never certified: the
    author route is the same. Such a row enters the residue for that line alone; its census is
    still not residue when `_closed`.
    """
    return _closed(row) and not _owes_capabilities(row)


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
        out[plan] = {"path": Path(home_dir("", "mise-workflows"), f"{run_id}.seam", f"{stem}.txt").as_posix(), "lines": lines}
    return out


def seam_ref_texts(rows: Iterable[dict], run_id: str) -> dict[str, str]:
    """`{findings file path: text}` for the members `residue_args` binds by reference.

    Newline-joined lines with a trailing newline. Pure: it writes nothing; the caller writes.
    """
    refs = _collision_refs([r for r in rows if not _certified(r)], run_id)
    return {ref["path"]: "\n".join(ref["lines"]) + "\n" for ref in refs.values()}


def ruling_item_id(item: dict) -> str:
    """The stable id an APM ruling echoes: `census[<index>]` or `cycle:<path>`."""
    if item.get("kind") == "seam-fix-cycle":
        return f"cycle:{item.get('path') or ''}"
    return f"census[{item['index']}]"


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def subject_sha(item: dict) -> str:
    """sha256[:12] of what the ruling is about: a census entry's command, or a cycle's sorted plans."""
    if item.get("kind") == "seam-fix-cycle":
        return _sha12("\n".join(sorted(item.get("plans") or [])))
    return _sha12(str(item.get("command") or "").strip())


def _quote(value: Any) -> str:
    """YAML single-quoted scalar on one line; the frontmatter parser does not unescape double-quoted escapes."""
    return "'" + " ".join(str(value).split()).replace("'", "''") + "'"


def render_pm_ruling(entries: Iterable[dict]) -> str:
    """The `pm_ruling:` block: a YAML list of `{item, ruling, ruled_by, run_id, on, pm_only, subject_sha}`.

    `on` is emitted quoted as a key (bare `on` parses as the YAML 1.1 boolean) and every string
    value is single-quoted on one line, so the block round-trips through `parse_frontmatter`.
    """
    lines = ["pm_ruling:"]
    for e in entries:
        lines += [
            f"  - item: {_quote(e['item'])}",
            f"    ruling: {_quote(e['ruling'])}",
            f"    ruled_by: {e.get('ruled_by') or APM_AGENT_TYPE}",
            f"    run_id: {_quote(e.get('run_id') or '')}",
            f"    \"on\": {_quote(e.get('on') or '')}",
            f"    pm_only: {'true' if e.get('pm_only') else 'false'}",
            f"    subject_sha: {_quote(e.get('subject_sha') or '')}",
        ]
    return "\n".join(lines) + "\n"


def ruled_items(plan_text: str, cycle_subjects: Optional[dict] = None) -> frozenset:
    """Item ids the plan's `pm_ruling` list has ruled and whose subject has not changed since.

    A `census[i]` ruling counts while its `subject_sha` equals the hash of the plan's current
    census entry i; a `cycle:` ruling counts when `cycle_subjects[id]` (the cycle's current
    `subject_sha`) equals it. Absent or malformed `pm_ruling` yields the empty set.
    """
    ruled = _frontmatter(plan_text).get("pm_ruling")
    if not isinstance(ruled, list):
        return frozenset()
    entries = _entries(plan_text)
    out = set()
    for r in ruled:
        if not isinstance(r, dict) or not isinstance(r.get("item"), str):
            continue
        item, sha = r["item"], r.get("subject_sha")
        m = re.fullmatch(r"census\[(\d+)\]", item)
        if m:
            i = int(m.group(1))
            entry = entries[i] if i < len(entries) else None
            current = _sha12(str(entry.get("command") or "").strip()) if isinstance(entry, dict) else None
        else:
            current = (cycle_subjects or {}).get(item)
        if current is not None and sha == current:
            out.add(item)
    return frozenset(out)


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
    """The `args` object for `workflow.bind_args`, exactly the pinned `mise-prep-residue-args/4`.

    `rows` are the dicts `mise-prep-run.main` builds (plan, verdict, failing, detail, seam,
    outcome). A certified or PREPPED row is skipped entirely, unless it carries a
    capabilities-undeclared seam finding: it then batches for that line alone. Otherwise no
    batch member and no census apmItem (its cross-plan seams belong to seam_fix; a census
    refusal the gate does not reproduce is not residue). Any other row enters a batch when it has bar classes to author,
    an author-routed seam finding, or a rewritable census entry. A member's `seam` carries only
    its capabilities-undeclared lines; its owned writes-collision lines (one per path) live in
    the findings file named by `seamRef` (`seamRefLines` lines), whose text is `seam_ref_texts`. Leftover census entries of non-PREPPED rows and
    `seam_fix_reply["skipped_cycles"]` become `apmItems`, each with `id` and `subjectSha`; an item
    its plan already carries a current `pm_ruling` for is not sent again. Both lists empty means no workflow.
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
        entries = [] if _closed(row) else census_entries(texts[plan], root, tracked)
        ruled = ruled_items(texts[plan])
        rewrite = [e for e in entries if e["class"] != "leftover"]
        for e in entries:
            if e["class"] == "leftover":
                item = {
                    "kind": "census-leftover", "plan": plan, "index": e["index"],
                    "question": e["question"], "command": e["command"], "refusal": e["refusal"],
                }
                if ruling_item_id(item) in ruled:
                    continue
                apm_items.append({**item, "id": ruling_item_id(item), "subjectSha": subject_sha(item)})
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
            "spine": spine_repairs(row.get("failing") or []),
            "census": [
                {"index": e["index"], "command": e["command"], "refusal": e["refusal"], "class": e["class"]}
                for e in rewrite
            ],
        }
        if ref:
            member["seamRef"] = ref["path"]
            member["seamRefLines"] = len(ref["lines"])
        members.append(member)

    for cycle in (seam_fix_reply or {}).get("skipped_cycles") or []:
        item = {
            "kind": "seam-fix-cycle", "plans": list(cycle.get("plans") or []),
            "path": cycle.get("path") or "", "detail": cycle.get("detail") or "",
        }
        cid, sha = ruling_item_id(item), subject_sha(item)
        if any(cid in ruled_items(texts[p] if p in texts else reader(root, p), {cid: sha}) for p in item["plans"]):
            continue
        apm_items.append({**item, "id": cid, "subjectSha": sha})

    return {
        "schema": SCHEMA,
        "repoRoot": root.as_posix(),
        "runId": run_id,
        "gateCommand": gate_command,
        "rulingsCommand": f"mise-prep-run --repo-root {root.as_posix()} --apply-rulings",
        "residue": {"agentType": residue_agent_type, "model": residue_model},
        "apm": {"agentType": APM_AGENT_TYPE, "model": apm_model},
        "batches": batch_plans(members),
        "apmItems": apm_items,
    }


def has_work(args: dict[str, Any]) -> bool:
    """True when the args name any batch or APM item; false means emit no workflow."""
    return bool(args.get("batches") or args.get("apmItems"))
