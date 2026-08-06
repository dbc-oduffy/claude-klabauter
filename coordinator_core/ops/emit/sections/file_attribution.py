"""Section porter — FileAttribution (envelope key: ``file_attributions``).

Emits one FileAttribution row per distinct (session_id, file_path) derived from the
Claude Code natural transcripts. The bash oracle delegates this section ENTIRELY to the
external Python producer ``bin/derive-file-attribution.py`` — it invokes no inline
aggregation logic. The port preserves that delegation: ``collect`` shells out to the
SAME script with the SAME CLI flags and applies the contract fix-ups the bash performs,
plus two porter-owned corrections layered on top (F1/F2 below) that the producer
itself does not and must not implement (F3 of the same fix — this porter, not the
producer, owns contract-boundary shaping; that division of labor holds regardless of who
owns the producer's bytes):

  1. provenance.derivation is rewritten from the module's internal "derived" label to
     "parsed" — the cockpit-contract enum value ("raw"|"parsed"|"rolled_up") that covers
     data extracted by parsing source artifacts (transcripts).
  2. provenance.ref is nullified — file-attribution rows are source_kind
     ``coordinator_artifact``, which requires ref: null per the ProvenanceEnvelope contract
     (D9 / source_kind non-git rule). The derive module stamps ref with git coords; this
     strip makes the emit side contract-valid without waiting for a derive-module fix.

Honesty markers (completeness, capture_source="derived", provenance_completeness) are
worst-case across merged operations and are NEVER stripped at the projection boundary (AC6).
Rows with null file_path are already excluded by the derivation module.

Graceful-absent: absent/empty transcript dir → derive emits [] and exits 0 → [] here.
The derivation module does not produce separate malformed rows on its own, so historically
the malformed bucket was always empty. That changed with the two fixes below.

Producer-failure observability (F1, break-class fix): ``_run_producer`` used to collapse
EVERY failure mode (subprocess error, non-zero exit, unparseable stdout, non-list payload)
to a bare ``[]`` — indistinguishable from "the producer ran and there are genuinely zero
attributed files this run". Combined with ``envelope.emit()``'s unconditional whole-file
overwrite, a producer breakage would have silently wiped the emitted section to ``[]`` and
reported ``ok: True, malformed_total: 0`` — invisible to any consumer, including one only
reading stderr. ``_run_producer`` now returns ``(records, producer_error)``; a non-None
``producer_error`` surfaces as a ``producer_failed: True`` marker row in
``malformed_records.file_attributions`` (the section's existing degraded-record channel —
schema-permissive, no new contract field) plus a loud ``warnings.warn`` — mirroring the
established pattern in ``sections/cross_repo_memos.py``'s ``query_failed`` marker. This
makes the failure visible in the emitted envelope's malformed-records bucket (written to
disk, not merely a stderr line) AND in ``emit()``'s existing ``malformed_counts`` /
``malformed_total`` return values and its stderr WARNING — the channel ``emit()`` already
surfaces for exactly this "quarantine, not clean success" signal. A genuinely empty
transcript dir still yields ``producer_error=None`` and ``[]`` records — the graceful-absent
contract above is unchanged; only real failures are loud now.

Contract-boundary path normalisation (F2, break-class fix): the FileAttribution contract
(``coordinator_core/contract/cockpit_schema/entities/file_attribution.py``) declares
``file_path`` as "Repo-relative path ... directly comparable to git-ls-files output", but
the producer emits whatever absolute path the transcript recorded (this repo, a
sibling repo, an agent scratchpad, a session-transcript path, /tmp, /var/folders — see
``_relativize_or_exclude``). Shaping this in the producer is out of scope (F3 of the
originating defect triage — contract-boundary shaping belongs in the porter, not the
producer, by design, independent of who owns the producer); this porter enforces the
contract at the boundary instead:
in-repo absolute paths are rewritten to forward-slash repo-relative form; out-of-repo/
ephemeral paths cannot satisfy a repo-relative contract and are EXCLUDED from
``file_attributions`` — but, per the same F1 no-silent-narrowing principle, the exclusion
is counted and made visible as an ``excluded: True`` marker row in
``malformed_records.file_attributions`` rather than just dropped.

ATTRIBUTION SCOPE — session transcripts AND subagent transcripts, one level deep. The
producer's ``_iter_transcript_entries`` enumerates each top-level ``<session>.jsonl`` plus
that session's own ``<session>/subagents/agent-*.jsonl``, so a subagent's Read/Edit/Write
touches are attributed rather than counted nowhere. Each subagent transcript's OWN stem
becomes the row's ``session_id``, so an agent keeps its identity on the row and the
contract's ``(session_id, file_path)`` key needs no new field. Recursion is exactly one
level (``<session>/subagents/``), deliberately NOT a general ``os.walk``: a future
transcript format that drops unrelated directories under a session dir must not be swept
in. Nested transcripts run through the same ``process_transcript`` path — one parsing
implementation, not two. Landed by ``e6449947`` (2026-07-29), measured at 4,946 -> 22,184
aggregate rows.

History, because the reversal is the load-bearing part (this paragraph is a record, NOT a
live claim): the read was non-recursive until ``e6449947``, and coverage was measured at
**34.7%** of a 2,924-file ``git ls-files`` union on the example-doctrine-repo corpus — 1,910 tracked
files attributed to nobody, the section failing at its stated job in a fleet where
subagents do most of the editing. DR-244 originally forbade the recursive read on the
premise that the additional (agent, file) pairs would overrun a 600-row publish cap.
**That premise was withdrawn** (``7c9d3889``) once cockpit supplied the value answer:
``FILE_ATTRIBUTIONS_PUBLISH_CAP`` guards only Firestore's 1 MiB limit on cockpit's hosted-web
published slice, its placement in a shared loader was cockpit's own defect, and the rag
projection and MCP read raw uncapped rows. For a query surface **coverage beats recency** —
a question answered over 34.7% of the corpus is answered wrong, quietly.

NEGATIVE-SPEC on future edits, both constraints live and both from DR-244 § Amendment
(2026-07-29):

  - Do NOT collapse a subagent's touches onto its parent session. That is the same lossy
    pre-aggregation cockpit rejected for the rows themselves — a query surface can aggregate
    at read time and can never disaggregate what was pre-summed. A parent-session LINK is
    the thing that would need a new contract field; route that through example-doctrine-repo rather than
    inventing it here.
  - Do NOT pre-aggregate into per-file totals or per-session counts, which locks consumers
    out of the other axis.

Citing DR-244's ORIGINAL text to restore the non-recursive read is NOT valid — its
2026-07-29 amendment answers Q3 and governs. Read the amendment, not only the decision.

Port of: emit-cockpit-snapshot.sh (example-doctrine-repo 07eedcfb, 2026-07-19) — § SECTION 8.14,
  FileAttribution. Byte/semantic parity port.
Producer: coordinator/bin/derive-file-attribution.py — claude-klabauter's own file, resolved via
  ``ctx.coordinator_root`` (a ``__file__`` walk from this repo, not a example-doctrine-repo/upstream path);
  change-controlled rather than frozen (a ``_DERIVATION_VERSION`` bump is required on any
  behavioural change, since the module's own stat cache can't detect a logic-only edit).
  Called the SAME way via subprocess.
Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § P17
"""

from __future__ import annotations

import json
import ntpath
import posixpath
import re
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.emit import skipped_stage as _skipped_stage
from coordinator_core.ops.emit.context import EmitContext

# Path of the file-attribution derivation module (claude-klabauter-owned, change-controlled — not
# frozen), relative to the coordinator (meta-repo) root. Mirrors bash
# "$SCRIPT_DIR/derive-file-attribution.py" where SCRIPT_DIR was the emitter's bin/ dir.
_PRODUCER_REL = ("bin", "derive-file-attribution.py")

# Reason prefix stamped on the synthetic malformed-bucket marker row emitted when the
# producer subprocess itself fails. Distinguishes "producer broke" from "producer ran and
# validly returned zero/malformed rows" — see module docstring F1.
_PRODUCER_FAILED_REASON_PREFIX = "derive-file-attribution.py producer failed"

# Reason stamped on the synthetic malformed-bucket marker row emitted for a row whose
# file_path could not be expressed as a repo-relative path — see module docstring F2.
_EXCLUDED_REASON = (
    "file_path is outside the emitting repo (or unresolvable) — cannot satisfy the "
    "repo-relative file_path contract; excluded from file_attributions"
)

# Reason stamped on the synthetic malformed-bucket marker row emitted when the producer's
# JSON array contains a non-dict element (e.g. a bare string/number/null instead of a
# record object). Review: code-reviewer — the prior bare `continue` on this branch dropped
# such elements uncounted, contradicting the same F1 no-silent-narrowing principle this
# module states for every other exclusion path.
_NON_DICT_RECORD_REASON = (
    "producer emitted a non-dict array element — cannot be interpreted as a "
    "file-attribution record"
)

# Ceiling for the producer subprocess. This is a fail-open safety guard, not a latency SLA —
# the section runs only on the full-enrichment cadence tier, so a generous ceiling costs
# nothing on the hot path. The budget must cover a COLD full-corpus scan, not a warm one, and
# cold is a routine path rather than an edge case: any bump of the producer's own
# _DERIVATION_VERSION invalidates the whole stat cache and forces a full cold run on the very
# next call, and the transcript corpus only grows monotonically. Tripping this ceiling reaches
# the F1 path and stamps an empty file_attributions array, i.e. silent data loss, which is why
# the margin is deliberately generous rather than tight.
#
# Sized on order of magnitude, NOT on a precise measurement, because the absolute cold cost
# does not reproduce: three runs of the same nominal quantity (non-recursive path, empty stat
# cache, example-doctrine-repo corpus) gave 14.12s, 2.56s and 4.87s, varying with OS page-cache state and
# concurrent load. The replicating quantity is the ratio — the 2026-07-29 recursive-read change
# costs ~2.5x the non-recursive path (12.16s vs 4.87s, measured back-to-back in one run). All
# observed cold readings sit an order of magnitude under this ceiling, which is the property
# that matters; a fast reading means a warm page cache, not cheaper work, so do not tighten
# this on the strength of one. Wall-clock, fresh processes, PYTHONHASHSEED pinned, no cProfile
# (it has over-attributed this op four times).
_PRODUCER_TIMEOUT_SECONDS = 180

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _extract_prior_file_attributions(prior: dict):
    """Read the last-emitted ``file_attributions`` array out of *prior*.

    Returns ``skipped_stage.absent()`` unless the prior emission carries a list under that
    key — an absent or non-list value is no last-known value at all, and under this stage's
    ``MUST_COMPUTE`` policy that forces a real compute rather than any synthesised stand-in.
    """
    rows = prior.get("file_attributions")
    if not isinstance(rows, list):
        return _skipped_stage.absent()
    return rows


def _validate_prior_file_attributions(rows: Any, repo_root: Path) -> Optional[str]:
    """Return None if *rows* satisfy the F2 ``file_path`` contract, else a short reason.

    The predicate is ``_relativize_or_exclude`` ITSELF — the F2 implementation the computed
    path uses — not a second predicate that agrees with it on the cases someone thought of. A
    row passes only when the computed path would have emitted it UNCHANGED: anything
    ``_relativize_or_exclude`` would exclude (returns None) or rewrite (returns a different
    string) is a row whose reuse is not equivalent to computing it, so the whole batch is
    rejected and recomputed.

    Review: code-reviewer (Findings 1-3) — the first cut hand-rolled the check as
    ``_is_absolute(path, _is_windows_shaped(path))``, a strict subset of F2 that ACCEPTED three
    row shapes the computed path excludes, each confirmed by direct call: a leading-``..``
    relative path (``"../sibling-repo/file.py"`` — relative, so never absolute), a
    backslash-relative path (``"subdir\\file.py"`` — Windows-shaped but matching neither the
    drive-letter regex nor the UNC prefix), and a missing/None/empty ``file_path`` (the loop
    ``continue``d instead of rejecting). Delegating is the fix rather than widening
    case-by-case: two implementations of one contract boundary is how they drift apart, which
    is ``skipped_stage``'s own negative-spec — and this validator was in breach of it.

    Why this exists at all: example-doctrine-repo's on-disk emission (2026-07-28, schema 3.7.0) carries
    4027 rows of which 3991 have ABSOLUTE ``file_path`` values and zero exclusion markers — it
    predates F2. Reusing it emitted absolute paths into a rag/cockpit join key, resurrected the
    out-of-repo rows F2 excludes, and hid the re-widening (no markers). Validated reuse turns
    that into a loud fall-through to a real compute.

    Deliberately NOT a repair pass: re-normalising stale rows here would be a second
    implementation of F2, and the computed path already does it correctly. Reject and
    recompute — see ``skipped_stage``'s negative-spec.

    Checks only ``file_path`` (plus each row being an object at all), the one field F2 owns.
    This is not a whole-contract validator and must not grow into one — schema conformance is
    ``validate.py``'s job; this is the narrow "is the reused value as good as a computed one"
    gate.
    """
    if not isinstance(rows, list):
        return f"expected a list of rows, got {type(rows).__name__}"
    non_dict = 0
    offenders = 0
    first: Optional[str] = None
    for row in rows:
        if not isinstance(row, dict):
            # The computed path quarantines these into malformed_records; a reused batch that
            # still carries them is not equivalent to a computed one.
            non_dict += 1
            continue
        original = row.get("file_path")
        relativized = _relativize_or_exclude(original, repo_root)
        if relativized is None or relativized != original:
            offenders += 1
            if first is None:
                first = original if isinstance(original, str) else repr(original)
    if offenders or non_dict:
        parts = []
        if offenders:
            parts.append(
                f"{offenders} of {len(rows)} rows carry a file_path the computed path would "
                f"exclude or rewrite (contract requires repo-relative, "
                f"git-ls-files-comparable), e.g. {first!r}"
            )
        if non_dict:
            parts.append(f"{non_dict} of {len(rows)} entries are not objects")
        return (
            "; ".join(parts)
            + " — the prior emission predates or bypassed this porter's F2 path normalisation"
        )
    return None


def _extract_prior_file_attribution_malformed(prior: dict):
    """Read the last-emitted ``malformed_records.file_attributions`` array out of *prior*.

    Carried alongside the rows on the skipped path so the reused emission stays internally
    consistent: the F1 ``producer_failed`` marker and the F2 ``excluded`` markers are the
    honesty record FOR those rows, and reusing rows while dropping their markers would
    silently upgrade a degraded prior run to a clean-looking one. Absent/non-list yields an
    empty malformed bucket, which is not a narrowing claim — the malformed bucket is a
    quarantine channel, and "the prior run recorded no quarantine" is what empty means there.
    """
    buckets = prior.get("malformed_records")
    if not isinstance(buckets, dict):
        return []
    rows = buckets.get("file_attributions")
    if not isinstance(rows, list):
        return []
    return rows


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Return (records, malformed) for the file-attribution section.

    Delegates to bin/derive-file-attribution.py with the bash's exact CLI flags:
        --project <ROOT> --repo-name <REPO_NAME> --git-branch <GIT_BRANCH>
        --git-sha <GIT_SHA> --observed-at <OBSERVED_AT>
    then applies the two contract fix-ups (provenance.derivation="parsed",
    provenance.ref=null), the F2 path-normalisation/exclusion pass, and the F1
    producer-failure observability signal (see module docstring for all three).

    CADENCE GATE (2026-07-29, Lever 3): the producer walks every top-level transcript file
    (557 on the example-doctrine-repo corpus), so it runs only on the full-enrichment tier. For this
    walk's cost see ``_PRODUCER_TIMEOUT_SECONDS`` — the single place those figures are
    stated; this docstring used to restate them and drifted to a pre-re-measurement pair
    (``~0.22s warm, ~2.3s cold``) that DR-244 then refuted, which is the whole reason the
    numbers now live in exactly one place. On the cheap tier this section REUSES the
    previously-emitted rows
    from the canonical on-disk emission, and falls through to a real compute when there is no
    prior emission to reuse — ``skipped_stage.MUST_COMPUTE``, because ``file_attributions`` is
    a required array whose ``[]`` is structurally schema-valid and semantically a lie: every
    consumer reads it as "no attributions exist". That is precisely the silent-narrowing
    failure this module's own F1/F2 fixes exist to prevent, so the gate must never be allowed
    to reintroduce it through the back door.

    The reuse is VALIDATED (``_validate_prior_file_attributions``): a prior emission whose
    ``file_path`` values are absolute — i.e. one predating F2, which is the live state of
    example-doctrine-repo's on-disk emission — is REJECTED and recomputed, not reused. Unvalidated reuse
    propagates exactly the contract rot F2 exists to remove, from the opposite direction. This
    means the cheap tier pays full producer cost on any repo whose last emission is stale,
    which is the correct trade and a real caveat on the lever's saving.

    That staleness SELF-HEALS after one paid run of EITHER tier, not only after a full-tier
    emit. A cheap-tier run that fails validation falls through to this same ``_run_producer``
    path, and ``envelope.emit``'s write target does not depend on the tier at all: a default
    run writes the canonical ``central_state_root`` artifact — the same file
    ``skipped_stage`` reads as "prior" — whether ``ctx.full_enrichment`` was True or False. So
    the next cheap-tier run already sees a fresh, valid prior and reuses it. (A run diverted to
    a scratch path via ``out`` heals nothing, by design: that is the same reason the reuse
    source is the canonical path and never ``out``.)
    Review: code-reviewer (Finding 4) — this paragraph and the commit message both said the
    cost persisted "until a full-tier emit refreshes the artifact", which would send an
    operator hunting for a full-tier run they do not need.
    """
    if not ctx.full_enrichment:
        satisfied, reused = _skipped_stage.skipped_stage_value(
            ctx,
            stage="file_attributions",
            extract=_extract_prior_file_attributions,
            absent_policy=_skipped_stage.MUST_COMPUTE,
            # Closure rather than a bare reference: the F2 predicate needs the repo root to
            # evaluate containment, and threading ctx through skipped_stage would make that
            # module know about this stage's shape — the thing its extract/validate seams exist
            # to avoid.
            validate=lambda rows: _validate_prior_file_attributions(rows, ctx.repo_root),
        )
        if satisfied:
            _, reused_malformed = _skipped_stage.skipped_stage_value(
                ctx,
                stage="file_attributions.malformed",
                extract=_extract_prior_file_attribution_malformed,
                absent_policy=_skipped_stage.NULL_SANCTIONED,
                # The markers describe rows this run already validated, and the extractor
                # normalises absent/non-list to []. There is no contract for a marker's shape
                # that F2 owns, so there is nothing here for a validator to check.
                validate=lambda _v: None,
            )
            return list(reused), list(reused_malformed or [])
        # No prior emission to reuse — fall through and compute for real.

    producer = ctx.coordinator_root.joinpath(*_PRODUCER_REL)

    records, producer_error = _run_producer(
        producer,
        "--project", str(ctx.repo_root),
        "--repo-name", ctx.repo_name,
        "--git-branch", ctx.git_branch,
        "--git-sha", ctx.git_sha,
        "--observed-at", ctx.observed_at,
    )

    malformed: list[dict] = []

    if producer_error is not None:
        # Loud observability signal (mirrors sections/cross_repo_memos.py's query_failed
        # convention) — a producer failure must never look like "zero files attributed" to
        # a consumer scanning logs, stderr, or the return value alone.
        warnings.warn(
            f"file_attribution: {_PRODUCER_FAILED_REASON_PREFIX}: {producer_error}; "
            "emitting file_attributions as empty. This is a PRODUCER FAILURE, not an "
            "absence of file attribution data — see malformed_records.file_attributions "
            "for the producer_failed marker row.",
            stacklevel=2,
        )
        malformed.append({
            "path": None,
            "reason": f"{_PRODUCER_FAILED_REASON_PREFIX}: {producer_error}",
            "producer_failed": True,
        })
        return [], malformed

    in_repo_records: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            # Review: code-reviewer (Finding 1) — count and mark, never silently drop;
            # the old bare `continue` here violated this module's own no-silent-narrowing
            # principle (and was introduced by this diff — the prior code had no isinstance
            # guard and would have raised AttributeError instead).
            malformed.append({
                "path": None,
                "reason": _NON_DICT_RECORD_REASON,
                "malformed_type": True,
            })
            continue
        provenance = record.get("provenance")
        if isinstance(provenance, dict):
            provenance["derivation"] = "parsed"
            provenance["ref"] = None

        original_path = record.get("file_path")
        relativized = _relativize_or_exclude(original_path, ctx.repo_root)
        if relativized is None:
            malformed.append({
                "path": original_path if isinstance(original_path, str) else None,
                "reason": _EXCLUDED_REASON,
                "excluded": True,
                "session_id": record.get("session_id"),
            })
            continue

        record["file_path"] = relativized
        in_repo_records.append(record)

    return in_repo_records, malformed


def _run_producer(producer, *args: str) -> "tuple[list[dict], Optional[str]]":
    """Invoke the derivation module and parse its JSON array.

    Returns ``(records, producer_error)``. ``producer_error`` is ``None`` on success —
    including a genuine empty result (``[]``), where the producer ran cleanly and validly
    reported zero attributed files — and a short diagnostic string on ANY failure mode
    (subprocess spawn error, timeout, non-zero exit, unparseable stdout, non-list payload).
    ``records`` collapses to ``[]`` on every failure path (fail-open — emission never
    aborts), but the caller now receives an explicit failure signal instead of an
    indistinguishable empty list. See module docstring F1.

    Deliberate isolation boundary — do not convert this call site to an
    in-process import on its own. The producer, `bin/derive-file-attribution.py`,
    is a FROZEN external script — same frozen-producer shape as sibling
    sections/lessons.py's `_run_producer` (which targets
    `bin/lib/emit-lesson-summaries.py`); converting requires first porting
    the producer under `coordinator_core` — that is a port, not a call-site
    edit. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    try:
        out = subprocess.run(
            # sys.executable (not hardcoded "python3", which is absent on Windows →
            # OSError → silent []); mirrors sibling sections/lessons.py. CREATE_NO_WINDOW
            # suppresses a console-window pop on win32 (no-op elsewhere).
            [sys.executable, str(producer), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PRODUCER_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError) as exc:
        return [], f"subprocess spawn raised {type(exc).__name__}: {exc}"
    except subprocess.TimeoutExpired as exc:
        return [], f"subprocess timed out after {exc.timeout}s"

    if out.returncode != 0:
        stderr_tail = (out.stderr or "").strip()[-500:]
        return [], f"producer exited with code {out.returncode}: {stderr_tail}"

    try:
        parsed = json.loads(out.stdout or "[]")
    except (json.JSONDecodeError, ValueError) as exc:
        stdout_tail = (out.stdout or "").strip()[-200:]
        return [], f"producer stdout was not valid JSON ({exc}): {stdout_tail!r}"

    if not isinstance(parsed, list):
        return [], f"producer returned non-list JSON payload ({type(parsed).__name__})"

    return parsed, None


def _is_windows_shaped(path_str: str) -> bool:
    """True when *path_str* looks like a Windows path (drive letter or backslashes).

    Used to pick the correct pure-path flavour for normalisation independent of the HOST
    platform this porter happens to run on — a Windows-shaped path must normalise/compare
    correctly whether this code executes on Windows, macOS, or Linux (matches this repo's
    Windows-and-macOS-first-class convention).
    """
    return bool(_WINDOWS_DRIVE_RE.match(path_str)) or "\\" in path_str


def _normalize_components(path_str: str) -> "tuple[list[str], bool]":
    """Normalise *path_str* into ``(components, is_windows)``.

    Collapses ``.``/``..`` segments and both separator styles via the stdlib's own
    ``ntpath``/``posixpath`` normalisation (no filesystem access — this must stay correct
    for paths that do not exist on THIS host, e.g. a Windows path normalised on a macOS
    test run). A Windows drive letter, when present, is folded into the component list as
    its own uppercased entry so drive-letter case differences (``c:`` vs ``C:``) do not
    defeat containment matching.
    """
    if _is_windows_shaped(path_str):
        normalized = ntpath.normpath(path_str)
        drive, rest = ntpath.splitdrive(normalized)
        parts = [p for p in rest.split(ntpath.sep) if p]
        if drive:
            parts = [drive.upper()] + parts
        return parts, True
    normalized = posixpath.normpath(path_str)
    parts = [p for p in normalized.split(posixpath.sep) if p]
    return parts, False


def _is_absolute(path_str: str, is_windows: bool) -> bool:
    if is_windows:
        return bool(_WINDOWS_DRIVE_RE.match(path_str)) or path_str.startswith("\\\\")
    return path_str.startswith("/")


def _relativize_or_exclude(file_path: Any, repo_root: Path) -> Optional[str]:
    """Return a forward-slash repo-relative path, or ``None`` if *file_path* cannot be
    expressed as one (out-of-repo, ephemeral, or malformed input).

    Robust containment check (never a string ``startswith``, which would false-positive on
    a sibling repo sharing a path prefix, e.g. ``claude-klabauter`` vs ``claude_klabauter2``):
    both *file_path* and *repo_root* are decomposed into path COMPONENTS (via
    ``_normalize_components``) and compared component-by-component, so containment is
    always evaluated on segment boundaries. Windows drive-letter/backslash paths are
    handled via ``ntpath`` regardless of the host OS running this code, and
    Windows-shaped comparisons are therefore host-OS independent. POSIX-shaped
    comparisons casefold per the ACTUAL running host's ``sys.platform`` (macOS
    case-insensitive, other POSIX case-sensitive) — this is correct at runtime because
    *repo_root* and *file_path* always originate on the same live host, but it is not
    host-OS independent the way the Windows-shaped branch is.
    Review: code-reviewer (Finding 5) — narrowed from a prior "regardless of the host OS"
    claim that overstated the POSIX branch's behaviour.

    An already-relative *file_path* (no drive letter, doesn't start with ``/`` or ``\\\\``)
    is treated as already repo-relative — normalised (separators/``.``/``..`` collapsed,
    forward-slash form) and returned as-is; this preserves any rows the producer already
    emits correctly relativized. The one exception is a normalised result starting with a
    ``..`` component (e.g. ``"../sibling-repo/file.py"``), which is excluded rather than
    passed through — see the leading-``..`` check below.
    """
    if not isinstance(file_path, str) or not file_path:
        return None

    cand_parts, cand_is_win = _normalize_components(file_path)
    if not cand_parts:
        return None

    if not _is_absolute(file_path, cand_is_win):
        if cand_parts[0] == "..":
            # Review: code-reviewer (Finding 2) — a normalized relative path with a
            # leading ".." (e.g. "../sibling-repo/file.py") escapes repo_root and can
            # never appear in `git ls-files` output, so it cannot satisfy the contract's
            # "directly comparable to git-ls-files output" bar. Exclude it the same way
            # an absolute out-of-repo path is excluded, rather than waving it through as
            # "already repo-relative".
            return None
        return "/".join(cand_parts)

    root_parts, root_is_win = _normalize_components(str(repo_root))
    if cand_is_win != root_is_win:
        # A Windows-shaped absolute path can never live under a POSIX-shaped repo root
        # (and vice versa) — different filesystem namespaces entirely.
        return None

    if len(cand_parts) <= len(root_parts):
        return None

    casefold = cand_is_win or sys.platform == "darwin"
    fold = (lambda s: s.lower()) if casefold else (lambda s: s)
    if [fold(p) for p in cand_parts[: len(root_parts)]] != [fold(p) for p in root_parts]:
        return None

    return "/".join(cand_parts[len(root_parts):])
