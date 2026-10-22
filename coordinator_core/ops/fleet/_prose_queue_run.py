"""Run driver for fleet.migrate_prose_queue: skip chain, in-process emission, bucket report.

Reads repo_root/state/<family>.md read-only and writes only through queue_append, whose
paths are confined to repo_root/state/<family>/. The report is returned, never written.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

from coordinator_core.ops.fleet._prose_queue_contract import (
    BUCKET_AMBIGUOUS,
    BUCKET_MIGRATED_INCOMPLETE,
    BUCKET_PARSE_ERROR,
    BUCKET_SKIPPED,
    BUCKET_TO_MIGRATE,
    BUCKET_UNIVERSAL_LEFTOVER,
    EXAMPLE_CAP,
    OUTCOME_FILE_UNPARSEABLE,
    OUTCOME_SOURCE_ABSENT,
    SKIP_REASONS,
    CandidateEntry,
)
from coordinator_core.ops.fleet._prose_queue_fields import FLAG_CLOSED, fill_fields
from coordinator_core.ops.fleet._prose_queue_grammars import parse_file
from coordinator_core.ops.queue_append import prepare_queue_entry, write_prepared_entry

REASON_CLOSED, REASON_ALREADY, REASON_DUPLICATE, REASON_TITLE_MATCH = SKIP_REASONS

_TITLE_LINE_RE = re.compile(r"^title:.*$", re.MULTILINE)


def _norm_title(title: str) -> str:
    return " ".join(title.split()).casefold()


def _existing_state(family_dir: Path) -> "tuple[List[str], Dict[str, str]]":
    """File names under family_dir and {normalised title: file name}; empty when absent."""
    if not family_dir.is_dir():
        return [], {}
    names = sorted(n for n in os.listdir(family_dir) if n.endswith(".yaml"))
    titles: Dict[str, str] = {}
    for name in names:
        try:
            text = (family_dir / name).read_text(encoding="utf-8")
            m = _TITLE_LINE_RE.search(text)
            if not m:
                continue
            loaded = yaml.safe_load(m.group(0))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        title = loaded.get("title") if isinstance(loaded, dict) else None
        if isinstance(title, str):
            titles.setdefault(_norm_title(title), name)
    return names, titles


def _empty_report(outcome: str) -> dict:
    return {
        "outcome": outcome,
        BUCKET_TO_MIGRATE: 0,
        BUCKET_AMBIGUOUS: 0,
        BUCKET_MIGRATED_INCOMPLETE: 0,
        BUCKET_PARSE_ERROR: 0,
        BUCKET_UNIVERSAL_LEFTOVER: 0,
        BUCKET_SKIPPED: {reason: 0 for reason in SKIP_REASONS},
        "examples": {},
    }


def _note(report: dict, bucket: str, cand: CandidateEntry, **extra: object) -> None:
    rows: List[dict] = report["examples"].setdefault(bucket, [])
    if len(rows) < EXAMPLE_CAP:
        row = cand.source_row
        rows.append(
            {
                "line": row.line_start if row else None,
                "title": cand.fields.get("title"),
                "flags": list(cand.flags),
                **extra,
            }
        )


def _skip(report: dict, reason: str, cand: CandidateEntry, **extra: object) -> None:
    report[BUCKET_SKIPPED][reason] += 1
    _note(report, f"{BUCKET_SKIPPED}:{reason}", cand, **extra)


def _kwargs(cand: CandidateEntry, repo_root: Path) -> dict:
    kwargs = dict(cand.fields)
    kwargs["queue_scope"] = "project"
    kwargs["caller_worktree"] = repo_root
    return kwargs


def _migrate_family(
    repo_root: Path, family: str, dry_run: bool
) -> "tuple[dict, Optional[str]]":
    """Return (family report, confinement-refusal message or None)."""
    source = repo_root / "state" / f"{family}.md"
    if not source.is_file():
        return _empty_report(OUTCOME_SOURCE_ABSENT), None

    with open(source, "r", encoding="utf-8", newline=None) as fh:
        text = fh.read()
    parsed = parse_file(text)
    report = _empty_report(parsed.outcome)
    if parsed.outcome == OUTCOME_FILE_UNPARSEABLE:
        return report, None

    family_dir = repo_root / "state" / family
    names, existing_titles = _existing_state(family_dir)
    confined_dir = family_dir.resolve()
    seen: Set[str] = set()

    for row in parsed.rows:
        cand = fill_fields(family, row)
        if cand.bucket_hint == BUCKET_PARSE_ERROR:
            report[BUCKET_PARSE_ERROR] += 1
            _note(report, BUCKET_PARSE_ERROR, cand)
            continue
        if cand.bucket_hint == BUCKET_UNIVERSAL_LEFTOVER:
            report[BUCKET_UNIVERSAL_LEFTOVER] += 1
            _note(report, BUCKET_UNIVERSAL_LEFTOVER, cand)
            continue
        if FLAG_CLOSED in cand.flags:
            _skip(report, REASON_CLOSED, cand)
            continue

        kwargs = _kwargs(cand, repo_root)
        try:
            prepared = prepare_queue_entry(family, **kwargs)
        except ValueError as exc:
            report[BUCKET_PARSE_ERROR] += 1
            _note(report, BUCKET_PARSE_ERROR, cand, error=str(exc))
            continue

        suffix = f"-{prepared.slug}-{prepared.digest12}.yaml"
        if any(n.endswith(suffix) for n in names):
            _skip(report, REASON_ALREADY, cand)
            continue
        if prepared.digest12 in seen:
            _skip(report, REASON_DUPLICATE, cand)
            continue
        seen.add(prepared.digest12)
        match = existing_titles.get(_norm_title(str(cand.fields["title"])))
        if match is not None:
            _skip(report, REASON_TITLE_MATCH, cand, path=f"state/{family}/{match}")
            continue

        if not dry_run:
            out_dir = Path(prepared.out_path).resolve().parent
            if out_dir != confined_dir:
                return report, (
                    f"queue entry path {prepared.out_path!r} is outside {str(confined_dir)!r}; "
                    "refusing to write (QUEUE_APPEND_OUTPUT_ROOT override or meta-repo route)"
                )
            write_prepared_entry(prepared)

        report[BUCKET_TO_MIGRATE] += 1
        if "ambiguous" in cand.flags:
            report[BUCKET_AMBIGUOUS] += 1
        if BUCKET_MIGRATED_INCOMPLETE in cand.flags:
            report[BUCKET_MIGRATED_INCOMPLETE] += 1

    return report, None


def run_migration(repo_root: Path, families: List[str], dry_run: bool) -> dict:
    """Bucket every row of each family's prose file; on act, emit the to-migrate rows.

    A confinement refusal returns {"exit_code": 1, "error": ...} with the report so far; it
    fires before the offending entry is written, and the output root is the same for every entry.
    """
    repo_root = Path(repo_root)
    files: Dict[str, dict] = {}
    for family in families:
        report, refusal = _migrate_family(repo_root, family, dry_run)
        files[family] = report
        if refusal is not None:
            return {"exit_code": 1, "error": refusal, "dry_run": dry_run, "files": files}
    return {"dry_run": dry_run, "files": files}
