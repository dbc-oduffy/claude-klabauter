"""Contract and reader for a family stage's vendor-CLI invocation record.

The engine invoker (`INVOKE_MODULE`) spawns the vendor CLI and writes, under
`stage_family.family_scratch_dir(row, run)`: `<stage>.invocation.json`, `<stage>.raw.stdout`,
`<stage>.raw.stderr`. A path is family-authored only when the record's own pre/post fingerprints
differ and the CLI exited 0. Stdlib only; spawns nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.dispatch_emit.write_baseline import fingerprint

INVOKE_MODULE = "coordinator_core.ops.dispatch_emit.family_invoke"
RECORD_SUFFIX = ".invocation.json"
RAW_STDOUT_SUFFIX = ".raw.stdout"
RAW_STDERR_SUFFIX = ".raw.stderr"
UNCHANGED_CAUSE = "unchanged across the CLI process"
RECORD_KEYS = (
    "family", "stage", "argv", "exit_code", "started_at", "ended_at",
    stage_family.VENDOR_MODEL_KEY, "pre", "post", "error",
)

__all__ = (
    "INVOKE_MODULE", "RECORD_SUFFIX", "RAW_STDOUT_SUFFIX", "RAW_STDERR_SUFFIX", "UNCHANGED_CAUSE", "RECORD_KEYS",
    "record_path", "raw_stdout_path", "raw_stderr_path", "read", "authored_paths", "fingerprint",
    "authoring_family",
)


def _dir(root: Path, run_id: str, row: str) -> Path:
    return Path(root) / stage_family.family_scratch_dir(row, run_id)


def record_path(root: Path, run_id: str, row: str, stage: str) -> Path:
    """Absolute path of `<stage>.invocation.json` for the row."""
    return _dir(root, run_id, row) / f"{stage}{RECORD_SUFFIX}"


def raw_stdout_path(root: Path, run_id: str, row: str, stage: str) -> Path:
    """Absolute path of the CLI's raw stdout capture for the stage."""
    return _dir(root, run_id, row) / f"{stage}{RAW_STDOUT_SUFFIX}"


def raw_stderr_path(root: Path, run_id: str, row: str, stage: str) -> Path:
    """Absolute path of the CLI's raw stderr capture for the stage."""
    return _dir(root, run_id, row) / f"{stage}{RAW_STDERR_SUFFIX}"


def _ended(rec: dict) -> str:
    v = rec.get("ended_at")
    return v if isinstance(v, str) else ""


def read(root: Path, run_id: str, row: str) -> list[dict]:
    """Every parseable `<stage>.invocation.json` in the row's dir, ordered by `ended_at`.

    A row may run more than one stage, so all records return. Unreadable or non-object files
    are skipped; an absent dir yields [].
    """
    d = _dir(root, run_id, row)
    try:
        files = sorted(d.glob(f"*{RECORD_SUFFIX}"))
    except OSError:
        return []
    out: list[dict] = []
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            out.append(doc)
    out.sort(key=_ended)
    return out


def authored_paths(record: Optional[dict]) -> frozenset:
    """Paths whose post fingerprint differs from pre, when the record's exit_code is 0.

    A non-zero or missing exit_code, or a malformed pre/post, authors nothing.
    """
    if not isinstance(record, dict) or record.get("exit_code") != 0:
        return frozenset()
    pre, post = record.get("pre"), record.get("post")
    if not isinstance(pre, dict) or not isinstance(post, dict):
        return frozenset()
    return frozenset(p for p in post if post.get(p) != pre.get(p))


def authoring_family(root: Path, run_id: str, rows) -> str:
    """Family of the latest record, across `rows`, that authored at least one path; "" when none did.

    A record with no authored path (read-only stage, failed CLI) never names an author.
    """
    latest: Optional[dict] = None
    for row in rows:
        for rec in read(root, run_id, row):
            if authored_paths(rec) and (latest is None or _ended(rec) >= _ended(latest)):
                latest = rec
    fam = latest.get("family") if latest else None
    return fam.strip().lower() if isinstance(fam, str) else ""
