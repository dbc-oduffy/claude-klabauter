"""
coordinator_core.ops.goal_kr_cascade -- flip the goal key result a plan discharges.

Called from plan_status_transition._run_cascade after a plan flips to implemented.
The link is the plan's ``prime_exit_criterion.derived_from`` when it has the
``<goal_id>#<kr-id>`` shape (plan.schema.json); ``<goal_id>`` is a goal's ``id``
field under this repo's ``state/goals/``. The write is ``goal.set_kr_status``'s
locked single-line rewrite.

Negative-spec: never writes outside the repo it is handed. A goal id with no file
under ``state/goals/`` (a goal owned by another repo) is reported, not written. The
goal schema has no per-KR plan or sha field. A KR cited by several plans in
``docs/plans/*.md`` or ``archive/specs/**/*.md`` flips only when every one of them
is implemented.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.goal_kr_status import set_kr_status

MET = "met"

_KR_REF_RE = re.compile(r"^([^#/\\]+)#(kr-[^#]+)$")


def _kr_ref(plan_text: str) -> Optional[Tuple[str, str]]:
    try:
        fm = yaml.safe_load(split_frontmatter(plan_text.replace("\r\n", "\n")).fm_text)
    except Exception:
        return None
    pec = fm.get("prime_exit_criterion") if isinstance(fm, dict) else None
    ref = pec.get("derived_from") if isinstance(pec, dict) else None
    m = _KR_REF_RE.match(ref.strip()) if isinstance(ref, str) else None
    return (m.group(1), m.group(2)) if m else None


def _find_goal_file(goals_dir: Path, goal_id: str) -> Optional[Path]:
    for f in sorted(goals_dir.glob("*.yaml")):
        try:
            text = f.read_text(encoding="utf-8")
            if goal_id not in text:
                continue
            doc = yaml.safe_load(text)
        except Exception:
            continue
        if isinstance(doc, dict) and doc.get("id") == goal_id:
            return f
    return None


def _outstanding_plans(repo_root: Path, ref: Tuple[str, str]) -> list:
    needle = f"{ref[0]}#{ref[1]}"
    pending = []
    files = list((repo_root / "docs" / "plans").glob("*.md"))
    files += (repo_root / "archive" / "specs").glob("**/*.md")
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
            if needle not in text:
                continue
            fm = yaml.safe_load(split_frontmatter(text.replace("\r\n", "\n")).fm_text)
        except Exception:
            continue
        if _kr_ref(text) == ref and isinstance(fm, dict) and fm.get("status") != "implemented":
            pending.append(f.name)
    return pending


def discharge_goal_kr(plan_path: Path, repo_root: Path) -> Optional[str]:
    """Set the KR *plan_path* cites to ``met``; return one stderr line, or None.

    None means the plan cites no KR or the KR was already met. A goal absent from
    ``<repo_root>/state/goals`` or a KR absent from the goal returns a line saying so.
    """
    try:
        ref = _kr_ref(Path(plan_path).read_text(encoding="utf-8"))
    except OSError:
        return None
    if ref is None:
        return None
    goal_id, kr_id = ref
    outstanding = _outstanding_plans(Path(repo_root), ref)
    if outstanding:
        return f"{goal_id}#{kr_id} held: plans not implemented: {', '.join(sorted(outstanding))}"
    goals_dir = Path(repo_root) / "state" / "goals"
    goal_file = _find_goal_file(goals_dir, goal_id) if goals_dir.is_dir() else None
    if goal_file is None:
        return (
            f"goal {goal_id} not found under {goals_dir}; {kr_id} left unchanged "
            "(a goal in another repo needs its owner to run goal.set_kr_status)"
        )
    doc = yaml.safe_load(goal_file.read_text(encoding="utf-8"))
    krs = {k.get("id"): k for k in (doc.get("key_results") or []) if isinstance(k, dict)}
    if kr_id not in krs:
        return f"{goal_file.name} has no {kr_id}; goal left unchanged"
    if krs[kr_id].get("status") == MET:
        return None
    try:
        set_kr_status(goal_file, kr_id, MET, repo_root=repo_root)
    except Exception as exc:
        return f"{goal_file.name} {kr_id} not set to {MET}: {exc}"
    return f"{goal_file.name} {kr_id} set to {MET}"
