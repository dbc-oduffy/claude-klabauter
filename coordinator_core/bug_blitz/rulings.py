"""Per-row rulings for a bug-blitz submission: parse the `rulings` arg and write each matching row's `pm_ruling`.

`pm_ruling` is the field triage and fix already treat as binding. An edited row is recorded in the run's
`minted.jsonl` before it is written (the `mint._record` contract), so `minted_records.settle` lands the
edit in the run's one commit. No git spawn.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from coordinator_core.bug_blitz.mint import SCHEMA, _record, _write_reuse
from coordinator_core.locked_write import contended_lock_wait_secs, held_lock

QUEUE_DIR = f"state/{SCHEMA}"
_JOIN = "\n\n"
_TOP_KEY = re.compile(r"[^\s#]")
_PM_KEY = re.compile(r"pm_ruling\s*:")


@dataclass(frozen=True)
class Ruling:
    """One ruling: `match` is a substring of a row id or title, None for every row of the run."""

    match: Optional[str]
    text: str


def parse_rulings(raw: Any) -> tuple[tuple[Ruling, ...], Optional[str]]:
    """(rulings, refusal) for the `rulings` arg: a string, or a list of `{match, ruling|context}` objects.

    Each text is capped at `requirements_ledger.VERBATIM_BYTE_CAP` bytes. None or an empty list is no rulings."""
    if raw is None:
        return (), None
    from coordinator_core.ops.dispatch_emit.requirements_ledger import VERBATIM_BYTE_CAP

    entries: list[tuple[Optional[str], Any]]
    if isinstance(raw, str):
        entries = [(None, raw)]
    elif isinstance(raw, list):
        entries = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                return (), f"rulings[{i}] must be an object {{match, ruling}}."
            extra = sorted(set(item) - {"match", "ruling", "context"})
            if extra:
                return (), f"rulings[{i}] has unknown keys: {', '.join(extra)}."
            match = item.get("match")
            if not isinstance(match, str) or not match.strip():
                return (), f"rulings[{i}].match must be a non-empty string."
            if "ruling" in item and "context" in item:
                return (), f"rulings[{i}] names both ruling and context; give one."
            entries.append((match.strip(), item.get("ruling", item.get("context"))))
    else:
        return (), "rulings must be a string or a list of {match, ruling} objects."
    out: list[Ruling] = []
    for i, (match, text) in enumerate(entries):
        if not isinstance(text, str) or not text.strip():
            return (), f"rulings[{i}] needs a non-empty ruling text."
        if len(text.encode("utf-8")) > VERBATIM_BYTE_CAP:
            return (), f"rulings[{i}] exceeds {VERBATIM_BYTE_CAP} bytes."
        out.append(Ruling(match, text.strip()))
    return tuple(out), None


def open_row_paths(root: Path, rulings: Iterable[Ruling]) -> list[str]:
    """Repo-relative paths of the open rows in the queue dir that a ruling's `match` could name.

    The profile's own where-filter admits open rows in `QUEUE_DIR` only; a row is parsed only when its
    stem or raw text holds a `match`, so a large backlog is listed, not parsed."""
    needles = [r.match for r in rulings if r.match]
    if not needles:
        return []
    try:
        with os.scandir(root / QUEUE_DIR) as it:
            names = sorted(e.name for e in it if e.name.endswith(".yaml") and e.is_file())
    except OSError:
        return []
    paths: list[str] = []
    for name in names:
        stem = name[: -len(".yaml")]
        if any(n in stem for n in needles):
            paths.append(f"{QUEUE_DIR}/{name}")
            continue
        try:
            text = (root / QUEUE_DIR / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(n in text for n in needles):
            paths.append(f"{QUEUE_DIR}/{name}")
    return paths


def _with_pm_ruling(text: str, value: str) -> str:
    """`text` with a top-level `pm_ruling` set to `value`: replaced in place, else appended."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    new = [f"pm_ruling: {json.dumps(value)}"]
    for start, line in enumerate(lines):
        if _PM_KEY.match(line):
            end = start + 1
            while end < len(lines) and not (lines[end] and _TOP_KEY.match(lines[end])):
                end += 1
            # Keep blank lines that separate this key from the next one out of the replaced span.
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            return nl.join(lines[:start] + new + lines[end:])
    if lines and lines[-1] == "":
        lines[-1:] = new + [""]
    else:
        lines += new
    return nl.join(lines)


def _combined(existing: Any, joined: str) -> str:
    """The row's new `pm_ruling`: an existing PM ruling stays and the new text follows it."""
    current = existing.strip() if isinstance(existing, str) else ""
    if not current or joined in current:
        return current or joined
    return current + _JOIN + joined


def apply_rulings(
    root: Path,
    run_id: str,
    row_paths: Iterable[str],
    rulings: tuple[Ruling, ...],
    run_dir: Path,
) -> tuple[list[str], list[str]]:
    """Write each matching open row's `pm_ruling`; returns (applied row ids, unmatched match labels).

    A ruling matches a row when its `match` is a substring of the row's id (file stem) or title; a string
    ruling (match None) matches every row. Several matches join in arg order, after any ruling the row
    already carries. An unchanged value writes nothing. A path with no readable open row is skipped. Holds
    the queue lock the mint holds, so a concurrent run cannot interleave a tag edit with this write."""
    root = Path(root)
    paths = list(dict.fromkeys(row_paths))
    hit = [False] * len(rulings)
    applied: list[str] = []
    if paths and rulings:
        with held_lock(
            root / "state" / SCHEMA, timeout=contended_lock_wait_secs(), holder_label=f"bug-blitz rulings {run_id}"
        ):
            for rel in paths:
                try:
                    with open(root / rel, encoding="utf-8", newline="") as handle:
                        text = handle.read()
                    row = yaml.safe_load(text)
                except (OSError, UnicodeDecodeError, yaml.YAMLError):
                    continue
                if not isinstance(row, dict) or row.get("status") != "open":
                    continue
                row_id = Path(rel).stem
                title = row.get("title") if isinstance(row.get("title"), str) else ""
                texts: list[str] = []
                for i, ruling in enumerate(rulings):
                    if ruling.match is None or ruling.match in row_id or ruling.match in title:
                        hit[i] = True
                        if ruling.text not in texts:
                            texts.append(ruling.text)
                if not texts:
                    continue
                applied.append(row_id)
                value = _combined(row.get("pm_ruling"), _JOIN.join(texts))
                if value == (row.get("pm_ruling").strip() if isinstance(row.get("pm_ruling"), str) else None):
                    continue
                updated = _with_pm_ruling(text, value)
                if yaml.safe_load(updated).get("pm_ruling") != value:
                    raise ValueError(f"{rel}: pm_ruling could not be written as a clean YAML field")
                _record(run_dir, rel, created=False)
                _write_reuse(root, rel, updated)
    unmatched = [(r.match if r.match is not None else "(every row)") for i, r in enumerate(rulings) if not hit[i]]
    return applied, unmatched
