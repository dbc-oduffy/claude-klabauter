"""
coordinator_core.ops.rollup_derive — derive an artifact's roll-up ship-state
from its resolving commits, on demand, with no cached/stored state.

Purpose: answer "which commits resolve <artifact-id>, and are they shipped?"
by (1) finding every commit whose message carries a `Resolves: <artifact-id>`
trailer (the format pinned by C4 — coordinator/docs/wiki/resolves-commit-trailer.md),
and (2) delegating the "shipped?" question to
coordinator_core.ops.emit.envelope.main (the check-shipped-on-main.sh port).
This module RE-DERIVES on every call; it never reads or writes a stored
roll-up state (canonical-artifact-shapes.md: liveness/roll-up-state is
derived, not stored).

Contract (four tokens, one emitted per call, plus the resolving SHA list):
  shipped               — envelope.main exit 0: all resolving commits are
                           ancestors of origin/main.
  not-shipped            — envelope.main exit 1: >=1 resolving commit is not
                           on origin/main.
  unknown-error          — envelope.main exit 2 (or any other unexpected
                           code): not a git repo, or origin/main unreachable.
                           PROPAGATED as its own token — never collapsed into
                           not-shipped, since "could not determine" !=
                           "confirmed not shipped".
  no-resolving-commits   — zero commits found with a `Resolves: <artifact-id>`
                           trailer. This is a VACUOUS PASS, not an error — the
                           normal pre-adoption state.

Malformed-trailer diagnostic (stderr only; stdout contract unmoved). Three
populations reach `no-resolving-commits`, and only one of them is a defect the
caller can act on: a commit that spells `Resolves: <artifact-id>` somewhere
other than the final trailer block, which git therefore never parses as a
trailer. `_candidate_shas` matches it on the raw message, the parse step drops
it, and stdout is indistinguishable from a true zero — a reader with a
`git log --grep` hit in hand concludes the primitive is broken. So when every
candidate was dropped because NO `Resolves:` trailer parsed at all, the count
is named on stderr. Deliberately NOT reported: a candidate whose trailer parsed
to a different artifact-id (the prefix-sharing narrowing in the negative-spec
below) — that drop is correct, and reporting it would train the reader to
ignore the line. The token, the exit code, and the stdout shape are unchanged;
this is diagnostic, never a verdict. Raised by coordinator-claude-em
(cross-repo/archive/2026-08-13-coordinator-claude-em-rollup-derive-malformed-trailer-diagnostic.md).

This op's own contract is already machine-distinguishable: `no-resolving-
commits` is a distinct token, never collapsed into `not-shipped`. The
consumer that historically discarded that distinction downstream is
coordinator_core.ops.promote_shipped_in_flight_stubs (its `main()` used to
`exit 0` unconditionally regardless of which token fired) — see that
module's own docstring for the AC14 fix and the two structural, NOT-fixed-
here reasons this op legitimately returns `no-resolving-commits` for active,
unmerged branch work: `commit_anchors` deliberately does not emit the
`Resolves:` trailer except at a completion event (precision over recall),
and the origin/main ancestry check below only evaluates commits that DO
carry the trailer. Both are Q3's territory (PM ruling R2, forward-only —
this repo does not sweep for or repair historical trailer adoption); this
module does not attempt to compensate for either.

Port of: rollup-derive.sh (coordinator-claude b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-07-08-lifecycle-vocab-c2-durable-links-rollup.md § C5
Port backlink: coordinator-claude tasks/2026-07-16-clean-slate-recon R1 DOE-PORT executor item.

Negative-spec (do NOT "fix" mid-port):
    - Candidate-commit filtering via `git log --grep --fixed-strings` is only a
      CANDIDATE filter (unanchored substring match against the full commit
      message) — a prefix-sharing artifact-id (e.g. querying `hnd-abc` against
      a commit carrying `Resolves: hnd-abc-def456`) would false-positive-match
      under substring comparison alone. Each candidate is re-checked via the
      exact-value primitive and kept only on a true `==` match against
      artifact_id — never on substring containment. This mirrors bash oracle
      review finding (code-reviewer F1/P1). C19: the exact-value check now
      runs primarily against ONE batched `git log --no-walk --ignore-missing`
      call (`_batch_primary_trailers`) instead of spawning
      `parse_resolves_trailer.run` per candidate; a candidate whose primary
      trailer is absent/empty still falls back to that per-commit primitive
      one at a time, preserving its case-insensitive fallback rung unchanged.
    - Does NOT store, cache, or stamp a roll-up result anywhere.
    - Does NOT collapse "unknown" into "not-shipped".
    - Read-only. Never mutates the repo.
"""

from __future__ import annotations

import io
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from contextlib import redirect_stdout
from typing import List

from coordinator_core.ops.parse_resolves_trailer import run as _parse_resolves_run

_PROG = "rollup-derive"  # literal program-name prefix — matches oracle stderr

_USAGE = """Usage: rollup-derive.sh <artifact-id>

Derives the roll-up ship-state of <artifact-id> by finding all commits whose
message carries a `Resolves: <artifact-id>` trailer, then checking whether
those commits are all on origin/main.

Re-derives every call. Never reads or writes a stored roll-up state.

Output (stdout):
  <token>
  <resolving-sha> [<resolving-sha> ...]   (omitted if no-resolving-commits)

Tokens:
  shipped               all resolving commits are on origin/main
  not-shipped           >=1 resolving commit is not on origin/main
  unknown-error         repo/ref error (not a git repo, or origin/main unreachable)
  no-resolving-commits  no commits found with a Resolves: <artifact-id> trailer"""


def _run_git(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _is_inside_git_repo() -> bool:
    r = _run_git(["rev-parse", "--is-inside-work-tree"])
    return r.returncode == 0


def _candidate_shas(artifact_id: str) -> List[str]:
    """Substring-match candidates via `git log --grep` (over-inclusive by design; see module docstring)."""
    r = _run_git(
        ["log", "--all", "--format=%H", f"--grep=Resolves: {artifact_id}", "--fixed-strings"]
    )
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.split("\n") if line.strip() != ""]


def _batch_primary_trailers(shas: List[str]) -> dict:
    """Resolve the PRIMARY `Resolves:` trailer values for a batch of candidate
    commit SHAs in ONE ``git log`` call, replacing the per-candidate
    ``parse_resolves_trailer.run`` spawn pair.

    This is an OBJECT question (trailer content at a caller-supplied SHA),
    not a RANGE question — ``git log --no-walk`` resolves each argv SHA
    independently and never merges them into one ancestry/reachability set
    expression, so it batches unconditionally (Anti-scope 1/2/4; the forbidden
    shape is ``git rev-list A..B C..D``, not this). Mirrors
    ``emit/sections/handoffs._resolve_shipped_in_dates``'s
    ``--no-walk=unsorted --ignore-missing`` shape and C13's
    ``_batch_commit_timestamps`` precedent (Anti-scope 25's reconciliation
    reference), not re-derived here.

    ``--ignore-missing`` makes an unresolvable SHA silently ABSENT from
    stdout (exit 0) rather than an error. Each record is delimited by a
    literal ``\\x1e`` (record separator) so a multi-value ``Resolves:``
    trailer's own embedded newlines never get mistaken for a record
    boundary; within a record, ``\\x1f`` (unit separator) splits the full
    40-char SHA from its trailer-value lines. A requested SHA absent from
    the returned dict (never resolved, or resolved with zero trailer lines)
    is the caller's signal to fall back to the per-commit primitive — the
    same "absent means try the fallback" contract the original per-candidate
    ``parse_resolves_trailer.run`` had for an empty primary result.
    """
    if not shas:
        return {}
    ordered = sorted(set(shas))
    record_sep = "\x1e"
    unit_sep = "\x1f"
    r = _run_git(
        [
            "log",
            "--no-walk=unsorted",
            "--ignore-missing",
            f"--format=%H{unit_sep}%(trailers:key=Resolves,valueonly){record_sep}",
            *ordered,
        ]
    )
    if r.returncode != 0 or not r.stdout:
        return {}

    result: dict = {}
    for record in r.stdout.split(record_sep):
        record = record.strip("\n")
        if not record:
            continue
        head, _, rest = record.partition(unit_sep)
        full_sha = head.strip()
        if not full_sha:
            continue
        values = [line for line in rest.split("\n") if line.strip() != ""]
        result[full_sha] = values
    return result


def _derive_resolving(artifact_id: str) -> tuple[List[str], List[str]]:
    """Return (resolving_shas, untrailered_shas).

    The second element is the malformed-trailer population: candidates whose
    raw message matched but for which NEITHER parse rung yielded any
    `Resolves:` trailer at all. It is the only drop reason a caller can act on
    — a candidate dropped because its trailer named a different artifact-id is
    the correct prefix-sharing narrowing and is deliberately absent here (see
    the module docstring's malformed-trailer diagnostic paragraph).
    """
    candidates = _candidate_shas(artifact_id)
    if not candidates:
        return [], []

    primary_map = _batch_primary_trailers(candidates)
    resolved: dict = {}
    untrailered: List[str] = []
    fallback_needed: List[str] = []
    for candidate_sha in candidates:
        values = primary_map.get(candidate_sha)
        if values:
            resolved[candidate_sha] = artifact_id in values
        else:
            # Absent or empty primary result -- mirrors the original
            # per-candidate parse_resolves_trailer.run's own primary-then-
            # fallback rung (Negative-spec: the case-insensitive fallback
            # asymmetry is inherited, not fixed here).
            fallback_needed.append(candidate_sha)

    for candidate_sha in fallback_needed:
        candidate_ids, _rc = _parse_resolves_run(candidate_sha)
        resolved[candidate_sha] = artifact_id in candidate_ids
        if not candidate_ids:
            untrailered.append(candidate_sha)

    return [sha for sha in candidates if resolved.get(sha)], untrailered


def _resolving_shas(artifact_id: str) -> List[str]:
    return _derive_resolving(artifact_id)[0]


def _shipped_rc(resolving_shas: List[str]) -> int:
    """Call envelope.main(shas) with stdout suppressed; return its exit code only.

    Mirrors the bash oracle's `"$CHECK_SHIPPED" "${RESOLVING_SHAS[@]}" >/dev/null 2>&1`
    — only the exit code is consumed, never the ported script's own stdout/stderr.
    """
    from coordinator_core.ops.emit.envelope import main as _shipped_main

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            return _shipped_main(list(resolving_shas))
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1


def main(argv: List[str]) -> int:
    """CLI entry: arg validation, derivation, token emission, return rc."""
    if not argv or argv[0] in ("--help", "-h"):
        print(_USAGE)
        return 0 if argv else 1

    artifact_id = argv[0]
    if not artifact_id:
        print(f"{_PROG}: artifact-id must not be empty", file=sys.stderr)
        return 1

    if not _is_inside_git_repo():
        print("unknown-error")
        print(f"{_PROG}: not inside a git repository", file=sys.stderr)
        return 0

    resolving_shas, untrailered_shas = _derive_resolving(artifact_id)

    if not resolving_shas:
        print("no-resolving-commits")
        if untrailered_shas:
            print(
                f"{_PROG}: {len(untrailered_shas)} commit(s) name "
                f"'Resolves: {artifact_id}' in the message but carry no parsed "
                f"trailer, so they do not resolve it: {' '.join(untrailered_shas)}",
                file=sys.stderr,
            )
            print(
                f"{_PROG}: git reads trailers only from the final block; check one with "
                "git log -1 --format=%(trailers:key=Resolves,valueonly) <sha>",
                file=sys.stderr,
            )
        return 0

    shipped_rc = _shipped_rc(resolving_shas)
    if shipped_rc == 0:
        print("shipped")
    elif shipped_rc == 1:
        print("not-shipped")
    elif shipped_rc == 2:
        print("unknown-error")
    else:
        print("unknown-error")
        print(
            f"{_PROG}: check-shipped-on-main exited unexpected code {shipped_rc}",
            file=sys.stderr,
        )

    for sha in resolving_shas:
        print(sha)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
