"""_research_layout -- mirror of the DoE docs/research layout contract: docs/research/<topic>/<date>-<run-slug>/.

Pinned to coordinator-content-repo coordinator/lib/research_layout.py by test_research_layout.py's parity test;
a change to a constant there must land here too. Stdlib only, path text only (no I/O).
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

RESERVED_TOPICS = ("trail", "spike-verdicts", "archive", "evidence")
TOPIC_RE = re.compile(
    r"^(?!\d{4}-\d{2}-\d{2}-)(?!(?:" + "|".join(RESERVED_TOPICS) + r")$)[a-z0-9][a-z0-9-]*$"
)
RUN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*$")

FIXED_CHILDREN = ("synthesis.md", "trail.yaml", "verdict.md", "outputs/", "workdir/")
INDEX = "README.md"
DEFAULT_TOPIC = "misc"

KNOWN_SUFFIXES = ("advisory", "survey", "audit", "review", "report", "notes", "findings", "synthesis")

INDEX_KIND = "index"
MARKER = "marker"
RUN_CHILD = "run-child"
TOPIC_DIR = "topic-dir"
RUN_DIR = "run-dir"
NONCONFORMING = "nonconforming"

_DIR_CHILDREN = frozenset(c.rstrip("/") for c in FIXED_CHILDREN if c.endswith("/"))
_FILE_CHILDREN = frozenset(c for c in FIXED_CHILDREN if not c.endswith("/"))
_SYNTHESIS_NAMES = ("synthesis.md", "digest.md")


def run_dir(topic: str, date: str, run_slug: str) -> PurePosixPath:
    """Path of one run relative to docs/research: <topic>/<date>-<run-slug>.

    Raises ValueError when topic or the joined run name breaks TOPIC_RE / RUN_RE.
    """
    if not TOPIC_RE.match(topic):
        raise ValueError(f"not a valid research topic: {topic!r}")
    name = f"{date}-{run_slug}"
    if not RUN_RE.match(name):
        raise ValueError(f"not a valid research run name: {name!r}")
    return PurePosixPath(topic) / name


def fold_topic(slug: str) -> tuple[str, str]:
    """Fold one trailing `-<suffix>` (KNOWN_SUFFIXES) into its base topic.

    Returns (topic, suffix) when folded, else (slug, ""). Folds only when the base is a valid topic.
    """
    for suffix in KNOWN_SUFFIXES:
        tail = "-" + suffix
        if slug.endswith(tail):
            base = slug[: -len(tail)]
            if base and TOPIC_RE.match(base):
                return base, suffix
    return slug, ""


def topic_for(topic_slug: str, topic: str | None = None) -> str:
    """The topic a close files under: the caller's `topic` when given, else the folded slug, else DEFAULT_TOPIC."""
    if topic is not None:
        return topic
    folded = fold_topic(topic_slug)[0]
    return folded if TOPIC_RE.match(folded) else DEFAULT_TOPIC


def child_for(scratch_rel: str) -> str:
    """Run-dir-relative POSIX path a scratch-relative output lands at.

    synthesis.md and digest.md land at synthesis.md, verdict.md at verdict.md, everything else
    (segment dirs included) under outputs/.
    """
    rel = _parts(scratch_rel)
    joined = "/".join(rel)
    if joined in _SYNTHESIS_NAMES:
        return "synthesis.md"
    if joined == "verdict.md":
        return "verdict.md"
    return "outputs/" + joined


def classify(relpath: str) -> str:
    """Classify a path relative to docs/research/ as index, marker, run-child, topic-dir, run-dir or nonconforming."""
    parts = _parts(relpath)
    if not parts:
        return NONCONFORMING
    if parts == [INDEX]:
        return INDEX_KIND
    if len(parts) == 1 and _is_marker(parts[0]):
        return MARKER
    if not TOPIC_RE.match(parts[0]):
        return NONCONFORMING
    if len(parts) == 1:
        return TOPIC_DIR
    if not RUN_RE.match(parts[1]):
        return NONCONFORMING
    if len(parts) == 2:
        return RUN_DIR
    child = parts[2]
    if len(parts) == 3 and child in _FILE_CHILDREN:
        return RUN_CHILD
    if child in _DIR_CHILDREN:
        return RUN_CHILD
    return NONCONFORMING


def _is_marker(name: str) -> bool:
    return len(name) > 1 and name.startswith(".") and name != ".." and "/" not in name and "\\" not in name


def _parts(relpath: str) -> list[str]:
    parts = [p for p in relpath.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return []
    return parts
