"""
coordinator_core.ops.plan_gated_criteria_met — JSON-RPC "plan.gated_criteria_met".

Purpose: the writer of a plan's ``gated_exit_criteria[*].met`` close-out flip. Sets
``met: true`` and ``evidence: <...>`` on each named brightline row in one
``locked_rmw`` closure, then commits the plan through ``commit_authored_content``.

Wire params:
    plan (str, required)  — plan path, repo-relative or absolute, inside the worktree.
    rows (list, required) — ``[{"brightline": <name>, "evidence": <sha | path>[+<sha | path>...]}]``.
        Evidence is a 7-40 hex commit sha reachable from HEAD (one batched
        ``rev-list --no-walk <shas> --not HEAD`` for all sha rows), else a
        repo-relative path that exists.

Reply: ``{"plan", "changed": [brightline, ...], "committed": bool, "message"}``.

Negative-spec:
  - A refusal (unknown brightline, bad evidence) raises ValueError and writes nothing.
  - Never flips ``met`` back to false and never touches ``statement``.
  - A re-run with identical evidence is byte-identical: no write, no commit.
"""

from __future__ import annotations

MUTATES = ["docs/plans/*.md"]  # met/evidence lines of the caller-named plan's gated rows
GENERATES: list = []

import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.frontmatter.primitives import rebuild, serialize_yaml_scalar, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony import git_native

_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_ROW = re.compile(r"^  - brightline:\s*(\S+)\s*$")
_EVIDENCE_MAX = 1024


def _check_evidence(rows: List[Dict[str, str]], worktree_root: Path) -> None:
    """Refuse the first row whose evidence is neither a reachable commit nor an existing path."""
    shas = []
    for row in rows:
        ev = row["evidence"]
        if len(ev) > _EVIDENCE_MAX:
            raise ValueError(f"evidence for {row['brightline']} exceeds {_EVIDENCE_MAX} chars")
        # `a+b` names several pieces of evidence; each is checked on its own.
        for part in ev.split("+"):
            if _SHA.match(part):
                shas.append(part.lower())
            elif not part or Path(part).is_absolute() \
                    or contained_path(worktree_root / part, [worktree_root]) is None \
                    or not (worktree_root / part).exists():
                raise ValueError(f"evidence for {row['brightline']} is not a commit sha or an existing path: {part}")
    if not shas:
        return
    res = git_native.rev_list_not(worktree_root, ["--no-walk", *shas], ["--not", "HEAD"])
    if not res.ok:
        raise ValueError(f"evidence sha does not resolve to a commit: {(res.stderr or '').strip()[:200]}")
    unreachable = set(res.stdout.split())
    for sha in shas:
        if any(full.startswith(sha) for full in unreachable):
            raise ValueError(f"evidence sha is not in HEAD's ancestry: {sha}")


def _flip(fm_text: str, evidence: Dict[str, str]) -> str:
    """Rewrite ``met``/``evidence`` on the named rows; raise MutateAbort on an unknown row."""
    lines = fm_text.split("\n")
    out: List[str] = []
    seen = set()
    i = 0
    n = len(lines)
    in_block = False
    while i < n:
        line = lines[i]
        if re.match(r"^\S", line):
            in_block = line.startswith("gated_exit_criteria:")
        m = _ROW.match(line) if in_block else None
        if not m or m.group(1) not in evidence:
            out.append(line)
            i += 1
            continue
        name = m.group(1)
        seen.add(name)
        j = i + 1
        while j < n and lines[j].startswith("    "):
            j += 1
        row = [r for r in lines[i + 1:j]]
        kept: List[str] = []
        k = 0
        while k < len(row):
            if row[k].startswith("    evidence:"):
                k += 1
                while k < len(row) and row[k].startswith("      "):
                    k += 1
                continue
            kept.append(row[k])
            k += 1
        ev_line = "    evidence: " + serialize_yaml_scalar(evidence[name], numeric_quoting=True)
        placed = False
        body: List[str] = []
        idx = 0
        while idx < len(kept):
            if kept[idx].startswith("    met:"):
                body.append("    met: true")
                body.append(ev_line)
                placed = True
            else:
                body.append(kept[idx])
            idx += 1
        if not placed:
            body.extend(["    met: true", ev_line])
        out.append(line)
        out.extend(body)
        i = j
    missing = sorted(set(evidence) - seen)
    if missing:
        raise MutateAbort(f"unknown brightline: {', '.join(missing)}")
    return "\n".join(out)


@register_op("plan.gated_criteria_met")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.gated_criteria_met" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.gated_criteria_met requires a resolved repo_root")
    raw_plan = params.get("plan")
    if not isinstance(raw_plan, str) or not raw_plan.strip():
        raise ValueError("plan must be a non-empty string naming one plan file")
    raw_rows = params.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("rows must be a non-empty list of {brightline, evidence}")
    rows: List[Dict[str, str]] = []
    for r in raw_rows:
        if not (isinstance(r, dict) and isinstance(r.get("brightline"), str) and r["brightline"].strip()
                and isinstance(r.get("evidence"), str) and r["evidence"].strip()):
            raise ValueError("each row needs a non-empty string brightline and evidence")
        rows.append({"brightline": r["brightline"].strip(), "evidence": r["evidence"].strip()})

    worktree_root = Path(main_worktree_root(repo_root))
    plan_path = Path(raw_plan.strip())
    if not plan_path.is_absolute():
        plan_path = worktree_root / plan_path
    if contained_path(plan_path, [worktree_root]) is None:
        raise ValueError(f"plan escapes the resolved worktree: {raw_plan!r}")
    rel = plan_path.resolve().relative_to(worktree_root.resolve()).as_posix()

    _check_evidence(rows, worktree_root)
    evidence = {r["brightline"]: r["evidence"] for r in rows}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {rel}")
        new_fm = _flip(split.fm_text, evidence)
        return old_text if new_fm == split.fm_text else rebuild(split, new_fm)

    state: Dict[str, str] = {}
    try:
        old_on_disk = plan_path.read_text(encoding="utf-8")
        new_text = locked_rmw(plan_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        raise ValueError(f"no such plan: {rel}")
    except LockTimeout as exc:
        raise ValueError(f"timed out waiting for the file lock on {rel}: {exc}")
    except MutateAbort as exc:
        raise ValueError(exc.args[0] if exc.args else "mutation aborted")

    changed = sorted(evidence)
    if new_text == old_on_disk:
        return {"plan": rel, "changed": [], "committed": False, "message": "already met with this evidence"}

    msg = f"plan: gated_exit_criteria met ({', '.join(changed)}) — {rel}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(msg)
        msg_path = fh.name
    try:
        res = git_native.commit_authored_content(rel, new_text, msg_path, worktree_root)
    finally:
        Path(msg_path).unlink(missing_ok=True)
    if not res.ok:
        raise ValueError(f"flip written to {rel} but commit failed: {(res.stderr or '').strip()[:200]}")
    return {"plan": rel, "changed": changed, "committed": True, "message": "met flipped and committed"}
