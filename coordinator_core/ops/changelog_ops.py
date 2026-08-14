"""
coordinator_core.ops.changelog_ops — Family-A changelog write ops (strang-10 C1).

Purpose: Port of two coordinator-claude ceremony-cadence writer scripts into
Claude-klabauter-native ops. All are MUTATING (DR-208) and write coordinator substrate directly.
DR-216 is the reserved-noun-write carve-out authority for this op family.

Three ops registered here, byte-parity ports of claude-klabauter-owned CLI writers:
    changelog.append_day     ← Port of: workday-complete-step9-append-changelog.sh (DoE 6fb5fb37, 2026-07-22)
    changelog.backfill_gaps  ← Port of: backfill-week-changelog-gaps.sh (DoE b5a4192c, 2026-07-20)
    changelog.inject_anchor  ← Port of (injection path only): coordinator/bin/
        workday-complete-backfill-inject-anchor.py — the archive/daily-summaries/ anchor
        writer, sanctioned as a second D2(iv) archive/ sub-noun by the 2026-07-28 DR-216
        amendment (strang-10 handoff: state/handoffs/2026-07-28_115857_successor-of-
        2026-07-06_210200_strang-10-inject-anchor-archive-carveout.md).

Handler shape (matches queue_append.py pattern):
    @register_op("...") async def _handler(params, repo_root=None)
    repo_root = git_common_dir from _OP_KEY_SCOPE=common_dir
    worktree = main_worktree_root(repo_root) before any state/ or archive/ path join

Negative-spec:
    - changelog.append_day MUST NOT git-commit even though the oracle does.
      DR-216 D2(v): write the file only; the EM/caller retains commit responsibility.
    - changelog.inject_anchor rewrites ONLY the `covered_tip_sha:` / `covered_machine:`
      values, and ONLY when the recorded anchor is a strict ancestor of the target tip or
      is unresolvable (DR-216 § D2(iii-b), the named coverage-anchor-bump exception to
      D2(iii), PM-ratified 2026-07-28). Equal, descendant, and divergent anchors are left
      byte-identical — never bump backwards or across a fork, which would lose coverage
      information. Equality is re-checked AFTER resolution, because
      `git merge-base --is-ancestor X X` succeeds for an equal commit. The human-authored
      summary body and the prose note are never rewritten.
    - No rag store write (DR-208 Invariant-1 dual-write ban; tri-plane DD#1).
    - Blocking I/O runs via asyncio.to_thread (DR-216 D3 async-loop mandate).
    - No git commit from any handler (DR-216 D2(v)).
    - No cross-repo index.

Spec backlink: pln-strang-10-residual-writer-clus-b67ff8 § C1
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md
Oracle parity: [DoE] coordinator/bin/ (append_day/backfill_gaps oracles);
    coordinator/bin/workday-complete-backfill-inject-anchor.py (inject_anchor oracle, in-repo)
"""

from __future__ import annotations

# Generator-provenance declaration: append_day()/backfill_gaps() write
# per-date files under state/week-changelog/{date}.md and
# state/week-changelog/{date}-{host}-backfill.md -- an unbounded,
# date/host-dependent output set of already-tracked/newly-appended files,
# not a single fixed artifact.
MUTATES = ["state/week-changelog/*.md"]

import asyncio
import datetime
import json
import logging
import os
import re
import socket
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.machine_resolver import compute_machine
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.ops.fleet._common import main_worktree_root, parse_frontmatter_status
from coordinator_core.ops.list_review_trail_records import _collect as _collect_review_trail_files
from coordinator_core.ops.records_query import (
    _RecordsCollectError,
    _collect_type_records,
    _matches_where,
    _parse_where,
)
from coordinator_core.ops.workday_complete_backfill_scan import _run_git as _wcbs_run_git
from coordinator_core.wire_paths import rel_id

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _today_utc() -> str:
    """Return today's date in YYYY-MM-DD (UTC). Mirrors oracle's python3 -c call."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _next_day(date_str: str) -> str:
    """Return the day after date_str (YYYY-MM-DD)."""
    d = datetime.date.fromisoformat(date_str)
    return (d + datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def _iso_utc(date_str: str) -> str:
    """Format date_str as an ISO-8601 UTC instant. Mirrors oracle's iso_utc()."""
    return f"{date_str}T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Atomic write (DR-216 D3 — content-additive, no partial-read risk)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via mkstemp + os.replace (DR-216 D3).

    newline="" disables universal-newline translation — changelog content is
    a byte-contract (oracle-parity); without it, Windows text mode silently
    rewrites every embedded "\\n" to "\\r\\n".
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            print(f"skip: _atomic_write: os.unlink(tmp) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        raise


# ---------------------------------------------------------------------------
# Hostname helper
# ---------------------------------------------------------------------------


def _get_hostname() -> str:
    """Return the short hostname. Mirrors oracle's `hostname -s || hostname`."""
    for args in (["hostname", "-s"], ["hostname"]):
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=5,
                **no_console_creationflags(),
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            print(f"skip: _get_hostname: r = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
    return socket.gethostname().split(".")[0]


# ===========================================================================
# changelog.append_day
# ===========================================================================


def _reviewed_block_lines(reviewed_lines: List[str], has_non_trivial: bool) -> List[str]:
    """Render the **Reviewed:** line-block exactly as the oracle's compose_block does.

    Single source of truth for "what does a correct Reviewed: block look
    like" — shared by `_compose_block` (whole-section recompose) and
    `upsert_reviewed` (surgical single-field replace), so the two write paths
    can never drift out of lockstep on this one rendering rule.

    Rule: present (one line per record) when reviewed_lines is non-empty;
    the "none — flag..." sentinel when empty but has_non_trivial; omitted
    entirely (empty list) otherwise.
    """
    if reviewed_lines:
        return [f"**Reviewed:** {rline}" for rline in reviewed_lines]
    if has_non_trivial:
        return ["**Reviewed:** none — flag for /workweek-complete Step 7"]
    return []


def _compose_block(
    date: str,
    machine: str,
    branch: str,
    commit_count: int,
    commit_range: str,
    scope: str,
    plans_touched: str,
    handoffs_list: str,
    decisions: str,
    blockers: str,
    rc_validate: str,
    rc_plugin_suite: str,
    reviewed_lines: List[str],
    has_non_trivial: bool,
    is_backfill: bool = False,
) -> str:
    """Compose the daily changelog block. Mirrors compose_block() in step9-append-changelog.sh.

    Byte-parity: field order and printf format strings reproduced exactly.
    The **Scope:** line is omitted when `scope` is empty (oracle C4 omit-by-default).
    Returns the block WITHOUT trailing newline (caller adds it when writing,
    matching oracle's `printf '%s\\n' "${NEW_BLOCK}"` after command-substitution
    strips trailing newlines from compose_block output).

    `is_backfill`: when True, renders a `**Backfilled:**` provenance line
    (omit-by-default, mirroring **Scope:**/**Reviewed:** — never renders a
    "no" line, only present when true). Placed immediately after the
    **Reviewed:** line(s), before **Links:**.
    Spec backlink: cross-repo/inbox/2026-07-20-claude-central-em-debash-windows-validation-gaps.md
    ASK 2 — `compute_day_fields` already computed+returned `is_backfill`
    (:1247) but this function never rendered it (self-documented gap,
    the `changelog.compute_day_fields` docstring above).
    """
    lines: List[str] = [
        f"## {date} — {machine}",  # em-dash U+2014 matches oracle
        "",  # blank line (oracle: printf '%s\n\n' "${header}")
        f"**Branch:** {branch}",
        f"**Commits:** {commit_count} (range: {commit_range})",
    ]
    # Scope: omitted when empty — mirrors oracle C4 omit-by-default
    # (step9-append-changelog.sh: `[[ -n "${SCOPE}" ]] && printf '**Scope:** %s\n'`)
    if scope:
        lines.append(f"**Scope:** {scope}")
    lines += [
        f"**Plans touched:** {plans_touched}",
        f"**Handoffs:** {handoffs_list}",
        f"**Decisions:** {decisions}",
        f"**Blockers:** {blockers}",
        f"**Validation:** validate={rc_validate} plugin-suite={rc_plugin_suite}",
    ]
    # Reviewed field: present only when reviewed_lines or HAS_NON_TRIVIAL.
    # Rendering rule lives in _reviewed_block_lines() (shared with
    # changelog.upsert_reviewed so both stay in lockstep — single source of
    # truth for what a "correct" Reviewed: block looks like).
    lines += _reviewed_block_lines(reviewed_lines, has_non_trivial)
    # Backfilled provenance line — omit-by-default (only rendered when True).
    if is_backfill:
        lines.append("**Backfilled:** yes")
    # Links line — matches oracle printf exactly
    month = date[:7]
    lines.append(
        f"**Links:** archive/daily-summaries/{date}-{machine}.md, "
        f"archive/completed/{month}/ "
        f"(per-entry files; query via "
        f'`bin/query-completions --where "created={date}"`)'
    )
    return "\n".join(lines)


def _normalise_block(block: str) -> str:
    """Strip trailing whitespace per line and trailing blank lines.

    Mirrors oracle normalise_block() for idempotency comparison.
    """
    lines = [line.rstrip() for line in block.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _find_section_start(content: str, header: str) -> int:
    """Return the line-anchored start index of header in content, or -1 if absent.

    Review: code-reviewer (F1) — a plain content.find(header)/`header in content`
    substring search is exploitable by any two machine-name section headers where
    one is a lexical prefix of the other (e.g. "## 2026-02-01 — a" is a literal
    prefix of "## 2026-02-01 — ab") — safe_id() permits both individually. The
    per-day filename collapse (multiple machine sections now coexist in ONE file)
    is what newly exposes this. Anchor the match to a line boundary: header must
    sit at content start OR be immediately preceded by "\n", AND be immediately
    followed by "\n" or end-of-string.
    """
    pattern = re.compile(r"(?:^|\n)" + re.escape(header) + r"(?:\n|$)")
    m = pattern.search(content)
    if not m:
        return -1
    start = m.start()
    if content[start : start + 1] == "\n":
        start += 1
    return start


def _extract_section(content: str, header: str) -> str:
    """Extract the changelog section starting at header up to the next ## or EOF."""
    idx = _find_section_start(content, header)
    if idx == -1:
        return ""
    rest = content[idx:]
    m = re.search(r"\n## ", rest[1:])
    if m:
        section = rest[: m.start() + 1]
    else:
        section = rest
    return section.rstrip()


def _replace_section(content: str, header: str, new_block: str) -> str:
    """Replace the changelog section starting at header with new_block.

    Mirrors the python3 replacement logic in step9-append-changelog.sh.
    """
    idx = _find_section_start(content, header)
    if idx == -1:
        return content
    rest = content[idx:]
    m = re.search(r"\n## ", rest[1:])
    before = content[:idx]
    if m:
        after = rest[m.start() + 1 :]
        new_content = before + new_block.rstrip("\n") + "\n"
        if after.strip():
            # Review: code-reviewer (F6) — this leading "\n" is what supplies the
            # blank-line separator before the next section; after/new_block do not
            # carry it themselves.
            new_content += "\n" + after.lstrip("\n")
    else:
        new_content = before + new_block.rstrip("\n") + "\n"
    return new_content


def append_day(
    *,
    worktree: Path,
    date: str,
    machine: str,
    branch: str,
    commit_count: int,
    commit_range: str,
    scope: str,
    plans_touched: str = "none",
    handoffs_list: str = "none",
    decisions: str = "none",
    blockers: str = "none",
    rc_validate: str = "skipped",
    rc_plugin_suite: str = "n/a",
    reviewed_lines: Optional[List[str]] = None,
    has_non_trivial: bool = False,
    is_backfill: bool = False,
) -> dict:
    """Write (or idempotently update) the daily changelog block in state/week-changelog/.

    Byte-parity port of workday-complete-step9-append-changelog.sh write path.
    DOES NOT git-commit (DR-216 D2(v)): caller retains commit responsibility.

    Accepts pre-computed field values as params. The calling facade is responsible
    for git-derived fields (branch, commit_count, commit_range, plans_touched, etc.)
    matching the oracle's computation from the same repo.

    Write behaviour (mirrors oracle):
      - Section absent → append (or create) with newline separator.
      - Section present, normalised-equal → no-op (idempotent).
      - Section present, differs → replace in-place (read-modify-write via temp+os.replace).

    Returns:
        {out_path: str, action: "written" | "replaced" | "unchanged"}

    Review: code-reviewer (F2) — this pure function trusts its arguments;
    `date`-shape validation lives only in `_append_day_handler`. A caller
    reaching this function directly does NOT get that guard for free.
    """
    if reviewed_lines is None:
        reviewed_lines = []

    block = _compose_block(
        date=date,
        machine=machine,
        branch=branch,
        commit_count=commit_count,
        commit_range=commit_range,
        scope=scope,
        plans_touched=plans_touched,
        handoffs_list=handoffs_list,
        decisions=decisions,
        blockers=blockers,
        rc_validate=rc_validate,
        rc_plugin_suite=rc_plugin_suite,
        reviewed_lines=reviewed_lines,
        has_non_trivial=has_non_trivial,
        is_backfill=is_backfill,
    )
    section_header = f"## {date} — {machine}"

    changelog_dir = worktree / "state" / "week-changelog"
    changelog_dir.mkdir(parents=True, exist_ok=True)
    changelog_file = changelog_dir / f"{date}.md"

    norm_new = _normalise_block(block)

    if changelog_file.exists():
        existing = changelog_file.read_text(encoding="utf-8", errors="replace")
        # Review: code-reviewer (F1) — line-anchored lookup, not a plain substring
        # membership test; see _find_section_start for why (prefix-colliding
        # machine-name headers, e.g. "## {date} — a" vs "## {date} — ab").
        if _find_section_start(existing, section_header) != -1:
            # Idempotency check
            existing_section = _extract_section(existing, section_header)
            if _normalise_block(existing_section) == norm_new:
                return {"out_path": str(changelog_file), "action": "unchanged"}
            # Replace existing section (in-place, content-additive via temp+os.replace)
            new_content = _replace_section(existing, section_header, block)
            _atomic_write(changelog_file, new_content)
            declare_write(changelog_file)
            return {"out_path": str(changelog_file), "action": "replaced"}
        else:
            # Append section with blank-line separator (mirrors oracle: printf '\n' then printf '%s\n')
            # Review: code-reviewer — atomic read-modify-write satisfies DR-216 D3; open("a")
            #   was non-atomic and left the file partially written on crash/full filesystem.
            new_content = existing + "\n" + block + "\n"
            _atomic_write(changelog_file, new_content)
            declare_write(changelog_file)
            return {"out_path": str(changelog_file), "action": "written"}
    else:
        # Fresh file: `printf '%s\n' "${NEW_BLOCK}"` → block + single trailing newline
        _atomic_write(changelog_file, block + "\n")
        declare_write(changelog_file)
        return {"out_path": str(changelog_file), "action": "written"}


@register_op("changelog.append_day")
async def _append_day_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC changelog.append_day handler.

    MUTATING (writes state/week-changelog/{date}.md; the section header inside the
    file remains machine-keyed — "## {date} — {machine}" — per the per-day changelog
    filename collapse, PM ruling 2026-07-19).
    DOES NOT git-commit (DR-216 D2(v)): caller/EM retains commit responsibility.

    Required params:
        machine (str) — machine name of the calling session; MUST be the caller's
                        machine, NOT the daemon's (preserve legacy attribution, strang-10 anti-scope).
        branch  (str) — current branch name.
        commit_count (int) — number of commits for the day (0 when no commits).
        commit_range (str) — commit range string (e.g. "abc..def", "abc1234", "n/a").

    Optional params:
        date (str, YYYY-MM-DD) — defaults to today UTC.
        scope (str) — day scope summary; defaults to "" (empty) when not provided,
            which omits the **Scope:** line entirely (oracle C4 omit-by-default).
        plans_touched (str) — defaults to "none".
        handoffs_list (str) — defaults to "none".
        decisions (str) — defaults to "none".
        blockers (str) — defaults to "none".
        rc_validate (str) — defaults to "skipped".
        rc_plugin_suite (str) — defaults to "n/a".
        reviewed_lines (list[str]) — review trail summary lines; defaults to [].
        has_non_trivial (bool) — whether non-trivial commits exist; defaults to False.
        is_backfill (bool) — whether this block is a backfilled (not same-day)
            entry; defaults to False. Renders a **Backfilled:** provenance
            line when True (ASK 2, 2026-07-20-claude-central-em memo).

    Returns:
        {out_path: str, action: "written" | "replaced" | "unchanged"}
    """
    if repo_root is None:
        return {"error": "changelog.append_day: repo_root required", "action": "error"}
    worktree = main_worktree_root(repo_root)

    date = params.get("date") or _today_utc()
    # Containment (AC13, PM ruling 2026-07-19): date is interpolated raw into the
    # write-target filename "{date}.md" — reject traversal/non-ISO input before it
    # reaches the filesystem. This extends (does not replace) the existing safe_id
    # containment guards below, which cover 'machine' but never covered 'date'.
    # Review: code-reviewer (F2) — the digit-grouping regex is shape-only (it
    # accepts calendar-invalid values like "2026-13-45"); genuine calendar
    # validity is checked via date.fromisoformat below. The regex is kept as a
    # first-pass gate so fromisoformat's lenient 3.11+ alternate-ISO-form parsing
    # (e.g. unseparated "20260101") can't slip a differently-shaped-but-"valid"
    # string past what the write-target filename format expects.
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {
            "error": f"changelog.append_day: 'date' param is not a valid YYYY-MM-DD date: {date!r}",
            "action": "error",
        }
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        print(f"skip: _append_day_handler: datetime.date.fromisoformat(date) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {
            "error": f"changelog.append_day: 'date' param is not a valid YYYY-MM-DD date: {date!r}",
            "action": "error",
        }
    machine = params.get("machine", "").strip()
    if not machine:
        return {"error": "changelog.append_day: 'machine' param is required", "action": "error"}
    # Containment (Review: op-family path-containment sweep, 2026-07-08): machine is
    # interpolated raw into the changelog section header/links — reject separators/
    # traversal before it reaches the filesystem. See
    # docs/problems/2026-07-08-op-family-path-containment-investigation.md § 1c.
    if not safe_id(machine):
        return {
            "error": f"changelog.append_day: 'machine' param is not a safe filename segment: {machine!r}",
            "action": "error",
        }

    branch = str(params.get("branch", "unknown"))
    commit_count = int(params.get("commit_count", 0))
    commit_range = str(params.get("commit_range", "n/a"))
    # Scope: empty unless explicitly provided — mirrors oracle omit-by-default.
    # The oracle never injects "no work today"; an empty scope omits the line entirely.
    scope = str(params.get("scope") or "")
    plans_touched = str(params.get("plans_touched", "none"))
    handoffs_list = str(params.get("handoffs_list", "none"))
    decisions = str(params.get("decisions", "none"))
    blockers = str(params.get("blockers", "none"))
    rc_validate = str(params.get("rc_validate", "skipped"))
    rc_plugin_suite = str(params.get("rc_plugin_suite", "n/a"))
    reviewed_lines = params.get("reviewed_lines") or []
    has_non_trivial = bool(params.get("has_non_trivial", False))
    is_backfill = bool(params.get("is_backfill", False))

    return await asyncio.to_thread(
        append_day,
        worktree=worktree,
        date=date,
        machine=machine,
        branch=branch,
        commit_count=commit_count,
        commit_range=commit_range,
        scope=scope,
        plans_touched=plans_touched,
        handoffs_list=handoffs_list,
        decisions=decisions,
        blockers=blockers,
        rc_validate=rc_validate,
        rc_plugin_suite=rc_plugin_suite,
        reviewed_lines=reviewed_lines,
        has_non_trivial=has_non_trivial,
        is_backfill=is_backfill,
    )


# ===========================================================================
# changelog.backfill_gaps
# ===========================================================================


def _compose_backfill_block(date: str, host: str, git_body: str) -> str:
    """Compose a synthesized backfill block. Mirrors backfill-week-changelog-gaps.sh body.

    git_body: raw stripped output of `git log --format='%h %s'` (newest-first).
    Returns full file content including trailing newline (matches oracle's {} > $out redirect).

    Byte-parity: each echo in the oracle's here-doc adds exactly one newline;
    printf '%s\\n' "$body" adds one trailing newline to the body.
    """
    lines_of_body = [l for l in git_body.splitlines() if l]
    n = len(lines_of_body)
    # Oracle: first = oldest (tail -1 | awk '{print $1}'), last = newest (head -1 | awk '{print $1}')
    first = lines_of_body[-1].split()[0] if lines_of_body else ""
    last = lines_of_body[0].split()[0] if lines_of_body else ""

    # Mirrors the { echo ...; echo; ...; echo '```' } > $out block exactly.
    # Each echo contributes its text + \n; the final redirect captures all of them.
    return (
        f"## {date} — {host} (synthesized backfill)\n"
        f"\n"
        f"**Commits:** {n} (oldest: {first}, newest: {last})\n"
        f"**Scope:** (synthesized — daily ceremony skipped, no human-curated narrative)\n"
        f"\n"
        f"### Commit log\n"
        f"\n"
        "```\n"
        f"{git_body}\n"
        "```\n"
    )


def _has_daily_file(date: str, today: str, host: str, week_changelog_dir: Path) -> bool:
    """Return True if a sacred (non-overwritable) daily file exists for date.

    Mirrors has_daily() in backfill-week-changelog-gaps.sh:
      - Past dates: any file is sacred.
      - Today: only this script's own <today>-<host>-backfill.md is overwritable;
        any other file is sacred.

    Per-day filename collapse (PM ruling 2026-07-19, AC8): changelog.append_day now
    writes the per-day file at the bare "{date}.md" path (no "-{machine}" suffix) —
    the old "{date}-*.md" glob alone does NOT match that filename, so without this
    explicit check backfill would spuriously synthesize a duplicate
    "{date}-{host}-backfill.md" for a day that already has a real changelog.
    Checked explicitly (not folded into a loose "{date}*.md" glob) to avoid an
    over-match against unrelated same-prefix filenames.
    """
    if (week_changelog_dir / f"{date}.md").exists():
        return True  # per-day collapsed file — always sacred, never overwritable
    # Review: code-reviewer (F5) — own_backfill is only ever consulted inside the
    # date == today branch below; compute it lazily (once, on first use) so a
    # past-date call doesn't pay an unconditional resolve()/stat.
    own_backfill: Optional[Path] = None
    for f in week_changelog_dir.glob(f"{date}-*.md"):
        if date == today:
            if own_backfill is None:
                own_backfill = (week_changelog_dir / f"{today}-{host}-backfill.md").resolve()
            if f.resolve() == own_backfill:
                continue  # overwritable today-backfill — keep scanning
        return True  # found a sacred file
    return False


def _git_log_for_date(repo_path: str, date: str, next_date: str) -> str:
    """Get git log body for the date window. Mirrors oracle's body= git log call.

    Window: --after={date}T00:00:00+00:00 --before={next_date}T00:00:00+00:00
    Format: '%h %s' (short-sha + subject, newest-first).
    Returns stripped output (trailing newlines removed), matching oracle's $() capture.
    """
    try:
        r = subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "log",
                f"--after={_iso_utc(date)}",
                f"--before={_iso_utc(next_date)}",
                "--format=%h %s",
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            **no_console_creationflags(),
        )
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _git_log_for_date: r = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""


def backfill_gaps(
    *,
    repo_root: Path,
    host: Optional[str] = None,
    today_override: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Backfill synthesized daily changelog blocks for any date gap since WEEK_START.

    Byte-parity port of backfill-week-changelog-gaps.sh.
    Advisory: errors do not propagate (matches oracle's `trap 'exit 0' ERR`).
    Idempotent: skips dates that already have a sacred daily file.

    Params:
        repo_root: git common_dir (main_worktree_root derives the worktree).
        host: machine/host name override; defaults to COORDINATOR_MACHINE env or hostname.
        today_override: date override for 'today' (YYYY-MM-DD); defaults to current UTC date.
        dry_run: when True, compute the same {backfilled, skipped} result WITHOUT
            writing anything to disk (no `_atomic_write`, no `declare_write`).
            Added for cross-repo/inbox/2026-08-11-example-retrieval-repo-em-backfill-
            changelog-cli-three-defects.md item 1 -- there was previously no
            way to ask this op what it would do before it did it.

    Returns:
        {backfilled: [str, ...], skipped: [str, ...]}

    Review: code-reviewer (F2) — this pure function trusts its arguments;
    `today_override`-shape validation (unbounded loop toward
    `datetime.date.max` otherwise, one git-log subprocess per iteration)
    lives only in `_backfill_gaps_handler`. A caller reaching this function
    directly does NOT get that DoS guard for free.
    """
    worktree = main_worktree_root(repo_root)
    week_changelog_dir = worktree / "state" / "week-changelog"
    header_file = week_changelog_dir / "HEADER.md"

    if not header_file.exists():
        logger.warning("backfill_gaps: no HEADER.md at %s — nothing to backfill", header_file)
        return {"backfilled": [], "skipped": [], "message": "no HEADER.md"}

    header_text = header_file.read_text(encoding="utf-8", errors="replace")
    # Oracle: sed -nE 's/^\*\*Week starting:\*\* *([0-9]{4}-[0-9]{2}-[0-9]{2}).*/\1/p'
    m = re.search(
        r"^\*\*Week starting:\*\*\s*(\d{4}-\d{2}-\d{2})",
        header_text,
        re.MULTILINE,
    )
    if not m:
        logger.warning("backfill_gaps: HEADER.md has no parseable 'Week starting:' — skipping")
        return {"backfilled": [], "skipped": [], "message": "no Week starting: in HEADER.md"}

    week_start = m.group(1)
    today = today_override or _today_utc()
    resolved_host = (
        host
        or os.environ.get("COORDINATOR_MACHINE", "").strip()
        or _get_hostname()
    )

    if not dry_run:
        week_changelog_dir.mkdir(parents=True, exist_ok=True)

    backfilled: List[str] = []
    skipped: List[str] = []

    d = week_start
    while d <= today:
        next_d = _next_day(d)
        if not _has_daily_file(d, today, resolved_host, week_changelog_dir):
            git_body = _git_log_for_date(str(worktree), d, next_d)
            if git_body:
                out = week_changelog_dir / f"{d}-{resolved_host}-backfill.md"
                if not dry_run:
                    content = _compose_backfill_block(d, resolved_host, git_body)
                    _atomic_write(out, content)
                    declare_write(out)
                backfilled.append(str(out))
        else:
            skipped.append(d)
        d = next_d

    return {"backfilled": backfilled, "skipped": skipped}


@register_op("changelog.backfill_gaps")
async def _backfill_gaps_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC changelog.backfill_gaps handler.

    MUTATING (writes state/week-changelog/{date}-{host}-backfill.md per gap date).
    Advisory: errors return a non-fatal dict (matches oracle's trap 'exit 0' ERR).

    Optional params:
        host (str) — machine/host name. Defaults to COORDINATOR_MACHINE env or hostname.
                     Must be the calling session's machine (preserve legacy attribution).
        today (str, YYYY-MM-DD) — override for 'today'; defaults to current UTC date.

    Returns:
        {backfilled: [str, ...], skipped: [str, ...]}
    """
    if repo_root is None:
        return {"backfilled": [], "skipped": [], "error": "backfill_gaps: repo_root required"}

    host = params.get("host") or os.environ.get("COORDINATOR_MACHINE", "").strip() or None
    today_override = params.get("today")

    # Containment (AC13, PM ruling 2026-07-19): today_override flows into the
    # "{date}-{host}-backfill.md" write-target filename (via backfill_gaps()'s date
    # iteration) — reject traversal/non-ISO input before it reaches the filesystem.
    # Extends (does not replace) the existing safe_id containment guard on 'host' below.
    # Review: code-reviewer (F2) — the digit-grouping regex is shape-only and does
    # not reject an out-of-range value like "9999-99-99". Because backfill_gaps'
    # loop bound (`d <= today`) is a raw string comparison, a "9999-99-99"-shaped
    # today sorts above every real calendar date (its month digit '9' beats any
    # real month's leading '0'/'1') and the loop cannot terminate via that
    # comparison — it would iterate one real day at a time toward
    # datetime.date.max, spawning a git-log subprocess per iteration, before
    # _next_day finally raises. Calendar-validate via date.fromisoformat to reject
    # this before it ever reaches the loop.
    if today_override is not None:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", today_override):
            return {
                "backfilled": [],
                "skipped": [],
                "error": f"backfill_gaps: 'today' param is not a valid YYYY-MM-DD date: {today_override!r}",
            }
        try:
            datetime.date.fromisoformat(today_override)
        except ValueError:
            print(f"skip: _backfill_gaps_handler: datetime.date.fromisoformat(today_override) failed: {sys.exc_info()[1]}", file=sys.stderr)
            return {
                "backfilled": [],
                "skipped": [],
                "error": f"backfill_gaps: 'today' param is not a valid YYYY-MM-DD date: {today_override!r}",
            }

    # Review: code-reviewer — require explicit host to prevent silent attribution flip to
    #   daemon hostname when caller omits host and COORDINATOR_MACHINE is unset. Matches
    #   changelog.append_day's mandatory-machine discipline (DR-216 D2(iv) attribution).
    if host is None:
        return {
            "backfilled": [],
            "skipped": [],
            "error": (
                "backfill_gaps: 'host' param is required — pass the calling session's machine "
                "name to preserve legacy attribution. Neither 'host' param nor "
                "COORDINATOR_MACHINE env var is set."
            ),
        }

    # Containment (Review: op-family path-containment sweep, 2026-07-08): host is
    # interpolated raw into the filename "{date}-{host}-backfill.md" — reject
    # separators/traversal before it reaches the filesystem. See
    # docs/problems/2026-07-08-op-family-path-containment-investigation.md § 1c.
    if not safe_id(host):
        return {
            "backfilled": [],
            "skipped": [],
            "error": f"backfill_gaps: 'host' param is not a safe filename segment: {host!r}",
        }

    try:
        return await asyncio.to_thread(
            backfill_gaps,
            repo_root=repo_root,
            host=host,
            today_override=today_override,
        )
    except Exception as exc:  # advisory — never abort the caller
        logger.warning("changelog.backfill_gaps: advisory error (non-fatal): %s", exc)
        return {"backfilled": [], "skipped": [], "error": str(exc)}


# ===========================================================================
# changelog.compute_day_fields
# ===========================================================================
#
# COMPUTE_ONLY sibling of changelog.append_day: derives the git-log/handoff/
# review-trail field bundle append_day's params expect, so a caller (the
# step9 ceremony facade) no longer hand-computes them in bash. Reuses
# workday_complete_backfill_scan._run_git for the actual subprocess
# invocation (same bounded-timeout / closed-stdin / CREATE_NO_WINDOW
# discipline) and list_review_trail_records._collect for review-trail file
# enumeration, rather than re-deriving either.
#
# Port of: workday-complete-step9-append-changelog.sh (DoE 6fb5fb37, 2026-07-22)
#   (commit collection, TRIVIAL_PATTERN/SELF_COMMIT_REGEX, plans-touched,
#   handoffs enumeration, Decisions:/Blockers: extraction — BOTH the
#   python3 YAML-aware primary path and the grep -E fallback path — and the
#   Reviewed: review-trail record parsing — BOTH the python3 JSON primary
#   path and the grep -oE fallback path).
#
# Negative-spec:
#   - Read-only. No write of any kind (matches workday_complete_backfill_scan).
#   - Does NOT decide the HEADER staleness SKIP action (exit 3 in the oracle)
#     — it reports `header_stale`/`header_stale_days`/`header_week_start` and
#     leaves the skip-or-proceed call to the caller, same COMPUTE_ONLY/write
#     split DR-216 already draws for changelog.append_day.
#   - Does NOT compute `machine` — DR-216 D2(iv) attribution requires the
#     caller's own machine name, never a value this op could derive.
#   - `is_backfill`/`local_today` are reported for the caller's backfill-
#     provenance-marker use. `is_backfill` IS now consumed and rendered by
#     `changelog.append_day` (**Backfilled:** provenance line, ASK 2 of
#     cross-repo/inbox/2026-07-20-claude-central-em-debash-windows-validation-gaps.md
#     — resolved 2026-07-21; `_compose_block` previously left it dead). The
#     caller must thread the returned `is_backfill` value through to
#     `changelog.append_day`'s own `is_backfill` param itself — this op does
#     not call append_day directly.

_TRIVIAL_PATTERN = re.compile(r"^(chore|docs?)([(:]|$)|^workstream-complete quick-save")
_SELF_COMMIT_PATTERN = re.compile(
    r"chore\(week-changelog\): (\[backfill\] )?daily block \d{4}-\d{2}-\d{2}"
)


def _yesterday(date_str: str) -> str:
    """Return the calendar day before date_str (YYYY-MM-DD)."""
    d = datetime.date.fromisoformat(date_str)
    return (d - datetime.timedelta(days=1)).isoformat()


def _git_lines_at(worktree: Path, args: List[str]) -> List[str]:
    """Run `git -C worktree <args>` and return non-empty stdout lines, [] on failure."""
    result = _wcbs_run_git(["-C", str(worktree), *args])
    if result is None or result.returncode != 0:
        return []
    return [ln for ln in result.stdout.splitlines() if ln]


def _git_text_at(worktree: Path, args: List[str]) -> Optional[str]:
    """Run `git -C worktree <args>` and return raw stdout, or None on failure.

    Distinct from `_git_lines_at`: preserves blank lines and returns None (not
    []) on non-zero exit, so callers can tell "command failed / object absent"
    apart from "succeeded, empty output". Used for `git show <sha>:<path>`,
    where a non-zero exit means the path does not exist at that commit.
    """
    result = _wcbs_run_git(["-C", str(worktree), *args])
    if result is None or result.returncode != 0:
        return None
    return result.stdout


# ---------------------------------------------------------------------------
# Commit window collection (mirrors ALL_COMMITS_RAW / NON_TRIVIAL_* in the oracle)
# ---------------------------------------------------------------------------


def _collect_commits(
    worktree: Path, date: str, commit_span: Optional[str] = None
) -> List[Tuple[str, str]]:
    """Return (full_sha, subject) pairs, newest-first, for the day's commit window.

    `--no-merges` only — NO `--first-parent` (2026-07-19 PM ruling: changelogs
    cover all work, not per-device work; superseded the prior first-parent
    scoping). Self-commit exclusion (SELF_COMMIT_PATTERN) applied identically
    whether commit_span is given (C3 span-keyed path, replaces the derived
    date window) or not (date-window path).
    """
    if commit_span:
        args = ["log", "--format=%H%x09%s", commit_span, "--no-merges"]
    else:
        yesterday = _yesterday(date)
        args = [
            "log",
            "--format=%H%x09%s",
            f"--after={yesterday}T23:59:59",
            f"--before={date}T23:59:59",
            "--no-merges",
        ]
    out: List[Tuple[str, str]] = []
    for line in _git_lines_at(worktree, args):
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        chash, csubj = parts
        if not chash:
            continue
        if _SELF_COMMIT_PATTERN.search(csubj):
            continue
        out.append((chash, csubj))
    return out


def _commit_range(hashes: List[str]) -> Tuple[str, str, str]:
    """Return (oldest_short, newest_short, range_str). hashes is newest-first.

    Mirrors the oracle's OLDEST_SHA/NEWEST_SHA/COMMIT_RANGE derivation.
    """
    if not hashes:
        return "n/a", "n/a", "n/a"
    newest = hashes[0][:8]
    oldest = hashes[-1][:8]
    if len(hashes) == 1:
        return oldest, newest, newest
    return oldest, newest, f"{oldest}..{newest}"


# ---------------------------------------------------------------------------
# Plans touched (docs/plans/*.md name-only diff over the same commit window)
# ---------------------------------------------------------------------------


#: Reviewer/checker sidecars land in docs/plans/ and match the `docs/plans/*.md`
#: pathspec, but they are not plans and carry no plan status — filter them out
#: rather than asserting a status about them (example-retrieval-repo memo 2026-07-20, item 3).
#:
#: STRUCTURAL, not an enumeration: a plan filename is `YYYY-MM-DD-slug.md` with no
#: internal dots, so a sidecar is exactly a basename carrying a dotted segment
#: before `.md` — which covers the `.<kind>.<ISO-stamp>.md` archival variant for
#: free, and every future sidecar kind without an edit here. Same predicate
#: `ops.fleet.archive_plans._is_sidecar` already uses, deliberately.
#:
#: This replaced a `\.[a-z0-9-]*check(\.|$)` regex whose comment asserted that
#: "every sidecar kind on disk carries a dotted `-check` segment". That was false
#: when written and falser since: docs/plans/ holds ~46 dotted sidecars with no
#: `-check` segment (`.the Staff Engineer-review.md`, `.sonnet-review.md`, `.review.md`,
#: `.eng-director-review.md`, `.node-map.md`, `.phase0.md`), every one of which
#: leaked into "Plans touched" and had a plan status asserted about it.
def _is_plan_sidecar(basename: str) -> bool:
    """True when a `docs/plans/` basename is a sidecar rather than a plan."""
    return basename.count(".") > 1


def _as_of_sha(worktree: Path, date: str, commit_span: Optional[str] = None) -> Optional[str]:
    """Resolve the commit whose tree defines "as of the end of this day".

    Every other field in a daily block is point-in-time (commits, branch,
    handoffs, decisions are all as-of-that-day), so plan status must be too —
    otherwise a block backfilled days later stamps TODAY's statuses under an
    older date. `changelog.backfill_gaps` is the routine gap-filler, not a
    history-rewriting tool, so that is a live path, not a hypothetical.

    - commit_span (`BASE..TIP`) → the span's TIP.
    - date window → the last commit at or before `{date}T23:59:59`, the same
      `git rev-list -1 --before=…` probe example-retrieval-repo's forensic audit used.

    Returns None when the revision cannot be resolved (empty history, or a day
    preceding the first commit); callers fall back to the worktree read.
    """
    if commit_span:
        rev = commit_span.rsplit("..", 1)[-1]
    else:
        rev_lines = _git_lines_at(worktree, ["rev-list", "-1", f"--before={date}T23:59:59", "HEAD"])
        if not rev_lines:
            return None
        rev = rev_lines[0]
    resolved = _git_lines_at(worktree, ["rev-parse", "--verify", f"{rev}^{{commit}}"])
    return resolved[0] if resolved else None


def _plan_status(worktree: Path, rel_path: str, as_of: Optional[str] = None) -> str:
    """Return the frontmatter `status:` of a plan path named by git log.

    Point-in-time when `as_of` resolves: the status is read from the plan's blob
    at that commit, so a July block reports what the plan's status WAS in July.

    - Readable at `as_of` with a `status:` key → that value verbatim.
    - Readable but no `status:` key → "unknown".
    - Not present at `as_of` (deleted within the window, or a rename reported by
      `--name-only` under its old path) → "removed".
    - `as_of` unresolvable → falls back to the compose-time worktree read, with
      the same unknown/removed distinctions.
    """
    if as_of is not None:
        blob = _git_text_at(worktree, ["show", f"{as_of}:{rel_path}"])
        if blob is None:
            return "removed"
        fm = parse_frontmatter(blob).get("frontmatter") or {}
        status = fm.get("status")
        return str(status) if status else "unknown"

    abs_path = worktree / rel_path
    if not abs_path.is_file():
        return "removed"
    return parse_frontmatter_status(abs_path) or "unknown"


def _plans_touched(worktree: Path, date: str, commit_span: Optional[str] = None) -> str:
    """Return the oracle's PLANS_TOUCHED string: "none" or a comma-joined,
    sorted-unique list of "<path> (status: <real frontmatter status>)" entries.

    The status token is read from each named plan's own frontmatter — NOT a
    literal. The predecessor bash hardcoded "in-progress" ("status detection is
    out of scope") and the hardcode survived the 2026-07-19 bash→Python port
    verbatim, making every plan in every daily block read as in-progress and the
    weekly prior-week digest unusable. Fixed per example-retrieval-repo-em memo 2026-07-20
    (`cross-repo/archive/2026-07-20-example-retrieval-repo-em-changelog-plans-touched-hardcoded-status.md`).
    Status is POINT-IN-TIME (resolved at the day's tip via `_as_of_sha`), matching
    every other field in the block; a day backfilled later reports what the plan's
    status WAS then, not what it is at compose time. Checker sidecars are filtered
    (see `_is_plan_sidecar`); paths absent at the as-of commit render
    `(status: removed)`.

    No self-commit exclusion here — mirrors the oracle's documented
    asymmetry (the self-generated changelog commit never touches
    docs/plans/, F5).
    """
    if commit_span:
        args = ["log", "--format=", "--name-only", commit_span, "--no-merges", "--", "docs/plans/*.md"]
    else:
        yesterday = _yesterday(date)
        args = [
            "log",
            "--format=",
            "--name-only",
            f"--after={yesterday}T23:59:59",
            f"--before={date}T23:59:59",
            "--no-merges",
            "--",
            "docs/plans/*.md",
        ]
    files = sorted(
        {
            f
            for f in _git_lines_at(worktree, args)
            if f and not _is_plan_sidecar(f.rsplit("/", 1)[-1])
        }
    )
    if not files:
        return "none"
    # Resolved once per compose call, not per plan — one rev-list + rev-parse.
    as_of = _as_of_sha(worktree, date, commit_span=commit_span)
    return ", ".join(f"{f} (status: {_plan_status(worktree, f, as_of=as_of)})" for f in files)


# ---------------------------------------------------------------------------
# Handoffs for the day
# ---------------------------------------------------------------------------


def _handoffs_for_date(worktree: Path, date: str) -> Tuple[str, List[Path]]:
    """Return (HANDOFFS_LIST string, list of matching handoff Paths).

    Mirrors the oracle's `state/handoffs/{date}-*.md` glob, sorted, rendered
    as a comma-joined list relative to worktree, or "none".
    """
    handoff_dir = worktree / "state" / "handoffs"
    if not handoff_dir.is_dir():
        return "none", []
    paths = sorted(handoff_dir.glob(f"{date}-*.md"))
    if not paths:
        return "none", []
    rel = []
    for p in paths:
        try:
            # rel_id(): the changelog is a portable text artifact — a Windows
            # WindowsPath str() would emit `state\handoffs\…` backslashes into it.
            # Routed through the shared helper so there is one construction, not
            # a hand-rolled as_posix() per call site (see coordinator_core.wire_paths).
            rel.append(rel_id(p, worktree))
        except ValueError:
            rel.append(p.as_posix())
    return ", ".join(rel), paths


# ---------------------------------------------------------------------------
# Decisions / Blockers extraction — BOTH paths ported (python3 YAML-aware
# primary + grep -E fallback in the oracle). Primary is tried first; on any
# parse exception (or when force_fallback=True, used by the test suite to
# exercise the fallback path directly) the fallback runs instead — mirrors
# the oracle's "python3 available, but empty result" -> grep fallback chain.
# ---------------------------------------------------------------------------

# Negative-spec (break-class fix, 2026-07-28 — do NOT restore `\s*` here).
# `\s` matches a NEWLINE. `_extract_field_fallback` below searches the WHOLE
# file text with re.MULTILINE, so with `\s*` a present-but-empty `field:` line
# let the pad walk past the line break and `(.+)` then harvested the FOLLOWING
# line into the changelog — a handoff carrying a bare `decisions:` reported its
# neighbour's `blockers:` value as its decisions. `(.+)` cannot settle for the
# empty value, so there was no benign "matched nothing" outcome. Padding is
# horizontal whitespace only; `(.+)` still never crosses a line because `.`
# excludes `\n` and a trailing `\r` is removed by the caller's `.strip()`.
#
# The canonical frontmatter key-resolution pattern (and the full statement of
# this defect class) lives in `coordinator_core.frontmatter.primitives`. This
# module deliberately does NOT route through `primitives.read_fm_field`: see
# `_extract_field_fallback`'s docstring for why. Fix the pad here; do not fork
# a fresh copy of this regex elsewhere.
_FM_FIELD_RE_TMPL = r"^{field}:[ \t]*(.+)"
_MD_HEADING_RE_TMPL = r"##\s+(?:{alternation})\s*\n((?:(?!##).)+)"

# Canonical field name -> heading aliases actually in use in state/handoffs/
# bodies. The bare display name (e.g. "Decisions") never appears as a heading
# on disk — handoffs write "## Key Decisions Made" / "## Blockers or Issues" —
# so a bare-name-only match returned "none" universally (break-class, fixed
# 2026-08-06). Data-driven: a field absent from this map falls back to
# `(field,)` rather than adding branching inside the regex builder.
_HEADING_ALIASES: Dict[str, Tuple[str, ...]] = {
    "Decisions": ("Key Decisions Made", "Decisions Made", "Decisions"),
    "Blockers": ("Blockers or Issues", "Blockers"),
}

# Decisions/Blockers sections are multi-paragraph handoff prose; joined across
# every matching handoff for the day this can run to several thousand
# characters, far too long for a single changelog block line. Truncate on a
# "; " bullet boundary where possible so the cut doesn't land mid-clause.
_FIELD_VALUE_CHAR_CAP = 400


def _heading_alternation(field: str) -> str:
    """Build `field`'s heading-match alternation, longest-alias-first.

    Longest-first so a longer alias ("Key Decisions Made") is tried before a
    shorter one that is also its prefix ("Decisions") would otherwise shadow
    it. `dict.fromkeys` dedupes while preserving the map's declared order
    before the length sort settles ties.
    """
    aliases = _HEADING_ALIASES.get(field, (field,))
    ordered = sorted(dict.fromkeys(aliases), key=len, reverse=True)
    return "|".join(re.escape(a) for a in ordered)


def _cap_joined_value(text: str) -> str:
    """Bound a joined Decisions/Blockers value to `_FIELD_VALUE_CHAR_CAP`.

    Truncates on the last "; " bullet-join boundary at or before the cap when
    one exists, so a cut lands between bullets rather than mid-clause, then
    appends an ellipsis. A value at or under the cap is returned unchanged.
    """
    if len(text) <= _FIELD_VALUE_CHAR_CAP:
        return text
    truncated = text[:_FIELD_VALUE_CHAR_CAP]
    boundary = truncated.rfind("; ")
    if boundary > 0:
        truncated = truncated[:boundary]
    return truncated + "…"


def _extract_field_primary(field: str, handoff_paths: List[Path]) -> str:
    """YAML-frontmatter-aware + markdown-heading-aware extraction.

    Mirrors the oracle's inline python3 heredoc for the frontmatter leg: looks
    for a `field: value` line inside a `---`-delimited frontmatter block. The
    markdown leg is NOT oracle parity — it matches `field`'s canonical heading
    aliases from `_HEADING_ALIASES` (e.g. "## Key Decisions Made", not just a
    bare "## Decisions"), because the bare name is not a heading any handoff
    template writes; matching only it made this leg permanently dead (see
    module comment above `_HEADING_ALIASES`). Content runs to the next `##`
    heading. Every match across every file is collected and semicolon-joined.
    """
    collected: List[str] = []
    fm_re = re.compile(_FM_FIELD_RE_TMPL.format(field=re.escape(field)), re.IGNORECASE | re.MULTILINE)
    md_re = re.compile(
        _MD_HEADING_RE_TMPL.format(alternation=_heading_alternation(field)),
        re.IGNORECASE | re.DOTALL,
    )

    for fpath in handoff_paths:
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            print(f"skip: _extract_field_primary: content = fpath.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue

        fm_match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
        if fm_match:
            fm = fm_match.group(1)
            for line in fm.splitlines():
                m = fm_re.match(line)
                if m:
                    val = m.group(1).strip()
                    if val and val not in ("none", "n/a", ""):
                        collected.append(val)

        for m in md_re.finditer(content):
            val = m.group(1).strip()
            lines = [re.sub(r"^[-*]\s+", "", ln).strip() for ln in val.splitlines() if ln.strip()]
            text = "; ".join(ln for ln in lines if ln)
            if text:
                collected.append(text)

    return "; ".join(collected) if collected else "none"


def _extract_field_fallback(field: str, handoff_paths: List[Path]) -> str:
    """grep -E fallback: frontmatter-style `field: value` line only (no
    markdown-heading scan — matches the oracle's grep fallback's narrower
    coverage, F6 generic-strip semantics).

    Shares `_FM_FIELD_RE_TMPL` with `_extract_field_primary` rather than
    carrying a second hand-copy of the same regex — the two copies had already
    drifted into being separately maintainable, which is how the
    newline-crossing `\\s*` pad documented on that constant survived a
    tree-wide sweep of the same defect (2026-07-28).

    Negative-spec — this reader does NOT route through
    `coordinator_core.frontmatter.primitives.read_fm_field`, and should not be
    "unified" with it:

    - `read_fm_field` is CASE-SENSITIVE; this path is `re.IGNORECASE` by
      contract. Callers pass display-cased field names ("Decisions",
      "Blockers") against lower-cased on-disk frontmatter keys, so the fold is
      load-bearing, not incidental.
    - `read_fm_field` takes a frontmatter BLOCK (the text between `---`
      delimiters). This function mirrors the oracle's `grep -E` fallback, which
      scans the whole file with no frontmatter parse at all — narrowing it to a
      parsed block would change which lines match and break byte-parity with
      the oracle it exists to mirror.
    - The two disagree on empty values by design: `read_fm_field` distinguishes
      absent (`None`) from present-but-empty (`''`), while this path's `(.+)`
      declines to match an empty value so the search continues to a later,
      non-empty occurrence.

    The single defect the two DID share — the `\\s*` pad — is fixed in place on
    `_FM_FIELD_RE_TMPL` above.

    Deliberate divergence (not a defect): `extract_field_from_handoffs` caps
    this function's joined return value at `_FIELD_VALUE_CHAR_CAP` chars. The
    oracle's `grep -E` fallback has no such cap — it was written for
    single-line frontmatter values, never for the multi-paragraph prose the
    markdown-heading leg (primary path only) now also feeds through this same
    joiner. The cap keeps the changelog block readable; it is not a parity
    target.
    """
    fm_re = re.compile(
        _FM_FIELD_RE_TMPL.format(field=re.escape(field)),
        re.IGNORECASE | re.MULTILINE,
    )
    combined: List[str] = []
    for fpath in handoff_paths:
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            print(f"skip: _extract_field_fallback: content = fpath.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        m = fm_re.search(content)
        if not m:
            continue
        val = m.group(1).strip()
        if val and val not in ("none", "n/a"):
            combined.append(val)
    return "; ".join(combined) if combined else "none"


def extract_field_from_handoffs(
    field: str, handoff_paths: List[Path], *, force_fallback: bool = False
) -> str:
    """Extract `field` (e.g. "Decisions", "Blockers") from handoff bodies.

    Public (unprefixed) — both extraction paths are independently unit-tested
    against this entry point. `force_fallback=True` bypasses the primary path
    to exercise the fallback path directly (mirrors the oracle's `command -v
    python3` absence branch, which this environment can never naturally hit
    since the op itself runs under Python). The returned value is always
    passed through `_cap_joined_value` — on BOTH the primary and fallback
    return paths, not just one — so a several-thousand-character joined
    prose section never reaches the changelog block unbounded.
    """
    if not handoff_paths:
        return "none"
    if not force_fallback:
        try:
            result = _extract_field_primary(field, handoff_paths)
        except Exception:  # noqa: BLE001 - any parse failure degrades to fallback
            result = ""
        if result:
            return _cap_joined_value(result)
    return _cap_joined_value(_extract_field_fallback(field, handoff_paths))


# ---------------------------------------------------------------------------
# Reviewed: review-trail record lines — BOTH paths ported (python3 json
# primary + grep -oE fallback in the oracle).
# ---------------------------------------------------------------------------


def _parse_review_record_primary(record_path: Path) -> str:
    """json.load parse — mirrors the oracle's inline python3 -c JSON reader."""
    with record_path.open(encoding="utf-8", errors="replace") as f:
        d = json.load(f)
    sha = d.get("sha_range", d.get("commit_range", "unknown"))
    rev = d.get("reviewer", "unknown")
    ver = d.get("verdict", "unknown")
    loc = d.get("diff_loc", d.get("diff_lines", "unknown"))
    return f"sha_range={sha} reviewer={rev} verdict={ver} diff_loc={loc}"


def _parse_review_record_fallback(record_path: Path) -> str:
    """grep -oE fallback: independent per-field regex extraction over raw text."""
    try:
        text = record_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""

    def _field(name: str) -> str:
        m = re.search(r'"' + name + r'"\s*:\s*"([^"]*)"', text)
        return m.group(1) if m else "unknown"

    sha_val = _field("sha_range")
    rev_val = _field("reviewer")
    ver_val = _field("verdict")
    loc_val = _field("diff_loc")
    return f"sha_range={sha_val} reviewer={rev_val} verdict={ver_val} diff_loc={loc_val}"


def parse_review_record(record_path: Path, *, force_fallback: bool = False) -> str:
    """Parse one review-trail JSON record into the oracle's Reviewed: line shape.

    Public (unprefixed) — both extraction paths independently unit-tested.
    """
    if not force_fallback:
        try:
            return _parse_review_record_primary(record_path)
        except Exception:  # noqa: BLE001 - any parse failure degrades to fallback
            pass
    return _parse_review_record_fallback(record_path)


def _reviewed_lines_for_date(worktree: Path, date: str) -> List[str]:
    """Enumerate + parse review-trail records for `date` (live + archive union).

    Reuses list_review_trail_records._collect for file enumeration (same live
    `state/review-trail/` + `archive/review-trail/` union, basename-sorted).
    """
    live_dir = worktree / "state" / "review-trail"
    archive_dir = worktree / "archive" / "review-trail"
    try:
        records = _collect_review_trail_files(str(live_dir)) + _collect_review_trail_files(str(archive_dir))
    except OSError:
        print(f"skip: _reviewed_lines_for_date: records = _collect_review_trail_files(str(live_dir)) + _collect_review failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    records = [r for r in records if r[0].startswith(date)]
    records.sort(key=lambda r: r[0])
    return [parse_review_record(Path(fullpath)) for _basename, fullpath in records]


def _has_non_trivial_for_date(worktree: Path, date: str) -> bool:
    """Return whether `date`'s commit window contains any non-trivial commit.

    Reuses `_collect_commits`/`_TRIVIAL_PATTERN` — the SAME date-window,
    no-machine-filter derivation `compute_day_fields` already uses for
    `has_non_trivial`. Not machine-scoped, by design: see module note at
    `upsert_reviewed` for why (review-trail records carry no machine field).
    """
    commits = _collect_commits(worktree, date)
    return any(not _TRIVIAL_PATTERN.search(subject) for _sha, subject in commits)


# ===========================================================================
# changelog.upsert_reviewed
# ===========================================================================
#
# Surgical single-field upsert of the **Reviewed:** line-block for a given
# (date, machine). Curation-preserving counterpart to changelog.append_day:
# append_day recomposes an ENTIRE machine section from caller-supplied
# fields, which would clobber any human-curated Scope:/Commits:/etc content
# on that day's block. This op touches ONLY the **Reviewed:** line(s),
# leaving every other line of the section byte-identical.
#
# Machine-scoping note: review-trail JSON records carry NO machine field
# (verified against the live schema — sha_range/reviewer/scope/scope_kind/
# verdict/diff_loc/session_id/workstream only). `_reviewed_lines_for_date`
# (reused here, same helper `_compose_block`'s whole-section path calls via
# `compute_day_fields`) is therefore a DATE-scoped derivation, not a
# (date, machine) filter — `machine` in this op's signature selects WHICH
# section of the file to touch, not a filter on what gets rendered into it.
# This mirrors the pre-existing behavior of `changelog.append_day` /
# `changelog.compute_day_fields`, which already render the same Reviewed:
# value into every machine's section for a given date; this op does not
# change that convention, only the write mechanics (surgical vs. whole-
# section recompose).
#
# Spec backlink: cross-repo/inbox/2026-07-21-claude-central-em-reviewed-line-surgical-upsert.md
# DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
#   (same state/week-changelog/ reserved-noun carve-out changelog.append_day
#   is sanctioned under — this op writes the identical noun, no new surface).


_REVIEWED_LINE_RE = re.compile(r"^\*\*Reviewed:\*\*.*$")
_VALIDATION_LINE_RE = re.compile(r"^\*\*Validation:\*\*.*$")
_LINKS_LINE_RE = re.compile(r"^\*\*Links:\*\*.*$")


def upsert_reviewed(*, worktree: Path, date: str, machine: str) -> dict:
    """Re-derive and upsert ONLY the **Reviewed:** line for (date, machine).

    Leaves the rest of the `## {date} — {machine}` section BYTE-IDENTICAL —
    unlike `append_day`, which recomposes the whole section from supplied
    fields and would clobber human-curated Scope:/Commits: content. Re-
    derives the Reviewed value itself from review-trail records for `date`
    (never trusts a caller-supplied snapshot — trusting a stale snapshot is
    what created the `Reviewed: none` staleness bug this op fixes).

    Write behaviour:
      - No `state/week-changelog/{date}.md` file yet → no-op ("no_match").
      - File exists but has no `## {date} — {machine}` section → no-op
        ("no_match"). Both are legitimate probes (the DoE-side caller scans
        many (date, machine) pairs at /workweek-complete), NOT errors.
      - Section exists, derived Reviewed: block already matches what is on
        disk → no-op ("unchanged") — idempotent re-run.
      - Section exists, derived Reviewed: block differs (including the
        insert-where-none-existed and remove-down-to-none cases) → in-place
        replace via the same atomic read-modify-write `append_day` uses.

    Returns:
        {out_path: str, action: "replaced" | "unchanged" | "no_match"}

    Review: code-reviewer (F2 precedent, see `append_day`'s own docstring) —
    this pure function trusts its arguments; `date`/`machine` shape
    validation lives only in `_upsert_reviewed_handler`.
    """
    changelog_dir = worktree / "state" / "week-changelog"
    changelog_file = changelog_dir / f"{date}.md"
    section_header = f"## {date} — {machine}"

    if not changelog_file.exists():
        return {"out_path": str(changelog_file), "action": "no_match"}

    existing = changelog_file.read_text(encoding="utf-8", errors="replace")
    if _find_section_start(existing, section_header) == -1:
        return {"out_path": str(changelog_file), "action": "no_match"}

    section = _extract_section(existing, section_header)
    section_lines = section.split("\n")

    reviewed_lines = _reviewed_lines_for_date(worktree, date)
    has_non_trivial = _has_non_trivial_for_date(worktree, date)
    new_reviewed_block = _reviewed_block_lines(reviewed_lines, has_non_trivial)

    old_indices = [i for i, ln in enumerate(section_lines) if _REVIEWED_LINE_RE.match(ln)]
    # Review: code-reviewer (Finding 1) — old_indices is the contiguous run
    # compose_block always emits together for machine-generated sections, but
    # this op's premise is that the section may carry human curation. A
    # curator-added, non-contiguous line elsewhere in the section that happens
    # to start with "**Reviewed:**" would make old_indices non-contiguous; the
    # strip-then-reinsert below would then relocate/collapse that stray
    # content. Treat non-contiguous matches as "no managed block found" and
    # fall through to the fresh-insertion path, leaving the stray line alone.
    is_contiguous = old_indices == list(range(old_indices[0], old_indices[0] + len(old_indices))) if old_indices else False

    if old_indices and is_contiguous:
        # old_indices is the contiguous run compose_block always emits
        # together — everything before it is untouched, so its count (and
        # therefore the correct re-insertion point) is unchanged by removal.
        insert_pos = old_indices[0]
        kept_lines = [ln for i, ln in enumerate(section_lines) if i not in old_indices]
        new_section_lines = kept_lines[:insert_pos] + new_reviewed_block + kept_lines[insert_pos:]
    else:
        # Never had a Reviewed: block — anchor the insert on **Validation:**
        # (compose_block's fixed position, immediately before Reviewed:),
        # falling back to immediately before **Links:**, then end-of-section,
        # for a hand-curated section that dropped/renamed either anchor line.
        validation_idx = next(
            (i for i, ln in enumerate(section_lines) if _VALIDATION_LINE_RE.match(ln)), None
        )
        if validation_idx is not None:
            insert_pos = validation_idx + 1
        else:
            links_idx = next(
                (i for i, ln in enumerate(section_lines) if _LINKS_LINE_RE.match(ln)), None
            )
            insert_pos = links_idx if links_idx is not None else len(section_lines)
        new_section_lines = section_lines[:insert_pos] + new_reviewed_block + section_lines[insert_pos:]

    new_section = "\n".join(new_section_lines)

    if new_section == section:
        return {"out_path": str(changelog_file), "action": "unchanged"}

    new_content = _replace_section(existing, section_header, new_section)
    _atomic_write(changelog_file, new_content)
    return {"out_path": str(changelog_file), "action": "replaced"}


@register_op("changelog.upsert_reviewed")
async def _upsert_reviewed_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC changelog.upsert_reviewed handler.

    MUTATING (surgically rewrites the **Reviewed:** line(s) inside
    state/week-changelog/{date}.md's `## {date} — {machine}` section; every
    other line of that section, and every other section in the file, is
    left byte-identical). DOES NOT git-commit (DR-216 D2(v)): caller/EM
    retains commit responsibility — same discipline as changelog.append_day.

    Required params:
        date (str, YYYY-MM-DD) — the changelog day to correct.
        machine (str) — the machine section to correct. MUST be the target
            section's own machine name (attribution parity with
            changelog.append_day's mandatory-machine discipline).

    Returns:
        {out_path: str, action: "replaced" | "unchanged" | "no_match"}
        (or {"error": str, "action": "error"} on param validation failure)
    """
    if repo_root is None:
        return {"error": "changelog.upsert_reviewed: repo_root required", "action": "error"}
    worktree = main_worktree_root(repo_root)

    date = params.get("date", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {
            "error": f"changelog.upsert_reviewed: 'date' param is not a valid YYYY-MM-DD date: {date!r}",
            "action": "error",
        }
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        print(f"skip: _upsert_reviewed_handler: datetime.date.fromisoformat(date) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {
            "error": f"changelog.upsert_reviewed: 'date' param is not a valid YYYY-MM-DD date: {date!r}",
            "action": "error",
        }

    machine = params.get("machine", "").strip()
    if not machine:
        return {"error": "changelog.upsert_reviewed: 'machine' param is required", "action": "error"}
    if not safe_id(machine):
        return {
            "error": f"changelog.upsert_reviewed: 'machine' param is not a safe filename segment: {machine!r}",
            "action": "error",
        }

    return await asyncio.to_thread(
        upsert_reviewed,
        worktree=worktree,
        date=date,
        machine=machine,
    )


# ===========================================================================
# changelog.inject_anchor
# ===========================================================================
#
# Injects covered_tip_sha:/covered_machine: anchor lines into a pre-existing
# archive/daily-summaries/ daily summary that lacks them. Port of (INJECTION
# PATH ONLY): coordinator/bin/workday-complete-backfill-inject-anchor.py.
#
# BUMP PATH (PM-ratified 2026-07-28, DR-216 § D2(iii-b)): the oracle's
# _rewrite_anchor path — replacing an existing covered_tip_sha:/covered_machine:
# line's value in place — IS ported, bounded strictly to convergent writes:
#   - recorded anchor is a strict ANCESTOR of the target tip, or UNRESOLVABLE
#     (a dangling SHA the repo no longer has) → bump.
#   - recorded anchor is EQUAL to, a DESCENDANT of, or DIVERGENT from the
#     target tip → already_anchored, file left byte-identical. Never bump
#     backwards or across a fork.
# Only the two anchor lines are ever rewritten (first occurrence of each);
# the prose note and the summary body stay untouched, and this op still never
# issues a git commit. D2(iii)'s general no-rewrite rule is otherwise unchanged
# — this is a named, bounded exception (D2(iii-b)), not a general softening.
#
# Sanctioned noun: archive/daily-summaries/<date>-<machine>.md, this op only
# (DR-216 D2(iv) amendment 2026-07-28).
#
# Negative-spec: the oracle's `_derive_machine` auto-derivation fallback
# (branch-ref enumeration over refs/heads/work/*, falling back to
# coordinator_core.machine_resolver.compute_machine() when the caller omits
# `machine`) is deliberately NOT ported — `machine` is a hard-required param
# here, with no derivation. Push resolution to the caller rather than embed
# ref-enumeration policy in this library-shaped op.

_ANCHOR_KEY = "covered_tip_sha:"
_MACHINE_KEY = "covered_machine:"


def _recorded_anchor_sha(content: str) -> Optional[str]:
    """Return the value of the first live covered_tip_sha: line, or None if no
    live anchor is present.

    Scans line-by-line, tracking fenced-code-block (```) state, so a marker
    string appearing inside a quoted code fence (e.g. documentation showing the
    anchor format) is never mistaken for a live anchor. Mirrors the oracle's
    bare line-start `_ANCHOR_KEY` check (workday-complete-backfill-inject-anchor.py),
    plus the fenced-code-block guard so presence and value extraction never
    disagree on what counts as a "live" anchor line.
    """
    in_fence = False
    for line in content.splitlines():
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(_ANCHOR_KEY):
            parts = line.split()
            return parts[1] if len(parts) >= 2 else None
    return None


def _anchor_present(content: str, date: str, machine: str) -> bool:
    """Return True if `content` already carries a live covered_tip_sha: anchor line.

    `date`/`machine` are accepted (not consulted for the presence test itself —
    the target file is already keyed to a single (date, machine) pair by its own
    filename) to keep this function's contract explicit and stable for callers,
    matching the C1 dispatch's named-contract requirement.
    """
    return _recorded_anchor_sha(content) is not None


def _resolve_daily_summary_target(worktree: Path, date: str, machine: str) -> Optional[Path]:
    """Resolve the archive/daily-summaries/ target file for (date, machine).

    Mirrors the oracle's three-way fallback exactly: an exact per-machine
    filename match, then any "{date}-*.md" match (sorted, first wins), then the
    legacy bare "{date}.md" shape. The real directory is a mix of both shapes
    (§ reconciliation pass item 2 of the retired plan) — do not assume uniform
    naming across archive/daily-summaries/.
    """
    ds_dir = worktree / "archive" / "daily-summaries"
    cand1 = ds_dir / f"{date}-{machine}.md"
    if cand1.is_file():
        return cand1
    for cand in sorted(ds_dir.glob(f"{date}-*.md")):
        if cand.is_file():
            return cand
    cand3 = ds_dir / f"{date}.md"
    if cand3.is_file():
        return cand3
    return None


def _render_anchor_injection(
    content: str, full_sha: str, machine: str, today: str
) -> Optional[str]:
    """Return the new file content with the anchor block inserted, or None if
    the file structure is malformed (unclosed frontmatter / no H1) — mirrors
    the oracle's awk END-guard exits (`_inject_anchor` in the oracle CLI).

    Pure content-additive: every existing line is preserved verbatim; only the
    two bare key lines + the prose note are inserted (DR-216 D2(iii)/D3).
    """
    lines = content.splitlines(keepends=True)
    note = (
        f"> _Record anchor injected {today} by /workday-complete backfill "
        "(mechanical) — summary content pre-existing._\n"
    )
    first_line = lines[0].rstrip("\n") if lines else ""

    out: List[str] = []
    if first_line == "---":
        # YAML frontmatter: insert bare key lines before the closing --- ;
        # prose note after the H1.
        keys_done = False
        note_done = False
        for i, line in enumerate(lines):
            if i == 0:
                out.append(line)
                continue
            if not keys_done and line.rstrip("\n") == "---":
                out.append(f"{_ANCHOR_KEY} {full_sha}\n")
                out.append(f"{_MACHINE_KEY} {machine}\n")
                keys_done = True
                out.append(line)
                continue
            if not note_done and line.lower().startswith("# daily summary"):
                out.append(line)
                out.append(note)
                note_done = True
                continue
            out.append(line)
        if not keys_done:
            return None
    else:
        # No frontmatter: insert all three lines after the "# Daily Summary" H1.
        done = False
        for line in lines:
            if not done and line.lower().startswith("# daily summary"):
                out.append(line)
                out.append("\n")
                out.append(f"{_ANCHOR_KEY} {full_sha}\n")
                out.append(f"{_MACHINE_KEY} {machine}\n")
                out.append(note)
                done = True
                continue
            out.append(line)
        if not done:
            return None

    return "".join(out)


def _resolve_commit(worktree: Path, rev: str) -> Optional[str]:
    """Resolve `rev` to a full commit SHA via `git rev-parse --verify
    rev^{commit}`, or None if it does not resolve (e.g. a dangling SHA the
    repo no longer has). Byte-parity port of the oracle's rev-parse --verify
    calls used to check whether a recorded anchor still resolves in this repo."""
    result = _wcbs_run_git(["-C", str(worktree), "rev-parse", "--verify", f"{rev}^{{commit}}"])
    if result is None or result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _is_ancestor(worktree: Path, ancestor: str, descendant: str) -> bool:
    """True if `ancestor` is an ancestor of (or equal to) `descendant` via
    `git merge-base --is-ancestor`. Byte-parity port of the oracle's freshness
    check (`wc.git_ok(..., "merge-base", "--is-ancestor", rec_full, full_sha)`).

    Callers that need the *strict*-ancestor bound (DR-216 § D2(iii-b)) must
    first rule out equality themselves — `--is-ancestor` returns success for
    A == B as well as for A strictly preceding B."""
    result = _wcbs_run_git(["-C", str(worktree), "merge-base", "--is-ancestor", ancestor, descendant])
    return result is not None and result.returncode == 0


def _render_anchor_bump(content: str, full_sha: str, machine: str) -> str:
    """Rewrite the first LIVE covered_tip_sha:/covered_machine: lines in
    place, bumping a convergent stale anchor to the new tip (DR-216 §
    D2(iii-b)).

    Fence-aware, like `_recorded_anchor_sha` above: tracks fenced-code-block
    (```) state and skips key-line matches while inside a fence, so a
    documentation example of the anchor format appearing earlier in the same
    file (plausible in a summary that discusses the backfill-anchor mechanism
    itself) is never rewritten in place of the real, live anchor line.

    This is a deliberate DIVERGENCE from the oracle's `_rewrite_anchor`
    (workday-complete-backfill-inject-anchor.py:62-79), which uses a plain
    first-match loop with no fence awareness and is therefore exposed to
    exactly this corruption — the oracle's own detection step has no
    fence-aware counterpart to disagree with, so it never surfaced there.
    Byte-parity with the oracle is NOT the governing bound here: DR-216 §
    D2(iii-b) sanctions rewriting the coverage anchor, not a fenced
    documentation EXAMPLE of one — a fenced `covered_tip_sha:` line is not
    the coverage anchor, so leaving it exposed to rewrite was outside the
    ratified bound, not a byte-parity commitment this fix breaks (see
    Finding 3 of the inject_anchor port review; ruled non-PM-facing because
    the prior unguarded behavior was itself the latent defect, not a
    ratified byte-parity floor). Every other line — including the prose note
    and the summary body — is preserved verbatim; only the first LIVE
    occurrence of each of the two bare key lines is replaced.
    """
    lines = content.splitlines(keepends=True)
    stip = smach = False
    in_fence = False
    out: List[str] = []
    for line in lines:
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if not stip and line.startswith(_ANCHOR_KEY):
            out.append(f"{_ANCHOR_KEY} {full_sha}\n")
            stip = True
            continue
        if not smach and line.startswith(_MACHINE_KEY):
            out.append(f"{_MACHINE_KEY} {machine}\n")
            smach = True
            continue
        out.append(line)
    return "".join(out)


# ---------------------------------------------------------------------------
# Content-gap guards — run BEFORE injection, in the oracle's own order. Any
# guard firing means "do not inject" (mirrors the oracle's exit 30). Guard 1
# is a NATIVE replacement for the oracle's node-based mechanism (see its own
# docstring); guards 2/3 are direct byte-parity ports (pure git + regex).
#
# Fail-CLOSED, not fail-open (deliberate divergence from the oracle — see
# `_completion_count_for_date`'s docstring and `inject_anchor`'s own).
# ---------------------------------------------------------------------------

_MORNING_SIGNAL_RE = re.compile(
    r"morning run|wraps the tail|spilled past midnight", re.IGNORECASE
)


def _completion_scan_error(worktree: Path) -> Optional[str]:
    """Probe archive/completed/ (and its per-batch subdirectories, one level
    deep) for a directory-scan failure that `_collect_type_records`'s own
    collection path would silently swallow.

    `completion`'s glob (`archive/completed/*/*.md`) is one of
    records_query.py's `_WILDCARD_DIR_TYPES` — collection for these types
    routes through `_walk_glob_segments`, not the non-wildcard `_collect_files`
    path. `_walk_glob_segments` is DELIBERATELY fail-open on an unreadable
    directory at any level (see its own docstring: "a wildcard-dir type has no
    established Tier-2 incomplete-signal contract, so an unreadable level is
    skipped"), so it never raises `_RecordsCollectError` — confirmed
    empirically: a chmod-000 `archive/completed/` makes
    `_collect_type_records(worktree, "completion")` return `[]`, not raise.
    Calling `_collect_type_records` directly (bypassing the ceremony-layer
    `query_records` wrapper, which separately swallows
    `_RecordsCollectError` when it IS raised) closes only HALF the fail-open
    gap Finding 1 of the inject_anchor port review named — the non-wildcard
    half. This probe closes the other half, scoped narrowly to the one
    directory `completion` records live under, rather than reimplementing
    `_walk_glob_segments`'s general wildcard-dir walk or changing its
    fail-open contract — that contract is shared substrate for
    `roadmap`/`handoff-archived`/`cutover` too, and giving it a Tier-2 raise
    is a decision for records_query.py's own owners, not a side effect of
    this guard.

    Returns a human-readable reason string on a scan failure, else None
    (including when `archive/completed/` does not exist at all — that is a
    legitimate zero, not a failure). A non-directory at that path IS reported
    as a failure; absent and wrong-type are different states.

    Residual limitation, accepted and named rather than implied-closed: there
    is a TOCTOU window between this probe and the `_collect_type_records` walk
    that follows it. A directory that becomes unreadable in between is still
    swallowed by `_walk_glob_segments`'s fail-open contract, and this guard
    will not catch it. Closing that would mean one shared filesystem walk
    instead of two, which is a change to records_query.py's contract, not to
    this guard. The window is accepted here because inject_anchor runs on a
    single-process, spawn-per-call ceremony path (DR-215), not a concurrent
    surface. Do not read this probe as making the fail-open gap impossible —
    it makes the steady-state case detectable.
    """
    base = worktree / "archive" / "completed"
    if not base.exists():
        # Genuinely absent → a legitimate zero, not a failure.
        return None
    if not base.is_dir():
        # A plain file (or anything non-directory) sitting where the completion
        # corpus belongs is an anomalous filesystem state, NOT a legitimate
        # zero. `Path.is_dir()` alone cannot tell the two apart — it returns
        # False for both — which is the blind spot `_walk_glob_segments` has and
        # the reason this probe exists. Treat it as a scan error.
        return f"expected a directory at {base}, found a non-directory"
    try:
        entries = os.listdir(base)
    except OSError as exc:
        return f"cannot list directory {base}: {exc}"
    for name in entries:
        sub = base / name
        if sub.is_dir():
            try:
                os.listdir(sub)
            except OSError as exc:
                return f"cannot list directory {sub}: {exc}"
    return None


def _completion_count_for_date(worktree: Path, date: str) -> int:
    """Return the count of archive/completed/ completion records for `date`.

    NATIVE replacement for the oracle's `_completion_count` (workday-complete-
    backfill-inject-anchor.py): the oracle shells to a bash-lib bridge to
    resolve CLAUDE_KLABAUTER_ROOT, then `command -v node`-gates a call to
    `query-completions.py` — that whole shape is retired here (naked-Python
    mandate; the query/read layer went fully native 2026-07-22, see this
    repo's CLAUDE.md § Runtime conventions).

    Calls `_completion_scan_error` FIRST (see its own docstring for why —
    `completion`'s wildcard-dir collection path is fail-open at a layer this
    function alone can't see past), then
    `coordinator_core.ops.records_query._collect_type_records` directly (the
    same lower-level entry point the `--unattached` union lens uses) rather
    than going through `coordinator_core.ops.ceremony.records_query.
    query_records` — that ceremony-layer helper deliberately catches
    `_RecordsCollectError` and returns `[]`, which reads identically to a
    legitimate zero-completions date (this was a real bug, not a
    hypothetical — see Finding 1 of the inject_anchor port review). The
    `where=` filter is applied locally afterward via the same
    `_parse_where`/`_matches_where` grammar `query_records` uses internally.

    Fail-CLOSED (deliberate divergence from the oracle, which fails OPEN — it
    prints a WARN and returns 0, letting the caller inject anyway, when the
    count can't be resolved via node/query-completions.py). Raises
    RuntimeError on any resolution failure — directory-scan failure (at
    either layer), bad `where` grammar, or an OS-level error — so
    `inject_anchor` refuses to inject on an unresolved guard rather than
    silently anchoring a possible content gap.
    """
    scan_error = _completion_scan_error(worktree)
    if scan_error is not None:
        raise RuntimeError(
            f"inject_anchor: completion-count guard unresolved for {date}: {scan_error}"
        )
    try:
        records = _collect_type_records(worktree, "completion")
        clauses = _parse_where(f"created={date}")
        records = [r for r in records if _matches_where(r["frontmatter"], clauses)]
    except (_RecordsCollectError, ValueError, SystemExit, OSError) as exc:
        raise RuntimeError(
            f"inject_anchor: completion-count guard unresolved for {date}: {exc}"
        ) from exc
    return len(records)


def _bullet_count(content: str) -> int:
    """Count bullet/H3 lines under a '## Work Completed' heading in `content`.

    Byte-parity port of the oracle's `_bullet_count`: counts lines matching
    `^[-*]\\s` or `^###\\s` between a `## Work Completed` heading and the next
    `##` heading (or EOF).
    """
    count = 0
    in_wc = False
    for raw in content.splitlines():
        line = raw.rstrip("\n")
        if re.match(r"^##\s", line):
            in_wc = "Work Completed" in line
            continue
        if in_wc and (re.match(r"^[-*]\s", line) or re.match(r"^###\s", line)):
            count += 1
    return count


def _range_commit_shas(worktree: Path, date: str, full_sha: str) -> List[str]:
    """Full SHAs reachable from `full_sha`, committed within `date`'s day window.

    Byte-parity port of the oracle's commit-density range query:
    `git log <full_sha> --no-merges --since='{date} 00:00:00'
    --until='{date} 23:59:59' --format=%H`.
    """
    return _git_lines_at(
        worktree,
        [
            "log",
            full_sha,
            "--no-merges",
            f"--since={date} 00:00:00",
            f"--until={date} 23:59:59",
            "--format=%H",
        ],
    )


def _cited_in_range_count(worktree: Path, body: str, range_shas: List[str]) -> int:
    """Count how many of `range_shas` are cited (as resolvable commit-ish
    tokens) in `body`. Byte-parity port of the oracle's cited-SHA scan."""
    range_set = set(range_shas)
    tokens = {t.lower() for t in re.findall(r"\b[0-9a-fA-F]{7,40}\b", body)}
    cited: set = set()
    for tok in tokens:
        resolved = _git_lines_at(worktree, ["rev-parse", "--verify", "-q", f"{tok}^{{commit}}"])
        if not resolved:
            continue
        full = resolved[0]
        if full in range_set:
            cited.add(full)
    return len(cited)


def _content_gap_reason(worktree: Path, date: str, full_sha: str, content: str) -> Optional[str]:
    """Return a human-readable content-gap reason if any pre-injection guard
    fires, else None. Runs the oracle's three guards in the oracle's own order.

    Fail-CLOSED: `_completion_count_for_date` raises RuntimeError (propagated,
    not swallowed) when guard 1's inputs can't be resolved — the caller must
    refuse to inject rather than silently anchoring a possible content gap
    (see that function's docstring for why this diverges from the oracle).
    """
    completion_count = _completion_count_for_date(worktree, date)  # may raise RuntimeError
    bullet_count = _bullet_count(content)
    if completion_count >= 3 and completion_count >= bullet_count * 2:
        return (
            f"{completion_count} completion entries vs {bullet_count} Work Completed "
            "bullets"
        )

    range_shas = _range_commit_shas(worktree, date, full_sha)
    range_count = len(range_shas)
    cited_count = _cited_in_range_count(worktree, content, range_shas)
    if range_count >= 3 and cited_count >= 1 and (cited_count * 2) < range_count:
        return (
            f"summary cites {cited_count} in-range commit SHAs vs {range_count} commits "
            f"in the {date} range (<50%)"
        )
    if _MORNING_SIGNAL_RE.search(content) and range_count >= 10:
        return f"morning-run/tail-wrap note anchored to a {range_count}-commit range"

    return None


def inject_anchor(
    *,
    worktree: Path,
    date: str,
    machine: str,
    full_sha: str,
    today: Optional[str] = None,
) -> dict:
    """Inject a covered_tip_sha:/covered_machine: anchor into a pre-existing
    archive/daily-summaries/ daily summary, or bump a convergent stale one.

    Byte-parity port of the oracle's injection path AND its bump path
    (DR-216 § D2(iii-b), PM-ratified 2026-07-28 — see the module-level comment
    above this op's section). Ordering matches the oracle exactly: the
    freshness/bump decision is made BEFORE the target file's presence is
    otherwise interrogated further — content-gap guards run ONLY when no live
    anchor is recorded at all (mirrors the oracle's `_rewrite_anchor` call
    sites at :284-300 preceding its content-gap guards at :302+, i.e. a bump
    is never content-gated). A missing target file short-circuits to
    `summary_absent` (mirrors the oracle's exit 20 — real content gap, route
    to a Phase A analyst).

    Does NOT git-commit — caller/EM retains commit responsibility (DR-216 D2(v)).

    Params:
        worktree: main worktree root (main_worktree_root(repo_root)).
        date, machine: identify the target file
            archive/daily-summaries/{date}-{machine}.md (or its glob/legacy
            fallback — see `_resolve_daily_summary_target`).
        full_sha: the commit SHA to record as covered_tip_sha. Resolved via
            `_resolve_commit` ONCE at the top of this function (mirrors the
            oracle's own entry-point `rev-parse --verify` gate,
            workday-complete-backfill-inject-anchor.py:249-252) and the
            resolved canonical form is used for every downstream comparison
            AND every write — an unresolvable value is a hard `error`, never
            written to `covered_tip_sha:` on trust. (Historical: this op
            previously trusted `full_sha` as caller-verified and never
            resolved it; that let a garbage/unresolvable value reach
            `covered_tip_sha:` on the bump path when the recorded anchor was
            itself unresolvable, and let an abbreviated-but-valid `full_sha`
            naming the already-anchored commit bump anyway, since only the
            `recorded` side was canonicalized — see Finding 2 of the
            inject_anchor port review. Fixed here.)
        today: override for the prose note's date; defaults to today UTC.

    Returns:
        {out_path: str|None, action: "injected" | "bumped" |
         "already_anchored" | "summary_absent" | "content_gap" | "error"}
        - "bumped": a recorded anchor that was a strict ancestor of `full_sha`
          (or unresolvable) was rewritten in place to `full_sha` (D2(iii-b)).
        - "already_anchored": no write occurred — either the recorded anchor
          already equals `full_sha` (fresh), or it is a descendant of or
          divergent from `full_sha` (never bumped backwards or across a fork).
        - "content_gap": one of the three pre-injection guards fired (mirrors
          the oracle's exit 30); `error` carries the human-readable reason;
          file left untouched. Only reachable when no anchor is recorded.
        - "error": a malformed target file (no frontmatter close / no
          "# Daily Summary" H1 — mirrors the oracle's exit 1), OR the
          completion-count guard could not be resolved (fail-CLOSED — see
          `_completion_count_for_date`'s docstring; this is a deliberate
          divergence from the oracle, which fails open on that same
          condition), OR `full_sha` does not resolve to a commit in
          `worktree` (mirrors the oracle's exit 1 on an unresolvable
          `DESCENDANT_TIP_SHA`). Either way `error` carries the reason and
          out_path is still populated (where a target was found) so the
          caller can inspect it.

    Review: code-reviewer (F2 precedent, see `append_day`'s own docstring) —
    this pure function trusts its arguments; `date`/`machine` shape validation
    lives only in `_inject_anchor_handler`.
    """
    resolved_today = today or _today_utc()
    resolved_full_sha = _resolve_commit(worktree, full_sha)
    if resolved_full_sha is None:
        return {
            "out_path": None,
            "action": "error",
            "error": f"inject_anchor: 'full_sha' does not resolve to a commit in {worktree}: {full_sha!r}",
        }
    full_sha = resolved_full_sha

    target = _resolve_daily_summary_target(worktree, date, machine)
    if target is None:
        return {"out_path": None, "action": "summary_absent"}

    content = target.read_text(encoding="utf-8", errors="replace")
    recorded = _recorded_anchor_sha(content)
    if recorded is not None:
        if recorded == full_sha:
            return {"out_path": str(target), "action": "already_anchored"}
        resolved_recorded = _resolve_commit(worktree, recorded)
        # Equality must be ruled out AFTER resolution, not only on the raw string.
        # An abbreviated recorded anchor naming the same commit as full_sha fails
        # the raw comparison above, and `--is-ancestor` returns success for an
        # equal commit — so without this check it would fall into the bump branch
        # and rewrite an anchor that already points at the target tip. DR-216
        # § D2(iii-b) forbids rewriting an equal anchor. This mirrors the oracle,
        # which compares only after `git rev-parse` (`rec_full == full_sha`).
        # `full_sha` is already canonical (resolved above), so this comparison
        # is symmetric — an abbreviated recorded anchor no longer escapes it.
        if resolved_recorded is not None and resolved_recorded == full_sha:
            return {"out_path": str(target), "action": "already_anchored"}
        if resolved_recorded is None or _is_ancestor(worktree, resolved_recorded, full_sha):
            new_content = _render_anchor_bump(content, full_sha, machine)
            _atomic_write(target, new_content)
            return {"out_path": str(target), "action": "bumped"}
        # Recorded anchor resolves and is neither equal nor an ancestor of the
        # target tip — it is a descendant or a fork (divergent). Never bump
        # backwards or across a fork (DR-216 § D2(iii-b)).
        return {"out_path": str(target), "action": "already_anchored"}

    try:
        gap_reason = _content_gap_reason(worktree, date, full_sha, content)
    except RuntimeError as exc:
        return {"out_path": str(target), "action": "error", "error": str(exc)}
    if gap_reason is not None:
        return {"out_path": str(target), "action": "content_gap", "error": gap_reason}

    new_content = _render_anchor_injection(content, full_sha, machine, resolved_today)
    if new_content is None:
        return {
            "out_path": str(target),
            "action": "error",
            "error": (
                "inject_anchor: malformed summary structure (unclosed frontmatter or "
                "no '# Daily Summary' H1); anchor not injected"
            ),
        }

    _atomic_write(target, new_content)
    return {"out_path": str(target), "action": "injected"}


@register_op("changelog.inject_anchor")
async def _inject_anchor_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC changelog.inject_anchor handler.

    MUTATING (writes archive/daily-summaries/<date>-<machine>.md — both the
    injection path and, as of the 2026-07-28 PM ratification, the bounded
    coverage-anchor bump path; see the module-level comment above this op's
    section and DR-216 § D2(iii-b)). DOES NOT git-commit (DR-216 D2(v)):
    caller/EM retains commit responsibility.

    Required params:
        date (str, YYYY-MM-DD) — the daily summary's date.
        machine (str) — the daily summary's machine name. MUST be the
            summary's own machine (preserve legacy attribution — do NOT
            normalize to the engine's own cwd/hostname).
        full_sha (str) — the resolved commit SHA to record as covered_tip_sha.

    Optional params:
        today (str, YYYY-MM-DD) — override for the prose note's date; defaults
            to today UTC.

    Returns:
        {out_path: str|None, action: "injected" | "bumped" |
         "already_anchored" | "summary_absent" | "content_gap" | "error"}
        (or {"error": str, "action": "error"} on param validation failure)
    """
    if repo_root is None:
        return {"error": "changelog.inject_anchor: repo_root required", "action": "error"}
    worktree = main_worktree_root(repo_root)

    date = params.get("date", "")
    # Containment (mirrors changelog.append_day's AC13 date guard): date is
    # interpolated raw into the write-target filename
    # "archive/daily-summaries/{date}-{machine}.md" — reject traversal/non-ISO
    # input before it reaches the filesystem.
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {
            "error": f"changelog.inject_anchor: 'date' param is not a valid YYYY-MM-DD date: {date!r}",
            "action": "error",
        }
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        print(f"skip: _inject_anchor_handler: datetime.date.fromisoformat(date) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {
            "error": f"changelog.inject_anchor: 'date' param is not a valid YYYY-MM-DD date: {date!r}",
            "action": "error",
        }

    machine = params.get("machine", "").strip()
    if not machine:
        return {"error": "changelog.inject_anchor: 'machine' param is required", "action": "error"}
    # Containment (mirrors the op-family path-containment sweep, 2026-07-08):
    # machine is interpolated raw into the same write-target filename.
    if not safe_id(machine):
        return {
            "error": f"changelog.inject_anchor: 'machine' param is not a safe filename segment: {machine!r}",
            "action": "error",
        }

    full_sha = str(params.get("full_sha", "")).strip()
    if not full_sha:
        return {"error": "changelog.inject_anchor: 'full_sha' param is required", "action": "error"}
    # Containment (git-flag-injection guard, mirrors changelog.compute_day_fields'
    # commit_span guard): full_sha reaches `git log <full_sha> …` as a bare
    # positional revision argument (the commit-density content-gap guard) with
    # no `--` separator ahead of it — a leading-dash value would be parsed by
    # git as a flag, not a revision.
    if full_sha.startswith("-"):
        return {
            "error": f"changelog.inject_anchor: 'full_sha' must not start with '-' (git-flag injection guard; got {full_sha!r})",
            "action": "error",
        }

    today = params.get("today")
    if today is not None:
        today = str(today)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
            return {
                "error": f"changelog.inject_anchor: 'today' param is not a valid YYYY-MM-DD date: {today!r}",
                "action": "error",
            }

    return await asyncio.to_thread(
        inject_anchor,
        worktree=worktree,
        date=date,
        machine=machine,
        full_sha=full_sha,
        today=today,
    )


# ---------------------------------------------------------------------------
# HEADER staleness (report-only — caller decides skip-vs-proceed)
# ---------------------------------------------------------------------------


def _header_staleness(worktree: Path, date: str) -> dict:
    """Return {stale, days_ago, week_start} for state/week-changelog/HEADER.md.

    Mirrors the oracle's WEEK_START/DAYS_AGO computation (>14 days -> stale).
    Absent HEADER.md or unparseable "Week starting:" -> not stale (nothing to
    compare against — matches the oracle's `[[ -f HEADER_FILE ]]` guard).
    """
    header_file = worktree / "state" / "week-changelog" / "HEADER.md"
    if not header_file.is_file():
        return {"stale": False, "days_ago": None, "week_start": None}
    try:
        text = header_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"skip: _header_staleness: text = header_file.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {"stale": False, "days_ago": None, "week_start": None}

    week_start = None
    for line in text.splitlines():
        if line.startswith("Week starting:"):
            week_start = line[len("Week starting:"):].strip()
            break
    if not week_start:
        return {"stale": False, "days_ago": None, "week_start": None}

    try:
        today = datetime.date.fromisoformat(date)
        start = datetime.date.fromisoformat(week_start)
    except ValueError:
        print(f"skip: _header_staleness: today = datetime.date.fromisoformat(date) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {"stale": False, "days_ago": None, "week_start": week_start}

    days_ago = (today - start).days
    return {"stale": days_ago > 14, "days_ago": days_ago, "week_start": week_start}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def compute_day_fields(
    *,
    worktree: Path,
    date: str,
    commit_span: Optional[str] = None,
    local_today: Optional[str] = None,
) -> dict:
    """Compute the full field bundle changelog.append_day's params consume.

    COMPUTE_ONLY (no write). `date` is the day being wrapped (may be a past
    day under --for-date backfill); `local_today` is the actual local day at
    computation time (defaults to `date` when omitted, matching the oracle's
    `LOCAL_TODAY` default when no --for-date override applies).

    Review: code-reviewer (F2) — this pure function trusts its arguments.
    `date`/`local_today`/`commit_span` shape validation (ISO-date regex,
    commit_span 2-dot-range + git-flag-injection containment) lives only in
    `_compute_day_fields_handler`; a caller reaching this function directly
    (bypassing the JSON-RPC handler) does NOT get those guards for free.
    """
    resolved_local_today = local_today or date
    is_backfill = date != resolved_local_today

    commits = _collect_commits(worktree, date, commit_span=commit_span)
    hashes = [c[0] for c in commits]
    subjects = [c[1] for c in commits]
    oldest_sha, newest_sha, commit_range = _commit_range(hashes)
    has_non_trivial = any(not _TRIVIAL_PATTERN.search(s) for s in subjects)

    plans_touched = _plans_touched(worktree, date, commit_span=commit_span)
    handoffs_list, handoff_paths = _handoffs_for_date(worktree, date)
    decisions = extract_field_from_handoffs("Decisions", handoff_paths)
    blockers = extract_field_from_handoffs("Blockers", handoff_paths)
    reviewed_lines = _reviewed_lines_for_date(worktree, date)
    staleness = _header_staleness(worktree, resolved_local_today)

    return {
        "date": date,
        "commit_count": len(commits),
        "commit_range": commit_range,
        "oldest_sha": oldest_sha,
        "newest_sha": newest_sha,
        "has_non_trivial": has_non_trivial,
        "plans_touched": plans_touched,
        "handoffs_list": handoffs_list,
        "decisions": decisions,
        "blockers": blockers,
        "reviewed_lines": reviewed_lines,
        "is_backfill": is_backfill,
        "local_today": resolved_local_today,
        "header_stale": staleness["stale"],
        "header_stale_days": staleness["days_ago"],
        "header_week_start": staleness["week_start"],
    }


@register_op("changelog.compute_day_fields")
async def _compute_day_fields_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC changelog.compute_day_fields handler.

    COMPUTE_ONLY — read-only sibling of changelog.append_day. Returns the
    field bundle append_day's params expect (commit_count, commit_range,
    plans_touched, decisions, blockers, reviewed_lines, has_non_trivial,
    …) so a caller no longer hand-computes them in bash.

    Required params:
        (none — `date` defaults to today UTC when omitted)

    Optional params:
        date (str, YYYY-MM-DD) — day being wrapped; defaults to today UTC.
        commit_span (str, "<BASE>..<TIP>") — machine-scoped span (C3) that
            replaces the derived date window for both commit collection and
            plans-touched (self-commit exclusion applied identically).
        local_today (str, YYYY-MM-DD) — actual local day at computation time;
            defaults to `date`. Drives `is_backfill` and the HEADER staleness
            check (which always compares against the CURRENT day, not a
            possibly-past `date`).

    Returns:
        {date, commit_count, commit_range, oldest_sha, newest_sha,
         has_non_trivial, plans_touched, handoffs_list, decisions, blockers,
         reviewed_lines, is_backfill, local_today, header_stale,
         header_stale_days, header_week_start}
    """
    if repo_root is None:
        return {"error": "changelog.compute_day_fields: repo_root required"}
    worktree = main_worktree_root(repo_root)

    date = params.get("date") or _today_utc()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"error": f"changelog.compute_day_fields: 'date' param is not a valid YYYY-MM-DD date: {date!r}"}
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        print(f"skip: _compute_day_fields_handler: datetime.date.fromisoformat(date) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {"error": f"changelog.compute_day_fields: 'date' param is not a valid YYYY-MM-DD date: {date!r}"}

    commit_span = params.get("commit_span") or None
    if commit_span is not None:
        commit_span = str(commit_span)
        if "..." in commit_span or ".." not in commit_span:
            return {
                "error": (
                    "changelog.compute_day_fields: 'commit_span' requires a "
                    f"<BASE>..<TIP> argument (2-dot range only; got {commit_span!r})"
                )
            }
        # Review: code-reviewer (F1) — Containment: commit_span reaches `git log`
        # as a bare positional argument with no `--` separator ahead of it
        # (_collect_commits/_plans_touched). A leading-dash BASE or TIP
        # (e.g. "--output=/tmp/pwned..x") is otherwise indistinguishable from a
        # valid 2-dot range and would be parsed by git as a flag, not a
        # revision — classic git-argument injection. Reject at parse time
        # (matching this file's date/host/machine containment posture) rather
        # than relying on a trailing `--`, which does not protect a
        # flag-shaped revision argument the way it protects pathspecs.
        if any(part.startswith("-") for part in commit_span.split("..")):
            return {
                "error": (
                    "changelog.compute_day_fields: 'commit_span' components must "
                    f"not start with '-' (git-flag injection guard; got {commit_span!r})"
                )
            }

    local_today = params.get("local_today")
    if local_today is not None:
        local_today = str(local_today)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", local_today):
            return {
                "error": (
                    "changelog.compute_day_fields: 'local_today' param is not a "
                    f"valid YYYY-MM-DD date: {local_today!r}"
                )
            }

    return await asyncio.to_thread(
        compute_day_fields,
        worktree=worktree,
        date=date,
        commit_span=commit_span,
        local_today=local_today,
    )


# ===========================================================================
# CLI entrypoint — direct-import trampoline target
# ===========================================================================


def main(argv: List[str]) -> int:
    """CLI entrypoint for the `backfill-week-changelog-gaps.sh` polyglot trampoline.

    Port of: backfill-week-changelog-gaps.sh (DoE b5a4192c, 2026-07-20). The
    cc_invoke/JSON-RPC veneer (T2-g1 strangler-facade) is retired on this
    cutover — direct in-process call replaces the subprocess round trip,
    matching the coordinator-auto-push / handoff-gate-aging direct-import
    trampoline pattern (no benefit to a second subprocess hop here: the op
    handler is a thin `asyncio.to_thread` wrapper around the same
    `backfill_gaps()` this calls directly).

    Usage (unchanged surface — zero caller repoints):
        backfill-week-changelog-gaps.sh [repo-root]
        NOTE: the optional [repo-root] positional is accepted but IGNORED —
        matches the legacy/facade contract exactly (repo root is always
        resolved from $PWD via git, never from argv).

    Emits the bare result dict as JSON to stdout — same shape the retired
    cc_invoke veneer's success envelope produced (`{backfilled: [...],
    skipped: [...]}`), so any downstream stdout-scraping caller sees an
    unchanged contract.

    Exit codes:
      0 — success or advisory-error. `backfill_gaps()` never raises for
          expected failure modes (missing HEADER.md, unparseable "Week
          starting:") — it returns a `message` key instead — mirroring the
          legacy `trap 'exit 0' ERR` advisory contract this whole op family
          preserves (DR-216 D2). Any other exception is also caught here and
          reported advisory (matches `_backfill_gaps_handler`'s own catch-all).
      1 — cannot resolve git repo root from $PWD (not a git repo). Matches the
          facade's pre-existing Check at this same failure point.

    Host resolution note (byte-parity target RETIRED 2026-08-11, see
    cross-repo/inbox/2026-08-11-example-retrieval-repo-em-backfill-changelog-cli-three-
    defects.md item 2): the retired facade computed `host` via
    `hostname -s || hostname` UNCONDITIONALLY, and this CLI used to reproduce
    that exactly — `_get_hostname()` unconditionally, `COORDINATOR_MACHINE`
    NOT consulted. That parity was itself the defect: `_get_hostname()`
    returns the raw OS hostname (e.g. "Machine-a"), while every other artifact
    in the daily ceremony (`archive/daily-summaries/<day>-<machine>.md`, the
    `<day>.md` block `changelog.append_day` writes) uses the lowercase
    machine slug (`coordinator_core.machine_resolver.compute_machine()`,
    e.g. "machine-a"). On a case-sensitive filesystem the mismatch reads as a
    second machine. This CLI now resolves `host` via `compute_machine()` —
    same slug the rest of the ceremony uses, which already honours
    `COORDINATOR_MACHINE` as its own first-priority override.

    Repo-root resolution note: `backfill_gaps()` takes the git COMMON_DIR
    (mirrors `_OP_KEY_SCOPE["changelog.backfill_gaps"] == "common_dir"` — the
    JSON-RPC engine supplies the handler with common_dir, not the worktree
    root; `main_worktree_root()` derives the worktree via `common_dir.parent`
    internally). This CLI resolves `--git-common-dir` (NOT `--show-toplevel`)
    for that reason — using the worktree root here would make
    `main_worktree_root()` walk one directory too high.
    """
    if "-h" in argv or "--help" in argv:
        print(main.__doc__)
        return 0
    dry_run = "--dry-run" in argv
    # every other positional (including the legacy [repo-root]) stays
    # accepted-but-ignored, per legacy/facade contract
    cwd = os.getcwd()
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        print(
            f"backfill-week-changelog-gaps.sh: cannot resolve git repo root from {cwd}",
            file=sys.stderr,
        )
        return 1
    # --git-common-dir may print a path relative to cwd (e.g. ".git") — resolve
    # against cwd to get an absolute common_dir before handing to backfill_gaps().
    common_dir = (Path(cwd) / proc.stdout.strip()).resolve()

    # Machine slug, not raw hostname — see the Host resolution note above.
    host = compute_machine()

    try:
        result = backfill_gaps(repo_root=common_dir, host=host, dry_run=dry_run)
    except Exception as exc:  # advisory — never fail the caller (DR-216 D2 legacy parity)
        logger.warning("backfill_gaps CLI: advisory error (non-fatal): %s", exc)
        result = {"backfilled": [], "skipped": [], "error": str(exc)}

    # Item 1: name the files this op writes (or, under --dry-run, would
    # write) on stderr — silent writes were half of why --help ran the
    # backfill unnoticed.
    for path in result.get("backfilled", []):
        verb = "would write" if dry_run else "wrote"
        print(f"backfill-week-changelog-gaps: {verb} {path}", file=sys.stderr)

    print(json.dumps(result))
    return 0

