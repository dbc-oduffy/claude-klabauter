"""Reap open `tf` bug-backlog rows whose member tests now pass, with no model call.

`reap` runs every open `tf` row's member test files in batched pytest spawns (one per chunk of files,
never one per row), closes each row whose members all passed, and writes a `ReapRecord` last. A row
whose tests could not be run (chunk refused, timeout, collection error, non-pytest member) is never
closed: it is recorded `unrun` with the reason. No git spawn; the record's `head_sha` is read before
the first test spawn, so a peer commit during a long reap cannot relabel the verdicts.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
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
    files: list[str], repo_root: Path, timeout: int, verdicts: dict[str, dict]
) -> None:
    """Run `files` once and fill `verdicts[file]` with `{"failed": [ids], "ran": [ids]}` or `{"unrun": reason}`.

    A refused argv or a timed-out run is halved and retried; a single file that still cannot run is unrun.
    """
    head = targeted_run.head_for(repo_root, files)
    if head is None:
        reason = "no pytest head admitted"
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
        _run_chunk(files[:half], repo_root, timeout, verdicts)
        _run_chunk(files[half:], repo_root, timeout, verdicts)
        return
    verdicts[files[0]] = {"unrun": reason}


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
        reason = f"pytest exit {rc} with no parsed failure"
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
) -> ReapRecord:
    """Run open tf rows' member tests, close all-green rows, write and return the `ReapRecord`."""
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

    all_files = sorted({f for _, members in rows.values() for f, _ in members})
    verdicts: dict[str, dict] = {}
    for chunk in _split_chunks(all_files, max(1, chunk_files)):
        _run_chunk(chunk, root, timeout, verdicts)

    red: list[str] = []
    green: list[str] = []
    for row_id, (row_path, members) in rows.items():
        verdict, reason = _row_verdict(members, verdicts)
        if verdict == "red":
            red.append(row_id)
        elif verdict == "unrun":
            unrun[row_id] = reason
        else:
            failure = _close_row(root, row_path)
            if failure is None:
                green.append(row_id)
            else:
                unrun[row_id] = failure

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
    out.write_text(json.dumps(record.to_json(), indent=2) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    """`python -m coordinator_core.bug_blitz.reap_green <repo>`: reap and print the counts."""
    args = list(sys.argv[1:] if argv is None else argv)
    record = reap(Path(args[0]) if args else Path.cwd())
    print(f"closed {len(record.green)}, red {len(record.red)}, unrun {len(record.unrun or {})}, {reap_record_rel()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
