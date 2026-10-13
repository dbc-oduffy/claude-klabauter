"""The session's own history window: one git spawn floored at the session's head_at_start.

`read_session_history(root, sid)` returns every commit in `head_at_start..HEAD` with parents, trailers
and rename-detected files. An unresolved history is never an empty one: read `resolved` before
trusting `window == ()` as "nothing committed". There is no full-history fallback in any path.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Bounds the one window walk; matches `workstream_complete._REVIEW_SCALE_GIT_TIMEOUT`.
_GIT_LOG_TIMEOUT_S = 15
_RS, _US, _GS = "\x1e", "\x1f", "\x1d"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SESSION_ID_RE = re.compile(r"^Session-Id:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
_DELIVERABLE_ID_RE = re.compile(r"^Deliverable-Id:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
_LOG_FORMAT = f"{_RS}%H{_US}%P{_US}%ct{_US}%cI{_US}%s{_US}%B{_GS}"


@dataclass(frozen=True)
class FileChange:
    """One file in a commit: destination path (forward slashes, repo-relative), `--raw` status letter.

    `added`/`deleted` are None for a binary row.
    """

    path: str
    status: str
    added: int | None
    deleted: int | None


@dataclass(frozen=True)
class SessionCommit:
    """One commit of the window. `files` is () for a merge (git prints no diff for one).

    `committer_epoch`, `committed_at` and `subject` have no reader in this slice: they are the quick-wrap
    brief's inputs (Plan B), pinned by `test_public_shape_is_pinned`.
    """

    sha: str
    parents: tuple[str, ...]
    committer_epoch: int
    committed_at: str
    subject: str
    session_ids: tuple[str, ...]
    deliverable_id: str
    files: tuple[FileChange, ...]

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass(frozen=True)
class SessionHistory:
    """`window` is EVERY commit in floor..head, oldest first; `own` is the subset carrying `sid`.

    `reason` is "" when resolved; otherwise a stable string and `window == ()`.
    """

    sid: str
    floor: str | None
    head: str | None
    window: tuple[SessionCommit, ...]
    resolved: bool
    reason: str

    @property
    def own(self) -> tuple[SessionCommit, ...]:
        return tuple(c for c in self.window if self.sid in c.session_ids)


def _unresolved(sid: str, floor: str | None, head: str | None, reason: str) -> SessionHistory:
    return SessionHistory(sid, floor, head, (), False, reason)


def _parse_files(diff_text: str) -> tuple[FileChange, ...]:
    raw: list[tuple[str, str]] = []
    counts: list[tuple[int | None, int | None]] = []
    for line in diff_text.replace("\r\n", "\n").split("\n"):
        if line.startswith(":"):
            meta, _, paths = line.partition("\t")
            status = meta.split(" ")[-1][:1]
            raw.append((status, paths.split("\t")[-1].replace("\\", "/")))
        elif line and line[0] in "0123456789-" and line.count("\t") >= 2:
            a, d, _ = line.split("\t", 2)
            counts.append((None if a == "-" else int(a), None if d == "-" else int(d)))
    return tuple(
        FileChange(path, status, *(counts[i] if i < len(counts) else (None, None)))
        for i, (status, path) in enumerate(raw)
    )


def _parse_commit(chunk: str) -> SessionCommit:
    header, _, diff_text = chunk.partition(_GS)
    sha, parents, epoch, iso, subject, body = header.split(_US, 5)
    session_ids = tuple(dict.fromkeys(_SESSION_ID_RE.findall(body)))
    deliverables = _DELIVERABLE_ID_RE.findall(body)
    return SessionCommit(
        sha=sha.strip(),
        parents=tuple(parents.split()),
        committer_epoch=int(epoch),
        committed_at=iso,
        subject=subject,
        session_ids=session_ids,
        deliverable_id=deliverables[-1] if deliverables else "",
        files=_parse_files(diff_text),
    )


def _resolve_floor(root: Path, sid: str) -> tuple[str | None, str]:
    """(floor, "") on success, (None, reason) otherwise. No spawn."""
    if not sid:
        return None, "no-session-dir"
    from coordinator_core.session.core import session_dir

    try:
        sdir = session_dir(sid, str(root))
    except ValueError:
        return None, "no-session-dir"
    if not sdir:
        return None, "no-session-dir"
    try:
        text = (Path(sdir) / "head_at_start").read_text(encoding="utf-8").strip()
    except OSError:
        return None, "floor-missing"
    if text == "unknown":
        return None, "floor-unknown"
    if not _SHA_RE.fullmatch(text):
        return None, "floor-malformed"
    return text, ""


def read_session_history(root: Path, sid: str, *, numstat: bool = True) -> SessionHistory:
    """`numstat=False` walks `--raw` only: every `FileChange.added`/`deleted` is None."""
    floor, reason = _resolve_floor(root, sid)
    if floor is None:
        return _unresolved(sid, None, None, reason)

    from coordinator_core.git.git_state import head_sha

    head = head_sha(root)
    if not head:
        return _unresolved(sid, floor, None, "head-unresolved")
    if floor == head:
        return SessionHistory(sid, floor, head, (), True, "")

    from coordinator_core.win_portability import no_console_creationflags

    cmd = [
        "git", "--no-optional-locks", "-c", "core.quotepath=false", "log", "--reverse",
        "--raw", *(("--numstat",) if numstat else ()), "-M", f"--format={_LOG_FORMAT}",
        f"{floor}..{head}",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_GIT_LOG_TIMEOUT_S, **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return _unresolved(sid, floor, head, "git-timeout")
    except (OSError, subprocess.SubprocessError):
        return _unresolved(sid, floor, head, "git-spawn-failed")
    if proc.returncode != 0:
        return _unresolved(sid, floor, head, f"git-exit-{proc.returncode}")

    window = tuple(_parse_commit(c) for c in proc.stdout.split(_RS)[1:])
    if not any(floor in c.parents for c in window):
        return _unresolved(sid, floor, head, "floor-not-ancestor")
    return SessionHistory(sid, floor, head, window, True, "")
