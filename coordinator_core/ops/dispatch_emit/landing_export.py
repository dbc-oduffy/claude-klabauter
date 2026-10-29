"""coordinator_core.ops.dispatch_emit.landing_export -- HEAD plus a run's own landing paths.

Purpose: a gate that fails in the shared worktree may be failing on a peer's uncommitted
bytes. Re-running the failing target inside this export (HEAD's tree, with the worktree
bytes of only the paths the run lands) tells the run's own breakage from a peer's.

Arm: overlay an existing run-base export to HEAD. A fresh `git archive HEAD` extraction
measured 23 s on this repo; the overlay measured 328 ms with zero git spawns (C3), so
the base export is an input and its absence is a refusal, never a fallback to extraction.
The tree diff base_sha..HEAD is read from the object store in process; blobs are written
verbatim (no smudge), as `git archive` writes them.

Negative-spec:
    - `build_landing_export` does NOT build a base export. No `base_export` holding `base_sha`
      -> LandingExportError. `build_head_export` is the one function that does (one
      `git archive`, ~23 s); it belongs in the detached child only, never an inline op.
    - Does NOT leave `base_export` intact: it is moved to `out_dir` and mutated in place
      (copying 40k files costs what extraction costs). A caller that still needs the
      base re-runs against it first.
    - Does NOT write outside `<worktree_root>/scratch/`.
    - `build_landing_export` does NOT spawn git, bash, tar or cp.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import head_sha
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.testing import baseline_export

_MODE_TREE = b"40000"
_MODE_SUBMODULE = b"160000"


class LandingExportError(Exception):
    """A refusal; the message is the one-line reason the CLI prints."""


class LandingExport(NamedTuple):
    path: str
    head_sha: str
    overlaid: tuple
    removed: tuple


def _read(common_dir: Path, sha: str, want: str) -> bytes:
    got = git_objects.read_object(common_dir, sha)
    if got is None or got[0] != want:
        raise LandingExportError(f"object {sha[:12]} ({want}) not readable")
    return got[1]


def _commit_tree(common_dir: Path, commit: str) -> str:
    body = _read(common_dir, commit, "commit")
    first = body.split(b"\n", 1)[0]
    if not first.startswith(b"tree "):
        raise LandingExportError(f"commit {commit[:12]} has no tree line")
    return first[5:].decode("ascii")


def _entries(common_dir: Path, tree: Optional[str]) -> dict:
    if tree is None:
        return {}
    data = _read(common_dir, tree, "tree")
    out: dict = {}
    pos = 0
    while pos < len(data):
        sp = data.index(b" ", pos)
        nul = data.index(b"\0", sp)
        mode = data[pos:sp]
        name = data[sp + 1 : nul].decode("utf-8", "surrogateescape")
        out[name] = (mode, data[nul + 1 : nul + 21].hex())
        pos = nul + 21
    return out


def _clear(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _safe_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise LandingExportError(f"unsafe tree entry name {name!r}")
    return name


def _apply_tree(common_dir: Path, old: Optional[str], new: Optional[str], dest: Path) -> None:
    """Make `dest` hold `new`'s tree, given it currently holds `old`'s."""
    if old == new:
        return
    a = _entries(common_dir, old)
    b = _entries(common_dir, new)
    for name in a.keys() - b.keys():
        _clear(dest / _safe_name(name))
    for name, (mode, sha) in b.items():
        target = dest / _safe_name(name)
        prev = a.get(name)
        if prev == (mode, sha):
            continue
        prev_is_tree = prev is not None and prev[0] == _MODE_TREE
        if mode == _MODE_TREE:
            if prev is not None and not prev_is_tree:
                _clear(target)
            target.mkdir(exist_ok=True)
            _apply_tree(common_dir, prev[1] if prev_is_tree else None, sha, target)
        elif mode == _MODE_SUBMODULE:
            if prev is not None:
                _clear(target)
            target.mkdir(exist_ok=True)
        else:
            if prev_is_tree or target.is_symlink():
                _clear(target)
            target.write_bytes(_read(common_dir, sha, "blob"))


def _tracked_at(common_dir: Path, tree: str, rel: str) -> bool:
    cur: Optional[str] = tree
    parts = rel.split("/")
    for i, part in enumerate(parts):
        entry = _entries(common_dir, cur).get(part)
        if entry is None:
            return False
        if i == len(parts) - 1:
            return entry[0] != _MODE_TREE
        if entry[0] != _MODE_TREE:
            return False
        cur = entry[1]
    return False


def _norm_landing(root: Path, raw: str) -> str:
    if not raw or "\0" in raw:
        raise LandingExportError("empty landing path")
    p = raw.replace("\\", "/")
    if ".." in p.split("/"):
        raise LandingExportError(f"landing path with '..': {raw}")
    candidate = Path(p)
    if candidate.is_absolute() or (len(p) > 1 and p[1] == ":"):
        try:
            p = candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            raise LandingExportError(f"landing path outside the repo: {raw}") from None
    while p.startswith("./"):
        p = p[2:]
    if not p or p == ".git" or p.startswith(".git/"):
        raise LandingExportError(f"landing path not allowed: {raw}")
    return p


def build_landing_export(
    worktree_root: Path,
    out_dir: Path,
    landing_paths: Sequence[str],
    *,
    base_export: Optional[Path] = None,
    base_sha: Optional[str] = None,
) -> LandingExport:
    """Materialise HEAD plus `landing_paths` (worktree bytes) at `out_dir`.

    `base_export` must hold the tree of commit `base_sha`; it is consumed (moved to
    `out_dir`). Argument refusals raise LandingExportError before `base_export` is touched; a
    failure while overlaying leaves it already moved to `out_dir`, half-applied.
    """
    root = Path(worktree_root).resolve()
    scratch = (root / "scratch").resolve()
    out = Path(out_dir)
    out = (out if out.is_absolute() else root / out).resolve()
    if out == scratch or scratch not in out.parents:
        raise LandingExportError(f"out_dir must be under {scratch.name}/ of the repo")
    paths = [_norm_landing(root, p) for p in landing_paths]
    if base_export is None or not base_sha:
        raise LandingExportError("no run_base export given (base_export and base_sha)")
    base = Path(base_export).resolve()
    if not base.is_dir():
        raise LandingExportError(f"base export is not a directory: {base_export}")
    if out.exists() and out != base:
        raise LandingExportError("out_dir already exists")
    head = head_sha(root, fresh=True)
    if not head:
        raise LandingExportError("HEAD unresolved")
    common = resolve_git_common_dir(root)
    try:
        head_tree = _commit_tree(common, head)
        base_tree = _commit_tree(common, base_sha)
    except (OSError, ValueError) as exc:
        raise LandingExportError(f"object store unreadable: {exc}") from None
    for rel in paths:
        src = root / rel
        if src.is_dir():
            raise LandingExportError(f"landing path is a directory: {rel}")
    if out != base:
        out.parent.mkdir(parents=True, exist_ok=True)
        os.replace(base, out)
    try:
        _apply_tree(common, base_tree, head_tree, out)
        overlaid, removed = [], []
        for rel in paths:
            src = root / rel
            dst = out / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.is_dir():
                    shutil.rmtree(dst)
                dst.write_bytes(src.read_bytes())
                overlaid.append(rel)
            elif _tracked_at(common, head_tree, rel):
                _clear(dst)
                removed.append(rel)
    except (OSError, ValueError) as exc:
        raise LandingExportError(f"export failed: {exc}") from None
    return LandingExport(out.as_posix(), head, tuple(overlaid), tuple(removed))


def run_export_root(worktree_root: Path, run_id: str) -> Path:
    """`<root>/scratch/warp/<run_id>/export`; LandingExportError on an unsafe `run_id`."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        raise LandingExportError(f"unsafe run id: {run_id!r}")
    return Path(worktree_root).resolve() / "scratch" / "warp" / run_id / "export"


def check_cap_adapting(root: Path, *, keep_run: Optional[str] = None) -> None:
    """`export_lifecycle.check_export_cap(root)`; on a refusal, sweep orphan exports once and
    re-check. Only the second refusal raises, with the remedy appended."""
    from coordinator_core.ops.dispatch_emit import export_lifecycle

    try:
        export_lifecycle.check_export_cap(root)
        return
    except LandingExportError:
        pass
    export_lifecycle.sweep_orphan_exports(root, keep_run=keep_run)
    try:
        export_lifecycle.check_export_cap(root)
    except LandingExportError as exc:
        raise LandingExportError(
            f"{exc}; raise {export_lifecycle.CAP_ENV} or free disk"
        ) from None


_ALLOWLIST_REL = "docs/reference/state-corpus-allowlist.txt"


def _seed_state_invariant_inputs(common_dir: Path, head_tree: str, dest: Path) -> None:
    """Give a DATA_DIRS-less extraction HEAD's state allowlist and one placeholder per tracked
    state/ first segment (empty dir, empty file for a tracked top-level file), read from the
    object store with no git spawn. The overlay then writes over them."""
    top = _entries(common_dir, head_tree)
    state = top.get("state")
    if state is not None and state[0] == _MODE_TREE:
        (dest / "state").mkdir(exist_ok=True)
        for name, (mode, _sha) in _entries(common_dir, state[1]).items():
            seg = dest / "state" / _safe_name(name)
            if mode == _MODE_TREE or mode == _MODE_SUBMODULE:
                seg.mkdir(exist_ok=True)
            else:
                seg.touch()
    cur: Optional[str] = head_tree
    parts = _ALLOWLIST_REL.split("/")
    for part in parts[:-1]:
        entry = _entries(common_dir, cur).get(part)
        if entry is None or entry[0] != _MODE_TREE:
            return
        cur = entry[1]
    entry = _entries(common_dir, cur).get(parts[-1])
    if entry is not None and entry[0] not in (_MODE_TREE, _MODE_SUBMODULE):
        target = dest / _ALLOWLIST_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_read(common_dir, entry[1], "blob"))


def build_head_export(
    worktree_root: Path, out_dir: Path, landing_paths: Sequence[str]
) -> LandingExport:
    """Extract HEAD (one git spawn) at `out_dir`, then overlay `landing_paths` worktree bytes.

    A prior export at `out_dir` is replaced. The extraction goes to the sibling `<out_dir>.base`,
    which `build_landing_export` consumes; on failure it is removed. DATA_DIRS stay unextracted;
    only HEAD's state allowlist and the tracked state/ first-segment set are seeded.
    """
    root = Path(worktree_root).resolve()
    scratch = (root / "scratch").resolve()
    out = Path(out_dir)
    out = (out if out.is_absolute() else root / out).resolve()
    if out == scratch or scratch not in out.parents:
        raise LandingExportError(f"out_dir must be under {scratch.name}/ of the repo")
    for p in landing_paths:
        _norm_landing(root, p)
    head = head_sha(root, fresh=True)
    if not head:
        raise LandingExportError("HEAD unresolved")
    warp = root / "scratch" / "warp"
    keep = out.relative_to(warp).parts[0] if warp in out.parents else None
    check_cap_adapting(root, keep_run=keep)
    staging = out.with_name(out.name + ".base")
    try:
        if out.exists() or out.is_symlink():
            baseline_export.remove(out)
        if staging.exists() or staging.is_symlink():
            baseline_export.remove(staging)
        staging.mkdir(parents=True)
        baseline_export.export(root.as_posix(), head, staging)
        common = resolve_git_common_dir(root)
        _seed_state_invariant_inputs(common, _commit_tree(common, head), staging)
        return build_landing_export(
            root, out, landing_paths, base_export=staging, base_sha=head
        )
    except (baseline_export.ExportError, OSError, ValueError) as exc:
        raise LandingExportError(f"head export failed: {exc}") from None
    finally:
        if staging.exists():
            baseline_export.remove(staging)


def _find_root(start: Path) -> Path:
    for cand in (start, *start.parents):
        if (cand / ".git").exists():
            return cand
    raise LandingExportError("not inside a git worktree")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    base_export: Optional[str] = None
    base_sha: Optional[str] = None
    rest: list = []
    i = 0
    try:
        while i < len(args):
            if args[i] in ("--base-export", "--base-sha"):
                if i + 1 >= len(args):
                    raise LandingExportError(f"{args[i]} needs a value")
                if args[i] == "--base-export":
                    base_export = args[i + 1]
                else:
                    base_sha = args[i + 1]
                i += 2
            else:
                rest.append(args[i])
                i += 1
        if len(rest) < 2:
            raise LandingExportError("usage: landing_export <out_dir> <path>...")
        result = build_landing_export(
            _find_root(Path.cwd().resolve()),
            Path(rest[0]),
            rest[1:],
            base_export=Path(base_export) if base_export else None,
            base_sha=base_sha,
        )
    except LandingExportError as exc:
        print(f"landing_export refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "path": result.path,
                "head_sha": result.head_sha,
                "overlaid": list(result.overlaid),
                "removed": list(result.removed),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
