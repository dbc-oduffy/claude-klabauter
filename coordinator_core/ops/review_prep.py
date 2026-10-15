"""
coordinator_core.ops.review_prep — registers ``review.prep``: one in-process call that freezes
a run's whole diff, partitions it into review slices and lists the foreign claims, returning
the ``review-prep-result`` shape of DoE's ``review-stage.schema.json`` plus ``error``.

Composes ``review_freeze_diff.freeze_diff`` and ``review_partition_slices.partition_slices``
in-process (no CLI). The whole diff is frozen under ``slice_prefix``; slices under
``<slice_prefix>-<n>``. Exactly one of ``head`` (committed ``base..head``) / ``worktree=true``.

Foreign claims: declared paths a commit in ``base..head`` (``base..HEAD`` in worktree mode) also
changed, kept only when a live session other than the caller's claims the path
(``claim_index.lookup``). The commit-range read is the one git spawn added over the two ops
composed; it is skipped when ``paths`` is empty.

Negative-spec: a single-reviewer-ok verdict over product files returns ONE whole-diff slice,
never an empty list; zero product files returns no slices. A refusal from either composed op is
returned as ``error`` with no slices, never raised. ``basis`` is ``"worktree"`` in worktree
mode and ``"range"`` in range mode.
"""

from __future__ import annotations

MUTATES = [
    "state/review-trail/diffs/*.diff",
    "state/review-trail/diffs/*.head.sha",
    ".coordinator-local/subagent-share/*/*.review-prep.md",
]

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.git.literal_pathspec import git_pathspec
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops.review_freeze_diff import freeze_diff
from coordinator_core.ops.review_partition_slices import partition_slices
from coordinator_core.ops.review_stamp import product_files as diff_product_files
from coordinator_core.session import claim_index
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.session.machinery_paths import share_dir

_SHA_RE = re.compile(r"^[0-9a-f]{4,64}$")
_GIT_TIMEOUT_SECS = 5.0


def _fail(message: str) -> dict:
    return {
        "plan_id": "",
        "share_dir": "",
        "verdict": None,
        "basis": "",
        "run_base_sha": "",
        "whole_diff_path": None,
        "product_files": 0,
        "foreign_claims": [],
        "slices": [],
        "whole_diff_sidecars": {"kira": "", "personas": [], "delivery": ""},
        "error": message,
    }


def _plan_id(repo_root: Path, plan_path: str) -> str:
    """The plan's frontmatter ``plan_id``, else its file stem."""
    plan_file = Path(plan_path)
    if not plan_file.is_absolute():
        plan_file = repo_root / plan_file
    try:
        split = split_frontmatter(plan_file.read_text(encoding="utf-8", errors="replace"))
        doc = yaml.safe_load(split.fm_text) if split else None
    except (OSError, yaml.YAMLError):
        doc = None
    value = doc.get("plan_id") if isinstance(doc, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else plan_file.stem


def foreign_claims(repo_root: Path, rev_range: str, paths: List[str], own_session: str) -> List[str]:
    """Declared `paths` changed by a commit in `rev_range` that a live session other than
    `own_session` claims. One `git log` spawn; none when `paths` is empty."""
    if not paths:
        return []
    proc = run_git(
        ["-C", str(repo_root), "log", "--name-only", "--format=", rev_range, "--", *map(git_pathspec, paths)],
        timeout=_GIT_TIMEOUT_SECS,
    )
    if proc.returncode != 0:
        return []
    changed = sorted({ln.strip() for ln in proc.stdout.splitlines() if ln.strip()})
    if not changed:
        return []
    claims = claim_index.lookup(changed, cwd=str(repo_root))
    return [
        p
        for p in changed
        if any(c not in (own_session, claim_index.UNANSWERABLE) for c in claims.get(p, []))
    ]


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def review_prep(
    repo_root: Path,
    *,
    base: str,
    paths: List[str],
    slice_prefix: str,
    plan_path: str,
    head: Optional[str] = None,
    session_id: str = "",
) -> dict:
    """Freeze, partition and claim-check one run's diff; see the module docstring."""
    if not base or not paths or not slice_prefix or not plan_path:
        return _fail("base, paths, slice_prefix and plan_path are required")
    if not _SHA_RE.match(base):
        return _fail("base must be a lowercase commit sha")
    session = session_id or resolve_session_id(str(repo_root))
    if not session:
        return _fail("no session id: pass session_id or run under a session")

    plan_id = _plan_id(repo_root, plan_path)
    share = Path(share_dir(str(repo_root), session))
    sidecar = share / f"{slice_prefix}.review-prep.md"
    whole_sidecars = {
        "kira": _rel(share / f"{slice_prefix}.kira.md", repo_root),
        "personas": [_rel(share / f"{slice_prefix}.whole-slice.md", repo_root)],
        "delivery": _rel(share / f"{slice_prefix}.delivery.md", repo_root),
    }
    out = _fail("")
    out.update(
        plan_id=plan_id,
        share_dir=_rel(share, repo_root),
        basis="range" if head else "worktree",
        run_base_sha=base,
        whole_diff_sidecars=whole_sidecars,
        error=None,
    )

    rev_range = f"{base}..{head or 'HEAD'}"
    try:
        if head:
            whole = freeze_diff(repo_root, rev_range, slice_prefix, paths)
        else:
            whole = freeze_diff(repo_root, base, slice_prefix, paths, worktree=True)
    except ValueError as exc:
        whole = {"error": str(exc)}
    if whole.get("error"):
        out["error"] = f"freeze of {slice_prefix} failed: {whole['error']}"
        return out
    out["whole_diff_path"] = whole["diff_path"]
    product = diff_product_files(Path(whole["diff_path"]))
    out["product_files"] = len(product)

    part = partition_slices(repo_root, base, paths, slice_prefix, head=head or None)
    if part["error"]:
        out["error"] = part["error"]
        return out
    out["verdict"] = part["verdict"]
    if part["verdict"] == "PARTITION-MANDATORY":
        out["slices"] = [
            {
                "id": s["slice_id"],
                "files": s["paths"],
                "diff_path": s["diff_path"],
                "sidecar_path": whole_sidecars["personas"][0],
                "contract_blocks": 0,
            }
            for s in part["slices"]
        ]
    elif product:
        out["slices"] = [
            {
                "id": "whole-diff",
                "files": product,
                "diff_path": whole["diff_path"],
                "sidecar_path": whole_sidecars["personas"][0],
                "contract_blocks": 0,
            }
        ]

    out["foreign_claims"] = foreign_claims(repo_root, rev_range, paths, session)
    _write_sidecar(sidecar, out, session)
    out["sidecar_path"] = _rel(sidecar, repo_root)
    return out


def _write_sidecar(sidecar: Path, result: dict, session: str) -> None:
    keys = (
        "plan_id", "run_base_sha", "verdict", "basis", "whole_diff_path", "product_files",
        "foreign_claims", "whole_diff_sidecars",
    )
    fm = {
        "agent_type": "engine:prep-freeze",
        "status": "complete",
        "lead_session_id": session,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        **{k: result[k] for k in keys},
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False).strip()
    atomic_write_bytes(
        sidecar,
        f"---\n{text}\n---\n\n## Review prep\n\nMechanical, no-agent record: review_prep.review_prep.\n".encode("utf-8"),
    )
    declare_write(sidecar)


@register_op("review.prep")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.prep". Params: ``base`` (str sha), ``paths`` (list[str]), ``slice_prefix``
    (str), ``plan_path`` (str), optional ``session_id``, and exactly one of ``head`` (str,
    committed range mode) or ``worktree: true``. Keying scope: show_top."""
    if repo_root is None:
        return _fail("review.prep requires a show_top-keyed dispatch; repo_root was not supplied")
    paths = params.get("paths")
    if not isinstance(paths, list):
        return _fail("review.prep requires params.paths as a list")
    head = str(params.get("head") or "")
    if bool(head) == bool(params.get("worktree")):
        return _fail("review.prep requires exactly one of head (range mode) or worktree=true")
    return review_prep(
        repo_root,
        base=str(params.get("base") or ""),
        paths=[str(p) for p in paths],
        slice_prefix=str(params.get("slice_prefix") or ""),
        plan_path=str(params.get("plan_path") or ""),
        head=head or None,
        session_id=str(params.get("session_id") or ""),
    )
