"""
coordinator_core.ops.strategic.version_highlights — Track A: version_highlights derivation.

Purpose: derives the `version_highlights[]` field of the strategic self-description draft
from repo history. Signal readers, in priority order:

  1. **Tagged repo** — `git tag` present -> `git log <last-tag>..HEAD` for the highlight
     window; label/date derive from the most recent tag (and its tagger/commit date).
  2. **Tag-less repo (claude-klabauter's own path — 0 git tags verified)** — read
     `state/week-changelog/*.md` (pre-summarized daily/weekly bullets, the PREFERRED source)
     over a bounded recent window (most recent N daily files); label/date derive from the
     window. There is no existing week-changelog READER to reuse —
     `coordinator_core/ops/changelog_ops.py` is a WRITER family
     (`changelog.append_day`/`changelog.backfill_gaps`) and does not parse the files back out.
     This module authors its own minimal reader.
  3. **Neither tags nor changelog data** — bounded `git log` subject window as last resort.

Pinned return shape (frozen schema, generatable subset — see
DoE-claude/coordinator/schemas/strategic-self-description.schema.json):
    list[{
        "label":      str,               # short human-readable version/period label
        "date":       str,               # "YYYY-MM-DD" (schema format:date)
        "bullets":    list[str],         # highlight bullets for this entry
        "provenance": "generated",       # THREE-value provenance enum; generator emits ONLY this
    }]

Negative-spec: never emits provenance "curated" or "asserted" — those are human-authored only.
Never mutates state; this module performs no I/O writes (read-only git/subprocess + file reads).
Never crashes on a repo with no history signal at all — degrades to a typed [] "no highlights"
result (AC2), not a silent/ambiguous empty pass on a repo that DOES have changelog data (i.e. []
is returned only when genuinely no signal exists across all three readers).

Spec backlink: pln-claude-klabauter-generation-leg-machine--127c81 § C2
"""

from __future__ import annotations
import sys

import re
import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

_SUBPROCESS_TIMEOUT = 15

# Bounded recent window when falling back to week-changelog files or raw git log.
_CHANGELOG_WINDOW_FILES = 10
_GITLOG_FALLBACK_LIMIT = 20

_DAILY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$")
_HEADER_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s+—\s+(.+?)\s*$")
_FIELD_RE = re.compile(r"^\*\*([^*]+):\*\*\s*(.*)$")

# week-changelog fields worth surfacing as highlight bullets. "Plans touched" and "Decisions"
# are the meaningful signal; commit counts/branch/validation noise are skipped.
_BULLET_FIELDS = ("Plans touched", "Decisions", "Handoffs")

# Review: code-reviewer (F2) — "Plans touched" is curated separately (see _summarize_plans_touched)
# rather than copied verbatim; the other bullet fields keep their raw pass-through.
_PLANS_TOUCHED_FIELD = "Plans touched"
_PLANS_TOUCHED_CAP = 8
_PLAN_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
_PLAN_STATUS_SUFFIX_RE = re.compile(r"\s*\(status:[^)]*\)\s*$")
_PLAN_MORE_TOKEN_RE = re.compile(r"^\.{3}\s*and\s+\d+\s+more\s*$", re.IGNORECASE)


def _summarize_plans_touched(field_value: str) -> str | None:
    """Curate a raw ``Plans touched`` changelog field into a concise plan-slug bullet.

    Interaction note (review-integrator, sidecar-filter-defects slice): the writer
    side (`coordinator_core.ops.changelog_ops._is_plan_sidecar`) now filters sidecar
    basenames OUT of the "Plans touched" field entirely at write time, for every
    changelog written from this point forward — so the first-dot sidecar-tag strip
    below has nothing to strip in a freshly-written field and is dead code on that
    path. It stays load-bearing for `state/week-changelog/*.md` files written before
    this fix, which still carry baked-in sidecar tags on disk and are parsed by this
    same function via `_derive_from_changelog`. Do not delete the strip.

    The raw field is a comma-separated wall of ``docs/plans/<slug>.md (status: ...)`` paths
    (sometimes with an inherited "...and N more" truncation token baked in by the changelog
    writer). This collapses it into distinct, deduped plan slugs — basename, date-prefix and
    ``.md``/sub-suffix stripped, status annotation dropped — capped at `_PLANS_TOUCHED_CAP`
    entries with a "(+N more)" summary tail. Returns None if no slugs survive parsing.
    """
    slugs: list[str] = []
    seen: set[str] = set()
    for raw_entry in field_value.split(","):
        entry = raw_entry.strip()
        if not entry or _PLAN_MORE_TOKEN_RE.match(entry):
            continue
        entry = _PLAN_STATUS_SUFFIX_RE.sub("", entry).strip()
        if not entry:
            continue
        basename = entry.rsplit("/", 1)[-1]
        if basename.endswith(".md"):
            basename = basename[: -len(".md")]
        # `.md` comes off FIRST, then the slug is everything up to the first
        # remaining dot — a plan slug never contains an internal dot (verified
        # against the full docs/plans/ corpus, ~280 files, zero exceptions), so
        # a dotted segment after the slug is always a sidecar tag
        # (`.the Staff Engineer-review`, `.prior-art-check`, ...), including the archival
        # `.<kind>.<ISO-stamp>` variant. Structural, not an enumeration — mirrors
        # `_is_plan_sidecar` in coordinator_core.ops.changelog_ops. Replaced a
        # `\.(review|plan-coverage-check|...)$` alternation that only matched a
        # closed list of ~4 tag vocabularies and left most real sidecar tags
        # (`.the Staff Engineer-review`, `.sonnet-review`, `.node-map`, ...) baked into the
        # emitted slug.
        basename = basename.split(".", 1)[0]
        slug = _PLAN_DATE_PREFIX_RE.sub("", basename)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)

    if not slugs:
        return None

    shown = slugs[:_PLANS_TOUCHED_CAP]
    remainder = len(slugs) - len(shown)
    summary = f"Plans advanced: {', '.join(shown)}"
    if remainder > 0:
        summary += f" (+{remainder} more)"
    return summary


def _run_git(repo_root: Path, args: list[str]) -> str | None:
    """Run a read-only git subprocess in repo_root; return stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _run_git: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _latest_tag(repo_root: Path) -> str | None:
    """Return the most recent tag (by creation/commit order) or None if the repo is tag-less."""
    out = _run_git(repo_root, ["tag", "--sort=-creatordate"])
    if out is None:
        return None
    tags = [line.strip() for line in out.splitlines() if line.strip()]
    return tags[0] if tags else None


def _tag_date(repo_root: Path, tag: str) -> str | None:
    """Return YYYY-MM-DD for the tag's commit date, or None on failure."""
    out = _run_git(repo_root, ["log", "-1", "--format=%ad", "--date=short", tag])
    if out is None:
        return None
    date_str = out.strip()
    return date_str or None


def _derive_from_tags(repo_root: Path, latest_tag: str) -> list[dict]:
    """Track A signal (1): git log since the last tag, labeled/dated from the tag itself."""
    log_out = _run_git(
        repo_root,
        ["log", f"{latest_tag}..HEAD", "--format=%s", f"-{_GITLOG_FALLBACK_LIMIT}"],
    )
    bullets = []
    if log_out:
        bullets = [line.strip() for line in log_out.splitlines() if line.strip()]

    date = _tag_date(repo_root, latest_tag)
    if date is None:
        # Typed no-signal: a tag exists but we cannot resolve its date — degrade cleanly
        # rather than emit a highlight with a missing required "date" field.
        return []
    if not bullets:
        # Tag exists, no commits since -> nothing to highlight for this window.
        return []

    return [
        {
            "label": latest_tag,
            "date": date,
            "bullets": bullets,
            "provenance": "generated",
        }
    ]


def _parse_changelog_file(path: Path) -> dict | None:
    """Parse one state/week-changelog/YYYY-MM-DD-<host>.md file into a highlight entry dict.

    Returns None if the file has no parseable header (typed skip, not a crash).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        print(f"skip: _parse_changelog_file: text = path.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    lines = text.splitlines()
    if not lines:
        return None

    header_match = _HEADER_RE.match(lines[0].strip())
    if not header_match:
        return None
    date, host = header_match.group(1), header_match.group(2)

    bullets: list[str] = []
    for line in lines[1:]:
        field_match = _FIELD_RE.match(line.strip())
        if not field_match:
            continue
        field_name, field_value = field_match.group(1).strip(), field_match.group(2).strip()
        if field_name not in _BULLET_FIELDS:
            continue
        if not field_value or field_value.lower() == "none":
            continue
        if field_name == _PLANS_TOUCHED_FIELD:
            summary = _summarize_plans_touched(field_value)
            if summary is not None:
                bullets.append(summary)
            continue
        bullets.append(f"{field_name}: {field_value}")

    return {
        "label": f"{date} — {host}",
        "date": date,
        "bullets": bullets,
        "provenance": "generated",
    }


def _derive_from_changelog(repo_root: Path) -> list[dict]:
    """Track A signal (2): tag-less fallback — read state/week-changelog/*.md daily files.

    Bounded to the most recent _CHANGELOG_WINDOW_FILES files (by filename, which sorts
    chronologically since the convention is YYYY-MM-DD-<host>.md). HEADER.md and any file
    that fails the date-prefixed-filename convention are skipped.
    """
    changelog_dir = repo_root / "state" / "week-changelog"
    if not changelog_dir.is_dir():
        return []

    candidates = [
        entry for entry in changelog_dir.iterdir()
        if entry.is_file() and _DAILY_FILE_RE.match(entry.name)
    ]
    if not candidates:
        return []

    candidates.sort(key=lambda p: p.name, reverse=True)
    window = candidates[:_CHANGELOG_WINDOW_FILES]

    highlights: list[dict] = []
    for path in window:
        entry = _parse_changelog_file(path)
        if entry is None:
            continue
        if not entry["bullets"]:
            # No meaningful signal in this particular day's file — skip it rather than
            # emit an empty-bullets highlight.
            continue
        highlights.append(entry)

    # Chronological order (oldest window entry first) reads naturally as a highlight reel.
    highlights.sort(key=lambda h: h["date"])
    return highlights


def _derive_from_gitlog_fallback(repo_root: Path) -> list[dict]:
    """Track A signal (3): last resort — bounded git log subject window, no tags/changelog."""
    log_out = _run_git(
        repo_root,
        ["log", "--format=%ad|%s", "--date=short", f"-{_GITLOG_FALLBACK_LIMIT}"],
    )
    if not log_out:
        return []

    entries = []
    for line in log_out.splitlines():
        if "|" not in line:
            continue
        date, subject = line.split("|", 1)
        date, subject = date.strip(), subject.strip()
        if date and subject:
            entries.append((date, subject))
    if not entries:
        return []

    latest_date = entries[0][0]
    oldest_date = entries[-1][0]
    label = latest_date if latest_date == oldest_date else f"{oldest_date}..{latest_date}"

    return [
        {
            "label": label,
            "date": latest_date,
            "bullets": [subject for _date, subject in entries],
            "provenance": "generated",
        }
    ]


def derive_version_highlights(repo_root: Path) -> list[dict]:
    """Derive version_highlights[] entries for repo_root.

    Tries signal readers in priority order — tags, then week-changelog, then a bounded git
    log fallback — and returns the first one that yields non-empty highlights. Returns []
    only when none of the three readers finds any signal at all (typed "no highlights").

    Args:
        repo_root: guarded, resolved repo root (see coordinator_core.cartography._guard.path_guard).

    Returns:
        list[dict] — see module docstring "Pinned return shape".
    """
    latest_tag = _latest_tag(repo_root)
    if latest_tag:
        tag_highlights = _derive_from_tags(repo_root, latest_tag)
        if tag_highlights:
            return tag_highlights

    changelog_highlights = _derive_from_changelog(repo_root)
    if changelog_highlights:
        return changelog_highlights

    return _derive_from_gitlog_fallback(repo_root)
