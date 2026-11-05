"""foreign_edits: which dirty paths are not a bug-blitz row's own, who holds them, and a HEAD tree to judge against.

`foreign_dirty` answers "which uncommitted paths are not this row's own edits" (one `git status`
spawn). `holders_of` answers "which live session holds each" (no spawn). `head_tree` answers "what
is HEAD plus this row's own edits": one shared base per HEAD sha under `temp_layout.managed_tempdir()`,
built once from a private index (cold: rev-parse + read-tree + checkout-index = 3 spawns; warm:
rev-parse = 1 spawn), plus a per-row hardlink mirror with the row's own files overlaid from the
live tree. Bulk-transfer class (not an engine op, outside the DR-344 bar). The tree lives outside
the repo on purpose: pytest walks ancestor conftest files, and a tree under the repo root would
load the live root conftest.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Sequence

from coordinator_core.git.run import run_git
from coordinator_core.temp_layout import managed_tempdir

UNATTRIBUTED = "unattributed"
_CACHE_DIRNAME = "coordinator-bug-blitz"
_SHA_DIR_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_STALE_TMP_S = 3600.0


def _normalize(repo_root: Path, raw: str) -> str:
    """Repo-relative POSIX form of *raw*; "" for an absolute path outside the repo or an escaping one."""
    p = Path(raw)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(repo_root.resolve())
        except ValueError:
            return ""
    rel = p.as_posix()
    if rel == ".." or rel.startswith("../") or "/../" in rel:
        return ""
    return rel


def foreign_dirty(repo_root: Path, own: Sequence[str]) -> list[str]:
    """Repo-relative POSIX paths git reports dirty (untracked included) that are not in *own*.

    One `git status --porcelain -z --untracked-files=all` spawn. A git failure returns [] so the
    caller keeps judging the live tree as before.
    """
    repo_root = Path(repo_root)
    res = run_git(
        ["status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=str(repo_root),
    )
    if res.returncode != 0:
        return []
    own_set = {n for n in (_normalize(repo_root, o) for o in own) if n}
    out: list[str] = []
    seen: set[str] = set()
    tokens = res.stdout.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC" or entry[1] in "RC":
            i += 1  # the rename/copy source path follows as its own token
        path = entry[3:]
        if path in own_set or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def holders_of(repo_root: Path, paths: Sequence[str], sid: str) -> dict[str, list[str]]:
    """Map each of *paths* to the live peer sessions holding it, else `["unattributed"]`.

    Wraps `scope.contested_by_live_peers`; a path it omits, or every path when it returns None
    ("could not establish"), reads `unattributed`, never "no holder". No spawn.
    """
    from coordinator_core.session.scope import contested_by_live_peers

    wanted = [p for p in paths if p]
    contested = contested_by_live_peers(wanted, sid, cwd=str(repo_root))
    result: dict[str, list[str]] = {}
    for p in wanted:
        holders = list((contested or {}).get(p) or [])
        result[p] = holders or [UNATTRIBUTED]
    return result


def _cache_parent(repo_root: Path) -> Path:
    key = hashlib.sha1(str(Path(repo_root).resolve()).encode("utf-8")).hexdigest()[:12]
    return managed_tempdir() / _CACHE_DIRNAME / key


def _build_base(repo_root: Path, base: Path, sha: str) -> None:
    parent = base.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    index = parent / f".index-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index)
    try:
        tmp.mkdir()
        rt = run_git(["read-tree", sha], cwd=str(repo_root), env=env)
        if rt.returncode != 0:
            raise RuntimeError(f"head_tree: read-tree failed: {rt.stderr.strip()}")
        prefix = tmp.as_posix().rstrip("/") + "/"
        co = run_git(["checkout-index", "-a", f"--prefix={prefix}"], cwd=str(repo_root), env=env)
        if co.returncode != 0:
            raise RuntimeError(f"head_tree: checkout-index failed: {co.stderr.strip()}")
        try:
            os.replace(tmp, base)
        except OSError:
            if not base.is_dir():
                raise
            # a concurrent builder won the race for this sha; its base is as good as ours
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            index.unlink()
        except OSError:
            pass


def _prune_siblings(base: Path) -> None:
    """Best-effort: drop cached trees of other HEAD shas and stale half-built dirs.

    A per-row mirror holds hardlinks, not the base, so removing an old base never breaks a live row.
    """
    now = time.time()
    try:
        for entry in base.parent.iterdir():
            if entry == base:
                continue
            if _SHA_DIR_RE.match(entry.name):
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.name.startswith((".tmp-", ".index-", "row-")):
                try:
                    if now - entry.stat().st_mtime > _STALE_TMP_S:
                        if entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _raise(err: OSError) -> None:
    raise err


def _mirror(base: Path, dest: Path) -> None:
    # os.walk swallows listing errors by default; a base pruned mid-mirror must fail, not yield a partial tree.
    for root, dirnames, filenames in os.walk(base, onerror=_raise):
        rel = Path(root).relative_to(base)
        target_dir = dest / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in list(dirnames):
            src = Path(root) / name
            if src.is_symlink():
                dirnames.remove(name)
                _copy_symlink(src, target_dir / name)
        for name in filenames:
            src = Path(root) / name
            dst = target_dir / name
            if src.is_symlink():
                _copy_symlink(src, dst)
                continue
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)


def _copy_symlink(src: Path, dst: Path) -> None:
    try:
        os.symlink(os.readlink(src), dst)
    except OSError:
        pass


def head_tree(repo_root: Path, own: Sequence[str]) -> Path:
    """A per-row directory holding HEAD's tree with each *own* path overlaid from the live tree.

    The shared base (one per HEAD sha) is never written after its build: each own entry is
    unlinked in the row's mirror first, then copied, because writing into a hardlinked file would
    write through to the base. An own path absent from the live tree (a row deletion) stays
    absent. The caller removes the returned directory.
    """
    repo_root = Path(repo_root)
    head = run_git(["rev-parse", "HEAD"], cwd=str(repo_root))
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        raise RuntimeError(f"head_tree: cannot resolve HEAD: {head.stderr.strip()}")
    base = _cache_parent(repo_root) / sha
    if not base.is_dir():
        _build_base(repo_root, base, sha)
        _prune_siblings(base)
    row = base.parent / f"row-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        _mirror(base, row)
        for raw in own:
            rel = _normalize(repo_root, raw)
            if not rel:
                continue
            dst = row / rel
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            src = repo_root / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    except BaseException:
        shutil.rmtree(row, ignore_errors=True)
        raise
    return row
