"""
daily_branch.py — the pure branch-shape / slug-sanitization core.

Port of: coordinator-daily-branch.sh (DoE 2fbe0e77, 2026-07-19)
Spec backlink: docs/plans/2026-05-07-daily-branch-doctrine-rethink.md (Phase 1)
De-bash campaign: kill bash on the Windows critical path — this is the Wave-2
"daily-branch" native op (import-only module, mirror-shape of daily_day.py).

Purpose: the daily-branch discipline oracles. These police branch *shape*, not
branch *date* — there is no notion of "today's daily" here, only "is this branch
an allowed workstream branch?" The canonical policy oracle is is_allowed_branch.

Scope note (why this port is pure string/regex): the DoE original also carried
cs_compute_machine / cs_compute_contributor resolvers, which shell out to the
machine-local registry, hostname, and `git config`. Those are external-primitive
resolvers, NOT branch-shape logic, and migrate as a separate machine-local
resolver unit — they are intentionally NOT ported here. This module is the pure
parsing/construction half:
  cs_sanitize_slug      -> sanitize_slug
  cs_parse_branch_span  -> parse_branch_span
  cs_is_allowed_branch  -> is_allowed_branch
  cs_is_canonical_branch-> is_canonical_branch
  cs_format_span_suffix -> format_span_suffix
  cs_rename_target      -> rename_target
  cs_should_prompt_rename-> should_prompt_rename

INTENTIONALLY ABSENT (do not re-add):
  cs_compute_active_daily_lc / cs_compute_today_daily_lc — redundant,
  concurrency-hostile "today" oracles. Policy oracle is is_allowed_branch.

Ceremony/oracle incoherence on -N collision suffixes (deliberate, not a bug):
ceremony-side branch creation (coordinator/lib/session_ensure_branch.py and
friends) mints work/{machine}/{today}-2 .. -9 collision suffixes when the
bare today-branch name is already taken. Those -N names are NOT accepted by
is_canonical_branch (or parse_branch_span) and never will be without a
deliberate widen. This is not a bug on either side: ceremony-side branch
creation runs via in-process subprocess.run with argv lists, never through
the PreToolUse Bash seam, so the branch-creation deny guard structurally
cannot see or reject a -N suffix — verified against HEAD, all nine
ceremony-side creation/rename sites in the repo are in-process with no
shell-visible path. Because the guard never judges these names, the oracle
is not required to accept a shape it never has to judge at that seam.
Widening _BRANCH_SHAPE_RE / parse_branch_span to accept -N was evaluated and
rejected: parse_branch_span has four live tool-time callers (workday-start-
step0.py Checks 2/3.5/4, workday-start-day-branch-resolve.py's
_span_assert, workday-complete-step3-consolidate.py's _parse_branch_span,
and session_ensure_branch.py), and widening would move Step 0's Check 3.5
named-workstream precedence boundary for -N branches — a real ceremony-path
behavior change with no guard-side benefit, since the guard never sees
these names. Recorded here so a future guard-plan pass does not re-propose
widening the shape regex without re-deriving this cost.
"""

from __future__ import annotations

import re
import time
from typing import Optional, Tuple

# work/<machine-segment>/<YYYY-MM-DD>[to<DD>] — the accepted daily/span shape.
# Mirrors the bash grep -E pattern verbatim.
_BRANCH_SHAPE_RE = re.compile(r"^work/[^/]+/[0-9]{4}-[0-9]{2}-[0-9]{2}(to[0-9]{2})?$")
_SPAN_SUFFIX_RE = re.compile(r"to[0-9]{2}$")
_NON_SLUG_RE = re.compile(r"[^a-z0-9-]")

_HOURS_48_SECONDS = 48 * 3600


def sanitize_slug(raw: str) -> str:
    """Port of cs_sanitize_slug (the _memo_filename idiom, cross-repo-memo:868-870).

    lowercase -> collapse non-[a-z0-9-] runs to single dash -> collapse dash runs
    -> strip leading/trailing dashes. Lowercases internally — callers do not need
    to pre-lowercase their input.
    """
    s = (raw or "").lower()
    s = _NON_SLUG_RE.sub("-", s)          # non-charset -> dash
    s = re.sub(r"-+", "-", s)             # collapse dash runs
    s = s.strip("-")                      # strip leading/trailing dashes
    return s


def parse_branch_span(name: str) -> Optional[Tuple[str, str]]:
    """Port of cs_parse_branch_span.

    Given work/{machine}/{date-or-span}, return (start_date, end_date) as
    YYYY-MM-DD strings. end_date == start_date for single-day branches. Returns
    None (bash: return 1) if not parseable as a daily/span branch.

    Accepted formats:
      work/<machine>/YYYY-MM-DD       -> (YYYY-MM-DD, YYYY-MM-DD)
      work/<machine>/YYYY-MM-DDtoDD   -> (YYYY-MM-DD, end) — month/year roll if end-DD < start-DD

    Month/year boundary rule (the Staff Engineer F4, option b): if end-DD < start-DD, advance
    month by 1 (advance year by 1 on Dec->Jan). Advances at most one month —
    spans >~28 days route through the A/B/C reconciliation flow, not this parser.

    Negative spec: rejects feature/foo, work/machine-a/feature-X, or invalid date
    components (month > 12, day > 31).
    """
    if name is None:
        return None
    lc = name.lower()

    if not _BRANCH_SHAPE_RE.match(lc):
        return None

    # Date portion is everything after the second '/'.
    date_part = re.sub(r"^work/[^/]*/", "", lc)

    start_year = date_part[0:4]
    start_month = date_part[5:7]
    start_day = date_part[8:10]

    # Validate start-date components (base-10; regex guarantees digits).
    sm = int(start_month)
    sd = int(start_day)
    if sm < 1 or sm > 12:
        return None
    if sd < 1 or sd > 31:
        return None

    start_date = f"{start_year}-{start_month}-{start_day}"

    if _SPAN_SUFFIX_RE.search(date_part):
        end_dd = re.sub(r".*to", "", date_part)
        end_day = end_dd

        if int(end_day) < sd:
            # Roll month forward.
            if sm == 12:
                # December -> January (year roll).
                end_year = str(int(start_year) + 1)
                end_month = "01"
            else:
                end_year = start_year
                end_month = f"{sm + 1:02d}"
        else:
            # Same month, end_DD >= start_DD.
            end_year = start_year
            end_month = start_month

        end_date = f"{end_year}-{end_month}-{end_day}"
        return (start_date, end_date)

    return (start_date, start_date)


def is_allowed_branch(name: str) -> bool:
    """Port of cs_is_allowed_branch — the SHAPE oracle (case-insensitive).

    True iff <name> is main, or any work/{machine}/{date-or-span} that parses via
    parse_branch_span. Case-insensitive by design so remediation paths (rename
    prompts, migration scripts) can recognise mixed-case input as "valid shape,
    wrong case" and fix it. For creation-time canonical-case enforcement use
    is_canonical_branch.

    Negative spec: rejects feature/foo, work/machine-a/feature-X, hotfix/*, etc.
    """
    if name is None:
        return False
    lc = name.lower()
    if lc == "main":
        return True
    return parse_branch_span(lc) is not None


def is_canonical_branch(name: str) -> bool:
    """Port of cs_is_canonical_branch — the CREATION oracle.

    True iff <name> is allowed AND already in canonical (lowercase) form. Distinct
    from is_allowed_branch because remediation paths need to recognise mixed-case
    input in order to fix it; this oracle rejects mixed-case so the hook can block
    its creation in the first place (Windows case-insensitive-FS ref hazard).
    """
    if name is None:
        return False
    if not is_allowed_branch(name):
        return False
    return name == name.lower()


def format_span_suffix(start_date: str, today: str) -> str:
    """Port of cs_format_span_suffix.

    Emit the full branch date portion: just <start_date> when start == today, or
    <start_date>to<end-DD> when start != today. Both arguments must be YYYY-MM-DD.
    """
    if start_date == today:
        return start_date
    end_dd = today.rsplit("-", 1)[-1]
    return f"{start_date}to{end_dd}"


def rename_target(machine: str, start_date: str, today: str, commits_ahead) -> str:
    """Port of cs_rename_target — midnight-rename target chooser (Check 4).

    When commits_ahead == 0 the branch history has all merged (or main moved ahead)
    — a span name would misleadingly advertise multi-day WIP that no longer exists,
    so the target is today-only work/{machine}/{today}. When commits_ahead > 0 the
    span is honest and carries the genuine WIP forward.

    Detect-then-fail-loud on non-integer input (the bash `[[ "" -eq 0 ]]` footgun):
    raises ValueError rather than silently selecting the 0-ahead path.

    Negative spec: does NOT touch git — the caller supplies commits_ahead so this
    is unit-testable without a repo.
    """
    if not re.fullmatch(r"[0-9]+", str(commits_ahead)):
        raise ValueError(
            f"rename_target: commits_ahead must be a non-negative integer, got '{commits_ahead}'"
        )
    if int(commits_ahead) == 0:
        return f"work/{machine}/{today}"
    return f"work/{machine}/{format_span_suffix(start_date, today)}"


def should_prompt_rename(
    branch: str,
    today: str,
    last_commit_epoch: float,
    now_epoch: Optional[float] = None,
) -> bool:
    """Port of cs_should_prompt_rename.

    True (should prompt for rename) when the last commit was within 48h (active
    work) AND the branch end-suffix does NOT already equal today. False (no-op)
    when the branch already covers today, or the last commit is older than 48h
    (stale — routes to A/B/C triage, not a rename prompt).

    now_epoch defaults to time.time() (the bash `date +%s`); it is injectable so
    this stays unit-testable without wall-clock coupling.

    Negative spec: does NOT compute git state — callers pass the epoch.
    """
    span = parse_branch_span(branch)
    if span is None:
        return False
    _start, end_date = span

    if end_date == today:
        return False  # Already covers today — no prompt needed.

    now = time.time() if now_epoch is None else now_epoch
    age_seconds = now - last_commit_epoch
    if age_seconds > _HOURS_48_SECONDS:
        return False  # Stale — A/B/C flow handles it.

    return True
