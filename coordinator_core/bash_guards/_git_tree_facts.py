"""coordinator_core.bash_guards._git_tree_facts -- in-process git facts for the PreToolUse Bash guard path.

Each function answers one question a `_dc_git_probe._run_git` probe asks, by reading
the repository directly (composing `coordinator_core.git`; no parser lives here), so the
commit-shaped hot path spawns no git process.

Contract, shared by every function:

* The argument is the payload cwd. The return is a value or ``None``.
* ``None`` means "could not answer in-process": the caller falls back to its existing
  `_run_git` probe. It never means "allow", "absent" or "empty".
* Any reader failure (unreadable or unsupported repository state, missing object, an
  exception) is returned as ``None``; no function raises.
* Reads only: no lock, no index write.
* Results are memoised per dispatch in a ContextVar, never a module global: two warm
  dispatches interleave (see `_dc_git_probe._git_probe_deadline`). `reset()` clears it.

`staged_name_status` diffs `git_state.read_index` against HEAD's tree, pruning by
in-memory tree hashes so only changed directories' tree objects are read. An index over
`INPROCESS_INDEX_BYTES_MAX` is not parsed here: one budgeted `git diff --cached -M -z` answers
both `staged_name_status` and `staged_name_status_renames`.
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_UNSET = object()

_memo_var: contextvars.ContextVar[Optional[Dict[Tuple[str, str], object]]] = (
    contextvars.ContextVar("_git_tree_facts_memo", default=None)
)


def reset() -> None:
    """Drop the current dispatch's memoised facts."""
    _memo_var.set(None)


def _memoised(kind: str, cwd: str, compute: Callable[[str], object]):
    """Return `compute(cwd)` memoised per (kind, cwd) for this dispatch; exceptions become None."""
    memo = _memo_var.get()
    if memo is None:
        memo = {}
        _memo_var.set(memo)
    key = (kind, cwd)
    hit = memo.get(key, _UNSET)
    if hit is not _UNSET:
        return hit
    try:
        value = compute(cwd)
    except Exception:
        value = None
    memo[key] = value
    return value


_HEADS_PREFIX = "refs/heads/"
_HEX = frozenset("0123456789abcdef")


def _compute_toplevel(cwd: str) -> Optional[str]:
    from coordinator_core.git import repo_root

    top = repo_root.show_toplevel(cwd)
    if not top:
        return None
    return Path(top).as_posix()


def _compute_show_prefix(cwd: str) -> Optional[str]:
    from coordinator_core.bash_guards._declared_delete_license import case_insensitive
    from coordinator_core.git import repo_root

    top = repo_root.show_toplevel(cwd)
    if not top:
        return None
    top_path = Path(top)
    here = Path(repo_root._resolve_cwd(cwd))
    try:
        rel = here.relative_to(top_path)
    except ValueError:
        if not case_insensitive(top_path):
            return None
        top_parts = [p.casefold() for p in top_path.parts]
        here_parts = [p.casefold() for p in here.parts]
        if here_parts[: len(top_parts)] != top_parts:
            return None
        rel = Path(*here.parts[len(top_parts):])
    parts = rel.parts
    if not parts:
        return ""
    if parts[0].casefold() == ".git":
        return None
    return "/".join(parts) + "/"


def _compute_head_ref(cwd: str) -> Optional[str]:
    from coordinator_core.git import git_state

    top = _compute_toplevel(cwd)
    if top is None:
        return None
    name = git_state.head_branch(top)
    if not name or name == "HEAD" or name.startswith("refs/"):
        return None
    return _HEADS_PREFIX + name


def _compute_head_commit(cwd: str) -> Optional[str]:
    from coordinator_core.git import git_state

    top = _compute_toplevel(cwd)
    if top is None:
        return None
    sha = git_state.head_sha(top, fresh=True)
    if not sha or len(sha) != 40 or not set(sha) <= _HEX:
        return None
    return sha


_TREE_MODE = 0o40000
_REGULAR_MODES = frozenset((0o100644, 0o100755))


class _Unanswerable(Exception):
    """Raised inside the staged diff for any shape git would print differently; mapped to None."""


def _nest_index(snap) -> dict:
    root: dict = {}
    for path, entry in snap.items():
        if entry.stage != 0 or entry.mode == _TREE_MODE or len(entry.sha) != 40:
            raise _Unanswerable
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise _Unanswerable
        if isinstance(node.get(parts[-1]), dict):
            raise _Unanswerable
        node[parts[-1]] = entry
    return root


def _tree_sha(node: dict, memo: Dict[int, str]) -> str:
    """Object id git would give the tree for index `node`, hashed in memory (nothing is written)."""
    import hashlib

    rows = []
    for name, child in node.items():
        if isinstance(child, dict):
            mode, sha, key = _TREE_MODE, _tree_sha(child, memo), name + "/"
        else:
            mode, sha, key = child.mode, child.sha, name
        rows.append((key.encode("utf-8", "surrogateescape"),
                     f"{mode:o}".encode() + b" " + name.encode("utf-8", "surrogateescape")
                     + b"\x00" + bytes.fromhex(sha)))
    rows.sort(key=lambda r: r[0])
    body = b"".join(r[1] for r in rows)
    sha = hashlib.sha1(b"tree %d\x00" % len(body) + body).hexdigest()
    memo[id(node)] = sha
    return sha


def _plain_path(path: str) -> bool:
    """True when `--name-status` prints `path` unquoted."""
    return all(32 <= ord(c) < 127 and c not in '"\\' for c in path)


#: Largest index (bytes) the in-process staged diff reads. The question it bounds: "can this
#: diff finish inside the 200ms leg bar" -- parse plus tree hashing cost ~2.5us per entry, so
#: 4 MiB (~30k entries) is ~75ms; a larger index is answered by one `git diff --cached` spawn.
INPROCESS_INDEX_BYTES_MAX = 4 * 1024 * 1024


def _index_too_large(top: str) -> bool:
    from coordinator_core.git.git_dir import resolve_git_dir

    try:
        return (resolve_git_dir(top) / "index").stat().st_size > INPROCESS_INDEX_BYTES_MAX
    except OSError:
        return False


def _spawn_status_m(top: str) -> Optional[Tuple[List[str], List[str]]]:
    """One budgeted `git diff --cached --name-status -M -z`: `(rename_free_rows, m_lines)`.

    `rename_free_rows` expands each `R` into `D old` + `A new` (what `--no-renames` prints);
    `m_lines` are the `-M` lines (`R100\\told\\tnew`). None when the dispatch's probe budget is
    spent, git fails, or a row letter other than A/M/D/R appears.
    """
    import os

    from coordinator_core.bash_guards import _dc_git_probe
    from coordinator_core.git.run import run_git

    if _dc_git_probe._git_probe_budget_spent():
        _dc_git_probe._git_probe_last_failure.set(
            "the guard's git-probe budget was already spent, so the staged diff was never spawned"
        )
        return None
    env = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    res = run_git(["diff", "--cached", "--name-status", "-M", "-z"], cwd=top, env=env, binary=True)
    if res.returncode != 0:
        _dc_git_probe._git_probe_last_failure.set("git exited %d" % res.returncode)
        return None
    toks = [t.decode("utf-8", "surrogateescape") for t in res.stdout_bytes.split(b"\0")]
    if toks and toks[-1] == "":
        toks.pop()
    free: List[Tuple[str, str]] = []
    m_lines: List[str] = []
    i = 0
    while i < len(toks):
        status = toks[i]
        letter = status[:1]
        if letter == "R" and i + 2 < len(toks):
            old, new = toks[i + 1], toks[i + 2]
            free.append((old, f"D\t{old}"))
            free.append((new, f"A\t{new}"))
            m_lines.append(f"{status}\t{old}\t{new}")
            i += 3
        elif letter in ("A", "M", "D") and i + 1 < len(toks):
            free.append((toks[i + 1], f"{letter}\t{toks[i + 1]}"))
            m_lines.append(f"{letter}\t{toks[i + 1]}")
            i += 2
        else:
            return None
    free.sort(key=lambda row: row[0].encode("utf-8", "surrogateescape"))
    return [line for _, line in free], m_lines


def _compute_spawn_status(cwd: str) -> Optional[Tuple[List[str], List[str]]]:
    top = _compute_toplevel(cwd)
    if top is None:
        return None
    return _spawn_status_m(top)


def _compute_staged_name_status(cwd: str) -> Optional[List[str]]:
    from coordinator_core.git import git_state
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object

    top = _compute_toplevel(cwd)
    if top is None:
        return None
    if _index_too_large(top):
        both = _memoised("spawn_status", cwd, _compute_spawn_status)
        return None if both is None else both[0]
    snap = git_state.read_index(top, fresh=True)
    head_tree = git_state.head_tree_sha(top)
    if head_tree is None or len(head_tree) != 40:
        return None
    common_dir = resolve_git_common_dir(top)
    index_root = _nest_index(snap)
    memo: Dict[int, str] = {}
    out: List[Tuple[str, str]] = []

    def head_entries(sha: Optional[str]) -> Dict[str, Tuple[int, str]]:
        if sha is None:
            return {}
        obj = read_object(common_dir, sha)
        if obj is None or obj[0] != "tree":
            raise _Unanswerable
        entries = git_state._parse_tree_entries(obj[1])
        if entries is None:
            raise _Unanswerable
        return entries

    def emit(letter: str, path: str) -> None:
        if not _plain_path(path):
            raise _Unanswerable
        out.append((path, f"{letter}\t{path}"))

    def all_head(prefix: str, sha: str, letter: str) -> None:
        for name, (mode, esha) in head_entries(sha).items():
            if mode == _TREE_MODE:
                all_head(f"{prefix}{name}/", esha, letter)
            else:
                emit(letter, prefix + name)

    def all_index(prefix: str, node: dict, letter: str) -> None:
        for name, child in node.items():
            if isinstance(child, dict):
                all_index(f"{prefix}{name}/", child, letter)
            else:
                emit(letter, prefix + name)

    def diff(prefix: str, sha: Optional[str], node: dict) -> None:
        head = head_entries(sha)
        for name in set(head) | set(node):
            h, i = head.get(name), node.get(name)
            path = prefix + name
            h_dir = h is not None and h[0] == _TREE_MODE
            i_dir = isinstance(i, dict)
            if h_dir and i_dir:
                if memo.get(id(i)) is None:
                    _tree_sha(i, memo)
                if memo[id(i)] != h[1]:
                    diff(path + "/", h[1], i)
            elif h_dir:
                all_head(path + "/", h[1], "D")
                if i is not None:
                    emit("A", path)
            elif i_dir:
                if h is not None:
                    emit("D", path)
                all_index(path + "/", i, "A")
            elif h is None:
                emit("A", path)
            elif i is None:
                emit("D", path)
            elif (h[0], h[1]) != (i.mode, i.sha):
                if (h[0] in _REGULAR_MODES) != (i.mode in _REGULAR_MODES) or (
                    h[0] not in _REGULAR_MODES and h[0] != i.mode
                ):
                    raise _Unanswerable
                emit("M", path)

    try:
        if _tree_sha(index_root, memo) != head_tree:
            diff("", head_tree, index_root)
    except _Unanswerable:
        return None
    out.sort(key=lambda row: row[0].encode("utf-8", "surrogateescape"))
    return [line for _, line in out]


def toplevel(cwd: str) -> Optional[str]:
    """Worktree root of the repo containing `cwd` (`rev-parse --show-toplevel`), or None to probe."""
    return _memoised("toplevel", cwd, _compute_toplevel)


def show_prefix(cwd: str) -> Optional[str]:
    """`cwd` relative to the worktree root, '' at the root, with a trailing '/' otherwise
    (`rev-parse --show-prefix`), or None to probe."""
    return _memoised("show_prefix", cwd, _compute_show_prefix)


def head_ref(cwd: str) -> Optional[str]:
    """Full ref HEAD points at (`symbolic-ref HEAD`), or None to probe -- also when HEAD is detached."""
    return _memoised("head_ref", cwd, _compute_head_ref)


def head_commit(cwd: str) -> Optional[str]:
    """40-hex commit HEAD resolves to (`rev-parse HEAD`), or None to probe -- also when HEAD is unborn."""
    return _memoised("head_commit", cwd, _compute_head_commit)


def staged_name_status(cwd: str) -> Optional[List[str]]:
    """Staged changes as `diff --cached --name-status` lines ('A\\tpath', 'M\\tpath', 'D\\tpath'),
    or None to probe. An empty list means nothing is staged. Rename detection (-M) is not
    performed: a caller that needs it, a SHA-256 repo or a missing object yields None.
    An index over `INPROCESS_INDEX_BYTES_MAX` is answered by one memoised git spawn instead."""
    return _memoised("staged_name_status", cwd, _compute_staged_name_status)


def index_over_bound(cwd: str) -> bool:
    """True when the repo's index exceeds `INPROCESS_INDEX_BYTES_MAX`: a caller that would parse
    the whole index in-process should ask git for a scoped answer instead."""
    top = toplevel(cwd)
    return top is not None and _index_too_large(top)


def staged_name_status_renames(cwd: str) -> Optional[List[str]]:
    """`diff --cached --name-status -M` lines (`R100\\told\\tnew`), or None to probe. Answered only
    for an index over `INPROCESS_INDEX_BYTES_MAX`, from the same single spawn as `staged_name_status`."""
    top = toplevel(cwd)
    if top is None or not _index_too_large(top):
        return None
    both = _memoised("spawn_status", cwd, _compute_spawn_status)
    return None if both is None else both[1]
