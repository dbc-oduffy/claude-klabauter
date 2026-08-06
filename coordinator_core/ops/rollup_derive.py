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

Port of: rollup-derive.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-07-08-lifecycle-vocab-c2-durable-links-rollup.md § C5
Port backlink: example-doctrine-repo tasks/2026-07-16-clean-slate-recon R1 DOE-PORT executor item.

Negative-spec (do NOT "fix" mid-port):
    - Candidate-commit filtering via `git log --grep --fixed-strings` is only a
      CANDIDATE filter (unanchored substring match against the full commit
      message) — a prefix-sharing artifact-id (e.g. querying `hnd-abc` against
      a commit carrying `Resolves: hnd-abc-def456`) would false-positive-match
      under substring comparison alone. Each candidate is re-checked via the
      exact-value primitive (parse_resolves_trailer.run, C4) and kept only on
      a true `==` match against artifact_id — never on substring containment.
      This mirrors bash oracle review finding (code-reviewer F1/P1).
    - Does NOT store, cache, or stamp a roll-up result anywhere.
    - Does NOT collapse "unknown" into "not-shipped".
    - Read-only. Never mutates the repo.
"""

from __future__ import annotations

import io
import subprocess
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
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


def _resolving_shas(artifact_id: str) -> List[str]:
    resolving: List[str] = []
    for candidate_sha in _candidate_shas(artifact_id):
        candidate_ids, _rc = _parse_resolves_run(candidate_sha)
        if artifact_id in candidate_ids:
            resolving.append(candidate_sha)
    return resolving


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

    resolving_shas = _resolving_shas(artifact_id)

    if not resolving_shas:
        print("no-resolving-commits")
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
