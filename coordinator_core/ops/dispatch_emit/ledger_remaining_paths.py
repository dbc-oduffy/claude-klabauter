"""Pure extractor deciding the write paths of a ledger-resume's remaining-work leg.

Stdlib only: no subprocess, no git.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from coordinator_core.claude_md_budget import is_claude_md_class
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_DISPATCH, LEG_REFUSE, LEG_SKIP
from coordinator_core.ops.dispatch_emit.pre_halt_credit import note_rerules
from coordinator_core.session import record_homes

_STRIP_EDGES = "`'\"()[]<>{}"
_TRAILING = ".,;:)"
_LINE_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?)+$")
_SYMBOL_SUFFIX = re.compile(r"::[\w.]+$")
_MODULE_TOKEN = re.compile(r"^([A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)?$")
_PATHLIKE = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)*$")
_DROP_HEADS = ("scratch/", Path(record_homes.home_dir("", "warp")).as_posix() + "/")
_REMEDY = "revise the plan with a row whose writes cover them, or send a re-ruling revise note"


def _clean(token: str) -> Optional[str]:
    t = token.replace("\\", "/").strip(_STRIP_EDGES)
    while t and t[-1] in _TRAILING:
        t = t[:-1].rstrip(_STRIP_EDGES)
    t = t.split("#", 1)[0]
    t = _SYMBOL_SUFFIX.sub("", t)
    t = _LINE_SUFFIX.sub("", t)
    while t and t[-1] in _TRAILING:
        t = t[:-1]
    if t.startswith("./"):
        t = t[2:]
    if not t or t.startswith("/") or re.match(r"^[A-Za-z]:", t):
        return None
    if ".." in t.split("/") or not _PATHLIKE.match(t):
        return None
    if "/" not in t and not re.search(r"\.[A-Za-z0-9]+$", t):
        return None
    if t.startswith(_DROP_HEADS):
        return None
    return t


def _exists(rel: str, repo_root: Path) -> bool:
    p = repo_root / rel
    return p.is_file() or ("/" in rel and p.parent.is_dir())


def named_paths(texts: Iterable[str], repo_root: Path) -> list[str]:
    """Ordered, de-duplicated repo-relative paths the whitespace tokens of `texts` name and that exist."""
    out: list[str] = []
    for text in texts:
        for tok in str(text).split():
            rel = _clean(tok)
            if rel and rel not in out and _exists(rel, repo_root):
                out.append(rel)
    return out


def gated_paths(paths: Iterable[str]) -> list[str]:
    # Surfaces a dispatched agent cannot write without a live PM grant, so the EM performs them inline.
    return [p for p in paths if is_claude_md_class(p)]


class PlanRowsUnreadable(Exception):
    """The plan cannot be read, or carries no parseable spine; the message names the failure."""


def plan_rows_serving(plan_path: Path, blocking: Sequence[str]) -> list[str]:
    """Ordered, de-duplicated `writes` of every spine row serving a blocking id.

    Raises `PlanRowsUnreadable` when the plan cannot be read or its spine does not parse: an
    unreadable plan is never an empty footprint.

    Landed rows count (the owed work lies on their files); `wont_do` and `spun_off` rows do not.
    A row serves an id through its `register_rows`, or through the frontmatter's
    `register_claims.rows` when it declares none.
    """
    import yaml

    from coordinator_core.ops.plan_tasks_render import load_rows
    from coordinator_core.frontmatter.body_blocks import LocateStatus

    try:
        text = Path(plan_path).read_text(encoding="utf-8")
        loaded = load_rows(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise PlanRowsUnreadable(f"plan {plan_path} unreadable: {exc}") from exc
    if loaded.status is not LocateStatus.LOCATED:
        raise PlanRowsUnreadable(f"plan {plan_path} spine is {loaded.status.name}")
    spine = loaded.rows
    claims: list[str] = []
    if text.startswith("---"):
        try:
            fm = yaml.safe_load(text.split("---", 2)[1]) or {}
            raw = (fm.get("register_claims") or {}).get("rows") or []
            claims = [str(c) for c in raw] if isinstance(raw, list) else []
        except (ValueError, yaml.YAMLError) as exc:
            raise PlanRowsUnreadable(f"plan {plan_path} frontmatter unparseable: {exc}") from exc
    blocked = set(blocking)
    out: list[str] = []
    for row in spine:
        if row.get("disposition") in ("wont_do", "spun_off"):
            continue
        rr = row.get("register_rows")
        serves = [str(x) for x in rr] if isinstance(rr, list) and rr else claims
        if not blocked.intersection(serves):
            continue
        for w in row.get("writes") or []:
            if isinstance(w, str) and w not in out:
                out.append(w)
    return out


def _module_paths(texts: Iterable[str], serving: Sequence[str], repo_root: Path) -> list[str]:
    dirs = list(dict.fromkeys(p.rsplit("/", 1)[0] for p in serving if "/" in p))
    mods: list[str] = []
    for text in texts:
        for tok in str(text).split():
            m = _MODULE_TOKEN.match(tok.strip(_STRIP_EDGES).rstrip(_TRAILING))
            if m and m.group(1) not in mods:
                mods.append(m.group(1))
    return [f"{d}/{m}.py" for m in mods for d in dirs if (repo_root / d / f"{m}.py").is_file()]


def derive_ledger_rows(
    remaining: Mapping[str, Mapping[str, Any]],
    notes: Sequence[str],
    blocking: Sequence[str],
    serving_writes: Mapping[str, Sequence[str]],
    existing_ids: Iterable[str],
    repo_root: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return `(rows, underivable)`: one dispatchable row per open ledger item.

    An item is open when its `remaining` list is non-empty, or it has none and a live (sha-free)
    note exists. A re-ruling note derives nothing. `writes` = named paths, then module tokens
    resolved beside the item's serving writes, then the serving writes; CLAUDE.md-class paths
    are excluded. An open item with no resolved write is returned in `underivable`.
    """
    from coordinator_core.ops.dispatch_emit.ask_contract import LEDGER_DERIVED_ROW_PREFIX

    if note_rerules(notes):
        return [], []
    taken = set(existing_ids)
    rows: list[dict[str, Any]] = []
    underivable: list[str] = []
    for item in blocking:
        steps = [str(s) for s in (remaining.get(item) or {}).get("remaining") or []]
        if not steps and not notes:
            continue
        texts = [*steps, *(str(n) for n in notes)]
        serving = [str(w) for w in serving_writes.get(item) or []]
        writes: list[str] = []
        for p in [*named_paths(texts, repo_root), *_module_paths(texts, serving, repo_root), *serving]:
            if p not in writes and p not in gated_paths([p]):
                writes.append(p)
        if not writes:
            underivable.append(item)
            continue
        rid = f"{LEDGER_DERIVED_ROW_PREFIX}{item}"
        n = 1
        while rid in taken:
            n += 1
            rid = f"{LEDGER_DERIVED_ROW_PREFIX}{item}-{n}"
        taken.add(rid)
        lines = [f"Ledger item {item} is open. Remaining steps:", *(f"- {s}" for s in steps)]
        if notes:
            lines += ["Revise notes:", *(f"- {n_}" for n_ in notes)]
        lines.append(f"Done when: the ledger check judges {item} met.")
        rows.append({
            "id": rid,
            "title": steps[0] if steps else f"Serve revise note for {item}",
            "change_kind": "code-edit",
            "writes": writes,
            "register_rows": [item],
            "disposition": "open",
            "body": "\n".join(lines),
        })
    return rows, underivable


def _named(remaining: Mapping[str, Mapping[str, Any]], repo_root: Path) -> list[str]:
    return named_paths((t for row in remaining.values() for t in row.get("remaining") or []), repo_root)


def remaining_leg_plan(
    remaining: Mapping[str, Mapping[str, Any]],
    notes: Sequence[str],
    rows: Sequence[Any],
    blocking: Sequence[str],
    doc_writes: Sequence[str],
    sizing_rel: Optional[str],
    repo_root: Path,
    *,
    plan_writes: Sequence[str] = (),
    arm: Optional[str] = None,
) -> dict[str, Any]:
    """Return `{action, writes, unwritable, em_owed, reason}` for the ledger-remaining leg.

    `writes` = writes of the manifest `rows` whose `register_rows` intersect `blocking` (a row
    with none serves no blocking row), plus the named remaining paths the run's commit can land
    (doc writes or the sizing); `em_owed` = named CLAUDE.md-class paths the EM edits inline (never
    in `writes` or `unwritable`); `unwritable` = other named paths nothing can land.
    `plan_writes` (see `plan_rows_serving`) joins `writes` after the manifest writes.
    A note naming a commit sha (digit and letter, 7-40 hex) re-rules the owed rows as met, so
    unwritable paths alone do not refuse, and with no writes the leg skips. A sha-free note with
    owed work and no writes routes to the re-plan leg, which only a non-xs arm runs; at `arm`
    xs nothing re-plans, so the leg refuses (`test_ask_compose_xs_empty_footprint_leg`). The sha is not resolved against
    git: the verify leg judges.
    """
    named = _named(remaining, repo_root)
    em_owed = gated_paths(named)
    allowed = set(doc_writes) | ({sizing_rel} if sizing_rel else set())
    blocked = set(blocking)
    manifest_writes = [
        w for r in rows if blocked.intersection(getattr(r, "register_rows", ()) or ()) for w in r.writes
    ]
    writes: list[str] = []
    for p in [*manifest_writes, *plan_writes, *(n for n in named if n in allowed)]:
        if p not in writes:
            writes.append(p)
    unwritable = [n for n in named if n not in writes and n not in em_owed]
    owed = [rid for rid, row in remaining.items() if row.get("remaining")]
    rerule = note_rerules(notes)
    if owed and ((unwritable and not rerule) or (not writes and not notes and not em_owed)):
        if unwritable:
            reason = "remaining names paths no row can land: " + ", ".join(unwritable)
        else:
            reason = "remaining of " + ", ".join(owed) + " names no landable path"
        return {"action": LEG_REFUSE, "writes": writes, "unwritable": unwritable, "em_owed": em_owed,
                "reason": f"{reason}; {_REMEDY}"}
    if not writes and em_owed:
        return {"action": LEG_SKIP, "writes": [], "unwritable": unwritable, "em_owed": em_owed,
                "reason": "remaining names CLAUDE.md-class paths the EM edits inline: "
                + ", ".join(em_owed) + "; the verify leg judges the EM's edit"}
    if not writes and notes:
        if rerule or not owed:
            return {"action": LEG_SKIP, "writes": [], "unwritable": unwritable, "em_owed": em_owed,
                    "reason": "re-ruling-only revise: no leg to run, the verify leg judges"}
        from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_XS

        if arm == ARM_XS:
            return {"action": LEG_REFUSE, "writes": [], "unwritable": unwritable, "em_owed": em_owed,
                    "reason": "remaining of " + ", ".join(owed) + " owes work but arm xs has no writes and "
                    f"no re-plan leg; {_REMEDY}"}
        return {"action": LEG_DISPATCH, "writes": [], "unwritable": unwritable, "em_owed": em_owed,
                "reason": "revise note asks for work: the re-plan leg adds the rows"}
    return {"action": LEG_DISPATCH, "writes": writes, "unwritable": unwritable, "em_owed": em_owed,
            "reason": ""}
