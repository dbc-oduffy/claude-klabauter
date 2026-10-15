"""Deterministic evidence parse for bug-blitz: resolve a `targets` arg and read failing tests from it.

Implements `contract.ParseTargets`. Closed line grammars only: vitest workspace line, vitest
`FAIL` line, pytest `FAILED` short-summary line, bare node id, and a JSON `failing: []` list.
Every non-blank line lands in `ParseResult.tests` or the unparsed count.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from coordinator_core.bug_blitz.contract import FailingTest, ParseResult, TargetSpec
from coordinator_core.testing.suite_run_record import parse_failed_ids, strip_ansi

BACKLOG = "backlog"
BACKLOG_DIR = "state/bug-backlog"
UNPARSED_SAMPLE_MAX = 5
_SAMPLE_WIDTH = 200
_DIR_LIST = "failing-tests.txt"

# `[FAIL] [<pkg>  ]<file> > <name>`: a package is separated from the file by 2+ spaces.
_VITEST_RE = re.compile(
    r"^\s*(?:FAIL\s+)?(?:(?P<pkg>\S+)\s{2,})?(?P<file>\S+\.(?:test|spec)\.\w+)\s+>\s+(?P<name>.+?)\s*$"
)
_NODE_ID_RE = re.compile(r"^(?P<file>[^\s:]+\.py)::(?P<name>\S+)$")
_PYTEST_MSG_RE = re.compile(r"^FAILED \S+ - (?P<msg>.*)$")


class EvidenceError(ValueError):
    """Base for refusals raised while resolving or parsing a target."""


class TargetRefused(EvidenceError):
    """The target path is outside the repo and is not an existing absolute path."""


class NoFailingTests(EvidenceError):
    """A non-backlog target parsed to zero failing tests."""


def resolve_target(raw: str, repo_root: Path) -> TargetSpec:
    """Resolve a raw `targets` arg: `backlog`, the bug-backlog dir, a red-set directory, or a file.

    The bug-backlog dir (`BACKLOG_DIR` under `repo_root`, in any spelling) is the backlog itself,
    never a red-set dir. A relative path must stay inside `repo_root`; an absolute path is accepted
    when it exists (the red-set may live in a sibling checkout).
    """
    text = (raw or "").strip()
    if not text:
        raise TargetRefused("targets is empty")
    if text == BACKLOG:
        return TargetSpec(kind="backlog")
    root = Path(repo_root).resolve()
    candidate = Path(text)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise TargetRefused(f"targets path escapes the repo: {text}")
    if resolved == root / BACKLOG_DIR:
        return TargetSpec(kind="backlog")
    if resolved.is_dir():
        return TargetSpec(kind="dir", path=str(resolved))
    if resolved.is_file():
        return TargetSpec(kind="file", path=str(resolved))
    raise TargetRefused(f"targets path does not exist: {text}")


def parse_targets(spec: TargetSpec) -> ParseResult:
    """Parse a resolved target; raises `NoFailingTests` when a non-backlog target yields none."""
    if spec.kind == "backlog":
        return ParseResult(tests=())
    path = _source_file(Path(spec.path)) if spec.kind == "dir" else Path(spec.path)
    text = path.read_text(encoding="utf-8", errors="replace")
    result = _parse_json(text) if path.suffix.lower() == ".json" else _parse_text(text)
    if not result.tests:
        raise NoFailingTests(f"no failing tests parsed from {path.name}")
    return result


def group_by_file(tests: tuple[FailingTest, ...]) -> dict[tuple[str, str], list[FailingTest]]:
    """Failing tests keyed by `(package, file)`, in first-seen order."""
    grouped: dict[tuple[str, str], list[FailingTest]] = {}
    for t in tests:
        grouped.setdefault((t.package, t.file), []).append(t)
    return grouped


def _source_file(directory: Path) -> Path:
    listed = directory / _DIR_LIST
    if listed.is_file():
        return listed
    for pattern in ("*-output.txt", "*.log"):
        for found in sorted(directory.glob(pattern)):
            if found.is_file():
                return found
    raise NoFailingTests(f"no {_DIR_LIST}, *-output.txt or *.log in {directory.name}")


def _parse_line(line: str) -> FailingTest | None:
    m = _VITEST_RE.match(line)
    if m:
        return FailingTest(file=m["file"], name=m["name"], package=m["pkg"] or "")
    stripped = line.strip()
    ids = parse_failed_ids(stripped)
    if ids:
        file, _, name = ids[0].partition("::")
        msg = _PYTEST_MSG_RE.match(stripped)
        return FailingTest(file=file, name=name, message=msg["msg"] if msg else "")
    m = _NODE_ID_RE.match(stripped)
    if m:
        return FailingTest(file=m["file"], name=m["name"])
    return None


def _assemble(tests: list[FailingTest], unparsed: list[str]) -> ParseResult:
    seen: set[FailingTest] = set()
    unique = []
    for t in tests:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    sample = tuple(u[:_SAMPLE_WIDTH] for u in unparsed[:UNPARSED_SAMPLE_MAX])
    return ParseResult(tests=tuple(unique), unparsed_count=len(unparsed), unparsed_sample=sample)


def _parse_text(text: str) -> ParseResult:
    tests: list[FailingTest] = []
    unparsed: list[str] = []
    for line in strip_ansi(text).splitlines():
        if not line.strip():
            continue
        found = _parse_line(line)
        if found:
            tests.append(found)
        else:
            unparsed.append(line.strip())
    return _assemble(tests, unparsed)


def _parse_json(text: str) -> ParseResult:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NoFailingTests(f"invalid JSON: {exc.msg}") from exc
    items = data.get("failing") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise NoFailingTests("JSON has no `failing` list")
    tests: list[FailingTest] = []
    unparsed: list[str] = []
    for item in items:
        found = None
        if isinstance(item, str):
            found = _parse_line(strip_ansi(item))
        elif isinstance(item, dict) and item.get("file") and item.get("name"):
            found = FailingTest(
                file=str(item["file"]),
                name=str(item["name"]),
                package=str(item.get("package") or ""),
                message=str(item.get("message") or ""),
            )
        if found:
            tests.append(found)
        else:
            unparsed.append(item if isinstance(item, str) else json.dumps(item, sort_keys=True))
    return _assemble(tests, unparsed)
