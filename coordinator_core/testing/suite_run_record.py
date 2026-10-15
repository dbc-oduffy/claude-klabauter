"""Two-slot (current, previous) suite-run record per repo and named collection.

Pure stdlib plus one lazy engine import (``session.core.pid_alive``), no git spawn.
Layout: ``<repo>/.coordinator-local/suite-runs/<collection>/{current,previous}.{json,log}``. Writers are serialized by the suite mutex; every write is
temp + ``os.replace`` and ``current.*`` is never absent once written. Each write also overwrites
``<repo>/state/test-runs/<collection>-last.{txt,json}``: the ANSI-stripped log and a summary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

SCHEMA = "suite-run-record/1"
SLOTS = ("current", "previous")
COLLECTION_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
NAMED_FIELDS = (
    "schema", "repo_root", "collection", "command", "head_sha", "started_at", "ended_at",
    "dirty_paths", "duration_s", "exit_code", "outcome", "serialized", "log",
    "log_truncated", "failed_ids",
)
OUTCOMES = ("pass", "fail", "timeout", "interrupted")
_FAILED_RE = re.compile(r"^FAILED (\S+)", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_SUMMARY_TAIL_RE = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)")
# One definition of the test-irrelevant set: classify_changed and the git status excludes
# are both built from these.
_IRRELEVANT_DIRS = ("docs", "state", "archive")
_IRRELEVANT_SUFFIX = ".md"
DOCS_EXCLUDE_PATHSPECS = (
    *(f":(exclude){d}" for d in _IRRELEVANT_DIRS),
    f":(exclude,glob)**/*{_IRRELEVANT_SUFFIX}",
)


def repo_root_for(cwd: str | os.PathLike) -> Path | None:
    """Nearest ancestor of ``cwd`` holding a ``.git`` entry (dir or file), else None."""
    start = Path(cwd).resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def read_head_sha(repo_root: str | os.PathLike) -> str | None:
    """HEAD commit sha read from ``.git`` without a spawn; None when it cannot be resolved."""
    try:
        git = Path(repo_root) / ".git"
        if git.is_file():
            git = (Path(repo_root) / git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:]
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (git, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip() or None
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except (OSError, IndexError):
        pass
    return None


def record_dir(repo_root: str | os.PathLike, collection: str) -> Path:
    """Record directory for ``collection``; raises ValueError on a non-slug name."""
    if not COLLECTION_RE.match(collection):
        raise ValueError(f"collection {collection!r} must match [a-z0-9][a-z0-9._-]*")
    return Path(repo_root) / ".coordinator-local" / "suite-runs" / collection


def parse_failed_ids(log_text: str) -> list[str] | None:
    """pytest short-summary ``FAILED <nodeid>`` ids in order, None when none parse."""
    ids = _FAILED_RE.findall(log_text)
    return ids or None


def classify_changed(paths) -> tuple[bool, list[str]]:
    """(docs_only, source_paths). A path is test-irrelevant when it is under ``docs/``,
    ``state/`` or ``archive/``, or is a ``.md`` file anywhere; an empty set is docs-only."""
    source = []
    for raw in paths:
        parts = PurePosixPath(str(raw).replace("\\", "/")).parts
        irrelevant = bool(parts) and (
            (len(parts) > 1 and parts[0] in _IRRELEVANT_DIRS)
            or parts[-1].endswith(_IRRELEVANT_SUFFIX)
        )
        if not irrelevant:
            source.append(str(raw))
    return (not source, source)


def write_run(rec_dir: str | os.PathLike, fields: dict, log_path: str | os.PathLike | None) -> Path:
    """Rotate current -> previous, then write the new current; returns current.json.

    ``log_path`` (the finished tee log, or None) is moved into ``current.log``. ``schema``,
    ``log`` and ``failed_ids`` are filled here; any other named field missing raises KeyError
    before anything is rotated.
    """
    rec = Path(rec_dir)
    log_text = ""
    if log_path is not None and Path(log_path).exists():
        log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    body = dict(fields)
    body["schema"] = SCHEMA
    body["log"] = "current.log"
    body.setdefault("failed_ids", parse_failed_ids(log_text))
    missing = [k for k in NAMED_FIELDS if k not in body]
    if missing:
        raise KeyError(f"missing record fields: {missing}")
    if body["outcome"] not in OUTCOMES:
        raise ValueError(f"outcome {body['outcome']!r} not in {OUTCOMES}")
    ordered = {k: body[k] for k in NAMED_FIELDS}

    rec.mkdir(parents=True, exist_ok=True)
    tmp_json = rec / "current.json.tmp"
    tmp_json.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    tmp_log = rec / "current.log.tmp"
    if log_path is not None and Path(log_path).exists():
        os.replace(log_path, tmp_log)
    else:
        tmp_log.write_text("", encoding="utf-8")
    for ext, staged in (("log", tmp_log), ("json", tmp_json)):
        _demote_current(rec, ext)
        os.replace(staged, rec / f"current.{ext}")
    _sweep_orphan_tmp_logs(rec)
    try:
        _write_last_pair(rec, ordered, log_text)
    except OSError as exc:
        print(f"suite_run_record: last-run pair not written: {exc}", file=sys.stderr)
    return rec / "current.json"


def read_branch(repo_root: str | os.PathLike) -> str | None:
    """Checked-out branch name read from ``.git/HEAD`` without a spawn; None on detached HEAD."""
    try:
        git = Path(repo_root) / ".git"
        if git.is_file():
            git = Path(repo_root) / git.read_text(encoding="utf-8").split(":", 1)[1].strip()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, IndexError):
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] or None if head.startswith(prefix) else None


def strip_ansi(text: str) -> str:
    """``text`` without ANSI CSI/OSC escape sequences and bare carriage returns."""
    return _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def parse_counts(log_text: str) -> dict | None:
    """Outcome counts from the last pytest summary line (``... in 1.2s``), None when absent."""
    for line in reversed(log_text.splitlines()):
        if _SUMMARY_TAIL_RE.search(line):
            found = _COUNT_RE.findall(line)
            if found:
                counts: dict = {}
                for n, word in found:
                    key = word + "s" if word in ("error", "warning") else word
                    counts[key] = counts.get(key, 0) + int(n)
                return counts
    return None


def last_run_dir(rec: Path) -> Path | None:
    """``<repo>/state/test-runs`` for a record dir laid out as
    ``<repo>/.coordinator-local/suite-runs/<collection>``; None for any other layout."""
    if rec.parent.name != "suite-runs" or rec.parents[1].name != ".coordinator-local":
        return None
    return rec.parents[2] / "state" / "test-runs"


def _write_last_pair(rec: Path, record: dict, log_text: str) -> None:
    """Overwrite ``<collection>-last.txt`` (ANSI-stripped log) and ``-last.json`` atomically."""
    out = last_run_dir(rec)
    if out is None:
        return
    out.mkdir(parents=True, exist_ok=True)
    clean = strip_ansi(log_text)
    summary = {
        "sha": record["head_sha"],
        "branch": read_branch(rec.parents[2]),
        "started_at": record["started_at"],
        "duration_s": record["duration_s"],
        "exit": record["exit_code"],
        "counts": parse_counts(clean),
        "failing": record["failed_ids"] or [],
    }
    name = record["collection"]
    for ext, text in (
        ("txt", clean),
        ("json", json.dumps(summary, indent=2) + "\n"),
    ):
        tmp = out / f"{name}-last.{ext}.tmp"
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, out / f"{name}-last.{ext}")


def _sweep_orphan_tmp_logs(rec: Path) -> None:
    """Unlink ``run-<pid>.log.tmp`` files whose writer pid is dead; skip unparseable names."""
    from coordinator_core.session.core import pid_alive

    for tmp in rec.glob("run-*.log.tmp"):
        pid = tmp.name[len("run-"):-len(".log.tmp")]
        if not pid.isdigit():
            continue
        try:
            if not pid_alive(int(pid)):
                tmp.unlink(missing_ok=True)
        except OSError:
            continue


def _demote_current(rec: Path, ext: str) -> None:
    """Make ``previous.<ext>`` a copy of ``current.<ext>`` while leaving current in place, so a
    crash never leaves the slot empty. The json copy names ``previous.log`` in its ``log``
    field; it is never a hard link, which would share current's field."""
    cur, prev = rec / f"current.{ext}", rec / f"previous.{ext}"
    if not cur.exists():
        prev.unlink(missing_ok=True)
        return
    staged = rec / f"previous.{ext}.tmp"
    staged.unlink(missing_ok=True)
    if ext == "json":
        try:
            body = json.loads(cur.read_text(encoding="utf-8"))
            body["log"] = "previous.log"
            staged.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError):
            shutil.copy2(cur, staged)
        os.replace(staged, prev)
        return
    try:
        os.link(cur, staged)
    except OSError:
        shutil.copy2(cur, staged)
    os.replace(staged, prev)


def read_runs(rec_dir: str | os.PathLike) -> dict:
    """``{"current": dict|None, "previous": dict|None}``; unreadable slots read as None."""
    out: dict = {}
    for slot in SLOTS:
        try:
            out[slot] = json.loads((Path(rec_dir) / f"{slot}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[slot] = None
    return out
