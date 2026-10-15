"""
coordinator_core.backlog_grind_assemble.grind_undo -- `grind-row undo`: put the
named paths back to HEAD, leaving and naming any path it may not or cannot touch.

`undo --paths-urlenc E --repo-root R [--decisions-file F]`: the named set is the
fix stage's accumulated touched/created paths. Each path no live peer holds, that
is not a directory, not in the decisions file's `baseline_dirty`, and is a blob or
absent at HEAD becomes equal to HEAD (restored, or deleted when HEAD lacks it).
Every other path is left and named with a reason. At most two git spawns
(`cat-file --batch-check`, `restore`).

Exit codes: 0 nothing left, 1 something left (stdout and stderr carry the
`{restored, removed, left: [{path, reason}]}` JSON), 2 usage (bad token, path
escapes root).

Negative-spec: never `git status`, `clean`, `reset`, `stash`; never a recursive
delete; never releases touch claims; never restores a `baseline_dirty` path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from coordinator_core.contract.grind_vocab import decode_undo_paths
from coordinator_core.git.run import GitResult, run_git
from coordinator_core.git_scope import scoped_git_env
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.grind_commit_contract import BASELINE_DIRTY_KEY


def _git(repo_root: Path, args: list[str], stdin: bytes) -> GitResult:
    """A spawn that cannot start or overruns comes back as a failed result, so
    every caller's non-zero branch leaves the paths with a reason instead of a traceback."""
    return run_git(args, cwd=str(repo_root), input=stdin, env=scoped_git_env())


def _baseline_dirty(decisions_file: str | None) -> set[str] | None:
    """None when a named decisions file cannot be read: the pre-run dirty set is then
    unknown and nothing may be restored."""
    if not decisions_file:
        return set()
    try:
        data = json.loads(Path(decisions_file).read_text(encoding="utf-8"))
        return {str(p) for p in data.get(BASELINE_DIRTY_KEY, [])}
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _report(restored: list[str], removed: list[str], left: dict[str, str]) -> int:
    body = json.dumps(
        {
            "left": [{"path": p, "reason": r} for p, r in sorted(left.items())],
            "removed": sorted(removed),
            "restored": sorted(restored),
        },
        sort_keys=True,
    )
    print(body)
    if left:
        print(f"grind-row undo: {body}", file=sys.stderr)
        return 1
    return 0


def cmd_undo(rest: list[str]) -> int:
    from coordinator_core.backlog_grind_assemble.grind_rows import (
        _declare_before_unlink,
        _declare_under_repo_root,
        _parse_flags,
        _usage,
    )
    from coordinator_core.session import core, scope

    flags = _parse_flags(rest, ("paths-urlenc", "repo-root"), ("decisions-file",))
    if flags is None:
        return _usage(
            "usage: grind-row undo --paths-urlenc <E> --repo-root <dir> [--decisions-file <F>]"
        )
    try:
        raw_paths = decode_undo_paths(flags["paths-urlenc"])
    except ValueError as exc:
        return _usage(f"undo: {exc}")
    repo_root = Path(flags["repo-root"])
    real_root = contained_path(repo_root, [repo_root])
    if real_root is None:
        return _usage(f"undo: --repo-root is not resolvable: {flags['repo-root']!r}")

    rels: list[str] = []
    for raw in raw_paths:
        if "\n" in raw or "\x00" in raw:
            return _usage(f"undo: path has a control character: {raw!r}")
        # Resolve the parent only: the named path itself may be a symlink, and
        # undo acts on the link, never on whatever it points at.
        name = Path(raw).name
        parent = contained_path((real_root / raw).parent, [real_root])
        if parent is None or name in ("", ".", ".."):
            return _usage(f"undo: path escapes --repo-root: {raw!r}")
        rels.append((parent / name).relative_to(real_root).as_posix())
    rels = sorted(set(rels))

    left: dict[str, str] = {}
    baseline = _baseline_dirty(flags.get("decisions-file"))
    if baseline is None:
        return _report([], [], {p: "baseline-unreadable" for p in rels})
    for p in rels:
        if p in baseline:
            left[p] = "baseline"
    todo = [p for p in rels if p not in left]

    root_str = str(real_root)
    sid = core.resolve_session_id(root_str)
    contested = scope.contested_by_live_peers(todo, sid, cwd=root_str) if sid and todo else None
    if todo and contested is None:
        for p in todo:
            left[p] = "contest-unknown"
        return _report([], [], left)
    for p in contested or {}:
        if p in todo:
            left[p] = "live-peer:" + ",".join(sorted(contested[p]))
    todo = [p for p in todo if p not in left]
    for p in todo:
        full = real_root / p
        if full.is_dir() and not full.is_symlink():
            left[p] = "directory"
    todo = [p for p in todo if p not in left]
    if not todo:
        return _report([], [], left)

    tokens = "".join(f"HEAD:{p}\n" for p in todo).encode("utf-8")
    proc = _git(real_root, ["cat-file", "--batch-check=%(objecttype)"], tokens)
    replies = proc.stdout.splitlines() if proc.returncode == 0 else []
    if len(replies) != len(todo):
        reason = f"git-cat-file-failed: {proc.stderr.strip()}" if proc.returncode else "git-cat-file-reply-mismatch"
        for p in todo:
            left[p] = reason
        return _report([], [], left)
    present: list[str] = []
    absent: list[str] = []
    for p, reply in zip(todo, replies):
        if reply == "blob":
            present.append(p)
        elif reply.endswith(" missing"):
            absent.append(p)
        else:
            left[p] = f"head-entry-not-blob: {reply}"

    restored: list[str] = []
    if present:
        stdin = b"".join(p.encode("utf-8") + b"\x00" for p in present)
        proc = _git(
            real_root,
            [
                "--literal-pathspecs",
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            stdin,
        )
        if proc.returncode != 0:
            reason = f"git-restore-failed: {proc.stderr.strip()}"
            for p in present:
                left[p] = reason
        else:
            restored = present
            for p in present:
                _declare_under_repo_root(real_root / p, real_root)

    removed: list[str] = []
    for p in absent:
        target = real_root / p
        if not (target.exists() or target.is_symlink()):
            continue
        try:
            _declare_before_unlink(target, real_root)
            target.unlink()
            removed.append(p)
        except OSError as exc:
            left[p] = f"delete-failed: {exc}"

    return _report(restored, removed, left)
