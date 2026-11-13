"""
coordinator_core.updatedocs.retired_terms -- retired-term manifest reader and doc scan.

Purpose: each repo declares the terms it has retired (a removed dependency, a renamed
subsystem) in a manifest it owns at `docs/retired-terms.yaml`; `scan_retired_terms`
returns every line of the repo's own markdown docs that still names one, as
file:line plus the retirement record. Catches stale prose that carries no
decides/implements anchor. No term is a literal here: the manifest is the only source.

Shape and convention: docs/reference/retired-terms-manifest.md.
Pure pathlib/os, no git, no shell-out, no writes.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coordinator_core.frontmatter.yaml_load import safe_load

MANIFEST_RELPATH = "docs/retired-terms.yaml"

_SCAN_ROOT = "docs"
_SKIP_DIRS = frozenset({"archive", "node_modules"})
_MAX_FILE_BYTES = 1_048_576
_MAX_FILES = 5000
_MAX_LINE_CHARS = 200
_SCAN_BUDGET_SECS = 0.2
_clock = time.process_time
"""Process-time cap on one scan (DR-344 bar); files past it are left unscanned."""


class RetiredTermsManifestError(ValueError):
    """Raised when the manifest exists but cannot be read as the documented shape."""


@dataclass(frozen=True)
class RetiredTerm:
    """One retirement record: `term` matched case-insensitively on word boundaries."""

    term: str
    removed: str
    note: str
    allow_in: tuple[str, ...]


def _entry(raw: Any, idx: int) -> RetiredTerm:
    if not isinstance(raw, dict):
        raise RetiredTermsManifestError(f"terms[{idx}] is not a mapping")
    term = raw.get("term")
    removed = raw.get("removed")
    if not isinstance(term, str) or not term.strip():
        raise RetiredTermsManifestError(f"terms[{idx}] has no `term` string")
    if removed is None or not str(removed).strip():
        raise RetiredTermsManifestError(f"terms[{idx}] ({term!r}) has no `removed` date")
    allow = raw.get("allow_in") or []
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise RetiredTermsManifestError(f"terms[{idx}] ({term!r}) `allow_in` is not a list of strings")
    return RetiredTerm(
        term=term.strip(),
        removed=str(removed).strip(),
        note=str(raw.get("note") or "").strip(),
        allow_in=tuple(a.replace("\\", "/") for a in allow),
    )


def read_manifest(root: Path) -> list[RetiredTerm]:
    """Return the repo's retirement records; `[]` when `root` carries no manifest.

    Raises `RetiredTermsManifestError` when the manifest is present but malformed.
    """
    path = Path(root) / MANIFEST_RELPATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RetiredTermsManifestError(f"{MANIFEST_RELPATH} unreadable: {exc}") from exc
    try:
        data = safe_load(text)
    except Exception as exc:  # yaml.YAMLError family
        raise RetiredTermsManifestError(f"{MANIFEST_RELPATH} is not valid YAML: {exc}") from exc
    if data is None:
        return []
    terms = data.get("terms") if isinstance(data, dict) else None
    if not isinstance(terms, list):
        raise RetiredTermsManifestError(f"{MANIFEST_RELPATH} needs a top-level `terms:` list")
    return [_entry(raw, i) for i, raw in enumerate(terms)]


def _iter_docs(root: Path):
    """Yield (abs_path, repo-relative posix path) for `docs/**/*.md` plus top-level `*.md`."""
    seen = 0
    try:
        top = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return
    for entry in top:
        if entry.is_file() and entry.name.endswith(".md"):
            seen += 1
            yield Path(entry.path), entry.name
    base = root / _SCAN_ROOT
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            if seen >= _MAX_FILES:
                return
            seen += 1
            abs_path = Path(dirpath) / name
            yield abs_path, abs_path.relative_to(root).as_posix()


def scan_retired_terms(root: Path) -> list[dict[str, Any]]:
    """Return the hits of `scan_retired_terms_report`, dropping its completeness flag."""
    return scan_retired_terms_report(root)[0]


def scan_retired_terms_report(root: Path) -> tuple[list[dict[str, Any]], bool]:
    """Return `(hits, complete)`; `complete` is False when the process-time budget cut the scan short.

    Hits: one per doc line naming a retired term, in file then line order.

    Each hit: `{file, line, term, removed, note, text}` -- `file` repo-relative posix,
    `line` 1-based, `text` the stripped line (truncated). A line naming several terms
    yields one hit per term. `[]` when the repo has no manifest. Raises
    `RetiredTermsManifestError` for a malformed manifest.
    """
    root = Path(root)
    terms = read_manifest(root)
    if not terms:
        return [], True
    patterns = [
        (t, re.compile(r"(?<!\w)" + re.escape(t.term) + r"(?!\w)", re.IGNORECASE)) for t in terms
    ]
    hits: list[dict[str, Any]] = []
    complete = True
    started = _clock()
    for abs_path, rel in _iter_docs(root):
        if _clock() - started > _SCAN_BUDGET_SECS:
            complete = False
            break
        if rel == MANIFEST_RELPATH:
            continue
        active = [(t, p) for t, p in patterns if not any(rel.startswith(a) for a in t.allow_in)]
        if not active:
            continue
        try:
            if abs_path.stat().st_size > _MAX_FILE_BYTES:
                continue
            lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            for t, pat in active:
                if pat.search(line):
                    hits.append(
                        {
                            "file": rel,
                            "line": lineno,
                            "term": t.term,
                            "removed": t.removed,
                            "note": t.note,
                            "text": line.strip()[:_MAX_LINE_CHARS],
                        }
                    )
    return hits, complete
