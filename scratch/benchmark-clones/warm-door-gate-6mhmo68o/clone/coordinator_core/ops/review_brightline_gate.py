"""
coordinator_core.ops.review_brightline_gate — mechanical partition-vs-single
review gate for /workstream-complete Step 2.9.

Prints: `range=… loc=… commits=… surfaces=… files=… [filtered_to=…] VERDICT={PARTITION-MANDATORY|single-reviewer-ok|indeterminate}`
to stdout, reading the diff over a git range (or a `--session-id`-filtered
subset of it) and comparing gross LOC / commit count / surface-bucket count
against fixed thresholds. `VERDICT=indeterminate` is the `--session-id`
zero-match outcome (see below) — it means the gate examined nothing, not
that what it examined was small; it is neither of the two measured verdicts
and a consumer must not coerce it into either.

Thresholds (any one trips PARTITION-MANDATORY): loc>=500 (gross insertions+
deletions), commits>=5, surfaces>=4. `files=` is reported for operator
visibility only — NOT a gate (2026-06-09 recalibration; file count is a blunt
review-cost proxy, dropped in favor of commits/surfaces).

Surfaces = file-role buckets (test/shell/python/js/config/doctrine/cpp/other),
classified by path pattern — NOT directories.

CLI contract (unchanged from the bash oracle):
    review-brightline-gate.sh [--session-id <id>] [<git-range>]
    range default: `git merge-base origin/main HEAD`..HEAD

--session-id <id> filters the range to commits whose trailer matches
`^Session-Id: <id>$` (prepare-commit-msg hook injects this trailer per
docs/wiki/workstream-complete-review.md), recomputing all four metrics over
the filtered commit set only. A zero-match against the resolved `range_`
first retries against a session-aware floor (C2, 2026-08-08,
`_resolve_session_floor`) — the session's own earliest commit reachable
from HEAD, found by an unbounded trailer search rather than the (peer-
advanceable) merge-base — before falling back to the vacuous outcome
(`VERDICT=indeterminate`, exit 0, stderr note) — NOT the same as a `range`
resolution failure (exit 1).

Exit codes: 0 — verdict printed (incl. vacuous zero-match). 1 — usage error
(bad --session-id, missing --session-id argument, unresolvable origin/main,
or a die-silent gate — see negative-spec).

Port of: review-brightline-gate.sh (DoE b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Spec backlink: docs/plans/2026-06-15-brightline-session-scope-fix.md § C3

Cross-repo session scope (XB-6, 2026-07-29) is REMOVED with the
`--from-handoff` mode it served (state/kill-ledger.md K-007, 2026-08-19, PM
ruling). It summed the Session-Id-matched diff across every repo a session
wrote into, feeding `_compute_session_oracle`; both that oracle and the
chain-terminal gate that consumed it are gone. The surviving session-scoped
and unfiltered modes below have always been single-repo.

Negative-spec (pre-existing bash-oracle quirks, faithfully REPRODUCED, not fixed):
    - DIE-SILENT GATE: the bash oracle runs under `set -euo pipefail`. Its
      `loc=` computation pipes `git diff --shortstat` through
      `grep -oE '[0-9]+ insertion|[0-9]+ deletion'` with NO catch-all rule —
      if that regex matches ZERO times (an invalid range where git errors, OR
      a syntactically-valid range whose diff is genuinely empty, e.g.
      `HEAD..HEAD`), grep exits 1, pipefail propagates that exit code through
      the assignment, and the whole script dies immediately: exit 1, ZERO
      stdout, ZERO stderr (git's own error text was already discarded via
      `2>/dev/null`; no explicit diagnostic is printed on this path). This
      port reproduces that: `_sum_loc` returns `matched=False` on zero
      matches and the caller returns 1 with nothing printed — a genuinely
      empty-but-valid range (or a bad range) is INDISTINGUISHABLE from each
      other in output, exactly as in the original.
    - Same die-silent gate applies a second time on the `--session-id` path:
      after the (`||`-guarded, non-fatal) `filtered_diff` fetch, the `loc=`
      recompute over `filtered_diff` is UNGUARDED — zero insertion/deletion
      matches there also silently kills the script. A THIRD unguarded gate
      exists on the `files=` grep (`grep -E '^\\s*\\S.*\\|'`) immediately
      after `loc=` succeeds — if `filtered_diff` has stat-summary lines but
      no per-file `path | N +++---` lines, that grep also finds zero matches
      and dies silently. Both gates are reproduced (see `_session_scoped`).
    - The `--session-id` zero-MATCH branch (no commits carry the trailer) is
      explicitly guarded in the bash source and is NOT a die-silent case —
      it prints `VERDICT=indeterminate` (2026-08-08 fix — the bash oracle
      and the original port both emitted a fabricated `VERDICT=single-
      reviewer-ok` here on zero examined commits; see
      docs/plans/2026-08-08-the-gate-says-ok-when-it-could-not-look.md) to
      stdout plus the existing stderr note, exit 0. This is a deliberate,
      NON-faithful deviation from the bash oracle's vacuous-path token —
      everything else in this negative-spec section reproduces the oracle's
      quirks faithfully; this one line does not, because the quirk it
      reproduced was a correctness defect (a permissive verdict on zero
      evidence), not a faithfully-portable idiosyncrasy. Do not conflate
      this branch with the die-silent gates above; they are reachable only
      once `filtered_count > 0`.
    - `git show --stat --format=` over the filtered SHAs is invoked with all
      SHAs as a single argv batch, not xargs' possible multi-invocation
      batching under ARG_MAX — immaterial for realistic corpora sizes but a
      literal implementation difference from the bash `xargs` pipeline.
    - Unfiltered-path `files=`/`commits=`/`surfaces=` computations have no
      grep-with-required-match step (unlike the session-scoped path) so they
      carry no independent die-silent gate of their own — they only die as a
      side effect of the shared `loc=` gate already having failed first
      (bash `set -e` aborts before those lines are even reached).
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from coordinator_core.coverage import (
    _is_planning_artifact_path,
    _resolve_numstat_row_path,
)
from coordinator_core.win_portability import no_console_creationflags

_PROG = "review-brightline-gate.sh"  # literal program-name prefix — matches bash oracle stderr

LOC_THRESHOLD = 500
COMMITS_THRESHOLD = 5
SURFACES_THRESHOLD = 4


_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_LOC_RE = re.compile(r"(\d+) insertion|(\d+) deletion")
_TEST_DIR_RE = re.compile(r"(^|/)tests?/")

# chain_oracle (C3) defensive file-granularity noise exclusion — a commit is
# noise IFF every file it touches matches one of these path rules; a MIXED
# commit keeps its code-path LOC and drops only the noise-path LOC. Two
# categories per the C1 contract: generated/vendored artifacts, and
# lifecycle/memo bookkeeping (handoffs, outboxes, lessons, trackers, pure
# archive/ moves, review-trail JSON, subagent-share sidecars, ceremony
# records, cross-repo inbox/archive memo files). Deliberately file-
# granularity, not commit-message-prefix matching — see the module
# docstring's chain_oracle negative-spec.
#
# state/review-trail/, state/subagent-share/, and state/ceremony/ were added
# 2026-08-04 after field evidence showed `chain_oracle` inflating on
# ceremony-emitted bookkeeping: one real chain range measured loc=15911
# across 286 files, of which 227 files / 31043 insertions were pure
# review-trail JSON, subagent-share sidecars, memo files, and handoff
# frontmatter (59 files / ~9100 insertions were substantive code/tests/
# docs) — reviewers_suggested=32 against plan_oracle=4, an inflated headline
# easy to dismiss. `cross-repo/(inbox|archive)/` (the memo channel; see
# `cross-repo/README.md`) is scoped to those two subdirs only, NOT the bare
# `cross-repo/` prefix, so a hand-edit to `cross-repo/README.md` itself
# stays reviewable. `state/[^/]+-outbox/` already covers
# `state/memo-outbox/` via the existing `-outbox` alternation — verified,
# not re-added. `state/sizings/` and `state/audits/` were measured and
# EXCLUDED from this list: both carry human/EM-authored routing rationale
# and analysis prose (scout_evidence, intent, audit findings), not
# mechanical bookkeeping — see the memo backlink below for the full
# before/after measurement on this repo.
# Spec backlink: cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-mandatory-does-not-halt.md
#   § "Two smaller observations" — `chain_oracle` counts ceremony bookkeeping as reviewable LOC.
_NOISE_BASENAMES = frozenset({"package-lock.json", "poetry.lock", "pnpm-lock.yaml", "bun.lockb"})
_NOISE_SUFFIXES = (".lock", ".pyc", ".min.js", ".min.css")
_NOISE_PATH_RE = re.compile(
    r"(^|/)("
    r"__pycache__|dist|build|vendored?"
    r")(/|$)"
)
_NOISE_LIFECYCLE_RE = re.compile(
    r"^("
    r"state/handoffs/"
    r"|state/[^/]+-outbox/"
    r"|state/lessons"
    r"|state/review-trail/"
    r"|state/subagent-share/"
    r"|state/ceremony/"
    r"|cross-repo/(inbox|archive)/"
    r"|archive/"
    r")"
)
_NOISE_TRACKER_RE = re.compile(r"^docs/.*-tracker\.md$")

# chain_oracle planning-artifact de-weight (C7, AC8) — a plan/research/
# problem-framing document or its own sidecar is real review obligation
# (unlike `_is_noise_path`, which drops LOC entirely), but it is NOT the
# same review cost per line as code: a 2799-line plan drove the un-patched
# chain_oracle to `1 + 2799//500 = 6`, a code-reviewer-count recommendation
# against a plan whose own `plan_oracle` (which excludes doc-edit rows by
# design — see `_CODE_BEARING_KINDS`) was 2. De-weight, not exclude:
# `chain_loc` sums code-path LOC at full weight plus planning-artifact LOC
# scaled by `_PLANNING_LOC_WEIGHT`, so a large plan still nudges the
# recommendation upward without being read as if it were code.
#
# `_PLANNING_ARTIFACT_PATH_PREFIXES` and `_is_planning_artifact_path` are
# imported from `coordinator_core.coverage` (both C2 and this module's own
# chain_oracle now on disk) — a single source for the prefix list so the
# brightline gate's reviewer-count heuristic and the coverage gate's
# crediting classifier cannot disagree about which paths are planning
# artifacts. `_PLANNING_LOC_WEIGHT` below stays LOCAL: it is brightline's
# own de-weighting heuristic, not part of the shared classification.
#
# Do NOT wire the shared predicate into `_is_noise_path` — a planning-artifact
# commit is not noise (AC9: the gate stays non-vacuous; a planning artifact
# still owes a review), it is merely cheaper-per-line than code.
# Spec backlink: pln-planning-artifacts-are-a-third-77111f § C7, AC8
#
# SUPERSEDED IN PART (C1a, 2026-08-12): `_is_prose_bearing_path` now runs
# BEFORE this weight is ever applied (see the `countable` filter in
# `_compute_chain_oracle`/`_compute_session_oracle_single`/the session-scoped
# range path) and fully excludes `.md`/`.yaml`/`.yml` — every planning-
# artifact path in practice, since all four `_PLANNING_ARTIFACT_PATH_PREFIXES`
# hold only `.md` files today. This weight now only still applies to a
# hypothetical non-prose-bearing file under a planning prefix (e.g. a binary
# or `.json` sidecar) — a narrower but not dead case, and left as-is per this
# module's Anti-scope ("leave `_PLANNING_LOC_WEIGHT`/`_is_planning_artifact_path`
# exactly as they are").
_PLANNING_LOC_WEIGHT = 0.2  # 1 planning-artifact LOC counts as 0.2 chain_loc


_CHAIN_SHOW_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CHAIN_NUMSTAT_RE = re.compile(r"^(-|\d+)\t(-|\d+)\t(.+)$")
# `git show --raw`'s per-file line: `:oldmode newmode oldsha newsha STATUS[score]\told[\tnew]`.
# Captures just the single-letter status (A/M/D/R/C) — the similarity score
# suffix on R/C rows (e.g. `R100`) is discarded. See `_parse_show_numstat`.
_CHAIN_RAW_STATUS_RE = re.compile(r"^:\d+ \d+ \S+ \S+ (\w)\d*\t")

# AC2/AC3 (C2, 2026-08-12): change-substance weighting for the three
# accumulation loops factored into `_accumulate_countable_rows` — a row's
# raw added+deleted LOC is scaled by whether it is a content-identical
# rename/move (AC2's explicit "breadth without burden" example) or genuine
# authored content (create/modify/delete/renamed-with-edits), rather than
# counted uniformly. MEASURED, not invented (AC3): over this branch's own
# history (`origin/main..HEAD`, 362 commits, 1054 changed-file rows — 522
# created, 502 modified, 1 deleted, 29 renamed), a three-way created/
# modified/deleted split produced IDENTICAL totals to this two-way rename/
# everything-else split (34553 either way): every renamed row in that
# corpus was itself prose-bearing (`.md`/`.yaml`) and already excluded by
# `_is_prose_bearing_path` before substance weighting runs, and the single
# deletion was prose-bearing too. See the C2 dispatch report's AC3 table.
# TWO constants, not three, per AC3's "do not force three to exist if two
# suffice." Deletions are NOT exempted by this: a deleted file lands in
# `_SUBSTANCE_WEIGHT_CONTENT` at full weight, same as a creation or a
# modification — never zeroed.
_SUBSTANCE_WEIGHT_RENAME = 0.0  # content-identical rename/move (status "R", 0 added + 0 deleted): already 0 raw LOC — named explicitly so the invariant is a deliberate constant, not an arithmetic accident of a+d
_SUBSTANCE_WEIGHT_CONTENT = 1.0  # created / modified / deleted / renamed-with-edits: genuine authored change, counted at the code-loc baseline


def _substance_weight(status: str, added: int, deleted: int) -> float:
    """AC2/AC3 change-substance weight for one numstat row. `status` is the
    single-letter git raw status (`""` if `_parse_show_numstat` could not
    pair a raw row to this numstat row — treated as content, the safe/
    never-under-count direction). Only a content-identical rename (status
    `"R"`, both counts zero) gets the reduced weight: a rename that also
    edited lines is real authored change and stays at full weight, since
    the added/deleted counts git reports for a rename already cover only
    the genuinely changed lines, not the whole moved file."""
    if status == "R" and added == 0 and deleted == 0:
        return _SUBSTANCE_WEIGHT_RENAME
    return _SUBSTANCE_WEIGHT_CONTENT

_JS_EXTS = (".ts", ".js", ".tsx", ".jsx")
_CONFIG_EXTS = (".json", ".yaml", ".yml", ".toml")
_DOCTRINE_EXTS = (".md", ".mdx")
_CPP_EXTS = (".cpp", ".h", ".hpp", ".c")


def _run_git(args: List[str], cwd: Optional[str] = None) -> Tuple[str, int]:
    """Run `git <args>`, stdout captured, stderr discarded (mirrors `2>/dev/null`).

    `cwd` (added XB-6) runs the command against a DIFFERENT repo than the
    process cwd — used to sum session_oracle across sibling repos without
    ever `cd`-ing the process itself. `None` (the default) preserves the
    original process-cwd behavior every prior caller relies on.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _run_git: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return "", 127
    return proc.stdout, proc.returncode


def classify_surface(path: str) -> str:
    """Map a changed-file path to its review-surface bucket.

    Rule order matches the bash awk chain verbatim — test-directory match is
    checked FIRST, so a `tests/foo.py` file classifies as "test", not
    "python".
    """
    if _TEST_DIR_RE.search(path):
        return "test"
    if path.endswith(".sh"):
        return "shell"
    if path.endswith(".py"):
        return "python"
    if path.endswith(_JS_EXTS):
        return "js"
    if path.endswith(_CONFIG_EXTS):
        return "config"
    if path.endswith(_DOCTRINE_EXTS):
        return "doctrine"
    if path.endswith(_CPP_EXTS):
        return "cpp"
    return "other"


#: Public alias — cross-module callers (e.g.
#: `backlog_grind_assemble.readers_mise._measure_range`) import this name
#: rather than the underscore-prefixed original, so this module can no
#: longer assume `_classify_surface` is purely internal (review finding,
#: 2026-08-04 review-integration pass). The private name stays working as
#: an alias; every in-module caller below is unchanged.
_classify_surface = classify_surface


def _is_noise_path(path: str) -> bool:
    """True iff `path` is a generated/vendored artifact or lifecycle/memo
    bookkeeping file per the chain_oracle defensive-exclusion contract (C1
    § step 4). Never raises — an empty/odd path simply falls through to False."""
    basename = path.rsplit("/", 1)[-1]
    if basename in _NOISE_BASENAMES:
        return True
    if path.endswith(_NOISE_SUFFIXES):
        return True
    if _NOISE_PATH_RE.search(path):
        return True
    if _NOISE_LIFECYCLE_RE.match(path):
        return True
    if _NOISE_TRACKER_RE.match(path):
        return True
    return False


_PROSE_BEARING_EXTS = (".md", ".markdown", ".yaml", ".yml")


def _is_prose_bearing_path(path: str) -> bool:
    """True iff `path` is markdown or YAML — prose-bearing, not code-bearing.

    Review-MANDATE-scoped only: applied in `_compute_chain_oracle`,
    `_compute_session_oracle_single`, and the session-scoped range path
    (C1a) so a change confined to these extensions contributes NOTHING to
    those arms' loc/commits/surfaces accumulation — stronger than
    `_PLANNING_LOC_WEIGHT`'s de-weight, which still lets a planning
    artifact's LOC nudge the count upward. Does NOT touch
    `_compute_plan_oracle` (C1b's remit, a different input entirely) and is
    deliberately NOT folded into `_is_noise_path` (Anti-scope) — that
    predicate serves other consumers (coverage crediting, defensive noise
    exclusion) that must stay independently editable from this
    mandate-only exemption.

    JUDGMENT CALL (dispatch brief, 2026-08-12): classification is by
    EXTENSION ONLY — no carve-out for a `.yaml`/`.yml` under a code
    directory (e.g. a fixture, or a config the engine reads at runtime).
    A directory-based carve-out would invent a second, unpinned axis
    ("which directories count as code") layered on top of this one, which
    is exactly the wrong-axis file-type bucketing that problem 2 of the
    ratified problem-set already names as a defect
    (`docs/problems/2026-08-12-the-brightline-verdict-fires-on-every-ch.md`).
    The exemption is narrow enough to absorb the risk: it only shrinks the
    review-COUNT heuristic for these two oracle arms, never suppresses
    review of the file itself, never touches noise exclusion or coverage
    crediting, and the plan-oracle arm still mandates review for any plan
    that declares enough code-bearing rows regardless of what a single
    close's yaml/md diff looks like.
    """
    return path.endswith(_PROSE_BEARING_EXTS)


def _sum_loc(text: str) -> Tuple[int, bool]:
    """Sum `N insertion`/`N deletion` occurrences in `text`.

    Returns `(total, matched_any)`. `matched_any=False` is the die-silent
    gate condition — see module negative-spec.
    """
    matches = _LOC_RE.findall(text)
    if not matches:
        return 0, False
    total = 0
    for insertion_n, deletion_n in matches:
        total += int(insertion_n or deletion_n)
    return total, True


def _verdict(loc: int, commits: int, surfaces: int) -> str:
    if loc >= LOC_THRESHOLD or commits >= COMMITS_THRESHOLD or surfaces >= SURFACES_THRESHOLD:
        return "PARTITION-MANDATORY"
    return "single-reviewer-ok"


def _resolve_range(argv: List[str]) -> Tuple[Optional[str], Optional[str], int]:
    """Parse `[--session-id <id>] [<range>]`.

    Returns `(range_, session_id, rc)`. On a usage error, `range_` is None
    and `rc` is the exit code the caller should return immediately (a
    diagnostic has already been printed to stderr).
    """
    argv = list(argv)
    session_id = ""

    if argv and argv[0] == "--session-id":
        if len(argv) < 2 or not argv[1]:
            print(f"{_PROG}: --session-id requires an argument", file=sys.stderr)
            return None, None, 1
        session_id = argv[1]
        argv = argv[2:]

    if session_id and not _SESSION_ID_RE.match(session_id):
        print(
            f"{_PROG}: --session-id must match ^[a-zA-Z0-9][a-zA-Z0-9_-]*$",
            file=sys.stderr,
        )
        return None, None, 1

    if argv:
        range_ = argv[0]
    else:
        base_out, rc = _run_git(["merge-base", "origin/main", "HEAD"])
        base = base_out.strip()
        if rc != 0 or not base:
            print(
                f"{_PROG}: cannot resolve origin/main — pass a range explicitly",
                file=sys.stderr,
            )
            return None, None, 1
        range_ = f"{base}..HEAD"

    return range_, session_id, 0

def _parse_show_numstat(text: str) -> Dict[str, List[Tuple[str, str, str, str]]]:
    """Parse `git show --raw --numstat --format=%H <shas...>` batched output
    into `{sha: [(added, deleted, path, status), ...]}`. A bare 40-hex line
    is a commit boundary. Each commit emits a RAW block
    (`:mode mode sha sha STATUS[score]\\told[\\tnew]`) followed by a NUMSTAT
    block (`added\\tdeleted\\tpath`) in the SAME per-file order (verified
    against real multi-file/create/delete/rename commits in this repo,
    2026-08-12, C2) — rows are paired POSITIONALLY within each commit, never
    by path string, because the two blocks format a renamed/moved path
    differently (`old\\tnew` in the raw block vs `{old => new}`/`old => new`
    in the numstat block; see `_resolve_numstat_row_path`). `status` is the
    single-letter git status code (A/M/D/R/C) — `""` if a numstat row could
    not be paired to a raw row (block-length mismatch; not observed in the
    verification above, but `_substance_weight` treats an empty status as
    content, the safe/never-under-count direction). `added`/`deleted` are
    `"-"` for binary files (never coerced to an int here — the caller
    decides how to treat that)."""
    commits: Dict[str, List[Tuple[str, str, str, str]]] = {}
    current: Optional[str] = None
    raw_statuses: List[str] = []
    numstat_idx = 0
    for line in text.splitlines():
        if _CHAIN_SHOW_SHA_RE.match(line):
            current = line
            commits[current] = []
            raw_statuses = []
            numstat_idx = 0
            continue
        if current is None or not line.strip():
            continue
        m_raw = _CHAIN_RAW_STATUS_RE.match(line)
        if m_raw:
            raw_statuses.append(m_raw.group(1))
            continue
        m = _CHAIN_NUMSTAT_RE.match(line)
        if not m:
            continue
        status = raw_statuses[numstat_idx] if numstat_idx < len(raw_statuses) else ""
        numstat_idx += 1
        commits[current].append((m.group(1), m.group(2), m.group(3), status))
    return commits


def _accumulate_countable_rows(
    per_commit: Mapping[str, List[Tuple[str, str, str, str]]],
    shas: Iterable[str],
    *,
    track_files: bool = False,
) -> Dict[str, object]:
    """Shared row-accumulation (C2, 2026-08-12) for `_compute_chain_oracle`,
    `_compute_session_oracle_single`, and the `--session-id` range path in
    `_session_scoped` — previously three near-duplicate loops over
    `_parse_show_numstat`'s output, now one. For each sha in `shas`: resolves
    rename notation to the destination path (`_resolve_numstat_row_path`),
    drops noise (`_is_noise_path`) and prose-bearing
    (`_is_prose_bearing_path`, C1a) rows, and — if any row survives — counts
    the commit and accumulates the survivors' LOC (change-substance-weighted
    per AC2/AC3's `_substance_weight`, then planning-artifact de-weighted per
    `_PLANNING_LOC_WEIGHT`, same order as before C2) and surfaces. A commit
    with zero surviving rows contributes to NEITHER loc, commits, nor
    surfaces — not merely loc.

    `track_files=True` additionally accumulates a `files` set — the
    `--session-id` range path's own `files=` metric; the chain/session-single
    callers don't report it and pass the default."""
    loc = 0
    commits = 0
    surfaces: Set[str] = set()
    files: Set[str] = set()
    for sha in shas:
        rows = per_commit.get(sha, [])
        resolved = [
            (a, d, _resolve_numstat_row_path(p), status) for a, d, p, status in rows
        ]
        countable = [
            (a, d, p, status)
            for a, d, p, status in resolved
            if not _is_noise_path(p) and not _is_prose_bearing_path(p)
        ]
        if not countable:
            continue
        commits += 1
        for added, deleted, path, status in countable:
            a = int(added) if added.isdigit() else 0
            d = int(deleted) if deleted.isdigit() else 0
            # Review: code-reviewer — P3: this two-step truncation (int() here,
            # then int() again below) is safe from compounding rounding error
            # ONLY because `_substance_weight` is 0-or-1 valued — the first
            # `int()` is a no-op whenever weight=1.0 (nothing to truncate) and
            # collapses row_loc to 0 whenever weight=0.0 (nothing left for the
            # second int() to round). If a THIRD, fractional substance weight
            # is ever added to this chain, this two-step shape stops being
            # equivalent to a single combined multiply and should be
            # collapsed to one `int()` over the full product at that point.
            row_loc = int((a + d) * _substance_weight(status, a, d))
            if _is_planning_artifact_path(path):
                row_loc = int(row_loc * _PLANNING_LOC_WEIGHT)
            loc += row_loc
            surfaces.add(_classify_surface(path))
            if track_files:
                files.add(path)

    result: Dict[str, object] = {"loc": loc, "commits": commits, "surfaces": surfaces}
    if track_files:
        result["files"] = files
    return result


def _resolve_session_floor(session_id: str) -> Optional[str]:
    """C2 (2026-08-08): the session's own earliest commit reachable from
    HEAD, found via an UNSCOPED (full HEAD ancestry, not merge-base-bounded)
    Session-Id-trailer search — the same technique
    `_compute_session_oracle_single` already uses for cross-repo siblings
    (an `--all`/unbounded log search in place of a range-scoped one, on the
    grounds that the trailer is a unique-enough needle). Returns
    `f"{earliest_sha}^"` (a floor EXCLUSIVE of that commit, so `floor..HEAD`
    includes it), or `None` if the trailer matches nothing reachable from
    HEAD at all — a session that made no commits on this branch, or whose
    trailer never fired, is a genuinely vacuous case this cannot rescue.

    Never widens what gets MEASURED: the caller re-applies the SAME
    Session-Id-trailer filter to whatever this floor's range contains, so
    only this session's own commits are ever counted toward loc/commits/
    surfaces — a peer's commits sitting between this floor and HEAD (e.g.
    because their push advanced origin/main past this session's own
    commits, the exact blindness this floor exists to route around) are
    present in the wider git range but filtered out downstream exactly as
    they already are in the merge-base-bounded case. See Anti-scope's
    "do not widen the range over peer commits" — that warning is about
    sweeping a peer's DIFF into the measurement, not about how far back the
    trailer search itself looks.
    """
    shas_out, rc = _run_git(
        ["log", "--pretty=%H", f"--grep=^Session-Id: {session_id}$", "HEAD"]
    )
    if rc != 0:
        return None
    shas = [line for line in shas_out.splitlines() if line.strip()]
    if not shas:
        return None
    earliest = shas[-1]  # git log lists newest-first; the last line is oldest
    return f"{earliest}^"


def _session_scoped(range_: str, session_id: str) -> int:
    """`--session-id`-filtered scan over `range_`.

    Zero-match against `range_` first retries with a session-aware floor
    (`_resolve_session_floor`, C2) — a shared-branch merge-base can advance
    past this session's own commits as peers push, which is not the same as
    the session genuinely having nothing to measure. Only if THAT also
    turns up nothing does the scan resolve to the vacuous outcome: it
    prints `VERDICT=indeterminate` (2026-08-08 fix), never
    `single-reviewer-ok` and never `PARTITION-MANDATORY` — the gate
    examined zero commits, so it has no basis to claim either a small diff
    or a mandatory partition. Per the PM constraint
    (docs/plans/2026-08-08-the-gate-says-ok-when-it-could-not-look.md), the
    vacuous case must not resolve to a forced partition either; it hands
    the "I could not look" fact to the consumer instead of manufacturing a
    verdict. Exit code stays 0 — this is a legitimate, honestly-reported
    outcome, not the die-silent infra-failure case below (which prints
    nothing and returns 1)."""
    shas_out, _rc = _run_git(
        ["log", "--pretty=%H", f"--grep=^Session-Id: {session_id}$", range_]
    )
    filtered_shas = [line for line in shas_out.splitlines() if line.strip()]
    filtered_count = len(filtered_shas)

    if filtered_count == 0:
        floor = _resolve_session_floor(session_id)
        if floor is not None:
            retry_range = f"{floor}..HEAD"
            retry_shas_out, _rc2 = _run_git(
                ["log", "--pretty=%H", f"--grep=^Session-Id: {session_id}$", retry_range]
            )
            retry_shas = [line for line in retry_shas_out.splitlines() if line.strip()]
            if retry_shas:
                print(
                    f"note: range={range_} matched 0 commits — recovered via "
                    f"session-aware floor, rescanning {retry_range}",
                    file=sys.stderr,
                )
                range_ = retry_range
                filtered_shas = retry_shas
                filtered_count = len(filtered_shas)

    if filtered_count == 0:
        print(
            f"range={range_} loc=0 commits=0 surfaces=0 files=0 "
            f"filtered_to=0 VERDICT=indeterminate"
        )
        print(
            "note: session-id matched 0 commits in range — gate vacuous, "
            "EM verify scope manually",
            file=sys.stderr,
        )
        return 0

    # Metric-wide noise exclusion (AC1) — same shape as `_compute_chain_oracle`:
    # `--numstat` (not `--stat`) so a per-file noise path can be dropped
    # before loc/files/surfaces accumulate, and a fully-noise commit
    # contributes to NEITHER loc, files, commits, nor surfaces, rather than
    # only loc. Reuses `_parse_show_numstat` (the chain-oracle parser) rather
    # than a fourth diffstat parser. `--raw` (C2) additionally recovers each
    # row's git status letter for AC2/AC3 change-substance weighting — see
    # `_accumulate_countable_rows`, the shared helper this used to duplicate
    # inline.
    show_out, rc2 = _run_git(["show", "--raw", "--numstat", "--format=%H", *filtered_shas])
    if rc2 != 0:
        print(
            f"{_PROG}: warning: git show failed over filtered SHAs — "
            f"metrics may be incomplete",
            file=sys.stderr,
        )

    per_commit = _parse_show_numstat(show_out)
    total_raw_rows = sum(len(rows) for rows in per_commit.values())
    if total_raw_rows == 0:
        return 1  # die-silent gate (loc=/files=) — see module negative-spec

    accumulated = _accumulate_countable_rows(per_commit, filtered_shas, track_files=True)
    loc = int(accumulated["loc"])  # type: ignore[arg-type]
    commits = int(accumulated["commits"])  # type: ignore[arg-type]
    surfaces_set = accumulated["surfaces"]  # type: ignore[assignment]
    files_set = accumulated["files"]  # type: ignore[assignment]

    files = len(files_set)
    surfaces = len(surfaces_set)

    verdict = _verdict(loc, commits, surfaces)
    print(
        f"range={range_} loc={loc} commits={commits} surfaces={surfaces} "
        f"files={files} filtered_to={filtered_count} VERDICT={verdict}"
    )
    return 0


def _unfiltered(range_: str) -> int:
    shortstat_out, _rc = _run_git(["diff", "--shortstat", range_])
    loc, matched = _sum_loc(shortstat_out)
    if not matched:
        return 1  # die-silent gate (loc=) — see module negative-spec

    name_only_out, _rc2 = _run_git(["diff", "--name-only", range_])
    name_lines = name_only_out.splitlines()
    files = len([ln for ln in name_lines if ln.strip()])

    oneline_out, _rc3 = _run_git(["log", "--oneline", range_])
    commits = len(oneline_out.splitlines())

    surfaces = len({_classify_surface(p) for p in name_lines})

    verdict = _verdict(loc, commits, surfaces)
    print(
        f"range={range_} loc={loc} commits={commits} surfaces={surfaces} "
        f"files={files} VERDICT={verdict}"
    )
    return 0


def main(argv: List[str]) -> int:
    # `--from-handoff` (the chain+plan two-oracle mode) is REMOVED —
    # state/kill-ledger.md K-007, 2026-08-19, PM ruling. It was reachable
    # only from `wsc-coverage-gate-runner.py brightline-gate`, itself
    # removed. The session-scoped and unfiltered modes below are untouched.
    if argv and argv[0] == "--from-handoff":
        print(
            f"{_PROG}: --from-handoff was removed (state/kill-ledger.md K-007, "
            "2026-08-19). The chain-scoped two-oracle gate no longer exists; "
            "run this gate without it for a session-scoped or range verdict.",
            file=sys.stderr,
        )
        return 1

    range_, session_id, rc = _resolve_range(argv)
    if range_ is None:
        return rc

    if session_id:
        return _session_scoped(range_, session_id)
    return _unfiltered(range_)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
