"""``dispatch.commit_merge``: commits an already-resolved two-parent merge without touching unrelated shared-index state.

Params: ``{theirs_ref, resolved_dir, conflicted: [repo-relative path], message, expected_head}``.
The merge tree is computed by ``merge-tree --write-tree`` and completed in a private index with the
bytes found under ``resolved_dir`` for each conflicted path; the commit has parents ``[HEAD, theirs]``.
The shared index and worktree are synced to the new commit BEFORE ``update-ref`` moves HEAD (a CAS on
``expected_head``), so a sync failure leaves HEAD where it was and the synced paths restored.
A path whose index entry already differs from HEAD is left staged; a path whose worktree differs from
HEAD keeps its worktree bytes and has only its index entry moved.
Reply (ok): ``{ok, commit, parents, synced: {checked_out, removed, index_only}, left_staged}``.
Reply (refused): ``{ok: false, refused, detail}``, ``refused`` one of ``bad-params``, ``head-moved``,
``conflict-set-changed`` (detail ``{new, gone}``), ``resolved-missing``, ``conflict-marker``,
``unexpected-staged``, ``sync-failed``; HEAD never moves on a refusal.
The git spawn count is a per-call constant (path lists are chunked at 200), not a function of path count.
No test step, no hooks: the reflog line is written explicitly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from coordinator_core.git.run import GitResult, run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.commit_merge"

_CHUNK = 200
_MARKER = re.compile(rb"(?m)^(?:<<<<<<<|>>>>>>>) ")
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def has_conflict_marker(data: bytes) -> bool:
    """True when ``data`` has a line starting ``<<<<<<< `` or ``>>>>>>> ``. A bare ``=======`` rule is not a marker. Pure."""
    return _MARKER.search(data) is not None


def _chunks(items: list) -> Iterable[list]:
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]


def _refuse(code: str, detail=None) -> dict:
    return {"ok": False, "refused": code, "detail": detail}


def _nul_split(raw: bytes) -> list:
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


class _Repo:
    """The shared worktree; every call runs ``git --literal-pathspecs -C root`` with the shared index unless ``env`` says otherwise."""

    def __init__(self, root: Path):
        self.root = root
        self.shared_env = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}

    def git(self, args: list, *, env=None, input: Optional[bytes] = None, binary: bool = False) -> GitResult:
        return run_git(
            ["--literal-pathspecs", "-C", str(self.root), *args],
            env=self.shared_env if env is None else env, input=input, binary=binary,
        )

    def over_paths(self, args: list, paths: list, *, env=None, binary: bool = False) -> Optional[list]:
        """Run ``args -- <chunk>`` per chunk of ``paths``; the results, or ``None`` when any chunk failed."""
        out = []
        for chunk in _chunks(paths):
            res = self.git([*args, "--", *chunk], env=env, binary=binary)
            if not res.ok:
                return None
            out.append(res)
        return out


def _norm_conflicted(value) -> Optional[list]:
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        return None
    out = set()
    for p in value:
        pure = PurePosixPath(p.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() == ".":
            return None
        out.add(pure.as_posix())
    return sorted(out)


def _merge_tree(repo: _Repo, old: str, theirs: str):
    """``(tree, conflicted names)`` of merging ``theirs`` into ``old``; ``None`` when git cannot compute it."""
    res = repo.git(["merge-tree", "--write-tree", "--name-only", "-z", old, theirs], binary=True)
    if res.returncode not in (0, 1):
        return None
    parts = res.stdout_bytes.split(b"\0")
    tree = parts[0].decode("ascii", "replace").strip()
    names = []
    if res.returncode == 1:
        for part in parts[1:]:
            if not part:
                break
            names.append(part.decode("utf-8", "replace"))
    return (tree, sorted(set(names))) if _SHA.match(tree) else None


def _read_resolved(resolved: Path, conflicted: list):
    """``(missing, marked)`` path lists over the conflicted paths under ``resolved``."""
    missing, marked = [], []
    for rel in conflicted:
        try:
            data = (resolved / rel).read_bytes()
        except OSError:
            missing.append(rel)
            continue
        if has_conflict_marker(data):
            marked.append(rel)
    return missing, marked


def _build_commit(repo: _Repo, common_dir: Path, resolved: Path, tree: str, conflicted: list,
                  old: str, theirs: str, message: str):
    """``(new sha, None)`` from a private index, or ``(None, failure text)``. HEAD is not touched."""
    index = common_dir / f"commit-merge-{os.getpid()}.index"
    env = {**repo.shared_env, "GIT_INDEX_FILE": str(index)}
    try:
        if not repo.git(["read-tree", tree], env=env).ok:
            return None, "read-tree failed"
        modes: dict = {}
        for chunk in _chunks(conflicted):
            res = repo.git(["ls-tree", "-z", tree, "--", *chunk], env=env, binary=True)
            if not res.ok:
                return None, "ls-tree failed"
            for entry in _nul_split(res.stdout_bytes):
                meta, _, name = entry.partition("\t")
                modes[name] = meta.split(" ", 1)[0]
        if conflicted:
            stdin = "\n".join(str(resolved / rel) for rel in conflicted).encode("utf-8")
            hashed = repo.git(["hash-object", "-w", "--no-filters", "--stdin-paths"], env=env, input=stdin)
            shas = hashed.stdout.split()
            if not hashed.ok or len(shas) != len(conflicted):
                return None, "hash-object failed"
            info = b"".join(
                f"{modes.get(rel, '100644')} {sha}\t{rel}".encode("utf-8") + b"\0"
                for rel, sha in zip(conflicted, shas)
            )
            if not repo.git(["update-index", "-z", "--index-info"], env=env, input=info).ok:
                return None, "update-index failed"
        written = repo.git(["write-tree"], env=env)
        final = written.stdout.strip()
        if not written.ok or not _SHA.match(final):
            return None, "write-tree failed"
        body = message if message.endswith("\n") else message + "\n"
        made = repo.git(["commit-tree", final, "-p", old, "-p", theirs], env=env, input=body.encode("utf-8"))
        new = made.stdout.strip()
        if not made.ok or not _SHA.match(new):
            return None, "commit-tree failed: " + made.stderr.strip()
        return new, None
    finally:
        for suffix in ("", ".lock"):
            try:
                os.unlink(str(index) + suffix)
            except OSError:
                pass


def _restore(repo: _Repo, old: str, synced: dict, status: dict) -> None:
    """Return the synced paths to ``old``: index for all of them, worktree for the checked-out and removed ones."""
    touched = synced["checked_out"] + synced["removed"] + synced["index_only"]
    if touched:
        repo.over_paths(["reset", "-q", old], touched)
    added = [p for p in synced["checked_out"] if status.get(p) == "A"]
    for rel in added:
        try:
            os.unlink(repo.root / rel)
        except OSError:
            pass
    back = [p for p in synced["checked_out"] + synced["removed"] if p not in added]
    if back:
        repo.over_paths(["checkout", old], back)


def _sync_shared(repo: _Repo, old: str, new: str):
    """Move the shared index/worktree from ``old`` to ``new`` for the paths the merge changes.

    Returns ``(synced, left_staged, status, staged_before, failure)``; ``failure`` is ``None`` on success.
    """
    synced = {"checked_out": [], "removed": [], "index_only": []}
    res = repo.git(["diff", "--name-status", "--no-renames", "-z", old, new], binary=True)
    if not res.ok:
        return synced, [], {}, set(), "diff old new failed"
    tokens = _nul_split(res.stdout_bytes)
    status = {tokens[i + 1]: tokens[i][:1] for i in range(0, len(tokens) - 1, 2)}
    changed = sorted(status)
    cached = repo.git(["diff", "--cached", "--name-only", "-z", old], binary=True)
    if not cached.ok:
        return synced, [], status, set(), "diff --cached old failed"
    staged_before = set(_nul_split(cached.stdout_bytes))
    left_staged = [p for p in changed if p in staged_before]
    movable = [p for p in changed if p not in staged_before]
    if not movable:
        return synced, left_staged, status, staged_before, None
    dirty_runs = repo.over_paths(["diff", "--name-only", "-z", old], movable, binary=True)
    if dirty_runs is None:
        return synced, left_staged, status, staged_before, "diff old failed"
    dirty = {p for r in dirty_runs for p in _nul_split(r.stdout_bytes)}
    dirty |= {p for p in movable if status[p] == "A" and os.path.lexists(repo.root / p)}
    for rel in movable:
        if rel in dirty:
            synced["index_only"].append(rel)
        elif status[rel] == "D":
            synced["removed"].append(rel)
        else:
            synced["checked_out"].append(rel)
    steps = (
        ("index_only", ["reset", "-q", new]),
        ("removed", ["rm", "-q"]),
        ("checked_out", ["checkout", new]),
    )
    for key, args in steps:
        if synced[key] and repo.over_paths(args, synced[key]) is None:
            return synced, left_staged, status, staged_before, f"{args[0]} failed"
    return synced, left_staged, status, staged_before, None


def commit_merge(root: Path, params: dict) -> dict:
    theirs_ref, message, expected = params.get("theirs_ref"), params.get("message"), params.get("expected_head")
    resolved_dir = params.get("resolved_dir")
    conflicted = _norm_conflicted(params.get("conflicted"))
    if (not isinstance(theirs_ref, str) or not theirs_ref or theirs_ref.startswith("-")
            or not isinstance(message, str) or not message.strip()
            or not isinstance(expected, str) or not _SHA.match(expected)
            or not isinstance(resolved_dir, str) or not resolved_dir or conflicted is None):
        return _refuse("bad-params", "theirs_ref, resolved_dir, conflicted, message and a full-sha expected_head are required")
    resolved = Path(resolved_dir)
    resolved = (resolved if resolved.is_absolute() else root / resolved).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return _refuse("bad-params", "resolved_dir is outside the repo")

    repo = _Repo(root)
    heads = repo.git(["rev-parse", "--path-format=absolute", "--git-common-dir", "HEAD", f"{theirs_ref}^{{commit}}"])
    lines = heads.stdout.splitlines()
    if not heads.ok or len(lines) != 3:
        return _refuse("bad-params", "cannot resolve HEAD and theirs_ref")
    git_common, old, theirs = Path(lines[0]), lines[1], lines[2]
    if old != expected:
        return _refuse("head-moved", {"expected": expected, "actual": old})

    computed = _merge_tree(repo, old, theirs)
    if computed is None:
        return _refuse("bad-params", "merge-tree could not compute the merge")
    tree, actual = computed
    if actual != conflicted:
        return _refuse("conflict-set-changed", {
            "new": [p for p in actual if p not in conflicted], "gone": [p for p in conflicted if p not in actual],
        })
    missing, marked = _read_resolved(resolved, conflicted)
    if missing:
        return _refuse("resolved-missing", missing)
    if marked:
        return _refuse("conflict-marker", marked)

    new, failure = _build_commit(repo, git_common, resolved, tree, conflicted, old, theirs, message)
    if new is None:
        return _refuse("sync-failed", failure)

    synced, left_staged, status, staged_before, failure = _sync_shared(repo, old, new)
    refusal = None
    if failure is None:
        after = repo.git(["diff", "--cached", "--name-only", "-z", new], binary=True)
        if not after.ok:
            failure = "diff --cached new failed"
        else:
            extra = sorted(set(_nul_split(after.stdout_bytes)) - staged_before)
            if extra:
                refusal = _refuse("unexpected-staged", extra)
    if failure is None and refusal is None:
        subject = message.strip().splitlines()[0]
        moved = repo.git(["update-ref", "-m", f"commit (merge): {subject}", "HEAD", new, old])
        if moved.ok:
            return {"ok": True, "commit": new, "parents": [old, theirs], "synced": synced, "left_staged": left_staged}
        refusal = _refuse("head-moved", moved.stderr.strip())
    _restore(repo, old, synced, status)
    return refusal or _refuse("sync-failed", failure)


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.commit_merge": one merge-tree, one private-index build, one pre-move sync, one CAS update-ref."""
    if repo_root is None:
        return {"ok": False, "error": f"{OP_NAME} requires repo_root"}
    params = params if isinstance(params, dict) else {}
    root = main_worktree_root(Path(repo_root))
    return commit_merge(root, params)
