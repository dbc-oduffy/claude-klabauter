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
live peer. They join ``paths``, and the reply and sidecar list them as ``undeclared_edits``.

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
from coordinator_core.git import git_state
from coordinator_core.git.content_hash import eol_folded_sha256
from coordinator_core.git.literal_pathspec import git_pathspec, is_magic_pathspec
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops import review_prep_legs
from coordinator_core.ops.dispatch_emit.run_ownership import LiveRun, blocks, holders_from_held, owns_path
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
        "error": message,
    }


def _plan_frontmatter_str(repo_root: Path, plan_path: str, key: str) -> Optional[str]:
    """The plan's frontmatter string ``key``, or ``None`` when absent, blank or unreadable."""
    plan_file = Path(plan_path)
    if not plan_file.is_absolute():
        plan_file = repo_root / plan_file
    try:
        split = split_frontmatter(plan_file.read_text(encoding="utf-8", errors="replace"))
        doc = yaml.safe_load(split.fm_text) if split else None
    except (OSError, yaml.YAMLError):
        doc = None
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


def _specs_by_base(paths: List[str], base: str, path_bases: dict) -> List[tuple]:
    """``(pathspec, effective base)`` per declared path, in declared order. Peer bases are keyed by
    file, so a declared directory holding keyed files stays on the run base minus those files
    (``:(exclude)`` entries) and each keyed file follows it on its own peer base."""
    from coordinator_core.ops.review_partition_slices import _effective_base

    out: List[tuple] = []
    for p in paths:
        eb = _effective_base(p, base, path_bases)
        out.append((p, eb))
        if eb != base:
            continue
        prefix = p.rstrip("/") + "/"
        inner = sorted(k for k in path_bases if k.startswith(prefix))
        out.extend((f":(exclude){k}", eb) for k in inner)
        out.extend((k, path_bases[k]) for k in inner)
    return out


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
    its group; a path on the run base keeps ``freeze_diff``'s own refusals."""
    from coordinator_core.ops.review_freeze_diff import _is_run_keyed_prep_id, _shelve_prior_attempt

    groups: dict = {}
    wt_set = set(worktree_paths or [])
    for spec, eb in _specs_by_base(paths, base, path_bases):
        # `worktree_paths` carry uncommitted bytes on top of a range delta: frozen base..worktree.
        groups.setdefault((eb, spec in wt_set), []).append(spec)
    texts: List[str] = []
    head_sha: Optional[str] = None
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
    "foreign_claims", "slices", "whole_diff_sidecars", "sidecar_path", "excluded",
)
REVIEW_PREP_BUSY_PREFIX = "review.prep busy:"
_LOCK_WAIT_SECS = 0.5
# 20x the wait: a live winner holding the lock this long is an over-bar op; a dead one clears fast.
_LOCK_STALE_SECS = 10.0
_LOCK_POLL_SECS = 0.05


def _declared_hashes(repo_root: Path, paths: List[str]) -> Optional[dict]:
    """Declared path -> content hash (``None`` when absent), or ``None`` when any entry is a magic
    pathspec or a directory: such a declaration cannot be proven unmoved, so it is never served."""
    out: dict = {}
    for entry in paths:
        if is_magic_pathspec(entry) or entry.endswith("/") or (repo_root / entry).is_dir():
            return None
        out[entry.replace("\\", "/")] = compute_content_hash(repo_root / entry)
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
) -> Optional[dict]:
    """The reply a success sidecar stored, or ``None`` when absent, unreadable, written before
    replies were stored, written for another base, basis, head or path list, (worktree mode)
    lacking a ``declared_sha256`` map or recorded over declared bytes that have since moved, or
    (when ``peer_bases`` is given) recorded under a ``peer_bases`` map that differs from the one it
    returns now: a peer commit can land without moving the declared bytes."""
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
    if not head and not _declared_bytes_unmoved(repo_root, fm.get("declared_sha256")):
        return None
    if peer_bases is not None and (fm.get("peer_bases") or {}) != peer_bases():
        return None
    reply = {**{k: fm[k] for k in _STORED_KEYS}, "error": None}
    if "reviewed_sha256" in fm:
        reply["reviewed_sha256"] = fm["reviewed_sha256"]
    reply["peer_held"] = fm.get("peer_held") or []
    reply["undeclared_edits"] = fm.get("undeclared_edits") or []
    return reply


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


def _write_pointers(share: Path, sidecar: Path, base: str, slice_prefix: str, plan_path: str) -> None:
    """Point the run-keyed, run-only and base-only names at ``sidecar``. A name already pointing at
    another existing sidecar is marked ``ambiguous``, which the reader answers ``None``. Written
    only for a prefix of the ``<plan stem>-<run key>-<base[:12]>-prep`` shape."""
    tail = f"-{base[:12]}-prep"
    if not slice_prefix.endswith(tail):
        return
    stem = _UNSAFE_NAME.sub("-", Path(plan_path.replace("\\", "/")).stem)
    head = slice_prefix[: -len(tail)]
    run_key = head[len(stem) + 1:] if head.startswith(stem + "-") else ""
    names = {_pointer_name(None, base)}
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
    ask route's canonical ``manifest-<run_key>-<base[:12]>-prep`` sidecar. A candidate is kept when
    its ``agent_type`` is ``engine:prep-freeze``, its ``run_base_sha`` is ``base`` and, when
    ``within`` is non-empty, every ``paths`` entry is in it.
    ``covering=True`` (keyword-only, default off) flips that check: a file is kept when every
    ``within`` entry is in its ``paths``, so a review that read extra paths is still found. It has
    no effect when ``within`` is empty.
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
    if isinstance(named, str) and named and Path(named).name == named:
        candidates.append(share / named)
    if run_key and base:
        from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

        canonical = share / f"{run_prep_slice_prefix(run_key, base)}.review-prep.md"
        if canonical not in candidates and canonical.is_file():
            candidates.append(canonical)
    for sidecar in candidates:
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
) -> dict:
    """Freeze, partition and claim-check one run's diff once per ``slice_prefix``; see the module
    docstring. ``undeclared_edits`` names the members of ``paths`` that no row declared and that
    changed since the stage dirt snapshot; the reply and sidecar carry them as ``undeclared_edits``.
    ``peer_held`` names members of ``paths`` a live session touches; they stay in review and ride on
    the reply's ``peer_held`` next to those ``live_runs`` withholds.
    With ``live_runs``, a path another live run withholds or claims stays in review and is
    listed in ``peer_held``; the peer-hunks gate settles it at commit. The handler derives ``live_runs``
    from halted runs' withheld sets (``land_run.foreign_halted_withheld``) when ``params.run_id`` is
    given and ``params.live_runs`` is not. ``excluded`` lists each declared path not reviewed with its
    reason (``outside-repo``, ``no-delta``)."""
    if not base or not slice_prefix or not plan_path:
        return _fail("base, paths, slice_prefix and plan_path are required")
    if not _SHA_RE.match(base):
        return _fail("base must be a lowercase commit sha")
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
    from coordinator_core.ops.dispatch_emit.landing_integrity import unparseable_py, unparseable_py_at

    from coordinator_core.ops.review_peer_bases import _has_object_store

    with review_prep_legs.leg("parse_gate"):
        if head:
            broken = []
            if _has_object_store(repo_root):
                broken = _unparseable_py_at_native(repo_root, head, paths)
                if broken is None:
                    broken = unparseable_py_at(repo_root, head, paths)
        else:
            broken = unparseable_py(repo_root, paths)
    if broken:
        return _fail(
            "review.prep refused: py-unparseable: " + "; ".join(f"{f.path} {f.detail}" for f in broken)
        )
    session = session_id or resolve_session_id(str(repo_root))
    if not session:
        return _fail("no session id: pass session_id or run under a session")

    share = Path(share_dir(str(repo_root), session))
    sidecar = share / f"{slice_prefix}.review-prep.md"
    lock = share / f"{slice_prefix}.review-prep.lock"

    fresh_bases: List[dict] = []

    def peer_bases() -> dict:
        if not fresh_bases:
            with review_prep_legs.leg("peer_bases"):
                fresh_bases.append(_peer_bases(repo_root, base, head, paths, session, plan_path))
        return fresh_bases[0]

    def served() -> Optional[dict]:
        with review_prep_legs.leg("stored_reply"):
            return _stored_reply(repo_root, sidecar, base, paths, head, peer_bases)

    def compute_locked() -> dict:
        try:
            return served() or _compute(
                repo_root, base, paths, slice_prefix, plan_path, head, session, share, sidecar, peer_bases(),
                excluded, peer_held, list(undeclared_edits),
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
        try:
            with review_prep_legs.leg("freeze_whole"):
                if fb or wt:
                    got = _freeze_whole_by_base(
                        repo_root, base, head, rest, peer_bases or {}, slice_prefix, fallback=fb, worktree_paths=wt
                    )
                elif peer_bases:
                    got = _freeze_whole_by_base(repo_root, base, head, paths, peer_bases, slice_prefix)
                elif head:
                    got = _heavy("freeze_diff")(repo_root, rev_range, slice_prefix, paths)
                else:
                    got = _heavy("freeze_diff")(repo_root, base, slice_prefix, paths, worktree=True)
        except ValueError as exc:
            got = {"error": str(exc)}
        frozen_whole.append(got)
        return got

    def run_partition() -> dict:
        # Range mode: an own file with no `base..head` delta (withheld edit, untracked, peer-held)
        # is measured and frozen HEAD->worktree. Directory and magic declarations are left to range.
        fallback = [
            p for p in paths
            if head and not is_magic_pathspec(p) and not p.endswith("/") and not (repo_root / p).is_dir()
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
        contents = {rel: _read_or_none(repo_root / rel) for rel in product}
        out["reviewed_sha256"] = {
            rel: eol_folded_sha256(data) for rel, data in contents.items() if data is not None
        }
    if head:
        with review_prep_legs.leg("range_blob_bytes"):
            contents = _range_blob_bytes(repo_root, head, contents)
    else:
        out["declared_sha256"] = _declared_hashes(repo_root, paths)
    with review_prep_legs.leg("hash_contents"):
        hashes = {rel: None if data is None else hashlib.sha256(data).hexdigest() for rel, data in contents.items()}
    with review_prep_legs.leg("pre_review_snapshot"):
        _write_pre_review_snapshot(share, repo_root, slice_prefix, base, paths, contents, hashes)
    out["pre_review_manifest"] = _rel(_manifest_path(share, slice_prefix), repo_root)

    out["peer_bases"] = peer_bases
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
    sliced_index = _cover_index(sliced)
    out["excluded"].extend(
        {"path": p, "reason": "no-delta"}
        for p in paths
        if not is_magic_pathspec(p) and not _covers(p, sliced_index)
    )
    with review_prep_legs.leg("foreign_claims"):
        out["foreign_claims"] = foreign_claims(repo_root, rev_range, paths, session)
    out["sidecar_path"] = _rel(sidecar, repo_root)
    with review_prep_legs.leg("write_sidecar"):
        _write_sidecar(sidecar, out, session, paths, head)
        _write_pointers(share, sidecar, base, slice_prefix, plan_path)
    out.pop("declared_sha256", None)
    out.pop("peer_bases", None)
    return out


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
    from coordinator_core.ops.review_stamp import PRODUCT_FILE_EXCLUDED_PREFIXES

    data = _read_or_none(diff_path)
    if not data:
        return []
    found: List[str] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            path = line.rsplit(" b/", 1)[1]
            if path not in found and not path.startswith(PRODUCT_FILE_EXCLUDED_PREFIXES):
                found.append(path)
    return found


def _read_or_none(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
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
    return share / f"{slice_prefix}.pre-review" / "manifest.json"


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
        blob = snap / digest
        if not blob.exists():
            atomic_write_bytes(blob, contents[rel])
            declare_write(blob)
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


def _write_sidecar(sidecar: Path, result: dict, session: str, paths: List[str], head: Optional[str]) -> None:
    fm = {
        "agent_type": "engine:prep-freeze",
        "status": "complete",
        "lead_session_id": session,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        **{k: result[k] for k in _STORED_KEYS},
        "reviewed_sha256": result.get("reviewed_sha256", {}),
        **({"declared_sha256": result["declared_sha256"]} if "declared_sha256" in result else {}),
        "peer_bases": result.get("peer_bases", {}),
        "peer_held": result.get("peer_held", []),
        "undeclared_edits": result.get("undeclared_edits", []),
        "head": head or None,
        "paths": paths,
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


def _read_json_object(path: Path) -> Optional[dict]:
    from coordinator_core.ops.dispatch_emit.halt_resume_record import bounded_json_object

    return bounded_json_object(path)


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
    manifest = _read_json_object(base / "manifest.json")
    if manifest is None:
        raise ValueError(f"no readable manifest for run {run_id!r}")
    declared = manifest.get("review_declared_paths")
    if not isinstance(declared, list) or not all(isinstance(p, str) for p in declared):
        raise ValueError(f"manifest review_declared_paths for run {run_id!r} is not a list of str")
    if leg:
        footprint = _read_json_object(base / LEG_FOOTPRINT_FILE)
        writes = footprint.get("writes") if footprint is not None else None
        if not isinstance(writes, list) or not all(isinstance(p, str) for p in writes):
            raise ValueError(f"no readable leg footprint writes list for run {run_id!r}")
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


def undeclared_edits_for_run(repo_root: Path, run_id: str, declared: Sequence[str], session_id: str = "") -> List[str]:
    """Paths changed since the run's stage dirt snapshot that none of ``declared`` covers and no live
    peer holds (``run_write_set._without_peer_held``). Empty without a snapshot; spawns no git
    unless a candidate survives to the peer-hold check."""
    from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.run_write_set import _without_peer_held

    try:
        record = record_dir(repo_root, run_id)
    except ValueError:
        return []
    found = stage_dirt_snapshot.undeclared_edits(repo_root, record, declared)
    return list(_without_peer_held(repo_root, found, session_id, run_id)) if found else []


@register_op("review.prep")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.prep". Params: ``base`` (str sha), exactly one of ``paths`` (list[str]) and
    ``run_id`` (str; the run's declared paths are read from its record), optional ``leg`` (bool,
    only with ``run_id``; requires the leg footprint; the run's whole write set is reviewed
    either way), ``slice_prefix``
    (str), ``plan_path`` (str), optional ``session_id``, and exactly one of ``head`` (str,
    committed range mode) or ``worktree: true``. With ``run_id`` the slice prefix is always
    ``review_slice_id.run_prep_slice_prefix(run_id, base)``: a typed ``slice_prefix`` is ignored
    and may be omitted; with ``paths`` it is required. Optional ``group_index`` / ``group_count`` (ints,
    together) return only that slice group. Keying scope: show_top."""
    return review_prep_legs.run(lambda: _handle(params, repo_root))


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
    if live_runs is None and run_id:
        from coordinator_core.ops.dispatch_emit.land_run import foreign_halted_withheld

        with review_prep_legs.leg("handler.halted_withheld"):
            held = foreign_halted_withheld(repo_root, run_id, str(params.get("plan_path") or "") or None, [str(p) for p in paths])
        live_runs = holders_from_held(held) or None
    return review_prep(
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
    )
