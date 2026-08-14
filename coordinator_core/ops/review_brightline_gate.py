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

Cross-repo session scope (XB-6, 2026-07-29) -- the `--from-handoff` `session_oracle`
computation (and the tier=A unwalked-repo ruling it feeds) now sums the
Session-Id-trailer-matched diff across EVERY repo this session actually wrote
into, not only the repo the ceremony happens to be invoked from. This closes a
blind spot: a session working under the DoE-claude standing cross-repo grant
(global CLAUDE.md section Cross-repo write discipline) commits directly into
Claude-klabauter (and, per several rows in the 2026-07-29 Windows-viability plan
family -- MP-8's post-merge/post-checkout hook, MP-9's untracks, WS-5's belts,
WS-1's marker -- into the `~/.claude` meta-repo too), and neither sibling
commit was ever visible to a gate that only ever looked at `repo_root`'s own
git log. See `_resolve_cross_repo_roots` for the resolved repo list (a NAMED
list, not a single hardcoded second entry) and `_compute_session_oracle` for
the per-repo sum. A repo in the list that cannot be resolved, or resolves to a
path absent on disk, is a FAIL-LOUD condition (`CrossRepoResolutionError`),
never a silently-dropped repo -- silently dropping one reintroduces exactly the
blind spot this widening exists to close.
Spec backlink: DoE-claude:pln-windows-viability-stop-the-spa-b969d9 section XB-6

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

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

import yaml

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.coverage import (
    _DagChainResult,
    _DagChainSetContext,
    _derive_dag_chain_set,
    _is_planning_artifact_path,
    _PLANNING_ARTIFACT_PATH_PREFIXES,
    _resolve_numstat_row_path,
)
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops import ownership_index
from coordinator_core.ops.deliverable_equivalence import canonicalize, load_equivalence_map
from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root
from coordinator_core._settings_home import home_dir as _resolve_claude_home_root
from coordinator_core.win_portability import no_console_creationflags

_PROG = "review-brightline-gate.sh"  # literal program-name prefix — matches bash oracle stderr

LOC_THRESHOLD = 500
COMMITS_THRESHOLD = 5
SURFACES_THRESHOLD = 4

# --from-handoff (chain+plan terminus) mode — plan-file oracle (C2 slice).
#
# code-bearing change_kind subset per docs/wiki/lessons-outbox-schema.md §
# Change-kind enum — doc-edit/wiki-*/skill-edit/doctrine-edit are EXCLUDED
# (they don't carry review-cost weight for the reviewer-quantity detector).
_CODE_BEARING_KINDS = frozenset(
    {"code-edit", "test-edit", "script-edit", "hook-edit", "agent-prompt-edit"}
)
_CLAUDE_KLABAUTER_SURFACE_PREFIX = "claude-klabauter:"
_META_REPO_SURFACE_PREFIX = "claude-meta:"  # XB-6 — the ~/.claude meta-repo sibling
_PLAN_TASKS_FENCE_RE = re.compile(r"```ya?ml plan-tasks\s*\n(.*?)```", re.DOTALL)

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_LOC_RE = re.compile(r"(\d+) insertion|(\d+) deletion")
_STAT_LINE_RE = re.compile(r"^\s*\S.*\|")
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


_SHIPPED_IN_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

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


def _split_frontmatter(raw: str) -> Tuple[dict, str]:
    """Best-effort `---\\n<yaml>\\n---` frontmatter split. Never raises — a missing
    fence, unparseable YAML, or non-dict result all fall back to `({}, raw)`."""
    if not raw.startswith("---\n"):
        return {}, raw
    parts = raw.split("---\n", 2)
    if len(parts) < 2:
        return {}, raw
    fm: dict = {}
    try:
        loaded = yaml.safe_load(parts[1])
        if isinstance(loaded, dict):
            fm = loaded
    except yaml.YAMLError:
        fm = {}
    body = parts[2] if len(parts) > 2 else ""
    return fm, body


def _read_frontmatter(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return {}
    fm, _body = _split_frontmatter(raw)
    return fm


def _extract_plan_tasks_rows(plan_text: str) -> List[dict]:
    """Parse the SINGLE ```yaml plan-tasks fenced block into a list of row dicts.

    Missing fence, unparseable YAML, or a non-list result all degrade to `[]`
    (a malformed plan contributes zero rows rather than raising) — the plan
    FILE itself being unparseable is the fail-loud case the caller guards.
    """
    m = _PLAN_TASKS_FENCE_RE.search(plan_text)
    if not m:
        return []
    try:
        rows = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _load_plan_file(path: Path) -> Tuple[dict, List[dict]]:
    try:
        raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return {}, []
    fm, _body = _split_frontmatter(raw)
    rows = _extract_plan_tasks_rows(raw)
    return fm, rows


def _resolve_closing_session_id(repo_root: Path, from_handoff: str) -> str:
    """Closing session id resolution — mirrors review-coverage-gate.py's D3 case 3
    env-first convention, falling back to the seed baton's own claim holder
    (the seed IS the closing session's own handoff), resolved LEDGER-FIRST
    through ``coordinator_core.claim_state.resolve_claim_state`` (C1) rather
    than the tracked-frontmatter mirror alone — a seed baton whose claim was
    stamped on a branch the shared worktree has since switched away from
    would otherwise silently resolve to "" (source: this plan's own Problem
    section incident) and hard-exit the gate below with no claim to run
    against.

    Widening what this resolves must not soften the empty-string failure
    case: a baton with no live claim on EITHER the ledger or the mirror
    still returns "" here, and the caller (main()) still fails loudly on
    that — this only widens which desynced-but-real claims resolve.
    """
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if env_sid:
        return env_sid
    seed_path = Path(from_handoff)
    if not seed_path.is_absolute():
        seed_path = repo_root / from_handoff
    claim_state = resolve_claim_state(seed_path, repo_root=repo_root)
    return claim_state.holder or ""


def _enumerate_owned_batons(
    repo_root: Path, closing_session_id: str
) -> Tuple[List[Tuple[Path, dict]], List[str]]:
    """AC17/AC20 owned-baton SET (N may be >1): every handoff this session's
    CLAIM RECORD attributes to it (never a raw ``claimed_by``/``consumed_by``
    frontmatter read), across BOTH the live location (`state/handoffs/`) and
    the archived locations (`archive/handoffs/`, `archive/completed/`).

    Delegates entirely to ``coordinator_core.ops.ownership_index.
    build_ownership_index`` (C19) — this function no longer walks the corpus
    itself; see that module for the claim-store-first data flow and the
    reason the frontmatter mirror is demoted to a validated join target, not
    the source of truth.

    AC20: `post_commit_stamp_and_ship` (wsc Step 2.7) stamps AND ARCHIVES every
    owned childless predecessor during the SAME close this gate runs in. A
    baton that archives before this scan runs must still be counted — a
    state/-only rglob silently drops it with no error, under-counting the
    reviewer-quantity headline. The ownership index unions live + archive and
    de-dups on resolved absolute path.

    Bidirectional negative spec (AC18) — ownership is not gating, and gating
    is not ownership: ownership (claim-record) is NEVER a gating input. Gate
    state (`blocked_by`, `deployment_state: awaiting_gate`) is NEVER an
    ownership input. The gate index and this ownership enumerator share ONLY
    the corpus walker; they must not share a lookup, a cache key, or a
    verdict. The invariant is discharged by the type signature — the gate
    evaluator never receives an ownership index because nothing passes it
    one.

    Two-definitions note (AC21): a *different* owned-set question is answered
    by ``coordinator_core.ops.ceremony.resolver.find_all_consumed_handoffs``
    — see that function's docstring for the named divergence. The two are
    not drop-in replacements for each other.

    Returns ``(owned_batons, scan_errors)`` — a non-empty ``scan_errors``
    means an archive subtree could not be fully scanned, so a claimed
    basename living under it may be silently missing from ``owned_batons``;
    the caller must surface this rather than treat an empty list as "no
    owned batons".

    Close-vs-session distinction (C2, 2026-08-08): this function's returned
    set is scoped to the SESSION, not to the CLOSE — a session that already
    capped one workstream and later takes a chain-terminal close for another
    still owns both batons here, by design (its session-wide contract is
    load-bearing for AC17/AC20 and for callers other than the two --from-
    handoff oracles that union over owned batons; see this module's
    Anti-scope). A caller that needs "only the batons THIS close is capping"
    (`_from_handoff_main`, which feeds the SAME filtered set to BOTH
    `_compute_chain_oracle` and `_compute_plan_oracle` — the plan's own
    Problem section names both as unioning across owned batons, and
    narrowing only one would manufacture spurious plan_oracle!=chain_oracle
    disagreement) filters the returned tuples itself, via
    `_capped_by_earlier_close` —
    excluding a baton whose frontmatter already carries a terminal
    `deployment_state` AND a `shipped_in` sha at scan time, since that pair
    can only have been written by an EARLIER close's own stamp-and-archive
    step (`d-run-wsc-tail`, which fires strictly after this gate in the same
    close's directive sequence — see `_capped_by_earlier_close`). This
    preserves AC17/AC20: a baton archived AND stamped during THIS SAME close
    reads `deployment_state: in_flight`/no `shipped_in` at gate-scan time
    (the stamp lands later in the tail), so it is never excluded by that
    filter — it stays counted, exactly as this function's own docstring
    already promises above."""
    if not closing_session_id:
        return [], []
    return ownership_index.build_ownership_index(repo_root, closing_session_id)


def _capped_by_earlier_close(baton_fm: dict) -> bool:
    """True iff `baton_fm` was already terminal-stamped AND shipped BEFORE
    this close's own gate-scan — i.e. it was capped by an EARLIER close, not
    the one running this gate right now (C2, 2026-08-08).

    Ordering this predicate depends on: `d-run-chain-plan-brightline-gate`
    (this gate) carries no `depends_on` and is appended to the directive
    list strictly BEFORE `d-run-wsc-tail` (the stamp-and-archive step that
    writes `deployment_state`/`shipped_in`) — see
    `coordinator_core.workstream_complete.directives_review
    .build_chain_plan_brightline_gate_directive` and
    `coordinator_core.workstream_complete.__init__.build_directives`, and
    the ordering-pin test
    `test_gate_directive_precedes_tail_directive_in_build_order` in this
    module's test file. So a baton this CURRENT close is capping has not
    been stamped yet at scan time, and reads a non-terminal
    `deployment_state` (or no `shipped_in`) here — never excluded.

    A baton reading BOTH a terminal `deployment_state` (member of
    `HANDOFF_TERMINAL_DEPLOYMENT`) AND a `shipped_in` value that parses as a
    plausible git sha can only have gotten that pair from a PRIOR close's
    own tail already having run — exclude it from the set this close's chain
    oracle unions over.

    Review: code-reviewer — P2: this predicate's safety rests on the AND
    with `shipped_in`, NOT on `HANDOFF_TERMINAL_DEPLOYMENT` membership alone.
    Two of that set's four members (`closed`, `abandoned`) are, per
    `handoff_archive_transition.py`, never freshly paired with a `shipped_in`
    stamp by this repo's own automated close paths today — only `shipped`
    and `continued` are. If some other writer ever hand-wrote or
    legacy-carried a `closed`/`abandoned` state alongside a plausible-looking
    `shipped_in` (e.g. copied from a predecessor), this predicate would
    exclude it even though it was not capped by a close's tail step in the
    sense this docstring describes. Do not read set membership by itself as
    proof of "capped by a close" — the `shipped_in` conjunct is what
    currently carries that guarantee.

    AC5: every ambiguity resolves toward KEEPING (returning False) — an
    absent/non-string `deployment_state`, an absent/non-string/malformed
    `shipped_in`, or a `deployment_state` outside `HANDOFF_TERMINAL_DEPLOYMENT`
    all return False (never dropped). Never raises."""
    deployment_state = baton_fm.get("deployment_state")
    if not isinstance(deployment_state, str):
        return False
    if deployment_state not in HANDOFF_TERMINAL_DEPLOYMENT:
        return False
    shipped_in = baton_fm.get("shipped_in")
    if not isinstance(shipped_in, str) or not _SHIPPED_IN_SHA_RE.match(shipped_in):
        return False
    return True


def _find_governing_plans(repo_root: Path, baton_fm: dict) -> List[Path]:
    """ID-JOIN (never scope-overlap): baton.deliverable_id <-> plan.deliverable_id,
    or baton.origin_plan_id <-> plan.plan_id.

    Both sides of the deliverable_id join are canonicalized through the declared
    fork-equivalence map (state/deliverable-equivalence.yaml) before comparison — a
    missed equivalence here is a GATE miss, not merely a reporting gap, since this
    result governs whether a baton is treated as claimed by a plan. Read-only:
    canonicalize() never writes back to baton_fm or the plan's own frontmatter.
    """
    equivalence_map = load_equivalence_map(repo_root)
    deliverable_id = canonicalize(baton_fm.get("deliverable_id"), equivalence_map)
    origin_plan_id = baton_fm.get("origin_plan_id")
    if not deliverable_id and not origin_plan_id:
        return []
    plans_dir = repo_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return []
    matches: List[Path] = []
    for p in sorted(plans_dir.rglob("*.md")):
        fm, _rows = _load_plan_file(p)
        if not fm:
            continue
        if deliverable_id and canonicalize(fm.get("deliverable_id"), equivalence_map) == deliverable_id:
            matches.append(p)
            continue
        if origin_plan_id and fm.get("plan_id") == origin_plan_id:
            matches.append(p)
    return matches


def _surface_repo(surface: str) -> str:
    """bare path = this repo (empty-string sentinel); "claude-klabauter:" prefix = claude-klabauter;
    "claude-meta:" prefix (XB-6) = the ~/.claude meta-repo."""
    if surface.startswith(_CLAUDE_KLABAUTER_SURFACE_PREFIX):
        return "claude-klabauter"
    if surface.startswith(_META_REPO_SURFACE_PREFIX):
        return "claude-meta"
    return ""


class CrossRepoResolutionError(RuntimeError):
    """Raised when a repo this session may have written into (XB-6's named
    cross-repo list) cannot be resolved, or resolves to a path absent on
    disk. Fail-loud by design — a repo that silently drops out of the list
    reintroduces the exact review-gate blind spot XB-6 exists to close."""


def _resolve_cross_repo_roots(this_repo_root: Path) -> Dict[str, str]:
    """Resolve every OTHER repo this session may have written into under the
    DoE-claude standing cross-repo grant (global CLAUDE.md § Cross-repo write
    discipline) — a NAMED list, never a single hardcoded second entry:

      "claude-klabauter" -> coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root()
                           (env CLAUDE_KLABAUTER_ROOT -> settings-home .claude-klabauter-root pointer
                           -> `machine-local get repos.claude_klabauter`).
      "claude-meta"     -> the ~/.claude meta-repo (the CLAUDE_HOME override
                           when set, else the platform home), via
                           coordinator_core._settings_home.home_dir().

    Both resolvers are the SAME ones the rest of the fleet already uses to find
    these repos (no new resolution mechanism invented here), so a machine with
    a working coordinator install always resolves both. A resolution failure —
    the registry key absent, or the resolved path missing on disk — raises
    `CrossRepoResolutionError` with the underlying remediation text; it is
    never silently swallowed into an empty/short list.

    `this_repo_root` is excluded from the returned mapping if a resolved
    sibling happens to canonicalize to the SAME path (e.g. this ceremony is
    itself running inside claude-klabauter, or inside ~/.claude) — a repo is
    never its own cross-repo sibling.
    """
    this_resolved = str(this_repo_root.resolve())
    roots: Dict[str, str] = {}

    try:
        claude_klabauter_root = coordinator_claude_klabauter_root()
    except RuntimeError as exc:
        raise CrossRepoResolutionError(
            f"XB-6 cross-repo review scope: cannot resolve claude-klabauter — {exc}"
        ) from exc
    if not os.path.isdir(claude_klabauter_root):
        raise CrossRepoResolutionError(
            f"XB-6 cross-repo review scope: resolved claude-klabauter root "
            f"'{claude_klabauter_root}' does not exist on disk"
        )
    roots["claude-klabauter"] = claude_klabauter_root

    try:
        meta_root = str(_resolve_claude_home_root() / ".claude")
    except ValueError as exc:
        raise CrossRepoResolutionError(
            f"XB-6 cross-repo review scope: cannot resolve the ~/.claude meta-repo — {exc}"
        ) from exc
    if not os.path.isdir(meta_root):
        raise CrossRepoResolutionError(
            f"XB-6 cross-repo review scope: resolved ~/.claude meta-repo root "
            f"'{meta_root}' does not exist on disk"
        )
    roots["claude-meta"] = meta_root

    return {
        label: root
        for label, root in roots.items()
        if str(Path(root).resolve()) != this_resolved
    }


def _compute_plan_oracle(
    repo_root: Path, owned_batons: List[Tuple[Path, dict]]
) -> Dict[str, object]:
    """Steps 2-3 of the --from-handoff contract: join each owned baton to its
    governing plan(s), then SUM deferred:false code-bearing plan_steps and
    UNION plan_surfaces/plan_repos across the M governing plans."""
    matched_plan_paths: Set[Path] = set()
    for _baton_path, baton_fm in owned_batons:
        matched_plan_paths.update(_find_governing_plans(repo_root, baton_fm))

    plan_steps = 0
    plan_surfaces: Set[str] = set()
    for plan_path in sorted(matched_plan_paths):
        _fm, rows = _load_plan_file(plan_path)
        for row in rows:
            if row.get("deferred") is True:
                continue
            if row.get("change_kind") not in _CODE_BEARING_KINDS:
                continue
            plan_steps += 1
            surface = row.get("surface")
            if isinstance(surface, str) and surface:
                plan_surfaces.add(surface)

    plan_repos = {_surface_repo(s) for s in plan_surfaces}
    plan_oracle = max(
        1 + plan_steps // 10,
        1 + len(plan_surfaces) // 4,
        len(plan_repos),
    )
    return {
        "plan_oracle": plan_oracle,
        "plan_steps": plan_steps,
        "plan_surfaces": plan_surfaces,
        "plan_repos": plan_repos,
        "matched_plan_paths": matched_plan_paths,
    }


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


def _compute_chain_oracle(
    repo_root: Path, owned_batons: List[Tuple[Path, dict]], closing_session_id: str
) -> Dict[str, object]:
    """Step 4 of the --from-handoff contract: chain_oracle over the UNION of
    chain SHA sets from each owned baton's _derive_dag_chain_set walk, with
    defensive file-granularity noise exclusion (see `_is_noise_path`).

    `indeterminate`/`notes` surface any owned baton's DAG walk that could not
    be safely resolved — the caller (C4) rules on how that propagates to the
    tier decision; this function never silently absorbs it into a number."""
    chain_shas: Set[str] = set()
    indeterminate = False
    notes: List[str] = []

    # C8: ONE _DagChainSetContext constructed here and threaded across every
    # baton in this loop — amortises _build_dag_index and the batched
    # Session-Id/Deliverable-Id git-log walk over all owned batons sharing
    # repo_root, instead of each baton's _derive_dag_chain_set call re-deriving
    # the whole DAG from scratch. Deliberately excludes walked_deliverable_ids
    # (baton-specific, derived from closing_set) — see _DagChainSetContext.
    shared_context = _DagChainSetContext()
    for baton_path, _baton_fm in owned_batons:
        result: _DagChainResult = _derive_dag_chain_set(
            str(baton_path),
            str(repo_root),
            closing_session_id,
            shared_context=shared_context,
        )
        if result.indeterminate:
            indeterminate = True
        notes.extend(result.notes)
        chain_shas.update(result.shas)

    chain_loc = 0
    chain_commits = 0
    chain_surfaces: Set[str] = set()

    if chain_shas:
        show_out, rc = _run_git(
            ["show", "--raw", "--numstat", "--format=%H", *sorted(chain_shas)]
        )
        if rc != 0:
            notes.append(
                f"git show --numstat failed over {len(chain_shas)} chain SHA(s) "
                f"— chain metrics may be incomplete"
            )
        per_commit = _parse_show_numstat(show_out)
        # C2: shared row-accumulation (noise/prose filtering, AC2/AC3
        # change-substance weighting, planning-artifact de-weighting) lives
        # in `_accumulate_countable_rows` — see its docstring for the full
        # contract this used to implement inline.
        accumulated = _accumulate_countable_rows(per_commit, chain_shas)
        chain_loc = int(accumulated["loc"])  # type: ignore[arg-type]
        chain_commits = int(accumulated["commits"])  # type: ignore[arg-type]
        chain_surfaces = accumulated["surfaces"]  # type: ignore[assignment]

    chain_oracle = max(
        1 + chain_loc // LOC_THRESHOLD,
        1 + chain_commits // COMMITS_THRESHOLD,
        1 + len(chain_surfaces) // SURFACES_THRESHOLD,
    )
    return {
        "chain_oracle": chain_oracle,
        "chain_loc": chain_loc,
        "chain_commits": chain_commits,
        "chain_surfaces": chain_surfaces,
        "chain_shas": chain_shas,
        "indeterminate": indeterminate,
        "notes": notes,
    }


def _compute_session_oracle_single(
    session_id: str, log_scope_args: List[str], cwd: Optional[str]
) -> Dict[str, object]:
    """Single-repo session_oracle CONTRIBUTION — the per-repo body
    `_compute_session_oracle` (XB-6) sums across repo_root plus every
    resolved cross-repo sibling. `log_scope_args` is `[range_]` for repo_root
    (preserving the original range-scoped behavior byte for byte) or
    `["--all"]` for a sibling repo, which has no equivalent resolved range —
    the `Session-Id:` trailer is a unique-enough needle that an unscoped
    `--all` search over a sibling repo's full ref set is exactly as correct
    as a range-scoped one, and does not require this module to know the
    sibling's branch/merge-base topology.
    """
    shas_out, _rc = _run_git(
        ["log", "--pretty=%H", f"--grep=^Session-Id: {session_id}$", *log_scope_args],
        cwd=cwd,
    )
    filtered_shas = [line for line in shas_out.splitlines() if line.strip()]

    if not filtered_shas:
        return {"loc": 0, "commits": 0, "surfaces": set()}

    # Metric-wide noise exclusion (AC2) — same shape as `_compute_chain_oracle`:
    # `--numstat` (not `--stat`) so a per-file noise path can be dropped
    # before LOC/surfaces accumulate, and a fully-noise commit contributes to
    # NEITHER loc, commits, nor surfaces, rather than only loc. `--raw`
    # (C2) additionally recovers each row's git status letter for AC2/AC3
    # change-substance weighting — see `_accumulate_countable_rows`, the
    # shared helper this used to duplicate inline.
    show_out, _rc2 = _run_git(
        ["show", "--raw", "--numstat", "--format=%H", *filtered_shas], cwd=cwd
    )
    per_commit = _parse_show_numstat(show_out)

    return _accumulate_countable_rows(per_commit, filtered_shas)


def _compute_session_oracle(
    range_: str, session_id: str, cross_repo_roots: Mapping[str, str]
) -> Dict[str, object]:
    """Step 5: session_oracle over the terminus session's OWN diff.

    WIDENED (XB-6): sums the Session-Id-trailer-matched diff across
    repo_root (scoped to `range_`, unchanged from the pre-XB-6 behavior)
    UNIONED with every repo in `cross_repo_roots` (module docstring §
    Cross-repo session scope) — never repo_root alone. `cross_repo_roots` is
    keyed by label ("claude-klabauter", "claude-meta", ...); repo_root itself
    is keyed by the empty-string sentinel, matching `_surface_repo`'s bare-path
    convention, so the two are directly joinable against `plan_repos`.

    Reuses the same `Session-Id:` trailer filter as the `--session-id` CLI
    path (`_session_scoped`), but is NOT that function and does not share its
    die-silent gates: `--from-handoff` mode is compute+emit only and must
    never die silently on a genuinely-empty diff (module docstring — fail
    loud is reserved for true infra errors, not "zero commits/zero LOC").
    A zero-match or zero-LOC outcome (in any one repo, or across all of them)
    degrades to the oracle floor (1), it does not abort the whole
    `--from-handoff` compute.
    """
    per_repo: Dict[str, Dict[str, object]] = {
        "": _compute_session_oracle_single(session_id, [range_], cwd=None)
    }
    for label, root in cross_repo_roots.items():
        per_repo[label] = _compute_session_oracle_single(session_id, ["--all"], cwd=root)

    total_loc = sum(int(r["loc"]) for r in per_repo.values())
    total_commits = sum(int(r["commits"]) for r in per_repo.values())
    all_surfaces: Set[str] = set()
    for r in per_repo.values():
        all_surfaces |= r["surfaces"]  # type: ignore[operator]
    session_repos_written = {label for label, r in per_repo.items() if r["commits"]}

    if total_commits == 0:
        session_oracle = 1
    else:
        session_oracle = max(
            1 + total_loc // LOC_THRESHOLD,
            1 + total_commits // COMMITS_THRESHOLD,
            1 + len(all_surfaces) // SURFACES_THRESHOLD,
        )
    return {
        "session_oracle": session_oracle,
        "session_loc": total_loc,
        "session_commits": total_commits,
        "session_surfaces": len(all_surfaces),
        "session_repos_written": session_repos_written,
        "session_per_repo": per_repo,
    }


def _chain_last_commit_epoch(chain_shas: Set[str]) -> Optional[int]:
    """Latest committer-epoch among `chain_shas`, or `None` if empty/unresolvable
    (used for the "plan may be stale" basis note — never fail-loud on this)."""
    if not chain_shas:
        return None
    out, rc = _run_git(["show", "-s", "--format=%ct", *sorted(chain_shas)])
    if rc != 0:
        return None
    times = [int(tok) for tok in out.split() if tok.strip().isdigit()]
    return max(times) if times else None


def _determine_tier(
    plan_repos: Set[str],
    chain_commits: int,
    chain_indeterminate: bool,
    session_repos_written: Optional[Set[str]] = None,
) -> Tuple[str, List[str]]:
    """Step 7 tier ruling.

    tier=A iff a deferred:false code-bearing plan row declares a repo the
    chain walk saw ZERO commits in, or the chain walk is indeterminate on a
    repo the plan declares — the declared-but-unwalked REPO case. The DAG
    chain walk (`_derive_dag_chain_set`) is inherently single-repo (it walks
    `repo_root`'s own git history) — a plan_repos entry OTHER than the bare
    "" (this-repo) sentinel (i.e. a `claude-klabauter:`-prefixed surface) is
    therefore structurally unwalkable by chain_oracle and always tier=A when
    declared, independent of chain_commits.

    WIDENED (XB-6): tier=A ALSO fires when `session_repos_written` (the
    labels session_oracle actually found Session-Id-trailer commits in — see
    `_compute_session_oracle`) names a sibling repo the PLAN never declared.
    A repo the session demonstrably wrote into is unwalked by chain_oracle
    for the exact same structural reason as a declared-but-unwalked plan
    repo — chain_oracle is single-repo by construction — whether or not the
    plan bothered to declare it. Without this, a session that commits into
    claude-klabauter or ~/.claude WITHOUT a plan row naming that surface stays
    tier=none/B even though real cross-repo work happened; this is precisely
    the blind spot XB-6 exists to close.

    tier=B else iff plan_oracle != chain_oracle (pure magnitude disagreement,
    no unwalked-repo case). tier=none otherwise.

    Returns `(tier, unwalked_repo_labels)` — the labels feed the basis text.
    """
    chain_saw_this_repo = chain_commits > 0 and not chain_indeterminate
    repos_of_interest = set(plan_repos) | {r for r in (session_repos_written or set()) if r != ""}
    unwalked: List[str] = []
    for repo in sorted(repos_of_interest):
        if repo == "":
            if not chain_saw_this_repo:
                unwalked.append("this-repo")
        else:
            unwalked.append(repo)
    if unwalked:
        return "A", unwalked
    return "", []  # caller resolves B-vs-none against plan_oracle != chain_oracle


def _from_handoff_main(rest: List[str]) -> int:
    """--from-handoff <path> [<git-range>] chain+plan terminus mode.

    Fully wired end-to-end (C2 plan_oracle, C3 chain_oracle, C4 session_oracle
    + ruling + emission — steps 1-8 of the contract). Emits exactly one
    `BRIGHTLINE …` line as the LAST stdout line. Compute+emit only — this
    module never HALTs; enforcement (tier=A hard-stop) lives in the caller
    (wsc-coverage-gate-runner.py), not here.
    """
    if not rest or not rest[0]:
        print(f"{_PROG}: --from-handoff requires a path argument", file=sys.stderr)
        return 1
    from_handoff = rest[0]

    range_, _session_id, rc = _resolve_range(rest[1:])
    if range_ is None:
        return rc

    repo_root_out, git_rc = _run_git(["rev-parse", "--show-toplevel"])
    repo_root_str = repo_root_out.strip() if git_rc == 0 else ""
    if not repo_root_str:
        print(f"{_PROG}: --from-handoff: cannot resolve git repo root", file=sys.stderr)
        return 1
    repo_root = Path(repo_root_str)

    seed_abs = Path(from_handoff)
    if not seed_abs.is_absolute():
        seed_abs = repo_root / from_handoff
    if not seed_abs.is_file():
        print(
            f"{_PROG}: --from-handoff: seed handoff not found: {from_handoff}",
            file=sys.stderr,
        )
        return 1

    closing_session_id = _resolve_closing_session_id(repo_root, from_handoff)
    if not closing_session_id:
        print(
            f"{_PROG}: --from-handoff: could not resolve a closing session id "
            f"(no $CLAUDE_CODE_SESSION_ID, no claimed_by on {from_handoff})",
            file=sys.stderr,
        )
        return 1

    owned_batons, ownership_scan_errors = _enumerate_owned_batons(
        repo_root, closing_session_id
    )
    for err in ownership_scan_errors:
        print(f"{_PROG}: --from-handoff: ownership scan note: {err}", file=sys.stderr)
    # Review: code-reviewer — Finding 4 (P2): distinguish "genuinely owns
    # nothing" from "the ownership scan failed and we undercounted" IN
    # CONTROL FLOW, not merely via the stderr notes above (easy to miss/
    # scroll past). `ownership_undercounted` is True only when the fallback
    # to the seed-only set is potentially masking real owned batons the scan
    # could not see — never when the emptiness is genuine (no scan errors).
    ownership_undercounted = bool(ownership_scan_errors) and not owned_batons
    if ownership_undercounted:
        print(
            f"{_PROG}: --from-handoff: WARNING — owned-baton set is empty AND "
            f"the ownership scan reported errors above; falling back to the "
            f"seed handoff alone, but this may UNDERCOUNT real owned batons "
            f"(degraded, not a genuine zero-owned-batons case)",
            file=sys.stderr,
        )
    if not owned_batons:
        # The seed itself didn't turn up in the state/handoffs/ walk (e.g. an
        # unpublished/relocated fixture) — fall back to the seed alone rather
        # than computing over an empty owned-baton set.
        owned_batons = [(seed_abs, _read_frontmatter(seed_abs))]

    try:
        cross_repo_roots = _resolve_cross_repo_roots(repo_root)
    except CrossRepoResolutionError as exc:
        print(f"{_PROG}: --from-handoff: {exc}", file=sys.stderr)
        return 1

    # C2 (2026-08-08): BOTH the plan oracle and the chain oracle union only
    # the batons THIS close is capping — a baton already terminal-stamped by
    # an EARLIER close is excluded here, at the oracle call site (never by
    # narrowing what `_enumerate_owned_batons` itself returns — see that
    # function's docstring and `_capped_by_earlier_close`). The plan's own
    # Problem section names `_compute_plan_oracle` as unioning across the
    # SAME owned-baton set as the chain oracle, so narrowing one side and
    # not the other would manufacture spurious plan_oracle!=chain_oracle
    # disagreement (which `_determine_tier`'s tier=B ruling reads as
    # meaningful) between a close-scoped chain oracle and a session-scoped
    # plan oracle. `session_result` is deliberately NOT filtered — it is
    # computed from the git range/trailer, not from `owned_batons`, so this
    # discrimination does not apply to it.
    # Review: code-reviewer — P1: `_capped_by_earlier_close`'s ordering
    # premise (this gate runs strictly before `d-run-wsc-tail` within a
    # SINGLE `build_directives()` call) does not hold across a SECOND pass
    # for the SAME close (`brief()` regenerates the directive list fresh on
    # every invocation). On a resumed/retried pass, the seed baton this
    # close is capping can already carry a terminal `deployment_state` +
    # `shipped_in` from pass 1's own tail — the predicate would otherwise
    # read that as "capped by an earlier close" and drop it, under-counting.
    # The seed IS, by definition, the baton this close is capping, whatever
    # its stamp state — never exclude it via `_capped_by_earlier_close`.
    # Compared by resolved path identity (the seed arg may be absolute or
    # repo-relative; owned-baton paths come from the ownership index).
    seed_resolved = seed_abs.resolve()
    chain_owned_batons = [
        (path, fm)
        for path, fm in owned_batons
        if path.resolve() == seed_resolved or not _capped_by_earlier_close(fm)
    ]

    plan_result = _compute_plan_oracle(repo_root, chain_owned_batons)
    chain_result = _compute_chain_oracle(repo_root, chain_owned_batons, closing_session_id)
    session_result = _compute_session_oracle(range_, closing_session_id, cross_repo_roots)

    for note in chain_result["notes"]:
        print(f"{_PROG}: --from-handoff: chain note: {note}", file=sys.stderr)
    if chain_result["indeterminate"]:
        print(
            f"{_PROG}: --from-handoff: chain walk INDETERMINATE for one or more "
            f"owned batons — see notes above",
            file=sys.stderr,
        )

    plan_oracle = plan_result["plan_oracle"]
    chain_oracle = chain_result["chain_oracle"]
    session_oracle = session_result["session_oracle"]

    tier, unwalked_repos = _determine_tier(
        plan_result["plan_repos"],
        chain_result["chain_commits"],
        chain_result["indeterminate"],
        session_result["session_repos_written"],
    )
    if not tier:
        tier = "B" if plan_oracle != chain_oracle else "none"

    reviewers_suggested = max(plan_oracle, chain_oracle, session_oracle)
    reviewers_low = min(plan_oracle, chain_oracle, session_oracle)
    reviewers_required = min(reviewers_suggested, 4)  # AC17/Finding7 multi-baton headline cap
    verdict = "PARTITION-MANDATORY" if reviewers_required >= 2 else "single-reviewer-ok"

    session_repo_breakdown = ",".join(
        f"{(label or 'this-repo')}={r['commits']}"
        for label, r in session_result["session_per_repo"].items()
    )
    basis_parts = [
        f"plan_oracle={plan_oracle}(steps={plan_result['plan_steps']},"
        f"surfaces={len(plan_result['plan_surfaces'])},repos={len(plan_result['plan_repos'])})",
        f"chain_oracle={chain_oracle}(loc={chain_result['chain_loc']},"
        f"commits={chain_result['chain_commits']},surfaces={len(chain_result['chain_surfaces'])})",
        f"session_oracle={session_oracle}(loc={session_result['session_loc']},"
        f"commits={session_result['session_commits']},surfaces={session_result['session_surfaces']},"
        f"cross_repo_commits=({session_repo_breakdown}))",
    ]
    if ownership_undercounted:
        basis_parts.append("ownership_scan_degraded=true (owned_batons may be undercounted)")
    if tier == "A":
        basis_parts.append(f"tier=A declared-but-unwalked repo(s)={','.join(unwalked_repos)}")
    elif tier == "B":
        basis_parts.append(f"tier=B plan_oracle!=chain_oracle ({plan_oracle}!={chain_oracle})")
    else:
        basis_parts.append("tier=none oracles agree, no unwalked repo")
    if reviewers_suggested > reviewers_required:
        basis_parts.append(f"headline capped from raw={reviewers_suggested} to {reviewers_required}")

    last_chain_epoch = _chain_last_commit_epoch(chain_result["chain_shas"])
    if last_chain_epoch is not None:
        for plan_path in sorted(plan_result["matched_plan_paths"]):
            try:
                plan_mtime = int(plan_path.stat().st_mtime)
            except OSError:
                continue
            if plan_mtime < last_chain_epoch:
                basis_parts.append("plan may be stale")
                break

    basis = " ".join(basis_parts)

    print(
        f"BRIGHTLINE reviewers_required={reviewers_required} "
        f"reviewers_suggested={reviewers_suggested} reviewers_low={reviewers_low} "
        f"plan_oracle={plan_oracle} chain_oracle={chain_oracle} session_oracle={session_oracle} "
        f'tier={tier} verdict={verdict} basis="{basis}"'
    )
    return 0


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
    if argv and argv[0] == "--from-handoff":
        return _from_handoff_main(argv[1:])

    range_, session_id, rc = _resolve_range(argv)
    if range_ is None:
        return rc

    if session_id:
        return _session_scoped(range_, session_id)
    return _unfiltered(range_)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
