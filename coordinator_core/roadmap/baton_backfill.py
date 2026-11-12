"""Back-fills ``shipped`` stamps onto open roadmap batons whose linked plan's rows all landed.

Selection reads the gate report (``gate_v2.compute_gate_report``, no git) and each linked plan's
spine through ``baton_rows_landed.rows_landed``. A caller may credit still-open rows to named
hand-commits through ``met_by``; the credit is proven (abbreviations resolved, ancestry of HEAD)
before anything is written, in one batched git call per question. Apply codes the credited rows,
stamps each baton through ``blitz_land.close_dispatched`` and lands one commit.

Invariants: a refused ``met_by`` row refuses the whole call and writes nothing; an unreadable
ancestry answer is a refusal, never "not an ancestor"; archived or already-terminal batons are
never touched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core import git_ancestry
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.roadmap import blitz_land, gate_v2
from coordinator_core.roadmap import plan_gate as pg
from coordinator_core.roadmap.baton_rows_landed import rows_landed
from coordinator_core.shipped_in_tokens import _SHA_HEX_RE

_MET_BY_SHA = re.compile(r"[0-9a-f]{4,40}")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def parse_met_by(met_by: Any) -> Dict[str, Dict[str, str]]:
    """``{plan_path: {row_id: lowercase sha}}`` from the wire value; ValueError names the offender."""
    if met_by is None:
        return {}
    if not isinstance(met_by, dict):
        raise ValueError("met_by must be an object {plan_path: {row_id: sha}}")
    out: Dict[str, Dict[str, str]] = {}
    for plan, rows in met_by.items():
        if not isinstance(plan, str) or not plan.strip() or not isinstance(rows, dict) or not rows:
            raise ValueError(f"met_by[{plan!r}] must be a non-empty object {{row_id: sha}}")
        key = plan.strip().replace("\\", "/")
        for rid, sha in rows.items():
            if not isinstance(rid, str) or not rid.strip():
                raise ValueError(f"met_by[{plan!r}] carries a non-string row id")
            if not isinstance(sha, str) or not _MET_BY_SHA.fullmatch(sha.strip().lower()):
                raise ValueError(f"met_by[{plan!r}][{rid!r}] is not a 4-40 character hex sha")
            out.setdefault(key, {})[rid.strip()] = sha.strip().lower()
    return out


def _commit(params: dict, repo_root: Path) -> Any:
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    return reentrant_dispatch("ceremony.commit_v2", params, repo_root=repo_root)


def _read(root: Path, rel: str, raw: bool = False) -> Optional[str]:
    """File text, LF-normalised unless ``raw`` (the spine reader does not read CRLF)."""
    try:
        text = (root / rel).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if raw else text.replace("\r\n", "\n")


def _select_batons(root: Path, wanted: Optional[set]) -> List[dict]:
    """Live open batons with a linked plan, in report order, narrowed by ``wanted`` ids/paths."""
    out = []
    for row in gate_v2.compute_gate_report(root)["batons"]:
        plan = row.get("plan")
        path = row["path"]
        if not plan or path.startswith("archive/") or plan["path"].startswith("archive/"):
            continue
        if row["deployment_state"] in pg.BATON_CODED_STATES:
            continue
        stem = path.rsplit("/", 1)[-1][:-3]
        if wanted and not ({row["id"], row.get("stub_id"), path, stem} & wanted):
            continue
        out.append(row)
    return out


def _credit(text: str, rows: Dict[str, str]) -> Tuple[str, set]:
    """``text`` with the ``rows`` ({row_id: full sha}) coded, and the ids that flipped."""
    from coordinator_core.ops.dispatch_emit.plan_row_coding import flip_rows_coded

    by_sha: Dict[str, set] = {}
    for rid, sha in rows.items():
        by_sha.setdefault(sha, set()).add(rid)
    flipped: set = set()
    for sha, ids in sorted(by_sha.items()):
        text, done = flip_rows_coded(text, ids, sha)
        flipped |= set(done)
    return text, flipped


def _prove_met_by(root: Path, met_by: Dict[str, Dict[str, str]]) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    """``(met_by with full shas, refused)``: one prefix resolution and one ancestry call in all."""
    shas = [s for rows in met_by.values() for s in rows.values()]
    if not shas:
        return {}, {}
    resolved, refusal = git_ancestry.resolve_commit_prefixes(shas, cwd=str(root))
    if refusal is not None:
        return {}, {"met_by": refusal}
    ok, reached = git_ancestry.ancestors_of(set(resolved.values()), "HEAD", cwd=str(root))
    if not ok or reached is None:
        return {}, {"met_by": "ancestry of the met_by shas could not be read; nothing verified"}
    full = {p: {r: resolved[s] for r, s in rows.items()} for p, rows in met_by.items()}
    refused = {
        f"{p}:{r}": f"met_by sha {met_by[p][r]} is not an ancestor of HEAD"
        for p, rows in full.items() for r, s in rows.items() if s not in reached
    }
    return full, refused


def backfill(
    root: Path,
    repo_root: Path,
    *,
    dry_run: bool = True,
    batons: Optional[List[str]] = None,
    met_by: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Propose (``dry_run``) or apply the back-fill. Reply ``{dry_run, proposals[{baton, plan,
    shipped_in, rows_coded}], stamped[], refused{}, commit_sha}``; a non-empty ``refused`` keyed
    by a met_by row, plan or ``met_by`` means nothing was written."""
    met_by = met_by or {}
    reply: Dict[str, Any] = {
        "dry_run": dry_run, "proposals": [], "stamped": [], "refused": {}, "commit_sha": None,
    }
    selected = _select_batons(root, set(batons) if batons else None)
    plan_paths = {row["plan"]["path"] for row in selected}
    unmatched = {
        p: "no live open baton in scope links this plan" for p in sorted(met_by) if p not in plan_paths
    }
    if unmatched:
        reply["refused"] = unmatched
        return reply
    full, refused = _prove_met_by(root, met_by)
    if refused:
        reply["refused"] = refused
        return reply

    texts: Dict[str, str] = {}
    flipped_by_plan: Dict[str, set] = {}
    for plan_path in sorted(plan_paths):
        text = _read(root, plan_path)
        if text is None:
            continue
        credit = full.get(plan_path, {})
        if credit:
            text, flipped = _credit(text, credit)
            missed = sorted(set(credit) - flipped)
            if missed:
                refused.update({
                    f"{plan_path}:{rid}": "met_by row is not an open row of this plan" for rid in missed
                })
            flipped_by_plan[plan_path] = flipped
        texts[plan_path] = text
    if refused:
        reply["refused"] = refused
        return reply

    proposals: List[dict] = []
    soft: Dict[str, str] = {}
    for row in selected:
        plan_path = row["plan"]["path"]
        landed = rows_landed(texts[plan_path]) if plan_path in texts else None
        if landed is None:
            continue
        sha = landed["coded_sha"]
        if not sha or not _SHA_HEX_RE.fullmatch(sha):
            soft[row["path"]] = "plan rows landed but no coded row carries a commit sha"
            continue
        proposals.append({
            "baton": row["path"], "plan": plan_path, "shipped_in": sha,
            "rows_coded": sorted(flipped_by_plan.get(plan_path, ())),
        })
    bad = blitz_land.shipped_in_refusals(root, [p["shipped_in"] for p in proposals])
    kept = []
    for prop in proposals:
        reason = bad.get(prop["shipped_in"].lower())
        if reason:
            soft[prop["baton"]] = reason
        else:
            kept.append(prop)
    reply["proposals"] = kept
    reply["refused"] = soft
    if dry_run or not kept:
        return reply
    return _apply(root, repo_root, reply, kept, full, flipped_by_plan, bad)


def _apply(root, repo_root, reply, proposals, full, flipped_by_plan, bad) -> Dict[str, Any]:
    originals: Dict[str, str] = {}

    def restore() -> None:
        for rel, text in originals.items():
            locked_rmw(root / rel, lambda _old, t=text: t, repo_root=root)

    def fail(key: str, reason: str) -> Dict[str, Any]:
        restore()
        reply["refused"] = {**reply["refused"], key: reason}
        reply["stamped"] = []
        return reply

    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source

    paths: List[str] = []
    for plan_path, flipped in sorted(flipped_by_plan.items()):
        if not flipped:
            continue
        old_text = _read(root, plan_path, raw=True)
        if old_text is None:
            return fail(plan_path, "plan unreadable; nothing written")
        new_text, _done = _credit(old_text, full[plan_path])
        if check_plan_tasks_source(new_text) is not None and check_plan_tasks_source(old_text) is None:
            return fail(plan_path, "stamped spine fails schema; nothing written")
        originals[plan_path] = old_text
        try:
            locked_rmw(root / plan_path, lambda _old, t=new_text: t, repo_root=root)
        except (MutateAbort, LockTimeout, OSError) as exc:
            return fail(plan_path, f"plan write failed: {exc}")
        paths.append(plan_path)
    stamped: List[str] = []
    for prop in proposals:
        baton = prop["baton"]
        before = _read(root, baton, raw=True)
        try:
            out = blitz_land.close_dispatched(root, baton, prop["shipped_in"], refusals=bad)
        except blitz_land.LandingRefused as exc:
            reply["refused"][baton] = str(exc)
            continue
        if not out.get("closed"):
            reply["refused"][baton] = str(out.get("note") or "baton not closed")
            continue
        if before is not None:
            originals[baton] = before
        stamped.append(baton)
        paths.append(baton)
    reply["stamped"] = stamped
    if not paths:
        return reply
    shas = sorted({p["shipped_in"][:7] for p in proposals if p["baton"] in stamped})
    message = (
        f"back-fill shipped stamp on {len(stamped)} baton(s) ({', '.join(shas)})"
        if stamped else f"mark rows coded via met_by on {len(paths)} plan(s)"
    )
    try:
        result = _commit({"paths": sorted(set(paths)), "message": message}, repo_root)
    except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal, files restored
        return fail("commit", f"commit did not land: {type(exc).__name__}: {exc}")
    if not isinstance(result, dict) or not result.get("committed"):
        detail = result.get("error") if isinstance(result, dict) else result
        return fail("commit", f"commit did not land: {detail!r}")
    reply["commit_sha"] = result.get("sha")
    return reply
