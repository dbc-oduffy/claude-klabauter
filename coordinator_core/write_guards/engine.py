"""coordinator_core.write_guards.engine — runner for the PreToolUse Write/Edit guards.

Discovers per-guard modules in this package (each exposing CLASS / MATCHERS /
PRIORITY / check(payload) per INTERFACE.md) and evaluates a PreToolUse payload:

  * hard-deny guards run first, in PRIORITY order (lower first); the first
    non-None return wins → that deny envelope is emitted, evaluation stops —
    UNLESS a one-shot operator unlock is on file for this
    ``(session_id, guard_name)`` pair (`coordinator_core.session.
    guard_unlock_sentinel`), in which case that guard's deny is skipped and
    the loop continues to the next hard-deny guard. An unresolvable
    session_id never grants — fail closed. Every hard-deny that IS emitted
    (unlock absent or session_id unresolvable) has the in-session unlock
    line appended to its `permissionDecisionReason` here, at this single
    seam, before it is returned — see `guard_unlock_sentinel.annotate_deny`
    (docs/plans/2026-08-03-in-session-operator-unlock-for-the-hard-.md § C4).
    A guard added tomorrow inherits the line for free; no per-guard edit.
  * if no hard-deny fired, advisory guards run in PRIORITY order. By default
    (`aggregate=False`, every existing call site) the FIRST non-None advisory
    wins (a PreToolUse response carries at most one envelope) and evaluation
    stops there — byte-identical to pre-C6 behaviour. An opt-in
    `aggregate=True` (docs/plans/2026-08-06-windows-hot-path-less-work-per-
    interpreter.md chunk C6) instead runs every applicable advisory and
    returns the full list of firing envelopes, in PRIORITY order; see
    `evaluate`'s own docstring for the full contract and the resulting
    change in `_record_advisory_fire` call arity.

Every guard's own failure is contained (a raising check is treated as no-op
ALLOW for advisories; hard guards that intend to fail-closed do so inside their
own check() by returning a deny — the engine never fabricates a deny on a guard
crash, matching the coordinator-claude dispatcher's per-check isolation).

Stateless / spawn-per-call (DR-215). stdin-based (no argv params) so it is not
exposed to the Windows ARG_MAX / fcntl regressions that affect argv/locked_rmw
paths.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.session.guard_unlock_sentinel import (
    annotate_deny as _annotate_unlock,
    consume as _consume_unlock,
)
from coordinator_core.bash_guards._helpers import (
    resolve_override_keys_doc_display as _override_keys_doc_display,
)
from coordinator_core.guard_advisory_counter import (
    record_advisory_fire as _record_advisory_fire,
    record_deny_fire as _record_deny_fire,
)

_PKG_NAME = __name__.rsplit(".", 1)[0]  # "coordinator_core.write_guards"
_PKG_DIR = str(Path(__file__).resolve().parent)

# NOTE (DR-054-adjacent, 2026-07-15): the subagent write-sandbox confinement
# (coordinator_core.subagent_sandbox) was removed from this engine — it
# surprised EMs and dispatched subagents by hard-denying legitimate writes
# outside a narrow sandbox. The `policy_path` param below is retained (accepted
# but unused) so the coordinator-claude dispatcher needs no change. Other write guards
# (illegal-filename, tracker-edit, consumed-handoff, etc.) are unaffected.

_VALID_CLASSES = {"hard-deny", "advisory"}
_VALID_MATCHERS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


class _Guard:
    __slots__ = ("name", "cls", "matchers", "priority", "check")

    def __init__(self, name: str, cls: str, matchers: List[str], priority: int,
                 check: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> None:
        self.name = name
        self.cls = cls
        self.matchers = matchers
        self.priority = priority
        self.check = check


def _discover_guards() -> Tuple[List[_Guard], List[str]]:
    """Import every sibling module exposing the guard interface; skip the rest.

    Returns `(guards, import_failed)`. `import_failed` names modules that
    RAISED on import — the anomaly this return exists to surface (see
    `discover_guard_names()` below). Modules that are intentionally not guards
    (`engine`, `__main__`, underscore-prefixed shared helpers) or that import
    cleanly but lack a valid CLASS/MATCHERS/check are NOT included in
    `import_failed` — those are by-design filtering, not a failure.
    """
    guards: List[_Guard] = []
    import_failed: List[str] = []
    for mod_info in pkgutil.iter_modules([_PKG_DIR]):
        name = mod_info.name
        if name in ("engine", "__main__") or name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{_PKG_NAME}.{name}")
        except Exception:
            # A broken guard module must never take the whole engine down —
            # it is simply absent (fail-open for that guard), matching the coordinator-claude
            # dispatcher's source-time isolation. Its name is still collected
            # so callers can surface the omission instead of it being silent.
            import_failed.append(name)
            continue
        cls = getattr(mod, "CLASS", None)
        matchers = getattr(mod, "MATCHERS", None)
        check = getattr(mod, "check", None)
        priority = getattr(mod, "PRIORITY", 100)
        if cls not in _VALID_CLASSES or not callable(check):
            continue
        if not isinstance(matchers, (list, tuple)):
            continue
        matchers = [m for m in matchers if m in _VALID_MATCHERS]
        if not matchers:
            continue
        guards.append(_Guard(name, cls, list(matchers), int(priority), check))
    return guards, import_failed


def discover_guard_names() -> Tuple[List[str], List[str]]:
    """Public, check()-free introspection: `(loaded_guard_names, import_failed_names)`.

    Runtime-visible half of the silent-omission fix (docs/plans/2026-07-29-
    hook-fan-in-write-path.md C12): a guard module that fails to import is no
    longer just absent — its name comes back in `import_failed` so a caller
    (the coordinator-claude PreToolUse dispatcher) can emit an operator-visible signal instead
    of the omission being silent. Pairs with
    `write_guards/tests/test_guard_registry_manifest.py`, the CI-visible half
    (a manifest mismatch fails a test instead of vanishing quietly) — that test
    only catches import failures reproducible in CI; an environment-dependent
    one (unresolvable claude-klabauter root, missing dependency, a Windows-only import
    quirk on one operator's machine) is exactly what this runtime signal is for.
    """
    guards, import_failed = _discover_guards()
    return [g.name for g in guards], import_failed


def _run_check(guard: _Guard, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        return guard.check(payload)
    except Exception:
        # Advisory crash → no-op. A hard guard that wants fail-closed encodes
        # that inside check() by returning a deny; the engine does not invent one.
        return None


def evaluate(
    payload: Dict[str, Any],
    *,
    policy_path: Optional[str] = None,
    cwd: Optional[str] = None,
    skipped_out: Optional[List[str]] = None,
    aggregate: bool = False,
) -> Optional[Dict[str, Any] | List[Dict[str, Any]]]:
    """Evaluate all guards; return the single hookSpecificOutput dict, or None (ALLOW).

    `skipped_out`, if given a list, is populated in-place with the names of
    guard modules that raised on import during THIS call's discovery pass —
    lets a caller (the coordinator-claude dispatcher) get the same signal
    `discover_guard_names()` would return without triggering a second,
    independent `_discover_guards()` pass (a duplicated directory-scan +
    import-probe over all guard modules) on every write.

    `_discover_guards()` runs fresh on every call within a process (no
    memoization here — see `docs/plans/2026-08-06-windows-hot-path-less-
    work-per-interpreter.md` chunk C5, deliberately `wont_do`); a repeat
    caller wanting to avoid re-discovery should memoize its own result.

    AGGREGATION (opt-in, `docs/plans/2026-08-06-windows-hot-path-less-work-
    per-interpreter.md` chunk C6): pass `aggregate=True` to get EVERY
    matching advisory back, in PRIORITY order, as a `List[Dict[str, Any]]`
    (empty list if none fired) instead of the legacy single-envelope
    shape. Every existing call site omits `aggregate` and keeps today's
    behaviour byte-identical: `Optional[Dict[str, Any]]`, the FIRST
    non-None advisory, evaluation stopping there. A keyword-only parameter
    sharing this one implementation was chosen over a sibling function so
    the hard-deny phase, discovery, and matcher-filtering logic have exactly
    one copy to keep in sync — a sibling function would either duplicate
    that phase or import back into this one for no benefit.

    The hard-deny phase is UNCHANGED by `aggregate` and still short-circuits
    before any advisory runs, in both shapes.

    Unlock/consume: `_consume_unlock` (see `coordinator_core.session.
    guard_unlock_sentinel`) is used ONLY by the hard-deny phase above, both
    before and after this change. Advisories have no unlock/consume path at
    all — nothing here invents one for the aggregate shape either.

    Per-guard bookkeeping (`_record_advisory_fire`) runs once per advisory
    RETURNED, in both shapes — under `aggregate=True` that means once per
    entry in the returned list, not once per call. This is a deliberate
    arity change from the pre-C6 counter (which fired at most once per
    `evaluate()` call, matching the old at-most-one-advisory contract); see
    `coordinator_core.guard_advisory_counter` for the record shape. The
    counter's own record does not carry an arity-distinguishing field (out
    of this chunk's write scope, `coordinator_core/write_guards/engine.py`
    only) — this docstring paragraph is the distinguishability note: a
    reader of `advisory-fire-counts.jsonl` should treat any session whose
    records were produced by a caller passing `aggregate=True` as
    potentially having more than one record per triggering `evaluate()`
    call, which is an arity change in the counter's calling convention, not
    a behavioural change in what counts as an advisory firing.
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _VALID_MATCHERS:
        return [] if aggregate else None

    guards, import_failed = _discover_guards()
    if skipped_out is not None:
        skipped_out[:] = import_failed
    applicable = [g for g in guards if tool_name in g.matchers]

    # --- hard-deny phase (first non-None wins), PRIORITY order ---------------
    hard = sorted(
        (g for g in applicable if g.cls == "hard-deny"),
        key=lambda g: g.priority,
    )
    for g in hard:
        out = _run_check(g, payload)
        if out is not None:
            session_id = payload.get("session_id") or ""
            if session_id and _consume_unlock(session_id, g.name):
                try:
                    _record_deny_fire(g.name, session_id, True, payload.get("cwd"))
                except Exception:
                    pass
                continue
            try:
                _record_deny_fire(g.name, session_id, False, payload.get("cwd"))
            except Exception:
                pass
            agent_id = payload.get("agent_id") or ""
            return _annotate_unlock(
                out,
                session_id,
                g.name,
                _override_keys_doc_display(),
                agent_id=agent_id,
            )

    # --- advisory phase, PRIORITY order ---------------------------------------
    # Legacy shape (aggregate=False): first non-None wins, at most one.
    # Aggregate shape (aggregate=True): every non-None advisory is collected
    # and returned, still in PRIORITY order.
    advisories = sorted(
        (g for g in applicable if g.cls == "advisory"),
        key=lambda g: g.priority,
    )
    if aggregate:
        fired: List[Dict[str, Any]] = []
        for g in advisories:
            out = _run_check(g, payload)
            if out is not None:
                try:
                    _record_advisory_fire(g.name, payload.get("session_id") or "", payload.get("cwd"))
                except Exception:
                    pass
                fired.append(out)
        return fired

    for g in advisories:
        out = _run_check(g, payload)
        if out is not None:
            try:
                _record_advisory_fire(g.name, payload.get("session_id") or "", payload.get("cwd"))
            except Exception:
                pass
            return out

    return None


def evaluate_payload_json(
    payload_text: str,
    *,
    policy_path: Optional[str] = None,
    cwd: Optional[str] = None,
    skipped_out: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Parse a PreToolUse JSON payload string and evaluate. Unparseable → ALLOW (None)."""
    try:
        payload = json.loads(payload_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return evaluate(payload, policy_path=policy_path, cwd=cwd, skipped_out=skipped_out)
