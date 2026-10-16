"""
coordinator_core.ops.plan_repin_register — JSON-RPC "plan.repin_register".

Purpose: the sanctioned re-pin for a `REGISTER:register-pin-drift` halt. Rewrites one plan's
`register_claims.pins` to the current text of the register rows it claims, and only when the
caller hands over a clear premise-check verdict and every claimed row is still on the register.

What it writes: the `register_claims.pins` line of one `docs/plans/*.md` (one `locked_rmw`).
Every other byte is kept, CRLF included. After the write the frontmatter is re-parsed and
`pin_drift` must be empty, else the write is aborted.

Trust: the `premise_check` verdict is caller-supplied input; the op validates its shape and
blocking verdicts (and a ledger when one is named), and cannot prove it came from a real review.

Invariants:
  - a missing, non-clear or unreadable `premise_check` refuses before the plan is read.
  - a claimed row gone from the register refuses: a gone row is re-planned, never re-pinned.
  - pins already matching -> `applied` False, bytes untouched.

Negative-spec: does NOT edit `rows`, `sizing` or any other plan field, never touches the sizing,
does NOT validate the plan schema, does NOT judge whether a changed row's words still fit the
plan (premise-check checks premises, not the requirement text), and does NOT spawn a process
or git-commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import record_homes

_PARAMS = frozenset({"plan", "premise_check"})
_PARAMS_HINT = (
    "params: plan (required, repo-relative path under docs/plans/), premise_check (required: "
    "the regate's premise-check review {verdict[, ledger]} or premise rows {rows: [{verdict}]})"
)
_PLANS_REL = ("docs", "plans")


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg, "message": msg}


def _frontmatter(text: str) -> Optional[str]:
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "\n".join(lines[1:i])
    return None


def _premise_clear(premise: Any, worktree: Path) -> Optional[str]:
    """None when `premise` is a clear premise-check verdict, else the reason it is not."""
    from coordinator_core.ops import plan_readiness_verdict as pvr

    if not isinstance(premise, dict):
        return "premise_check is required: " + _PARAMS_HINT
    rows = premise.get("rows")
    if rows is not None:
        if not isinstance(rows, list) or not rows:
            return "premise_check.rows is empty: no premise-check verdict to rely on"
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("verdict"), str):
                return "premise_check.rows entries must be {verdict, ...}"
        blocking = [r["verdict"] for r in rows if r["verdict"] in pvr._PREMISE_BLOCKING]
        if blocking:
            return f"premise-check is not clear: {len(blocking)} {'/'.join(sorted(set(blocking)))} row(s)"
        return None
    if pvr._clear(premise.get("verdict")) or pvr._ledger_clear(premise, worktree):
        return None
    return f"premise-check verdict {premise.get('verdict')!r} is not clear"


@register_op("plan.repin_register")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.repin_register" handler.

    Plain `def`, deliberately not `async def`: the steps are blocking (path stats,
    `locked_rmw`), so an `async def` would make DISPATCH_TIMEOUT_SECS unenforceable.

    Params:
        plan          (str)    — repo-relative path under docs/plans/. Required.
        premise_check (object) — `{verdict[, ledger]}` or `{rows: [{verdict, ...}]}`. Required.

    Returns: {exit_code, applied, plan, repinned: [{id, pinned, current}], message|error}.

    Exit-code contract:
        exit_code 1 — an unknown or missing param; a non-clear premise_check; a plan outside
                      docs/plans or absent; no register_claims; a sizing outside the sizings
                      home, unreadable or without a register; a claimed row absent from the
                      register; lock timeout.
        exit_code 0, applied True  — the pins were rewritten.
        exit_code 0, applied False — the pins already match; no-op.
    """
    unknown = sorted(set(params) - _PARAMS)
    if unknown:
        return _err(f"unknown param(s): {', '.join(unknown)} — {_PARAMS_HINT}")
    plan_raw = params.get("plan")
    plan_raw = plan_raw.strip() if isinstance(plan_raw, str) else ""
    if not plan_raw:
        return _err(f"missing required param: plan — {_PARAMS_HINT}")
    if params.get("premise_check") is None:
        return _err(f"missing required param: premise_check — {_PARAMS_HINT}")
    if repo_root is None:
        return _err(
            "plan.repin_register: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)
    refusal = _premise_clear(params["premise_check"], worktree)
    if refusal:
        return _err(refusal)

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
        raw = p.read_bytes()
        fm = _frontmatter(raw.decode("utf-8").replace("\r\n", "\n"))
    except (OSError, UnicodeDecodeError) as exc:
        return _err(f"plan unreadable: {exc}")
    crlf = b"\r\n" in raw
    sizing_rel, _ids = rr.plan_claims(fm or "")
    if not sizing_rel:
        return _err(f"{plan_rel} carries no register_claims.sizing")
    sizing = contained_path(
        worktree / sizing_rel, [Path(record_homes.home_dir(str(worktree), "sizings"))]
    )
    if sizing is None:
        return _err(f"register_claims.sizing escapes the sizings home: {sizing_rel}")
    try:
        register = rr.read_register(sizing.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return _err(f"sizing-object unreadable: {sizing_rel}")
    if register is None:
        return _err(f"{sizing_rel} carries no requirement_register")

    state: dict = {"repinned": []}

    def mutate(old_text: str) -> str:
        try:
            new_text, changed = rr.repin_plan_text(old_text, register)
        except ValueError as exc:
            raise MutateAbort(str(exc)) from exc
        state["repinned"] = changed
        if not changed:
            return old_text
        new_fm = _frontmatter(new_text) or ""
        left = rr.pin_drift(register, rr.plan_pins(new_fm))
        if left:
            raise MutateAbort(
                f"repin left drift on {', '.join(d['id'] for d in left)}; plan not written"
            )
        return new_text.replace("\n", "\r\n") if crlf else new_text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"plan disappeared before the lock could be acquired: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "repin_register: mutation aborted")
    except Exception as exc:  # noqa: BLE001
        return _err(f"repin_register: {type(exc).__name__}: {exc}")

    repinned = state["repinned"]
    if repinned:
        ids = ", ".join(d["id"] for d in repinned)
        return {
            "exit_code": 0,
            "applied": True,
            "plan": plan_rel,
            "repinned": repinned,
            "message": f"re-pinned {ids} on {plan_rel}",
        }
    return {
        "exit_code": 0,
        "applied": False,
        "plan": plan_rel,
        "repinned": [],
        "message": f"{plan_rel} pins already match the register — idempotent no-op",
    }
