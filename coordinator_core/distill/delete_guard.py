"""
coordinator_core.distill.delete_guard — mechanical delete-safety guard for handoffs
and cross-repo memos.

Purpose: implement all 4 handoff + 5 cross-repo-memo delete-safety guards as a pure
mechanical check — this module never writes the prose reason a delete decision is
reported with (that stays the LLM's job downstream); it only answers "is this
candidate eligible to delete, and if not, which guard(s) blocked it."

Guards implemented (CLASS-KEYED — see `classify_artifact` below; a guard that is
inapplicable to a candidate's artifact class does not run against it, rather than
run-and-fail):
  1. shipped_in present (the frontmatter must carry a non-empty `shipped_in:` field)
     — HANDOFF-ONLY: memos never carry `shipped_in`, so running this guard against a
     memo blocks every memo by construction (the 2026-07-23 cockpit 164/164-blocked
     defect).
  2. status: actioned (the frontmatter `status:` field must read exactly "actioned")
     — MEMO-ONLY: a live handoff's `status:` vocabulary is open/closed, never
     "actioned", so running this guard against a handoff blocks every handoff by
     construction (same defect, other half).
  3. active-reference ripgrep (via `_common.active_reference_guard`; a still-referenced
     candidate is never eligible) — both classes.
  4. commitment-closure vs `state/cross-repo-commitments` — both classes. **Fails
     loud** (blocks) when that surface is absent on disk: a guard that cannot check
     commitment-closure must never silently pass a delete on "can't check". When the
     surface exists, each ledger entry (a bare YAML doc carrying a top-level
     `status: open|closed` field) is checked: only an OPEN commitment that references
     the candidate blocks it; a closed commitment never blocks. An unreadable or
     unparseable ledger entry fails CLOSED (blocks, with a reason naming the entry)
     rather than crashing or silently passing.
  5. realized_by resolves-on-disk — dispatches on VALUE SHAPE before any numeric
     handling (path-shaped -> file-exists; SHA-shaped, full 40-char OR bare-short e.g.
     `b812d89`/`b6143a5` -> `git cat-file -e <sha>` existence check ONLY — `git
     rev-parse --verify` is deliberately not used as a fallback, since it echoes back
     any syntactically SHA-shaped string as "verified" even when no such object
     exists; literal "inline" (or a future sentinel) -> resolves-true). This is the
     exact hazard named
     in the auto-memory lesson `realized-by-short-sha-scientific-notation-trap`: a bare
     short SHA like `717e385` must never fall through to a naive numeric-coercion path
     (where it would be misread as scientific notation and become `Infinity`).

  #12 memory-pointer exclusion: if a candidate's delete-eligibility basis cites ONLY a
  `~/.claude` path (no other basis reference), the candidate is downgraded to RETAIN
  and surfaced — a `~/.claude` reference is not something this repo's guard can verify
  reachability against, so it can never be the sole basis for a delete.
  6. distill_fate — both classes. `distill_fate` declares what the candidate's content
     is *owed* on delete: absent fails CLOSED (retain) unless the candidate's
     git-history actioned date is determinably on/after `DISTILL_FATE_STAMPING_CUTOVER`
     (2026-08-06 safety-hole fix — see `check_distill_fate`) / `ephemeral` (nothing owed)
     / `commitment` (owed by Guard 5's `realized_by` loop AND Guard 7's harvest-provenance
     check below) PASS; `ratification` requires a durable in-repo capture
     (`in_repo_capture:` present, resolves on disk, is a file, non-empty) or BLOCKS;
     any other value fails CLOSED. See `check_distill_fate` for the full semantics
     table.
     Spec backlink: pln-distill-delete-guard-fate-enfo-12cddd § E1
  7. harvest-provenance — both classes, but a no-op unless `distill_fate == commitment`
     (2026-07-23 Gap 2 follow-up, code-review Finding 1). None of Guard 5's three
     `realized_by` shapes (`inline`, a bare SHA, a path) verify that a `commitment`
     candidate's content actually reached `docs/wiki/**` or `docs/decisions/**` —
     `realized_by: inline` resolves True with zero external verification, and a SHA
     only proves *some* git object exists, not that it touched docs. This guard
     closes that gap independently: for a `commitment` candidate it greps
     `docs/wiki/**` + `docs/decisions/**` for the candidate's repo-relative path or
     bare basename and BLOCKS on absence. For every other `distill_fate` value
     (including absent) it is a PASS-through no-op, so it never touches the routine
     `ephemeral` deletion majority. See `check_harvest_provenance` for the full
     semantics.
     Spec backlink: pln-distill-delete-guard-fate-enfo-12cddd § E1

Output shape: `{"eligible": bool, "artifact_class": "memo"|"handoff"|None,
"blocked_by": [<guard-name>, ...]}` per candidate. An unclassifiable candidate fails
CLOSED with a synthetic "artifact-class-unresolved" blocker. The LLM consumer writes
the prose reason; this script never does.

Negative-spec: this module performs no writes. It reads frontmatter, shells out to
read-only `git`/`rg` subprocesses, and stats the filesystem — it never mutates repo
state or forms a durable store (claude-klabauter's store-less invariant holds).

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C3
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

from coordinator_core.distill._common import active_reference_guard, read_fm_field, split_frontmatter
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.wire_paths import rel_id

__all__ = [
    "GuardResult",
    "DeleteCandidate",
    "DISTILL_FATE_STAMPING_CUTOVER",
    "classify_artifact",
    "check_shipped_in",
    "check_status_actioned",
    "check_active_reference",
    "check_commitment_closure",
    "check_realized_by",
    "resolve_realized_by",
    "check_distill_fate",
    "check_harvest_provenance",
    "evaluate_candidate_detailed",
    "evaluate_candidate",
]


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,39}$")
_INLINE_SENTINELS = frozenset({"inline"})

DISTILL_FATE_STAMPING_CUTOVER = "2026-07-27"
"""Date the `distill_fate` stamping mechanism landed in THIS repo (git history,
not a guess): commit `94d07f5753` ("C1: memo.transition resolve — atomic
open -> actioned, disposition required", authored 2026-07-27 09:15:31 +0100)
introduced the atomic `distill_fate`/`in_repo_capture` stamp write inside
`memo.transition`'s actioned-transition path (Finding #11, C3 — see
`coordinator_core/ops/memo_transition.py` around the `if distill_fate:` /
`insert_fm_field(fm_text, "distill_fate", ...)` block). Established via
`git blame`/`git log -L` on that exact block plus `git log -S"distill_fate"`
and `git log -S"Finding #11"` across the file's full history: every line of
the stamp-write block traces to this single commit, and no earlier commit
touches a `distill_fate` write path anywhere in `memo_transition.py`. A memo
actioned before this date could never have received a `distill_fate` stamp
through this mechanism, regardless of caller behavior — see
`_candidate_actioned_date` and `check_distill_fate`'s absent-fate branch."""


_UNTRACKED = "untracked"
"""Sentinel `_candidate_actioned_date` returns for a candidate with ZERO git
commit history under a working `git log` (as opposed to a git-command
failure) — see that function's docstring for why this is distinct from
None."""


def _candidate_actioned_date(path: Path, repo_root: Path) -> str | None:
    """Best-effort git-history-derived proxy for "when was this candidate
    actioned": the date (YYYY-MM-DD) of the EARLIEST commit that added this
    exact path, following renames.

    Cross-repo memos move from `cross-repo/inbox/` to `cross-repo/archive/`
    (a git-tracked add, effectively a rename) at action-time
    (`archive_stamp.py`), so an archived candidate's own first commit is a
    reliable actioned-date proxy — the schema carries no dedicated
    `actioned_at` field for `status: actioned` (only the back-compat
    `action_taken`/`closed` statuses have their own timestamp fields), so
    this is the only mechanically-available signal without inventing a new
    frontmatter field.

    Three distinct outcomes, NOT collapsed to a bool:
      - a real date string -> the candidate's earliest commit date.
      - `_UNTRACKED` -> `git log` ran successfully (returncode 0) but found
        ZERO history for this exact path. This is DELIBERATELY treated as
        fail-closed (undeterminable) by `check_distill_fate`, not as a proof
        of "never committed": a shallow clone (`git fetch --depth=1`), a
        `git gc` that pruned unreachable history after a rebase/squash, or a
        filtered/sparse checkout can each make `git log --follow` return zero
        lines for a genuinely old, fully-committed memo — indistinguishable
        from "never committed" to this function. A guard whose entire purpose
        is fail-closed must not have a fast-path whose safety depends on the
        completeness of the local clone (2026-08-06 code-review Finding 1).
        The rare true-untracked case (e.g. `distill_apply_disposal.py`'s
        `deleted_untracked` leg) accepts a false-negative (retained rather
        than deleted) under this posture — cheaper than the alternative.
      - None -> the git command itself failed (non-repo `repo_root`, `git`
        missing, or any other command-level error) — genuinely
        undeterminable. Callers MUST fail CLOSED on None, never treat "can't
        determine" as "must be recent" (the whole point of Guard 6's
        fail-closed posture)."""
    try:
        from coordinator_core.win_portability import no_console_creationflags

        result = subprocess.run(
            ["git", "log", "--follow", "--format=%ad", "--date=short", "--", str(path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    dates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not dates:
        return _UNTRACKED
    return dates[-1]


@dataclass(frozen=True)
class GuardResult:
    """Outcome of a single guard check.

    `passed=True` means the guard clears (does not block) the delete. `blocked_by`
    is the guard's own name, always populated (even on pass) so callers can log
    which guards ran, not just which ones blocked.
    """

    guard: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class DeleteCandidate:
    """One candidate handed to the delete guard.

    `path` is the candidate file (handoff or cross-repo memo) on disk. `basis_refs`
    is the list of reference strings the delete-eligibility decision is *based on*
    (e.g. paths cited as "this is superseded by X") — used by the #12 memory-pointer
    exclusion check.
    """

    path: Path
    repo_root: Path
    basis_refs: tuple[str, ...] = field(default_factory=tuple)


def classify_artifact(frontmatter_text: str, path: Path, repo_root: Path) -> str | None:
    """Classify a candidate as "memo" or "handoff" (None if unresolvable).

    Frontmatter SHAPE is primary — it survives relocation (a memo swept from
    cross-repo/inbox/ to cross-repo/archive/ keeps its shape) and matches how the
    corpus actually distinguishes the two classes on disk:
      - memos carry `from:` + `to:` (foreign-authored, sender/receiver addressed);
      - handoffs carry `deployment_state:` (lifecycle-stamped, never addressed).
    Path prefix is the fallback for shape-degraded files: a `cross-repo/` segment
    -> memo, a `handoffs` segment (state/handoffs/, archive/handoffs/YYYY-MM/)
    -> handoff. A candidate neither shape nor path can place returns None — the
    caller fails CLOSED on that (blocks; an unclassifiable candidate must never be
    evaluated against a guessed guard set)."""
    has_from = bool(read_fm_field(frontmatter_text, "from"))
    has_to = bool(read_fm_field(frontmatter_text, "to"))
    if has_from and has_to:
        return "memo"
    if read_fm_field(frontmatter_text, "deployment_state"):
        return "handoff"
    try:
        parts = path.resolve().relative_to(repo_root.resolve()).parts
    except ValueError:
        parts = path.parts
    if "cross-repo" in parts:
        return "memo"
    if "handoffs" in parts:
        return "handoff"
    return None


def check_shipped_in(frontmatter_text: str) -> GuardResult:
    """Guard 1: `shipped_in:` must be present and non-empty."""
    value = read_fm_field(frontmatter_text, "shipped_in")
    if value:
        return GuardResult("shipped_in", True, value)
    return GuardResult("shipped_in", False, "shipped_in absent or empty")


def check_status_actioned(frontmatter_text: str) -> GuardResult:
    """Guard 2: `status:` must read exactly "actioned"."""
    value = read_fm_field(frontmatter_text, "status")
    if value == "actioned":
        return GuardResult("status-actioned", True, value)
    return GuardResult("status-actioned", False, f"status={value!r}, expected 'actioned'")


def check_active_reference(needle: str, repo_root: Path) -> GuardResult:
    """Guard 3: the candidate must NOT still be actively referenced under
    docs/ tasks/ archive/. A hit (still referenced) BLOCKS the delete."""
    still_referenced = active_reference_guard(needle, repo_root)
    if still_referenced:
        return GuardResult("active-reference", False, f"still referenced: {needle}")
    return GuardResult("active-reference", True, "no active references found")


def check_commitment_closure(needle: str, repo_root: Path) -> GuardResult:
    """Guard 4: commitment-closure vs `state/cross-repo-commitments`.

    FAILS LOUD when the surface is absent — never silently passes a delete because
    it "couldn't check". When present, scans every `*.yaml`/`*.yml` ledger entry
    (each a bare YAML doc with a top-level `status:` field, `open`/`closed`
    vocabulary): only a NON-closed commitment that references the candidate blocks
    it. Reference detection matches `needle` (the candidate's repo-relative
    forward-slash path) OR the candidate's bare filename against the entry's raw
    text — the filename leg matters because ledger entries cite memos by their
    delivery path (`cross-repo/inbox/...`), while an actioned candidate has been
    swept to `cross-repo/archive/` under the same filename.

    Fail-closed edges (block, never crash, never silently pass): an unreadable
    entry, an entry PyYAML cannot parse, a non-mapping doc, and a missing
    `status:` field each block with a reason naming the entry. A status other
    than `open`/`closed` is treated as open (conservative) for the reference
    check."""
    commitments_path = repo_root / "state" / "cross-repo-commitments"
    if not commitments_path.exists():
        return GuardResult(
            "commitment-closure",
            False,
            "commitment-closure: surface absent",
        )
    if not commitments_path.is_dir():
        return GuardResult(
            "commitment-closure",
            False,
            "commitment-closure: surface present but not a directory",
        )
    filename = needle.rsplit("/", 1)[-1]
    entries = sorted(
        (p for p in commitments_path.iterdir() if p.suffix in (".yaml", ".yml")),
        key=lambda p: p.name,
    )
    for entry in entries:
        try:
            raw = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return GuardResult(
                "commitment-closure",
                False,
                f"commitment-closure: unreadable ledger entry {entry.name} "
                f"({exc.__class__.__name__}) — fail-closed",
            )
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError:
            return GuardResult(
                "commitment-closure",
                False,
                f"commitment-closure: unparseable ledger entry {entry.name} — fail-closed",
            )
        if not isinstance(doc, dict):
            return GuardResult(
                "commitment-closure",
                False,
                f"commitment-closure: ledger entry {entry.name} is not a YAML mapping — fail-closed",
            )
        status = doc.get("status")
        if status is None:
            return GuardResult(
                "commitment-closure",
                False,
                f"commitment-closure: ledger entry {entry.name} has no status field — fail-closed",
            )
        if str(status).strip() == "closed":
            continue
        if needle in raw or filename in raw:
            return GuardResult(
                "commitment-closure",
                False,
                f"commitment-closure: open commitment {entry.name} references candidate",
            )
    return GuardResult("commitment-closure", True, "no open commitment references candidate")


def resolve_realized_by(
    value: str,
    repo_root: Path,
    existence_map: Mapping[str, bool] | None = None,
) -> bool:
    """Resolve a `realized_by:` value to True/False based on its SHAPE.

    Dispatch order (shape-first, before any numeric handling):
      1. literal sentinel ("inline", or a future sentinel) -> True
      2. SHA-shaped (full 40-char hex OR bare-short 7-39 char hex) -> git-verified
      3. otherwise treated as path-shaped -> file-exists check

    This ordering is deliberate: a bare short SHA like `b812d89` or `717e385` is
    hex-only and would otherwise be misread by a naive numeric-coercion path as
    scientific notation (`7.17e385` -> `Infinity`) — see the auto-memory lesson
    `realized-by-short-sha-scientific-notation-trap`. Checking SHA-shape (hex
    digits only, length 7-40) before any numeric interpretation avoids that trap
    entirely; this function never calls `float()`/`int()` on the value. SHA
    existence is verified via `git cat-file -e` only (see `_git_object_exists`).

    `existence_map` (C31, `coordinator/bin/distill-delete-guard.py`'s multi-
    candidate batching seam): OPTIONAL, keyed by lower-cased SHA, sourced from
    `_git_objects_exist`'s single-spawn batch resolution. Absent (default) ->
    behavior is byte-identical to before this parameter existed — every
    SHA-shaped value falls through to the scalar `_git_object_exists`. When
    supplied, a SHA PRESENT in the map uses its value directly (no spawn); a
    SHA ABSENT from the map still falls through to `_git_object_exists` — a
    caller-supplied map is a PREFETCH, not an authoritative closed set, so a
    map miss must never be read as "does not exist" (that would invert C29's
    fail-closed direction into a guard that blocks nothing). This function
    itself decides nothing about existence beyond that lookup-or-fallback;
    `_git_objects_exist`'s index-paired fail-closed reconciliation remains the
    only place SHA existence is actually decided."""
    value = value.strip()
    if value in _INLINE_SENTINELS:
        return True
    lowered = value.lower()
    if _FULL_SHA_RE.match(lowered) or _SHORT_SHA_RE.match(lowered):
        if existence_map is not None and lowered in existence_map:
            return existence_map[lowered]
        return _git_object_exists(lowered, repo_root)
    # Path-shaped fallback.
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.exists()


def _git_object_exists(sha: str, repo_root: Path) -> bool:
    """Return True if `sha` resolves to a real, existing git object in repo_root's
    repo.

    Uses `git cat-file -e <sha>` ONLY — deliberately does NOT fall back to
    `git rev-parse --verify`, which syntactically echoes back any well-formed
    40-char-hex-looking string (exit 0) even when no such object exists in the
    repo; that would silently defeat this guard's whole purpose (a nonexistent
    SHA passing as "resolved")."""
    from coordinator_core.win_portability import leaf_spawn_creationflags

    result = subprocess.run(
        ["git", "cat-file", "-e", sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
        **leaf_spawn_creationflags(),
    )
    return result.returncode == 0


def _git_objects_exist(shas: list[str], repo_root: Path) -> dict[str, bool]:
    """Batched sibling of `_git_object_exists`: resolve EXISTENCE for MANY shas in
    ONE `git cat-file --batch-check` spawn instead of one spawn per sha.

    PUBLISHED CONTRACT — `coordinator/bin/distill-delete-guard.py` (C31) consumes
    this next wave; do not change its accepted/returned shapes without updating
    that caller.

      - Accepts: a list of sha strings (full 40-char hex or bare-short 7-39 char
        hex, already lower-cased by the caller — this function does not
        normalize case). Duplicates are fine; each is resolved independently.
      - Returns: `{sha: bool}`, ONE entry per element of the input list
        (duplicates collapse to one dict key, value shared) — never a partial
        map. `True` means git resolved the sha to a real, existing object;
        `False` covers every other outcome (unresolvable token, ambiguous
        prefix, a `<sha> missing` batch-check record, a short-circuited/failed
        git invocation) — this function FAILS CLOSED, never treats an
        unreconciled or absent record as existing. An empty input list
        returns `{}` without spawning git.

    `--batch-check` is the correct form here (existence only, metadata-only) —
    NOT `--batch`, which additionally streams full blob content and would be
    wasted work for an existence probe (contrast `coordinator_core.coverage`
    callers that read blob content and need `--batch`).

    This is EXISTENCE batching, not RANGE batching: `cat-file --batch-check`
    resolves each stdin line independently against the object store, so
    grouping many candidate shas into one spawn is safe — it must never be
    read as license to combine distinct `git log`/`git rev-list` RANGE queries
    into one call elsewhere.

    Reconciliation is EXPLICIT (mirrors `emit/sections/handoffs.py`'s
    `_resolve_shipped_in_dates`, and the empirically-verified one-line-per-token,
    input-order-preserving output shape documented on `coverage._batch_check_hex_tokens`):
    each of the N requested shas is paired index-wise with its corresponding
    stdout line, and a line is only counted as "exists" when it parses as
    `<sha> <type>` (this call's `--batch-check` format string is
    `%(objectname) %(objecttype)`, deliberately omitting `%(objectsize)` —
    an existence probe needs no size) with `<type>` in `_GIT_OBJECT_TYPES` —
    a `<token> missing` (or `<token> ambiguous`) line, or a shortfall in the
    number of output lines (fewer lines than requested shas — a truncated or
    failed spawn), is never read as "exists". A wrongly-reported existing
    object is a guard permitting a deletion it should have blocked, so every
    unreconciled case defaults to False, never True.

    Windows stdin note: feeds the token list via `input=<bytes>`, NOT
    `text=True` — a text-mode stdin pipe on Windows translates each `\n` to
    `\r\n`, corrupting the one-token-per-line feed `--batch-check` expects
    (the exact hazard chunk C10 hit). Output is decoded manually and CRLF is
    normalized before splitting.
    """
    if not shas:
        return {}
    from coordinator_core.win_portability import leaf_spawn_creationflags

    unique_shas = list(dict.fromkeys(shas))
    stdin_payload = ("\n".join(unique_shas) + "\n").encode("utf-8")
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input=stdin_payload,
            cwd=repo_root,
            capture_output=True,
            **leaf_spawn_creationflags(),
        )
    except OSError:
        return {sha: False for sha in unique_shas}
    if result.returncode != 0:
        return {sha: False for sha in unique_shas}

    stdout_text = result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = stdout_text.splitlines()

    exists: dict[str, bool] = {}
    for sha, line in zip(unique_shas, lines):
        parts = line.split()
        exists[sha] = len(parts) == 2 and parts[1] in _GIT_OBJECT_TYPES
    for sha in unique_shas[len(lines):]:
        exists[sha] = False
    return exists


_GIT_OBJECT_TYPES = frozenset({"blob", "tree", "commit", "tag"})
"""Valid `git cat-file --batch-check` `%(objecttype)` values — mirrors
`coordinator_core.coverage._GIT_OBJECT_TYPES`, kept as an independent module-
local constant rather than a cross-module import (this module has no other
dependency on `coverage.py`, and the value is a stable git-format-string
vocabulary, not project-specific behavior)."""


def check_realized_by(
    frontmatter_text: str,
    repo_root: Path,
    existence_map: Mapping[str, bool] | None = None,
) -> GuardResult:
    """Guard 5: `realized_by:` must resolve on disk (per resolve_realized_by shape
    dispatch). Absence of the field entirely is a distinct condition from an
    unresolvable value — both block, with different detail strings.

    Uses `read_fm_field_unquoted`, not the raw `read_fm_field`: an all-digit
    short SHA (e.g. `44379324`) is written with `numeric_quoting=True`
    (`memo_transition.py`) and round-trips through `read_fm_field` still
    quoted (`'44379324'`), which `resolve_realized_by`'s SHA regex does not
    match — a legitimately-resolvable value would otherwise be reported as
    not resolving on disk and falsely block an eligible delete.

    `existence_map` is forwarded verbatim to `resolve_realized_by` (see that
    function's docstring) — absent means no behavior change; present is a
    prefetch, never an authoritative closed set."""
    value = read_fm_field_unquoted(frontmatter_text, "realized_by")
    if not value:
        return GuardResult("realized_by", False, "realized_by absent or empty")
    if resolve_realized_by(value, repo_root, existence_map=existence_map):
        return GuardResult("realized_by", True, value)
    return GuardResult("realized_by", False, f"realized_by does not resolve on disk: {value}")


def _absent_fate_result(passed: bool, detail: str) -> GuardResult:
    """Shared constructor for `check_distill_fate`'s absent-fate branch (2026-08-06
    code-review Finding — the four near-identical `GuardResult("distill-fate", ...)`
    blocks repeated the cutover-citation string-building pattern independently).
    Appends the `DISTILL_FATE_STAMPING_CUTOVER` citation uniformly so every one of
    the four outcomes reads against the module docstring's four-way table."""
    return GuardResult("distill-fate", passed, f"{detail} (cutover: {DISTILL_FATE_STAMPING_CUTOVER})")


def check_distill_fate(frontmatter_text: str, path: Path, repo_root: Path) -> GuardResult:
    """Guard 6: `distill_fate` must be honored before a candidate is deleted.

    `distill_fate` is the fate-stamp declaring what a candidate's content is
    *owed* on delete — see the module docstring's Guard 6 summary for the
    full table. This function is the fail-closed enforcement of that table:

      - absent/empty -> the SAFETY-HOLE FIX (2026-08-06, cross-repo ask
        `2026-08-06-example-retrieval-repo-em-distill-fate-coverage-and-legacy-log-
        reader.md` item 1, ruling (b)): no longer an unconditional PASS. The
        candidate's git-history-derived actioned date
        (`_candidate_actioned_date`) is compared against
        `DISTILL_FATE_STAMPING_CUTOVER`. Undeterminable OR strictly before
        the cutover -> BLOCK (retain) — the pre-stamping cohort has zero
        ephemerality signal and must never be treated as delete-eligible by
        default. Only a candidate whose actioned date is determinably ON OR
        AFTER the cutover passes here (an omitted stamp on a fresh memo is a
        caller-discipline gap outside this guard's remit, not a legacy
        grandfather case).
      - `ephemeral` -> PASS (nothing durable owed).
      - `commitment` -> PASS from this guard alone, deferring to Guard 5
        (`check_realized_by`) AND Guard 7 (`check_harvest_provenance`): this
        guard adds no independent block for `commitment` so the receipt does
        not double-report the same failure under three guard names, but
        `commitment`'s durable-capture obligation is NOT fully closed by
        Guard 5 alone (see `check_harvest_provenance`'s docstring for why —
        none of `realized_by`'s three accepted shapes verify the content
        actually reached `docs/wiki/**`/`docs/decisions/**`).
      - `ratification` -> the teeth: requires `in_repo_capture:` present,
        resolving on disk to a FILE (repo-relative paths resolve against
        `repo_root`; a directory does not count), AND the resolved file
        non-empty at check time. Any of {absent, unresolved, not-a-file,
        empty} BLOCKS.
      - any other value -> BLOCK, fail-closed on an unrecognized fate (never
        silently pass an unknown stamp). The comparison is exact-case
        (`value == "ephemeral"`, etc.) with no normalization — a case-mismatched
        stamp such as `Ratification` falls through to this fail-closed BLOCK
        branch by design, the same undocumented-but-safe idiom
        `check_status_actioned` already relies on.

    Content-equality limitation: the mechanically-enforceable invariant is
    *path exists + non-empty at check time*, not deep content-equality
    against the value the memo/handoff carried at stamp time — this module
    cannot reconstruct stamp-time content, so it does not attempt a body-diff.

    TOCTOU note: `evaluate_candidate_detailed` (this guard's only caller) is
    re-run at delete-apply time by `distill_apply_disposal.compute_apply_plan`
    as its own TOCTOU re-verify, so the `ratification` durable-capture check
    below is automatically re-checked immediately before deletion — no
    separate apply-side code is required to satisfy "verified present at
    delete time, not merely at stamp time."

    Harvest-provenance (Gap 2, RESOLVED 2026-07-23 code-review Finding 1):
    formerly left as an open reviewer question here — the fold into
    `realized_by` alone was found insufficient (`realized_by: inline` and a
    bare code SHA both resolve True with zero verification that content ever
    reached `docs/wiki/**`/`docs/decisions/**`). `check_harvest_provenance`
    (Guard 7) now independently closes this gap for `commitment` candidates;
    it is a no-op for every other `distill_fate` value so it never touches
    the routine `ephemeral` majority of a distill run.
    """
    value = read_fm_field(frontmatter_text, "distill_fate")
    if not value:
        actioned_date = _candidate_actioned_date(path, repo_root)
        if actioned_date is None:
            return _absent_fate_result(
                False,
                "distill_fate absent and actioned-date undeterminable — fail-closed retain",
            )
        if actioned_date == _UNTRACKED:
            # Review: code-reviewer Finding 1 (2026-08-06) — fail-closed, matching
            # _candidate_actioned_date's docstring: a zero-history result is
            # indistinguishable from a shallow clone / gc-pruned / sparse-checkout
            # false negative on a genuinely old, fully-committed candidate. The
            # rare true-untracked case accepts a false-negative (retained rather
            # than deleted) — cheaper than the alternative.
            return _absent_fate_result(
                False,
                "distill_fate absent and candidate has no git history (untracked) — "
                "fail-closed retain (indistinguishable from a shallow/pruned clone "
                "masking a pre-cutover candidate)",
            )
        if actioned_date < DISTILL_FATE_STAMPING_CUTOVER:
            return _absent_fate_result(
                False,
                f"distill_fate absent and candidate predates stamping cutover "
                f"(actioned_date={actioned_date}) — fail-closed retain",
            )
        return _absent_fate_result(
            True,
            f"distill_fate absent but actioned_date={actioned_date} is on/after stamping cutover",
        )
    if value == "ephemeral":
        return GuardResult("distill-fate", True, "distill_fate=ephemeral (nothing durable owed)")
    if value == "commitment":
        return GuardResult(
            "distill-fate",
            True,
            "distill_fate=commitment (durable-capture obligation deferred to realized_by "
            "and harvest-provenance)",
        )
    if value == "ratification":
        capture = read_fm_field(frontmatter_text, "in_repo_capture")
        if not capture:
            return GuardResult(
                "distill-fate",
                False,
                "distill_fate=ratification but in_repo_capture is absent or empty",
            )
        target = Path(capture)
        if not target.is_absolute():
            target = repo_root / target
        # Review: code-reviewer — Finding 2: `.exists()` alone is also True for a
        # directory (whose `.stat().st_size` is platform-dependent, often non-zero),
        # so a mis-stamped `in_repo_capture:` pointing at a directory could pass this
        # guard despite capturing no actual content. `.is_file()` closes that.
        if not target.exists() or not target.is_file():
            return GuardResult(
                "distill-fate",
                False,
                f"distill_fate=ratification but in_repo_capture does not resolve on disk: {capture}",
            )
        if target.stat().st_size == 0:
            return GuardResult(
                "distill-fate",
                False,
                f"distill_fate=ratification but in_repo_capture target is empty: {capture}",
            )
        return GuardResult("distill-fate", True, f"in_repo_capture resolves and is non-empty: {capture}")
    return GuardResult("distill-fate", False, f"unrecognized distill_fate: {value}")


_HARVEST_PROVENANCE_SCOPE = ("docs/wiki", "docs/decisions")
"""Subtrees `check_harvest_provenance` searches for a `commitment` candidate's
citation — deliberately narrower than `ACTIVE_REFERENCE_SCOPE` (which also covers
tasks/ and archive/): a citation anywhere in docs/wiki or docs/decisions is the
actual "content survived somewhere readable" signal Gap 2 asks for; a tasks/ or
archive/ mention is process scaffolding, not durable capture."""


def check_harvest_provenance(frontmatter_text: str, path: Path, repo_root: Path) -> GuardResult:
    """Guard 7: harvest-provenance — closes Gap 2 for `commitment` candidates
    (2026-07-23 code-review Finding 1).

    A no-op (PASS, "n/a") for every `distill_fate` value OTHER than
    `commitment` — including absent — so this guard never touches the routine
    `ephemeral` majority of a distill run; `distill_fate` is read internally,
    the same idiom `check_distill_fate` uses, rather than trusting a caller to
    pre-gate the call.

    For `commitment`: none of Guard 5's (`check_realized_by`) three accepted
    `realized_by` shapes verify that the candidate's *content* actually
    reached a durable, readable location — `inline` resolves True with zero
    external check, and a SHA only proves *some* git object exists (e.g. the
    code commit that shipped the feature), not that it touched docs. This
    guard is the independent check: it searches `docs/wiki/**` +
    `docs/decisions/**` (recursively) for a citation of the candidate's
    repo-relative forward-slash path OR its bare basename, and BLOCKS on
    absence. Reuses `_common.active_reference_guard`'s `rg --fixed-strings`
    scan (SECURITY-ADJACENT: the needle passed to it must be forward-slash —
    see `evaluate_candidate_detailed`'s note on why a native-separator needle
    fails OPEN on Windows; this guard computes its needle the identical way).
    The two needles are searched under DIFFERENT `exclude_provenance_blocks`
    settings — see the two call sites below — because a full path inside a
    provenance block is trustworthy proof but a bare basename inside one is
    not (2026-08-29 code-review Finding 1: a same-basename-different-path
    artifact could otherwise falsely satisfy this guard for an unrelated
    candidate).

    Fail-closed: if `docs/wiki/` and `docs/decisions/` are BOTH absent on
    disk, that is "no citation found" -> BLOCK, consistent with
    `check_distill_fate`'s `ratification` branch — a guard that cannot check
    a durable-capture claim must never silently pass the delete because it
    couldn't check."""
    value = read_fm_field(frontmatter_text, "distill_fate")
    if value != "commitment":
        return GuardResult(
            "harvest-provenance",
            True,
            "harvest-provenance: n/a (distill_fate != commitment)",
        )

    try:
        needle = rel_id(path, repo_root)
    except ValueError:
        needle = path.name
    basename = path.name

    # `exclude_provenance_blocks=False` is load-bearing, not a default being restated.
    # The canonical durable-capture shape IS a provenance-marker block: a docs/wiki or
    # docs/decisions page recording where this artifact's content went, via its own
    # `archived_handoff:` / `cross_repo_memo:` entry. Guard 3 excludes that block because a
    # tombstone is not a dependency; this guard REQUIRES it because a tombstone is exactly
    # the proof it looks for. Leaving the exclusion on here makes Guard 7's own evidence
    # invisible to it — fail-closed, so no data loss, but the artifact becomes permanently
    # undeletable, which is the DR-111 self-pinning defect relocated rather than fixed.
    cited = active_reference_guard(
        needle,
        repo_root,
        scope=_HARVEST_PROVENANCE_SCOPE,
        exclude_provenance_blocks=False,
    )
    if not cited and basename != needle:
        # Basename fallback keeps the SAFE default (exclude_provenance_blocks=True,
        # i.e. not passed here at all): a bare basename is not path-qualified, so a
        # provenance block belonging to a DIFFERENT harvested artifact that merely
        # shares this candidate's filename must not count as proof. A basename
        # citation in prose (outside any provenance block) still counts — only the
        # provenance-block reading of the basename needle is excluded. The full-path
        # needle above is unaffected: a provenance block citing the candidate's own
        # full repo-relative path IS durable-capture proof and must keep counting.
        cited = active_reference_guard(
            basename,
            repo_root,
            scope=_HARVEST_PROVENANCE_SCOPE,
        )
    if cited:
        return GuardResult(
            "harvest-provenance",
            True,
            f"citation found under docs/wiki or docs/decisions: {needle}",
        )
    return GuardResult(
        "harvest-provenance",
        False,
        f"harvest-provenance: no docs/wiki or docs/decisions citation found for "
        f"{needle} (or basename {basename})",
    )


def _cites_only_memory_pointer(basis_refs: tuple[str, ...]) -> bool:
    """#12 memory-pointer exclusion check: True if `basis_refs` is non-empty and
    EVERY entry is a `~/.claude`-rooted path (no other basis reference present)."""
    if not basis_refs:
        return False
    return all(_is_memory_pointer(ref) for ref in basis_refs)


def _is_memory_pointer(ref: str) -> bool:
    return ref.startswith("~/.claude") or "/.claude/" in ref or ref.startswith(".claude/")


def evaluate_candidate_detailed(
    path: Path,
    repo_root: Path,
    existence_map: Mapping[str, bool] | None = None,
) -> tuple[str | None, list[GuardResult]]:
    """Run the CLASS-APPLICABLE mechanical guard set (per `classify_artifact`)
    against one on-disk candidate and return `(artifact_class, guard_results)`
    — the single dispatch-order authority both `evaluate_candidate` (below,
    collapses to `blocked_by` names for the delete-guard's own callers) and
    `distill_disposal_manifest.evaluate_candidate_receipts` (full per-guard
    receipts for the C9 manifest's AC3 requirement) consume, so exactly one
    guard-selection/ordering implementation exists (2026-07-23 architecture
    review § 2a — this function replaces a prior duplicate reimplementation
    that had hardened into distill.apply_disposal's act-time TOCTOU
    re-verify).

    Handoffs run `shipped_in` (never `status-actioned`); memos run
    `status-actioned` (never `shipped_in`); the shared guards
    (`active-reference`, `commitment-closure`, `realized_by`,
    `distill-fate`, `harvest-provenance`) run for both classes —
    `harvest-provenance` is a no-op unless `distill_fate == commitment`
    (checked internally by the guard itself, not gated by this caller).
    `artifact_class` is None when
    unclassifiable — callers are
    responsible for fail-closed handling of that case (this function does not
    synthesize an "artifact-class-unresolved" blocker itself; that is a
    caller-level concern, since the #12 memory-pointer exclusion is also
    caller-level and the two synthetic blockers are conventionally applied
    together).

    Does NOT apply the #12 memory-pointer exclusion — that check is keyed on
    `basis_refs`, which is a per-caller delete-eligibility-basis concern, not
    part of the guard dispatch order itself; each caller applies it separately
    (both callers do, identically).

    `existence_map` (C31): forwarded verbatim to `check_realized_by` /
    `resolve_realized_by`. Absent -> no behavior change; present -> a
    multi-candidate caller's batched prefetch (see `resolve_realized_by`'s
    docstring for the fail-closed-on-miss contract)."""
    text = path.read_text(encoding="utf-8")
    fm_split = split_frontmatter(text)
    frontmatter_text = fm_split.fm_text if fm_split is not None else ""

    # Review: workflow-review (2026-07-12) — previously passed candidate.path.name
    # (bare filename), a strictly looser rg needle than sidecar_sweep.py's sibling
    # caller of the same shared guard (which passes the repo-relative path). A bare
    # filename over-matches for generically-named candidates; normalized to
    # repo-relative for consistency + precision with the sidecar-sweep caller.
    #
    # SECURITY-ADJACENT — the needle MUST be forward-slash. `active_reference_guard`
    # feeds it to `rg --fixed-strings`, and every in-repo document spells its
    # references with '/'. A native-separator needle (`docs\plans\x.md` on Windows)
    # therefore matches NOTHING, the guard returns "no active references found",
    # and a still-referenced artifact is reported DELETE-ELIGIBLE. That is the
    # guard failing OPEN on Windows, not a cosmetic id nit.
    try:
        needle = rel_id(path, repo_root)
    except ValueError:
        needle = path.name

    artifact_class = classify_artifact(frontmatter_text, path, repo_root)

    guard_results: list[GuardResult] = []
    if artifact_class == "handoff":
        guard_results.append(check_shipped_in(frontmatter_text))
    elif artifact_class == "memo":
        guard_results.append(check_status_actioned(frontmatter_text))
    guard_results.extend(
        [
            check_active_reference(needle, repo_root),
            check_commitment_closure(needle, repo_root),
            check_realized_by(frontmatter_text, repo_root, existence_map=existence_map),
            check_distill_fate(frontmatter_text, path, repo_root),
            check_harvest_provenance(frontmatter_text, path, repo_root),
        ]
    )
    return artifact_class, guard_results


def evaluate_candidate(
    candidate: DeleteCandidate,
    existence_map: Mapping[str, bool] | None = None,
) -> dict:
    """Run the candidate's CLASS-APPLICABLE mechanical guards (via
    `evaluate_candidate_detailed`) + the #12 memory-pointer exclusion. Returns
    `{"eligible": bool, "artifact_class": "memo"|"handoff"|None, "blocked_by":
    [<guard-name>, ...]}`.

    An unclassifiable candidate fails CLOSED with a synthetic
    `"artifact-class-unresolved"` blocker — never evaluated against a guessed
    guard set.

    `blocked_by` lists only the guards that BLOCKED (did not pass); an empty list
    with `eligible=True` means every applicable guard cleared. The #12 exclusion,
    when it fires, appends a synthetic `"memory-pointer-exclusion"` blocker and
    forces `eligible=False` regardless of the other guards' outcome (RETAIN +
    surface).

    `existence_map` (C31, optional): a caller's batched-prefetch of
    SHA-shaped `realized_by:` existence (see `resolve_realized_by`'s
    docstring for the fail-closed-on-miss contract). Absent -> byte-identical
    to pre-C31 behavior; `coordinator/bin/distill-delete-guard.py` is the
    multi-candidate caller that supplies it.
    """
    artifact_class, guard_results = evaluate_candidate_detailed(
        candidate.path, candidate.repo_root, existence_map=existence_map
    )

    blocked_by = [r.guard for r in guard_results if not r.passed]

    if artifact_class is None:
        blocked_by.append("artifact-class-unresolved")

    if _cites_only_memory_pointer(candidate.basis_refs):
        blocked_by.append("memory-pointer-exclusion")

    return {
        "eligible": len(blocked_by) == 0,
        "artifact_class": artifact_class,
        "blocked_by": blocked_by,
    }
