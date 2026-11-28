"""Which comments in a commit range were added by agent-trailered commits."""
from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.git.provider_attribution import ATTRIBUTIONS
from coordinator_core.win_portability import no_console_creationflags

from .added_types import SUFFIXES, AddedComment

GitRunner = Callable[[list[str], Path, Optional[bytes]], Optional[bytes]]

_SESSION_ID_TRAILER_RE = re.compile(r"(?m)^Session-Id:[ \t]*(\S+)[ \t]*$")
_CO_AUTHOR_RE = re.compile(r"(?mi)^Co-Authored-By:[^\n<]*<([^>\s]+)>[ \t]*$")
_NON_SESSION_IDS = frozenset({"claude-klabauter-engine"})
_AGENT_ADDRESSES = frozenset(
    a[a.index("<") + 1 : a.rindex(">")].lower() for a in ATTRIBUTIONS.values()
)
_COMMIT_MARK = "\x1e"
_FIELD_MARK = "\x1f"
_MARKER_PREFIX_RE = re.compile(r"^(?:#+|//+|/\*+|\*+|<!--|\"\"\"|''')\s*")


def is_agent_commit(message: str) -> bool:
    """True iff the message carries an agent Session-Id trailer or an agent Co-Authored-By; never the author name."""
    for sid in _SESSION_ID_TRAILER_RE.findall(message):
        if sid not in _NON_SESSION_IDS:
            return True
    return any(addr.lower() in _AGENT_ADDRESSES for addr in _CO_AUTHOR_RE.findall(message))


def _norm(line: str) -> str:
    return _MARKER_PREFIX_RE.sub("", line.strip()).strip()


def _ends_at_boundary(line: str, tail: str) -> bool:
    return line.endswith(tail) and (len(line) == len(tail) or not line[-len(tail) - 1].isalnum())


def default_runner(args: list[str], cwd: Path, stdin: Optional[bytes]) -> Optional[bytes]:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        check=False,
        **no_console_creationflags(),
    )
    return proc.stdout if proc.returncode == 0 else None


@functools.lru_cache(maxsize=4)
def _agent_added_lines(log_out: str) -> dict[str, list[str]]:
    """Per path, the added lines of agent commits in a `%x1e%H%x1f%B%x1f` + `-p -U0` log."""
    by_path: dict[str, list[str]] = {}
    for chunk in log_out.split(_COMMIT_MARK):
        parts = chunk.split(_FIELD_MARK, 2)
        if len(parts) != 3 or not is_agent_commit(parts[1]):
            continue
        path: Optional[str] = None
        in_header = False
        for line in parts[2].split("\n"):
            line = line.rstrip("\r")
            if line.startswith("diff --git "):
                path, in_header = None, True
            elif in_header and line.startswith("+++ "):
                target = line[4:].rstrip("\t")
                path = target[2:] if target.startswith("b/") else None
            elif line.startswith("@@"):
                in_header = False
            elif not in_header and path is not None and line.startswith("+"):
                by_path.setdefault(path, []).append(line[1:])
    return by_path


def _parse_cat_file(out: bytes, count: int) -> list[Optional[str]]:
    """Contents in request order from `cat-file --batch`; None for a missing object."""
    results: list[Optional[str]] = []
    pos = 0
    for _ in range(count):
        eol = out.find(b"\n", pos)
        if eol < 0:
            results.append(None)
            continue
        header = out[pos:eol].split()
        pos = eol + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            results.append(out[pos : pos + size].decode("utf-8", errors="replace"))
            pos += size + 1
        else:
            results.append(None)
    return results


_CLIKE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".mjs", ".cpp", ".h", ".hpp", ".cs", ".jsonc"})


def may_hold_comment(path: str, added: list[str]) -> bool:
    """Whether any agent-added line carries a comment or docstring opener for ``path``'s language."""
    # invariant: an agent comment has every line added, its opener line included.
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        openers: tuple[str, ...] = ("#", '"""', "'''")
    elif suffix in _CLIKE_SUFFIXES:
        openers = ("//", "/*")
    else:
        openers = ("#",)
    return any(o in line for line in added for o in openers)


_CODE_PATHSPECS = tuple(f"*{suffix}" for suffix in sorted(SUFFIXES))


def agent_added_comments(
    repo_root: Path | str,
    base: str,
    head: str,
    *,
    runner: Optional[GitRunner] = None,
    comments_fn: Optional[Callable[[str, str, str], list[AddedComment]]] = None,
    paths: Optional[Sequence[str]] = None,
) -> list[AddedComment]:
    """Comments at `head` absent at `base` whose text sits in an agent commit's added lines; at most two git spawns, [] on git failure. `paths` (repo-relative) limits the log to those code files; empty spawns nothing."""
    root = Path(repo_root)
    run = runner or default_runner
    rng = f"{base}..{head}"
    if paths is None:
        pathspecs: tuple[str, ...] = _CODE_PATHSPECS
    else:
        pathspecs = tuple(
            f":(literal){p}" for p in dict.fromkeys(paths) if Path(p).suffix.lower() in SUFFIXES
        )
        if not pathspecs:
            return []
    log_raw = run(
        [
            "log", "-p", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
            f"--format={_COMMIT_MARK}%H{_FIELD_MARK}%B{_FIELD_MARK}", rng,
            "--", *pathspecs,
        ],
        root,
        None,
    )
    if not log_raw:
        return []
    added = _agent_added_lines(log_raw.decode("utf-8", errors="replace"))
    code = [p for p in added if Path(p).suffix.lower() in SUFFIXES]
    paths = sorted(p for p in code if may_hold_comment(p, added[p]))
    if not paths:
        if code and runner is not None:
            run(["cat-file", "--batch"], root, b"")
        return []
    if comments_fn is None:
        from .added import added_comments as comments_fn
    request = "".join(f"{base}:{p}\n{head}:{p}\n" for p in paths).encode("utf-8")
    blobs_raw = run(["cat-file", "--batch"], root, request)
    if blobs_raw is None:
        return []
    blobs = _parse_cat_file(blobs_raw, 2 * len(paths))
    found: list[AddedComment] = []
    for i, path in enumerate(paths):
        after = blobs[2 * i + 1]
        if after is None:
            continue
        agent_lines = [ln.rstrip() for ln in added[path]]
        for comment in comments_fn(path, blobs[2 * i] or "", after):
            lines = [_norm(ln) for ln in comment.text.split("\n")]
            lines = [ln for ln in lines if ln]
            if lines and all(any(_ends_at_boundary(a, ln) for a in agent_lines) for ln in lines):
                found.append(comment)
    return found
