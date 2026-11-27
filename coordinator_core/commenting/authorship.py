"""Which comments in a commit range were added by agent-trailered commits."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

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


def _default_runner(args: list[str], cwd: Path, stdin: Optional[bytes]) -> Optional[bytes]:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        check=False,
        **no_console_creationflags(),
    )
    return proc.stdout if proc.returncode == 0 else None


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
                target = line[4:]
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


def agent_added_comments(
    repo_root: Path | str,
    base: str,
    head: str,
    *,
    runner: Optional[GitRunner] = None,
    comments_fn: Optional[Callable[[str, str, str], list[AddedComment]]] = None,
) -> list[AddedComment]:
    """Comments present at `head` and absent at `base` whose text sits in an added line of an agent commit.

    At most two git spawns whatever the range size: one `log -p -U0` and, only when an agent commit
    added lines, one `cat-file --batch`. Any git failure returns [].
    """
    root = Path(repo_root)
    run = runner or _default_runner
    rng = f"{base}..{head}"
    log_raw = run(
        [
            "log", "-p", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
            f"--format={_COMMIT_MARK}%H{_FIELD_MARK}%B{_FIELD_MARK}", rng,
        ],
        root,
        None,
    )
    if not log_raw:
        return []
    added = _agent_added_lines(log_raw.decode("utf-8", errors="replace"))
    paths = sorted(p for p in added if Path(p).suffix in SUFFIXES)
    if not paths:
        return []
    if comments_fn is None:
        try:
            from .added import added_comments as comments_fn
        except ImportError:
            from .added_types import added_comments as comments_fn
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
        agent_text = "\n".join(added[path])
        for comment in comments_fn(path, blobs[2 * i] or "", after):
            lines = [_norm(ln) for ln in comment.text.split("\n")]
            lines = [ln for ln in lines if ln]
            if lines and all(ln in agent_text for ln in lines):
                found.append(comment)
    return found
