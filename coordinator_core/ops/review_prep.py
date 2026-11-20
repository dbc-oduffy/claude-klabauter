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

Peer hunks: ``review_peer_bases.peer_path_bases`` maps a declared path a peer run committed ahead of
this run's own work on it to that peer commit; the whole diff and the partition then take that path
from the mapped base, so the reviewer sees only this run's hunks. The map is stored in the sidecar as
``peer_bases`` (outside ``_STORED_KEYS``) and a stored reply is served only while a fresh map equals
it, which costs one ``git log`` spawn on the served path.

Computes once per ``slice_prefix``: the success sidecar stores the whole reply, and a later call
with the same prefix, base, basis, head and paths reads it back and spawns no git. Concurrent
first callers claim ``<slice_prefix>.review-prep.lock`` with ``O_EXCL``; the loser waits at most
``_LOCK_WAIT_SECS`` for the winner's sidecar, then returns it or a refusal whose ``error`` starts
with ``REVIEW_PREP_BUSY_PREFIX`` for the caller to re-call. No path computes without the lock.
Only a success is stored, so a refusal is recomputed.

The sidecar also stores ``reviewed_sha256`` (repo-relative POSIX product path -> sha256 of the
bytes frozen) beside ``recorded_at``; it feeds terminal_commit's blob-drift check, not serving.
A worktree-mode sidecar also stores ``declared_sha256`` (declared path -> hash, null when absent;
the whole value null when a declared entry is a magic pathspec or directory). A worktree-mode stored
reply is served only when that is a map and every entry still matches the bytes on disk, so a path
that was absent or unchanged at the freeze and written later recomputes, shelving the prior freeze.
Range mode ignores it. Trap: both keys are deliberately outside ``_STORED_KEYS``, so an older sidecar
without them stays readable by ``stored_reply_for_run`` (and recomputes in worktree mode).

Both modes write a write-once pre-review snapshot beside the sidecar (``review.reverify`` refuses a prefix without one):
``<slice_prefix>.pre-review/manifest.json`` (``base``, ``head``, ``recorded_at``, ``paths``, ``files``: product
path -> sha256) and one blob per distinct hash holding the bytes frozen. The reply names the manifest
as ``pre_review_manifest`` (outside ``_STORED_KEYS``, like ``reviewed_sha256``).

With ``run_id`` the handler also adds ``undeclared_edits``: paths dirty now with bytes the run's
``stage-dirt.json`` (``stage_dirt_snapshot``) did not hold, covered by no declared path and held by no
live peer. They join ``paths``, and the reply and sidecar list them as ``undeclared_edits``. A run
whose every declared path is an indexed file with no worktree change made no edit of its own, so it
adopts none: dirt elsewhere is a peer's.

Optional ``group_index`` / ``group_count`` (both or neither) narrow the reply's ``slices`` to the
one contiguous balanced group at ``group_index`` of ``min(slices, group_count)`` groups, the split
``execute_review._GROUP_SLICES_JS`` applies; every other field is the whole-run reply. Without
them ``slices`` is the full list.

``run_id`` (instead of ``paths``) resolves the declared paths via ``declared_paths_and_held`` (peer-held paths included): the run
manifest's ``review_declared_paths``, plus the leg footprint's ``writes`` only when ``leg`` is true.
``leg`` is explicit because ``leg-footprint.json`` outlives the ledger resume that wrote it, so
presence cannot signal this fire has a leg. A run_id resolving to no path is a refusal naming the
run, never the empty-``paths`` zero-file reply.

Negative-spec: a single-reviewer-ok verdict over product files returns ONE whole-diff slice,
never an empty list; zero product files returns no slices. Empty ``paths`` returns the zero-file
reply (no slices, ``product_files`` 0, no sidecar, lock or git) rather than a refusal. A refusal from either composed op is
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
    ".coordinator-local/subagent-share/*/*.review-prep-ptr.json",
    ".coordinator-local/subagent-share/*/*.review-prep.lock",
    ".coordinator-local/subagent-share/*/*.pre-review/*",
]

import hashlib
import json
import os
import posixpath
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.frontmatter.yaml_load import safe_load as _safe_load
from coordinator_core.git import git_state
from coordinator_core.git.content_hash import eol_folded_sha256
from coordinator_core.git.literal_pathspec import git_pathspec, is_magic_pathspec
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops import review_prep_legs
from coordinator_core.ops.dispatch_emit.record_paths import is_record_path
from coordinator_core.ops.dispatch_emit.run_ownership import LiveRun, blocks, holders_from_held, owns_path
from coordinator_core.ops.pre_review_dir import pre_review_dir
from coordinator_core.ops.review_slice_id import RUN_PREP_PLAN_STEM
from coordinator_core.session import claim_index
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.session.touch_record import compute_content_hash

_SHA_RE = re.compile(r"^[0-9a-f]{4,64}$")
_GIT_TIMEOUT_SECS = 5.0

_LAZY_HEAVY = {
    "freeze_diff": ("coordinator_core.ops.review_freeze_diff", "freeze_diff"),
    "partition_slices": ("coordinator_core.ops.review_partition_slices", "partition_slices"),
    "diff_product_files": ("coordinator_core.ops.review_stamp", "product_files"),
}


def _heavy(name: str):
    """The freeze/partition/product-files callable, imported on first use and cached as a module
    global. A module global already set (a test patch) wins; `import review_prep` alone, as
    `stored_reply_for_run` callers do, never loads their closure."""
    got = globals().get(name)
    if got is None:
        import importlib

        module, attr = _LAZY_HEAVY[name]
        got = globals()[name] = getattr(importlib.import_module(module), attr)
    return got


def __getattr__(name: str):
    if name in _LAZY_HEAVY:
        return _heavy(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        "excluded": [],
        "peer_held": [],
        "review_skip": None,
        "error": message,
    }


#: ``(path, mtime_ns, size) -> parsed frontmatter`` of the last plan read: ``_plan_id`` and the peer-bases
#: lookup read the same plan within one prep. Keyed on the file's stat, so an edited plan is re-read.
#: Holds one entry, so a warm engine never accumulates plans.
_PLAN_DOC: Dict[tuple, object] = {}


def _plan_doc(plan_file: Path) -> object:
    """The plan's parsed frontmatter (any YAML value, ``None`` when absent or unreadable)."""
    try:
        st = plan_file.stat()
        key = (str(plan_file), st.st_mtime_ns, st.st_size)
        if key in _PLAN_DOC:
            return _PLAN_DOC[key]
        split = split_frontmatter(plan_file.read_text(encoding="utf-8", errors="replace"))
        doc = _safe_load(split.fm_text) if split else None
    except (OSError, yaml.YAMLError):
        return None
    _PLAN_DOC.clear()
    _PLAN_DOC[key] = doc
    return doc


def _plan_frontmatter_str(repo_root: Path, plan_path: str, key: str) -> Optional[str]:
    """The plan's frontmatter string ``key``, or ``None`` when absent, blank or unreadable."""
    plan_file = Path(plan_path)
    if not plan_file.is_absolute():
        plan_file = repo_root / plan_file
    doc = _plan_doc(plan_file)
    value = doc.get(key) if isinstance(doc, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _plan_id(repo_root: Path, plan_path: str) -> str:
    """The plan's frontmatter ``plan_id``, else its file stem."""
    return _plan_frontmatter_str(repo_root, plan_path, "plan_id") or Path(plan_path).stem


def _unparseable_py_at_native(repo_root: Path, head: str, paths: List[str]) -> Optional[list]:
    """`unparseable_py_at` with the blobs read from the object store in process (no `cat-file` spawn).
    None when `head` is not a full sha or a tree or blob is unreadable here: the caller asks git."""
    wanted = [p for p in paths if p.endswith(".py")]
    if not wanted:
        return []
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.ops.dispatch_emit.landing_halted_view import tree_entries
    from coordinator_core.ops.dispatch_emit.landing_integrity import _cached_parse_finding

    try:
        entries = tree_entries(repo_root, head, wanted)
        if entries is None:
            return None
        common = resolve_git_common_dir(repo_root)
        out = []
        for rel in wanted:
            if rel not in entries:
                continue
            blob = read_object(common, entries[rel][1])
            if blob is None or blob[0] != "blob":
                return None
            found = _cached_parse_finding(repo_root, rel, blob[1])
            if found:
                out.append(found)
        return out
    except Exception:  # noqa: BLE001 -- undecided here: git decides
        return None


def _peer_bases(repo_root: Path, base: str, head: Optional[str], paths: List[str], session: str, plan_path: str) -> dict:
    """Declared path -> effective base sha for every path a peer run committed ahead of this run's own work."""
    from coordinator_core.ops.review_peer_bases import peer_path_bases

    return peer_path_bases(
        repo_root, base, head, paths, session, _plan_frontmatter_str(repo_root, plan_path, "deliverable_id")
    )


def _with_no_delta_bases(
    repo_root: Path, base: str, head: Optional[str], paths: List[str], run_id: str, found: dict
) -> dict:
    """``found`` plus a base for each declared path with no ``base..head`` delta: a live revise note's
    named commit, and (only when every declared path is no-delta, the zero-slice halt shape) the first
    parent of the commit that introduced it unreviewed. Named wins over introducer; a peer base is never
    overridden. Range mode only; a degraded or non-``PeerBases`` result leaves ``found`` as is."""
    from coordinator_core.ops.review_peer_bases import PeerBases

    if not head or not run_id or not isinstance(found, PeerBases) or found.degraded:
        return found
    missing = [p for p in paths if not is_magic_pathspec(p) and not any(c == p or c.startswith(p.rstrip("/") + "/") for c in found.changed)]
    if not missing:
        return found
    from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot

    # A path with uncommitted bytes has a delta the partition measures HEAD->worktree; rebasing it
    # to an introducer's parent would freeze the whole file. Unprovable (no stat scan) keeps `found`.
    scanned = stage_dirt_snapshot._stat_dirty(repo_root)
    if scanned is None:
        return found
    dirty, entries = scanned
    clean = [p for p in missing if p.replace("\\", "/") in entries and p.replace("\\", "/") not in dirty]
    if not clean:
        return found
    all_no_delta = len(clean) == len(paths)
    missing = clean
    from coordinator_core.ops.review_named_base import named_review_base
    from coordinator_core.ops.review_unreviewed_bases import unreviewed_introducer_bases

    extra: Dict[str, str] = {}
    listed: set = set()
    if all_no_delta:
        extra.update(unreviewed_introducer_bases(repo_root, base, missing, listed))
    extra.update(named_review_base(repo_root, run_id, missing, listed))
    extra = {p: b for p, b in extra.items() if p not in found and b != base}
    if not extra:
        return found
    merged = PeerBases({**found, **extra})
    merged.changed = found.changed
    merged.degraded = found.degraded
    return merged


def _specs_by_base(paths: List[str], base: str, path_bases: dict) -> List[tuple]:
    """``(pathspec, effective base)`` per declared path, in declared order. Peer bases are keyed by
    file, so a declared directory holding keyed files stays on the run base minus those files
    (``:(exclude)`` entries) and each keyed file follows it on its own peer base."""
    from coordinator_core.ops.review_partition_slices import base_resolver

    effective_base = base_resolver(base, path_bases)
    out: List[tuple] = []
    for p in paths:
        eb = effective_base(p)
        out.append((p, eb))
        if eb != base:
            continue
        prefix = p.rstrip("/") + "/"
        inner = sorted(k for k in path_bases if k.startswith(prefix))
        out.extend((f":(exclude){k}", eb) for k in inner)
        out.extend((k, path_bases[k]) for k in inner)
    return out


def _plain_paths_by_base(repo_root: Path, specs: List[tuple]) -> Optional[Dict[str, List[str]]]:
    """``{effective base: [path]}`` when every spec is a plain file path, else None (magic, glob, directory)."""
    by_base: Dict[str, List[str]] = {}
    for p, eb in specs:
        if is_magic_pathspec(p) or p.endswith("/") or any(c in p for c in "*?") or (repo_root / p).is_dir():
            return None
        by_base.setdefault(eb, []).append(p)
    return by_base


def _merged_base(repo_root: Path, specs: List[tuple]) -> Optional[str]:
    """One rev whose worktree diff over every spec equals the per-base diffs, or None: the caller
    diffs per base. Either a base the other bases' paths are unchanged at (nothing written), else
    ``_sparse_base_tree``."""
    from coordinator_core.git.ref_cas_retry import paths_unchanged_between

    by_base = _plain_paths_by_base(repo_root, specs)
    if by_base is None:
        return None
    for cand in by_base:
        if all(
            paths_unchanged_between(repo_root, eb, cand, group) is True
            for eb, group in by_base.items()
            if eb != cand
        ):
            return cand
    return _sparse_base_tree(repo_root, by_base)


def _sparse_base_tree(repo_root: Path, by_base: Dict[str, List[str]]) -> Optional[str]:
    """Sha of a tree holding each path's blob at its own base (``by_base``), written in process (no
    spawn); ``git diff <tree> -- <paths>`` then diffs every path from its own base. None when a
    base tree is unreadable or a path is a gitlink or directory at its base."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import build_tree
    from coordinator_core.git.ref_cas_retry import _root_tree

    entries: dict = {}
    for eb, group in by_base.items():
        tree = _root_tree(repo_root, eb)
        spine = git_state.read_tree_spine(repo_root, group, root_tree_sha=tree) if tree else None
        if spine is None:
            return None
        for p in group:
            parent, _, name = p.rpartition("/")
            entry = spine.get(parent, {}).get(name)
            if entry is None:
                continue
            if entry[0] in (0o40000, 0o160000):
                return None
            entries[p] = entry
    try:
        return build_tree(resolve_git_common_dir(repo_root), entries)
    except (OSError, TimeoutError):
        return None


def _freeze_whole_by_base(
    repo_root: Path,
    base: str,
    head: Optional[str],
    paths: List[str],
    path_bases: dict,
    slice_prefix: str,
    fallback: Optional[List[str]] = None,
    worktree_paths: Optional[List[str]] = None,
) -> dict:
    """Freeze the whole diff with each path taken from its effective base, as the ``freeze_diff``
    reply shape for ``<slice_prefix>.diff``. ``fallback`` (range mode) paths have no range delta;
    they are frozen HEAD->worktree under ``<slice_prefix>-gfb-prep`` and concatenated last.

    One ``freeze_diff`` per distinct base under ids ``<slice_prefix>-g<n>-prep`` (distinct paths
    cannot share a ``freeze_diffs_batch`` call, and worktree mode never batches), concatenated in
    declared-path order. A path with nothing left after its peer base (peer-only work) drops out of
    its group; a path on the run base keeps ``freeze_diff``'s own refusals.

    Worktree mode over plain file paths with several bases is one ``freeze_diff`` (one ``git diff``)
    under ``<slice_prefix>-g1-prep`` against ``_merged_base``; the stanzas equal the per-base diffs."""
    from coordinator_core.ops.review_freeze_diff import _is_run_keyed_prep_id, _shelve_prior_attempt

    groups: dict = {}
    wt_set = set(worktree_paths or [])
    specs = _specs_by_base(paths, base, path_bases)
    for spec, eb in specs:
        # `worktree_paths` carry uncommitted bytes on top of a range delta: frozen base..worktree.
        groups.setdefault((eb, spec in wt_set), []).append(spec)
    texts: List[str] = []
    head_sha: Optional[str] = None
    if not head and not fallback and len(groups) > 1:
        merged_base = _merged_base(repo_root, specs)
        if merged_base:
            peer_keyed = {p for p, eb in specs if eb != base}
            merged_paths = list(paths)
            while True:
                try:
                    frozen = _heavy("freeze_diff")(repo_root, merged_base, f"{slice_prefix}-g1-prep", merged_paths, worktree=True)
                except ValueError:
                    frozen = {"error": "sparse base diff failed"}
                # A peer-base path with no delta left is uncovered: drop it, as the per-base loop does.
                drop = [p for p in (frozen.get("uncovered_paths") or []) if p in peer_keyed]
                if not frozen.get("error") or not drop:
                    break
                merged_paths = [p for p in merged_paths if p not in drop]
                if not merged_paths:
                    frozen = {"error": "no delta left"}
                    break
            if not frozen.get("error"):
                groups = {}
                head_sha = f"{base} worktree"
                texts.append(Path(frozen["diff_path"]).read_text(encoding="utf-8"))
    for n, ((eb, in_wt), group) in enumerate(groups.items(), 1):
        slice_id = f"{slice_prefix}-g{n}-prep"
        while group:
            if head and not in_wt:
                frozen = _heavy("freeze_diff")(repo_root, f"{eb}..{head}", slice_id, group)
            else:
                frozen = _heavy("freeze_diff")(repo_root, eb, slice_id, group, worktree=True)
            err = frozen.get("error")
            if not err or eb == base:
                break
            uncovered = frozen.get("uncovered_paths") or []
            if uncovered:
                group = [p for p in group if p not in uncovered]
            elif "ZERO commits" in err:  # couples to freeze_diff's peer-only refusal wording; untested
                group = []
            else:
                break
        if not group:
            continue
        if frozen.get("error"):
            return frozen
        head_sha = head_sha or frozen["head_sha"]
        texts.append(Path(frozen["diff_path"]).read_text(encoding="utf-8"))
    if fallback:
        frozen = _heavy("freeze_diff")(
            repo_root, git_state.head_sha(repo_root), f"{slice_prefix}-gfb-prep", fallback, worktree=True
        )
        if frozen.get("error"):
            return frozen
        head_sha = head_sha or frozen["head_sha"]
        texts.append(Path(frozen["diff_path"]).read_text(encoding="utf-8"))
    diffs_dir = repo_root / "state" / "review-trail" / "diffs"
    diff_path, sha_path = diffs_dir / f"{slice_prefix}.diff", diffs_dir / f"{slice_prefix}.head.sha"
    text = "".join(texts)
    if diff_path.exists() and diff_path.read_bytes().decode("utf-8") != text:
        if not _is_run_keyed_prep_id(slice_prefix):
            return {"error": f"slice_id '{slice_prefix}' already names a frozen diff at {diff_path} with different content"}
        _shelve_prior_attempt(diff_path, sha_path)
    diff_path.write_text(text, encoding="utf-8", newline="\n")
    declare_write(diff_path)
    sha_path.write_text((head_sha or head or f"{base} worktree") + "\n", encoding="utf-8", newline="\n")
    declare_write(sha_path)
    return {"diff_path": str(diff_path), "error": None}


def foreign_claims(
    repo_root: Path,
    rev_range: str,
    paths: List[str],
    own_session: str,
    scanned: Optional[frozenset] = None,
    dirty: Optional[frozenset] = None,
) -> List[str]:
    """Declared `paths` changed by a commit in `rev_range`, or dirty in the worktree (`dirty`, an
    already-computed set: no spawn), that a live session other than `own_session` claims. `scanned`
    (the peer-base log's changed set) answers the commit side with no spawn; without it, one `git log`
    spawn; none when `paths` is empty."""
    if not paths:
        return []
    if scanned is not None:
        changed = sorted(scanned | dirty) if dirty else sorted(scanned)
    else:
        proc = run_git(
            ["-C", str(repo_root), "log", "--name-only", "--format=", rev_range, "--", *map(git_pathspec, paths)],
            timeout=_GIT_TIMEOUT_SECS,
        )
        if proc.returncode != 0:
            return []
        changed = sorted({ln.strip() for ln in proc.stdout.splitlines() if ln.strip()} | set(dirty or ()))
    if not changed:
        return []
    claims = claim_index.lookup(changed, cwd=str(repo_root))
    return [
        p
        for p in changed
        if any(c not in (own_session, claim_index.UNANSWERABLE) for c in claims.get(p, []))
    ]


def _untracked_under_declared(repo_root: Path, paths: List[str]) -> List[str]:
    """Untracked, non-ignored files under declared directory or magic pathspecs. The worktree freeze
    synthesizes a new-file hunk only for a literal declared file, so a directory declaration would
    otherwise drop its untracked members. One `git ls-files` spawn, none when every path is a file."""
    wide = [p for p in paths if is_magic_pathspec(p) or p.endswith("/") or (repo_root / p).is_dir()]
    if not wide:
        return []
    proc = run_git(
        ["-C", str(repo_root), "--no-optional-locks", "ls-files", "--others", "--exclude-standard", "-z",
         "--", *map(git_pathspec, wide)],
        timeout=_GIT_TIMEOUT_SECS,
    )
    if proc.returncode != 0:
        raise ValueError(f"untracked-file listing under declared paths failed (rc {proc.returncode})")
    have = set(paths)
    return sorted({p for p in proc.stdout.split("\0") if p and p not in have})


def live_runs_from_params(raw: object) -> Optional[dict]:
    """Parse ``{run_id: {plan, superseded_by, successor_landed, withheld, claims}}`` into ``LiveRun``s; None when absent or malformed."""
    if not isinstance(raw, dict) or not raw:
        return None
    out = {}
    for rid, run in raw.items():
        if not isinstance(run, dict):
            return None
        out[str(rid)] = LiveRun(
            plan=str(run.get("plan") or ""),
            superseded_by=str(run.get("superseded_by") or ""),
            successor_landed=bool(run.get("successor_landed")),
            withheld=frozenset(str(p) for p in run.get("withheld") or []),
            claims=frozenset(str(p) for p in run.get("claims") or []),
        )
    return out


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
    "foreign_claims", "slices", "whole_diff_sidecars", "sidecar_path", "excluded", "review_skip",
)
REVIEW_PREP_BUSY_PREFIX = "review.prep busy:"
_LOCK_WAIT_SECS = 0.5
# 20x the wait: a live winner holding the lock this long is an over-bar op; a dead one clears fast.
_LOCK_STALE_SECS = 10.0
_LOCK_POLL_SECS = 0.05


def _declared_hashes(repo_root: Path, paths: List[str], known: Optional[dict] = None) -> Optional[dict]:
    """Declared path -> content hash (``None`` when absent), or ``None`` when any entry is a magic
    pathspec or a directory: such a declaration cannot be proven unmoved, so it is never served.
    ``known`` maps paths already hashed with ``eol_folded_sha256`` (same function as
    ``compute_content_hash``) to that hash; those files are not read again."""
    out: dict = {}
    for entry in paths:
        if is_magic_pathspec(entry) or entry.endswith("/") or (repo_root / entry).is_dir():
            return None
        key = entry.replace("\\", "/")
        out[key] = (known or {}).get(key) or compute_content_hash(repo_root / entry)
    return out


def _declared_bytes_unmoved(repo_root: Path, declared: object) -> bool:
    """True when ``declared`` is a ``declared_sha256`` map and every entry still hashes equal
    (absent still equals absent)."""
    if not isinstance(declared, dict):
        return False
    return all(compute_content_hash(repo_root / rel) == digest for rel, digest in declared.items())


def _stored_reply(
    repo_root: Path,
    sidecar: Path,
    base: str,
    paths: List[str],
    head: Optional[str],
    peer_bases: Optional[Callable[[], dict]] = None,
    run_id: str = "",
    committed_only: bool = False,
) -> Optional[dict]:
    """The reply a success sidecar stored, or ``None`` when absent, unreadable, written before
    replies were stored, written for another base, basis, head or path list, (with ``run_id``)
    not stamped with that run id (every manifest-driven run shares ``plan_id`` ``manifest``), (worktree mode)
    lacking a ``declared_sha256`` map or recorded over declared bytes that have since moved, or
    (when ``peer_bases`` is given) recorded under a ``peer_bases`` map that differs from the one it
    returns now: a peer commit can land without moving the declared bytes."""
    try:
        split = split_frontmatter(sidecar.read_text(encoding="utf-8"))
        fm = _safe_load(split.fm_text) if split else None
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(fm, dict) or any(k not in fm for k in _STORED_KEYS):
        return None
    if (
        fm["run_base_sha"] != base
        or fm["basis"] != ("range" if head else "worktree")
        or fm.get("head") != (head or None)
        or fm.get("paths") != paths
        or (run_id and fm.get("run_id") != run_id)
        or bool(fm.get("committed_only")) != committed_only
    ):
        return None
    if not head and not _declared_bytes_unmoved(repo_root, fm.get("declared_sha256")):
        return None
    if peer_bases is not None and (fm.get("peer_bases") or {}) != peer_bases():
        return None
    reply = {**{k: fm[k] for k in _STORED_KEYS}, "error": None}
    for pinned in ("reviewed_sha256", VERDICT_JUDGED_KEY):
        if pinned in fm:
            reply[pinned] = fm[pinned]
    reply["peer_held"] = fm.get("peer_held") or []
    reply["undeclared_edits"] = fm.get("undeclared_edits") or []
    return reply


VERDICT_JUDGED_KEY = "judged_sha256"


def slice_pins(reviewed: Mapping, sliced: Sequence[str]) -> Dict[str, str]:
    """``{path: sha256}`` of every file in the review slices, as hashed at review time: the producer
    of the verdict pin. A sliced file the prep could not read has no entry (nothing to pin)."""
    return {rel: reviewed[rel] for rel in dict.fromkeys(sliced) if isinstance(reviewed.get(rel), str)}


def judged_mismatches(repo_root: Path, judged: Mapping, paths: Sequence[str]) -> List[str]:
    """Sorted ``paths`` whose bytes now match none of the sha256 the verdicts recorded for them.

    ``judged`` maps a repo-relative POSIX path to one sha256 or a collection of them (one per
    verdict that judged it): the path stands when any recorded sha equals its current
    ``compute_content_hash``. A path with no recorded sha, a path absent from the tree and a path
    that cannot be read are no mismatch: nothing was pinned, or nothing can be compared.
    A sidecar's own ``VERDICT_JUDGED_KEY`` map names the bytes judged (re-hashed after any fix);
    otherwise the prep's own ``VERDICT_JUDGED_KEY`` (``slice_pins``) is the pin."""
    out: List[str] = []
    for rel in dict.fromkeys(paths):
        recorded = judged.get(rel)
        many = recorded if isinstance(recorded, (list, tuple, set, frozenset)) else ()
        wanted = {recorded} if isinstance(recorded, str) else {s for s in many if isinstance(s, str)}
        wanted.discard("")
        if not wanted:
            continue
        now = compute_content_hash(repo_root / rel)
        if now is not None and now not in wanted:
            out.append(rel)
    return sorted(out)


_POINTER_SUFFIX = ".review-prep-ptr.json"
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _pointer_name(run_key: Optional[str], base: Optional[str]) -> str:
    """The fixed file name of the pointer to the reply stored for ``run_key`` at ``base``."""
    if run_key:
        safe = _UNSAFE_NAME.sub("-", run_key)
        return f"ptr-run-{safe}-{base[:12]}{_POINTER_SUFFIX}" if base else f"ptr-run-{safe}{_POINTER_SUFFIX}"
    return f"ptr-base-{(base or '')[:12]}{_POINTER_SUFFIX}"


def _read_pointer(path: Path) -> Optional[dict]:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _named_for_run(name: str, run_key: Optional[str], base: Optional[str]) -> bool:
    """True when the pointer-named sidecar ``name`` may serve this selection. A run-owned
    ``manifest-<run key>-<base[:12]>-prep`` sidecar serves only its own run key (and base when
    given); a run-less selection never takes one; other names pass."""
    if not name.startswith(f"{RUN_PREP_PLAN_STEM}-"):
        return True
    if not run_key:
        return False
    safe = re.escape(_UNSAFE_NAME.sub("-", run_key))
    tail = re.escape(base[:12]) if base else "[0-9a-f]{12}"
    return re.fullmatch(rf"{RUN_PREP_PLAN_STEM}-{safe}-{tail}-prep\.review-prep\.md", name) is not None


def _write_pointers(share: Path, sidecar: Path, base: str, slice_prefix: str, plan_path: str) -> None:
    """Point the run-keyed, run-only and base-only names at ``sidecar``. A name already pointing at
    another existing sidecar is marked ``ambiguous``, which the reader answers ``None``. Written
    only for a prefix of the ``<plan stem>-<run key>-<base[:12]>-prep`` shape. A run-owned
    ``manifest-`` prefix writes no base-only pointer: that name is shared by every run at the base."""
    tail = f"-{base[:12]}-prep"
    if not slice_prefix.endswith(tail):
        return
    stem = _UNSAFE_NAME.sub("-", Path(plan_path.replace("\\", "/")).stem)
    head = slice_prefix[: -len(tail)]
    run_key = head[len(stem) + 1:] if head.startswith(stem + "-") else ""
    names = set() if slice_prefix.startswith(f"{RUN_PREP_PLAN_STEM}-") else {_pointer_name(None, base)}
    if run_key:
        names.update({_pointer_name(run_key, base), _pointer_name(run_key, None)})
    for name in sorted(names):
        path = share / name
        old = _read_pointer(path)
        other = old.get("sidecar") if old else None
        ambiguous = bool(old and old.get("ambiguous")) or (
            isinstance(other, str) and other != sidecar.name and (share / other).exists()
        )
        atomic_write_bytes(path, json.dumps({"sidecar": sidecar.name, "ambiguous": ambiguous}).encode("utf-8"))


def _covered_or_not_owed(wanted: set, stored: Sequence[str]) -> bool:
    """True when every ``wanted`` path is in ``stored`` or is not review-owed by the floor's own
    classifier: a landing's extra bookkeeping or doc path never voids a reply that read the code."""
    missing = wanted.difference(stored)
    if not missing:
        return True
    from coordinator_core.ops.dispatch_emit import review_floor

    return not review_floor.review_owed_paths(missing)


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

    Never walks the share dir: it opens the run-keyed pointer ``_write_pointers`` wrote when the
    reply was stored (``ptr-run-<run_key>-<base[:12]>``, ``ptr-run-<run_key>`` when ``base`` is
    ``None``, ``ptr-base-<base[:12]>`` when ``run_key`` is ``None``) and the sidecar it names, plus the
    ask route's canonical ``manifest-<run_key>-<base[:12]>-prep`` sidecar. A run-owned reply writes
    no ``ptr-base-`` pointer, and a pointer naming another run's ``manifest-`` sidecar is not taken.
    A candidate is kept when
    its ``agent_type`` is ``engine:prep-freeze``, its ``run_base_sha`` is ``base`` and, when
    ``within`` is non-empty, every ``paths`` entry is in it.
    ``covering=True`` (keyword-only, default off) flips that check: a file is kept when every
    ``within`` entry is in its ``paths``, so a review that read extra paths is still found. It has
    no effect when ``within`` is empty.
    With ``run_key``, a file stamped with a different ``run_id`` is dropped; one with no ``run_id``
    (stored before the stamp existed) is still kept.
    Exactly one kept file returns; none, several or an unreadable one return ``None``.

    ``base`` may be ``None`` only together with ``run_key``: the base check is skipped, so two
    replies for one run (different bases) return ``None``. Trap: a reply stored by an engine that
    wrote no pointer, under a plan-route prefix, is not found."""
    if not session_id or not (base or run_key):
        return None
    share = Path(share_dir(str(repo_root), session_id))
    allowed = set(within)
    kept: List[dict] = []
    pointer = _read_pointer(share / _pointer_name(run_key, base))
    if pointer is not None and pointer.get("ambiguous"):
        return None
    candidates: List[Path] = []
    named = pointer.get("sidecar") if pointer else None
    if isinstance(named, str) and named and Path(named).name == named and _named_for_run(named, run_key, base):
        candidates.append(share / named)
    if run_key and base:
        from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

        canonical = share / f"{run_prep_slice_prefix(run_key, base)}.review-prep.md"
        if canonical not in candidates and canonical.is_file():
            candidates.append(canonical)
    for sidecar in candidates:
        try:
            split = split_frontmatter(sidecar.read_text(encoding="utf-8"))
            fm = _safe_load(split.fm_text) if split else None
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(fm, dict) or any(k not in fm for k in _STORED_KEYS):
            continue
        if fm.get("agent_type") != "engine:prep-freeze" or (base and fm["run_base_sha"] != base):
            continue
        if run_key and fm.get("run_id") not in (None, run_key):
            continue
        paths = fm.get("paths")
        if allowed:
            if not isinstance(paths, list):
                continue
            if covering:
                if not _covered_or_not_owed(allowed, paths):
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
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
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


def _inside_repo(path: str) -> bool:
    """False for a path git cannot pathspec in this repo: absolute, drive-lettered, or escaping via ``..``
    (a withheld cross-repo row's write)."""
    norm = posixpath.normpath(path.replace("\\", "/"))
    return not (norm == ".." or norm.startswith("../") or norm.startswith("/") or PureWindowsPath(path).drive)


def _record_run_sidecar(repo_root: Path, run_id: str, sidecar: Path) -> None:
    """Ledger the sidecar under the run so its close leg reaps it; never raises into review.prep."""
    try:
        from coordinator_core.ops.dispatch_emit import run_scratch

        run_scratch.record_paths(repo_root, run_id, [_rel(sidecar, repo_root)])
    except (OSError, ValueError):  # recording is best-effort: a failed record must not fail the prep
        pass


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
    run_id: str = "",
    live_runs: Optional[Mapping[str, LiveRun]] = None,
    undeclared_edits: Sequence[str] = (),
    peer_held: Sequence[str] = (),
    own_hunk_bytes: Optional[Mapping[str, bytes]] = None,
    committed_only: bool = False,
) -> dict:
    """Freeze, partition and claim-check one run's diff once per ``slice_prefix``; see the module
    docstring. ``committed_only`` (range mode only) freezes exactly ``base..head``: no declared path
    is measured or frozen from working-tree bytes, so a peer's uncommitted hunk in a shared tree
    never enters the slice (a path with no range delta is excluded ``no-delta``). ``own_hunk_bytes`` maps a worktree-mode path that also carries a halted peer's hunks to
    its HEAD bytes plus only the run's own hunks: the frozen diff holds that view for the path.
    ``undeclared_edits`` names the members of ``paths`` that no row declared and that
    changed since the stage dirt snapshot; the reply and sidecar carry them as ``undeclared_edits``.
    ``peer_held`` names members of ``paths`` a live session touches; they stay in review and ride on
    the reply's ``peer_held`` next to those ``live_runs`` withholds. An unparseable ``.py`` among
    ``undeclared_edits`` never refuses: it leaves review and rides on the reply's ``foreign_unparseable``.
    With ``live_runs``, a path another live run withholds or claims stays in review and is
    listed in ``peer_held``; the peer-hunks gate settles it at commit. The handler derives ``live_runs``
    from halted runs' withheld sets (``land_run.foreign_halted_withheld``) when ``params.run_id`` is
    given and ``params.live_runs`` is not. ``excluded`` lists each declared path not reviewed with its
    reason (``outside-repo``, ``no-delta``)."""
    if not base or not slice_prefix or not plan_path:
        return _fail("base, paths, slice_prefix and plan_path are required")
    if not _SHA_RE.match(base):
        return _fail("base must be a lowercase commit sha")
    if committed_only and not head:
        return _fail("committed_only requires head (range mode)")
    if (group_index is None) != (group_count is None):
        return _fail("group_index and group_count go together")
    if group_count is not None and not (group_count >= 1 and 0 <= group_index < group_count):
        return _fail("group_index must be in [0, group_count) and group_count at least 1")
    excluded = [{"path": p, "reason": "outside-repo"} for p in paths if not _inside_repo(p)]
    paths = [posixpath.normpath(p.replace("\\", "/")) if ".." in p else p for p in paths if _inside_repo(p)]
    run_held = [p for p in paths if blocks(owns_path(run_id, p, live_runs, plan_path))] if live_runs else []
    peer_held = [*run_held, *(p for p in peer_held if p in paths and p not in run_held)]
    if not paths:
        # A pathspec-less `git diff <base>` would diff the whole tree: answer before any git, lock or sidecar.
        zero = _fail("")
        zero.update(
            plan_id=_plan_id(repo_root, plan_path),
            share_dir=_rel(Path(share_dir(str(repo_root), session_id)), repo_root) if session_id else "",
            basis="range" if head else "worktree",
            run_base_sha=base,
            excluded=excluded,
            error=None,
        )
        return _for_group(zero, group_index, group_count)
    if all(is_record_path(p) for p in paths):
        # Records are never product: answer a typed skip before any git, lock or sidecar.
        skip = _fail("")
        skip.update(
            plan_id=_plan_id(repo_root, plan_path),
            share_dir=_rel(Path(share_dir(str(repo_root), session_id)), repo_root) if session_id else "",
            basis="range" if head else "worktree",
            run_base_sha=base,
            excluded=excluded,
            peer_held=peer_held,
            review_skip={"kind": "records-only", "paths": list(paths)},
            error=None,
        )
        return _for_group(skip, group_index, group_count)
    from coordinator_core.ops.dispatch_emit.landing_integrity import unparseable_py, unparseable_py_at

    from coordinator_core.ops.review_peer_bases import _has_object_store

    held = set(peer_held)
    own_paths = [p for p in paths if p not in held]
    with review_prep_legs.leg("parse_gate"):
        if head:
            broken = []
            if _has_object_store(repo_root):
                broken = _unparseable_py_at_native(repo_root, head, own_paths)
                if broken is None:
                    broken = unparseable_py_at(repo_root, head, own_paths)
        else:
            broken = unparseable_py(repo_root, own_paths)
    undeclared_set = set(undeclared_edits)
    foreign_broken = [f for f in broken if f.path in undeclared_set]
    broken = [f for f in broken if f.path not in undeclared_set]
    if broken:
        return _fail(
            "review.prep refused: py-unparseable: " + "; ".join(f"{f.path} {f.detail}" for f in broken)
        )
    foreign_unparseable = [{"path": f.path, "detail": f.detail} for f in foreign_broken]
    if foreign_unparseable:
        dropped = {f["path"] for f in foreign_unparseable}
        paths = [p for p in paths if p not in dropped]
        peer_held = [p for p in peer_held if p not in dropped]
        undeclared_edits = [p for p in undeclared_edits if p not in dropped]
        if not paths:
            zero = _fail("")
            zero.update(
                plan_id=_plan_id(repo_root, plan_path),
                share_dir=_rel(Path(share_dir(str(repo_root), session_id)), repo_root) if session_id else "",
                basis="range" if head else "worktree",
                run_base_sha=base,
                excluded=excluded,
                error=None,
                foreign_unparseable=foreign_unparseable,
            )
            return _for_group(zero, group_index, group_count)
    reply = _prepare(
        repo_root, base, paths, slice_prefix, plan_path, head, session_id, group_index, group_count,
        run_id, excluded, peer_held, undeclared_edits, own_hunk_bytes, committed_only,
    )
    if foreign_unparseable and not reply.get("error"):
        reply = {**reply, "foreign_unparseable": foreign_unparseable}
    return reply


def _prepare(
    repo_root: Path,
    base: str,
    paths: List[str],
    slice_prefix: str,
    plan_path: str,
    head: Optional[str],
    session_id: str,
    group_index: Optional[int],
    group_count: Optional[int],
    run_id: str,
    excluded: List[dict],
    peer_held: List[str],
    undeclared_edits: Sequence[str],
    own_hunk_bytes: Optional[Mapping[str, bytes]] = None,
    committed_only: bool = False,
) -> dict:
    """The claim, serve-or-compute and group-slice tail of ``review_prep`` once the parse gate passed."""
    session = session_id or resolve_session_id(str(repo_root))
    if not session:
        return _fail("no session id: pass session_id or run under a session")

    share = Path(share_dir(str(repo_root), session))
    sidecar = share / f"{slice_prefix}.review-prep.md"
    lock = share / f"{slice_prefix}.review-prep.lock"
    if run_id:
        _record_run_sidecar(repo_root, run_id, sidecar)

    fresh_bases: List[dict] = []

    def peer_bases() -> dict:
        if not fresh_bases:
            with review_prep_legs.leg("peer_bases"):
                found = _peer_bases(repo_root, base, head, paths, session, plan_path)
                fresh_bases.append(_with_no_delta_bases(repo_root, base, head, paths, run_id, found))
        return fresh_bases[0]

    def served() -> Optional[dict]:
        with review_prep_legs.leg("stored_reply"):
            return _stored_reply(repo_root, sidecar, base, paths, head, peer_bases, run_id, committed_only)

    def compute_locked() -> dict:
        try:
            return served() or _compute(
                repo_root, base, paths, slice_prefix, plan_path, head, session, share, sidecar, peer_bases(),
                excluded, peer_held, list(undeclared_edits), own_hunk_bytes, run_id, committed_only,
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
    manifest = _manifest_path(share, slice_prefix)
    if not reply.get("error") and "pre_review_manifest" not in reply and manifest.exists():
        reply = {**reply, "pre_review_manifest": _rel(manifest, repo_root)}
    degraded = getattr(fresh_bases[0], "degraded", None) if fresh_bases else None
    if degraded and not reply.get("error"):
        reply = {**reply, "peer_bases_degraded": degraded}
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
    peer_bases: Optional[dict] = None,
    excluded: Optional[List[dict]] = None,
    peer_held: Optional[List[str]] = None,
    undeclared_edits: Optional[List[str]] = None,
    own_hunk_bytes: Optional[Mapping[str, bytes]] = None,
    run_id: str = "",
    committed_only: bool = False,
) -> dict:
    if peer_bases is None:
        peer_bases = _peer_bases(repo_root, base, head, paths, session, plan_path)
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
        excluded=list(excluded or []),
        peer_held=list(peer_held or []),
        undeclared_edits=list(undeclared_edits or []),
        error=None,
    )

    rev_range = f"{base}..{head or 'HEAD'}"
    frozen_whole: List[dict] = []

    def freeze_whole(part: Optional[dict]) -> dict:
        # Frozen once: the partition cuts its slices from this same diff instead of re-freezing.
        if frozen_whole:
            return frozen_whole[0]
        fb = list(part.get("fallback_paths") or []) if part else []
        wt = list(part.get("worktree_paths") or []) if part else []
        rest = paths
        if fb:
            # Trap: with a fallback, only declared paths the partition saw as product are frozen in the whole diff.
            seen = part.get("product_paths") or []
            fb_index, seen_index = _cover_index(fb), _cover_index(seen)
            rest = [p for p in paths if not _covers(p, fb_index) and _covers(p, seen_index)]
        def freeze_over(live: List[str]) -> dict:
            if fb or wt:
                return _freeze_whole_by_base(
                    repo_root, base, head, [p for p in rest if p in live], peer_bases or {}, slice_prefix,
                    fallback=fb, worktree_paths=wt,
                )
            if peer_bases:
                return _freeze_whole_by_base(
                    repo_root, base, head,
                    live if head else live + _untracked_under_declared(repo_root, live),
                    peer_bases, slice_prefix,
                )
            if head:
                return _heavy("freeze_diff")(repo_root, rev_range, slice_prefix, live)
            return _heavy("freeze_diff")(
                repo_root, base, slice_prefix, live + _untracked_under_declared(repo_root, live), worktree=True
            )

        try:
            with review_prep_legs.leg("freeze_whole"):
                live = list(paths)
                while True:
                    got = freeze_over(live)
                    # committed_only has no worktree fallback to absorb a path with no range delta: it drops out.
                    gone = [p for p in (got.get("uncovered_paths") or []) if p in live] if committed_only else []
                    if not got.get("error") or not gone:
                        break
                    live = [p for p in live if p not in gone]
                    if not live:
                        got = {"error": f"no committed delta in {base}..{head} for any declared path"}
                        break
                if own_hunk_bytes and not got.get("error"):
                    _narrow_frozen_diff(repo_root, base, Path(got["diff_path"]), own_hunk_bytes)
        except ValueError as exc:
            got = {"error": str(exc)}
        frozen_whole.append(got)
        return got

    def run_partition() -> dict:
        # Range mode: an own file with no `base..head` delta (withheld edit, untracked, peer-held)
        # is measured and frozen HEAD->worktree. Directory and magic declarations are left to range.
        # Trap: committed_only passes no fallback, which also leaves the partition's dirty-path probe
        # idle; both are the only routes by which worktree bytes enter a range-mode slice.
        fallback = [
            p for p in paths
            if head and not committed_only and not is_magic_pathspec(p) and not p.endswith("/") and not (repo_root / p).is_dir()
        ]
        with review_prep_legs.leg("partition"):
            return _heavy("partition_slices")(
                repo_root, base, paths, slice_prefix, head=head or None,
                **({"path_bases": peer_bases} if peer_bases else {}),
                **({"head_fallback": fallback} if fallback else {}),
                whole_diff=freeze_whole,
            )

    # Trap: range mode partitions first; the whole-diff freeze needs the fallback paths it names.
    part = run_partition() if head else None
    if part is not None and part["error"]:
        out["error"] = part["error"]
        return out
    whole = freeze_whole(part)
    if whole.get("error"):
        out["error"] = f"freeze of {slice_prefix} failed: {whole['error']}"
        return out
    out["whole_diff_path"] = whole["diff_path"]
    product = _heavy("diff_product_files")(Path(whole["diff_path"]))
    out["product_files"] = len(product)
    # Trap: hashed after the freeze, so a write landing between the two is recorded as reviewed
    # though the frozen diff lacks it; the moved-bytes check then serves that stale freeze.
    with review_prep_legs.leg("hash_contents"):
        contents = {rel: None if committed_only else _read_or_none(repo_root / rel) for rel in product}
        if committed_only:
            # Pins come from the head blobs the frozen range names; a worktree read would pin dirt the slice lacks.
            contents = _range_blob_bytes(repo_root, head, contents)
        out["reviewed_sha256"] = {
            rel: eol_folded_sha256(data) for rel, data in contents.items() if data is not None
        }
    if head and not committed_only:
        with review_prep_legs.leg("range_blob_bytes"):
            contents = _range_blob_bytes(repo_root, head, contents)
    else:
        out["declared_sha256"] = _declared_hashes(repo_root, paths, out["reviewed_sha256"])
    with review_prep_legs.leg("hash_contents"):
        hashes = {rel: None if data is None else hashlib.sha256(data).hexdigest() for rel, data in contents.items()}
    with review_prep_legs.leg("pre_review_snapshot"):
        _write_pre_review_snapshot(share, repo_root, slice_prefix, base, paths, contents, hashes)
    out["pre_review_manifest"] = _rel(_manifest_path(share, slice_prefix), repo_root)

    out["peer_bases"] = dict(peer_bases)
    if part is None:
        part = run_partition()
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

    sliced = [f for s in out["slices"] for f in s["files"]]
    if not sliced and paths:
        out["no_slice_reason"] = (
            f"verdict {part['verdict']}: the frozen diff of {len(paths)} declared path(s) holds no product file"
        )
        unsliced = _header_only_product_paths(Path(whole["diff_path"]))
        if unsliced:
            out["error"] = (
                f"review prep sliced nothing from a non-empty frozen diff of {len(paths)} declared path(s); "
                f"product delta with no ---/+++ lines: {', '.join(unsliced[:5])}"
            )
    out[VERDICT_JUDGED_KEY] = slice_pins(out.get("reviewed_sha256") or {}, sliced)
    # A declared path the frozen diff holds a product delta for is never `no-delta`, sliced or not.
    sliced_index = _cover_index([*sliced, *product])
    out["excluded"].extend(
        {"path": p, "reason": "no-delta"}
        for p in paths
        if not is_magic_pathspec(p) and not _covers(p, sliced_index)
    )
    with review_prep_legs.leg("foreign_claims"):
        from coordinator_core.ops.review_peer_bases import PeerBases

        scanned = peer_bases.changed if isinstance(peer_bases, PeerBases) else None
        dirty = frozenset(part.get("worktree_paths") or []) | frozenset(part.get("fallback_paths") or [])
        out["foreign_claims"] = foreign_claims(repo_root, rev_range, paths, session, scanned, dirty or None)
    out["sidecar_path"] = _rel(sidecar, repo_root)
    with review_prep_legs.leg("write_sidecar"):
        _write_sidecar(sidecar, out, session, paths, head, run_id, committed_only)
        _write_pointers(share, sidecar, base, slice_prefix, plan_path)
    out.pop("declared_sha256", None)
    out.pop("peer_bases", None)
    return out


def _own_hunk_views(repo_root: Path, run_id: str, paths: Sequence[str]) -> Dict[str, Optional[bytes]]:
    """``{path: view}`` for each of ``paths`` (declared by ``run_id``, dirty beside a halted peer's
    hunks) that holds a hunk of the run's own. ``view`` is HEAD plus only the run's hunks
    (``own_hunk_candidate.candidate``), or None when the worktree bytes carry no foreign hunk. A path with
    no own hunk, an unreadable side or a refused attribution is absent: the caller drops it. Spawns nothing."""
    from coordinator_core.git import call_io_scope
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.ops.dispatch_emit import kept_work, own_hunk_candidate

    views: Dict[str, Optional[bytes]] = {}
    try:
        common = resolve_git_common_dir(repo_root)
        entries = git_state.head_blobs(repo_root, list(paths))
    except Exception:  # noqa: BLE001 -- no view is a drop, as before
        return views
    for path in paths:
        try:
            entry = entries.get(path)
            stored = read_object(common, entry[1]) if entry else None
            raw = call_io_scope.read_bytes(repo_root / path)
            if raw is None:
                continue
            tree = kept_work.checkin_form(repo_root, path, raw)
            if stored is None or tree is None:
                continue
            res = own_hunk_candidate.candidate(repo_root, run_id, path, stored[1], tree, declared=True)
        except (OSError, ValueError):
            continue
        if "bytes" not in res:
            continue
        if not res["excluded"]:
            views[path] = None
        elif res["bytes"] != stored[1]:
            views[path] = res["bytes"]
    return views


def _own_section(path: str, old: bytes, new: bytes) -> str:
    """One ``diff --git`` stanza of ``old`` -> ``new`` for ``path``; empty when they are equal."""
    import difflib

    def lines(blob: bytes) -> List[str]:
        return blob.decode("utf-8", "surrogateescape").splitlines(True)

    body = []
    for line in difflib.unified_diff(lines(old), lines(new), f"a/{path}", f"b/{path}", n=3, lineterm="\n"):
        body.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return f"diff --git a/{path} b/{path}\n" + "".join(body) if body else ""


def _narrow_frozen_diff(repo_root: Path, base: str, diff_path: Path, own: Mapping[str, bytes]) -> None:
    """Rewrite the frozen worktree diff at ``diff_path`` so each path of ``own`` carries ``base`` -> its
    own-hunk view, never the halted peer's hunks beside it. Raises ValueError when a base blob is
    unreadable: a diff that still holds the foreign hunks is never served."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.rollback_check import _blob_at_commit

    common = resolve_git_common_dir(repo_root)
    text = diff_path.read_bytes().decode("utf-8")
    out = []
    for part in re.split(r"(?m)^(?=diff --git )", text):
        hit = next((p for p in own if part.startswith(f"diff --git a/{p} b/{p}\n")), None)
        if hit is None:
            out.append(part)
            continue
        sha = _blob_at_commit(common, base, hit)
        stored = read_object(common, sha) if isinstance(sha, str) else None
        if stored is None:
            raise ValueError(f"own-hunk view of {hit}: base blob at {base[:12]} unreadable")
        out.append(_own_section(hit, stored[1], own[hit]))
    narrowed = "".join(out)
    if narrowed != text:
        diff_path.write_text(narrowed, encoding="utf-8", newline="\n")
        declare_write(diff_path)


def _cover_index(files: Sequence[str]) -> frozenset:
    """Every file in ``files`` and every directory above one: `_covers` against it is one set lookup."""
    held = set()
    for f in files:
        while f and f not in held:
            held.add(f)
            f = f.rpartition("/")[0]
    return frozenset(held)


def _covers(declared: str, files: "Sequence[str] | frozenset") -> bool:
    """True when ``declared`` names one of ``files`` or a directory holding one. Pass a `_cover_index`
    when asking many declared paths of the same files."""
    d = declared.rstrip("/")
    if isinstance(files, frozenset):
        return d in files
    return any(f == d or f.startswith(d + "/") for f in files)


def _header_only_product_paths(diff_path: Path) -> List[str]:
    """Non-bookkeeping paths a frozen diff names in `diff --git` headers: a mode-only, rename-only or
    binary stanza carries no ---/+++ lines, so `product_files` never sees it."""
    from coordinator_core.ops.review_stamp import is_product_path

    data = _read_or_none(diff_path)
    if not data:
        return []
    found: List[str] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            path = line.rsplit(" b/", 1)[1]
            if path not in found and is_product_path(path):
                found.append(path)
    return found


def _read_or_none(path: Path) -> Optional[bytes]:
    from coordinator_core.git import call_io_scope

    try:
        return call_io_scope.read_bytes(path)
    except OSError:
        return None


def _range_blob_bytes(repo_root: Path, head: str, worktree: dict) -> dict:
    """``worktree`` with each path's bytes replaced by its blob at ``head``, the bytes the range's
    diff names (None when the path is absent at ``head``). In-process object reads, no spawn.
    Trap: an unreadable ``head`` commit or tree keeps the worktree bytes for every path."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.ref_cas_retry import _root_tree

    tree = _root_tree(repo_root, head)
    spine = git_state.read_tree_spine(repo_root, list(worktree), root_tree_sha=tree) if tree else None
    if spine is None:
        return worktree
    common_dir = resolve_git_common_dir(repo_root)
    out: dict = {}
    for rel in worktree:
        parent, _, name = rel.rpartition("/")
        entry = spine.get(parent, {}).get(name)
        found = read_object(common_dir, entry[1]) if entry is not None and entry[0] != 0o40000 else None
        if entry is not None and (found is None or found[0] != "blob"):
            return worktree
        out[rel] = None if entry is None else found[1]
    return out


def _manifest_path(share: Path, slice_prefix: str) -> Path:
    return pre_review_dir(share, slice_prefix) / "manifest.json"


def _land_blob(blob: Path, data: bytes) -> bool:
    """Write ``data`` to the content-addressed ``blob`` in place (no tempfile replace); True when written.

    Trap: a crash mid-write leaves a short or torn file, so an existing blob counts only when its
    bytes equal ``data``."""
    try:
        if blob.stat().st_size == len(data) and blob.read_bytes() == data:
            return False
    except OSError:
        pass
    blob.write_bytes(data)
    return True


def _write_pre_review_snapshot(
    share: Path,
    repo_root: Path,
    slice_prefix: str,
    base: str,
    paths: List[str],
    contents: dict,
    hashes: dict,
) -> None:
    """Write the P3 pre-review snapshot: one blob per distinct hash plus ``manifest.json``.

    Trap: write-once. A recompute for the same ``slice_prefix`` happens precisely when review has
    already moved bytes, so an existing manifest is never rewritten and its blobs are the
    pre-review truth. Blobs land before the manifest so a manifest never names a missing blob."""
    manifest = _manifest_path(share, slice_prefix)
    if manifest.exists():
        return
    snap = manifest.parent
    snap.mkdir(parents=True, exist_ok=True)
    files = {rel: h for rel, h in hashes.items() if h is not None}
    for rel, digest in files.items():
        if _land_blob(snap / digest, contents[rel]):
            declare_write(snap / digest)
    body = json.dumps(
        {
            "base": base,
            "head": git_state.head_sha(repo_root),
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "paths": paths,
            "files": files,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    try:
        fd = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    declare_write(manifest)


def _write_sidecar(
    sidecar: Path, result: dict, session: str, paths: List[str], head: Optional[str], run_id: str = "",
    committed_only: bool = False,
) -> None:
    fm = {
        "agent_type": "engine:prep-freeze",
        "status": "complete",
        "lead_session_id": session,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        **{k: result[k] for k in _STORED_KEYS},
        "reviewed_sha256": result.get("reviewed_sha256", {}),
        VERDICT_JUDGED_KEY: result.get(VERDICT_JUDGED_KEY, {}),
        **({"declared_sha256": result["declared_sha256"]} if "declared_sha256" in result else {}),
        "peer_bases": result.get("peer_bases", {}),
        "peer_held": result.get("peer_held", []),
        "undeclared_edits": result.get("undeclared_edits", []),
        "head": head or None,
        "paths": paths,
        **({"run_id": run_id} if run_id else {}),
        **({"committed_only": True} if committed_only else {}),
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(
        fm, Dumper=getattr(yaml, "CSafeDumper", yaml.SafeDumper), default_flow_style=False, sort_keys=False
    ).strip()
    atomic_write_bytes(
        sidecar,
        f"---\n{text}\n---\n\n## Review prep\n\nMechanical, no-agent record: review_prep.review_prep.\n".encode("utf-8"),
    )
    declare_write(sidecar)


#: Deadline of the one re-read after a stalled run-record read; it also waits out the in-flight read.
RUN_RECORD_REREAD_DEADLINE_MS = 500.0


STALLED_CAUSE = "stalled"


def _read_run_record(path: Path) -> Tuple[Optional[dict], str]:
    """``(record, "")``, or ``(None, cause)`` with cause ``absent``, ``malformed`` or ``stalled <N> ms``.
    A stalled first read is retried once, bounded by ``RUN_RECORD_REREAD_DEADLINE_MS``."""
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr

    t0 = time.monotonic()
    got = hrr.bounded_json_state(path, hrr.JSON_READ_DEADLINE_MS)
    if got is hrr.STALLED:
        got = hrr.bounded_json_state(path, RUN_RECORD_REREAD_DEADLINE_MS, await_inflight=True)
    if isinstance(got, dict):
        return got, ""
    if got is hrr.STALLED:
        return None, f"{STALLED_CAUSE} {round((time.monotonic() - t0) * 1000)} ms"
    return None, "absent" if got is hrr.MISSING else "malformed"


class RunRecordReadStalled(ValueError):
    """A run-record read outlasted both read bounds: transient and retryable, unlike an absent or
    malformed record. The message is the same one fact a plain ValueError carried."""


def _read_failure_type(cause: str) -> type:
    """Exception type for a `_read_run_record` cause: only the `stalled <N> ms` cause is retryable."""
    return RunRecordReadStalled if cause.startswith(STALLED_CAUSE) else ValueError


MANIFEST_WAIT_S = 0.4
MANIFEST_POLL_S = 0.02


def _read_manifest(base: Path) -> Tuple[Optional[dict], str]:
    """``(manifest, cause)`` of the run record dir `base` (``_read_run_record``). While a recompose claim
    stands there the read retries, capped at `MANIFEST_WAIT_S`; with none it is a single read. The claim
    is sampled before each read, so a None read with no claim standing is final."""
    from coordinator_core.ops.dispatch_emit.halt_resume_record import recompose_in_flight

    deadline = time.monotonic() + MANIFEST_WAIT_S
    while True:
        in_flight = recompose_in_flight(base, time.time())
        manifest, cause = _read_run_record(base / "manifest.json")
        if manifest is not None or not in_flight or time.monotonic() >= deadline:
            return manifest, cause
        time.sleep(MANIFEST_POLL_S)


def declared_paths_and_held(
    repo_root: Path, run_id: str, *, leg: bool = False, own_session: str = ""
) -> Tuple[List[str], Dict[str, List[str]]]:
    """``(paths, held)``: the run's whole write set (``run_write_set``: manifest ``review_declared_paths``,
    leg footprint writes, credited chunk writes, halted-withheld paths), deduped, peer-held paths
    included, plus ``{path: [session ids]}`` for those a live session other than ``own_session`` holds.
    Review is read-only, so a path the run wrote stays reviewable when a peer touched it. An empty
    ``own_session`` lets ``_peer_held`` resolve the calling session itself. Raises ValueError (one fact)
    on an unsafe run_id, a missing manifest, a non-list-of-str field, a missing footprint under ``leg``,
    or an empty write set. Spawns nothing."""
    from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, record_dir
    from coordinator_core.ops.dispatch_emit.run_write_set import run_write_set_full

    base = record_dir(repo_root, run_id)
    manifest, cause = _read_manifest(base)
    if manifest is None:
        raise _read_failure_type(cause)(f"no readable manifest for run {run_id!r}: {cause}")
    declared = manifest.get("review_declared_paths")
    if not isinstance(declared, list) or not all(isinstance(p, str) for p in declared):
        raise ValueError(f"manifest review_declared_paths for run {run_id!r} is not a list of str")
    if leg:
        footprint, cause = _read_run_record(base / LEG_FOOTPRINT_FILE)
        writes = footprint.get("writes") if footprint is not None else None
        if not isinstance(writes, list) or not all(isinstance(p, str) for p in writes):
            raise _read_failure_type(cause)(
                f"no readable leg footprint writes list for run {run_id!r}: "
                + (cause or "writes is not a list of str")
            )
    ordered, held = run_write_set_full(repo_root, run_id, own_session)
    if not ordered:
        raise ValueError(f"run {run_id!r} has no reviewable paths (none declared, or all scratch)")
    return list(ordered), held


def declared_paths_for_run(repo_root: Path, run_id: str, *, leg: bool = False, own_session: str = "") -> List[str]:
    """``declared_paths_and_held``'s paths minus those a live peer holds: the landing-side view.
    Raises ValueError naming each peer-held path and holder when none survive."""
    ordered, held = declared_paths_and_held(repo_root, run_id, leg=leg, own_session=own_session)
    resolved = [p for p in ordered if p not in held]
    if not resolved:
        named = "; ".join(f"{p} (held by {', '.join(sids)})" for p, sids in held.items())
        raise ValueError(
            f"run {run_id!r} has no reviewable paths (none declared, or all scratch or claimed by another session)"
            + (f"; peer-held: {named}" if named else "")
        )
    return resolved


def _declared_provably_clean(repo_root: Path, declared: Sequence[str]) -> bool:
    """True when every ``declared`` entry is an indexed file whose worktree stat matches the index:
    the run left no edit on its own paths. A directory, an untracked or deleted path, or an
    unreadable index is not proof, so it answers False. No git spawn. A staged edit matches the index
    and so reads as clean: the proof is worktree-vs-index, never index-vs-HEAD."""
    from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot

    if not declared:
        return False
    scanned = stage_dirt_snapshot._stat_dirty(repo_root)
    if scanned is None:
        return False
    dirty, entries = scanned
    for entry in declared:
        rel = entry.replace("\\", "/")
        # `_stat_dirty` never stats non-product prefixes, so absence from `dirty` proves nothing there.
        if rel.startswith(stage_dirt_snapshot._NON_PRODUCT_PREFIXES) or rel not in entries or rel in dirty:
            return False
    return True


#: Most stray edits one run adopts. Past it the candidates are shared-tree dirt (hundreds of files
#: edited by peers since the stage marker), which the run cannot be told apart from: it adopts none.
MAX_ADOPTED_UNDECLARED = 16


def undeclared_edits_for_run(repo_root: Path, run_id: str, declared: Sequence[str], session_id: str = "") -> List[str]:
    """Paths changed since the run's stage dirt snapshot that none of ``declared`` covers and no live
    peer holds (``run_write_set._without_peer_held``) and that a self session positively claimed
    (``_without_foreign_claimed``). Empty without a snapshot, and empty when every declared path is clean
    (``_declared_provably_clean``): a run with no edit of its own adopts no foreign dirt. Empty too when
    more than ``MAX_ADOPTED_UNDECLARED`` survive: the frozen diff stays the declared footprint. Spawns no
    git unless a candidate survives to the peer-hold check."""
    from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.run_write_set import _without_peer_held

    try:
        record = record_dir(repo_root, run_id)
    except ValueError:
        return []
    found = stage_dirt_snapshot.undeclared_edits(repo_root, record, declared)
    if not found or _declared_provably_clean(repo_root, declared):
        return []
    found = _without_foreign_claimed(repo_root, found, session_id, run_id)
    kept = list(_without_peer_held(repo_root, found, session_id, run_id)) if found else []
    return kept if len(kept) <= MAX_ADOPTED_UNDECLARED else []


def _without_foreign_claimed(
    repo_root: Path, found: Sequence[str], session_id: str, run_id: str
) -> List[str]:
    """``found`` narrowed to dirt the run positively wrote: a path stays only when a self session
    (``land_run._firing_sessions`` plus prior-pass sessions) holds a write claim on it. A path with no
    claimant, only foreign claimants, an unanswerable lookup, or a failing attribution is foreign and
    is dropped: absence of a witness is never evidence the run wrote it. One claim lookup; spawns nothing."""
    try:
        from coordinator_core.ops.dispatch_emit import land_run
        from coordinator_core.session import claim_index, touch_record

        selves = land_run._firing_sessions(repo_root, run_id, session_id or None) | land_run._prior_pass_sessions(
            repo_root, run_id
        )
        table = land_run._bounded_claim_lookup(claim_index, list(found), str(repo_root))
        kinds = getattr(table, "recorded_kind", None) or {}

        def writers(path: str) -> List[str]:
            claimants = table.get(path, [])
            return [s for s in claimants if touch_record.kind_blocks_a_peer_commit(kinds.get(path, {}).get(s))]

        claimed = [path for path in found if any(s in selves for s in writers(path))]
        sibling = _sibling_declared(repo_root, claimed, run_id)
        return [path for path in claimed if path not in sibling]
    except Exception:  # noqa: BLE001 -- no witness is foreign: a failing attribution drops every candidate
        return []


def _sibling_declared(repo_root: Path, paths: Sequence[str], run_id: str) -> set:
    """Those of ``paths`` another live run declares by exact path, a sibling fired from this run's own
    session included: a session claim cannot tell sibling runs apart, a run's declaration can. A bare
    prefix declaration never counts, so it cannot swallow this run's genuine undeclared write. Raises
    when the live-run view is unknowable."""
    from coordinator_core.ops import warp_run_deliverable as wrd

    view = wrd._peer_view(repo_root, "", None, None, run_id)
    if view is None:
        raise ValueError("live sibling declarations unreadable")
    wanted = {wrd._norm(p) for p in paths}
    return {p for _, _, _, declared, _, withheld in view for p in wanted & declared - withheld}


@register_op("review.prep")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.prep". Params: ``base`` (str sha), exactly one of ``paths`` (list[str]) and
    ``run_id`` (str; the run's declared paths are read from its record), optional ``leg`` (bool,
    only with ``run_id``; requires the leg footprint; the run's whole write set is reviewed
    either way), ``slice_prefix``
    (str), ``plan_path`` (str), optional ``session_id``, and exactly one of ``head`` (str,
    committed range mode) or ``worktree: true``; optional ``committed_only`` (bool, with ``head`` only: the
    slice is exactly ``base..head``, never working-tree bytes). With ``run_id`` the slice prefix is always
    ``review_slice_id.run_prep_slice_prefix(run_id, base)``: a typed ``slice_prefix`` is ignored
    and may be omitted; with ``paths`` it is required. Optional ``group_index`` / ``group_count`` (ints,
    together) return only that slice group. Keying scope: show_top."""
    from coordinator_core.git.call_io_scope import call_io_scope

    def _scoped() -> dict:
        # One scope for the whole call: peer records and worktree files are each read once across the declared-path,
        # hold-scan, own-hunk, parse-gate, freeze and content-hash legs.
        with call_io_scope():
            return _handle(params, repo_root)

    return review_prep_legs.run(_scoped)


def _handle(params: dict, repo_root: Optional[Path]) -> dict:
    if repo_root is None:
        return _fail("review.prep requires a show_top-keyed dispatch; repo_root was not supplied")
    paths = params.get("paths")
    run_id = params.get("run_id")
    leg = params.get("leg")
    if run_id is not None:
        if paths is not None:
            return _fail("review.prep takes exactly one of paths or run_id")
        if not isinstance(run_id, str) or not run_id:
            return _fail("review.prep run_id must be a non-empty string")
        if leg is not None and not isinstance(leg, bool):
            return _fail("review.prep leg must be a boolean")
        try:
            with review_prep_legs.leg("handler.declared_paths"):
                paths, held_by_peer = declared_paths_and_held(
                    repo_root, run_id, leg=bool(leg), own_session=str(params.get("session_id") or "")
                )
        except RunRecordReadStalled as exc:
            return _fail(f"{REVIEW_PREP_BUSY_PREFIX} {exc}; re-run the same call")
        except ValueError as exc:
            return _fail(str(exc))
        with review_prep_legs.leg("handler.undeclared_edits"):
            undeclared = undeclared_edits_for_run(repo_root, run_id, paths, str(params.get("session_id") or ""))
        paths = [*paths, *undeclared]
    else:
        undeclared = []
        held_by_peer = {}
        if leg is not None:
            return _fail("review.prep leg is accepted only with run_id")
        if not isinstance(paths, list):
            return _fail("review.prep requires params.paths as a list")
    head = str(params.get("head") or "")
    if bool(head) == bool(params.get("worktree")):
        return _fail("review.prep requires exactly one of head (range mode) or worktree=true")
    group_index, group_count = params.get("group_index"), params.get("group_count")
    if any(v is not None and (isinstance(v, bool) or not isinstance(v, int)) for v in (group_index, group_count)):
        return _fail("review.prep group_index and group_count must be integers")
    base = str(params.get("base") or "")
    if run_id is not None:
        if not _SHA_RE.match(base):
            return _fail("base must be a lowercase commit sha")
        from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

        slice_prefix = run_prep_slice_prefix(run_id, base)
    else:
        slice_prefix = str(params.get("slice_prefix") or "")
    live_runs = live_runs_from_params(params.get("live_runs"))
    if params.get("live_runs") and live_runs is None:
        return _fail("review.prep params.live_runs must map run ids to objects")
    run_id = str(params.get("run_id") or "")
    halted_foreign: List[str] = []
    own_hunk_bytes: Dict[str, Optional[bytes]] = {}
    if live_runs is None and run_id:
        from coordinator_core.ops.dispatch_emit.land_run import foreign_halted_withheld

        with review_prep_legs.leg("handler.halted_withheld"):
            held = foreign_halted_withheld(repo_root, run_id, str(params.get("plan_path") or "") or None, [str(p) for p in paths])
        if not head:
            halted_foreign = [str(p) for p in paths if str(p) in held]
            if halted_foreign:
                own_hunk_bytes = _own_hunk_views(repo_root, run_id, halted_foreign)
                gone = {p for p in halted_foreign if p not in own_hunk_bytes}
                paths = [p for p in paths if str(p) not in gone]
                held_by_peer = {p: v for p, v in held_by_peer.items() if p not in gone}
                undeclared = [p for p in undeclared if p not in gone]
                held = {p: r for p, r in held.items() if p not in gone}
        live_runs = holders_from_held(held) or None
    from coordinator_core.ops.review_freeze_diff import diff_targets_batch

    with diff_targets_batch(repo_root):
        reply = review_prep(
            repo_root,
            base=base,
            paths=[str(p) for p in paths],
            slice_prefix=slice_prefix,
            plan_path=str(params.get("plan_path") or ""),
            head=head or None,
            session_id=str(params.get("session_id") or ""),
            group_index=group_index,
            group_count=group_count,
            run_id=run_id,
            live_runs=live_runs,
            undeclared_edits=undeclared,
            peer_held=list(held_by_peer),
            own_hunk_bytes={p: b for p, b in own_hunk_bytes.items() if b is not None and p in paths},
            committed_only=bool(params.get("committed_only")),
        )
    if halted_foreign and not reply.get("error"):
        reply = {**reply, "foreign_claims": sorted({*(reply.get("foreign_claims") or []), *halted_foreign})}
    return reply
