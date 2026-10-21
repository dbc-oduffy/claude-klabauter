"""Per-send memo records: the write-once replacement for the shared sent-ledger.

Each `memo.send` writes one JSON file under
`.coordinator-local/memo-outbox/sent-records/<YYYY-MM>/`; the "ledger view"
readers want is derived on read from the newest record names. Pure stdlib,
zero git spawns, no lock: no file is rewritten after it is written.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Optional

from coordinator_core.session import machinery_paths as _machinery_paths

#: Read bounds, not storage bounds: records are never pruned. 250 rows is
#: ~2.5 days at this fleet's 2026-08 rate (~92 sends/day), sized for the
#: question the view is asked ("did my send land"). The durable record of a
#: send is the receiver's tree, the delivery commit, and this repo's
#: never-evicted `sent/<topic>.md` copy; a reader needing older history reads
#: one of those, never widens these.
VIEW_MAX_ROWS = 250

#: The age half of the bound. Almost every repo sends a handful of memos a
#: month, so the 250-row cap alone would keep stale rows readable for months.
VIEW_MAX_AGE_DAYS = 30

RECORDS_LEAF = "sent-records"
RETIRED_LEAF = "retired-ledgers"
_UNATTRIB = "unattrib"
_UNRESOLVED = "unresolved"
_STAMP_FMT = "%Y%m%dT%H%M%SZ"
_ROW_STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"
_STAMP_LEN = 16
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

_LEGACY_LIVE_NAME = "sent-ledger.jsonl"
_RETIRED_LIVE_NAME = _LEGACY_LIVE_NAME
_RETIRED_STATE_NAME = "state-memo-outbox-sent-ledger.jsonl"


def _part(value: object, fallback: str) -> str:
    text = _UNSAFE.sub("-", str(value or "")).strip("-.")
    return text or fallback


def _row_datetime(row: dict) -> Optional[datetime.datetime]:
    sent_at = row.get("sent_at")
    if not isinstance(sent_at, str):
        return None
    try:
        return datetime.datetime.strptime(sent_at, _ROW_STAMP_FMT).replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None


def record_relpath(row: dict) -> str:
    """Outbox-relative path of the record for `row`:
    `sent-records/<YYYY-MM>/<stamp>-<topic>-<sid8>.json`.

    Raises ValueError when `sent_at` or `topic` is missing or unparseable.
    """
    when = _row_datetime(row)
    topic = _part(row.get("topic"), "")
    if when is None or not topic:
        raise ValueError("row lacks a parseable sent_at or a topic")
    sent_by = row.get("sent_by")
    sid8 = _UNATTRIB
    if isinstance(sent_by, str) and sent_by and sent_by != _UNRESOLVED:
        sid8 = _part(sent_by[:8], _UNATTRIB)
    return (
        f"{RECORDS_LEAF}/{when.strftime('%Y-%m')}/"
        f"{when.strftime(_STAMP_FMT)}-{topic}-{sid8}.json"
    )


def records_dir(worktree: object) -> Path:
    return Path(_machinery_paths.memo_outbox_sent_records_dir(str(worktree)))


def legacy_ledger_paths(worktree: object) -> list[Path]:
    """The live ledger and the retired `state/memo-outbox/` ledger."""
    return [
        Path(_machinery_paths.memo_outbox_sent_ledger_path(str(worktree))),
        Path(worktree) / "state" / "memo-outbox" / _LEGACY_LIVE_NAME,
    ]


def _retired_dir(worktree: object) -> Path:
    return Path(_machinery_paths.memo_outbox_dir(str(worktree))) / RETIRED_LEAF


def write_record(worktree: object, row: dict) -> Path:
    """Write `row` as a new record and return its path; never overwrites.

    A same-name collision takes `-2`, `-3`, ... before the extension.
    """
    rel = record_relpath(row)
    base = Path(_machinery_paths.memo_outbox_dir(str(worktree))) / rel
    base.parent.mkdir(parents=True, exist_ok=True)
    tmp = base.parent / f".{base.stem}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=False))
        target = base
        n = 1
        while True:
            # os.link refuses an existing name atomically, so two concurrent
            # same-second sends cannot overwrite each other the way an
            # exists()-then-os.replace check would let them.
            try:
                os.link(tmp, target)
                break
            except FileExistsError:
                n += 1
                target = base.with_name(f"{base.stem}-{n}{base.suffix}")
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def _iter_jsonl(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _shards(root: Path, now: datetime.datetime) -> list[Path]:
    first = now.replace(day=1)
    prev = (first - datetime.timedelta(days=1)).strftime("%Y-%m")
    return [root / now.strftime("%Y-%m"), root / prev]


def iter_view(
    worktree: object, *, now: Optional[datetime.datetime] = None,
) -> list[dict]:
    """The bounded ledger view, oldest-first: at most VIEW_MAX_ROWS rows sent
    within VIEW_MAX_AGE_DAYS, chosen by record filename before any file opens.

    Also yields rows of any unmigrated legacy ledger, de-duplicated against
    records by `record_relpath`. Malformed files and lines are skipped.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff_stamp = (
        now - datetime.timedelta(days=VIEW_MAX_AGE_DAYS)
    ).strftime(_STAMP_FMT)
    root = records_dir(worktree)

    # (stamp, record Path | None, legacy row | None)
    cands: list[tuple[str, Optional[Path], Optional[dict]]] = []
    names: set[str] = set()
    for shard in _shards(root, now):
        try:
            entries = list(os.scandir(shard))
        except OSError:
            continue
        for ent in entries:
            if not ent.name.endswith(".json") or ent.name.startswith("."):
                continue
            names.add(f"{RECORDS_LEAF}/{shard.name}/{ent.name}")
            cands.append((ent.name[:_STAMP_LEN], Path(ent.path), None))

    for ledger in legacy_ledger_paths(worktree):
        for row in _iter_jsonl(ledger):
            when = _row_datetime(row)
            if when is None:
                continue
            try:
                rel = record_relpath(row)
            except ValueError:
                continue
            if rel in names:
                continue
            names.add(rel)
            cands.append((when.strftime(_STAMP_FMT), None, row))

    cands = [c for c in cands if c[0] >= cutoff_stamp]
    cands.sort(key=lambda c: c[0], reverse=True)
    rows: list[dict] = []
    for _stamp, path, legacy in cands:
        if len(rows) >= VIEW_MAX_ROWS:
            break
        if legacy is not None:
            rows.append(legacy)
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    rows.reverse()
    return rows


def migrate_legacy_ledgers(worktree: object, *, dry_run: bool) -> dict:
    """Lift every legacy-ledger row into a record, then move the ledgers to
    `retired-ledgers/`. Idempotent. Retired ledgers are read too, so a row a
    late merge appended there is lifted. Returns `{written, skipped, moved}`.
    """
    outbox = Path(_machinery_paths.memo_outbox_dir(str(worktree)))
    live, state = legacy_ledger_paths(worktree)
    retired = _retired_dir(worktree)
    moves = [
        (live, retired / _RETIRED_LIVE_NAME),
        (state, retired / _RETIRED_STATE_NAME),
    ]
    sources = [live, state] + [dst for _src, dst in moves]
    written = skipped = moved = 0
    seen: set[str] = set()
    for src in sources:
        for row in _iter_jsonl(src):
            try:
                rel = record_relpath(row)
            except ValueError:
                skipped += 1
                continue
            if rel in seen or (outbox / rel).exists():
                skipped += 1
                continue
            seen.add(rel)
            written += 1
            if not dry_run:
                write_record(worktree, row)
    for src, dst in moves:
        if not src.exists():
            continue
        moved += 1
        if dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            merged = dst.read_bytes()
            if merged and not merged.endswith(b"\n"):
                merged += b"\n"
            tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
            tmp.write_bytes(merged + src.read_bytes())
            os.replace(tmp, dst)
            src.unlink()
        else:
            os.replace(src, dst)
    return {"written": written, "skipped": skipped, "moved": moved}
