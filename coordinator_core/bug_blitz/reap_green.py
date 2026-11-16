"""Reap open `tf` bug-backlog rows whose member tests now pass, with no model call.

`reap` runs every open `tf` row's member test files in batched pytest spawns (one per chunk of files,
never one per row), closes each row whose members all passed, and rewrites the `ReapRecord` after every chunk. A row
whose tests could not be run (chunk refused, timeout, collection error, non-pytest member) is never
closed: it is recorded `unrun` with the reason. No git spawn; the record's `head_sha` is read before
the first test spawn, so a peer commit during a long reap cannot relabel the verdicts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from coordinator_core.bug_blitz import targeted_run
from coordinator_core.bug_blitz.contract import (
    REAP_CLOSED_BY,
    ReapRecord,
    reap_record_rel,
)
from coordinator_core.bug_blitz.mint import SCHEMA, TF_TAG
from coordinator_core.frontmatter import primitives as fm_primitives
from coordinator_core.git import git_state
from coordinator_core.ops.queue_family import load_family_records
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.testing.suite_run_record import strip_ansi

# `targeted_run.admit` sets no file-count ceiling (600 files measured as scoped); 40 files bound one
# chunk's runtime under `targeted_run.DEFAULT_TIMEOUT`, and a refused or timed-out chunk is halved.
CHUNK_FILES = 40
_FIRST_CHUNK_FLOOR_S = 60
# Checked between chunks only: a run overshoots by the chunk in flight, whose timeout-halving retries
# can each take `DEFAULT_TIMEOUT`, so the overshoot is bounded by the chunk's attempts, not by one timeout.
DEFAULT_BUDGET_S = 420.0

_ERROR_RE = re.compile(r"^ERROR (\S+)", re.MULTILINE)
_RAN_RE = re.compile(r"^(?:PASSED|FAILED|XFAIL|XPASS) (\S+)", re.MULTILINE)
_PYTEST_FLAGS = ("-q", "-rA", "-p", "no:cacheprovider", "--continue-on-collection-errors")

_Member = tuple[str, str]  # (file, node id; equals file for a file-only member)


def member_nodeids(body: str) -> list[_Member]:
    """Parse the `- <file>[::<name>][ :: msg]` lines `mint._body` writes into (file, node id) pairs."""
    members: list[_Member] = []
    for raw in str(body or "").splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        head = line[2:].split(" :: ", 1)[0].strip()
        if not head:
            continue
        members.append((head.split("::", 1)[0], head))
    return members


def _matches(failed: str, node: str, file: str) -> bool:
    if node == file:
        return failed == file or failed.startswith(file + "::")
    return failed == node or failed.startswith((node + "::", node + "["))


def _split_chunks(files: list[str], size: int) -> list[list[str]]:
    return [files[i : i + size] for i in range(0, len(files), size)]


def _run_chunk(
    files: list[str], repo_root: Path, timeout: int, verdicts: dict[str, dict], deadline: float | None = None
) -> None:
    """Run `files` once and fill `verdicts[file]` with `{"failed": [ids], "ran": [ids]}` or `{"unrun": reason}`.

    A refused argv or a timed-out run is halved and retried; a single file that still cannot run is unrun.
    """
    # The budget binds inside a chunk too: a chunk timeout and its halving retries are capped at what is left,
    # and a run the deadline cut short is recorded unrun "budget", never halved again.
    if deadline is not None:
        left = int(deadline - time.monotonic())
        if left <= 0:
            for f in files:
                verdicts[f] = {"unrun": "budget"}
            return
        timeout = min(timeout, left)
    head = targeted_run.head_for(repo_root, files)
    if head is None:
        reason = _no_head_reason(repo_root, files)
    else:
        argv = [*head[1], *_PYTEST_FLAGS, *files]
        reason = targeted_run._pytest_spawn_refusal(argv, repo_root)
        if reason is None and not targeted_run.admit(argv, repo_root):
            reason = "chunk refused by the Tier-T classifier"
        if reason is None:
            resolved = [shutil.which(argv[0]) or argv[0], *argv[1:]]
            rc, out = targeted_run._spawn_pytest_capped(resolved, repo_root, timeout)
            if rc == targeted_run._TIMEOUT_EXIT:
                reason = f"pytest timed out after {timeout}s"
            else:
                _record_run(files, rc, out, verdicts)
                return
    if len(files) > 1:
        half = len(files) // 2
        _run_chunk(files[:half], repo_root, timeout, verdicts, deadline)
        _run_chunk(files[half:], repo_root, timeout, verdicts, deadline)
        return
    verdicts[files[0]] = {"unrun": reason}


def _no_head_reason(repo_root: Path, files: list[str]) -> str:
    """Why no pytest head was admitted: the unresolved pin's defect when the repo declares one."""
    from coordinator_core.ops.dispatch_emit.interpreter_pin import resolve_targeted_test_python

    defect = resolve_targeted_test_python(repo_root).defect if all(targeted_run._is_py(f) for f in files) else None
    return f"pinned interpreter unresolved: {defect}" if defect else "no pytest head admitted"


_ERROR_LINE_RE = re.compile(r"^(?:ERROR\b|E\s+\S|\S*(?:Error|Exception)\b|.*No module named)")
_ERROR_LINE_MAX = 200


def _error_line(text: str) -> str:
    """One bounded line of pytest's own error output: the first error-shaped line, else the last non-empty one."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    line = next((ln for ln in lines if _ERROR_LINE_RE.match(ln)), lines[-1] if lines else "")
    return line[:_ERROR_LINE_MAX]


def _record_run(files: list[str], rc: int, out: str, verdicts: dict[str, dict]) -> None:
    text = strip_ansi(out)
    failed = [i.replace("\\", "/") for i in targeted_run._failing_ids(text)]
    errored = [i.replace("\\", "/") for i in _ERROR_RE.findall(text)]
    ids = failed + [i for i in errored if "::" in i]
    collection_errors = {i for i in errored if "::" not in i}
    ran = [i.replace("\\", "/") for i in _RAN_RE.findall(text)] + ids
    if rc == 0:
        reason = None
    elif rc == 5:
        reason = "no tests collected"
    elif rc == 1 and (ids or collection_errors):
        reason = None
    else:
        line = _error_line(text)
        reason = f"pytest exit {rc}: {line}" if line else f"pytest exit {rc} with no parsed failure"
    for file in files:
        if reason is not None:
            verdicts[file] = {"unrun": reason}
        elif file in collection_errors:
            verdicts[file] = {"unrun": "collection error"}
        else:
            verdicts[file] = {"failed": ids, "ran": ran}


def _row_verdict(members: list[_Member], verdicts: dict[str, dict]) -> tuple[str, str]:
    """("red" | "green" | "unrun", reason); red wins over unrun, green needs every member to have run and passed."""
    unrun = ""
    for file, node in members:
        verdict = verdicts.get(file) or {"unrun": "file not run"}
        if "unrun" in verdict:
            unrun = unrun or verdict["unrun"]
        elif any(_matches(f, node, file) for f in verdict["failed"]):
            return "red", ""
        elif not any(_matches(r, node, file) for r in verdict["ran"]):
            unrun = unrun or "member not collected"
    return ("unrun", unrun) if unrun else ("green", "")


def _close_text(text: str, closed_at: str) -> str:
    """The row text with status closed and the three stamp fields; ValueError on a block-scalar field."""
    for key, value in (("status", "closed"), ("closed_by", REAP_CLOSED_BY), ("evidence", reap_record_rel())):
        text = (
            fm_primitives.replace_fm_field(text, key, value)
            if fm_primitives.read_fm_field(text, key) is not None
            else fm_primitives.insert_fm_field(text, key, value)
        )
    if fm_primitives.read_fm_field(text, "closed_at") is not None:
        return fm_primitives.replace_fm_field_raw(text, "closed_at", closed_at)
    return fm_primitives.insert_fm_field_raw(text, "closed_at", closed_at, after_key="status")


def _close_row(repo_root: Path, rel: str) -> str | None:
    """Close one row file in place; None on success, else the reason it stays open."""
    path = repo_root / rel
    try:
        text = path.read_text(encoding="utf-8")
        new = _close_text(text, date.today().isoformat())
        parsed = yaml.safe_load(new)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        return f"close failed: {exc}"
    if not isinstance(parsed, dict) or parsed.get("status") != "closed":
        return "close failed: result did not parse as a closed row"
    declare_write(rel)
    try:
        path.write_text(new, encoding="utf-8")
    except OSError as exc:
        return f"close failed: {exc}"
    return None


def reap(
    repo_root: Path | str,
    *,
    chunk_files: int = CHUNK_FILES,
    timeout: int = targeted_run.DEFAULT_TIMEOUT,
    budget: float | None = DEFAULT_BUDGET_S,
) -> ReapRecord:
    """Run open tf rows' member tests, close all-green rows, rewrite the `ReapRecord` after every chunk, return it.

    A record already at the current head_sha keeps its red/green rows without re-running them. `budget`
    seconds (monotonic) is checked between chunks, never within one, and the first chunk always runs; rows
    left unsettled when it expires are recorded unrun `budget`.
    """
    root = Path(repo_root)
    head = git_state.head_sha(root, fresh=True) or ""

    rows: dict[str, tuple[str, list[_Member]]] = {}
    unrun: dict[str, str] = {}
    for rec in load_family_records(SCHEMA, root, where="status=open"):
        fm = rec.get("frontmatter") or {}
        if TF_TAG not in (fm.get("tags") or []):
            continue
        row_id = Path(rec["path"]).stem
        members = []
        for raw_file, node in member_nodeids(fm.get("body")):
            file = targeted_run._normalize(root, raw_file)
            members.append((file, file + node[len(raw_file) :]))
        if not members:
            unrun[row_id] = "no member tests in body"
        elif not all(f.endswith(".py") and targeted_run.is_test_file(f) for f, _ in members):
            unrun[row_id] = "member is not a pytest file"
        elif not all((root / f).is_file() for f, _ in members):
            unrun[row_id] = "member test file is gone"
        else:
            rows[row_id] = (rec["path"], members)

    red: set[str] = set()
    green: set[str] = set()
    prior = _prior_record(root, head)
    if prior is not None:
        # Open red rows are skipped; closed green rows are no longer in `rows` and carry by id.
        red.update(r for r in prior.red if r in rows)
        green.update(g for g in prior.green if g not in rows)
        for done in red:
            del rows[done]

    pending = dict(rows)
    all_files = sorted({f for _, members in pending.values() for f, _ in members})
    verdicts: dict[str, dict] = {}
    deadline = None if budget is None else time.monotonic() + budget

    def settle() -> None:
        for row_id in [r for r, (_, m) in pending.items() if all(f in verdicts for f, _ in m)]:
            row_path, members = pending.pop(row_id)
            verdict, reason = _row_verdict(members, verdicts)
            if verdict == "red":
                red.add(row_id)
            elif verdict == "unrun":
                unrun[row_id] = reason
            else:
                failure = _close_row(root, row_path)
                if failure is None:
                    green.add(row_id)
                else:
                    unrun[row_id] = failure

    for index, chunk in enumerate(_split_chunks(all_files, max(1, chunk_files))):
        if index and deadline is not None and time.monotonic() >= deadline:
            break
        # The first chunk always runs (a reap makes progress), with at least _FIRST_CHUNK_FLOOR_S of timeout.
        floor = None if deadline is None or index else max(deadline, time.monotonic() + _FIRST_CHUNK_FLOOR_S)
        _run_chunk(chunk, root, timeout, verdicts, floor if floor is not None else deadline)
        settle()
        _persist(root, head, red, green, unrun)

    for row_id in pending:
        unrun[row_id] = "budget"
    return _persist(root, head, red, green, unrun)


def _prior_record(root: Path, head: str) -> ReapRecord | None:
    """The on-disk record when it was written at `head`; None when absent, unreadable or at another head."""
    if not head:
        return None
    try:
        record = ReapRecord.from_json(json.loads((root / reap_record_rel()).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return record if record.head_sha == head else None


def _persist(root: Path, head: str, red: set[str], green: set[str], unrun: dict[str, str]) -> ReapRecord:
    """Atomically rewrite the reap record from the verdicts so far and return it."""
    record = ReapRecord(
        head_sha=head,
        ran_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        red=tuple(sorted(red)),
        green=tuple(sorted(green)),
        unrun=dict(sorted(unrun.items())),
    )
    out = root / reap_record_rel()
    out.parent.mkdir(parents=True, exist_ok=True)
    declare_write(reap_record_rel())
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(record.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return record


def main(argv: list[str] | None = None) -> int:
    """`python -m coordinator_core.bug_blitz.reap_green <repo> [--budget SECONDS]`: reap and print the counts."""
    parser = argparse.ArgumentParser(prog="reap_green")
    parser.add_argument("repo", nargs="?", default=None)
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_S)
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)
    record = reap(Path(ns.repo) if ns.repo else Path.cwd(), budget=ns.budget)
    unrun = record.unrun or {}
    print(f"closed {len(record.green)}, red {len(record.red)}, unrun {len(unrun)}, {reap_record_rel()}")
    if unrun and not record.green and not record.red:
        reason, count = Counter(unrun.values()).most_common(1)[0]
        print(f"nothing ran: all {len(unrun)} rows unrun; dominant reason ({count} rows): {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
