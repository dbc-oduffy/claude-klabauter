"""
coordinator_core.ops.dispatch_emit.run_baton_cascade -- fire deliverable.cascade_terminal for a run's batons.

Purpose: resolve a run baton wherever it is now (live under the handoffs home, or archived at its
handoff_archive_dest) and, for each terminal baton that carries a deliverable_id, dispatch the v2
cascade op once per distinct deliverable_id with the baton as source.

Negative-spec:
  - No git, no subprocess: the op's own commit_paths is the only writer.
  - Never guesses ship evidence: no ship_sha and no `shipped_in` -> the baton is skipped by name.
  - Never moves the baton itself: it is the cascade's source, never its target.
  - The op, its library and asyncio are imported only once a terminal baton needs them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.commit_request import valid_deliverable_id
from coordinator_core.ops.fleet._common import handoff_archive_dest
from coordinator_core.session import record_homes

SKIP_REASONS = ("not-terminal", "no-deliverable-id", "no-ship-sha", "unreadable", "op-unavailable")

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class BatonState(NamedTuple):
    rel: str
    deployment_state: Optional[str]
    deliverable_id: Optional[str]
    shipped_in: Optional[str]


def _read_state(path: Path, worktree_root: Path) -> Optional[BatonState]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    fm = split.fm_text
    return BatonState(
        rel=path.relative_to(worktree_root.resolve()).as_posix(),
        deployment_state=read_fm_field_unquoted(fm, "deployment_state"),
        deliverable_id=valid_deliverable_id(read_fm_field_unquoted(fm, "deliverable_id")),
        shipped_in=read_fm_field_unquoted(fm, "shipped_in"),
    )


def resolve_run_baton(worktree_root: Path, rel: str) -> Optional[BatonState]:
    """The baton at `rel` under the handoffs home, else at its handoff_archive_dest.

    `rel` is worktree-relative; a `rel` that is absolute, uses `..`, or sits outside the
    handoffs and archive homes resolves to None before any read. A baton already under
    `archive/handoffs` is read where it is. In-process reads only, no spawn.
    """
    if not isinstance(rel, str) or not rel:
        return None
    parts = Path(rel.replace("\\", "/")).parts
    if Path(rel).is_absolute() or ".." in parts or (parts and parts[0].endswith(":")):
        return None
    root = Path(worktree_root)
    handoffs_home = Path(record_homes.home_dir(str(root), "handoffs"))
    archive_home = root / "archive" / "handoffs"
    candidate = root.joinpath(*parts)

    live = contained_path(candidate, [handoffs_home])
    if live is not None:
        if live.is_file():
            return _read_state(live, root)
        dest = contained_path(handoff_archive_dest(root, candidate), [archive_home])
    else:
        dest = contained_path(candidate, [archive_home])
    if dest is not None and dest.is_file():
        return _read_state(dest, root)
    return None


def fire_for_batons(
    worktree_root: Path, common_dir: Path, baton_rels: Sequence[str], ship_sha: Optional[str],
) -> dict:
    """Dispatch deliverable.cascade_terminal once per distinct deliverable_id among the terminal batons.

    Source is the first baton for each deliverable_id (source_kind "handoff", its current rel);
    ship evidence is `ship_sha`, else the baton's own `shipped_in`. `common_dir` is the git
    common dir, never the worktree root. Reply: {"fired": [{"deliverable_id", "source",
    "advanced", "refused", "commit_sha", "commit_error"?}], "skipped": [{"baton", "reason"}]},
    each reason one of SKIP_REASONS. Never raises: an op exception lands in `fired[i]` as
    `commit_error` naming the exception type.
    """
    fired: list = []
    skipped: list = []
    seen: set = set()
    for rel in baton_rels:
        state = resolve_run_baton(worktree_root, rel)
        if state is None:
            skipped.append({"baton": rel, "reason": "unreadable"})
            continue
        if state.deployment_state not in HANDOFF_TERMINAL_DEPLOYMENT:
            skipped.append({"baton": rel, "reason": "not-terminal"})
            continue
        if state.deliverable_id is None:
            skipped.append({"baton": rel, "reason": "no-deliverable-id"})
            continue
        sha = ship_sha or state.shipped_in
        if not sha or not _SHA_RE.match(sha):
            skipped.append({"baton": rel, "reason": "no-ship-sha"})
            continue
        if state.deliverable_id in seen:
            continue
        seen.add(state.deliverable_id)
        entry = _fire_one(common_dir, state, sha)
        if entry is None:
            skipped.append({"baton": rel, "reason": "op-unavailable"})
        else:
            fired.append(entry)
    return {"fired": fired, "skipped": skipped}


def _fire_one(common_dir: Path, state: BatonState, sha: str) -> Optional[dict]:
    """The `fired` entry for one baton, or None when the op is unregistered or suspended."""
    entry: dict = {
        "deliverable_id": state.deliverable_id,
        "source": state.rel,
        "advanced": [],
        "refused": [],
        "commit_sha": None,
    }
    try:
        import coordinator_core.ops.cascade_terminal_op  # noqa: F401  registers the op
        from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

        reply = reentrant_dispatch(
            "deliverable.cascade_terminal",
            {
                "deliverable_id": state.deliverable_id,
                "source_kind": "handoff",
                "source_path": state.rel,
                "ship_sha": sha,
            },
            repo_root=common_dir,
        )
    except OpUnavailableError:
        return None
    except Exception as exc:  # noqa: BLE001 -- the helper never raises into a close or halt
        entry["commit_error"] = type(exc).__name__
        return entry
    for kind_result in (reply.get("by_kind") or {}).values():
        entry["advanced"].extend(_rel_of(a.get("path")) for a in kind_result.get("advanced", []))
        entry["refused"].extend(
            {"path": _rel_of(r.get("path")), "reason": r.get("reason")}
            for r in kind_result.get("refused", [])
        )
    entry["commit_sha"] = reply.get("commit_sha")
    if reply.get("commit_error"):
        entry["commit_error"] = reply["commit_error"]
    return entry


def _rel_of(path: object) -> str:
    """Repo-relative POSIX tail of an op-reported path (`state/...` or `.coordinator-local/...`)."""
    text = str(path).replace("\\", "/")
    for marker in ("/state/", "/.coordinator-local/"):
        idx = text.find(marker)
        if idx >= 0:
            return text[idx + 1:]
    return text
