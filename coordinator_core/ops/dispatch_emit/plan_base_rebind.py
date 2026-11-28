"""``dispatch.rebind_plan_base``: rebinds a plan's pinned base stamp and ledger-requirement pins in one op.

Params: ``plan`` (repo-relative path, required) and ``stamp`` (full 40-hex sha, default ``HEAD``). Rewrites the plan's
``prime_exit_criterion.falsifier.baseline_ref`` to the stamp and recomputes every ``register_claims.pins`` value from the
sizing's live register (``requirement_register.repin_plan_text``). Only those two frontmatter lines change, in place,
under ``locked_rmw``: never a YAML re-dump, since the frontmatter carries load-bearing comments. A plan with no
``register_claims`` rebinds the base only. Refuses, writing nothing: a plan outside the repo or unreadable, one with no
``falsifier.baseline_ref``, a claimed row absent from the live register (named), an unreadable sizing, and a stamp that
is not a full sha or not an ancestor of HEAD. A repeat call replies ``applied: false``. One git spawn: ``rev-parse HEAD``
for the default stamp, ``rev-list`` ancestry for an explicit one.
Reply: ``{ok, plan, applied, stamp, pins}`` or ``{ok: false, error}``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit.rebind_contract import APPLIED, OP_REBIND_PLAN_BASE, PINS, STAMP

OP_NAME = OP_REBIND_PLAN_BASE

_SHA40 = re.compile(r"[0-9a-f]{40}")
_FALSIFIER = re.compile(r"^(\s*)falsifier:\s*(#.*)?$")
_BASELINE = re.compile(r"^(\s+baseline_ref:\s*)(\"[^\"]*\"|'[^']*'|[^\s#]+)(\s*#.*)?$")


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _resolve_stamp(root: Path, stamp: object) -> tuple:
    """``(full_sha, None)`` or ``(None, reason)``; one git spawn."""
    from coordinator_core import git_ancestry
    from coordinator_core.git.run import run_git

    if stamp in (None, "", "HEAD"):
        proc = run_git(["rev-parse", "HEAD"], cwd=str(root))
        sha = proc.stdout.strip() if proc.returncode == 0 else ""
        return (sha, None) if _SHA40.fullmatch(sha) else (None, "HEAD does not resolve to a commit")
    if not isinstance(stamp, str) or not _SHA40.fullmatch(stamp):
        return None, f"stamp {stamp!r} is not a full 40-hex sha"
    read_ok, reached = git_ancestry.ancestors_of([stamp], "HEAD", cwd=str(root))
    if not read_ok or reached is None:
        return None, f"stamp {stamp} does not resolve to a commit"
    if stamp not in reached:
        return None, f"stamp {stamp} is not an ancestor of HEAD"
    return stamp, None


def _rebind_baseline(text: str, stamp: str) -> str:
    """``text`` with only the ``falsifier.baseline_ref`` value replaced; ValueError when the plan carries none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("plan has no frontmatter")
    fm_end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    if fm_end is None:
        raise ValueError("plan frontmatter is not closed")
    head = next((i for i in range(1, fm_end) if _FALSIFIER.match(lines[i].rstrip("\r\n"))), None)
    if head is None:
        raise ValueError("plan has no falsifier block")
    base_indent = len(lines[head]) - len(lines[head].lstrip(" "))
    for i in range(head + 1, fm_end):
        raw = lines[i].rstrip("\r\n")
        if raw.strip() and len(raw) - len(raw.lstrip(" ")) <= base_indent:
            break
        m = _BASELINE.match(raw)
        if m:
            eol = lines[i][len(raw):]
            lines[i] = f"{m.group(1)}{stamp}{m.group(3) or ''}{eol}"
            return "".join(lines)
    raise ValueError("plan has no falsifier.baseline_ref")


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.rebind_plan_base": rebind a plan's baseline_ref and register pins to one stamp."""
    from coordinator_core.locked_write import MutateAbort, locked_rmw
    from coordinator_core.ops import requirement_register as rr
    from coordinator_core.ops._path_guard import contained_path

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    plan = params.get("plan")
    if not isinstance(plan, str) or not plan:
        return _refuse(f"{OP_NAME} requires a repo-relative string plan")
    root = main_worktree_root(Path(repo_root))
    plan_path = contained_path(root / plan, [root])
    if plan_path is None or not plan_path.is_file():
        return _refuse(f"plan {plan} is not a file inside the repo")
    try:
        text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _refuse(f"plan {plan} is unreadable: {exc}")

    register = None
    fm = text.replace("\r\n", "\n").split("\n---", 1)[0] if text.startswith("---") else ""
    sizing_rel, _ids = rr.plan_claims(fm)
    if sizing_rel:
        sizing_path = contained_path(root / sizing_rel, [root])
        try:
            register = rr.read_register(sizing_path.read_text(encoding="utf-8")) if sizing_path else None
        except (OSError, UnicodeDecodeError):
            register = None
        if register is None:
            return _refuse(f"sizing {sizing_rel} is unreadable or carries no register; pins are not rebound")

    stamp, why = _resolve_stamp(root, params.get(STAMP))
    if stamp is None:
        return _refuse(why)

    changed = []

    def mutate(current: str) -> str:
        try:
            new = _rebind_baseline(current, stamp)
            if register is not None:
                new, _ = rr.repin_plan_text(new, register)
        except ValueError as exc:
            raise MutateAbort(str(exc)) from exc
        changed.append(new != current)
        return new

    try:
        after = locked_rmw(plan_path, mutate, repo_root=root)
    except MutateAbort as exc:
        return _refuse(f"{plan}: {exc}")
    except Exception as exc:  # noqa: BLE001 - lock timeout or write failure refuses, never raises
        return _refuse(f"{plan} could not be rewritten: {exc}")
    pins = {}
    if register is not None:
        pins = rr.plan_pins(after.replace("\r\n", "\n").split("\n---", 1)[0])
    return {"ok": True, "plan": plan, APPLIED: bool(changed and changed[-1]), STAMP: stamp, PINS: pins}
