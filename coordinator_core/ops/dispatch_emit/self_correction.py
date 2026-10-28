"""In-run halt self-correction: shared cap, fix-leg contract, red-ownership split, progress rule.

Pure and stdlib-only. The `*_JS` strings mirror the Python reference and are spliced into the
composed ask script; the parity test pins both to one table of cases.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, NamedTuple

from coordinator_core.ops.dispatch_emit.ask_contract import HALT_GAP_RULE
from coordinator_core.ops.dispatch_emit.ledger_recovery import RECOVERY_ATTEMPT_CAP

#: One cap for the review loop and the ledger loop.
SELF_CORRECTION_CAP = RECOVERY_ATTEMPT_CAP

HALT_FIX_LABEL = "halt-fix"
HALT_REVERIFY_LABEL = "halt-reverify"
HALT_FIX_PHASE = "halt-fix"

#: Key on the review re-verify record listing node ids the run does not own.
FOREIGN_FIELD = "foreign"
#: Halt key: {phase: review|ledger, attempts, status: fixed|exhausted|no-progress|scope|blocked,
#: gaps, needs_paths, foreign}.
SELF_CORRECTION_FIELD = "self_correction"

NO_PROGRESS = "self-correction-no-progress"

HALT_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["DONE", "BLOCKED", "SCOPE"]},
        "paths": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "needs_paths": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "paths", "reason", "needs_paths"],
    "additionalProperties": False,
}

_STR_LIST = {"type": "array", "items": {"type": "string"}}

HALT_REVERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "test": {
            "type": "object",
            "properties": {
                "status_after": {"type": "string"},
                "failing": _STR_LIST,
                "failing_detail": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"node_id": {"type": "string"}, "message": {"type": "string"}},
                        "required": ["node_id", "message"],
                        "additionalProperties": False,
                    },
                },
                "pre_existing": _STR_LIST,
                "passed_alone": _STR_LIST,
                "passed_at_base": _STR_LIST,
                "reverted": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"hunk": {"type": "string"}, "tests": _STR_LIST},
                        "required": ["tests"],
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["status_after", "failing", "failing_detail", "pre_existing", "reverted"],
            "additionalProperties": False,
        },
        "slices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slice_id": {"type": "string"},
                    "findings": _STR_LIST,
                    "applied": {"type": "boolean"},
                },
                "required": ["slice_id", "findings", "applied"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["test", "slices"],
    "additionalProperties": False,
}


def notes_sentence(notes_path: str) -> str:
    """The revise-notes sentence for the two fix prompts: halt_fix_prompt here and ledger_recovery.ledger_fix_prompt."""
    return (
        f"The EM's revise notes for this run are in `{notes_path}` (one JSON object per line; "
        "skip retracted ones): read them first and treat each live note as a requirement."
    )


def halt_fix_prompt(gaps_var: str, slices_var: str, footprint_var: str, notes_path: str) -> str:
    """Instruction tail for the halt-fix leg.

    The three `_var` args are JS expressions spliced into a template literal; `notes_path` is literal text.
    """
    return (
        "The run is about to halt on gaps it can close itself. Close each one (each is still true; "
        "make it false): the failing test node ids with their first message line: ${"
        + gaps_var + "}; and the blocked slices' sidecar findings: ${" + slices_var + "}. "
        + HALT_GAP_RULE + " "
        "The ONLY paths you may edit: ${" + footprint_var + "}. "
        "Reproduce first with `python -m pytest <failing> -q`. "
        "Fix within the footprint and return DONE with every path written. "
        "Return SCOPE with `needs_paths` naming each path, rather than write outside the footprint, "
        "when closing a gap needs a path outside it or a direction call. "
        "Return BLOCKED with the reason when the failure is not caused by this run's change. "
        "Never resolve a finding by deleting the requirement or reference it names, and never edit a "
        "test to delete its assertion. "
        + notes_sentence(notes_path) + " "
        "Commit nothing. Save captures under `scratch/halt-fix/<run-id>/`."
    )


def _posix(p: Any) -> str:
    s = str(p).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _file_of(node_id: Any) -> str:
    return _posix(node_id).split("::", 1)[0]


class RedSplit(NamedTuple):
    owned: tuple
    foreign: tuple
    unverified: tuple  # subset of `foreign`: outside the write set with no evidence either way


def classify_reds(
    failing: Iterable[str],
    own_paths: Iterable[str],
    *,
    caused: Iterable[str] | None = None,
    foreign: Iterable[str] | None = None,
    pre_existing: Iterable[str] | None = None,
    reverted: Iterable[Mapping[str, Any]] | None = None,
) -> RedSplit:
    """Split failing node ids into the run's own and foreign reds.

    Owned: the id's file is in `own_paths`, or the id is in `reverted[].tests`, or it is in `caused`
    and not in `foreign`. Everything else is foreign; `unverified` are the foreign ids that no list
    proves foreign. With `caused` and `reverted` both None only write-set membership counts.
    """
    own = {_posix(p) for p in own_paths or ()}
    caused_s = None if caused is None else {_posix(i) for i in caused}
    foreign_s = {_posix(i) for i in foreign or ()}
    pre_s = {_posix(i) for i in pre_existing or ()}
    reverted_s = None if reverted is None else {_posix(t) for r in reverted for t in (r.get("tests") or [])}
    owned, foreign_out, unverified = set(), set(), set()
    for raw in failing or ():
        nid = _posix(raw)
        if _file_of(nid) in own or (reverted_s is not None and nid in reverted_s):
            owned.add(nid)
        elif caused_s is not None and nid in caused_s and nid not in foreign_s:
            owned.add(nid)
        else:
            foreign_out.add(nid)
            if nid not in foreign_s and nid not in pre_s:
                unverified.add(nid)
    return RedSplit(tuple(sorted(owned)), tuple(sorted(foreign_out)), tuple(sorted(unverified)))


def classify_flakes(test: Mapping[str, Any], run_id: str) -> tuple[list[str], list[dict]]:
    """Split `test["failing"]` into (remaining_failing, flakes).

    A flake is a failing id present in both `passed_alone` and `passed_at_base`; absent or
    non-list evidence never makes one. Ids are normalised as classify_reds does.
    """
    def ids(key: str) -> list[str]:
        v = test.get(key)
        return [_posix(i) for i in v] if isinstance(v, list) else []

    alone, base = set(ids("passed_alone")), set(ids("passed_at_base"))
    detail = test.get("failing_detail")
    first: dict[str, str] = {}
    for d in detail if isinstance(detail, list) else []:
        if isinstance(d, Mapping) and d.get("node_id") is not None:
            first.setdefault(_posix(d["node_id"]), str(d.get("message") or "").split("\n", 1)[0])
    remaining: list[str] = []
    flakes: list[dict] = []
    for nid in ids("failing"):
        if nid in alone and nid in base:
            head = first.get(nid, "")
            flakes.append({
                "node_id": nid,
                "run": run_id,
                "evidence": (head + "; " if head else "") + "passed alone; passed at base",
            })
        else:
            remaining.append(nid)
    return remaining, flakes


def progressed(before: Iterable[str], after: Iterable[str]) -> bool:
    """True iff at least one gap key in `before` is absent from `after`."""
    after_s = set(after or ())
    return any(k not in after_s for k in (before or ()))


CLASSIFY_REDS_JS = r"""
const classifyReds = (failing, ownPaths, opts) => {
  const o = opts || {};
  const px = (p) => { let s = String(p).replace(/\\/g, '/'); while (s.startsWith('./')) s = s.slice(2); return s; };
  const own = new Set((ownPaths || []).map(px));
  const causedS = o.caused == null ? null : new Set(o.caused.map(px));
  const foreignS = new Set((o.foreign || []).map(px));
  const preS = new Set((o.pre_existing || []).map(px));
  const revertedS = o.reverted == null ? null : new Set(o.reverted.flatMap((r) => (r.tests || []).map(px)));
  const owned = new Set(), foreign = new Set(), unverified = new Set();
  for (const raw of failing || []) {
    const id = px(raw);
    if (own.has(id.split('::')[0]) || (revertedS !== null && revertedS.has(id))) owned.add(id);
    else if (causedS !== null && causedS.has(id) && !foreignS.has(id)) owned.add(id);
    else {
      foreign.add(id);
      if (!foreignS.has(id) && !preS.has(id)) unverified.add(id);
    }
  }
  const srt = (s) => [...s].sort();
  return { owned: srt(owned), foreign: srt(foreign), unverified: srt(unverified) };
};
"""

CLASSIFY_FLAKES_JS = r"""
const classifyFlakes = (rv, runId) => {
  const t = rv || {};
  const px = (p) => { let s = String(p).replace(/\\/g, '/'); while (s.startsWith('./')) s = s.slice(2); return s; };
  const ids = (k) => (Array.isArray(t[k]) ? t[k].map(px) : []);
  const alone = new Set(ids('passed_alone')), base = new Set(ids('passed_at_base'));
  const first = new Map();
  for (const d of Array.isArray(t.failing_detail) ? t.failing_detail : []) {
    if (d && typeof d === 'object' && d.node_id != null && !first.has(px(d.node_id))) {
      first.set(px(d.node_id), String(d.message || '').split('\n')[0]);
    }
  }
  const remaining = [], flakes = [];
  for (const id of ids('failing')) {
    if (alone.has(id) && base.has(id)) {
      const head = first.get(id) || '';
      flakes.push({ node_id: id, run: runId, evidence: (head ? head + '; ' : '') + 'passed alone; passed at base' });
    } else remaining.push(id);
  }
  return [remaining, flakes];
};
"""

PROGRESSED_JS = r"""
const progressed = (before, after) => {
  const a = new Set(after || []);
  return (before || []).some((k) => !a.has(k));
};
"""
