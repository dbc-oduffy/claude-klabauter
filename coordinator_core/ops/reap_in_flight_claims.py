"""
coordinator_core.ops.reap_in_flight_claims — return-data survey (plus a
delegated apply path) of orphaned in_flight handoff claims.

Purpose, per `docs/plans/2026-08-26-two-callers-want-two-numbers-not-a-1301-line-cli.md`
chunk C1: `coordinator/bin/reap-orphaned-in-flight-handoffs.py` measured
515.6ms warm on its read side (DR-344 § 6, Deleted). Its job — "release
crash-orphaned claims on consumed+in_flight handoffs, and name the ones it
cannot dispose of" — is still needed, read off its two actual callers rather
than off the deleted code:

  - `orient_assemble/readers_health_reaper.py` wants two integers,
    `would_release` / `would_reclaim`, in-process (no subprocess, no prose
    parsing).
  - `workday_complete/brief.py` wants a CLI directive that mutates.

This module answers both from ONE corpus pass: `survey()` returns a
`SurveyResult` carrying the two integers AND the per-candidate
`Disposition` list the CLI prints; `apply_dispositions()` performs the
mutations by delegating to the existing, tested `archive-stamp-cli`
verbs — this module never writes a handoff's frontmatter itself.

ONE open per corpus file (AC3): `_build_corpus` reads `state/handoffs/*.md`
exactly once each, and every downstream question (census, ship-detection)
is answered from that in-memory table — never a second pass over the same
files. Frontmatter is read via `coordinator_core.frontmatter.primitives
.read_fm_field_unquoted`, never a hand-rolled scanner (it is both the cheap
and the correct reader: it strips trailing inline `# comment`s that a naive
scanner misses).

The census counts claims in `state/handoffs` ONLY (PM ruling 2026-08-26) —
the archive walk plays no part in `survey()`.

Negative-spec:
    - Does NOT open `coordinator/bin/reap-orphaned-in-flight-handoffs.py`
      (DR-344 § 6) — built from the plan's Problem section and from
      `coordinator/bin/tests/test_reap_orphaned_in_flight_handoffs.py`,
      which encodes the requirement as predicates.
    - Does NOT re-implement a prose-parsing contract — `survey()` returns
      integers and structured dispositions, never a sentence a caller must
      regex.
    - Does NOT mutate frontmatter directly. `apply_dispositions()` shells
      out to `coordinator/bin/archive-stamp-cli.py`'s tested verbs
      (`unclaim-handoff`, `stamp-shipped-in`, `ship-handoff`) for every
      write; a single-writer invariant it does not reimplement.
    - Does NOT register a JSON-RPC op — both callers import this module
      in-process (the `reap_orphaned_agent_dirs` shape), so there is
      nothing to dispatch over IPC and no eager-module-list entry is owed.
    - Does NOT build the completion index or the implemented-plan index
      when there are no dead-holder candidates to judge (pay-for-use) —
      each is a whole-corpus-adjacent read of its own and neither is
      free.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.handoff_children import has_live_children_many
from coordinator_core.session.liveness import session_live

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ARCHIVE_STAMP_CLI = (
    Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "archive-stamp-cli.py"
)

#: Both the current and the legacy (DR-084 pre-rename) spellings this census
#: must recognise as "claimed" — never widened beyond these two, and never
#: narrowed to just one (a corpus can carry either).
_CLAIMED_STATUS_VALUES = ("claimed", "consumed")

_VERDICT_RELEASE = "release"
_VERDICT_RECLAIM_SHIPPED = "reclaim_shipped"
_VERDICT_SKIP_LIVE_CHILDREN = "skip_live_children"
_VERDICT_SKIP_GOVERNED_PLAN = "skip_governed_plan_implemented"


@dataclass
class HandoffRecord:
    """One `state/handoffs/*.md` file's census-relevant frontmatter, read
    from exactly one open+parse (AC3)."""

    path: Path
    resolved_path: Path
    status: Optional[str]
    deployment_state: Optional[str]
    consumed_by: Optional[str]
    kind: Optional[str]
    deliverable_id: Optional[str]
    handoff_id: Optional[str]


@dataclass
class Disposition:
    """One candidate's verdict — the shape both callers consume: orientation
    folds these into the two counts, the CLI prints them per-candidate."""

    path: str
    holder: str
    verdict: str
    detail: str
    sha: Optional[str] = None


@dataclass
class SurveyResult:
    would_release: int
    would_reclaim: int
    dispositions: List[Disposition] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Corpus pass — ONE open per file (AC3)
# ---------------------------------------------------------------------------


def _read_text_once(path: Path) -> str:
    """The sole file-open site in the corpus pass — factored out so a test
    can count invocations against the corpus size (AC3's own oracle)."""
    return path.read_text(encoding="utf-8", errors="replace")


def _build_corpus(handoffs_dir: Path) -> List[HandoffRecord]:
    """One pass over `state/handoffs/*.md`: one open, one frontmatter split,
    one resolve() per file. Every downstream read (census, ship-detection)
    consumes this table — never a second pass over the same files."""
    records: List[HandoffRecord] = []
    if not handoffs_dir.is_dir():
        return records

    for entry in sorted(handoffs_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".md":
            continue
        text = _read_text_once(entry)
        split = split_frontmatter(text)
        if split is None:
            continue
        fm = split.fm_text
        records.append(
            HandoffRecord(
                path=entry,
                resolved_path=entry.resolve(),
                status=read_fm_field_unquoted(fm, "status"),
                deployment_state=read_fm_field_unquoted(fm, "deployment_state"),
                consumed_by=read_fm_field_unquoted(fm, "consumed_by"),
                kind=read_fm_field_unquoted(fm, "kind"),
                deliverable_id=read_fm_field_unquoted(fm, "deliverable_id"),
                handoff_id=read_fm_field_unquoted(fm, "handoff_id"),
            )
        )
    return records


def _in_flight_claims(corpus: List[HandoffRecord]) -> List[HandoffRecord]:
    return [
        r
        for r in corpus
        if r.status in _CLAIMED_STATUS_VALUES
        and r.deployment_state == "in_flight"
        and r.consumed_by
    ]


# ---------------------------------------------------------------------------
# Corpus-adjacent indexes — built ONLY when a dead-holder candidate needs
# them (pay-for-use); each is its own single call, never per-candidate.
# ---------------------------------------------------------------------------


def _build_completion_index(repo_root: Path) -> Dict[str, List[dict]]:
    """`{authored_by: [completion-record, ...]}` from ONE unfiltered
    `query_records` call. Keying `query_records` with a per-holder `where=`
    is refuted (35 keyed calls ~= 2188ms); grouping one unkeyed call in
    memory is the cheap and correct shape."""
    index: Dict[str, List[dict]] = {}
    for rec in query_records("completion", repo_root):
        authored_by = rec.get("frontmatter", {}).get("authored_by")
        if authored_by:
            index.setdefault(authored_by, []).append(rec)
    return index


def _build_implemented_plan_index(repo_root: Path) -> Dict[str, dict]:
    """`{deliverable_id: {"path":..., "title":...}}` for every `implemented`
    plan, from ONE `where`-filtered `query_records` call."""
    index: Dict[str, dict] = {}
    for rec in query_records("plan", repo_root, where="status=implemented"):
        deliverable_id = rec.get("frontmatter", {}).get("deliverable_id")
        if deliverable_id:
            index[deliverable_id] = {
                "path": rec.get("path"),
                "title": rec.get("frontmatter", {}).get("title"),
            }
    return index


# ---------------------------------------------------------------------------
# Ship-detection (P1-P4) — orphan-candidate raw shas, batched git-log
# resolution, MAX-committer-timestamp selection. Requirement encoded in
# coordinator/bin/tests/test_reap_orphaned_in_flight_handoffs.py.
# ---------------------------------------------------------------------------


def _shipped_orphan_candidate_shas(
    holder: str,
    dead_holders_seen: Dict[str, int],
    completion_index: Dict[str, List[dict]],
) -> Optional[List[str]]:
    """P2+P3, zero git spawns. Returns the raw `commits[]` candidate list, or
    None on any P2/P3 failure.

    P2 (ambiguous): more than one dead-holder in-flight claim shares this
    holder — which claim a completion entry ships for cannot be
    disambiguated, so both fail closed to None.
    P3 (no completions): the holder authored zero completion entries.
    """
    if dead_holders_seen.get(holder, 0) > 1:
        return None
    entries = completion_index.get(holder) or []
    if not entries:
        return None
    shas: List[str] = []
    for entry in entries:
        shas.extend(entry.get("frontmatter", {}).get("commits") or [])
    return shas or None


def _batch_commit_timestamps(shas: List[str], repo_root: str) -> Dict[str, int]:
    """ONE `git log --no-walk=unsorted --ignore-missing` call resolving
    committer timestamps for every candidate SHA at once — never one `git
    show` per commit per orphan. `--ignore-missing` silently drops an
    unresolvable SHA from stdout; a dropped SHA is simply absent from the
    returned map, never defaulted to a resolved answer."""
    if not shas:
        return {}
    cmd = [
        "git",
        "-C",
        repo_root,
        "log",
        "--no-walk=unsorted",
        "--ignore-missing",
        "--format=%H %ct",
        *shas,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=_NO_WINDOW,
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    matched: Dict[str, int] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        full_sha, ct = parts
        for sha in shas:
            if full_sha.startswith(sha):
                try:
                    matched[sha] = int(ct)
                except ValueError:
                    continue
    return matched


def _best_shipped_sha(candidates: List[str], sha_ct: Dict[str, int]) -> str:
    """Pure in-memory MAX-committer-timestamp selection. A candidate absent
    from `sha_ct` (dropped by `--ignore-missing`) never wins by virtue of
    being "found" in some other sense — only `sha_ct` entries count. Fails
    closed to `""` when nothing resolves."""
    best_sha = ""
    best_ct = None
    for sha in candidates:
        ct = sha_ct.get(sha)
        if ct is None:
            continue
        if best_ct is None or ct > best_ct:
            best_ct = ct
            best_sha = sha
    return best_sha


# ---------------------------------------------------------------------------
# survey() — the return-data call both callers consume
# ---------------------------------------------------------------------------


def survey(repo_root: Path, *, handoffs_dir: Optional[Path] = None) -> SurveyResult:
    """One-pass survey of orphaned in_flight handoff claims under
    `state/handoffs` (archived twins are out of scope — PM ruling
    2026-08-26). Performs no mutation; `apply_dispositions()` is the write
    path.
    """
    repo_root = Path(repo_root)
    if handoffs_dir is None:
        handoffs_dir = repo_root / "state" / "handoffs"

    corpus = _build_corpus(Path(handoffs_dir))
    claims = _in_flight_claims(corpus)
    if not claims:
        return SurveyResult(0, 0, [])

    dead: List[HandoffRecord] = [
        r for r in claims if not session_live(r.consumed_by, cwd=str(repo_root))
    ]
    if not dead:
        return SurveyResult(0, 0, [])

    common_dir = git_common_dir(repo_root)
    live_children = asyncio.run(
        has_live_children_many([str(r.resolved_path) for r in dead], common_dir)
    )

    dead_holders_seen: Dict[str, int] = {}
    for r in dead:
        dead_holders_seen[r.consumed_by] = dead_holders_seen.get(r.consumed_by, 0) + 1

    pending: List[HandoffRecord] = []
    dispositions: List[Disposition] = []
    for r in dead:
        exit_code = live_children.get(str(r.resolved_path), 2)
        if exit_code != 1:
            reason = (
                "has live children"
                if exit_code == 0
                else "live-children check indeterminate — fail-closed"
            )
            dispositions.append(
                Disposition(str(r.path), r.consumed_by, _VERDICT_SKIP_LIVE_CHILDREN, reason)
            )
            continue
        pending.append(r)

    if not pending:
        return SurveyResult(0, 0, dispositions)

    implemented_plan_index = _build_implemented_plan_index(repo_root)

    governed: List[HandoffRecord] = []
    ship_check: List[HandoffRecord] = []
    for r in pending:
        if (
            r.kind != "spinoff"
            and r.deliverable_id
            and r.deliverable_id in implemented_plan_index
        ):
            plan = implemented_plan_index[r.deliverable_id]
            dispositions.append(
                Disposition(
                    str(r.path),
                    r.consumed_by,
                    _VERDICT_SKIP_GOVERNED_PLAN,
                    f"deliverable_id {r.deliverable_id!r} governed by implemented plan "
                    f"{plan.get('path')!r} — releasing would re-advertise shipped work",
                )
            )
            governed.append(r)
            continue
        ship_check.append(r)

    if not ship_check:
        return SurveyResult(0, 0, dispositions)

    completion_index = _build_completion_index(repo_root)

    candidate_shas_by_path: Dict[str, List[str]] = {}
    all_shas: List[str] = []
    for r in ship_check:
        shas = _shipped_orphan_candidate_shas(r.consumed_by, dead_holders_seen, completion_index)
        if shas:
            candidate_shas_by_path[str(r.resolved_path)] = shas
            all_shas.extend(shas)

    sha_ct = _batch_commit_timestamps(all_shas, str(repo_root))

    would_release = 0
    would_reclaim = 0
    for r in ship_check:
        shas = candidate_shas_by_path.get(str(r.resolved_path))
        best = _best_shipped_sha(shas, sha_ct) if shas else ""
        if best:
            would_reclaim += 1
            dispositions.append(
                Disposition(
                    str(r.path),
                    r.consumed_by,
                    _VERDICT_RECLAIM_SHIPPED,
                    f"holder {r.consumed_by} is dead but authored a completion "
                    f"entry whose commits include shipped sha {best}",
                    sha=best,
                )
            )
        else:
            would_release += 1
            dispositions.append(
                Disposition(
                    str(r.path),
                    r.consumed_by,
                    _VERDICT_RELEASE,
                    f"holder {r.consumed_by} is dead with no resolvable shipped commit",
                )
            )

    return SurveyResult(would_release, would_reclaim, dispositions)


# ---------------------------------------------------------------------------
# apply_dispositions() — delegates every mutation to archive-stamp-cli
# ---------------------------------------------------------------------------


def _run_archive_stamp_cli(args: List[str]) -> "tuple[bool, str]":
    try:
        proc = subprocess.run(
            [sys.executable, str(_ARCHIVE_STAMP_CLI), *args],
            capture_output=True,
            text=True,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def apply_dispositions(dispositions: List[Disposition]) -> "tuple[List[str], List[str]]":
    """Perform every mutating disposition by delegating to `archive-stamp-cli`'s
    tested verbs — `release` -> `unclaim-handoff`, `reclaim_shipped` ->
    `stamp-shipped-in` + `ship-handoff`. A skip verdict performs no write.

    Returns `(applied_paths, failed_details)`.
    """
    applied: List[str] = []
    failed: List[str] = []
    for d in dispositions:
        if d.verdict == _VERDICT_RELEASE:
            ok, detail = _run_archive_stamp_cli(
                ["unclaim-handoff", d.path, "--reaped-from", d.holder]
            )
            if ok:
                applied.append(d.path)
            else:
                failed.append(f"{d.path}: unclaim-handoff failed: {detail}")
        elif d.verdict == _VERDICT_RECLAIM_SHIPPED:
            ok, detail = _run_archive_stamp_cli(
                ["stamp-shipped-in", d.path, "--sha", d.sha or "", "--kind", "ship-commit"]
            )
            if not ok:
                failed.append(f"{d.path}: stamp-shipped-in failed: {detail}")
                continue
            ok, detail = _run_archive_stamp_cli(["ship-handoff", d.path, "--sha", d.sha or ""])
            if ok:
                applied.append(d.path)
            else:
                failed.append(f"{d.path}: ship-handoff failed: {detail}")
    return applied, failed


# ---------------------------------------------------------------------------
# main() — a thin CLI convenience, not the C3 rebuild (out of this chunk's
# scope; C3 owns `coordinator/bin/reap-orphaned-in-flight-handoffs.py`).
# ---------------------------------------------------------------------------
