"""Phase-1 gates for a chain run, in-process: stamp check, sizing authorization, roadmap gate, claim.

``run`` is C0's order 1 to 4; the first halt returns and nothing after it runs. No step spawns a
subprocess: every callee is a library function called directly.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.plan_chain.contract import ChainManifest, Halt, halt
from coordinator_core.pickup_assemble import _parse_fm_dict
from coordinator_core.pickup_assemble.stamp_check import stamp_check
from coordinator_core.review_assemble import exec_auth_stamp
from coordinator_core.roadmap.plan_gate import assemble_plan_gate
from coordinator_core.session.claims import claim_plan

_EXECUTE = "execute"


def _rel_and_abs(plan_path: str | Path, repo_root: Path) -> tuple[str, Path]:
    p = Path(plan_path)
    absolute = p if p.is_absolute() else repo_root / p
    try:
        rel = absolute.relative_to(repo_root).as_posix()
    except ValueError:
        rel = p.as_posix()
    return rel, absolute


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _read_fm(path: Path) -> dict:
    try:
        split = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}
    return _parse_fm_dict(split.fm_text) if split is not None else {}


def _stamp_gate(rel: str, repo_root: Path) -> Halt | None:
    _code, gate = stamp_check(rel, repo_root=repo_root)
    if gate.get("verdict") == "stale-substantive":
        return halt(
            "stamp-stale-substantive",
            f"{rel}: stale-substantive: {gate.get('next_move', 'surface to the PM')}",
        )
    return None


def _authorize_gate(absolute: Path, sizing: str, repo_root: Path) -> Halt | None:
    sizing_path = Path(sizing)
    if not sizing_path.is_absolute():
        sizing_path = repo_root / sizing_path
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = exec_auth_stamp.main(
            ["authorize-invocation", str(absolute), "--authorized-by-sizing", str(sizing_path)]
        )
    if code == 0:
        return None
    detail = _last_line(err.getvalue()) or _last_line(out.getvalue()) or f"exit {code}"
    return Halt(_EXECUTE, f"authorize-invocation refused: {detail}")


def _roadmap_gate(manifest: ChainManifest, repo_root: Path) -> Halt | None:
    baton_fm = _read_fm(repo_root / manifest.baton)
    if not baton_fm.get("roadmap_id"):
        return None
    report = assemble_plan_gate(repo_root, subject=manifest.baton)
    rows = [b for b in report.get("batons", []) if b.get("path") == manifest.baton]
    if not rows:
        return halt("roadmap-gate-shut", f"{manifest.baton}: not found in the roadmap gate report")
    gate = rows[0].get("execution_gate") or {}
    if gate.get("open"):
        return None
    blockers = ", ".join(str(b.get("blocker")) for b in gate.get("blocking", [])) or "unknown"
    return halt("roadmap-gate-shut", f"{manifest.baton}: execution gate shut by {blockers}")


def _claim_gate(rel: str, repo_root: Path) -> Halt | None:
    slug = Path(rel).stem
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        claimed = claim_plan(slug, str(repo_root), for_execution=True, plan_path=rel)
    if claimed:
        return None
    return halt("peer-claim", f"claim-plan {slug} refused: {_last_line(err.getvalue())}")


def run(manifest: ChainManifest, plan_path: str | Path, *, repo_root: str | Path) -> Halt | None:
    """Return the first gate's Halt, or None when the plan may execute now."""
    root = Path(repo_root)
    rel, absolute = _rel_and_abs(plan_path, root)
    return (
        _stamp_gate(rel, root)
        or _authorize_gate(absolute, manifest.sizing_object, root)
        or _roadmap_gate(manifest, root)
        or _claim_gate(rel, root)
    )
