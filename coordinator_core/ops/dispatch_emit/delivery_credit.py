"""coordinator_core.ops.dispatch_emit.delivery_credit -- spine rows already delivered before a
run's base.

A run's delivery diff starts at `run_base_sha`, so a row coded before a resume (on another
machine, in an earlier run) has no hunk in it and reads as unbacked. `rows_backed_before_base`
names those rows: `disposition: coded` whose `disposition_ref` commit exists and is an ancestor
of `run_base_sha` (a `repo_key:` prefix on the ref is stripped). A row closed as a no-op is
re-keyed to the checkpoint commit that landed it (`landing_commits`). Two git spawns for the whole
spine, three when a no-op row exists; never one per row.

Failure contract: `ancestor_refs` returns `(ancestors, unknown)`. A failed git spawn or a
line-count mismatch puts every given ref in `unknown`, never in a silent "no ancestors" set;
a ref git answered as missing or non-commit is in neither set. Membership in `ancestors` is
parity-tested against `git merge-base --is-ancestor`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.git.run import run_git
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.frontmatter.body_blocks import LocateStatus

NOOP_DETAIL_PREFIX = "resolved as a no-op:"

_SHA = re.compile(r"[0-9a-fA-F]{7,40}")
_CHECKPOINT_PLAN_LINE = re.compile(r"^Checkpoint-Plan: (\S.*?)\s*$", re.M)
_LOG_DEPTH = 1000  # newest commits scanned for a landing; older landings read as unbacked
_REC_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def ancestor_refs(repo_root: Path, refs: List[str], base: str) -> Tuple[set, set]:
    """`(ancestors, unknown)` over `refs` (as given). `ancestors` names a commit reachable from
    `base`; `unknown` is every ref when git could not answer (failed spawn or line mismatch).
    A ref git answered as missing or non-commit is in neither set."""
    if not refs or not base:
        return set(), set()
    cwd = str(repo_root)
    everything = set(refs)
    checked = run_git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd=cwd,
        input=(chr(10).join(refs) + chr(10)).encode("utf-8"),
    )
    lines = checked.stdout.splitlines()
    if checked.returncode != 0 or len(lines) != len(refs):
        return set(), everything
    oid_of = {}
    for ref, line in zip(refs, lines):
        parts = line.split()
        if len(parts) == 2 and parts[1] == "commit":
            oid_of[ref] = parts[0].lower()
    if not oid_of:
        return set(), set()
    ahead = run_git(["rev-list", *sorted(set(oid_of.values())), "--not", base], cwd=cwd)
    if ahead.returncode != 0:
        return set(), everything
    not_ancestors = {ln.strip().lower() for ln in ahead.stdout.splitlines()}
    return {ref for ref, oid in oid_of.items() if oid not in not_ancestors}, set()


def _row_paths(row: dict) -> List[str]:
    out = []
    for key in ("writes", "writes_under"):
        for p in row.get(key) or []:
            if isinstance(p, str) and p.strip():
                out.append(p.strip().replace("\\", "/").rstrip("/"))
    return out


def _plan_rel(repo_root: Path, plan_path: str) -> str:
    p = Path(plan_path)
    if p.is_absolute():
        try:
            p = p.relative_to(repo_root)
        except ValueError:
            pass
    return p.as_posix()


def landing_commits(repo_root: Path, plan_rel: str, rows: Dict[str, List[str]], before: str) -> Dict[str, str]:
    """{row id: sha} of the newest commit at or before `before` that changed any of the row's
    declared writes, whose Checkpoint-Plan trailer names `plan_rel` and whose subject names the
    row id (word-bounded, so C1 never matches C10). One git spawn; a row with no such commit is
    absent, and any git failure yields {}. A path-only match never counts: every path has some
    last-touching commit."""
    paths = sorted({p for ps in rows.values() for p in ps})
    if not paths or not before:
        return {}
    res = run_git(
        ["log", "-n", str(_LOG_DEPTH), "--name-only",
         "--format=%x1e%H%x1f%s%x1f%b%x1f",
         before, "--", *paths],
        cwd=str(repo_root),
    )
    if res.returncode != 0:
        return {}
    want = plan_rel.replace("\\", "/")
    id_res = {rid: re.compile(r"(?<![A-Za-z0-9_])" + re.escape(rid) + r"(?![A-Za-z0-9_])") for rid in rows}
    found: Dict[str, str] = {}
    for rec in res.stdout.split(_REC_SEP):
        parts = rec.split(_FIELD_SEP, 3)
        if len(parts) != 4:
            continue
        named = {m.strip().replace("\\", "/") for m in _CHECKPOINT_PLAN_LINE.findall(parts[2])}
        if want not in named:
            continue
        sha, subject, files = parts[0].strip(), parts[1], [f.strip() for f in parts[3].splitlines() if f.strip()]
        for rid, ps in rows.items():
            if rid in found or not id_res[rid].search(subject):
                continue
            if any(f == p or f.startswith(p + "/") for f in files for p in ps):
                found[rid] = sha
    return found


def coded_row_landings(plan_path: str, *, repo_root: Optional[Path] = None, before: str = "HEAD"):
    """`({sha: [row ids]}, {row id: landing sha})` for the plan's `coded` rows. With `repo_root`, a
    row whose disposition_detail starts with NOOP_DETAIL_PREFIX is re-keyed to its landing commit
    when `landing_commits` finds one; the second mapping names those re-keyed rows."""
    try:
        result = load_rows(Path(plan_path).read_text(encoding="utf-8"))
    except OSError:
        return {}, {}
    if result.status is not LocateStatus.LOCATED:
        return {}, {}
    coded = [r for r in result.rows if isinstance(r, dict) and r.get("disposition") == "coded" and r.get("id")]
    landed: Dict[str, str] = {}
    if repo_root is not None:
        noop = {
            str(r["id"]): _row_paths(r)
            for r in coded
            if str(r.get("disposition_detail") or "").lstrip().startswith(NOOP_DETAIL_PREFIX)
        }
        if noop:
            landed = landing_commits(Path(repo_root), _plan_rel(Path(repo_root), plan_path), noop, before)
    by_ref: dict = {}
    for row in coded:
        rid = str(row["id"])
        ref = landed.get(rid) or str(row.get("disposition_ref") or "").strip().rpartition(":")[2].strip()
        if _SHA.fullmatch(ref):
            by_ref.setdefault(ref, []).append(rid)
    return by_ref, landed


def coded_row_refs(plan_path: str, *, repo_root: Optional[Path] = None, before: str = "HEAD") -> dict:
    """`{sha: [row ids]}` for the plan's `coded` rows, a `repo_key:` prefix stripped from each
    `disposition_ref`; `{}` when the spine is unreadable. With `repo_root`, no-op rows are re-keyed
    to the commit that landed them (see `coded_row_landings`)."""
    return coded_row_landings(plan_path, repo_root=repo_root, before=before)[0]


def rows_backed_before_base(repo_root: Path, plan_path: str, run_base_sha: Optional[str]) -> List[str]:
    """Ids of the plan's `coded` rows whose `disposition_ref` commit is an ancestor of
    `run_base_sha`; `[]` when the spine is unreadable or `run_base_sha` is empty. Refs whose
    ancestry git could not read are omitted (read as unbacked here); only the reverify credit
    note reports them as unknown."""
    if not run_base_sha:
        return []
    by_ref = coded_row_refs(plan_path, repo_root=repo_root, before=run_base_sha)
    if not by_ref:
        return []
    ok, _unknown = ancestor_refs(repo_root, list(by_ref), run_base_sha)
    return [i for ref, ids in by_ref.items() if ref in ok for i in ids]
