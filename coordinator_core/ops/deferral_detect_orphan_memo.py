"""
coordinator_core.ops.deferral_detect_orphan_memo — JSON-RPC
"deferral.detect_orphan_memo" operation.

Purpose: a read-only hidden-deferral detector — scans THIS repo's
`cross-repo/inbox/` for actioned-in-theory-but-actually-forgotten memos: an
`ask`/`proposal` memo left `status: open` past an age threshold with NO
owning artifact (plan, handoff, or decision) referencing it. Detector 2 of
the hidden-deferral-detectors pair (Detector 1 is
`deferral.detect_partial_strangle`).

Offers-not-nags contract (same silent-clean / lead-with-offer shape as
engine_drift.py's three-state contract):
  - "clean"          — no orphaned-aging memo found. Silent; no offer/findings.
  - "orphans_found"  — one or more inbox memos are actionable, aging, and
                        UNOWNED (no plan/handoff/DR references them). Carries
                        a `findings` list (one dict per orphan) plus a
                        top-level `offer` string summarizing all of them —
                        lead with the concrete fix (route to /plan, reply,
                        or archive if already done), never a bare alarm.
  - "indeterminate"  — reachable when EITHER the owning-artifact scan (a
                        plans/handoffs/decisions directory could not be
                        listed) OR the inbox scan itself (`cross-repo/inbox/`
                        could not be listed — Review: code-reviewer Finding 2)
                        is degraded, e.g. permission-denied — a dropped
                        owning dir means an ORPHANED verdict cannot be
                        trusted (the memo could be owned by something under
                        the unscanned subtree), and a dropped inbox means the
                        detector cannot even enumerate its primary input
                        surface (a much higher-severity miss: it silently
                        suppresses the whole detector, not just risks one
                        false verdict). Carries `notice` plus `scan_errors`,
                        and `findings`/`offer` when candidates were found
                        under the (incomplete) scan.

A memo is an ORPHANED-AGING DEFERRAL iff ALL hold:
  1. File lives in `cross-repo/inbox/*.md`.
  2. `kind` in {ask, proposal} (frontmatter; defaults to "ask" when absent).
     `fyi`/`consult` memos are explicitly out of scope — they are not asks
     awaiting a decision.
  3. `status: open` (actioned/in_progress/anything else is excluded).
  4. `today - created >= age_threshold_days` (default 3; overridable via
     `params["age_threshold_days"]`).
  5. NO owning artifact references it — grepped from `docs/plans/*.md`
     (including `source_memo:` frontmatter), `state/handoffs/*.md`, and
     `docs/decisions/*.md`. A word/token-boundary-anchored match on the
     memo's FULL BASENAME against any of the three surfaces is OWNED.
     A match on the looser TOPIC SLUG (date prefix + extension stripped)
     is OWNED only against `docs/plans/` and `docs/decisions/` — handoffs
     are excluded from the slug match because they routinely name-drop a
     memo's basename in passing prose without actually owning it (a
     full-basename match against a handoff still counts; a slug match
     does not).

Discriminator from `/workday-start` Step 1.45 (workday_start_cross_repo_memo_surface):
that surfacer inventories ALL open/in_progress memos unconditionally, every
session. This op fires ONLY on the actionable-AND-unowned-AND-aging subset —
a much narrower, higher-signal set meant for periodic drift-detection, not
every-session orientation noise.

Spec backlink: tasks/hidden-deferral-detectors/design.md § Detector 2

Self-registration: importing this module calls register_op("deferral.detect_orphan_memo",
...) as a side-effect (same pattern as ops/engine_drift.py).
"""

from __future__ import annotations
import sys

import fnmatch
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops._fm_util import extract_frontmatter_scalar

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)$")
_DEFAULT_AGE_THRESHOLD_DAYS = 3
_ACTIONABLE_KINDS = ("ask", "proposal")

# Review: code-reviewer Finding 3 — handoffs get their own, narrower owning-text
# blob because they routinely name-drop a memo's basename in passing prose
# ("read memo X, noted Y") without actually tracking/owning it. Only a full
# basename match against handoff bodies counts as ownership (no slug match) —
# the slug match stays reserved for docs/plans/ (source_memo: frontmatter) and
# docs/decisions/, where a topic-slug reference is a deliberate structured cite,
# not incidental prose.
_SLUG_OWNING_GLOBS = ("docs/plans/*.md", "docs/decisions/*.md")
_BASENAME_ONLY_OWNING_GLOBS = ("state/handoffs/*.md",)
_OWNING_GLOBS = _SLUG_OWNING_GLOBS + _BASENAME_ONLY_OWNING_GLOBS


def _topic_slug(basename: str) -> str:
    """Derive a topic slug from a memo filename: strip the leading
    YYYY-MM-DD- date prefix and the .md extension. Falls back to the
    extension-stripped stem when there is no date-prefix match.
    """
    stem = basename[:-3] if basename.endswith(".md") else basename
    m = _DATE_PREFIX_RE.match(stem)
    return m.group(1) if m else stem


def _anchored_match(needle: str, haystack: str) -> bool:
    """Word/token-boundary-anchored substring test — a plain `needle in
    haystack` check lets a short/generic slug (or, less likely, a full
    basename) match inside an unrelated longer word and silently suppress a
    real orphan. Mirrors the \\b-anchored discipline Detector 1's
    `_make_planned_check` already applies to verb matching (see that
    module's own "avoids a substring false-positive like 'list' inside
    'checklist'" negative-spec).
    """
    if not needle:
        return False
    return bool(re.search(r"\b" + re.escape(needle) + r"\b", haystack))


def classify_orphan_memos(
    memos: Iterable[Dict[str, Optional[str]]],
    owning_text: str,
    today: date,
    age_threshold_days: int = _DEFAULT_AGE_THRESHOLD_DAYS,
    owning_text_slug: Optional[str] = None,
    scan_degraded: bool = False,
    scan_errors: Optional[List[str]] = None,
) -> dict:
    """Pure decision logic — no I/O, deterministically testable.

    Args:
        memos: iterable of dicts, one per inbox memo, each with keys
            "basename", "kind", "status", "created", "from" (all values are
            already-extracted frontmatter scalars, or None when absent).
        owning_text: concatenated text of every candidate owning artifact
            (plans, handoffs, decisions) — a full-basename match is checked,
            word/token-boundary-anchored, against this blob, not a per-file
            grep loop, so the core stays I/O-free.
        today: the date to compute age against (injected for determinism).
        age_threshold_days: minimum age in days to qualify as "aging".
        owning_text_slug: the (narrower) blob the loose topic-slug match is
            checked against. Handoffs routinely name-drop a memo's basename
            in passing prose without actually owning it, so production
            callers pass a handoffs-EXCLUDED blob here (plans + decisions
            only) — the slug match stays reserved for the structured
            `source_memo:`/DR-reference surfaces, not incidental mentions.
            Defaults to `owning_text` when omitted (single-blob callers get
            the pre-Finding-3 combined behavior for both match types).
        scan_degraded: True when the caller's owning-artifact scan could not
            fully list one of its candidate directories (e.g. permission-
            denied). A dropped owning dir means an ORPHANED verdict cannot be
            trusted — the memo could be owned by something under the
            unscanned subtree — so this downgrades the outcome to
            "indeterminate" rather than a confident "orphans_found".
        scan_errors: human-readable strings describing which scan(s) failed,
            carried through onto the "indeterminate" result for diagnostics.

    Returns a result dict with a "state" key set to "clean", "orphans_found",
    or "indeterminate" (only reachable when `scan_degraded` is True — see
    Args). Bad/missing dates on a memo cause that memo to be skipped (never
    crash) — see module docstring predicate #4.
    """
    if owning_text_slug is None:
        owning_text_slug = owning_text

    findings: List[dict] = []

    for memo in memos:
        basename = (memo.get("basename") or "").strip()
        kind = (memo.get("kind") or "ask").strip() or "ask"
        status = (memo.get("status") or "").strip()
        created = (memo.get("created") or "").strip()
        frm = (memo.get("from") or "").strip()

        if kind not in _ACTIONABLE_KINDS:
            continue
        if status != "open":
            continue
        if not _DATE_RE.match(created):
            continue
        try:
            created_date = date.fromisoformat(created)
        except ValueError:
            print(f"skip: classify_orphan_memos: created_date = date.fromisoformat(created) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue

        age_days = (today - created_date).days
        # Review: code-reviewer Finding 7 — a valid-but-absurd future
        # `created:` (fat-fingered year) yields negative age_days that would
        # never clear the threshold and read permanently, silently clean.
        # A negative age is itself suspicious operator-authored data, so
        # flag it anyway rather than let it suppress a real orphan.
        suspicious_future_date = age_days < 0
        if not suspicious_future_date and age_days < age_threshold_days:
            continue

        if basename and _anchored_match(basename, owning_text):
            continue
        slug = _topic_slug(basename) if basename else ""
        if slug and _anchored_match(slug, owning_text_slug):
            continue

        if suspicious_future_date:
            offer = (
                f"[deferral] inbox memo {basename} (from {frm}, kind:{kind}) — "
                f"has a suspicious future `created:` date ({-age_days}d ahead of "
                "today), no owning plan/baton/DR. Check the date is not a "
                "typo; route to /plan or reply, or archive if already done."
            )
        else:
            offer = (
                f"[deferral] inbox memo {basename} (from {frm}, {age_days}d, "
                f"kind:{kind}) — actionable, no owning plan/baton/DR. "
                "Route to /plan or reply, or archive if already done."
            )

        findings.append(
            {
                "basename": basename,
                "from": frm,
                "kind": kind,
                "age_days": age_days,
                "offer": offer,
            }
        )

    if not findings:
        if scan_degraded:
            return {
                "state": "indeterminate",
                "notice": (
                    "owning-artifact scan is degraded (a plans/handoffs/decisions "
                    "directory could not be listed) — cannot confirm this repo is "
                    "genuinely orphan-free"
                ),
                "scan_errors": scan_errors or [],
            }
        return {"state": "clean"}

    if scan_degraded:
        return {
            "state": "indeterminate",
            "findings": findings,
            "offer": "\n".join(f["offer"] for f in findings),
            "notice": (
                "owning-artifact scan is degraded — these candidates could not be "
                "verified as truly unowned; treat as indeterminate, not confirmed orphans"
            ),
            "scan_errors": scan_errors or [],
        }

    return {
        "state": "orphans_found",
        "findings": findings,
        "offer": "\n".join(f["offer"] for f in findings),
    }


def _resolve_today(today_param: Optional[str]) -> date:
    """Resolve the "today" reference date: params["today"] (ISO) when
    given and parseable, else the real date.today().
    """
    if today_param:
        try:
            return date.fromisoformat(today_param)
        except ValueError:
            print(f"skip: _resolve_today: return date.fromisoformat(today_param) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
    return date.today()


def _read_inbox_memos(
    inbox_dir: Path,
) -> Tuple[List[Dict[str, Optional[str]]], bool]:
    """List `inbox_dir`'s top-level *.md files and extract the frontmatter
    scalars this op needs.

    Returns `(memos, degraded)`. A MISSING directory is a normal,
    non-degraded "no inbox here" (`degraded=False`, empty `memos`) — that
    mirrors workday_start_cross_repo_memo_surface's tolerance and is not a
    scan failure. An UNREADABLE (permission-denied) directory is a genuine
    scan failure: `degraded=True`, empty `memos`. Review: code-reviewer
    Finding 2 — `os.listdir` DOES raise `PermissionError` correctly here (this
    is not the glob-selector variant of the silent-enumeration bug), but the
    previous version converted that raised exception into a bare `[]` with
    zero signal propagated to the caller, so `_handler` could not distinguish
    "inbox scan failed" from "inbox genuinely empty" — the primary input
    surface got no equivalent protection to the owning-artifact scan's
    `scan_degraded`/`scan_errors` signal (see `_read_owning_text`). Callers
    MUST OR `degraded` into their own `scan_degraded` computation before
    calling `classify_orphan_memos` — an unreadable inbox must never read as
    a confident "clean" verdict.
    """
    if not inbox_dir.is_dir():
        return [], False

    try:
        names = sorted(os.listdir(inbox_dir))
    except OSError as exc:
        logger.warning(
            "deferral.detect_orphan_memo: cannot scan %s — %s; "
            "inbox discovery degraded (NOT the same as \"no memos found\")",
            inbox_dir,
            exc,
        )
        return [], True

    memos: List[Dict[str, Optional[str]]] = []
    for name in names:
        if not name.endswith(".md"):
            continue
        path = inbox_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            print(f"skip: _read_inbox_memos: text = path.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        memos.append(
            {
                "basename": name,
                "kind": extract_frontmatter_scalar(text, "kind"),
                "status": extract_frontmatter_scalar(text, "status"),
                "created": extract_frontmatter_scalar(text, "created"),
                "from": extract_frontmatter_scalar(text, "from"),
            }
        )
    return memos, False


def _read_owning_text(root: Path, globs: Iterable[str] = _OWNING_GLOBS) -> Tuple[str, List[str]]:
    """Concatenate the text of every candidate owning artifact matched by
    `globs` under `root` into one blob for substring matching. `globs`
    defaults to every owning surface (plans + handoffs + decisions) —
    callers that need the handoffs-EXCLUDED slug-eligible blob (Finding 3)
    pass `_SLUG_OWNING_GLOBS` explicitly.

    NOTE: each `pattern`'s directory is listed via `os.scandir()`, NOT
    `root.glob(pattern)` — `Path.glob()`'s selector silently swallows
    `PermissionError` while walking (empirically re-verified: a chmod-000 dir
    yields an empty iterator from `glob()`, no exception), which would make a
    dropped owning directory read as "this memo has no owning artifact"
    rather than "ownership could not be determined" — a false ORPHANED
    verdict. An unreadable individual file is still skipped silently (best-
    effort content read).

    Returns `(blob, scan_errors)`. `scan_errors` names each pattern's
    directory that could not be listed (permission-denied, etc.) — non-empty
    ONLY on a genuine directory-listing failure, never on a merely-absent
    directory (absent is a normal, non-degraded "no candidates here").
    """
    parts: List[str] = []
    scan_errors: List[str] = []
    for pattern in globs:
        pattern_path = Path(pattern)
        target_dir = root / pattern_path.parent
        name_pattern = pattern_path.name
        if not target_dir.is_dir():
            continue
        try:
            entries = sorted(os.scandir(target_dir), key=lambda e: e.name)
        except OSError as exc:
            print(
                f"deferral.detect_orphan_memo: cannot scan {target_dir} — {exc}; "
                "owning-artifact scan degraded (NOT the same as \"no owning artifact\")",
                file=sys.stderr,
            )
            scan_errors.append(f"{target_dir}: {exc}")
            continue
        for entry in entries:
            if not fnmatch.fnmatch(entry.name, name_pattern):
                continue
            path = Path(entry.path)
            if not path.is_file():
                continue
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except OSError:
                print(f"skip: _read_owning_text: parts.append(path.read_text(encoding=\"utf-8\")) failed: {sys.exc_info()[1]}", file=sys.stderr)
                continue
    return "\n".join(parts), scan_errors


@register_op("deferral.detect_orphan_memo")
async def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'deferral.detect_orphan_memo' handler — probe THIS repo's
    cross-repo/inbox/ for orphaned-aging deferral memos.

    Params:
        age_threshold_days (optional int, or int-coercible str): overrides
            the default 3-day aging threshold. Coerced via int() with a
            fallback to the default on a non-coercible value — the
            standalone `python3 -m` invocation path can plausibly hand this
            through as a string.
        today (optional ISO date string): overrides date.today() for
            deterministic testing.
    repo_root: resolved main-worktree root for common_dir scope; falls back
        to Path.cwd() for the standalone `python3 -m` invocation path.

    Resolves the inbox memos and the owning-artifact text via the I/O
    helpers above, then delegates the (up to) three-state decision to the
    pure classify_orphan_memos(). Returns its result dict unmodified — this
    handler is a thin I/O-resolving wrapper, not a second decision point.

    `scan_degraded` is True when EITHER the inbox scan (`_read_inbox_memos` —
    Review: code-reviewer Finding 2) OR the owning-artifact scan
    (`_read_owning_text`) could not fully list a candidate directory — an
    unreadable inbox is at least as severe a miss as an unreadable owning
    dir (it can silently suppress the entire detector, not just risk a false
    orphan), so both surfaces feed the same degraded signal.
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    try:
        age_threshold_days = int(
            params.get("age_threshold_days", _DEFAULT_AGE_THRESHOLD_DAYS)
        )
    except (TypeError, ValueError):
        age_threshold_days = _DEFAULT_AGE_THRESHOLD_DAYS
    today = _resolve_today(params.get("today"))

    memos, inbox_degraded = _read_inbox_memos(root / "cross-repo" / "inbox")
    owning_text, owning_scan_errors = _read_owning_text(root)
    owning_text_slug, slug_scan_errors = _read_owning_text(root, _SLUG_OWNING_GLOBS)
    scan_errors = owning_scan_errors + slug_scan_errors
    if inbox_degraded:
        scan_errors = [f"{root / 'cross-repo' / 'inbox'}: unreadable"] + scan_errors

    return classify_orphan_memos(
        memos,
        owning_text,
        today,
        age_threshold_days,
        owning_text_slug=owning_text_slug,
        scan_degraded=inbox_degraded or bool(owning_scan_errors) or bool(slug_scan_errors),
        scan_errors=scan_errors,
    )
