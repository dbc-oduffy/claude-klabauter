"""
chain_ancestry_waivers.py

Shared home for the chain-ancestry waiver artifact's directory name and
on-disk path shape. The PM-vouch waiver shape it once stood alongside
(coordinator_core.coverage._pm_vouched_waiver_shas /
coordinator_core.ops.review_trail_write's PM-vouch waiver recording) was
deleted in docs/plans/2026-08-08-vouch-free-review-coverage-gates.md — this
store is now the sole waiver source for coverage crediting.

Why a THIRD module, not folded into coverage.py or review_trail_write.py:
this plan (docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md
§ C1) adds a third write site for a waiver-shaped artifact
(coordinator_core.ops.coverage_gate, landing in C2) alongside the two
existing readers/writers of the PM-vouch shape that then existed.
`_PM_VOUCH_WAIVER_DIRNAME` was ALREADY duplicated across coverage.py and
review_trail_write.py; adding a third copy of a NEW constant, rather than
giving the new artifact one canonical home, is exactly the shape this
plan's Anti-scope explicitly permits avoiding ("This does NOT forbid one
shared dirname/path-builder module for the new artifact alone").

Anti-scope, hard (binding on later chunks too): this module holds ONLY the
chain-ancestry waiver's path shape and its own read/write primitives. It
must NOT grow a parameter that lets a caller pick between this shape and
`pm-vouches/`, and must NOT become a shared, parameterised store for both
waiver kinds — they stay distinct artifact kinds with distinct provenance
and distinct trust bases (gate-derived vs. PM-granted), per AC3.

Spec backlink: pln-teach-the-review-trail-foreign-fa3c96 § C1
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import FrozenSet, Optional, Sequence

from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.session.resolve_chain_terminal_disposition import (
    classify_chain_terminal_disposition,
)
from coordinator_core.win_portability import no_console_creationflags

logger = logging.getLogger(__name__)

#: state/review-trail/ subdirectory holding permanent, idempotent,
#: chain-scoped waivers minted by the coverage gate (C2) whenever a caller
#: passes `mint_chain_waivers=True` and `from_handoff` and the gate's
#: `result.uncovered_shas` is non-empty — NOT gated on `result.verdict`
#: (that leg was dropped 2026-08-07,
#: state/audits/2026-08-07-review-gate-scoping-predecessor-and-planning-artifacts.md;
#: "minted at HALT" is stale prose from before that fix). Kept physically
#: separate from DR-243's `pm-vouches/` so a gate-derived waiver and a
#: PM-granted one are never confused on disk (AC3's audit distinction lives
#: in the directory name, not in a shared reader).
CHAIN_ANCESTRY_WAIVER_DIRNAME = "chain-ancestry-waivers"

#: Directory-name-safety guard for `chain_id` (expected value: the closing
#: session's UUID — see coverage.py's `_derive_dag_chain_set`'s
#: `closing_session_id` parameter, the "which derivation minted it" chain
#: identity this plan's C1 body names). Mirrors the shape of coverage.py's
#: own `_UUID_RE` / archive_stamp.py's `_SESSION_ID_UUID_RE` — this module
#: deliberately does NOT import either (session_attribution.py already
#: documents that cross-module unification of these UUID patterns was a
#: dated, deliberate non-goal). This copy exists purely to keep an
#: attacker- or bug-controlled string from escaping the waiver root as a
#: directory traversal (`..`, path separators) when used as a directory
#: name — it is a path-safety guard, not a session-id semantics validator.
# Review: code-reviewer — `$` matches at end-of-string OR immediately before
# one trailing "\n", so a chain_id ending in a literal newline would pass
# this guard and the newline would land in a directory name. `\Z` matches
# ONLY true end-of-string, closing that gap for a validator whose entire
# job is path-name safety.
_CHAIN_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{0,127}[0-9a-fA-F]\Z")


def chain_root_dir(cwd: str) -> Path:
    """`<cwd>/state/review-trail/chain-ancestry-waivers` — the parent of
    every per-chain subdirectory. Nothing is ever written directly here;
    every waiver file lives one level down, under its minting chain's own
    subdirectory (see `chain_waiver_dir`).
    """
    return Path(cwd) / "state" / "review-trail" / CHAIN_ANCESTRY_WAIVER_DIRNAME


def chain_waiver_dir(cwd: str, chain_id: str) -> Optional[Path]:
    """Path for one chain's waiver subdirectory, or None if `chain_id` fails
    the directory-name-safety shape check (`_CHAIN_ID_RE`). Callers MUST
    treat None as "cannot resolve a path for this chain_id" — never as a
    repo-root or root-directory fallback.
    """
    if not chain_id or not _CHAIN_ID_RE.match(chain_id):
        return None
    return chain_root_dir(cwd) / chain_id


#: Fixed, machine-readable statement of what this artifact's own trust basis
#: is — written into every minted record so a reader who finds one on disk can
#: answer "reviewed under chain X, or closed without reviewing?" from the file
#: itself. DR-245 § "The disclosed limit" already states this limit in prose
#: ("a visibility gain, not a discrimination gain ... whether or not any human
#: individually opened it"), but a decision record is not what an auditor finds
#: when they open `state/review-trail/chain-ancestry-waivers/<chain>/<sha>.json`
#: — the schema-v1 record carried no reviewer, verdict, reason or disposition,
#: and therefore read as a coverage certification it never was. Per this repo's
#: discharge test (CLAUDE.md § North star): the artifact discharges the claim,
#: not the operator's memory of a DR.
_MINT_BASIS = "dag-mode-uncovered-shas-at-chain-terminal-close"

_READER_NOTE = (
    "This waiver records ONLY that this commit was in the minting chain's walked "
    "ancestry when the coverage gate found it uncovered at a chain-terminal close. "
    "The gate does not necessarily HALT to reach this point: a chain whose only "
    "uncovered commits are planning artifacts nets a COVERED verdict and still "
    "mints. It is NOT a record that anyone reviewed the commit, and no reviewer, verdict or "
    "justification is implied by its existence. Its sole effect is to relax the "
    "foreign-session strip (coverage.py::_narrow_foreign_session_scope) so that a "
    "close record whose range covers this commit MAY credit it. Crediting is "
    "range-based, not per-SHA. See docs/decisions/"
    "DR-245-gate-minted-chain-ancestry-waivers-supersede-in.md "
    '§ "The disclosed limit". An `em_disposition` of null means no EM recorded a '
    "review disposition at close — read that as 'ancestry NOT reviewed by this "
    "close', never as 'reviewed'."
)


def repo_relative_source_handoff(cwd: str, source_handoff: Optional[str]) -> Optional[str]:
    """Normalize `source_handoff` to a path relative to `cwd` before it is
    persisted into a waiver record.

    Waiver records land under a consumer repo's committed `state/` tree and
    travel with the repo across clones/machines. `source_handoff` arrives
    here as a machine-absolute in-process path (correct for the caller's own
    seam) — storing that verbatim bakes one contributor's home directory
    into a committed artifact, which is wrong the moment anyone else clones
    the repo.

    Negative-spec: an absolute path OUTSIDE `cwd` is stored UNCHANGED, never
    rewritten via `..` traversal — a `../../elsewhere/foo` string looks
    repo-relative but is a lie about what travels with the repo, which is
    worse than an honest absolute path a human can recognize as
    machine-local. A relative path, `None`, or an empty string pass through
    unchanged. Symlinks are resolved on both sides before comparing (e.g.
    macOS `/tmp` vs `/private/tmp`) so the containment check isn't fooled by
    a symlinked repo root. This function must never raise — mirrors this
    module's best-effort persistence posture; any failure falls back to the
    original value unchanged.
    """
    if not source_handoff:
        return source_handoff
    try:
        candidate = Path(source_handoff)
        if not candidate.is_absolute():
            return source_handoff
        real_cwd = Path(os.path.realpath(cwd))
        real_candidate = Path(os.path.realpath(source_handoff))
        relative = real_candidate.relative_to(real_cwd)
        return relative.as_posix()
    except Exception:
        return source_handoff


def _session_id_trailers_batch(cwd: str, shas: Sequence[str]) -> dict[str, list[str]]:
    """Map each sha in `shas` to the non-empty lines of its own commit-message
    `Session-Id:` trailer, resolved in ONE `git log --no-walk` over every sha
    rather than one spawn per sha.

    Framing is NUL-delimited (`%x00%H%x00<trailers>`) precisely because a
    trailer value is not guaranteed to be single-line: a multi-valued trailer
    renders as several lines, and a newline-delimited format could not tell
    "second line of this commit's trailer" from "next commit's record". Callers
    read a len != 1 line list as ambiguous, so mis-framing here would silently
    change a verdict.

    Returns `{}` on any git failure, and omits any sha git did not report. Both
    are read by the caller as "no evidence" -> PROCEED, preserving
    `_refuse_if_live_foreign_chain_sha`'s terminal-safe contract: a single
    unresolvable sha makes `git log` fail for the whole batch, which degrades
    every sha to proceed, exactly as the per-sha form degraded each one
    individually.

    Keys are git's own full object names (`%H`). An abbreviated input sha is
    re-attached by unique prefix, and a prefix matching more than one reported
    commit is dropped rather than guessed.
    """
    if not shas:
        return {}
    try:
        proc = subprocess.run(
            [
                "git", "log", "--no-walk",
                "--format=%x00%H%x00%(trailers:key=Session-Id,valueonly)",
                *shas,
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}

    by_full_sha: dict[str, list[str]] = {}
    fields = proc.stdout.split("\x00")
    for i in range(1, len(fields) - 1, 2):
        full_sha = fields[i].strip()
        if full_sha:
            by_full_sha[full_sha] = [ln.strip() for ln in fields[i + 1].splitlines() if ln.strip()]

    resolved: dict[str, list[str]] = {}
    for sha in shas:
        if sha in by_full_sha:
            resolved[sha] = by_full_sha[sha]
            continue
        matches = [full for full in by_full_sha if full.startswith(sha)]
        if len(matches) == 1:
            resolved[sha] = by_full_sha[matches[0]]
    return resolved


def _refusal_from_trailer_lines(
    cwd: str, sha: str, chain_id: str, lines: Optional[list[str]]
) -> Optional[str]:
    """The refusal DECISION for one sha, given its already-resolved trailer
    lines (`None` when git could not report them). Split out of
    `_refuse_if_live_foreign_chain_sha` so the decision can be applied over a
    batched trailer read without one git spawn per sha; the terminal-safe rules
    live here and are shared by both the single and batched entry points."""
    if lines is None or len(lines) != 1:
        # Absent (untrailered/unreported) or ambiguous (multi-valued trailer,
        # which `%(trailers:valueonly)` renders as more than one line) — cannot
        # positively establish a foreign owner, proceed.
        return None
    from coordinator_core.session.liveness import session_live

    trailer_sid = lines[0]
    if not trailer_sid or trailer_sid == chain_id:
        return None
    if not session_live(trailer_sid, cwd=cwd):
        return None
    return (
        f"sha {sha} carries Session-Id trailer {trailer_sid!r}, a LIVE "
        f"session different from the minting chain {chain_id!r} — refusing "
        "to mint a chain-ancestry waiver that would self-relax ancestry "
        "review for a foreign in-flight commit"
    )


def _refuse_if_live_foreign_chain_sha(cwd: str, sha: str, chain_id: str) -> Optional[str]:
    """Refuse to mint a chain-ancestry waiver for `sha` under `chain_id` when
    `sha`'s own commit-message `Session-Id:` trailer names a DIFFERENT
    session that is currently LIVE — defence-in-depth for the same
    session-shape-misdetection incident `plan_status_transition._refuse_
    if_live_foreign_holder` and `coordinator_complete_entry._refuse_if_live_
    foreign_entry_holder` guard against (cross-repo memo `2026-08-10-
    example-retrieval-repo-em-wsc-misdetection-wrote-to-a-live-peers-plan.md`): a
    misclassified session would otherwise mint a PERMANENT per-(sha,
    chain_id) waiver under ITS OWN `chain_id` for a commit it does not own
    — self-relaxing ancestry review (`coverage.py::_narrow_foreign_session_
    scope`) for a live peer's own in-flight commit, under the closing
    session's own chain identity. Waivers are already chain-scoped
    (`chain_ancestry_waived_shas` matches by exact `chain_id` equality, so
    a waiver minted under chain A cannot relax chain B) — this closes the
    remaining gap: a session self-relaxing review for commits it does not
    own, under its own chain.

    Returns a human-readable refusal reason (non-None -> caller must skip
    minting THIS sha) or `None` (proceed). TERMINAL-SAFE by construction,
    mirroring both sibling guards exactly: a git failure, an unresolvable
    log, an absent/ambiguous (merge, multi-valued) trailer, self-authorship
    (trailer == `chain_id`), or a dead holder all proceed — this only fires
    on a POSITIVELY-established LIVE foreign owner, never on absence of
    evidence. A guard that blocked on absence would wedge the ordinary
    halt->disposition->re-run path this revert exists to restore.
    """
    return _refusal_from_trailer_lines(
        cwd, sha, chain_id, _session_id_trailers_batch(cwd, [sha]).get(sha)
    )


def record_chain_ancestry_waiver(
    cwd: str,
    shas: FrozenSet[str],
    chain_id: str,
    source_handoff: Optional[str] = None,
    em_disposition: Optional[str] = None,
) -> FrozenSet[str]:
    """Persist an idempotent, PERMANENT per-(sha, chain_id) waiver under
    `state/review-trail/chain-ancestry-waivers/<chain_id>/<sha>.json` for
    each sha in `shas`, and return the (possibly empty) subset of `shas`
    REFUSED — never minted — because `_refuse_if_live_foreign_chain_sha`
    positively established a LIVE foreign owner for that sha (2026-08-10,
    see that function's own docstring for the incident and the
    terminal-safe discriminator). This is the sole write site for this
    artifact (every caller — `coordinator_core.ops.coverage_gate`, the
    ceremony-close subcommands in `coordinator/bin/wsc-coverage-gate-
    runner.py` — routes through this one function), so the refusal is
    enforced here rather than at any individual caller, and cannot be
    bypassed by a caller that forgets to re-check ownership itself. Refused
    shas are logged loudly (`logger.warning`, one line per sha) so an
    operator relying on silence == "minted everything" is never misled —
    callers that want this surfaced further (e.g. into a ceremony's own
    stderr/notes) should inspect the returned set, not just this function's
    log output.

    `em_disposition` is the closing EM's stated disposition toward the
    ancestry this mint waives, or None when none was recorded. It is written
    into every record as an EXPLICIT field precisely so that "no disposition
    was recorded" is a stated fact on disk rather than an absent one — a
    schema with no field for it left a reader unable to distinguish "reviewed
    under chain X" from "closed without reviewing", which is the defect
    example-retrieval-repo-em reported on 2026-08-04 (18 waivers, no reviewer, no
    verdict, no justification, no disposition).

    Negative-spec: this parameter does NOT gate the mint and must never grow
    into an authorization check. The mint's soundness argument (DR-245) rests
    on HALT-time VISIBILITY of the obligation, not on a discrimination between
    reviewed and unreviewed commits; adding a field that records the
    disposition is discharging that visibility claim on disk, not upgrading
    the artifact into the review certification DR-245 is explicit it is not.
    Whether a brightline PARTITION-MANDATORY verdict should SUPPRESS the mint
    outright is a separate, ceremony-gating question and is deliberately not
    decided here.

    Why a per-chain SUBDIRECTORY, not one scalar file per SHA (the
    pm-vouches/<sha>.json shape) or an appendable list inside one file per
    SHA — the design call this plan's C1 body requires be made explicitly:
    a SHA belonging to more than one chain's ancestry is the ROUTINE case,
    not an edge case — ancestry nodes are shared across chains by
    construction (a predecessor handoff can be a walked ancestor of more
    than one later closing chain). A single scalar frozen by
    O_CREAT|O_EXCL on first mint would silently deny a second, equally
    legitimate chain's waiver for the same SHA the moment the read side's
    chain-identity scope check (coverage.py's `_narrow_foreign_session_scope`
    via `_chain_ancestry_waived_shas`) is live — a fail-closed-looking bug
    an operator would be tempted to "fix" by deleting the very scope check
    that is the point of this artifact (AC3). Keying the directory on
    `chain_id` instead means two concurrent mints for DIFFERENT chains on
    the SAME sha write to two DIFFERENT paths (`<chain-a>/<sha>.json` and
    `<chain-b>/<sha>.json`) — there is no shared file for them to race on,
    so no read-modify-write lock is needed for that case. The remaining
    single-writer race (the SAME (sha, chain_id) pair minted twice, e.g. a
    re-run gate at the same HALT) is handled by the O_CREAT|O_EXCL open
    below: the second writer's FileExistsError is swallowed, making
    re-mint of an already-waived (sha, chain_id) pair a true no-op.

    Best-effort: an OSError creating the directory or a waiver file is
    logged and swallowed, never raised — mirrors
    review_trail_write._record_pm_vouch_waivers' posture: a waiver-
    persistence failure degrades to "the read side will not credit this
    commit for this chain," the safe (fail-closed on crediting) direction,
    never a silent authorization bypass.

    A `chain_id` failing the directory-name-safety shape check
    (`chain_waiver_dir` returning None) skips the WHOLE `shas` set for
    that call, with a warning — never partially resolved, never a path
    escape. That case still returns `frozenset(shas)` (every sha refused)
    rather than an empty set — a caller inspecting the return value must
    not read it as "everything minted cleanly."
    """
    chain_dir = chain_waiver_dir(cwd, chain_id)
    if chain_dir is None:
        logger.warning(
            "chain_ancestry_waivers: refusing to mint — chain_id %r fails "
            "the directory-name-safety shape check", chain_id,
        )
        return frozenset(shas)
    try:
        chain_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "chain_ancestry_waivers: could not create waiver dir %s (%s) — "
            "this mint proceeds, but the coverage read side will NOT credit "
            "the waived-for commit(s) for chain %r without a waiver file",
            chain_dir, exc, chain_id,
        )
        return frozenset(shas)
    written_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    refused: set[str] = set()
    ordered_shas = sorted(shas)
    # One git read for the whole mint, not one per sha: the ownership refusal
    # needs every sha's Session-Id trailer, and `git log --no-walk` takes them
    # all in a single invocation. Gate:
    # coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py.
    trailers_by_sha = _session_id_trailers_batch(cwd, ordered_shas)
    for sha in ordered_shas:
        refusal_reason = _refusal_from_trailer_lines(
            cwd, sha, chain_id, trailers_by_sha.get(sha)
        )
        if refusal_reason is not None:
            logger.warning("chain_ancestry_waivers: %s", refusal_reason)
            refused.add(sha)
            continue
        target = chain_dir / f"{sha}.json"
        if target.exists():
            continue
        waiver_record = {
            "schema_version": 2,
            "sha": sha,
            "chain_id": chain_id,
            "source_handoff": repo_relative_source_handoff(cwd, source_handoff),
            "waiver_written_at": written_at,
            "certifies_review": False,
            "basis": _MINT_BASIS,
            "em_disposition": em_disposition,
            "waived_commit_count": len(shas),
            "reader_note": _READER_NOTE,
        }
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(waiver_record, fh, indent=2)
                fh.write("\n")
        except FileExistsError:
            continue
        except OSError as exc:
            logger.warning(
                "chain_ancestry_waivers: could not write waiver %s (%s) — "
                "this mint proceeds, but the coverage read side will NOT "
                "credit this commit for chain %r without a waiver file",
                target, exc, chain_id,
            )
    return frozenset(refused)


def chain_reached_terminal_close(cwd: str, chain_id: str) -> bool:
    """True only when `chain_id` (the closing session's own id — the SAME
    identity that minted this chain's waiver subdirectory, per
    `record_chain_ancestry_waiver`'s `_MINT_BASIS` docstring comment) has
    itself reached a TERMINAL "closed" disposition, per the ratified DR-084
    vocabulary (`open` / `claimed` / `continued` / `closed`).

    `cwd` is the repo/worktree root, matching every other function in this
    module (`chain_root_dir`, `chain_waiver_dir`); it is resolved to the
    git COMMON dir (`git_common_dir`, e.g. `<cwd>/.git`) before being handed
    to the classification core, which expects that shape as its `common_dir`
    param (`main_worktree_root` derives the worktree back out via
    `common_dir.parent` — passing `cwd` itself in would double that
    derivation and scan the wrong tree). Any failure resolving it (not a
    git repo, `git` unavailable) fails closed — non-terminal, never terminal.

    Reuses, rather than re-derives, the classification core already built
    for `session.resolve_chain_terminal_disposition`
    (`coordinator_core.ops.session.resolve_chain_terminal_disposition.
    classify_chain_terminal_disposition`) — this repo's single-accessor
    convention for archived-handoff `deployment_state` reads (the same
    discipline that module's own docstring names for claimed_by/consumed_by
    reads). Calling with `chain_id` as the EXPLICIT `param_sid` argument
    bypasses that op's 5-way env-based session-id resolution entirely: no
    env var reads, no ambient-session confusion — the classification runs
    against `chain_id` itself, not whatever session happens to be calling.

    Semantics (easy to get wrong — read carefully):
      - `disposition == "closed"` (which the op only ever returns alongside
        `chain_terminal == True`) is the ONLY terminal case this returns
        True for.
      - `"continued"` is NOT terminal: the chain handed off to a successor
        session under a DIFFERENT chain_id. A later close under that
        successor's own chain_id does not license reaping waivers minted
        under THIS chain_id — those are two distinct waiver subdirectories.
      - `"open"` / `"claimed"` are non-terminal (the chain has not reached
        any terminal disposition yet).

    FAIL CLOSED, always: an exit_code:1 CC-7 structured-error result (an
    unknown/banned `deployment_state` token, or a malformed `closed_reason`)
    and a "no claimed handoff found" (`disposition == "open"`) result are
    BOTH treated as NON-terminal. A classification failure or ambiguity must
    never be read as "safe to reap" — this predicate is a published contract
    a later reaper builds on, and widening what counts as terminal fails
    OPEN in the direction that matters (a reaped waiver that should not have
    been removed silently strips coverage crediting a later close could
    otherwise have relied on).
    """
    try:
        common_dir = git_common_dir(Path(cwd))
    except RuntimeError:
        # Not inside a git repo (or `git` unresolvable) — cannot classify;
        # fail closed, same posture as every other error branch below.
        return False
    result = classify_chain_terminal_disposition(common_dir, chain_id, {})
    if result.get("exit_code") != 0:
        return False
    return result.get("disposition") == "closed"


def chain_ancestry_waived_shas(cwd: str, chain_id: str) -> FrozenSet[str]:
    """Return the set of commit SHAs carrying a chain-ancestry waiver minted
    FOR `chain_id` specifically — i.e. the file stems under
    `state/review-trail/chain-ancestry-waivers/<chain_id>/*.json`.

    Deliberately NOT a union across every chain's subdirectory: that would
    reproduce exactly the presence-only (any chain, anywhere) semantics
    DR-243's pm-vouches/ has, which this artifact must NOT — see
    coverage.py's `_narrow_foreign_session_scope` / `_chain_ancestry_waived_shas`
    for the read-side chain-identity match this narrow, single-chain
    lookup makes possible (AC3's named scope-mismatch test).

    An unreadable/absent chain directory or a shape-invalid `chain_id`
    returns an empty set and never raises — mirroring coverage.py's
    `_pm_vouched_waiver_shas` posture, because a missing waiver dir is the
    overwhelmingly common case (no chain has ever HALTed and minted here)
    and must never fail a coverage-gate run. Note the polarity: an empty
    result waives NOTHING, so the foreign SHAs stay stripped and stay
    uncredited. That is fail-CLOSED with respect to crediting — the safe
    direction, and the same direction `record_chain_ancestry_waiver`
    degrades in. Do not describe this degradation as fail-open: crediting
    and refusing have opposite polarities, and conflating them is the
    precise error that killed this plan's first mechanism (see the plan's
    § Why the first mechanism was rejected).
    """
    chain_dir = chain_waiver_dir(cwd, chain_id)
    if chain_dir is None:
        return frozenset()
    try:
        entries = list(chain_dir.iterdir())
    except OSError:
        return frozenset()
    return frozenset(p.stem for p in entries if p.suffix == ".json" and p.is_file())
