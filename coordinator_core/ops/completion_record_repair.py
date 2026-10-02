"""
coordinator_core.ops.completion_record_repair — repair mode for the hollow-
completion-record sweep (`coordinator_core.ops.completion_record_sweep`).

Purpose: `completion_record_sweep` is READ-ONLY diagnostic; this module is the
companion WRITER that, per `coordinator_core.completion_record_integrity.
hollow_reasons` finding, does the smallest defensible repair:

  - `empty-commits` — backfill `commits:` from git history, in PRECISION
    order (a record describes ONE completion, not a whole chain's history):
      1. Deliverable-Id trailer AND Session-Id trailer both match the
         record's own `deliverable_id:`/`authored_by:` fields -- the tightest
         bound this module can express.
      2. (no session match, or the record carries no `authored_by:`)
         Deliverable-Id trailer matches, bounded to commits whose date falls
         within a small margin of the record's own `created:` date.
      3. (no date to bound with) Deliverable-Id trailer matches, unbounded --
         last resort, only reached when the record carries neither a
         resolvable session nor a created date.
      4. (no `deliverable_id:` at all, or zero candidates from 1-3) the
         record's governing plan path / `chain:` slug appears in a commit
         SUBJECT line (never a full-body scan -- see cost note below).
    A match resolving to more than `MAX_COMMITS_PER_RECORD` shas is NOT a
    single-completion's worth of work -- it goes to residue, unwritten,
    with the count named, rather than being accepted as "found".
  - `placeholder-marker` — NEVER touched. `completion-entry.schema.json`'s
    `status` enum is `{pending-release, released}` -- no `needs-author` value
    exists. Reported in residue per the brief's "if none, report instead of
    inventing" fallback; the prose placeholder itself is never touched.
  - `plan-not-landed` / `plan-unresolvable` — NEVER written. Residue only.

COST: ONE `git log` spawn per repo (`build_git_history_index`), TRAILERS AND
SUBJECT ONLY -- no `%B` (full body). A full-body walk over this repo's ~38k
non-merge commits cost ~440ms of git-spawn-plus-Python-parse alone (measured
2026-09-29), blowing the repo's own 500ms brightline before any per-record
work ran. Trailers+subject is a small fraction of that payload -- see the
module-level `PROCESS_MS_MEASURED` note in this file's own test module for
the re-measurement. `--since` bounds the walk to shortly before the earliest
hollow record's `created:` date (never a per-record git spawn -- one bound
computed from all findings up front, applied once).

The plan-path/`chain:`-slug fallback (case 4 above) runs ONE alternation-
regex pass over every commit's SUBJECT line for every hollow record still
needing it (`_build_term_index`) -- O(commits), never O(records * commits),
and never a second `git log`/`--grep` spawn (a `--grep`-filtered call would
DROP every commit not matching the grep from the SAME walk's trailer/session
output, corrupting cases 1-3 for every other record in the same call).

Dry-run by default (`apply=False`): computes and reports exactly what WOULD be
written, touches no file. `apply=True` performs a read-modify-write per
repaired record (atomic replace through the claiming seam -- this module has no
IPC engine caller supplying a `repo_root`-derived lock namespace).

Negative-spec:
  - Never invents a commit SHA, a plan status, or a schema-illegal field
    value. A record this module cannot resolve -- or resolves too broadly
    (> MAX_COMMITS_PER_RECORD) -- is reported, not repaired.
  - Never rewrites the prose placeholder body text -- only `commits:` is ever
    mutated, and only for the `empty-commits` check.
  - Never spawns `git` more than once per repo, regardless of finding count.
  - Never reads a commit's full body/message -- trailers and subject only.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple

from coordinator_core.completion_record_integrity import (
    REASON_EMPTY_COMMITS,
    REASON_PLACEHOLDER,
    REASON_PLAN_NOT_LANDED,
    REASON_PLAN_UNRESOLVABLE,
)
from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.ops.completion_record_sweep import sweep_repo
from coordinator_core.session.claimed_write import replace_text

#: Legal `status:` enum values per `completion-entry.schema.json` (1.4.0) --
#: no `needs-author` slot exists.
LEGAL_STATUS_VALUES = ("pending-release", "released")

#: A record describes ONE completion. A "match" wider than this many shas is
#: symptomatic of a Deliverable-Id shared across a whole chain/plan's life
#: (real data, but not this record's own work) -- residue, not a write.
MAX_COMMITS_PER_RECORD = 20

#: Symmetric margin (days) around a record's `created:` date for the
#: date-bounded Deliverable-Id fallback (case 2). Small on purpose: wide
#: enough to catch a commit landing a day or two either side of the
#: completion-record's authoring date, not wide enough to pull in a whole
#: chain's unrelated later work.
DATE_WINDOW_MARGIN_DAYS = 3

#: Per-record window for the ONE `git log` walk: [created - WINDOW_BEFORE_DAYS,
#: created + WINDOW_AFTER_DAYS]. The union of every hollow record's window
#: bounds BOTH the `--since`/`--until` spawn args (outer envelope) AND which
#: individual commits survive parsing (`_merge_windows`/`_date_in_any_window`
#: -- a commit outside every record's window is discarded even if it falls
#: inside the outer `--since`/`--until` envelope, so a repo whose hollow
#: records cluster in two distant weeks doesn't pay for the gap between them).
WINDOW_BEFORE_DAYS = 14
WINDOW_AFTER_DAYS = 1

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
#: Multi-value trailer separator (`%(trailers:...,separator=%x02)`) --
#: distinct from both `_RECORD_SEP`/`_FIELD_SEP` so a multi-valued
#: Deliverable-Id or Session-Id trailer can never be mistaken for a record or
#: field boundary.
_TRAILER_SEP = "\x02"

_COMMITS_FLOW_RE = re.compile(r"^commits:\s*\[(.*)\]\s*(#.*)?$")
_COMMITS_BLOCK_RE = re.compile(r"^commits:\s*(#.*)?$")


class CommitRow(NamedTuple):
    sha: str
    deliverable_ids: FrozenSet[str]
    session_ids: FrozenSet[str]
    date: str  # YYYY-MM-DD, commit date
    subject: str


class GitHistoryIndex(NamedTuple):
    """The ONE `git log` walk's worth of lookup structures for a repo --
    built once, consulted per record. `ok=False` means the walk itself
    failed (spawn error or non-zero exit); every empty-commits record then
    goes to residue with `error` as the reason, never silently repaired."""

    commits: Tuple[CommitRow, ...]  # chronological (oldest first), non-merge, WITHIN a record window
    by_deliverable_id: Dict[str, List[str]]  # deliverable_id -> [sha, ...]
    #: sha -> CommitRow, built ONCE here -- `_match_by_deliverable_and_session`
    #: is called once per hollow record (up to hundreds of times); rebuilding
    #: this dict per call turned an O(commits) index build into an
    #: O(records * commits) one and was the actual cost driver behind an
    #: earlier over-budget measurement, not the git spawn itself.
    by_sha: Dict[str, "CommitRow"]
    ok: bool
    error: Optional[str] = None
    #: Raw record count `git log` emitted inside the `--since`/`--until`
    #: envelope, BEFORE per-commit window discard -- "commits actually read".
    commits_scanned: int = 0


def _record_windows(records: List[dict]) -> List[Tuple[datetime.date, datetime.date]]:
    """Every record's own `[created - WINDOW_BEFORE_DAYS, created +
    WINDOW_AFTER_DAYS]` window, one per record carrying a parseable
    `created:` date. A record with no `created:` contributes no window (see
    module docstring's edge-case note)."""
    windows: List[Tuple[datetime.date, datetime.date]] = []
    for fm in records:
        created = fm.get("created")
        if not created:
            continue
        try:
            d = datetime.date.fromisoformat(str(created)[:10])
        except ValueError:
            continue
        windows.append((d - datetime.timedelta(days=WINDOW_BEFORE_DAYS), d + datetime.timedelta(days=WINDOW_AFTER_DAYS)))
    return windows


def _merge_windows(windows: List[Tuple[datetime.date, datetime.date]]) -> List[Tuple[datetime.date, datetime.date]]:
    """Sorted, overlap/adjacency-merged windows -- turns the per-record union
    membership test into one bisect + range check per commit instead of an
    O(records) scan per commit."""
    if not windows:
        return []
    ordered = sorted(windows)
    merged: List[Tuple[datetime.date, datetime.date]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _date_in_any_window(d: datetime.date, merged: List[Tuple[datetime.date, datetime.date]]) -> bool:
    import bisect

    starts = [w[0] for w in merged]
    idx = bisect.bisect_right(starts, d) - 1
    if idx < 0:
        return False
    start, end = merged[idx]
    return start <= d <= end


def build_git_history_index(
    repo_root: Path,
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    windows: Optional[List[Tuple[datetime.date, datetime.date]]] = None,
) -> GitHistoryIndex:
    """ONE `git log --no-merges --reverse` walk over `HEAD`, TRAILERS AND
    SUBJECT ONLY (no `%B`) -- parsed into per-commit rows plus a
    Deliverable-Id -> [sha, ...] convenience map. Never spawns git again.

    `since`/`until`, when given, are passed as single `--since=<since>`/
    `--until=<until>` argv elements (never string-concatenated) -- the outer
    envelope covering every hollow record's window. `windows` (pre-merged via
    `_merge_windows`), when given, additionally DISCARDS any parsed commit
    whose date falls outside every individual record's window -- a repo whose
    hollow records cluster in two distant weeks doesn't pay for the calendar
    gap between them just because both weeks are inside the outer envelope.
    `commits_scanned` on the returned index counts records BEFORE this
    discard, so a caller can report "read N of the repo's full history",
    where N reflects the `--since`/`--until` bound, not the post-discard
    count.
    """
    fmt = (
        "%H"
        + _FIELD_SEP
        + f"%(trailers:key=Deliverable-Id,valueonly,separator={_TRAILER_SEP})"
        + _FIELD_SEP
        + f"%(trailers:key=Session-Id,valueonly,separator={_TRAILER_SEP})"
        + _FIELD_SEP
        + "%cs"  # committer date, short YYYY-MM-DD -- no body/subject byte
        + _FIELD_SEP
        + "%s"
        + _RECORD_SEP
    )
    argv = ["-C", str(repo_root), "log", "--no-merges", "--reverse", f"--format={fmt}"]
    if since:
        argv.append(f"--since={since}")
    if until:
        argv.append(f"--until={until}")
    argv.append("HEAD")
    proc = run_git(argv)
    if proc.timed_out:
        return GitHistoryIndex(commits=(), by_deliverable_id={}, by_sha={}, ok=False, error="git log timed out")
    if proc.returncode != 0:
        return GitHistoryIndex(
            commits=(), by_deliverable_id={}, by_sha={}, ok=False,
            error=f"git log exited {proc.returncode}: {proc.stderr.strip()}",
        )

    commits: List[CommitRow] = []
    by_deliverable: Dict[str, List[str]] = {}
    scanned = 0
    for raw_record in (proc.stdout or "").split(_RECORD_SEP):
        record = raw_record.strip("\n")
        if not record:
            continue
        fields = record.split(_FIELD_SEP, 3)
        if len(fields) < 4:
            continue
        sha, deliv_raw, sess_raw, rest = fields
        sha = sha.strip()
        if not sha:
            continue
        scanned += 1
        date, _, subject = rest.partition(_FIELD_SEP)
        date = date.strip()

        if windows:
            try:
                commit_date = datetime.date.fromisoformat(date)
            except ValueError:
                continue
            if not _date_in_any_window(commit_date, windows):
                continue  # outside every record's window -- discarded, never indexed

        deliverable_ids = frozenset(v.strip() for v in deliv_raw.split(_TRAILER_SEP) if v.strip())
        session_ids = frozenset(v.strip() for v in sess_raw.split(_TRAILER_SEP) if v.strip())
        row = CommitRow(sha=sha, deliverable_ids=deliverable_ids, session_ids=session_ids, date=date, subject=subject)
        commits.append(row)
        for deliv in deliverable_ids:
            by_deliverable.setdefault(deliv, []).append(sha)

    by_sha = {row.sha: row for row in commits}
    return GitHistoryIndex(
        commits=tuple(commits),
        by_deliverable_id=by_deliverable,
        by_sha=by_sha,
        ok=True,
        error=None,
        commits_scanned=scanned,
    )


def _in_date_window(commit_date: str, created: str, margin_days: int) -> bool:
    try:
        c_date = datetime.date.fromisoformat(commit_date)
        r_date = datetime.date.fromisoformat(str(created)[:10])
    except ValueError:
        return False
    delta = abs((c_date - r_date).days)
    return delta <= margin_days


def _record_chain_terms(fm: dict, plan_path: Optional[str]) -> Set[str]:
    """Literal substrings a commit SUBJECT line must contain to count as
    "carrying the plan path/plan_id" for this record -- the `chain:` slug
    itself, the resolved governing plan's path (live or archived), and that
    path's filename stem."""
    terms: Set[str] = set()
    chain = fm.get("chain")
    if chain:
        terms.add(str(chain))
    if plan_path:
        terms.add(str(plan_path))
        terms.add(Path(str(plan_path)).stem)
    return {t for t in terms if t}


def _build_term_index(commits: Tuple[CommitRow, ...], terms: Set[str]) -> Dict[str, List[str]]:
    """ONE alternation-regex pass over every commit SUBJECT (never the full
    body), answering every term's match set at once."""
    if not terms:
        return {}
    ordered = sorted(terms, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(t) for t in ordered))
    result: Dict[str, List[str]] = {t: [] for t in terms}
    for row in commits:
        seen: Set[str] = set()
        for m in pattern.finditer(row.subject):
            t = m.group(0)
            if t not in seen:
                seen.add(t)
                result[t].append(row.sha)
    return result


def _match_by_deliverable_and_session(fm: dict, index: GitHistoryIndex) -> Tuple[List[str], str]:
    """Cases 1-3 of the module docstring's precision ladder. Returns
    `([], "")` when `deliverable_id:` is absent or has zero candidates at
    all (caller then tries the subject fallback)."""
    deliv = fm.get("deliverable_id")
    if not deliv:
        return [], ""
    candidates = index.by_deliverable_id.get(str(deliv), [])
    if not candidates:
        return [], ""

    _empty_row = CommitRow("", frozenset(), frozenset(), "", "")

    session = fm.get("authored_by")
    if session:
        session = str(session)
        precise = [sha for sha in candidates if session in index.by_sha.get(sha, _empty_row).session_ids]
        if precise:
            return precise, "deliverable_id+session_id"

    created = fm.get("created")
    if created:
        bounded = [
            sha
            for sha in candidates
            if _in_date_window(index.by_sha.get(sha, _empty_row).date, str(created), DATE_WINDOW_MARGIN_DAYS)
        ]
        if bounded:
            return bounded, "deliverable_id+date-window"

    # Case 3: neither a session nor a date narrowed it -- unbounded, last
    # resort. Still subject to the MAX_COMMITS_PER_RECORD residue cap below.
    return candidates, "deliverable_id-unbounded"


def _write_commits_field(text: str, shas: List[str]) -> str:
    """Content-additive frontmatter rewrite: replace (or insert) the
    `commits:` field with a block-style list of *shas*, leaving every other
    byte of the record untouched."""
    lines = text.splitlines()
    fence_indices = [i for i, l in enumerate(lines) if l == "---"]
    if len(fence_indices) < 2:
        raise ValueError("no closing frontmatter fence found -- refusing to write commits:")
    close_idx = fence_indices[1]

    new_block = ["commits:"] + [f'  - "{sha}"' for sha in shas]

    key_idx = None
    is_flow = False
    for i in range(fence_indices[0] + 1, close_idx):
        if _COMMITS_FLOW_RE.match(lines[i]):
            key_idx = i
            is_flow = True
            break
        if _COMMITS_BLOCK_RE.match(lines[i]):
            key_idx = i
            is_flow = False
            break

    if key_idx is None:
        new_lines = lines[:close_idx] + new_block + lines[close_idx:]
    elif is_flow:
        new_lines = lines[:key_idx] + new_block + lines[key_idx + 1 :]
    else:
        end = key_idx + 1
        while end < close_idx and re.match(r"^\s+-\s", lines[end]):
            end += 1
        new_lines = lines[:key_idx] + new_block + lines[end:]

    return "\n".join(new_lines) + "\n"


def _atomic_write(path: Path, new_text: str) -> None:
    """Atomic replace through the claiming seam."""
    replace_text(path, new_text)


class RecordRepairResult(NamedTuple):
    record_path: str  # repo-root-relative, forward-slash
    shas: Tuple[str, ...]
    match_source: str
    written: bool  # True only when apply=True and the write actually happened


class RepairReport(NamedTuple):
    repo_root: str
    repairable: Tuple[RecordRepairResult, ...]
    #: (record_path, reason) -- never written, per this module's negative-spec.
    residue: Tuple[Tuple[str, str], ...]
    process_ms: float = 0.0
    #: Records whose best match exceeded MAX_COMMITS_PER_RECORD -- reported
    #: (path, sha_count) pairs, also present in `residue` with the same
    #: reason text.
    over_cap: Tuple[Tuple[str, int], ...] = ()
    #: `git log` record count actually emitted inside the `--since`/`--until`
    #: envelope ("commits actually read"), vs. `commits_kept` after the
    #: per-record-window discard. Both 0 when no record needed history.
    commits_scanned: int = 0
    commits_kept: int = 0


def repair_repo(repo_root: Path, *, apply: bool = False) -> RepairReport:
    """Repair every hollow record `sweep_repo(repo_root)` finds, per this
    module's docstring rules. `apply=False` (default) computes and reports
    without touching disk; `apply=True` writes the `commits:` backfill for
    every record this resolves a match for (subject to the
    `MAX_COMMITS_PER_RECORD` residue cap).

    ONE `git log` spawn total, regardless of how many records `sweep_repo`
    finds.
    """
    t0 = datetime.datetime.now()
    findings = sweep_repo(repo_root)
    repairable: List[RecordRepairResult] = []
    residue: List[Tuple[str, str]] = []
    over_cap: List[Tuple[str, int]] = []

    if not findings:
        elapsed_ms = (datetime.datetime.now() - t0).total_seconds() * 1000
        return RepairReport(repo_root=str(repo_root), repairable=(), residue=(), process_ms=elapsed_ms, over_cap=())

    parsed_by_path: Dict[str, Tuple[dict, str]] = {}
    for f in findings:
        record_path = repo_root / f.record_path
        text = record_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_frontmatter(text)
        fm = parsed.get("frontmatter") or {}
        parsed_by_path[f.record_path] = (fm, text)

    needs_history = any(REASON_EMPTY_COMMITS in f.failed_checks for f in findings)
    if needs_history:
        empty_commit_fms = [parsed_by_path[f.record_path][0] for f in findings if REASON_EMPTY_COMMITS in f.failed_checks]
        merged_windows = _merge_windows(_record_windows(empty_commit_fms))
        since = merged_windows[0][0].isoformat() if merged_windows else None
        until = merged_windows[-1][1].isoformat() if merged_windows else None
        index = build_git_history_index(repo_root, since=since, until=until, windows=merged_windows or None)
    else:
        index = GitHistoryIndex(commits=(), by_deliverable_id={}, by_sha={}, ok=True, error=None)

    pending_terms: Dict[str, Set[str]] = {}  # record_path -> its chain terms (subject fallback still needed)

    for f in findings:
        fm, _text = parsed_by_path[f.record_path]

        if REASON_EMPTY_COMMITS in f.failed_checks:
            if not index.ok:
                residue.append((f.record_path, f"empty-commits: git history index unavailable -- {index.error}"))
            else:
                shas, source = _match_by_deliverable_and_session(fm, index)
                if shas:
                    if len(shas) > MAX_COMMITS_PER_RECORD:
                        over_cap.append((f.record_path, len(shas)))
                        residue.append((
                            f.record_path,
                            f"empty-commits: {source} match resolved {len(shas)} shas (> "
                            f"{MAX_COMMITS_PER_RECORD}) -- too broad for one completion, residue not written",
                        ))
                    else:
                        repairable.append(RecordRepairResult(f.record_path, tuple(shas), source, False))
                else:
                    terms = _record_chain_terms(fm, f.plan_path)
                    if terms:
                        pending_terms[f.record_path] = terms
                    else:
                        residue.append((
                            f.record_path,
                            "empty-commits: no deliverable_id/session/date match and no chain/plan-path to search with",
                        ))

        if REASON_PLACEHOLDER in f.failed_checks:
            residue.append((
                f.record_path,
                "placeholder-marker: prose placeholder left untouched by design; completion-entry "
                f"schema's status enum is {LEGAL_STATUS_VALUES} -- no 'needs-author' value exists, "
                "reporting per brief rather than inventing one",
            ))

        if REASON_PLAN_NOT_LANDED in f.failed_checks:
            residue.append((
                f.record_path,
                f"plan-not-landed: governing plan {f.plan_path} status={f.plan_status!r} -- never faked",
            ))

        if REASON_PLAN_UNRESOLVABLE in f.failed_checks:
            residue.append((
                f.record_path,
                f"plan-unresolvable: chain={f.chain_slug!r} -- no plan doc found anywhere -- never faked",
            ))

    if pending_terms and index.ok:
        all_terms: Set[str] = set()
        for terms in pending_terms.values():
            all_terms |= terms
        term_index = _build_term_index(index.commits, all_terms)
        for record_path_rel, terms in pending_terms.items():
            matched: List[str] = []
            seen: Set[str] = set()
            for t in terms:
                for sha in term_index.get(t, []):
                    if sha not in seen:
                        seen.add(sha)
                        matched.append(sha)
            if matched:
                if len(matched) > MAX_COMMITS_PER_RECORD:
                    over_cap.append((record_path_rel, len(matched)))
                    residue.append((
                        record_path_rel,
                        f"empty-commits: plan-path/chain-slug match resolved {len(matched)} shas (> "
                        f"{MAX_COMMITS_PER_RECORD}) -- too broad for one completion, residue not written",
                    ))
                else:
                    repairable.append(RecordRepairResult(record_path_rel, tuple(matched), "plan-path/chain-slug", False))
            else:
                residue.append((
                    record_path_rel,
                    "empty-commits: no commit found carrying this record's Deliverable-Id, session, date window, plan path, or chain slug",
                ))

    if apply and repairable:
        applied: List[RecordRepairResult] = []
        for r in repairable:
            _fm, text = parsed_by_path[r.record_path]
            new_text = _write_commits_field(text, list(r.shas))
            _atomic_write(repo_root / r.record_path, new_text)
            applied.append(r._replace(written=True))
        repairable = applied

    elapsed_ms = (datetime.datetime.now() - t0).total_seconds() * 1000
    return RepairReport(
        repo_root=str(repo_root),
        repairable=tuple(repairable),
        residue=tuple(residue),
        process_ms=elapsed_ms,
        over_cap=tuple(over_cap),
        commits_scanned=index.commits_scanned,
        commits_kept=len(index.commits),
    )


def repair_repos(repo_roots: List[Path], *, apply: bool = False) -> List[RepairReport]:
    return [repair_repo(root, apply=apply) for root in repo_roots]
