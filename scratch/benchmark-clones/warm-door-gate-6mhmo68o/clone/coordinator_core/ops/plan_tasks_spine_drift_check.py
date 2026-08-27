"""
coordinator_core.ops.plan_tasks_spine_drift_check — JSON-RPC
"plan.tasks.spine_drift_check" operation.

Purpose: `close_out_and_stamp.py` already carries a hardened oracle for
"does this plan's `## Tasks` spine have a covering commit" —
`_committed_chunk_shas` (the Deliverable-Id-trailer/subject-chunk-id join,
scarred against a named 2026-07-27 cross-plan chunk-id collision) and
`_committed_id_covers_spine_id` (sub-chunk-suffix coverage). Today the only
way to SEE that a spine row still reads `disposition: open` while the tree
already shipped its work is to run `close_out_and_stamp` itself — a
mutating call. A fireable wave-map (`spine_read.read_spine` ->
`wave_map.build_waves`) will happily re-dispatch an already-landed chunk in
the meantime, silently. This op exposes the SAME oracle read-only, so a
spine can be checked against the tree without stamping anything.

Backlog record: state/bug-backlog/2026-08-21-spine-drift-is-invisible-
between-execute-and-emit-a1c4e7b20d13.yaml
Sizing object: state/sizings/2026-08-21-a-spine-that-disagrees-with-the-
tree-sho.yaml

REUSE, not reimplementation: `_parse_spine_rows`, `_all_spine_ids`,
`_plan_deliverable_id` and `_row_disposition` are still direct calls into
`close_out_and_stamp`'s own private helpers, via the deferred `coas`
reference (see IMPORT CYCLE below). `_committed_chunk_shas` and
`_committed_id_covers_spine_id` are RELOCATED here as private local copies
(C4, 2026-08-20 plan "the close ceremony stops paying for the join", Gap 1,
2026-08-21 second pass) — same treatment `coordinator_core.ops.
cascade_baton_rows` already carries for the same two symbols, both on C3's
deletion list from `close_out_and_stamp.py`. NOT the same narrowing,
though: this op's `_handler` threads `plan_path_rel` through, so this copy
keeps the full body including the Session-Id fallback leg
`cascade_baton_rows` deliberately dropped as dead code at ITS call site —
see the RELOCATION note above `_committed_id_covers_spine_id`'s definition
for the full accounting. Still not reimplemented: same bodies, same rules,
moved rather than rewritten. Nothing here re-derives the trailer/subject
join, opens a second commit ledger, or relaxes `_committed_chunk_shas`'s
exact-equality Deliverable-Id join.

IMPORT CYCLE (Review, 2026-08-21 -- do not revert this to a module-level
import): `close_out_and_stamp.py` itself imports from `coordinator_core.
ops.*` in several places (ceremony, plan_status_transition,
handoff_close_origin_stub, fleet._common). A module-level
`from coordinator_core.execute_plan_assemble.close_out_and_stamp import
(...)` here therefore creates a real cycle whenever `close_out_and_stamp`
happens to be the FIRST of the two imported: close_out_and_stamp ->
coordinator_core.ops -> the package's own eager-import loop -> this module
-> back into a partially-initialized close_out_and_stamp, which raises
`ImportError: cannot import name ... from partially initialized module`.
`coordinator_core/ops/__init__.py`'s eager-import loop CATCHES that
ImportError so nothing fails loudly -- this op simply never registers:
present in the source tree, absent from `plan.tasks.spine_drift_check`'s
own registry entry, exactly the "reachable by name" failure this op's own
dispatch brief warned against, arriving by an import-order route neither
the brief nor the first review pass anticipated. `_coas()` below defers
the import to CALL time instead: by the time a real request reaches
`_handler`, `close_out_and_stamp` has always finished importing elsewhere
first, so the cycle never has a chance to fire. See
`test_registers_when_close_out_and_stamp_imports_first` (this module's own
test file) for the regression pin -- it deliberately imports
`close_out_and_stamp` BEFORE `coordinator_core.ops`, in a fresh subprocess,
and asserts this op is still in the registry.

Report-only by architectural boundary (DR-263), mirroring
`coordinator_core.ops.cascade_backstop_sweep`'s own "reports but never
flips" posture: this op never writes the plan file, never calls
`_auto_resolve_committed_open_rows` (the existing WRITE-side consumer of
this same evidence — `close_out_and_stamp.close_out_and_stamp` itself
remains the only entrypoint that flips a row), and never stamps anything.

Brightline (`docs/decisions/DR-344-*`): exactly ONE batched `git log` call
per invocation — `_committed_chunk_shas`, called once — never one per open
row. When the spine has no commit-required OR no open rows at all, the git
call is skipped entirely (nothing to check against).

KNOWN, NAMED, out-of-remit cost this op INHERITS rather than introduces
(Review, 2026-08-21): `_committed_chunk_shas`'s own `_chunk_evidence_log_
lines` query measured 4.5-6.4s wall / 469-719ms in-process-CPU on a
15,988-line log range in this repo -- over the 500ms brightline
(`docs/decisions/DR-344-*`) and the >2s CLAUDE.md forbids outright. This
predates this op (it is `close_out_and_stamp`'s own existing query, reused
verbatim per this module's REUSE mandate) and also implicates the existing
mutating close-out path, not merely this read-only one -- fixing it here
would be a narrow, load-bearing perf change to shared machinery, above
this op's remit. NOT fixed here; surfaced by the reviewing EM to the PM
instead. Do not narrow the log range or otherwise perf-patch this from
inside this module without that decision.

Self-registration: importing this module calls
register_op("plan.tasks.spine_drift_check") as a side-effect. Added to
coordinator_core/ops/__init__.py's eager-import table so registration fires
at start_server() time.

NEGATIVE-SPEC:
  - Does NOT write, anywhere, under any code path. No `locked_rmw`, no
    `_stamp_rows_in_body`, no frontmatter mutation of any kind.
  - Does NOT re-derive the Deliverable-Id trailer/subject-chunk-id join, or
    the sub-chunk-suffix coverage match — both are relocated (C4, Gap 1)
    private local copies, same bodies/rules as `close_out_and_stamp.py`'s
    own, not reimplemented.
  - Does NOT call `_commit_subject` per drifted row (a covering commit's
    subject) — that is a SECOND git spawn per row, which the brightline
    forbids; only the sha `_committed_chunk_shas` already captured in its
    one batched call is reported.
  - Does NOT touch `spine_read.py` or `emit.py`.
  - Does NOT perf-patch `_chunk_evidence_log_lines`/`_committed_chunk_shas`
    from inside this module -- see the KNOWN cost paragraph above.

Known narrowing vs. the full oracle: `evidence_available` is `True`, and
`drift_status` can be `"drift_detected"`/`"verified_no_drift"`, only for
the exact-equality Deliverable-Id-trailer join (`JOIN_PROVENANCE_JOINED`)
— this op does NOT recognize `_committed_chunk_shas`'s own Session-Id-
scoped fallback leg (`JOIN_PROVENANCE_SESSION_FALLBACK_PARTIAL` in
`_determine_shipped`) as evidence, nor the sibling-repo/`disposition_ref`
unions `_determine_shipped` layers on afterward. A row this fallback alone
covers is therefore reported `"unknown"` rather than as drift — a false
negative, never a false positive, matching this module's own documented
"false-negative-over-false-positive" posture throughout.

Stated plainly because the failure direction matters (Review, 2026-08-21):
a row covered ONLY by the Session-Id fallback or a sibling-repo/
`disposition_ref` union lands in the `"unknown"` bucket, NEVER in
`"verified_no_drift"`. Under-reporting drift (a real drift this narrowing
misses) is the safe, accepted direction; silently reporting such a row as
verified-clean would not be — `"unknown"` is the only bucket this
narrowing is allowed to produce for evidence it cannot see. A future
widening that recognizes those legs must preserve that: promote a
newly-recognized row into `"drift_detected"`/`"verified_no_drift"` only
when it is ACTUALLY checked, never by relabeling `"unknown"` wholesale.

Measurement basis for the `drift_status` three-way split (2026-08-21, over
600 commits on `work/machine-a/2026-08-18to20`): only ~6% of commits carry a
chunk-id subject (`C1:`, `C1,C2:`) at all; when one IS present its
`Deliverable-Id` trailer is essentially always there too (the join is
trustworthy when it fires), but its RECALL across the corpus is very low.
The join can confirm a row shipped; it can never confirm a row did not —
so `"unknown"` (join found nothing to compare against) is the
overwhelmingly common outcome, not an edge case, and must never collapse
into the same signal as `"verified_no_drift"` (join found real evidence
and no open row was covered by it).

`drift_status` is set on EVERY return path, including every error/
`exit_code: 1` early return (Review, 2026-08-21 dogfooding across 254 real
plans: 3 came back with `drift_status` absent -- read externally as
`None` by any `dict.get`-style caller, an unhandled fifth shape outside
the documented four states, and precisely how "unknown" quietly becomes
"clean" downstream if left unstated). `DRIFT_STATUS_ERROR` names that
fifth state explicitly rather than leaving it an implicit key-absence.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.fleet._common import main_worktree_root, plan_claim_dir

SCHEMA_VERSION = 1

#: Five-way `drift_status` this op reports (2026-08-21, measurement-driven
#: widening -- only ~6% of commits on this branch carry a chunk-id subject
#: at all; when one is present its `Deliverable-Id` trailer is reliable,
#: but the JOIN'S RECALL is low). The oracle can confirm a row shipped; it
#: can never confirm a row did NOT ship. Collapsing "no evidence either
#: way" into the same `[]` as "checked, genuinely clean" would recreate
#: exactly the false-green this whole area exists to end -- a caller must
#: be able to tell the two apart without inspecting `join_provenance`
#: itself. Values are plain english labels for `join_provenance`'s own
#: four-state enum (`close_out_and_stamp.py`), not a competing vocabulary,
#: plus `"error"`/`"no_open_rows"` for this op's own two non-oracle exits.
DRIFT_STATUS_DRIFT_DETECTED = "drift_detected"
DRIFT_STATUS_VERIFIED_NO_DRIFT = "verified_no_drift"
DRIFT_STATUS_UNKNOWN = "unknown"
DRIFT_STATUS_NO_OPEN_ROWS = "no_open_rows"
DRIFT_STATUS_ERROR = "error"

#: RELOCATION (C4, 2026-08-20 plan "the close ceremony stops paying for the
#: join", Gap 1, 2026-08-21 second pass): `_committed_chunk_shas` and
#: `_committed_id_covers_spine_id` are private local copies below, not
#: `coas.`-routed -- both are on C3's deletion list from
#: `close_out_and_stamp.py`, and this op's own IMPORT CYCLE paragraph above
#: already rules out a module-level `from ...cascade_baton_rows import
#: (...)` too (`cascade_baton_rows` itself imports `close_out_and_stamp` at
#: module level, so importing IT here reintroduces the identical cycle this
#: module's `_coas()` exists to dodge). A deferred import of
#: `cascade_baton_rows` was considered and rejected on a second ground, not
#: just the cycle: `cascade_baton_rows`'s own `_committed_chunk_shas` is
#: NARROWED -- it deliberately dropped the `plan_path_rel`-gated Session-Id
#: fallback leg because that module's one call site never threads
#: `plan_path_rel` through. THIS module's call site DOES (see `_handler`
#: below), so sharing that narrowed copy would silently drop real fallback
#: evidence this op has always reported. This copy therefore carries the
#: FULL original body, including the fallback leg and its own two helpers
#: (`_plan_claim_holder_session_id`, `_session_id_fallback_evidence`) --
#: same treatment as `cascade_baton_rows`, not the same narrowing, because
#: the two modules' real call sites differ. `JOIN_PROVENANCE_*`/
#: `_JOIN_PROVENANCE_REASON` were ALSO on C3's deletion list (added to the
#: row at execution, 2026-08-21, after this docstring first shipped) and
#: are now relocated here too, as private local module-level constants
#: (not `coas.`-routed) -- this op is their sole surviving consumer;
#: `close_out_and_stamp._determine_shipped` no longer classifies a
#: join-provenance value at all. `_parse_spine_rows`, `_all_spine_ids`,
#: `_plan_deliverable_id`, `_row_disposition`, `_OPEN` are NOT on C3's
#: deletion list and stay `coas.`-routed through the existing deferred
#: `_coas()` import, unchanged.
#:
#: `_run_git` and `_plan_deliverable_id` are NOT on C3's deletion list (both
#: have other live callers inside `close_out_and_stamp.py`), so the
#: relocated functions below thread `coas` through as a parameter to reach
#: them at call time rather than importing either -- identical in spirit to
#: `_open_spine_rows(rows, coas)`/`_join_provenance(join_stats, coas)`
#: above, and the only way to reach `coas._run_git` without the module-level
#: import this file's own cycle paragraph forbids.
_CHUNK_ID_LIST_GRAMMAR = (
    r"[A-Za-z0-9._'-]+(?:\([^()]*\))?"
    r"(?:(?:,\s*|\s*[+/]\s*)[A-Za-z0-9._'-]+(?:\([^()]*\))?)*"
)
_CHUNK_SUBJECT_RE = re.compile(rf"^({_CHUNK_ID_LIST_GRAMMAR}):\s")
_CHUNK_SUBJECT_PREFIXED_RE = re.compile(
    rf"^(?:\S+\s+)+?({_CHUNK_ID_LIST_GRAMMAR}):\s"
)
_CHUNK_ID_PAREN_SUFFIX_RE = re.compile(r"\([^()]*\)$")
_CHUNK_ID_SHAPE_RE = re.compile(r"^C\d")

_SINGLE_LETTER_SUFFIX_RE = re.compile(r"^[a-z]$")
_DASH_TAG_SUFFIX_RE = re.compile(r"^-[a-z][a-z0-9]*$")
_TRAILING_DIGITS_SUFFIX_RE = re.compile(r"^\d+$")
_ADJACENCY_DASH_TAGS = frozenset({"pre", "prep", "post"})

_LOG_RECORD_SEP = "\x1e"
_LOG_FIELD_SEP = "\x1f"
_DELIVERABLE_ID_BODY_LINE_RE = re.compile(
    r"^Deliverable-Id:[ \t]*(\S[^\r\n]*?)[ \t]*$", re.MULTILINE
)

#: RELOCATION (C3, 2026-08-21 -- "the close ceremony stops paying for the
#: join"): `close_out_and_stamp.JOIN_PROVENANCE_*`/`_JOIN_PROVENANCE_REASON`
#: were on C3's own deletion list too (the close ceremony's `_determine_
#: shipped` no longer classifies a join-provenance value at all -- see that
#: module's docstring). This op is their SOLE surviving consumer, so the
#: four-state classification `_join_provenance` below already computes is
#: relocated here as a private local copy rather than deleted outright --
#: same treatment `_committed_chunk_shas`/`_committed_id_covers_spine_id`
#: already got (RELOCATION note above). Only the four values this op's own
#: `_join_provenance` can return are carried; `JOIN_PROVENANCE_LEDGER_
#: FALLBACK`/`JOIN_PROVENANCE_NO_EVIDENCE_SOURCE`/`JOIN_PROVENANCE_SESSION_
#: FALLBACK_PARTIAL` described the close ceremony's OWN dispatch-ledger-
#: fallback/no-evidence-source/session-id-fallback branches, none of which
#: this local `_committed_chunk_shas` copy's own narrower body reaches.
JOIN_PROVENANCE_JOINED = "joined"
JOIN_PROVENANCE_NO_JOIN_KEY = "no_join_key"
JOIN_PROVENANCE_NO_JOIN_CANDIDATES = "no_join_candidates"
JOIN_PROVENANCE_KEY_MISMATCH = "key_mismatch"

#: Plain-language reason strings for every NON-`"joined"` provenance value
#: above -- this op's own `evidence_reason` result field uses these so a
#: reader sees WHY attribution failed, not just that it did.
_JOIN_PROVENANCE_REASON = {
    JOIN_PROVENANCE_NO_JOIN_KEY: (
        "the plan's own frontmatter carries no deliverable_id: field, so the "
        "commit-coverage join was never attempted"
    ),
    JOIN_PROVENANCE_NO_JOIN_CANDIDATES: (
        "no commit in the search range carries a Deliverable-Id trailer at "
        "all, so there was nothing to join against"
    ),
    JOIN_PROVENANCE_KEY_MISMATCH: (
        "commits in range carry a Deliverable-Id trailer, but never one "
        "equal to this plan's own frontmatter value, so the join could not "
        "match them"
    ),
}


def _committed_id_covers_spine_id(committed_id: str, spine_id: str) -> bool:
    """Private local copy of `close_out_and_stamp._committed_id_covers_
    spine_id`, relocated here (not reimplemented) -- see that function's
    original docstring (module history, `close_out_and_stamp.py`) for the
    full three-suffix-shape rationale this copy preserves exactly."""
    if committed_id == spine_id:
        return True
    if not committed_id.startswith(spine_id):
        return False
    suffix = committed_id[len(spine_id):]
    if _SINGLE_LETTER_SUFFIX_RE.match(suffix):
        return True
    if _DASH_TAG_SUFFIX_RE.match(suffix):
        return suffix[1:].lower() not in _ADJACENCY_DASH_TAGS
    if spine_id and not spine_id[-1].isdigit() and _TRAILING_DIGITS_SUFFIX_RE.match(suffix):
        return True
    return False


def _strip_chunk_id_paren_suffix(token: str) -> str:
    """Private local copy of
    `close_out_and_stamp._strip_chunk_id_paren_suffix`."""
    return _CHUNK_ID_PAREN_SUFFIX_RE.sub("", token)


def _extract_chunk_ids(
    subject: str, spine_ids: Optional[Iterable[str]] = None
) -> list[str]:
    """Private local copy of `close_out_and_stamp._extract_chunk_ids`,
    relocated here -- same body, same rules, not reimplemented. See that
    function's original docstring (module history, `close_out_and_stamp.py`)
    for the full separator-grammar, bounding, and known-false-negative
    rationale this copy preserves exactly."""
    match = _CHUNK_SUBJECT_RE.match(subject)
    if not match:
        match = _CHUNK_SUBJECT_PREFIXED_RE.match(subject)
    if not match:
        return []
    raw = match.group(1)
    tokens = [
        _strip_chunk_id_paren_suffix(token)
        for token in re.findall(r"[A-Za-z0-9._'-]+(?:\([^()]*\))?", raw)
    ]
    if len(tokens) == 1:
        bare = tokens[0]
        if spine_ids is not None:
            if any(_committed_id_covers_spine_id(bare, spine_id) for spine_id in spine_ids):
                return [bare]
            return []
        return [bare]
    if spine_ids is not None:
        spine_id_list = list(spine_ids)
        return [
            token
            for token in tokens
            if any(_committed_id_covers_spine_id(token, spine_id) for spine_id in spine_id_list)
        ]
    return [token for token in tokens if _CHUNK_ID_SHAPE_RE.match(token)]


@dataclasses.dataclass(frozen=True)
class DeliverableJoinStats:
    """Private local copy of `close_out_and_stamp.DeliverableJoinStats`,
    relocated here -- same four fields, same meaning. See that class's
    original docstring (module history, `close_out_and_stamp.py`) for the
    full rationale this copy preserves exactly."""

    attempted: bool
    trailered_commit_count: int
    matched_commit_count: int
    trailer_matched_no_chunk_id_count: int


def _resolve_deliverable_id(trailer_block: str, body: str) -> str:
    """Private local copy of `close_out_and_stamp._resolve_deliverable_id`,
    relocated here -- same trailer-first, body-fallback join-key
    resolution. See that function's original docstring (module history,
    `close_out_and_stamp.py`) for the full rationale this copy preserves
    exactly."""
    for candidate in trailer_block.splitlines():
        value = candidate.strip()
        if value:
            return value
    matches = _DELIVERABLE_ID_BODY_LINE_RE.findall(body)
    if matches:
        return matches[-1].strip()
    return ""


def _deliverable_log_records(
    coas: Any, repo_root: Path, log_args: Sequence[str], full_sha: bool = False
) -> tuple:
    """Private local copy of `close_out_and_stamp._deliverable_log_records`,
    relocated here -- same single-producer `git log` shape, same record
    parse, same message-line fallback. See that function's original
    docstring (module history, `close_out_and_stamp.py`) for the full
    rationale this copy preserves exactly. Returns `(query_ok,
    [(sha, subject, deliverable_id)])`. `_run_git` is not relocated (it has
    other live callers in `close_out_and_stamp.py`), so it is reached via
    `coas` at call time -- see the RELOCATION note above."""
    sha_atom = "%H" if full_sha else "%h"
    result = coas._run_git(
        [
            "log",
            "--format="
            + _LOG_RECORD_SEP
            + sha_atom
            + _LOG_FIELD_SEP
            + "%s"
            + _LOG_FIELD_SEP
            + "%(trailers:key=Deliverable-Id,valueonly)"
            + _LOG_FIELD_SEP
            + "%B",
            *log_args,
        ],
        repo_root,
    )
    if result.returncode != 0:
        return False, []
    records: list = []
    for raw_record in (result.stdout or "").split(_LOG_RECORD_SEP):
        if not raw_record.strip():
            continue
        fields = raw_record.split(_LOG_FIELD_SEP, 3)
        if len(fields) < 4:
            continue
        sha = fields[0].strip()
        if not sha:
            continue
        records.append((sha, fields[1], _resolve_deliverable_id(fields[2], fields[3])))
    return True, records


def _plan_execution_authorized_sha(plan_text: str) -> Optional[str]:
    """Private local copy of
    `close_out_and_stamp._plan_execution_authorized_sha`, relocated here --
    reads the plan's own `execution_authorized_sha:` frontmatter field,
    unquoted. See that function's original docstring (module history,
    `close_out_and_stamp.py`) for the full rationale this copy preserves
    exactly."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    return read_fm_field_unquoted(split.fm_text, "execution_authorized_sha")


def _first_deliverable_commit_range_base(
    coas: Any, repo_root: Path, deliverable_id: Optional[str]
) -> Optional[str]:
    """Private local copy of
    `close_out_and_stamp._first_deliverable_commit_range_base`, relocated
    here -- same earliest-commit-for-this-deliverable lookup, same
    parent-sha return. See that function's original docstring (module
    history, `close_out_and_stamp.py`) for the full rationale this copy
    preserves exactly."""
    if not deliverable_id:
        return None
    query_ok, records = _deliverable_log_records(coas, repo_root, ["--reverse", "HEAD"], full_sha=True)
    if not query_ok:
        return None
    for commit_sha, _subject, trailer_value in records:
        if not trailer_value:
            continue
        if trailer_value != deliverable_id:
            continue
        parent_result = coas._run_git(["rev-parse", "--verify", "--quiet", f"{commit_sha}^"], repo_root)
        parent_sha = (parent_result.stdout or "").strip()
        if parent_result.returncode == 0 and parent_sha:
            return parent_sha
        return ""
    return None


def _chunk_evidence_log_range(
    coas: Any, repo_root: Path, plan_text: Optional[str] = None
) -> list[str]:
    """Private local copy of `close_out_and_stamp._chunk_evidence_log_range`,
    relocated here -- same rung ladder (`execution_authorized_sha:` literal,
    earliest-deliverable-commit base, `merge-base origin/main HEAD`, bare
    `HEAD`). See that function's original docstring (module history,
    `close_out_and_stamp.py`) for the full rationale this copy preserves
    exactly."""
    if plan_text is not None:
        sha = _plan_execution_authorized_sha(plan_text)
        if sha:
            resolved = coas._run_git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], repo_root)
            resolved_sha = (resolved.stdout or "").strip()
            if resolved.returncode == 0 and resolved_sha:
                return [f"{resolved_sha}..HEAD"]

        deliverable_id = coas._plan_deliverable_id(plan_text)
        base = _first_deliverable_commit_range_base(coas, repo_root, deliverable_id)
        if base is not None:
            if base == "":
                return ["HEAD"]
            return [f"{base}..HEAD"]

    merge_base_result = coas._run_git(["merge-base", "origin/main", "HEAD"], repo_root)
    base_sha = (merge_base_result.stdout or "").strip()
    if merge_base_result.returncode == 0 and base_sha:
        return [f"{base_sha}..HEAD"]
    return ["HEAD"]


def _chunk_evidence_log_lines(
    coas: Any, repo_root: Path, plan_text: Optional[str] = None
) -> tuple:
    """Private local copy of `close_out_and_stamp._chunk_evidence_log_lines`,
    relocated here -- same single `git log` query, same tab-separated
    `<short-sha>\\t<subject>\\t<deliverable-id>` line shape. See that
    function's original docstring (module history, `close_out_and_stamp.py`)
    for the full rationale this copy preserves exactly. Returns
    `(query_ok, lines, log_range)`."""
    log_range = _chunk_evidence_log_range(coas, repo_root, plan_text)
    query_ok, records = _deliverable_log_records(coas, repo_root, log_range)
    if not query_ok:
        return False, [], log_range
    return True, [f"{sha}\t{subject}\t{deliverable_id}" for sha, subject, deliverable_id in records], log_range


def _plan_claim_holder_session_id(root: Path, plan_path_rel: Optional[str]) -> Optional[str]:
    """Private local copy of
    `close_out_and_stamp._plan_claim_holder_session_id`, relocated here --
    same claim-dir `session_id` read. See that function's original
    docstring (module history, `close_out_and_stamp.py`) for the full
    rationale this copy preserves exactly."""
    if not plan_path_rel:
        return None
    try:
        common_dir = git_common_dir(root)
    except RuntimeError:
        return None
    claim_dir = plan_claim_dir(common_dir, Path(plan_path_rel))
    try:
        value = (claim_dir / "session_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _session_id_fallback_evidence(
    coas: Any,
    repo_root: Path,
    log_range: list[str],
    claim_holder_sid: str,
    spine_ids: Optional[Iterable[str]],
) -> tuple[set, dict]:
    """Private local copy of
    `close_out_and_stamp._session_id_fallback_evidence`, relocated here --
    same Session-Id-scoped chunk-subject matching, same zero-evidence-gated
    caller contract. See that function's original docstring (module
    history, `close_out_and_stamp.py`) for the full rationale this copy
    preserves exactly."""
    result = coas._run_git(
        [
            "log",
            "--format="
            + _LOG_RECORD_SEP
            + "%h"
            + _LOG_FIELD_SEP
            + "%s"
            + _LOG_FIELD_SEP
            + "%(trailers:key=Session-Id,valueonly)",
            *log_range,
        ],
        repo_root,
    )
    if result.returncode != 0:
        return set(), {}

    committed: set = set()
    committed_shas: dict = {}
    for raw_record in (result.stdout or "").split(_LOG_RECORD_SEP):
        if not raw_record.strip():
            continue
        fields = raw_record.split(_LOG_FIELD_SEP, 2)
        if len(fields) < 3:
            continue
        sha = fields[0].strip()
        if not sha:
            continue
        subject = fields[1]
        session_id_value = fields[2].strip()
        if not session_id_value or session_id_value != claim_holder_sid:
            continue
        for chunk_id in _extract_chunk_ids(subject, spine_ids):
            committed.add(chunk_id)
            committed_shas.setdefault(chunk_id, sha)
    return committed, committed_shas


def _committed_chunk_shas(
    coas: Any,
    repo_root: Path,
    deliverable_id: Optional[str],
    spine_ids: Optional[Iterable[str]] = None,
    plan_text: Optional[str] = None,
    plan_path_rel: Optional[str] = None,
) -> tuple:
    """Private local copy of `close_out_and_stamp._committed_chunk_shas`,
    relocated here -- FULL body carried over (not narrowed, unlike
    `cascade_baton_rows`'s own copy): this op's `_handler` DOES thread
    `plan_path_rel` through, so the `plan_path_rel`-gated Session-Id
    fallback leg is live here, not dead code. See that function's original
    docstring (module history, `close_out_and_stamp.py`) for the full
    join-semantics rationale this copy preserves exactly. Returns
    `(query_ok, committed_ids, committed_shas, join_stats)`."""
    query_ok, log_lines, log_range = _chunk_evidence_log_lines(coas, repo_root, plan_text)
    if not query_ok:
        return (
            False,
            set(),
            {},
            DeliverableJoinStats(
                attempted=bool(deliverable_id),
                trailered_commit_count=0,
                matched_commit_count=0,
                trailer_matched_no_chunk_id_count=0,
            ),
        )

    committed: set = set()
    committed_shas: dict = {}
    trailered_commit_count = 0
    matched_commit_count = 0
    trailer_matched_no_chunk_id_count = 0
    for line in log_lines:
        parts = line.split("\t", 2)
        if len(parts) < 2 or not parts[0]:
            continue
        sha = parts[0]
        subject = parts[1]
        trailer_value = parts[2].strip() if len(parts) > 2 else ""
        if trailer_value:
            trailered_commit_count += 1
        if not deliverable_id or trailer_value != deliverable_id:
            continue
        subject_chunk_ids = _extract_chunk_ids(subject, spine_ids)
        if not subject_chunk_ids:
            trailer_matched_no_chunk_id_count += 1
            continue
        matched_commit_count += 1
        for chunk_id in subject_chunk_ids:
            committed.add(chunk_id)
            committed_shas.setdefault(chunk_id, sha)

    join_stats = DeliverableJoinStats(
        attempted=bool(deliverable_id),
        trailered_commit_count=trailered_commit_count,
        matched_commit_count=matched_commit_count,
        trailer_matched_no_chunk_id_count=trailer_matched_no_chunk_id_count,
    )

    if matched_commit_count == 0 and plan_path_rel:
        claim_holder_sid = _plan_claim_holder_session_id(repo_root, plan_path_rel)
        if claim_holder_sid:
            fallback_committed, fallback_shas = _session_id_fallback_evidence(
                coas, repo_root, log_range, claim_holder_sid, spine_ids
            )
            committed |= fallback_committed
            for chunk_id, sha in fallback_shas.items():
                committed_shas.setdefault(chunk_id, sha)

    return True, committed, committed_shas, join_stats


_COAS_MODULE: Any = None


def _coas() -> Any:
    """Deferred import of `close_out_and_stamp` -- returns the module,
    cached in a module-level global after the first call. Called ONLY from
    inside `_handler` (never at this module's own import time) -- see the
    module docstring's "IMPORT CYCLE" paragraph for why a top-level import
    here is unsafe. `sys.modules` already caches the underlying import
    after the first real one anywhere in the process, so this adds no
    meaningful cost beyond the first call; the module-level cache below is
    purely to avoid a dict lookup+attribute walk through `sys.modules` on
    every request, not a correctness requirement."""
    global _COAS_MODULE
    if _COAS_MODULE is None:
        import coordinator_core.execute_plan_assemble.close_out_and_stamp as _mod

        _COAS_MODULE = _mod
    return _COAS_MODULE


def _open_spine_rows(rows: list[Any], coas: Any) -> list[dict]:
    """Non-`deferred`, `id`-bearing rows whose disposition reads `open`
    (D1 schema default, via `coas._row_disposition`) — the exact
    population `_auto_resolve_committed_open_rows` (`close_out_and_
    stamp.py`, AC8) scans for its write-side counterpart, restated here
    read-only. `coas` is the deferred-imported module (see `_coas()`),
    passed in rather than imported at this function's own module level."""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        if not row.get("id"):
            continue
        if coas._row_disposition(row) == coas._OPEN:
            out.append(row)
    return out


def _join_provenance(join_stats: Any, coas: Any) -> str:
    """Classifies an already-computed `DeliverableJoinStats` into one of
    the four join-provenance states `close_out_and_stamp._determine_
    shipped` itself reports — mirrors that function's own branching
    verbatim (same field reads, same order) since the classification is
    not exposed as a standalone callable there. Does NOT recompute
    anything `_committed_chunk_shas` didn't already capture in its one
    call — this only reads the `DeliverableJoinStats` it returned. `coas`
    is the deferred-imported module (see `_coas()`)."""
    if not join_stats.attempted:
        return JOIN_PROVENANCE_NO_JOIN_KEY
    if join_stats.trailered_commit_count == 0:
        return JOIN_PROVENANCE_NO_JOIN_CANDIDATES
    if join_stats.matched_commit_count > 0:
        return JOIN_PROVENANCE_JOINED
    return JOIN_PROVENANCE_KEY_MISMATCH


@register_op("plan.tasks.spine_drift_check")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.tasks.spine_drift_check" handler — read-only.

    Required params:
        plan_path (str) — path to the plan, absolute or relative to the
                           worktree root.

    Returns:
        {
          "exit_code": 0,
          "schema_version": 1,
          "plan_path": <worktree-relative posix path>,
          "deliverable_id": <str or None>,
          "open_row_count": <int>,          # commit-required rows still `open`
          "drifted_rows": [
            {"chunk_id": ..., "covering_sha": ...}, ...
          ],
          "drifted_row_count": <int>,
          "join_provenance": <one of close_out_and_stamp's four values, or None>,
          "evidence_available": <bool or None>,
          "drift_status": "drift_detected" | "verified_no_drift" | "unknown"
                           | "no_open_rows" | "error",
        }

    A `drifted_rows` entry names an `open` spine row whose chunk-id is
    COVERED (`_committed_id_covers_spine_id`) by a commit the SAME
    Deliverable-Id-trailer join `close_out_and_stamp` itself trusts
    (`_committed_chunk_shas`) — i.e. a row the mutating close-out would
    auto-resolve to `coded` today, had it run.

    `drift_status` is the field callers should branch on, and is set on
    EVERY return path — a caller must never infer it from key-absence:
      - `"drift_detected"` — `drifted_rows` is non-empty: real signal, a
        row this plan's own oracle would auto-resolve today.
      - `"verified_no_drift"` — the join found real evidence for this plan
        (`join_provenance == "joined"`) and no open row was covered by it.
        A genuine negative, backed by a lookup — NOT "we found nothing to
        check" (see `"unknown"` below).
      - `"unknown"` — the join found NOTHING to compare against at all
        (`join_provenance` one of `no_join_key`/`no_join_candidates`/
        `key_mismatch`) — the OVERWHELMINGLY COMMON case on this branch
        (only ~6% of commits carry a chunk-id subject at all; absence of a
        match is NOT evidence of absence of shipped work). `drifted_rows`
        is always `[]` here too, but that `[]` means "no evidence either
        way", not "checked and clean".
      - `"no_open_rows"` — nothing in this plan's spine is `open` and
        commit-required; there was nothing to check.
      - `"error"` — `exit_code` is `1` and `error` names what failed
        (unreadable plan, malformed spine, or the git-log query itself
        failing); every other field in this shape is meaningless.

    `evidence_available` (`join_provenance == "joined"`) is retained
    alongside `drift_status` for callers that want the raw provenance
    value rather than the five-way label.
    """
    if repo_root is None:
        return {
            "exit_code": 1,
            "error": "plan.tasks.spine_drift_check: repo_root is required (no founding root available)",
            "drift_status": DRIFT_STATUS_ERROR,
        }

    plan_path = (params or {}).get("plan_path")
    if not isinstance(plan_path, str) or not plan_path.strip():
        return {
            "exit_code": 1,
            "error": "plan.tasks.spine_drift_check: params.plan_path is required",
            "drift_status": DRIFT_STATUS_ERROR,
        }

    coas = _coas()

    worktree_root = main_worktree_root(repo_root)
    candidate = Path(plan_path)
    plan_file = candidate if candidate.is_absolute() else worktree_root / candidate

    try:
        text = plan_file.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "exit_code": 1,
            "error": f"plan.tasks.spine_drift_check: could not read {plan_path}: {exc}",
            "drift_status": DRIFT_STATUS_ERROR,
        }

    try:
        plan_path_rel = plan_file.resolve().relative_to(worktree_root.resolve()).as_posix()
    except ValueError:
        plan_path_rel = plan_path

    rows, rows_error = coas._parse_spine_rows(text, plan_path_rel)
    if rows_error is not None:
        return {"exit_code": 1, "error": rows_error, "drift_status": DRIFT_STATUS_ERROR}
    if rows is None:
        # Belt-and-braces (Review, 2026-08-21): `_parse_spine_rows`'s own
        # contract pairs `rows=None` with a non-None `error` on every path
        # (MALFORMED spine) -- the branch above already excludes that case
        # at runtime. This is a second, explicit check on the SAME
        # contract rather than a cast or a suppressed type-checker
        # warning, so a future change to that contract fails loud here
        # instead of silently reaching `_open_spine_rows(None, coas)`.
        return {
            "exit_code": 1,
            "error": f"{plan_path_rel}: _parse_spine_rows returned no rows and no error",
            "drift_status": DRIFT_STATUS_ERROR,
        }

    open_rows = _open_spine_rows(rows, coas)
    deliverable_id = coas._plan_deliverable_id(text)

    if not open_rows:
        return {
            "exit_code": 0,
            "schema_version": SCHEMA_VERSION,
            "plan_path": plan_path_rel,
            "deliverable_id": deliverable_id,
            "open_row_count": 0,
            "drifted_rows": [],
            "drifted_row_count": 0,
            "join_provenance": None,
            "evidence_available": None,
            "drift_status": DRIFT_STATUS_NO_OPEN_ROWS,
        }

    spine_ids = coas._all_spine_ids(rows)
    query_ok, _committed, committed_shas, join_stats = _committed_chunk_shas(
        coas,
        worktree_root,
        deliverable_id,
        spine_ids,
        plan_text=text,
        plan_path_rel=plan_path_rel,
    )
    if not query_ok:
        return {
            "exit_code": 1,
            "error": (
                f"{plan_path_rel}: git-log query for landed chunk commits failed -- "
                "cannot determine spine drift mechanically"
            ),
            "drift_status": DRIFT_STATUS_ERROR,
        }

    join_provenance = _join_provenance(join_stats, coas)
    evidence_available = join_provenance == JOIN_PROVENANCE_JOINED

    drifted_rows: list[dict[str, Any]] = []
    if evidence_available:
        for row in open_rows:
            chunk_id = str(row["id"])
            sha = next(
                (
                    committed_shas[committed_id]
                    for committed_id in committed_shas
                    if _committed_id_covers_spine_id(committed_id, chunk_id)
                ),
                None,
            )
            if sha is not None:
                drifted_rows.append({"chunk_id": chunk_id, "covering_sha": sha})

    if drifted_rows:
        drift_status = DRIFT_STATUS_DRIFT_DETECTED
    elif evidence_available:
        drift_status = DRIFT_STATUS_VERIFIED_NO_DRIFT
    else:
        drift_status = DRIFT_STATUS_UNKNOWN

    result: dict[str, Any] = {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "plan_path": plan_path_rel,
        "deliverable_id": deliverable_id,
        "open_row_count": len(open_rows),
        "drifted_rows": drifted_rows,
        "drifted_row_count": len(drifted_rows),
        "join_provenance": join_provenance,
        "evidence_available": evidence_available,
        "drift_status": drift_status,
    }
    if not evidence_available:
        result["evidence_reason"] = _JOIN_PROVENANCE_REASON.get(join_provenance)
    return result
