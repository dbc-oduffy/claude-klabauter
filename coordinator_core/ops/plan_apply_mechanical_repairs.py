"""
coordinator_core.ops.plan_apply_mechanical_repairs — JSON-RPC "plan.apply_mechanical_repairs".

Purpose: applies a `plan.readiness_verdict` reply's `mechanical_repairs` to ONE plan in place, in a
single `locked_rmw`, so a plan pulled only for mechanically repairable findings reaches `ready`.

Repair kinds (entries are `{kind, target, instruction}`):
  - premise-citation-corrected: every occurrence of the old citation (`target`, or `old`) becomes
    the new one (`new`, or the tail of `instruction` "replace <old> with <new>"), body and spine.
  - register-pin-missing / register-rows-unclaimed: omitted sizing rows join `register_claims.rows`
    (rr.claim_rows_plan_text) and every claimed row gains its pin (rr.repin_plan_text).
  - uncheckable-to-falsifier-row: an execution-time falsifier row is appended to the plan-tasks
    spine (`target` is the anchor id, `instruction` its claim).

Invariants:
  - an unknown kind, or a repair whose edit cannot be made, is returned in `skipped` with a
    `reason` and never applied; the other repairs still land.
  - a register repair is skipped when a claimed pin has drifted: re-pinning is `plan.repin_register`.
  - every other byte is kept, CRLF included; nothing applicable -> bytes untouched.

Negative-spec: does NOT judge the plan, does NOT edit the sizing, does NOT validate the plan
schema, and does NOT spawn a process or git-commit.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops._premise_repair import (
    REPAIR_CITATION,
    REPAIR_FALSIFIER,
    REPAIR_REGISTER_PIN,
    REPAIR_REGISTER_UNCLAIMED,
    falsifier_task,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.plan_repin_register import _frontmatter
from coordinator_core.session import record_homes

_PARAMS = frozenset({"plan", "repairs"})
_PARAMS_HINT = (
    "params: plan (required, repo-relative path under docs/plans/), repairs (required: "
    "a plan.readiness_verdict reply's mechanical_repairs, [{kind, target, instruction}])"
)
_PLANS_REL = ("docs", "plans")
_REGISTER_KINDS = frozenset({REPAIR_REGISTER_PIN, REPAIR_REGISTER_UNCLAIMED})
_SPINE_OPEN = re.compile(r"^```yaml\s+plan-tasks\s*$")


def _err(msg: str) -> dict:
    return {"exit_code": 1, "ok": False, "applied": [], "skipped": [], "error": msg, "message": msg}


class _Skip(Exception):
    """One repair cannot be made; the others still land."""


def _citation_pair(repair: dict) -> tuple[str, str]:
    old = str(repair.get("old") or repair.get("target") or "").strip()
    new = repair.get("new")
    if not isinstance(new, str) or not new.strip():
        instruction = str(repair.get("instruction") or "")
        prefix = f"replace {old} with "
        new = instruction[len(prefix):] if old and instruction.startswith(prefix) else ""
    new = new.strip()
    if not old or not new:
        raise _Skip("no old/new citation pair (need target and 'replace <old> with <new>')")
    return old, new


def _fix_citation(text: str, repair: dict) -> str:
    old, new = _citation_pair(repair)
    pattern = re.compile(r"(?<![\w./\\-])" + re.escape(old) + r"(?![\w/\\-]|\.\w)")
    out, count = pattern.subn(lambda _m: new, text)
    if not count:
        raise _Skip(f"old citation not found in the plan: {old}")
    return out


def _fix_register(text: str, worktree: Path) -> str:
    fm = _frontmatter(text) or ""
    sizing_rel, _ids = rr.plan_claims(fm)
    if not sizing_rel:
        raise _Skip("plan carries no register_claims.sizing")
    sizing = contained_path(
        worktree / sizing_rel, [Path(record_homes.home_dir(str(worktree), "sizings"))]
    )
    if sizing is None:
        raise _Skip(f"register_claims.sizing escapes the sizings home: {sizing_rel}")
    try:
        register = rr.read_register(sizing.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise _Skip(f"sizing-object unreadable: {sizing_rel}") from None
    if register is None:
        raise _Skip(f"{sizing_rel} carries no requirement_register")
    drift = rr.pin_drift(register, rr.plan_pins(fm))
    if drift:
        raise _Skip(
            f"register row {drift[0]['id']} pin has drifted: re-pin it with plan.repin_register"
        )
    try:
        claimed, _added = rr.claim_rows_plan_text(text, register)
        pinned, _changed = rr.repin_plan_text(claimed, register)
    except ValueError as exc:
        raise _Skip(str(exc)) from exc
    return pinned


def _spine_span(lines: list[str]) -> Optional[tuple[int, int]]:
    start = next((i for i, ln in enumerate(lines) if _SPINE_OPEN.match(ln.rstrip())), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].rstrip() == "```"), None)
    return None if end is None else (start + 1, end)


def _add_falsifier(text: str, repair: dict) -> str:
    anchor = str(repair.get("target") or "").strip()
    claim = str(repair.get("instruction") or "").strip()
    if not anchor or not claim:
        raise _Skip("falsifier repair needs target (anchor id) and instruction (claim)")
    lines = text.split("\n")
    span = _spine_span(lines)
    if span is None:
        raise _Skip("plan carries no ```yaml plan-tasks block")
    start, end = span
    try:
        rows = yaml.safe_load("\n".join(lines[start:end])) or []
    except yaml.YAMLError as exc:
        raise _Skip(f"plan-tasks block does not parse: {exc}") from exc
    if not isinstance(rows, list):
        raise _Skip("plan-tasks block is not a list")
    title = falsifier_task(anchor, claim, "")["title"]
    if any(isinstance(r, dict) and r.get("title") == title for r in rows):
        raise _Skip(f"falsifier row for premise {anchor} already present")
    taken = {str(r.get("id")) for r in rows if isinstance(r, dict)}
    n = 1
    while f"F{n}" in taken:
        n += 1
    _sizing, claimed_ids = rr.plan_claims(_frontmatter(text) or "")
    dumped = yaml.safe_dump(
        [falsifier_task(anchor, claim, f"F{n}", list(claimed_ids)[:1])],
        default_flow_style=False, sort_keys=False, allow_unicode=True, width=10**6,
    ).rstrip("\n")
    first = next((ln for ln in lines[start:end] if ln.strip()), "")
    pad = " " * (len(first) - len(first.lstrip(" ")))
    block = [pad + ln for ln in dumped.split("\n")]
    at = end
    while at > start and not lines[at - 1].strip():
        at -= 1
    lines[at:at] = block
    return "\n".join(lines)


def _apply(text: str, repair: Any, worktree: Path) -> str:
    if not isinstance(repair, dict):
        raise _Skip("repair is not an object")
    kind = repair.get("kind")
    if kind == REPAIR_CITATION:
        return _fix_citation(text, repair)
    if kind in _REGISTER_KINDS:
        fixed = _fix_register(text, worktree)
        if fixed == text:
            raise _Skip("register claims and pins already complete")
        return fixed
    if kind == REPAIR_FALSIFIER:
        return _add_falsifier(text, repair)
    raise _Skip(f"unknown repair kind: {kind!r}")


@register_op("plan.apply_mechanical_repairs")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.apply_mechanical_repairs" handler.

    Plain `def`, deliberately not `async def`: the steps are blocking (path stats,
    `locked_rmw`), so an `async def` would make DISPATCH_TIMEOUT_SECS unenforceable.

    Params:
        plan    (str)  — repo-relative path under docs/plans/. Required.
        repairs (list) — a readiness reply's `mechanical_repairs`. Required.

    Returns: {exit_code, ok, plan, applied: [{kind, target}], skipped: [{kind, target, reason}],
              message|error}.

    Exit-code contract:
        exit_code 1 — an unknown or missing param; a plan outside docs/plans, absent or
                      unreadable; lock timeout.
        exit_code 0 — the plan was processed: `applied` landed, `skipped` did not.
    """
    unknown = sorted(set(params) - _PARAMS)
    if unknown:
        return _err(f"unknown param(s): {', '.join(unknown)} — {_PARAMS_HINT}")
    plan_raw = params.get("plan")
    plan_raw = plan_raw.strip() if isinstance(plan_raw, str) else ""
    if not plan_raw:
        return _err(f"missing required param: plan — {_PARAMS_HINT}")
    repairs = params.get("repairs")
    if not isinstance(repairs, list):
        return _err(f"missing required param: repairs (a list) — {_PARAMS_HINT}")
    if repo_root is None:
        return _err(
            "plan.apply_mechanical_repairs: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)
    p = Path(plan_raw)
    if not p.is_absolute():
        p = worktree / p
    p = contained_path(p, [worktree.joinpath(*_PLANS_REL)])
    if p is None:
        return _err(f"plan escapes docs/plans: {plan_raw!r}")
    if not p.is_file():
        return _err(f"plan not found on disk: {plan_raw}")
    plan_rel = p.relative_to(worktree.resolve()).as_posix()
    try:
        crlf = b"\r\n" in p.read_bytes()
    except OSError as exc:
        return _err(f"plan unreadable: {exc}")

    state: dict = {"applied": [], "skipped": []}

    def mutate(old_text: str) -> str:
        applied: list[dict] = []
        skipped: list[dict] = []
        text = old_text.replace("\r\n", "\n")
        for repair in repairs:
            ref = {
                "kind": repair.get("kind") if isinstance(repair, dict) else None,
                "target": repair.get("target") if isinstance(repair, dict) else None,
            }
            try:
                text = _apply(text, repair, worktree)
            except _Skip as exc:
                skipped.append({**ref, "reason": str(exc)})
            else:
                applied.append(ref)
        state["applied"], state["skipped"] = applied, skipped
        if text == old_text.replace("\r\n", "\n"):
            return old_text
        return text.replace("\n", "\r\n") if crlf else text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"plan disappeared before the lock could be acquired: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "apply_mechanical_repairs: mutation aborted")
    except Exception as exc:  # noqa: BLE001
        return _err(f"apply_mechanical_repairs: {type(exc).__name__}: {exc}")

    applied, skipped = state["applied"], state["skipped"]
    return {
        "exit_code": 0,
        "ok": True,
        "plan": plan_rel,
        "applied": applied,
        "skipped": skipped,
        "message": f"{plan_rel}: applied {len(applied)}, skipped {len(skipped)}",
    }
