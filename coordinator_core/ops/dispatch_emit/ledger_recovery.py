"""Ledger-recovery interface: attempt cap, fix-leg label/schema/prompt, reopen attribution and landing-withheld rule.

Pure: no I/O, no git, no subprocess; stdlib only. `ask_compose` imports this module and splices
`REOPEN_PLAN_JS` into the workflow; `reopen_plan` is that function's Python statement, as
`seam_repair.is_repairable` is for `_SEAM_REPAIRABLE`. `landing_withheld` runs engine-side only.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from coordinator_core.ops.dispatch_emit.ask_contract import HALT_GAP_RULE

#: One attempt is one `ledger-fix` leg per reopened chunk followed by one re-verify.
RECOVERY_ATTEMPT_CAP = 2

LEDGER_FIX_LABEL = "ledger-fix:"
LEDGER_FIX_PHASE = "ledger-fix"
#: Never `verify`: the ask script composes exactly one `verify` leg.
LEDGER_REVERIFY_LABEL = "ledger-reverify"

REMNANT_LABEL = "remnant-complete"
REMNANT_PHASE = "remnant-complete"
#: Never `verify` nor `ledger-reverify`: the ask script composes exactly one `verify` leg.
REMNANT_REVERIFY_LABEL = "remnant-reverify"
#: One remnant leg plus one re-verify; a gap still open after it halts as before.
REMNANT_ATTEMPT_CAP = 1

RECOVERY_RECORD_FILE = "recovery.json"
RECOVERY_WITHHELD_PARAM = "recovery_withheld"

CAP_EXHAUSTED = "recovery-cap-exhausted"
FIX_BLOCKED = "recovery-fix-blocked"
HELD = "recovery-held"
UNATTRIBUTED = "recovery-unattributed"
IMPORT_CLOSURE_OPEN = "import-closure-open"

STATUS_FIXED = "fixed"
STATUS_EXHAUSTED = "exhausted"
STATUS_HELD = "held"
STATUS_BLOCKED = "blocked"

#: `recovery.json` status -> withhold reason. `fixed` is verified and lands.
STATUS_REASON = {
    STATUS_EXHAUSTED: CAP_EXHAUSTED,
    STATUS_BLOCKED: FIX_BLOCKED,
    STATUS_HELD: HELD,
}

LEDGER_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["DONE", "BLOCKED"]},
        "paths": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["status", "paths", "reason"],
    "additionalProperties": False,
}


def ledger_fix_prompt(remaining_var: str, evidence_var: str, failing_var: str, footprint_var: str) -> str:
    """Instruction tail for the fix agent; the four args are JS expressions spliced in.

    Trap: the footprint is prose here; the only check is the agent's self-reported `paths`.
    """
    return (
        "A requirement row is not met. Gaps to close (each is still true; make it false): ${"
        + remaining_var + "}. " + HALT_GAP_RULE + " "
        "Evidence: ${" + evidence_var + "}. Failing output: ${" + failing_var + "}. "
        "The ONLY paths you may edit: ${" + footprint_var + "}. "
        "Reproduce first with `python -m pytest <failing> -q`. "
        "Fix within the footprint and return DONE with every path written. "
        "Return BLOCKED with the reason when the fix needs a path outside the footprint "
        "or the failure is not caused by this run's change. "
        "Commit nothing. Never edit a test to delete its assertion. "
        "Save captures under `scratch/ledger-fix/<run-id>/`."
    )


_PATH_TOKEN = re.compile(r"[A-Za-z0-9_.\-/]+")
_ID_TOKEN = re.compile(r"[A-Za-z0-9_.\-]+")


def _posix(p: Any) -> str:
    s = str(p).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _path_tokens(text: str) -> list[str]:
    out = []
    for t in _PATH_TOKEN.findall(_posix(text)):
        t = t.rstrip(".")
        if t:
            out.append(t)
    return out


def _id_tokens(text: str) -> list[str]:
    return [t for t in (x.strip(".-") for x in _ID_TOKEN.findall(str(text))) if t]


def _item_text(item: Mapping[str, Any]) -> str:
    rem = item.get("remaining")
    rem_s = " ".join(str(x) for x in rem) if isinstance(rem, (list, tuple)) else str(rem or "")
    return rem_s + " " + str(item.get("evidence") or "")


def _writes(row: Mapping[str, Any]) -> list[str]:
    return [_posix(w) for w in row.get("writes") or ()]


def _owns(row: Mapping[str, Any], path: str) -> bool:
    if path in _writes(row):
        return True
    return any(path.startswith(_posix(d).rstrip("/") + "/") for d in row.get("writes_under") or ())


def _failed_files(failed_tests: Iterable[str]) -> list[str]:
    return [_posix(str(t).split("::", 1)[0]) for t in failed_tests or ()]


def _blocking(item: Any) -> bool:
    if not isinstance(item, Mapping) or item.get("status") != "not_met":
        return False
    if item.get("essential") is False or item.get("withheld_by") or item.get("owed_by"):
        return False
    return True


def _add(bucket: dict, key: str, row_id: str) -> None:
    ids = bucket.setdefault(key, [])
    if row_id not in ids:
        ids.append(row_id)


def reopen_plan(items, manifest_rows, failed_tests, held_ids) -> dict:
    """Attribute each blocking `not_met` item to chunks, checked in order:

    (1) chunk ids appearing as tokens in `remaining`/`evidence`: a manifest row id reopens, a
    `held_ids` id absent from the manifest is held; (2) manifest chunks whose `writes` include a
    failing test file or a path named in `remaining`; (3) only when (1) and (2) are empty, the
    single manifest chunk whose `register_rows` claims the row. Anything else is unattributed.
    `drifted`, `post_commit`, owed and withheld items are never reopened.
    """
    rows = [r for r in manifest_rows or () if isinstance(r, Mapping)]
    by_id = {str(r.get("id")): r for r in rows}
    held_set = {str(h) for h in held_ids or ()}
    failed = _failed_files(failed_tests)
    reopen: dict[str, list[str]] = {}
    held: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for item in items or ():
        if not _blocking(item):
            continue
        rid = str(item.get("id"))
        text = _item_text(item)
        hit = False
        for tok in _id_tokens(text):
            if tok in by_id:
                _add(reopen, tok, rid)
                hit = True
            elif tok in held_set:
                _add(held, tok, rid)
                hit = True
        named = set(_path_tokens(text)) | set(failed)
        for row in rows:
            cid = str(row.get("id"))
            if any(_owns(row, p) for p in named):
                _add(reopen, cid, rid)
                hit = True
        if hit:
            continue
        claimants = [str(r.get("id")) for r in rows if rid in {str(x) for x in r.get("register_rows") or ()}]
        if len(claimants) == 1:
            _add(reopen, claimants[0], rid)
        elif rid not in unattributed:
            unattributed.append(rid)
    return {"reopen": reopen, "held": held, "unattributed": unattributed}


def _text_list(v: Any) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)] if v is not None and str(v).strip() else []


def _remnant_obj(v: Any) -> dict | None:
    if not isinstance(v, Mapping) or not isinstance(v.get("remnant"), Mapping):
        return None
    r = v["remnant"]
    return {"gap": str(r.get("gap") or ""), "paths": [_posix(p) for p in _text_list(r.get("paths"))], "checks": _text_list(r.get("checks"))}


def remnant_items(items, unattributed, capped, criterion, delivery) -> list[dict]:
    """The not_met work left after the ledger-fix loop, as `{kind, id, gap, paths, checks}` items.

    `ledger` items: blocking rows (as `reopen_plan` reads them; withheld, owed and non-essential rows
    never count) whose id is in `unattributed` or `capped`. A `criterion` item when the falsifier
    verdict is `not_met`, built from its `remnant` object `{gap, paths, checks}` when present, else
    from its `reason`. A `delivery` item when the verdict is `FAIL`. `[]` when all three are clear.
    """
    open_ids = {str(x) for x in unattributed or ()} | {str(x) for x in capped or ()}
    out: list[dict] = []
    for item in items or ():
        if _blocking(item) and str(item.get("id")) in open_ids:
            out.append({"kind": "ledger", "id": str(item.get("id")), "gap": " ".join(_text_list(item.get("remaining"))) or str(item.get("evidence") or ""),
                        "paths": [], "checks": []})
    if isinstance(criterion, Mapping) and criterion.get("status") == "not_met":
        r = _remnant_obj(criterion) or {"gap": "", "paths": [], "checks": []}
        out.append({"kind": "criterion", "id": "criterion", "gap": r["gap"] or str(criterion.get("reason") or ""), "paths": r["paths"], "checks": r["checks"]})
    if isinstance(delivery, Mapping) and delivery.get("verdict") == "FAIL":
        r = _remnant_obj(delivery) or {"gap": "", "paths": [], "checks": []}
        out.append({"kind": "delivery", "id": "delivery", "gap": r["gap"] or str(delivery.get("reason") or delivery.get("summary") or ""),
                    "paths": r["paths"], "checks": r["checks"]})
    return out


def remnant_prompt(items_var: str, footprint_var: str) -> str:
    """Instruction tail for the remnant agent; both args are JS expressions spliced in.

    The footprint is prose here; the engine fence is `remnant_breach` over the recorded
    `recovery.json` `remnant` object, applied at landing by `land_run`.
    """
    return (
        "The run's close checks found work still undone. Items (each gap is still true; make it false): ${"
        + items_var + "}. " + HALT_GAP_RULE + " "
        "The ONLY paths you may edit: ${" + footprint_var + "}. "
        "Reproduce first with each item's checks. "
        "Complete the work within the footprint and return DONE with every path written. "
        "Return BLOCKED with the reason when it needs a path outside the footprint. "
        "Commit nothing. Never edit a test to delete its assertion. "
        "Save captures under `scratch/remnant-complete/<run-id>/`."
    )


def _in_footprint(path: str, footprint_entries: Iterable[str]) -> bool:
    for f in footprint_entries:
        if f.endswith("/"):
            if path.startswith(f):
                return True
        elif path == f:
            return True
    return False


def remnant_breach(remnant: Any) -> list[str]:
    """Repo-relative paths a recorded remnant attempt wrote outside its recorded footprint.

    `remnant` is the `recovery.json` `remnant` object: its `paths` (what the leg reported writing
    outside the footprint) checked again against its `footprint` (entries ending `/` are
    prefixes), so a record whose own status was edited to hide the breach still names it. Absolute,
    drive-lettered and `..` paths are always a breach. Trap: this sees only paths the record
    carries; a write the leg never reported is not attributable without a pre-leg baseline.
    """
    if not isinstance(remnant, Mapping):
        return []
    fp = [_posix(p) for p in _text_list(remnant.get("footprint"))]
    out: list[str] = []
    for p in (_posix(x) for x in _text_list(remnant.get("paths"))):
        unsafe = p.startswith("/") or ".." in p.split("/") or re.match(r"^[A-Za-z]:", p) is not None
        if (unsafe or not _in_footprint(p, fp)) and p not in out:
            out.append(p)
    return out


def footprint(chunk_row: Mapping[str, Any], failed_tests: Iterable[str]) -> list[str]:
    """The chunk's declared `writes` plus the failing test files it owns, POSIX, de-duplicated."""
    out: list[str] = []
    for p in _writes(chunk_row) + [f for f in _failed_files(failed_tests) if _owns(chunk_row, f)]:
        if p not in out:
            out.append(p)
    return out


def _status_of(entry: Any) -> str:
    return str(entry.get("status")) if isinstance(entry, Mapping) else str(entry)


def landing_withheld(reopen_status, manifest_rows, import_edges) -> dict:
    """`{chunk: reason}` of chunks a commit leaves out.

    `reopen_status` maps chunk -> status (or a `recovery.json` entry). Exhausted, blocked and held
    chunks are withheld with their reason; every other finished chunk whose `.py` writes import a
    path a withheld chunk owns (its `writes` or recorded `paths`) is withheld `import-closure-open`,
    transitively. `import_edges` is `{importer: [imported]}` or an iterable of (importer, imported).
    """
    rows = [r for r in manifest_rows or () if isinstance(r, Mapping)]
    withheld: dict[str, str] = {}
    owned: dict[str, set[str]] = {}
    by_id = {str(r.get("id")): r for r in rows}
    for cid, entry in (reopen_status or {}).items():
        reason = STATUS_REASON.get(_status_of(entry))
        if reason is None:
            continue
        withheld[str(cid)] = reason
        paths = set(_writes(by_id.get(str(cid), {})))
        if isinstance(entry, Mapping):
            paths |= {_posix(p) for p in entry.get("paths") or ()}
        owned[str(cid)] = paths
    pairs: list[tuple[str, str]] = []
    if isinstance(import_edges, Mapping):
        pairs = [(_posix(a), _posix(b)) for a, targets in import_edges.items() for b in targets or ()]
    else:
        pairs = [(_posix(a), _posix(b)) for a, b in import_edges or ()]
    changed = True
    while changed:
        changed = False
        reach = set().union(*owned.values()) if owned else set()
        for row in rows:
            cid = str(row.get("id"))
            if cid in withheld:
                continue
            py = {p for p in _writes(row) if p.endswith(".py")}
            if any(a in py and b in reach for a, b in pairs):
                withheld[cid] = IMPORT_CLOSURE_OPEN
                owned[cid] = set(_writes(row))
                changed = True
    return withheld


REOPEN_PLAN_JS = r"""function _reopenPlan(items, manifestRows, failedTests, heldIds) {
  const posix = (p) => { let s = String(p).replace(/\\/g, '/'); while (s.startsWith('./')) s = s.slice(2); return s; };
  const pathTokens = (t) => (posix(t).match(/[A-Za-z0-9_.\-\/]+/g) ?? []).map((x) => x.replace(/\.+$/, '')).filter(Boolean);
  const idTokens = (t) => (String(t).match(/[A-Za-z0-9_.\-]+/g) ?? []).map((x) => x.replace(/^[.\-]+|[.\-]+$/g, '')).filter(Boolean);
  const rows = (manifestRows ?? []).filter((r) => r && typeof r === 'object');
  const byId = new Set(rows.map((r) => String(r.id)));
  const heldSet = new Set((heldIds ?? []).map(String));
  const failed = (failedTests ?? []).map((t) => posix(String(t).split('::')[0]));
  const writesOf = (r) => (r.writes ?? []).map(posix);
  const owns = (r, p) => writesOf(r).includes(p) || (r.writes_under ?? []).some((d) => p.startsWith(posix(d).replace(/\/+$/, '') + '/'));
  const reopen = {}; const held = {}; const unattributed = [];
  const add = (b, k, v) => { (b[k] = b[k] ?? []); if (!b[k].includes(v)) b[k].push(v); };
  for (const item of (items ?? [])) {
    if (!item || item.status !== 'not_met' || item.essential === false || item.withheld_by || (item.owed_by ?? []).length) continue;
    const rid = String(item.id);
    const text = (Array.isArray(item.remaining) ? item.remaining.join(' ') : String(item.remaining ?? '')) + ' ' + String(item.evidence ?? '');
    let hit = false;
    for (const tok of idTokens(text)) {
      if (byId.has(tok)) { add(reopen, tok, rid); hit = true; }
      else if (heldSet.has(tok)) { add(held, tok, rid); hit = true; }
    }
    const named = [...new Set([...pathTokens(text), ...failed])];
    for (const r of rows) {
      if (named.some((p) => owns(r, p))) { add(reopen, String(r.id), rid); hit = true; }
    }
    if (hit) continue;
    const claimants = rows.filter((r) => (r.register_rows ?? []).map(String).includes(rid)).map((r) => String(r.id));
    if (claimants.length === 1) add(reopen, claimants[0], rid);
    else if (!unattributed.includes(rid)) unattributed.push(rid);
  }
  return { reopen, held, unattributed };
}"""

REMNANT_ITEMS_JS = r"""function _remnantItems(items, unattributed, capped, criterion, delivery) {
  const posix = (p) => { let s = String(p).replace(/\\/g, '/'); while (s.startsWith('./')) s = s.slice(2); return s; };
  const list = (v) => (Array.isArray(v) ? v : (v == null ? [] : [v])).map(String).filter((x) => x.trim());
  const truthy = (v) => (Array.isArray(v) ? v.length > 0 : !!v);
  const openIds = new Set([...(unattributed ?? []), ...(capped ?? [])].map(String));
  const fromRemnant = (v) => (v && typeof v.remnant === 'object' && v.remnant) ? { gap: String(v.remnant.gap ?? ''), paths: list(v.remnant.paths).map(posix), checks: list(v.remnant.checks) } : { gap: '', paths: [], checks: [] };
  const out = [];
  for (const item of (items ?? [])) {
    if (!item || item.status !== 'not_met' || item.essential === false || truthy(item.withheld_by) || truthy(item.owed_by)) continue;
    if (!openIds.has(String(item.id))) continue;
    out.push({ kind: 'ledger', id: String(item.id), gap: list(item.remaining).join(' ') || String(item.evidence ?? ''), paths: [], checks: [] });
  }
  if (criterion && criterion.status === 'not_met') {
    const r = fromRemnant(criterion);
    out.push({ kind: 'criterion', id: 'criterion', gap: r.gap || String(criterion.reason ?? ''), paths: r.paths, checks: r.checks });
  }
  if (delivery && delivery.verdict === 'FAIL') {
    const r = fromRemnant(delivery);
    out.push({ kind: 'delivery', id: 'delivery', gap: r.gap || String(delivery.reason ?? delivery.summary ?? ''), paths: r.paths, checks: r.checks });
  }
  return out;
}"""
