"""
coordinator_core.ops.crossrepo_closure_status — JSON-RPC "crossrepo.closure_status"
operation.

Purpose: join the memo corpus (``cross-repo/inbox/*.md`` + ``cross-repo/archive/*.md``)
against the commitment ledger (``state/cross-repo-commitments/*.yaml``) and report, per
memo, which ledger entries reference it, each entry's raw status, whether that entry
resolves a ``realized_by`` artifact on disk, and an overall open/closed/none closure
verdict for that memo.

Reference-matching semantics are DELIBERATELY the same substring match
``coordinator_core.distill.delete_guard.check_commitment_closure`` already uses (a
ledger entry "references" a memo iff the memo's repo-relative forward-slash path OR its
bare filename appears in the entry's raw YAML text) — this op does not invent a second
matching rule that could silently diverge from the guard's. Likewise the closed/open
verdict per entry reuses that guard's literal vocabulary: ONLY a status of exactly
``"closed"`` counts as closed; every other value (``"open"``, ``"fulfilled"``, an unknown
string, or a missing field) counts as open. This is an intentionally INHERITED quirk, not
a bug this op fixes — the ledger corpus observed on disk today uses ``fulfilled`` for a
satisfied commitment, which this op (matching the guard) reports as still "open"; a
distinct ledger-vocabulary reconciliation is out of this chunk's scope.

Realizing-artifact resolution reuses ``delete_guard.resolve_realized_by``'s shape
dispatch (sentinel / SHA / path) unchanged — see that function's docstring for the
short-SHA-vs-scientific-notation hazard it avoids. An entry's ``realizing`` field is the
``realized_by`` value ONLY when it is present AND resolves on disk; otherwise ``None``.

Join invariant (AC7): the memo corpus is the LEFT table — every memo enumerated from
disk gets exactly one output row, with ``commitments: []`` / ``closure: "none"`` when no
ledger entry references it. A join can silently drop a row only by failing to iterate
one side fully; this op's ``compute_closure_status`` asserts
``len(output) == len(memo records)`` structurally (see test suite), never merely
returning matched pairs.

Self-registration: importing this module calls
register_op("crossrepo.closure_status", _handler) as a side-effect (same pattern as
ops/memo_fate_partition.py). This module IS imported by
coordinator_core/ops/__init__.py's _EAGER_OP_MODULES and crossrepo.closure_status IS
wired into authz/classification.py's OP_CLASSIFICATION and op_scopes.py's
_OP_KEY_SCOPE ("common_dir").

DR-208 five-question affirmation (COMPUTE_ONLY):
  1. Writes, deletes, or reorders any state file, queue, or git object?      No.
     Every filesystem touch is a read (``read_text``, ``iterdir``/``scandir``,
     ``Path.exists``) or a read-only ``git cat-file -e`` subprocess (via the reused
     ``delete_guard.resolve_realized_by``).
  2. Writes into rag's relational store?                                    No.
  3. Opens any file for write (including sentinel creation)?                No.
  4. Mutates shared mutable state outside its own module?                   No.
  5. Persistent state changes observable across process boundaries?         No.
This handler returns a computed JSON payload only; it emits no artifact to disk.

Negative-spec:
  - Does NOT judge whether an open commitment SHOULD be closed, or write/append to the
    ledger — read-only join, full stop.
  - Does NOT invent a second reference-matching rule — reuses
    ``delete_guard.check_commitment_closure``'s exact needle-substring semantics.
  - Does NOT reconcile the "fulfilled" vs "closed" ledger-vocabulary divergence — flagged
    in the module docstring, not silently patched over.
  - Does NOT write any file, shard, or artifact — pure read + compute + return.

Spec backlink: pln-makima-driven-ceremony-redesig-c7fe9a § C15 / AC7
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import yaml

from coordinator_core.distill.delete_guard import resolve_realized_by
from coordinator_core.ipc import register_op

__all__ = [
    "collect_memo_records",
    "collect_commitment_entries",
    "compute_closure_status",
]


def _rel_posix(path: Path, worktree_root: Path) -> str:
    try:
        return path.resolve().relative_to(worktree_root.resolve()).as_posix()
    except ValueError:
        # Not under worktree_root (test isolation, override paths) — fall back to
        # the file's own posix path so callers still get a forward-slash needle
        # (Windows portability — never a backslash path in a JSON payload).
        return path.as_posix()


def collect_memo_records(memo_dirs: Iterable[Path], worktree_root: Path) -> Tuple[List[dict], bool]:
    """Enumerate every ``*.md`` file under each dir in ``memo_dirs`` (typically
    ``cross-repo/inbox`` + ``cross-repo/archive``) as a memo record.

    Each record: {"memo_id", "path" (rel-posix to worktree_root), "filename"}.

    Returns ``(records, degraded)``. ``degraded`` is True iff ANY of the supplied
    dirs exists but could not be listed (``os.scandir`` raising, e.g.
    PermissionError) — uses ``os.scandir()``, not ``Path.glob``, which silently
    swallows that exact error while walking (same rationale as
    ops/memo_fate_partition.py's ``collect_memo_records``). A dir that simply does
    not exist is NOT a degradation (a fresh repo may lack cross-repo/inbox/).
    """
    records: List[dict] = []
    degraded = False

    for memo_dir in memo_dirs:
        if not memo_dir.is_dir():
            continue
        try:
            entries = sorted(os.scandir(memo_dir), key=lambda e: e.name)
        except OSError as exc:
            print(
                f"crossrepo.closure_status: cannot scan {memo_dir} — {exc}; memo "
                "corpus is degraded (NOT the same as \"no memos to join\")",
                file=sys.stderr,
            )
            degraded = True
            continue

        for entry in entries:
            if not entry.name.endswith(".md"):
                continue
            fpath = Path(entry.path)
            records.append(
                {
                    "memo_id": fpath.stem,
                    "path": _rel_posix(fpath, worktree_root),
                    "filename": fpath.name,
                }
            )

    return records, degraded


def collect_commitment_entries(commitments_dir: Path) -> Tuple[List[dict], bool]:
    """Enumerate every ``*.yaml``/``*.yml`` file under ``commitments_dir`` as a
    parsed ledger-entry record.

    Each record: {"entry" (filename), "raw" (full file text), "status" (raw
    ``status:`` value, or None if absent/unparseable), "realized_by" (raw value or
    None)}. An unreadable or unparseable entry is INCLUDED with ``status=None`` and
    a ``parse_error`` detail — never dropped (mirrors ``check_commitment_closure``'s
    fail-closed-not-crash posture, extended here to fail-INCLUDED rather than
    silently absent from the join).

    Returns ``(records, degraded)`` — ``degraded`` is True iff ``commitments_dir``
    exists but could not be listed.
    """
    records: List[dict] = []
    if not commitments_dir.is_dir():
        return records, False

    try:
        entries = sorted(
            (e for e in os.scandir(commitments_dir) if e.name.endswith((".yaml", ".yml"))),
            key=lambda e: e.name,
        )
    except OSError as exc:
        print(
            f"crossrepo.closure_status: cannot scan {commitments_dir} — {exc}; "
            "commitment ledger is degraded (NOT the same as \"no commitments\")",
            file=sys.stderr,
        )
        return records, True

    for entry in entries:
        fpath = Path(entry.path)
        try:
            raw = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            records.append(
                {
                    "entry": fpath.name,
                    "raw": "",
                    "status": None,
                    "realized_by": None,
                    "parse_error": f"unreadable: {exc.__class__.__name__}",
                }
            )
            continue

        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError:
            records.append(
                {
                    "entry": fpath.name,
                    "raw": raw,
                    "status": None,
                    "realized_by": None,
                    "parse_error": "unparseable YAML",
                }
            )
            continue

        if not isinstance(doc, dict):
            records.append(
                {
                    "entry": fpath.name,
                    "raw": raw,
                    "status": None,
                    "realized_by": None,
                    "parse_error": "not a YAML mapping",
                }
            )
            continue

        records.append(
            {
                "entry": fpath.name,
                "raw": raw,
                "status": doc.get("status"),
                "realized_by": doc.get("realized_by"),
                "parse_error": None,
            }
        )

    return records, False


def _entry_closed(status: Optional[str]) -> bool:
    """Same literal vocabulary as delete_guard.check_commitment_closure: ONLY an
    exact "closed" status counts closed; everything else (incl. "fulfilled", an
    unknown string, or None/missing) is treated as open."""
    return isinstance(status, str) and status.strip() == "closed"


def _resolve_realizing(realized_by: Any, repo_root: Path) -> Optional[str]:
    if not isinstance(realized_by, str) or not realized_by.strip():
        return None
    if resolve_realized_by(realized_by, repo_root):
        return realized_by
    return None


def _references(entry: dict, needle: str, filename: str) -> bool:
    raw = entry.get("raw") or ""
    return needle in raw or filename in raw


def compute_closure_status(
    memo_records: Iterable[dict],
    commitment_records: Iterable[dict],
    *,
    repo_root: Path,
    memo_degraded: bool = False,
    commitment_degraded: bool = False,
) -> dict:
    """Pure join core — memo corpus (left table) joined against the commitment
    ledger (right table) by needle-substring reference match.

    Returns:
        {
            "memos": [{"memo_id", "path", "commitments": [{"entry", "status",
                       "realizing"}, ...], "closure": "open"|"closed"|"none"}, ...],
            "counts": {"total_memos": int, "open": int, "closed": int, "none": int},
            "degraded": bool,
        }

    Invariant (AC7): len(result["memos"]) == len(list(memo_records)) ALWAYS — every
    memo enumerated on the left gets exactly one output row, whether or not any
    ledger entry references it.
    """
    commitment_records = list(commitment_records)
    memos_out: List[dict] = []
    counts = {"open": 0, "closed": 0, "none": 0}

    for rec in memo_records:
        needle = rec["path"]
        filename = rec["filename"]
        commitments = []
        for entry in commitment_records:
            if not _references(entry, needle, filename):
                continue
            commitments.append(
                {
                    "entry": entry["entry"],
                    "status": entry["status"],
                    "realizing": _resolve_realizing(entry.get("realized_by"), repo_root),
                }
            )

        if not commitments:
            closure = "none"
        elif all(_entry_closed(c["status"]) for c in commitments):
            closure = "closed"
        else:
            closure = "open"

        counts[closure] += 1
        memos_out.append(
            {
                "memo_id": rec["memo_id"],
                "path": rec["path"],
                "commitments": commitments,
                "closure": closure,
            }
        )

    return {
        "memos": memos_out,
        "counts": {"total_memos": len(memos_out), **counts},
        "degraded": bool(memo_degraded or commitment_degraded),
    }


@register_op("crossrepo.closure_status")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "crossrepo.closure_status" handler.

    Params:
        inbox_dir / archive_dir / commitments_dir (str, optional): overrides for
            cross-repo/inbox, cross-repo/archive, state/cross-repo-commitments —
            test isolation, mirrors memo_fate_partition.py's convention.

    Worktree resolution mirrors memo_fate_partition.py / memo_triage.py:
      - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
      - None → structured error (no silent meta-repo fallback).
    """
    from coordinator_core.ops.fleet._common import main_worktree_root

    if repo_root is None:
        raise ValueError(
            "crossrepo.closure_status requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be "
            "'common_dir'. No silent fallback to meta-repo."
        )

    worktree_root = main_worktree_root(repo_root)

    def _resolve(param_name: str, default_rel: Tuple[str, ...]) -> Path:
        override = params.get(param_name)
        if isinstance(override, str) and override:
            return Path(override)
        return worktree_root.joinpath(*default_rel)

    inbox_dir = _resolve("inbox_dir", ("cross-repo", "inbox"))
    archive_dir = _resolve("archive_dir", ("cross-repo", "archive"))
    commitments_dir = _resolve("commitments_dir", ("state", "cross-repo-commitments"))

    memo_records, memo_degraded = collect_memo_records([inbox_dir, archive_dir], worktree_root)
    commitment_records, commitment_degraded = collect_commitment_entries(commitments_dir)

    return compute_closure_status(
        memo_records,
        commitment_records,
        repo_root=worktree_root,
        memo_degraded=memo_degraded,
        commitment_degraded=commitment_degraded,
    )
