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

PROGRESSED_JS = r"""
const progressed = (before, after) => {
  const a = new Set(after || []);
  return (before || []).some((k) => !a.has(k));
};
"""
