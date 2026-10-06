"""
coordinator_core.backlog_grind_assemble.grind_undo -- `grind-row undo`: put the
named paths back to HEAD, only when this session declared them and no live peer
holds them.

`undo --paths-urlenc E --repo-root R`: each path becomes equal to HEAD (restored
when HEAD has it, deleted when HEAD does not). All-or-nothing predicate before
any write; at most two git spawns (`cat-file --batch-check`, `restore`).

Exit codes: 0 ok, 1 refusal (undeclared, contested, directory, tree/submodule at
HEAD, git failure, unresolvable session), 2 usage (bad token, path escapes root).

Negative-spec: never `git status`, `clean`, `reset`, `stash`; never a recursive
delete; never releases touch claims; never accepts a path outside
`own_path_claims`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from coordinator_core.contract.grind_vocab import decode_undo_paths
from coordinator_core.git.run import GitResult, run_git
from coordinator_core.git_scope import scoped_git_env
from coordinator_core.ops._path_guard import contained_path


def _refuse(msg: str) -> int:
    print(f"grind-row undo: {msg}", file=sys.stderr)
    return 1


def _git(repo_root: Path, args: list[str], stdin: bytes) -> GitResult:
    """A spawn that cannot start or overruns comes back as a failed result, so
    every caller's non-zero branch refuses with exit 1 instead of a traceback."""
    return run_git(args, cwd=str(repo_root), input=stdin, env=scoped_git_env())


def cmd_undo(rest: list[str]) -> int:
    from coordinator_core.backlog_grind_assemble.grind_rows import (
        _declare_before_unlink,
        _declare_under_repo_root,
        _parse_flags,
        _usage,
    )
    from coordinator_core.session import core, scope

    flags = _parse_flags(rest, ("paths-urlenc", "repo-root"))
    if flags is None:
        return _usage("usage: grind-row undo --paths-urlenc <E> --repo-root <dir>")
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

    root_str = str(real_root)
    sid = core.resolve_session_id(root_str)
    if not sid:
        return _refuse("no resolvable session id; cannot establish a declared claim")
    claims = set(scope.own_path_claims(sid, cwd=root_str))
    undeclared = [p for p in rels if p not in claims]
    if undeclared:
        return _refuse(f"paths this session never declared: {json.dumps(undeclared)}")
    contested = scope.contested_by_live_peers(rels, sid, cwd=root_str)
    if contested is None:
        return _refuse("could not establish whether a live peer holds these paths")
    if contested:
        return _refuse(f"paths held by a live peer: {json.dumps(contested, sort_keys=True)}")
    dirs = [p for p in rels if (real_root / p).is_dir() and not (real_root / p).is_symlink()]
    if dirs:
        return _refuse(f"paths that are directories (no recursive delete): {json.dumps(dirs)}")

    tokens = "".join(f"HEAD:{p}\n" for p in rels).encode("utf-8")
    proc = _git(real_root, ["cat-file", "--batch-check=%(objecttype)"], tokens)
    if proc.returncode != 0:
        return _refuse(proc.stderr.strip() or "git cat-file failed")
    replies = proc.stdout.splitlines()
    if len(replies) != len(rels):
        return _refuse("git cat-file reply did not match the paths asked about")
    present: list[str] = []
    absent: list[str] = []
    for p, reply in zip(rels, replies):
        if reply == "blob":
            present.append(p)
        elif reply.endswith(" missing"):
            absent.append(p)
        else:
            return _refuse(f"HEAD entry for {p!r} is {reply!r}, not a blob")

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
            return _refuse(proc.stderr.strip() or "git restore failed")
        for p in present:
            _declare_under_repo_root(real_root / p, real_root)

    left: list[str] = []
    removed: list[str] = []
    for p in absent:
        target = real_root / p
        if not (target.exists() or target.is_symlink()):
            continue
        try:
            _declare_before_unlink(target, real_root)
            target.unlink()
            removed.append(p)
        except OSError:
            left.append(p)
    if left:
        return _refuse(f"restore done; could not delete: {left}")

    print(json.dumps({"removed": sorted(removed), "restored": sorted(present)}, sort_keys=True))
    return 0
