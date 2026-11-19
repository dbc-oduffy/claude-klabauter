"""coordinator_core.ops.dispatch_emit.em_test_criterion -- a run whose only test obligation is an
EM-performed UE automation criterion.

INVARIANT: the declaration is explicit. A plan row that is em-performed (`em_only_act.row_is_em_performed`,
the single em-performance predicate) carries `test_criterion: {kind: ue-automation, tests: [...]}`;
an empty or failing collection never implies it.

TRAP: `row_is_em_performed` is False for a `code_deliverable` row on its declared-field legs, so a
row that writes code and carries `test_criterion` is ignored here.

Pure Python: pathlib and json only, no spawn.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional

from coordinator_core.ops.dispatch_emit.em_only_act import row_is_em_performed

EM_OWED_TEST_CRITERION_FIELD = "em_owed_test_criterion"
UE_AUTOMATION_KIND = "ue-automation"
_PASSED_STATE = "Success"


def declared_test_criterion(rows: Iterable[Any]) -> Optional[Dict[str, Any]]:
    """``{row, kind, tests}`` for the first em-performed row declaring a ``ue-automation``
    ``test_criterion``, else None. ``row`` is the declaring row's id."""
    for raw in rows or ():
        if not isinstance(raw, dict):
            continue
        crit = raw.get("test_criterion")
        if not isinstance(crit, dict) or crit.get("kind") != UE_AUTOMATION_KIND:
            continue
        if not row_is_em_performed(raw):
            continue
        tests = crit.get("tests")
        names = [t for t in tests if isinstance(t, str)] if isinstance(tests, list) else []
        return {"row": raw.get("id"), "kind": UE_AUTOMATION_KIND, "tests": names}
    return None


def em_owed_test_criterion(rows: Iterable[Any], written_paths: Iterable[str]) -> Optional[Dict[str, Any]]:
    """``declared_test_criterion`` when no written path is a ``.py`` file, else None: a Python change
    keeps the ``tests_not_run`` halt."""
    for p in written_paths or ():
        if isinstance(p, str) and PurePosixPath(p.replace("\\", "/")).suffix.lower() == ".py":
            return None
    return declared_test_criterion(rows)


def parse_ue_automation_report(index_json: Path, tests: List[str]) -> Dict[str, Any]:
    """Check each of ``tests`` passed in a UE automation ``report/index.json``.

    Returns ``{ok, missing, failed}``: ``missing`` are names no report entry matches
    (``fullTestPath`` or ``testDisplayName``), ``failed`` are matched entries whose ``state`` is not
    ``Success``. ``ok`` is False with a ``reason`` for an unreadable or malformed file, an empty
    ``tests`` list, or any missing/failed name. Never raises.
    """
    names = [t for t in (tests or ()) if isinstance(t, str)]
    out: Dict[str, Any] = {"ok": False, "missing": list(names), "failed": []}
    if not names:
        out["reason"] = "no test names given"
        return out
    try:
        data = json.loads(Path(index_json).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        out["reason"] = f"report unreadable: {exc.__class__.__name__}"
        return out
    entries = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        out["reason"] = "report has no tests[] list"
        return out
    state_of: Dict[str, str] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        state = str(e.get("state"))
        for key in ("fullTestPath", "testDisplayName"):
            name = e.get(key)
            if isinstance(name, str) and name:
                prior = state_of.get(name)
                state_of[name] = state if prior in (None, _PASSED_STATE) else prior
    out["missing"] = [n for n in names if n not in state_of]
    out["failed"] = [n for n in names if n in state_of and state_of[n] != _PASSED_STATE]
    out["ok"] = not out["missing"] and not out["failed"]
    if not out["ok"]:
        out["reason"] = "named tests missing or not passed"
    return out
