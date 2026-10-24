"""Pure extractor deciding the write paths of a ledger-resume's remaining-work leg.

Stdlib only: no subprocess, no git.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from coordinator_core.claude_md_budget import is_claude_md_class
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_DISPATCH, LEG_REFUSE, LEG_SKIP
from coordinator_core.ops.dispatch_emit.pre_halt_credit import note_rerules, sha_tokens
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


def drop_excluded(paths: Sequence[str], excluded: Sequence[str]) -> list[str]:
    # Lazy: ask_exclusions imports `_clean` from this module.
    from coordinator_core.ops.dispatch_emit import ask_exclusions

    return ask_exclusions.drop_excluded(paths, excluded)


def _exists(rel: str, repo_root: Path) -> bool:
    p = repo_root / rel
    return p.is_file() or ("/" in rel and p.parent.is_dir())


Spelling = Callable[[Sequence[str]], Mapping[str, Optional[str]]]


def _cleaned(texts: Iterable[str]) -> list[str]:
    out: list[str] = []
    for text in texts:
        for tok in str(text).split():
            rel = _clean(tok)
            if rel and rel not in out:
                out.append(rel)
    return out


def _named_tagged(
    texts: Iterable[str], repo_root: Path, spelling: Optional[Spelling], tag_texts: Sequence[str] = ()
) -> tuple[list[str], list[str], set[str]]:
    """`(admitted, dropped, tagged)`: `tagged` is the admitted subset naming a token of `tag_texts`.

    With `spelling`, every parent dir of the named paths rides in the one resolver call, unfiltered
    by disk case, so a new file under a case-variant spelling of a tracked dir is admitted in the
    dir's tracked spelling.
    """
    tag_cands = set(_cleaned(tag_texts))
    out: list[str] = []
    tagged: set[str] = set()
    if spelling is None:
        for rel in _cleaned(texts):
            if rel not in out and _exists(rel, repo_root):
                out.append(rel)
                if rel in tag_cands:
                    tagged.add(rel)
        return out, [], tagged
    cands = _cleaned(texts)
    parents = list(dict.fromkeys(c.rsplit("/", 1)[0] for c in cands if "/" in c))
    resolved = dict(spelling([*cands, *(p for p in parents if p not in cands)])) if cands else {}
    dropped: list[str] = []
    for c in cands:
        s = resolved.get(c)
        if s is None and "/" in c:
            parent, base = c.rsplit("/", 1)
            ps = resolved.get(parent)
            if ps is not None and (repo_root / ps).is_dir():
                s = f"{ps}/{base}"
        if s is None:
            dropped.append(c)
            continue
        if s not in out:
            out.append(s)
        if c in tag_cands:
            tagged.add(s)
    return out, dropped, tagged


def _named_split(
    texts: Iterable[str], repo_root: Path, spelling: Optional[Spelling]
) -> tuple[list[str], list[str]]:
    """`(admitted, dropped)` named paths, from one resolver call; `dropped` is empty when `spelling` is None."""
    out, dropped, _ = _named_tagged(texts, repo_root, spelling)
    return out, dropped


def named_paths(texts: Iterable[str], repo_root: Path, *, spelling: Optional[Spelling] = None) -> list[str]:
    """Ordered, de-duplicated repo-relative paths the whitespace tokens of `texts` name and that exist.

    With `spelling`, a path is admitted iff the resolver backs it, or its parent directory exists
    (a new file); it is returned in the resolver's spelling.
    """
    return _named_split(texts, repo_root, spelling)[0]


def gated_paths(paths: Iterable[str]) -> list[str]:
    # Surfaces a dispatched agent cannot write without a live PM grant, so the EM performs them inline.
    return [p for p in paths if is_claude_md_class(p)]


class PlanRowsUnreadable(Exception):
    """The plan cannot be read, or carries no parseable spine; the message names the failure."""


def serving_rows(plan_path: Path, blocking: Sequence[str]) -> list[dict[str, Any]]:
    """Spine rows (dicts, row `id` included) serving a blocking id, in spine order.

    Raises `PlanRowsUnreadable` when the plan cannot be read or its spine does not parse: an
    unreadable plan is never an empty selection.

    Landed rows count; `wont_do` and `spun_off` rows do not. A row serves an id through its
    `register_rows`, or through the frontmatter's `register_claims.rows` when it declares none.
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
    claims: list[str] = []
    if text.startswith("---"):
        try:
            fm = yaml.safe_load(text.split("---", 2)[1]) or {}
            raw = (fm.get("register_claims") or {}).get("rows") or []
            claims = [str(c) for c in raw] if isinstance(raw, list) else []
        except (ValueError, yaml.YAMLError) as exc:
            raise PlanRowsUnreadable(f"plan {plan_path} frontmatter unparseable: {exc}") from exc
    return rows_serving(loaded.rows, claims, blocking)


def rows_serving(rows: Iterable[Mapping[str, Any]], claims: Sequence[str], blocking: Sequence[str]) -> list[dict[str, Any]]:
    """`serving_rows`' predicate over spine `rows` in hand; `claims` is the frontmatter `register_claims.rows`."""
    blocked = set(blocking)
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("disposition") in ("wont_do", "spun_off"):
            continue
        rr = row.get("register_rows")
        serves = [str(x) for x in rr] if isinstance(rr, list) and rr else claims
        if blocked.intersection(serves):
            out.append(row)  # type: ignore[arg-type]
    return out


def row_ids_named(remaining: Iterable[str], ids: Iterable[str]) -> list[str]:
    """The `ids` (in their order) that are a whole whitespace token of a `remaining` line.

    Edge quotes, brackets and trailing punctuation are stripped first, so `X1:` and `(X1)` match
    `X1` and `X10` does not. Exact and case-sensitive.
    """
    tokens: set[str] = set()
    for line in remaining:
        for tok in str(line).split():
            t = tok.strip(_STRIP_EDGES)
            while t and t[-1] in _TRAILING:
                t = t[:-1].rstrip(_STRIP_EDGES)
            tokens.add(t)
    return [i for i in dict.fromkeys(ids) if i in tokens]


def plan_rows_serving(plan_path: Path, blocking: Sequence[str]) -> list[str]:
    """Ordered, de-duplicated `writes` of every spine row serving a blocking id (see `serving_rows`)."""
    out: list[str] = []
    for row in serving_rows(plan_path, blocking):
        declared = row.get("writes") or []
        for w in [declared] if isinstance(declared, str) else declared:
            if isinstance(w, str) and w not in out:
                out.append(w)
    return out


def _module_cands(texts: Iterable[str], serving: Sequence[str]) -> list[str]:
    dirs = list(dict.fromkeys(p.rsplit("/", 1)[0] for p in serving if "/" in p))
    mods: list[str] = []
    for text in texts:
        for tok in str(text).split():
            m = _MODULE_TOKEN.match(tok.strip(_STRIP_EDGES).rstrip(_TRAILING))
            if m and m.group(1) not in mods:
                mods.append(m.group(1))
    return [f"{d}/{m}.py" for m in mods for d in dirs]


def _module_paths(
    texts: Iterable[str], serving: Sequence[str], repo_root: Path, *, spelling: Optional[Spelling] = None
) -> list[str]:
    cands = _module_cands(texts, serving)
    if spelling is None:
        return [c for c in cands if (repo_root / c).is_file()]
    resolved = spelling(cands) if cands else {}
    return [c for c in cands if resolved.get(c) == c]


_DOC_BODY_HEADS = ("docs/plans/", "docs/problems/")


def _is_note_only_doc_body(path: str, step_named: set[str]) -> bool:
    """A plan or problem body named only by a revise note is evidence, never a write."""
    return path.casefold().startswith(_DOC_BODY_HEADS) and path.casefold() not in step_named


def derive_ledger_rows(
    remaining: Mapping[str, Mapping[str, Any]],
    notes: Sequence[str],
    blocking: Sequence[str],
    serving_writes: Mapping[str, Sequence[str]],
    existing_ids: Iterable[str],
    repo_root: Path,
    *,
    spelling: Optional[Spelling] = None,
    exclude: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return `(rows, underivable)`: one dispatchable row per open ledger item.

    An item is open when its `remaining` list is non-empty, or it has none and a live (sha-free)
    note exists. A sha-bearing (re-ruling) note credits landed rows but does not suppress an item
    that still has `remaining` steps (the call-site predicate, `pre_halt_credit` unchanged).
    With a sha-bearing note and `remaining` steps, an item derives a row only if that note's
    text itself resolves a write: a named path, a module token beside the item's serving writes,
    or a serving write named by path or file name (the note also rules new work). Otherwise it
    derives nothing and is not underivable. With no `remaining` it derives nothing.

    `writes` = named paths, then module tokens resolved beside the item's serving writes, then
    the serving writes; CLAUDE.md-class paths are excluded. An open item with no resolved write
    is returned in `underivable`.

    With `spelling`, every write is in the resolver's spelling: a named path it does not back is
    dropped and named in the row body, a serving write it does not back is kept as declared.

    Each `remaining` line is a gap to close, not a requirement: the title is `GAP_TITLE_PREFIX` +
    the first line (an item with only revise notes keeps "Serve revise note for <item>"), and the
    body lists the lines under `HALT_GAP_RULE` with a done-when that no gap line holds at HEAD.
    """
    from coordinator_core.ops.dispatch_emit.ask_contract import (
        GAP_TITLE_PREFIX,
        HALT_GAP_RULE,
        LEDGER_DERIVED_ROW_PREFIX,
    )

    rerule = note_rerules(notes)
    sha_notes = [str(n) for n in notes if sha_tokens(n)]
    taken = set(existing_ids)
    rows: list[dict[str, Any]] = []
    underivable: list[str] = []
    for item in blocking:
        steps = [str(s) for s in (remaining.get(item) or {}).get("remaining") or []]
        if not steps and (not notes or rerule):
            continue
        texts = [*steps, *(str(n) for n in notes)]
        serving = [str(w) for w in serving_writes.get(item) or []]
        named, dropped, note_named = _named_tagged(texts, repo_root, spelling, sha_notes)
        if spelling is not None and serving:
            resolved = spelling(serving)
            serving = [resolved.get(s) or s for s in serving]
        step_named = {c.casefold() for c in _cleaned(steps)} | {s.casefold() for s in serving}
        named = [p for p in named if not _is_note_only_doc_body(p, step_named)]
        note_named &= set(named)
        modules = _module_paths(texts, serving, repo_root, spelling=spelling)
        writes: list[str] = []
        for p in drop_excluded([*named, *modules, *serving], exclude):
            if p not in writes and p not in gated_paths([p]):
                writes.append(p)
        if rerule and steps:
            note_mods = set(_module_cands(sha_notes, serving))
            note_serving = {s for s in serving for c in _cleaned(sha_notes) if s == c or s.endswith("/" + c)}
            hits = note_named | {m for m in modules if m in note_mods} | note_serving
            if not any(p not in gated_paths([p]) for p in drop_excluded(sorted(hits), exclude)):
                continue
        dropped = [d for d in dropped if d not in writes]
        if not writes:
            if not rerule:
                underivable.append(item)
            continue
        rid = f"{LEDGER_DERIVED_ROW_PREFIX}{item}"
        n = 1
        while rid in taken:
            n += 1
            rid = f"{LEDGER_DERIVED_ROW_PREFIX}{item}-{n}"
        taken.add(rid)
        lines = [
            f"Ledger item {item} is open. Gaps to close (each line is still true; make it false):",
            *(f"- {s}" for s in steps),
            HALT_GAP_RULE,
        ]
        if notes:
            lines += ["Revise notes:", *(f"- {n_}" for n_ in notes)]
        if dropped:
            lines.append("Not declared (no tracked or present path): " + ", ".join(dropped))
        lines.append(f"Done when: no gap line above holds at HEAD and the ledger check judges {item} met.")
        rows.append({
            "id": rid,
            "title": GAP_TITLE_PREFIX + steps[0] if steps else f"Serve revise note for {item}",
            "change_kind": "code-edit",
            "writes": writes,
            "register_rows": [item],
            "disposition": "open",
            "body": "\n".join(lines),
        })
    return rows, underivable


def _named(
    remaining: Mapping[str, Mapping[str, Any]], repo_root: Path, *, spelling: Optional[Spelling] = None
) -> list[str]:
    return named_paths(
        (t for row in remaining.values() for t in row.get("remaining") or []), repo_root, spelling=spelling
    )


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
    spelling: Optional[Spelling] = None,
    exclude: Sequence[str] = (),
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
    named = drop_excluded(_named(remaining, repo_root, spelling=spelling), exclude)
    em_owed = gated_paths(named)
    allowed = set(doc_writes) | ({sizing_rel} if sizing_rel else set())
    blocked = set(blocking)
    manifest_writes = [
        w for r in rows if blocked.intersection(getattr(r, "register_rows", ()) or ()) for w in r.writes
    ]
    plan_writes = [str(w).replace("\\", "/").removeprefix("./") for w in plan_writes]
    if spelling is not None and plan_writes:
        respelled = spelling(plan_writes)
        plan_writes = [respelled.get(w) or w for w in plan_writes]
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
