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

Computes once per ``slice_prefix``: the success sidecar stores the whole reply, and a later call
with the same prefix, base, basis, head and paths reads it back and spawns no git. Concurrent
first callers claim ``<slice_prefix>.review-prep.lock`` with ``O_EXCL``; the loser waits at most
``_LOCK_WAIT_SECS`` for the winner's sidecar, then returns it or a refusal whose ``error`` starts
with ``REVIEW_PREP_BUSY_PREFIX`` for the caller to re-call. No path computes without the lock.
Only a success is stored, so a refusal is recomputed.

The sidecar also stores ``reviewed_sha256`` (repo-relative POSIX product path -> sha256 of the
bytes frozen) beside ``recorded_at``. A worktree-mode stored reply is served only when that map is
present and every entry still matches the bytes on disk; a moved worktree recomputes, shelving the
prior freeze. Range mode ignores it. Trap: the key is deliberately outside ``_STORED_KEYS``, so an
older sidecar without it stays readable by ``stored_reply_for_run``.

Optional ``group_index`` / ``group_count`` (both or neither) narrow the reply's ``slices`` to the
one contiguous balanced group at ``group_index`` of ``min(slices, group_count)`` groups, the split
``execute_review._GROUP_SLICES_JS`` applies; every other field is the whole-run reply. Without
them ``slices`` is the full list.

Negative-spec: a single-reviewer-ok verdict over product files returns ONE whole-diff slice,
never an empty list; zero product files returns no slices. A refusal from either composed op is
returned as ``error`` with no slices, never raised. ``basis`` is ``"worktree"`` in worktree
mode and ``"range"`` in range mode. A group past the last populated one returns no slices; that
is not an error. ``stored_reply_for_run`` reads a run's stored reply back: it never computes, never
spawns git and takes no lock.
"""

from __future__ import annotations

MUTATES = [
    "state/review-trail/diffs/*.diff",
    "state/review-trail/diffs/*.head.sha",
    ".coordinator-local/subagent-share/*/*.review-prep.md",
    ".coordinator-local/subagent-share/*/*.review-prep.lock",
]

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

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
from coordinator_core.session.touch_record import compute_content_hash

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


def group_slices(slices: List[dict], group_count: int) -> List[List[dict]]:
    """``slices`` split into ``max(1, min(len(slices), group_count))`` contiguous groups whose sizes
    differ by at most one, empty groups dropped."""
    n = max(1, min(len(slices), group_count))
    groups: List[List[dict]] = []
    at = 0
    for i in range(n):
        size = len(slices) // n + (1 if i < len(slices) % n else 0)
        groups.append(slices[at : at + size])
        at += size
    return [g for g in groups if g]


def _for_group(reply: dict, group_index: Optional[int], group_count: Optional[int]) -> dict:
    if group_index is None or group_count is None or reply.get("error"):
        return reply
    groups = group_slices(reply["slices"], group_count)
    return {**reply, "slices": groups[group_index] if group_index < len(groups) else []}


_STORED_KEYS = (
    "plan_id", "share_dir", "run_base_sha", "verdict", "basis", "whole_diff_path", "product_files",
    "foreign_claims", "slices", "whole_diff_sidecars", "sidecar_path",
)
REVIEW_PREP_BUSY_PREFIX = "review.prep busy:"
_LOCK_WAIT_SECS = 0.5
# 20x the wait: a live winner holding the lock this long is an over-bar op; a dead one clears fast.
_LOCK_STALE_SECS = 10.0
_LOCK_POLL_SECS = 0.05


def _reviewed_bytes_unmoved(repo_root: Path, reviewed: object) -> bool:
    """True when ``reviewed`` is a ``reviewed_sha256`` map and every entry still hashes equal."""
    if not isinstance(reviewed, dict):
        return False
    return all(compute_content_hash(repo_root / rel) == digest for rel, digest in reviewed.items())


def _stored_reply(
    repo_root: Path, sidecar: Path, base: str, paths: List[str], head: Optional[str]
) -> Optional[dict]:
    """The reply a success sidecar stored, or ``None`` when absent, unreadable, written before
    replies were stored, written for another base, basis, head or path list, or (worktree mode)
    lacking ``reviewed_sha256`` or recorded over bytes that have since moved."""
    try:
        split = split_frontmatter(sidecar.read_text(encoding="utf-8"))
        fm = yaml.safe_load(split.fm_text) if split else None
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(fm, dict) or any(k not in fm for k in _STORED_KEYS):
        return None
    if (
        fm["run_base_sha"] != base
        or fm["basis"] != ("range" if head else "worktree")
        or fm.get("head") != (head or None)
        or fm.get("paths") != paths
    ):
        return None
    if not head and not _reviewed_bytes_unmoved(repo_root, fm.get("reviewed_sha256")):
        return None
    reply = {**{k: fm[k] for k in _STORED_KEYS}, "error": None}
    if "reviewed_sha256" in fm:
        reply["reviewed_sha256"] = fm["reviewed_sha256"]
    return reply


def stored_reply_for_run(
    repo_root: Path,
    session_id: str,
    base: Optional[str],
    *,
    run_key: Optional[str] = None,
    within: Sequence[str] = (),
    covering: bool = False,
) -> Optional[dict]:
    """The reply ``review.prep`` stored for one run, found without its slice prefix, or ``None``.

    Globs the session's share dir for ``*-<base[:12]>-prep.review-prep.md`` (the suffix shape of
    ``execute_review.prep_slice_id_for``, not imported: that module is heavy). A file is kept when
    its name contains ``run_key`` (if given), its ``agent_type`` is ``engine:prep-freeze``, its
    ``run_base_sha`` is ``base`` and, when ``within`` is non-empty, every ``paths`` entry is in it.
    ``covering=True`` (keyword-only, default off) flips that check: a file is kept when every
    ``within`` entry is in its ``paths``, so a review that read extra paths is still found. It has
    no effect when ``within`` is empty.
    Exactly one kept file returns; none, several or an unreadable one return ``None``.

    ``base`` may be ``None`` only together with ``run_key``: the glob is then
    ``*-prep.review-prep.md`` and the base check is skipped, so two kept replies for one run
    (different bases) return ``None``. Trap: ``run_key`` is a substring match on the file name, so a
    run id that prefixes another run's id keeps both and returns ``None``."""
    if not session_id or not (base or run_key):
        return None
    share = Path(share_dir(str(repo_root), session_id))
    allowed = set(within)
    kept: List[dict] = []
    try:
        candidates = sorted(share.glob(f"*-{base[:12]}-prep.review-prep.md" if base else "*-prep.review-prep.md"))
    except OSError:
        return None
    for sidecar in candidates:
        if run_key and run_key not in sidecar.name:
            continue
        try:
            split = split_frontmatter(sidecar.read_text(encoding="utf-8"))
            fm = yaml.safe_load(split.fm_text) if split else None
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(fm, dict) or any(k not in fm for k in _STORED_KEYS):
            continue
        if fm.get("agent_type") != "engine:prep-freeze" or (base and fm["run_base_sha"] != base):
            continue
        paths = fm.get("paths")
        if allowed:
            if not isinstance(paths, list):
                continue
            if covering:
                if not allowed.issubset(paths):
                    continue
            elif not all(p in allowed for p in paths):
                continue
        entry = {**{k: fm[k] for k in _STORED_KEYS}, "error": None, "sidecar_path": _rel(sidecar, repo_root)}
        for optional in ("reviewed_sha256", "recorded_at"):
            if optional in fm:
                entry[optional] = fm[optional]
        kept.append(entry)
    return kept[0] if len(kept) == 1 else None


def prep_block(reply: dict) -> dict:
    """A stored reply in the run record's ``prep`` shape; ``slice_files`` is the ordered,
    deduplicated union of the dict slices' ``files``."""
    seen: dict = {}
    for s in reply.get("slices") or []:
        if isinstance(s, dict):
            for f in s.get("files") or []:
                seen.setdefault(f, None)
    return {
        "run_base_sha": reply.get("run_base_sha"),
        "product_files": reply.get("product_files"),
        "foreign_claims": reply.get("foreign_claims") or [],
        "slice_files": list(seen),
    }


def _claim(lock: Path) -> bool:
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < _LOCK_STALE_SECS:
                    return False
                lock.unlink()
            except OSError:
                return False
        except OSError:
            return False
    return False


def review_prep(
    repo_root: Path,
    *,
    base: str,
    paths: List[str],
    slice_prefix: str,
    plan_path: str,
    head: Optional[str] = None,
    session_id: str = "",
    group_index: Optional[int] = None,
    group_count: Optional[int] = None,
) -> dict:
    """Freeze, partition and claim-check one run's diff once per ``slice_prefix``; see the module
    docstring."""
    if not base or not paths or not slice_prefix or not plan_path:
        return _fail("base, paths, slice_prefix and plan_path are required")
    if not _SHA_RE.match(base):
        return _fail("base must be a lowercase commit sha")
    if (group_index is None) != (group_count is None):
        return _fail("group_index and group_count go together")
    if group_count is not None and not (group_count >= 1 and 0 <= group_index < group_count):
        return _fail("group_index must be in [0, group_count) and group_count at least 1")
    session = session_id or resolve_session_id(str(repo_root))
    if not session:
        return _fail("no session id: pass session_id or run under a session")

    share = Path(share_dir(str(repo_root), session))
    sidecar = share / f"{slice_prefix}.review-prep.md"
    lock = share / f"{slice_prefix}.review-prep.lock"

    def served() -> Optional[dict]:
        return _stored_reply(repo_root, sidecar, base, paths, head)

    def compute_locked() -> dict:
        try:
            return served() or _compute(
                repo_root, base, paths, slice_prefix, plan_path, head, session, share, sidecar
            )
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    reply = served()
    if reply is None:
        share.mkdir(parents=True, exist_ok=True)
        if _claim(lock):
            reply = compute_locked()
        else:
            deadline = time.monotonic() + _LOCK_WAIT_SECS
            while reply is None and lock.exists() and time.monotonic() < deadline:
                time.sleep(_LOCK_POLL_SECS)
                reply = served()
            reply = reply or served()
            if reply is None and not lock.exists() and _claim(lock):
                reply = compute_locked()
            if reply is None:
                reply = _fail(f"{REVIEW_PREP_BUSY_PREFIX} another caller holds the lock")
    return _for_group(reply, group_index, group_count)


def _compute(
    repo_root: Path,
    base: str,
    paths: List[str],
    slice_prefix: str,
    plan_path: str,
    head: Optional[str],
    session: str,
    share: Path,
    sidecar: Path,
) -> dict:
    plan_id = _plan_id(repo_root, plan_path)
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
    # Trap: hashed after the freeze, so a write landing between the two is recorded as reviewed
    # though the frozen diff lacks it; the moved-bytes check then serves that stale freeze.
    hashes = {rel: compute_content_hash(repo_root / rel) for rel in product}
    out["reviewed_sha256"] = {rel: h for rel, h in hashes.items() if h is not None}

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
    out["sidecar_path"] = _rel(sidecar, repo_root)
    _write_sidecar(sidecar, out, session, paths, head)
    return out


def _write_sidecar(sidecar: Path, result: dict, session: str, paths: List[str], head: Optional[str]) -> None:
    fm = {
        "agent_type": "engine:prep-freeze",
        "status": "complete",
        "lead_session_id": session,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        **{k: result[k] for k in _STORED_KEYS},
        "reviewed_sha256": result.get("reviewed_sha256", {}),
        "head": head or None,
        "paths": paths,
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
    committed range mode) or ``worktree: true``; optional ``group_index`` / ``group_count`` (ints,
    together) return only that slice group. Keying scope: show_top."""
    if repo_root is None:
        return _fail("review.prep requires a show_top-keyed dispatch; repo_root was not supplied")
    paths = params.get("paths")
    if not isinstance(paths, list):
        return _fail("review.prep requires params.paths as a list")
    head = str(params.get("head") or "")
    if bool(head) == bool(params.get("worktree")):
        return _fail("review.prep requires exactly one of head (range mode) or worktree=true")
    group_index, group_count = params.get("group_index"), params.get("group_count")
    if any(v is not None and (isinstance(v, bool) or not isinstance(v, int)) for v in (group_index, group_count)):
        return _fail("review.prep group_index and group_count must be integers")
    return review_prep(
        repo_root,
        base=str(params.get("base") or ""),
        paths=[str(p) for p in paths],
        slice_prefix=str(params.get("slice_prefix") or ""),
        plan_path=str(params.get("plan_path") or ""),
        head=head or None,
        session_id=str(params.get("session_id") or ""),
        group_index=group_index,
        group_count=group_count,
    )
