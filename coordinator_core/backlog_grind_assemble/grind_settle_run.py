"""`backlog-grind-assemble grind-row settle-run`: finish an already-ended grind run.

Re-runs only the commit-and-settle stage of a run whose process died before its terminal commit
fired: no fix, verify or close is repeated. Rows come from the first source that exists:
`--rows-file`, the run's persisted terminal-commit params file, then reconstruction from evidence.

Reconstruction trusts a row only when its `<id>.done.md` evidence exists and its archive
destination is on disk; its paths are its triage `declared_files` plus that destination, and its
deletion is the source row under the named queue. A fixer's undeclared file is left out;
`--dry-run` prints it under `unclaimed_dirty` so an operator can supply the corrected set through
`--rows-file`. Unclaimed dirt is never staged.

Exit codes: 0 ok, 1 refusal (profile, commit or malformed rows), 2 usage.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.ops.dispatch_emit import grind_profile

SOURCE_ROWS_FILE = "rows-file"
SOURCE_PARAMS_FILE = "params-file"
SOURCE_EVIDENCE = "evidence"
RECONSTRUCTED_OUTCOME = "settled"

_USAGE = (
    "usage: grind-row settle-run --profile-dir D --profile P --run-id T --queue Q [--queue Q ...] "
    "--repo-root R [--run-dir RD] [--rows-file F] [--dry-run]"
)


def _split_argv(rest: list[str]) -> Optional[tuple[dict[str, str], list[str], bool]]:
    queues: list[str] = []
    others: list[str] = []
    dry_run = False
    i = 0
    while i < len(rest):
        if rest[i] == "--dry-run":
            dry_run = True
            i += 1
        elif rest[i] == "--queue" and i + 1 < len(rest):
            queues.append(rest[i + 1])
            i += 2
        else:
            others.append(rest[i])
            i += 1
    flags = grind_rows._parse_flags(
        others,
        required=("profile-dir", "profile", "run-id", "repo-root"),
        optional=("run-dir", "rows-file"),
    )
    if flags is None or not queues:
        return None
    return flags, queues, dry_run


def _rows_from_file(path: Path) -> list:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(doc, dict):
        grind = doc.get("grind")
        doc = grind.get("rows") if isinstance(grind, dict) else doc.get("rows")
    if not isinstance(doc, list):
        raise ValueError(f"{path}: no grind rows list")
    return [
        {"paths": [], "deleted_paths": [], **row} if isinstance(row, dict) else row for row in doc
    ]


def _triage_declared_files(run_dir: Path) -> dict[str, list[str]]:
    """`row_id -> declared_files` over every `<run_dir>/records/*.json` triage record."""
    declared: dict[str, list[str]] = {}
    for record_file in sorted((run_dir / "records").glob("*.json")):
        try:
            doc = json.loads(record_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rec in (doc.get("rows") if isinstance(doc, dict) else None) or []:
            if not isinstance(rec, dict) or not isinstance(rec.get("row"), str):
                continue
            files = [f for f in rec.get("declared_files") or [] if isinstance(f, str) and f]
            declared.setdefault(rec["row"], [])
            declared[rec["row"]] += [f for f in files if f not in declared[rec["row"]]]
    return declared


def _queue_rel(queue: str, repo_root: Path) -> Optional[str]:
    rel = os.path.relpath(os.path.join(repo_root, queue), repo_root).replace("\\", "/")
    return None if rel == ".." or rel.startswith("../") else rel.rstrip("/")


def reconstruct_rows(
    repo_root: Path, profile: Any, run_id: str, queues: list[str], run_dir: Path
) -> list[dict]:
    """Rows of `run_id` that reached their done evidence and whose archive destination is on disk."""
    evidence_dir = repo_root / "state" / "queue-grind" / profile.name / "evidence" / run_id
    declared = _triage_declared_files(run_dir)
    queue_rels = [q for q in (_queue_rel(q, repo_root) for q in queues) if q is not None]
    rows: list[dict] = []
    for done in sorted(evidence_dir.glob("*.done.md")):
        row_id = done.name[: -len(".done.md")]
        archived = grind_rows.archive_destination(profile.archive_path, run_id, f"{row_id}.yaml")
        if not (repo_root / archived).is_file():
            continue
        rows.append(
            {
                "row_id": row_id,
                "outcome": RECONSTRUCTED_OUTCOME,
                "paths": list(dict.fromkeys([*declared.get(row_id, []), archived])),
                "deleted_paths": [f"{q}/{row_id}.yaml" for q in queue_rels],
            }
        )
    return rows


def _load_rows(
    flags: dict[str, str], queues: list[str], repo_root: Path, profile: Any, run_dir: Path
) -> tuple[str, list]:
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path

    run_id = flags["run-id"]
    if "rows-file" in flags:
        return SOURCE_ROWS_FILE, _rows_from_file(Path(flags["rows-file"]))
    persisted = params_path(repo_root, run_id)
    if persisted is not None and persisted.is_file():
        return SOURCE_PARAMS_FILE, _rows_from_file(persisted)
    return SOURCE_EVIDENCE, reconstruct_rows(repo_root, profile, run_id, queues, run_dir)


def _unclaimed_dirty(main_root: Path, rows: list) -> list[str]:
    """Dirty paths no row names, from the one status read; ledger-root dirt is not reported."""
    from coordinator_core.ops.dispatch_emit.grind_commit_contract import LEDGER_ROOT
    from coordinator_core.ops.dispatch_emit.terminal_commit import _worktree_snapshot

    snapshot = _worktree_snapshot(main_root, [], [], [":/"])
    if snapshot is None:
        return []
    named = {p for row in rows if isinstance(row, dict) for p in (row.get("paths") or []) + (row.get("deleted_paths") or [])}
    return sorted(p for p in snapshot.changed if p not in named and not p.startswith(LEDGER_ROOT))


def cmd_settle_run(rest: list[str]) -> int:
    parsed = _split_argv(rest)
    if parsed is None:
        return grind_rows._usage(_USAGE)
    flags, queues, dry_run = parsed
    repo_root = Path(flags["repo-root"])
    run_id = flags["run-id"]
    try:
        profile = grind_profile.load_profile(flags["profile"], Path(flags["profile-dir"]))
    except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal, never swallowed
        print(f"grind-row settle-run: profile refused: {exc}", file=sys.stderr)
        return grind_rows.EXIT_REFUSAL
    from coordinator_core import lifecycle
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    run_dir = Path(flags["run-dir"]) if "run-dir" in flags else repo_root / RUN_DIR_ROOT / run_id
    try:
        source, rows = _load_rows(flags, queues, repo_root, profile, run_dir)
    except (OSError, ValueError) as exc:
        print(f"grind-row settle-run: rows refused: {exc}", file=sys.stderr)
        return grind_rows.EXIT_REFUSAL
    main_root = lifecycle.main_worktree_root(lifecycle.git_common_dir(repo_root))

    if dry_run:
        print(
            json.dumps(
                {"rows": rows, "unclaimed_dirty": _unclaimed_dirty(main_root, rows), "source": source},
                sort_keys=True,
            )
        )
        return grind_rows.EXIT_OK

    from coordinator_core.ops.dispatch_emit.grind_terminal_commit import commit_grind_rows

    params = {
        "run_id": run_id,
        "grind": {"profile": profile.name, "rows": rows, "halted": f"settled-after-run {run_id}"},
    }
    reply = commit_grind_rows(main_root, repo_root, params)
    print(json.dumps({**reply, "source": source}, sort_keys=True, default=str))
    return grind_rows.EXIT_REFUSAL if reply.get("refused") or reply.get("error") else grind_rows.EXIT_OK
