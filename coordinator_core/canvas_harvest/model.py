"""canvas_harvest.model - the shared contract of the canvas harvest op.

Holds the record schema name, the refusal reasons, the frozen read types, the one
JSON serializer and the path check every writer in the package calls. Stdlib only,
plus coordinator_core.ops._path_guard for containment. Every file the package writes
goes through coordinator_core.session.claimed_write directly; there is no wrapper here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import contained_path

SCHEMA = "canvas-harvest-record/1"

# The most files one record may hold; a larger record is refused before any write.
MAX_RECORD_FILES = 48

PROJECT_PREFIX = "project/"


def is_project_file(path: object) -> bool:
    """True iff the canvas-relative `path` is a file under project/**; type runtime files answer False."""
    return isinstance(path, str) and path.startswith(PROJECT_PREFIX) and len(path) > len(PROJECT_PREFIX)


CANVAS_UNREADABLE = "canvas-unreadable"
READ_ERROR_UNCLASSIFIED = "read-error-unclassified"
NO_TRANSCRIPT = "no-transcript"
NO_MAIN_LOOP_READ = "no-main-loop-read"
FRAMES_READ_INCOMPLETE = "frames-read-incomplete"
COMMENTS_READ_INCOMPLETE = "comments-read-incomplete"
FRAME_SOURCE_MISSING = "frame-source-missing"
FRAME_SHA_MISMATCH = "frame-sha-mismatch"
UNSAFE_PATH = "unsafe-path"
NO_HARVEST_RECORD = "no-harvest-record"
RECORD_UNPARSEABLE = "record-unparseable"
PROBE_READS_STALE = "probe-reads-stale"
RECORD_ARTIFACT_MISMATCH = "record-artifact-mismatch"
NO_SESSION_ID = "no-session-id"
RECORD_TOO_LARGE = "record-too-large"


class HarvestError(Exception):
    """A named refusal: `reason` is one of the reason constants above, `detail` the one-line specifics."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FrameSource:
    path: str
    saved_to: str
    sha256: str


@dataclass(frozen=True)
class ArtifactRead:
    url: str
    artifact_id: str
    title: str
    type: str
    version: Optional[str]
    frames: tuple
    read_at: str


@dataclass(frozen=True)
class CommentsRead:
    """`threads` is the list of thread dicts exactly as parsed from the payload."""

    threads: list
    rendered_text: str
    read_at: str


@dataclass(frozen=True)
class VersionRead:
    version: str
    read_at: str


@dataclass(frozen=True)
class ReadError:
    """`kind` is `not-found`, `needs-approval`, or None when the text matched no marker."""

    kind: Optional[str]
    first_line: str
    at: str


@dataclass(frozen=True)
class Lifted:
    artifact_read: Optional[ArtifactRead]
    comments: Optional[CommentsRead]
    version: Optional[VersionRead]
    error: Optional[ReadError]
    transcript_dir: Optional[Path]
    session_id: Optional[str]


@dataclass(frozen=True)
class RecordWrite:
    """`written` and `deleted` are tuples of Path; `deleted` holds files a re-harvest removed."""

    written: tuple
    deleted: tuple


NOT_FOUND = "not-found"
NEEDS_APPROVAL = "needs-approval"

# (first-line prefix, kind); the two texts of DoE's fixture unreadable/ directory.
UNREADABLE_MARKERS = (
    ("artifact not found", NOT_FOUND),
    ("Nothing was done. This needs a yes to:", NEEDS_APPROVAL),
)


def classify_error(text: str) -> Optional[str]:
    """Map an error result's text to `not-found`, `needs-approval` or None."""
    first = (text or "").lstrip().split("\n", 1)[0]
    if first.startswith("Error: "):
        first = first[len("Error: "):]
    for prefix, kind in UNREADABLE_MARKERS:
        if first.startswith(prefix):
            return kind
    return None


_ARTIFACT_URL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://)?claude\.ai/artifact/([A-Za-z0-9_-]+)/?(?:[?#].*)?$")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def artifact_id_from_url(url: str) -> Optional[str]:
    """The artifact id of a claude.ai artifact URL or a bare id; None for anything else."""
    text = (url or "").strip()
    match = _ARTIFACT_URL.match(text)
    if match:
        return match.group(1)
    if _BARE_ID.match(text):
        return text
    return None


def dump_json(obj) -> bytes:
    """The one serializer: UTF-8, ensure_ascii False, indent 2, key order as given, LF, trailing newline."""
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _part_refusal(part: str) -> Optional[str]:
    if part in ("", ".", ".."):
        return "empty or dot part"
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in part):
        return "control character"
    if ":" in part or "\\" in part:
        return "colon or backslash"
    if part.endswith(".") or part.endswith(" "):
        return "trailing dot or space"
    if part.split(".", 1)[0].upper() in _DEVICE_NAMES:
        return "device name"
    return None


def safe_child(base: Path, name: str) -> Path:
    """`base / name` iff `name` is a relative path every OS can hold and the result stays under `base`."""
    base = Path(base)
    if not name:
        raise HarvestError(UNSAFE_PATH, "empty name")
    if name.startswith("/") or Path(name).is_absolute() or re.match(r"^[A-Za-z]:", name):
        raise HarvestError(UNSAFE_PATH, f"absolute name {name!r}")
    for part in name.split("/"):
        why = _part_refusal(part)
        if why:
            raise HarvestError(UNSAFE_PATH, f"{why} in {name!r}")
    child = base / name
    if contained_path(child, [base]) is None:
        raise HarvestError(UNSAFE_PATH, f"{name!r} resolves outside the record directory")
    return child


def thread_ids(threads) -> list:
    """The `id` of every thread, in payload order."""
    return [t["id"] for t in threads]


def utc_string(moment: datetime) -> str:
    """`moment` as `YYYY-MM-DDTHH:MM:SSZ`; a naive datetime is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant(text: str) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def latest_comment_at(threads) -> Optional[str]:
    """The greatest comments[].created_at as an instant, returned as the payload's own string; None with no comment.

    A created_at that is not an ISO instant is ignored.
    """
    best_moment = None
    best_text = None
    for thread in threads:
        for comment in thread.get("comments", ()):
            text = comment.get("created_at")
            moment = _instant(text) if isinstance(text, str) else None
            if moment is not None and (best_moment is None or moment > best_moment):
                best_moment, best_text = moment, text
    return best_text
