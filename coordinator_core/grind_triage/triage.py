"""Deterministic pre-agent triage: drop rows that pass in isolation or no longer reproduce at HEAD.

`triage` returns one `TriageResult` per row, in input order. A test row is rerun by `reap_green`'s
chunked file spawns (one spawn per chunk, never per row); every member passing is a FLAKE. A non-test
row's cited `path[::anchor]` is checked against HEAD through the in-process object reader (no spawn);
a path or anchor absent at HEAD is NOT_REPRODUCED. Anything unrun, red, unreadable or
ambiguous is a SURVIVOR: a verdict is evidence for the refute-close floor, never a replacement for it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Sequence

from coordinator_core.bug_blitz import reap_green, targeted_run
from coordinator_core.grind_triage.contract import TriageResult, TriageRow, Verdict
from coordinator_core.git import git_objects, git_state
from coordinator_core.git.git_dir import resolve_git_common_dir

_ChunkRunner = Callable[[list, Path, int, dict], None]
# A cited surface may carry `:line` or `#anchor`; the file at HEAD is named without it.
_LINE_OR_FRAGMENT = re.compile(r"(?:#.*|(?::\d+)+)$")


def _survivor(row: TriageRow, evidence: str) -> TriageResult:
    return TriageResult(key=row.key, verdict=Verdict.SURVIVOR, evidence=evidence)


def split_target(target: str) -> tuple[str, str]:
    """`path[::anchor]` -> (path, anchor); a test node id splits the same way (file, node tail)."""
    head, _, anchor = str(target or "").partition("::")
    return head.strip().replace("\\", "/"), anchor.strip()


def _triage_tests(
    rows: list[TriageRow],
    root: Path,
    chunk_files: int,
    timeout: int,
    run_chunk: _ChunkRunner,
) -> dict[str, TriageResult]:
    out: dict[str, TriageResult] = {}
    runnable: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        raw_file, anchor = split_target(row.target)
        file = targeted_run._normalize(root, raw_file) if raw_file else ""
        if not file:
            out[row.key] = _survivor(row, "no test file in target")
        elif not (file.endswith(".py") and targeted_run.is_test_file(file)):
            out[row.key] = _survivor(row, "target is not a pytest file")
        elif not (root / file).is_file():
            out[row.key] = _survivor(row, "test file is gone")
        else:
            node = file + ("::" + anchor if anchor else "")
            runnable[row.key] = [(file, node)]

    files = sorted({f for members in runnable.values() for f, _ in members})
    verdicts: dict[str, dict] = {}
    for i in range(0, len(files), max(1, chunk_files)):
        run_chunk(files[i : i + max(1, chunk_files)], root, timeout, verdicts)

    by_key = {r.key: r for r in rows}
    for key, members in runnable.items():
        state, reason = reap_green._row_verdict(members, verdicts)
        if state == "green":
            out[key] = TriageResult(
                key=key, verdict=Verdict.FLAKE, evidence="passed in isolation: " + members[0][1]
            )
        elif state == "red":
            out[key] = _survivor(by_key[key], "red in isolation")
        else:
            out[key] = _survivor(by_key[key], f"unrun: {reason}")
    return out


def _head_blobs(root: Path, paths: Sequence[str]) -> dict[str, str | None] | None:
    """Text of each path at HEAD (None when absent), read in-process from the object store; None when HEAD is unreadable."""
    if not paths:
        return {}
    try:
        if git_state.head_sha(root) is None:
            return None
        common_dir = resolve_git_common_dir(root)
        entries = git_state.head_blobs(root, list(paths))
        result: dict[str, str | None] = {}
        for path in paths:
            entry = entries.get(path)
            if entry is None:
                result[path] = None
                continue
            obj = git_objects.read_object(common_dir, entry[1])
            if obj is None or obj[0] != "blob":
                return None
            result[path] = obj[1].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - an unreadable store makes every cited row a survivor
        return None
    return result


def _triage_cited(rows: list[TriageRow], root: Path) -> dict[str, TriageResult]:
    cited: dict[str, tuple[TriageRow, str, str]] = {}
    out: dict[str, TriageResult] = {}
    for row in rows:
        raw_path, anchor = split_target(row.target)
        raw_path = _LINE_OR_FRAGMENT.sub("", raw_path)
        path = targeted_run._normalize(root, raw_path) if raw_path else ""
        if not path:
            out[row.key] = _survivor(row, "no cited path in target")
        elif path.endswith("/") or "." not in path.rsplit("/", 1)[-1]:
            # The HEAD reader omits directory entries, so a directory would read as absent.
            out[row.key] = _survivor(row, "cited target may be a directory")
        else:
            cited[row.key] = (row, path, anchor)

    blobs = _head_blobs(root, sorted({p for _, p, _ in cited.values()}))
    for key, (row, path, anchor) in cited.items():
        if blobs is None:
            out[key] = _survivor(row, "HEAD unreadable")
            continue
        text = blobs[path]
        if text is None:
            out[key] = TriageResult(key=key, verdict=Verdict.NOT_REPRODUCED, evidence=f"{path} absent at HEAD")
        elif anchor and anchor not in text:
            out[key] = TriageResult(
                key=key, verdict=Verdict.NOT_REPRODUCED, evidence=f"anchor {anchor!r} absent from {path} at HEAD"
            )
        else:
            out[key] = _survivor(row, f"cited {path} still present at HEAD")
    return out


def triage(
    rows: Sequence[TriageRow],
    repo_root: Path | str,
    *,
    chunk_files: int = reap_green.CHUNK_FILES,
    timeout: int = targeted_run.DEFAULT_TIMEOUT,
    run_chunk: _ChunkRunner = reap_green._run_chunk,
) -> list[TriageResult]:
    """One verdict per row in input order; only SURVIVOR results should reach an agent."""
    root = Path(repo_root)
    rows = list(rows)
    by_key = _triage_tests([r for r in rows if r.is_test], root, chunk_files, timeout, run_chunk)
    by_key.update(_triage_cited([r for r in rows if not r.is_test], root))
    return [by_key[r.key] for r in rows]


def survivors(rows: Sequence[TriageRow], results: Sequence[TriageResult]) -> list[TriageRow]:
    """The rows whose result is SURVIVOR, input order preserved."""
    keep = {r.key for r in results if r.verdict is Verdict.SURVIVOR}
    return [r for r in rows if r.key in keep]
