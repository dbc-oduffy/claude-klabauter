"""Deterministic evidence parse for bug-blitz: resolve a `targets` arg and read failing tests from it.

Implements `contract.ParseTargets`. Closed line grammars only: vitest workspace line, vitest
`FAIL` line, pytest `FAILED` short-summary line, bare node id, and a JSON `failing: []` list.
Every non-blank line of a list source lands in `ParseResult.tests` or refuses the whole parse; a log
refuses on target-shaped lines and ignores chatter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from coordinator_core.bug_blitz.contract import FailingTest, ParseResult, TargetSpec
from coordinator_core.session.record_homes import home_dir
from coordinator_core.testing.suite_run_record import parse_failed_ids, strip_ansi

BACKLOG = "backlog"
BACKLOG_DIR = Path(home_dir("", "bug-backlog")).as_posix()
_DIR_LIST = "failing-tests.txt"

# `[FAIL] [<pkg>  ]<file> > <name>`: a package is separated from the file by 2+ spaces.
_VITEST_RE = re.compile(
    r"^\s*(?:FAIL\s+)?(?:(?P<pkg>\S+)\s{2,})?(?P<file>\S+\.(?:test|spec)\.\w+)\s+>\s+(?P<name>.+?)\s*$"
)
# A parametrize id may hold spaces (`test_x[This is BREADTH]`): the bracket runs to the `]` that
# ends the token, so `FAILED <id> - <msg>` and a bare id both keep the whole id.
_NODE_ID_RE = re.compile(
    r"^(?:FAILED )?(?P<file>[^\s:]+\.py)::(?P<name>[^\s\[]+(?:\[.*?\](?=\s|$))?)(?: - (?P<msg>.*))?$"
)
_TARGET_SHAPE_RE = re.compile(r"^\S*(?:::\S*|\.py|\.(?:test|spec)\.\w+)$")
# A lone test-file path in a list source is a file-level target (empty test name): the grind runs the whole file.
_BARE_FILE_RE = re.compile(r"^[^\s:]+(?:\.py|\.(?:test|spec)\.\w+)$")
_PYTEST_MSG_RE = re.compile(r"^FAILED \S+ - (?P<msg>.*)$")


class EvidenceError(ValueError):
    """Base for refusals raised while resolving or parsing a target."""


class TargetRefused(EvidenceError):
    """The target path is outside the repo and is not an existing absolute path."""


class UnparsedTargets(EvidenceError):
    """A target line could not be parsed; it is named in the message, never dropped."""


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
    if path.suffix.lower() == ".json":
        result = _parse_json(text)
    else:
        result = _parse_text(text, noisy=_is_log(path))
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
    m = _NODE_ID_RE.match(stripped)
    if m:
        return FailingTest(file=m["file"], name=m["name"], message=m["msg"] or "")
    ids = parse_failed_ids(stripped)
    if ids:
        file, _, name = ids[0].partition("::")
        msg = _PYTEST_MSG_RE.match(stripped)
        return FailingTest(file=file, name=name, message=msg["msg"] if msg else "")
    return None


def _looks_like_target(line: str) -> bool:
    """A lone token naming a test file or node id: in a log it is a target, never noise."""
    return bool(_TARGET_SHAPE_RE.match(line))


def _assemble(tests: list[FailingTest], unparsed: list[str], *, noisy: bool = False) -> ParseResult:
    """Dedupe `tests`; refuse when any input was not parsed.

    Invariant: an unparsed line is never counted and dropped. A list source (`noisy=False`) refuses
    on every unparsed line. A log (`noisy=True`) carries pytest/vitest chatter, so only target-shaped
    lines refuse. Refusing needs no process: expanding a bare file to node ids would need a pytest
    collection spawn, which the 500ms bar forbids; a bare file is a file-level target instead.
    """
    refused = [u for u in unparsed if not noisy or _looks_like_target(u)]
    if refused:
        listed = "\n".join(f"  {u}" for u in refused)
        raise UnparsedTargets(
            f"{len(refused)} target(s) could not be parsed; nothing was run. "
            f"Rewrite each in the `file::test` form (e.g. `path/test_x.py::test_name`):\n{listed}"
        )
    seen: set[FailingTest] = set()
    unique = []
    for t in tests:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return ParseResult(tests=tuple(unique))


def _is_log(path: Path) -> bool:
    return path.suffix.lower() == ".log" or path.name.endswith("-output.txt")


def _parse_text(text: str, *, noisy: bool) -> ParseResult:
    tests: list[FailingTest] = []
    unparsed: list[str] = []
    for line in strip_ansi(text).splitlines():
        stripped = line.strip()
        if not stripped or (not noisy and stripped.startswith("#")):
            continue
        found = _parse_line(line)
        if not found and not noisy and _BARE_FILE_RE.match(stripped):
            found = FailingTest(file=stripped, name="")
        if found:
            tests.append(found)
        else:
            unparsed.append(line.strip())
    return _assemble(tests, unparsed, noisy=noisy)


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
