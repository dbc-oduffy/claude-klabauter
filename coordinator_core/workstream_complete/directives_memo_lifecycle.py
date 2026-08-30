"""
coordinator_core.workstream_complete.directives_memo_lifecycle — memo
lifecycle (Step 2.65/2.66) + scratch self-clean (Step 2.67) slice of the
`/workstream-complete` computed-skill directive spine.

Purpose: computes the mechanical half of DoE-claude
`coordinator/skills/workstream-complete/SKILL.md`'s Steps 2.65-2.67 —
cross-repo memo lifecycle bookkeeping and session-authored scratch cleanup —
as importable, read-mostly functions the assembler
(`coordinator_core.workstream_complete.__init__`, wired in a later chunk of
this same plan) folds into one decision object alongside its six sibling
submodules. This module names mutating actions as `directive[]`-shaped
dicts (never invokes them in-process); the two ambiguous, non-automatable
calls this Step pair still requires (WHICH memos resolved this session, and
WHICH scratch files to delete-vs-keep) are judgment points owned by
`coordinator_core.workstream_complete.judgments` (C2f of this plan), not
this module — this module only supplies the evidence those judgment points
are built from.

Split provenance (F6/eng-director, docs/plans/2026-07-26-workstream-complete-
computed-frontage.md § D-4): this module is ONE HALF of what the compute-half
split first drafted as a single `directives_scratch_memo.py` row. That row
bundled four unrelated concerns — memo lifecycle, scratch self-clean, the
orientation pinboard, and the machine-local regeneratability check — under
step-number ADJACENCY in the SKILL's now-deleted ordinal spine, not real
domain cohesion (adjacency in a spine this plan's whole thesis says was never
the real structure is not cohesion). It was split at review into this module
(memo lifecycle + scratch self-clean, Steps 2.65-2.67) and the sibling
`directives_session_hygiene.py` (the orientation pinboard, the machine-local
regeneratability check, and the completeness-checklist WARN gate, Steps
2.8/2.95/2.96) — see that module for the other half.

Multi-module-assembler convention (F7/D-4): this is one of SEVEN submodules
(`directives_lessons_plan`, `directives_completion`, `directives_memo_lifecycle`
[this module], `directives_session_hygiene`, `directives_review`,
`directives_commit_tail`, `judgments`) this plan introduces as the FIRST
multi-module computed-skill engine in the tree — `coordinator_core.
pickup_assemble`, the nearest precedent, is one large module. `__init__.py`
remains the assembly + CLI seam: it imports each submodule's `build_directives`
(and, for `judgments`, its judgment-point builders) and folds their output
into the one 8-key envelope. A future converter inheriting this shape should
split its own submodules by domain cohesion, exactly as the split provenance
note above says NOT to do — never by source-document step-number adjacency.
Registered centrally in `computed-skills-conversion-checklist.md` (C10 of
this same plan) so the next converter inherits the convention deliberately.

Consumes manifest (orchestrates, reimplements none):
    coordinator/bin/archive-stamp-cli.py `claim-memo-stamp <path>` /
        `action-memo <path> [--decision <text>]` — the two-step memo
        status-flip ceremony (no single-shot `resolve` verb is wired into
        this CLI's argv dispatch as of this module's authoring — see
        Negative-spec).
    coordinator_core.frontmatter.primitives.split_frontmatter / read_fm_field
        — frontmatter parsing for the inbox memo glob; never a full YAML load.
    A plain `git` subprocess (status/log/rev-parse) for the session-authored
        predicate's git-log and git-status facts — mirrors
        `coordinator_core.workstream_complete.resolve_repo_root`'s own plain-
        subprocess shape (see Negative-spec on why this module does not
        import that helper).

Negative-spec:
    - Do NOT invoke any CLI in-process here. Every mutating action (the memo
      claim/action pair, the deletion-blocks tail-args call) is returned as a
      `directives[]` entry naming an existing atomic CLI; this module reads
      disk and git state only.
    - Do NOT decide WHICH memos resolved this session, or WHICH scratch files
      to delete vs. justify-keep. `judgments.py` (C2f) builds the
      `memo-resolution-attribution` and `scratch-disposition-per-file`
      judgment points this module's evidence feeds; this module never
      resolves them itself — for `memo-resolution-attribution` that means
      `compute_memo_resolution_attribution` returns the union of THREE
      signals fired, never a disposition. This narrows, it does not remove:
      a memo resolved without any of the three signals firing is still a
      genuine judgment call the EM must confirm — SKILL text's "deciding
      whether a specific scratch file is still useful is not reducible to a
      predicate" still governs scratch disposition (unchanged, still
      genuinely non-automatable). (Corrected 2026-07-30: the memo-side half
      of this bullet used to also quote the SKILL's "no reliable
      programmatic signal connects commits to memo resolution" — that
      premise was stale; see `compute_memo_resolution_attribution`'s own
      docstring and the 2026-07-30 doe-claude-em cross-repo memo it
      backlinks to. Also corrected: the census line that seeded the stale
      premise, `docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-
      workstream.node-map.md:57`, carries a dated annotation naming this
      same correction.)
    - Do NOT add a `--close`/`--resolve` subcommand to `archive-repo-memo` or
      `archive-stamp-cli`. `coordinator/bin/cross-repo-memo.py`'s own header
      states there is NO closure subcommand by design; `archive-stamp-cli`
      exposes the two-step `claim-memo-stamp` + `action-memo` ceremony (backed
      by `coordinator_core.ops.memo_transition`'s `claim`/`action` verbs) and
      this module composes exactly that pair — `memo_transition.py` also
      defines a native-only single-shot `resolve` verb, but no `coordinator/
      bin/` CLI wires it into an invocable subcommand as of this module's
      authoring, so it is not used here (a future chunk may wire it and
      collapse the pair below into one directive; flagged, not silently
      assumed here).
    - Do NOT import `_directive`/`_NO_CONSOLE` from `coordinator_core.
      workstream_complete.__init__` — that module is the assembler that
      imports THIS module once the C3 wiring chunk lands; importing back from
      it here would create an import cycle. This module defines its own tiny
      copies of those process-local helpers, matching the pre-split compute
      module's own shape byte-for-byte (see `__init__.py:96,243-250`)
      deliberately, not by accidental duplication — every sibling submodule
      in this split does the same.
    - Do NOT fold `d-emit-deletion-blocks` and the pre-existing `__init__.py`
      `d-close-tail-args`/`d-deletion-blocks` directives into one call here —
      that de-duplication is a C3 wiring-chunk decision (which of the two
      call shapes survives touches every other submodule's tail-args
      contribution, not just this one), not this chunk's to make.
    - The keep-list (`_KEEP_LIST_PATTERNS`) and the banned-phrasing patterns
      (`_BANNED_MEMO_PHRASINGS`, `_BANNED_DEFERRAL_PHRASINGS`) are allowlist/
      denylist CONFIG DATA, not narrated prose — extend the tuples, don't
      hand-roll a parallel prose description of what they cover.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Optional

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.git.repo_root import git_common_dir
from coordinator_core.win_portability import no_console_creationflags

_NO_CONSOLE = no_console_creationflags()

#: The `decisions` keys this module's `build_directives` reads — declared
#: once so a caller (`__init__.py`'s `preflight.decisions_template`
#: composition) can import and union this tuple rather than hand-copying
#: the key list. See AC3
#: (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md):
#: the arg-builder and the template read this SAME constant.
_KEY_MEMO_DISPOSITIONS = "memo_dispositions"

FREE_VALUE_KEYS: tuple[str, ...] = (_KEY_MEMO_DISPOSITIONS,)


def _directive(
    id_: str,
    cli: str,
    args: list[str],
    depends_on: Any = None,
    already_satisfied: bool = False,
) -> dict[str, Any]:
    """Same 5-key shape as `coordinator_core.workstream_complete._directive`
    (id/cli/args/depends_on/already_satisfied) — a local copy, not an import;
    see the module docstring's Negative-spec for why."""
    return {"id": id_, "cli": cli, "args": args, "depends_on": depends_on, "already_satisfied": already_satisfied}


def _run_git(repo_root: Path, args: list[str]) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            # `--no-optional-locks`: every caller here is read-only (`log`,
            # `status`, `merge-base`), and without this `git status` takes the
            # index lock and rewrites a 4.9MB / 35k-entry index as a side
            # effect of being asked a question. On a tree ~50 sessions write
            # concurrently that lock is contended, which is both the cost and
            # its variance. Same shape `archive_stamp.py` already uses.
            ["git", "-C", str(repo_root), "--no-optional-locks", *args],
            capture_output=True,
            text=True,
            timeout=30,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------
# Step 2.65 — cross-repo memo lifecycle sweep
# ---------------------------------------------------------------------------


def list_open_memos(repo_root: Path) -> list[dict[str, str]]:
    """Step 2.65 sub-steps 1-2 (`d-list-open-memos`): glob
    `cross-repo/inbox/*.md`, parse frontmatter, filter to `status: open` (a
    missing `status:` field is treated as open, per the SKILL text). Returns
    `[]` on zero matches — the SKILL's own "skip this step silently" rule is
    a caller decision (the EM prompt / `judgments.build_memo_resolution_
    attribution_judgment_point` in `judgments.py`, C2f), not this function's;
    this function only supplies the evidence.

    Read-only: text + frontmatter parse only, never a write, never a `git`
    call — memo status is read straight off disk.

    Uses `read_fm_field_unquoted`, not the raw `read_fm_field`, for every
    field below — `status` is compared against an in-memory literal (the
    comparison-safe case that primitive exists for; a quoted `status:
    "open"` must compare equal to unquoted `"open"`), and `from`/`topic` are
    echoed as evidence a caller may itself compare or log, so the same
    one-layer-unquoted convention applies uniformly rather than leaving a
    quoting inconsistency between the three fields.
    """
    inbox = repo_root / "cross-repo" / "inbox"
    if not inbox.is_dir():
        return []
    memos: list[dict[str, str]] = []
    for path in sorted(inbox.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        split = split_frontmatter(text)
        if split is None:
            continue
        status = read_fm_field_unquoted(split.fm_text, "status") or "open"
        if status != "open":
            continue
        memos.append(
            {
                "path": str(path),
                "basename": path.name,
                "title": _first_heading(split.body_with_leading_newline) or path.name,
                "from": read_fm_field_unquoted(split.fm_text, "from") or "",
                "topic": read_fm_field_unquoted(split.fm_text, "topic") or "",
                "status": status,
            }
        )
    return memos


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _first_heading(body: str) -> Optional[str]:
    m = _HEADING_RE.search(body)
    return m.group(1) if m else None


def build_memo_disposition_directives(dispositions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Step 2.65 sub-steps 3-4 (`d-flip-memo-status`): for each
    EM/PM-resolved memo disposition — `{"path": <inbox memo path>,
    "decision": <one-line decision text>}`, supplied by the caller once the
    `memo-resolution-attribution` judgment point (`judgments.py`, C2f) is
    answered — emits the two-step `archive-stamp-cli` ceremony: `claim-memo-
    stamp` then `action-memo --decision <text>`, the latter `depends_on` the
    former. IDs are suffixed with the memo's basename (`d-claim-memo-stamp:
    <basename>`, `d-flip-memo-status:<basename>`) since a session may resolve
    more than one memo in one pass — every other directive in this spine is a
    fixed singleton id, this pair is not.
    """
    directives: list[dict[str, Any]] = []
    for idx, disposition in enumerate(dispositions):
        if not isinstance(disposition, Mapping):
            # A {path: decision} MAPPING, or a bare list of memo paths, is the
            # natural first guess at this shape and used to reach `["path"]` and
            # die on `TypeError: string indices must be integers` — a traceback
            # that names neither the key nor the entry. Mirrors the identical
            # refusal `directives_lessons_plan.build_lesson_capture_directives`
            # already gives for its own bare-string case: same rejection, same
            # nothing-written guarantee, just legible.
            raise ValueError(
                f"decisions['memo_dispositions'][{idx}] is a "
                f"{type(disposition).__name__}, not a mapping (required key: "
                "'path'; optional: 'decision'). Supply a list of "
                "{'path': <inbox memo path>, 'decision': <one-line text>} "
                "mappings and re-run apply — this is a malformed decisions map, "
                "not a ceremony failure, and nothing has been written."
            )
        if not str(disposition.get("path") or "").strip():
            raise ValueError(
                f"decisions['memo_dispositions'][{idx}] is missing required key "
                "'path'. Supply it and re-run apply — this is a malformed "
                "decisions map, not a ceremony failure, and nothing has been "
                "written."
            )
        path = str(disposition["path"])
        basename = Path(path).name
        claim_id = f"d-claim-memo-stamp:{basename}"
        flip_id = f"d-flip-memo-status:{basename}"
        directives.append(_directive(claim_id, "archive-stamp-cli", ["claim-memo-stamp", path]))
        flip_args = ["action-memo", path]
        decision = disposition.get("decision")
        if decision:
            flip_args += ["--decision", str(decision)]
        directives.append(_directive(flip_id, "archive-stamp-cli", flip_args, depends_on=claim_id))
    return directives


def memo_flip_resolves_ids(dispositions: list[dict[str, Any]]) -> list[str]:
    """The `d-flip-memo-status:<basename>` ids `build_memo_disposition_
    directives` emits for the same input — the exact list the `memo-
    resolution-attribution` judgment point's "resolved" disposition must
    name in `resolves`.

    `apply`'s gate matches a `resolves` entry against a directive id
    EXACTLY, never by prefix, so naming the unsuffixed `d-flip-memo-status`
    base leaves the gate permanently shut and the memo never gets stamped.
    Delegating to the builder keeps the two exact by construction.

    The `d-claim-memo-stamp:<basename>` half is deliberately EXCLUDED: it
    gates on nothing (`depends_on=None`) and is the flip's own producer, so
    it must fire whenever memo dispositions exist at all.
    """
    return [
        d["id"]
        for d in build_memo_disposition_directives(dispositions)
        if d["id"].startswith("d-flip-memo-status:")
    ]


# ---------------------------------------------------------------------------
# Step 2.65 — memo-resolution attribution signals (feeds `judgments.build_
# memo_resolution_attribution_judgment_point`'s `recommendation`)
# ---------------------------------------------------------------------------


def _scan_archived_memos(repo_root: Path) -> list[dict[str, str]]:
    """Frontmatter scan of `cross-repo/archive/*.md`, pulling only the two
    fields `compute_memo_resolution_attribution` needs (`picked_up_by`,
    `realized_by`) — both fields `coordinator_core.ops.memo_transition`'s
    claim/action verbs write with `numeric_quoting=True`, so
    `read_fm_field_unquoted` is required here for the same comparison-safe
    reason `list_open_memos` above uses it for `status`."""
    archive_dir = repo_root / "cross-repo" / "archive"
    if not archive_dir.is_dir():
        return []
    memos: list[dict[str, str]] = []
    for path in sorted(archive_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        split = split_frontmatter(text)
        if split is None:
            continue
        memos.append(
            {
                "path": str(path),
                "basename": path.name,
                "picked_up_by": read_fm_field_unquoted(split.fm_text, "picked_up_by") or "",
                "realized_by": read_fm_field_unquoted(split.fm_text, "realized_by") or "",
            }
        )
    return memos


def _since_iso(session_start_time: Optional[datetime]) -> Optional[str]:
    if session_start_time is None:
        return None
    return session_start_time.astimezone(timezone.utc).isoformat()


def _session_commit_shas(repo_root: Path, since_iso: Optional[str]) -> "frozenset[str]":
    """Full (`%H`) commit SHAs on the current branch since `since_iso` —
    mirrors `_created_this_session`'s own `--since=<start>` shape rather
    than inventing a second git-range recipe. `since_iso=None` (session
    start time unresolvable) falls back to the whole branch history, same
    degrade-gracefully posture `resolve_session_start_time`'s callers
    already accept elsewhere in this module."""
    args = ["log", "--format=%H"]
    if since_iso:
        args.append(f"--since={since_iso}")
    proc = _run_git(repo_root, args)
    if proc is None or proc.returncode != 0:
        return frozenset()
    return frozenset(line.strip() for line in proc.stdout.splitlines() if line.strip())


def _inbox_to_archive_renames(repo_root: Path, since_iso: Optional[str]) -> dict[str, str]:
    """`basename -> archive path` for every `cross-repo/inbox/*` ->
    `cross-repo/archive/*` rename in range, enumerated via `git log
    --diff-filter=R -M --name-status` — `R100`-shaped output has no false
    positives from an ordinary edit, per the 2026-07-30 cross-repo memo this
    function's own module docstring backlinks to."""
    args = ["log", "--diff-filter=R", "-M", "--name-status", "--format="]
    if since_iso:
        args.append(f"--since={since_iso}")
    proc = _run_git(repo_root, args)
    if proc is None or proc.returncode != 0:
        return {}
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("R"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        _status, old_path, new_path = parts
        if old_path.startswith("cross-repo/inbox/") and new_path.startswith("cross-repo/archive/"):
            result[Path(new_path).name] = new_path
    return result


def compute_memo_resolution_attribution(repo_root: Path, sid: str) -> list[dict[str, Any]]:
    """Step 2.65's memo-resolution-attribution EVIDENCE — feeds `judgments.
    build_memo_resolution_attribution_judgment_point`'s `recommendation`,
    never the disposition itself; this function only supplies which of
    three signals this engine itself writes fired for which archived memo.
    Source: 2026-07-30 doe-claude-em cross-repo memo (`cross-repo/archive/
    2026-07-30-doe-claude-em-wsc-review-trail-passthrough-and-memo-
    attribution.md`) — the prior "non-automatable" premise (see this
    module's own Negative-spec, and `judgments.py`'s pre-2026-07-30 module
    docstring) was stale: `picked_up_by`, `realized_by`, and the inbox-
    >archive `git mv` are all fields/facts this engine's own claim/action
    ceremony (`coordinator_core.ops.memo_transition`) already writes.

    Three independent signals, scanned over `cross-repo/archive/*.md`
    frontmatter and this session's own commit range (anchored at
    `resolve_session_start_time`, same anchor `classify_session_authored_
    files` uses):
      1. `picked_up_by == sid` — direct equality against this session's id.
      2. `realized_by` is a PREFIX of a commit SHA in this session's range
         (`realized_by` is written as a short/abbreviated SHA; `git log
         --format=%H` returns full SHAs, and an abbreviated SHA is always a
         prefix of its full form).
      3. the memo's archive path was reached via an inbox->archive rename
         in this session's commit range (`_inbox_to_archive_renames`).

    A memo with NO signal firing is simply absent from the returned list —
    this function names ONLY what fired; `judgments.py` owns the
    not-resolved fallback when the list comes back empty.

    Returns one dict per memo with >=1 signal firing, sorted by basename:
    `{"path": str, "basename": str, "signals": list[str]}`, `signals` a
    subset of `{"picked_up_by", "realized_by", "archive_rename"}` in
    that fixed order (never re-sorted per-memo — a fixed enumeration order
    is easier to read than an alphabetized one for a 3-element set).
    """
    if not sid:
        return []
    since_iso = _since_iso(resolve_session_start_time(repo_root, sid))
    session_shas = _session_commit_shas(repo_root, since_iso)
    renamed = _inbox_to_archive_renames(repo_root, since_iso)
    memos = _scan_archived_memos(repo_root)

    records: dict[str, dict[str, Any]] = {}

    def _record(basename: str, path: str) -> dict[str, Any]:
        return records.setdefault(basename, {"path": path, "basename": basename, "signals": []})

    for memo in memos:
        if memo["picked_up_by"] and memo["picked_up_by"] == sid:
            _record(memo["basename"], memo["path"])["signals"].append("picked_up_by")
        if memo["realized_by"] and any(sha.startswith(memo["realized_by"]) for sha in session_shas):
            _record(memo["basename"], memo["path"])["signals"].append("realized_by")

    for basename, archive_path in renamed.items():
        _record(basename, archive_path)["signals"].append("archive_rename")

    return sorted(records.values(), key=lambda r: r["basename"])


# ---------------------------------------------------------------------------
# Step 2.65 (do-now check) / Step 2.66 — banned-phrasing lints
# ---------------------------------------------------------------------------

#: Step 2.66's sender-side no-renag rule (`d-lint-banned-memo-phrasings`) —
#: denylist CONFIG DATA, matched case-insensitively against the EM's drafted
#: Final Summary / `Flag to PM:` / handoff text before it is finalized.
_BANNED_MEMO_PHRASINGS: tuple[re.Pattern[str], ...] = (
    re.compile(r"PM-relays?\s+pending\s+your\s+action", re.IGNORECASE),
    re.compile(r"awaiting relay", re.IGNORECASE),
    re.compile(r"pending sibling-EM action", re.IGNORECASE),
)


def lint_banned_memo_phrasings(text: str) -> list[str]:
    """Step 2.66 (`d-lint-banned-memo-phrasings`): a mechanical check, not a
    directive naming a CLI — no existing CLI performs this scan, and it is a
    trivial in-process string match over EM-authored draft text, not disk
    state. Returns the matched pattern strings (empty list = clean)."""
    return [p.pattern for p in _BANNED_MEMO_PHRASINGS if p.search(text)]


# ---------------------------------------------------------------------------
# Step 2.67 — session-authored scratch self-clean
# ---------------------------------------------------------------------------

#: Step 2.67's keep-list (never a self-clean candidate regardless of the
#: session-authored predicate) — allowlist CONFIG DATA, `fnmatch` glob
#: patterns relative to the repo root. Extend this tuple when the SKILL's own
#: keep-list grows; do not hand-roll a parallel prose description elsewhere.
_KEEP_LIST_PATTERNS: tuple[str, ...] = (
    "docs/plans/*.md",
    "tasks/*/todo.md",
    "tasks/*/plan.md",
    "archive/completed/*",
    "archive/completed/*/*",
    "state/orientation_cache*",
    "state/handoffs/*",
    "state/handoffs/*/*",
    "state/handoff-tracker*",
    "state/lessons/*",
    "state/week-changelog/*",
    "state/review-trail/*",
    "state/review-trail/*/*",
    "state/memos/*",
    "state/*ledger*",
    "state/*queue*",
    "state/*queue*/*",
    "archive/handoffs/*",
    "archive/handoffs/*/*",
    "cross-repo/inbox/*",
    "cross-repo/archive/*",
)


def _is_keep_listed(rel_path: str) -> bool:
    return any(fnmatch(rel_path, pattern) for pattern in _KEEP_LIST_PATTERNS)


def _git_common_dir(repo_root: Path) -> Optional[Path]:
    out = git_common_dir(str(repo_root))
    if not out:
        return None
    common = Path(out)
    return common if common.is_absolute() else repo_root / common


def resolve_session_start_time(repo_root: Path, sid: str) -> Optional[datetime]:
    """Resolves `$SESSION_START_TIME` (`d-resolve-session-start-time`): the
    mtime of `.git/coordinator-sessions/<sid>/` under the git COMMON dir
    (never a literal `<repo_root>/.git` join — see
    `coordinator_core.archive_stamp.cs_action_memo`'s own comment on why a
    linked-worktree join is wrong), or — if that claim dir is absent — the
    timestamp of the earliest commit not shared with the branch's upstream
    (falling further back to `origin/main`/`main`/`master` if no upstream is
    configured, and finally to the repo's very first commit if none of those
    resolve). The SKILL text's "this session's first commit on the active
    branch" has no disk-level branch-point marker to pin against without an
    upstream/main to diff against — this ladder is the best-effort resolution
    of that fuzziness, not a guess invented here from nothing.
    """
    common_dir = _git_common_dir(repo_root)
    if common_dir is not None:
        claim_dir = common_dir / "coordinator-sessions" / sid
        if claim_dir.is_dir():
            try:
                return datetime.fromtimestamp(claim_dir.stat().st_mtime, tz=timezone.utc)
            except OSError:
                pass

    for base in ("@{upstream}", "origin/main", "origin/master", "main", "master"):
        proc = _run_git(repo_root, ["merge-base", "HEAD", base])
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            merge_base = proc.stdout.strip()
            log_proc = _run_git(
                repo_root, ["log", "--reverse", "--format=%cI", f"{merge_base}..HEAD"]
            )
            if log_proc is not None and log_proc.returncode == 0 and log_proc.stdout.strip():
                first_line = log_proc.stdout.strip().splitlines()[0]
                parsed = _parse_iso(first_line)
                if parsed is not None:
                    return parsed

    log_proc = _run_git(repo_root, ["log", "--reverse", "--format=%cI"])
    if log_proc is not None and log_proc.returncode == 0 and log_proc.stdout.strip():
        first_line = log_proc.stdout.strip().splitlines()[0]
        return _parse_iso(first_line)
    return None


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _git_status_porcelain(repo_root: Path) -> list[tuple[str, str]]:
    proc = _run_git(repo_root, ["status", "--porcelain"])
    if proc is None or proc.returncode != 0:
        return []
    rows: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        status_code, _, rel_path = line[:2], line[2:], line[3:]
        # Rename entries ("R  old -> new") carry the arrow in rel_path; the
        # new path is what matters for a session-authored disposition.
        if " -> " in rel_path:
            rel_path = rel_path.split(" -> ", 1)[1]
        rows.append((rel_path.strip(), status_code))
    return rows


def _created_this_session(repo_root: Path, rel_path: str, session_start_time: datetime) -> bool:
    since = session_start_time.astimezone(timezone.utc).isoformat()
    proc = _run_git(
        repo_root,
        ["log", "--diff-filter=A", f"--since={since}", "--format=%H", "--", rel_path],
    )
    return bool(proc is not None and proc.returncode == 0 and proc.stdout.strip())


def _session_created_paths(repo_root: Path, session_start_time: datetime) -> "frozenset[str]":
    """Batched replacement for per-path `_created_this_session`: ONE `git
    log --diff-filter=A --since=<start> --name-only` spawn for the whole
    `classify_session_authored_files` pass, rather than one spawn per dirty
    path (the N+1 this function exists to eliminate -- see
    state/bug-backlog for the hang this caused under this repo's dirty-file
    load). Same predicate as `_created_this_session`, computed once: "does
    `git log --diff-filter=A --since=<start>` show this session created
    it" -- with no pathspec, `--name-only` enumerates every path any
    in-range added-file commit touched, which is the exact union of paths
    `_created_this_session` would have returned True for individually.

    `--format=` (empty commit format) is deliberate: it suppresses the
    commit-header lines `--name-only` would otherwise interleave with
    paths, leaving pure path lines (plus blank separators, filtered below)
    -- no per-commit parsing needed since only path membership matters
    here, never which commit added which path.

    Normalization: no `-c core.quotepath=false` override, matching
    `_git_status_porcelain`'s own convention in this module (that function
    does not force quotepath either) -- paths from this call and from
    `_git_status_porcelain` see the same quoting behavior, so a raw
    string-equality membership test compares like with like.

    Returns `frozenset()` on git failure (`proc is None or returncode !=
    0`) or on a git call that would not run at all -- the caller is
    responsible for not invoking this when `session_start_time` is None,
    matching `_created_this_session`'s own degrade-to-False posture on
    failure (never raises, never over-reports as authored).
    """
    since = session_start_time.astimezone(timezone.utc).isoformat()
    proc = _run_git(
        repo_root,
        ["log", "--diff-filter=A", f"--since={since}", "--name-only", "--format="],
    )
    if proc is None or proc.returncode != 0:
        return frozenset()
    return frozenset(line.strip() for line in proc.stdout.splitlines() if line.strip())


def _mtime_after(path: Path, session_start_time: datetime) -> bool:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return mtime > session_start_time.astimezone(timezone.utc)


def classify_session_authored_files(
    repo_root: Path,
    session_start_time: Optional[datetime],
    known_concurrent_paths: frozenset[str] = frozenset(),
    known_added_paths: "Optional[frozenset[str]]" = None,
) -> list[dict[str, Any]]:
    """Step 2.67's session-authored predicate (`d-classify-session-authored-
    files`): a file is session-authored iff it appears in `git status
    --porcelain` AND ((a) `git log --diff-filter=A --since=<start>` shows
    this session created it, OR (b) it is untracked, its mtime is after
    `session_start_time`, and it is not a known-concurrent-session path).
    Files on the keep-list (`_KEEP_LIST_PATTERNS`) or in
    `known_concurrent_paths` are classified NOT session-authored regardless
    of (a)/(b) — this is also the ordering constraint the SKILL text states
    for the Step 2.67 / Step 3.0 seam: this function's `known_concurrent_
    paths` input is the Step 3.0 case-(b) classification, computed elsewhere
    and handed in, never re-derived here.

    `known_added_paths` (C5, docs/plans/2026-08-26-the-gate-paths-six-spawns-
    collapse-to-four.md § C5): optional, additive. When supplied (even an
    empty `frozenset()` — a confirmed "this session added nothing"), predicate
    (a) is answered from this set directly and `_session_created_paths`'s own
    `git log --diff-filter=A` spawn is skipped entirely — the caller has
    already resolved the trailer-attributed add-set from data it fetched for
    another purpose (`__init__.py`'s `own_numstat_blocks`, see that module's
    own derivation) and handing it straight through costs this function
    nothing. `None` (the default) reproduces this function's pre-C5 behaviour
    exactly: predicate (a) falls through to `_session_created_paths`'s own
    spawn whenever `session_start_time` is resolvable. A caller unable to
    derive a trustworthy add-set (no data reached that path, or the data it
    has cannot distinguish an add from a modify) must pass `None`, never a
    guessed/partial set — this function does not itself validate the set it
    is handed.

    `session_start_time=None` (both the claim-dir mtime and every fallback in
    `resolve_session_start_time` came up empty) degrades every non-keep-
    listed, non-known-concurrent file to `session_authored: False` — with
    neither (a) nor (b) computable, the predicate cannot affirmatively fire,
    matching the SKILL text's own "fails both (a) and (b) -> not session-
    authored" fallthrough. This still applies when `known_added_paths` is
    supplied: (b) is unconditionally unavailable without a resolvable start
    time, and a file's own mtime has no bearing on predicate (a).

    Returns one dict per porcelain-dirty path: `{"path", "session_authored",
    "reason"}`. Files this function marks NOT session-authored still need a
    Step 3.0 (a)/(b)/(c) disposition — that classifier is out of this
    module's scope (owned by `directives_commit_tail.py`, C2e); this function
    only says whether Step 2.67 itself may touch the path.
    """
    session_created_paths: "frozenset[str]" = frozenset()
    if known_added_paths is not None:
        session_created_paths = known_added_paths
    elif session_start_time is not None:
        session_created_paths = _session_created_paths(repo_root, session_start_time)

    results: list[dict[str, Any]] = []
    for rel_path, status_code in _git_status_porcelain(repo_root):
        if _is_keep_listed(rel_path):
            results.append({"path": rel_path, "session_authored": False, "reason": "keep-list"})
            continue
        if rel_path in known_concurrent_paths:
            results.append({"path": rel_path, "session_authored": False, "reason": "known-concurrent"})
            continue

        authored = False
        reason = "fails predicate (a) and (b)"
        if session_start_time is not None:
            if rel_path in session_created_paths:
                authored = True
                reason = "predicate (a): created this session"
            elif status_code.startswith("??") and _mtime_after(repo_root / rel_path, session_start_time):
                authored = True
                reason = "predicate (b): untracked, mtime after session start"
        results.append({"path": rel_path, "session_authored": authored, "reason": reason})
    return results


#: `build_deletion_blocks_directive` (`d-emit-deletion-blocks`) was REMOVED
#: 2026-08-30. It emitted `archive-session-scope.py tail-args --deleted-paths/--kept-
#: entries`; `251ff57703` deleted that subcommand the same day, having already
#: retired the OTHER producer of the same argv (`d-close-tail-args`) — this one
#: was missed and outlived the parser it fed by one commit. The consumer chain
#: died before either: gravestone: wsc-tail.py — K-046 deleted `coordinator/bin/wsc-tail.py` on
#: 2026-08-23 (`c07062c99`). Nothing replaced it and nothing needs to; the
#: `deleted_paths`/`kept_entries` decisions keys go with it. NOT to be confused
#: with `build_deletion_blocks_check_directive` (`__init__.py`), which fronts
#: the still-live op-side deletion-block gate.


#: Step 2.67's banned Final Summary phrasings (`d-lint-banned-deferral-
#: phrasings`) — denylist CONFIG DATA, the enumeration-without-deletion shape
#: the SKILL text bans, broadened to catch paraphrase evasion.
_BANNED_DEFERRAL_PHRASINGS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Transient artifacts.{0,20}/distill will sweep", re.IGNORECASE),
    re.compile(r"Other working files predating this session", re.IGNORECASE),
    re.compile(r"Will be cleaned up later", re.IGNORECASE),
    re.compile(r"safe to delete in a future pass", re.IGNORECASE),
    re.compile(
        r"(transient|scratch|working|temporary|trash|tmp|residual|leftover)"
        r".{0,40}"
        r"(sweep|distill|future|later|next|cleanup|clean.?up|downstream|prune[d]?|pruning)",
        re.IGNORECASE,
    ),
)


def lint_banned_deferral_phrasings(text: str) -> list[str]:
    """Step 2.67 (`d-lint-banned-deferral-phrasings`): same shape as
    `lint_banned_memo_phrasings` — a mechanical in-process check over
    EM-authored draft text, not a `directives[]`-shaped CLI call. Returns the
    matched pattern strings (empty list = clean)."""
    return [p.pattern for p in _BANNED_DEFERRAL_PHRASINGS if p.search(text)]


# ---------------------------------------------------------------------------
# This module's directive-spine contribution
# ---------------------------------------------------------------------------


def build_directives(decisions: dict[str, Any]) -> list[dict[str, Any]]:
    """Assembles this module's contribution to the mechanical directive
    spine from `decisions` — same role
    `coordinator_core.workstream_complete.build_directives` plays for the
    pre-split compute module; the C3 wiring chunk calls this alongside its
    six sibling submodules' own `build_directives` and folds every result
    into one `directives[]` list.

    Recognised `decisions` keys (all optional — an absent key contributes no
    directive, never a directive with guessed/empty required args):
      - `memo_dispositions`: `list[{"path": str, "decision": str}]` — see
        `build_memo_disposition_directives`.
    """
    directives: list[dict[str, Any]] = []
    directives.extend(build_memo_disposition_directives(decisions.get(_KEY_MEMO_DISPOSITIONS) or []))
    return directives
