"""
coordinator_core.tests.generate_session_attribution_golden

Provenance generator for `session_attribution_golden.json` — chunk A0 of
docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md.

Run ONCE, by a human/executor, against TODAY'S UNMODIFIED
`coordinator_core.session_attribution.trailer_foreign_shas` (P1) and
`coordinator_core.ops.review_trail_write._guard_foreign_session_range`,
against this repo's REAL history, PINNED at an explicit commit SHA
(`PINNED_HEAD_SHA` below — must be an ancestor of whatever HEAD the test
suite later runs at; git history is append-only forward so the pinned SHAs
stay reachable). The output is committed as
`coordinator_core/tests/session_attribution_golden.json` and
`test_session_attribution.py` reads it — it NEVER re-runs this generator.

Anti-scope 19 (plan doc): every fixture below is an EXPLICIT, PINNED SHA —
none is derived from a symbolic ref that could move under a later run.

Corpus, each entry a `(case_id, sha_range, own_session_id)` real-history
triple (see docstring on `CASES` for what each pins):
  - all-own-trailered range
  - a foreign-trailered commit (Case 1 of the guard)
  - an untrailered commit, `X~1..X` shape (NOT `X^..X` — plan Anti-scope 21;
    this is the real on-disk trail-record shape, taken from the five known
    false-positive records fork-adjudication.md § 11.1 replayed:
    session 4524bf7d-7d1e-4c5f-85ed-90f723b912a3, workstream
    handoff-write-cas, 2026-07-28)
  - a range spanning a merge commit
  - d21d8b023a29d4ce5e9d64a874f5646e1cdcb43b — the grep/atom divergent shape:
    its commit MESSAGE contains a `Session-Id:` line a `--grep` would match,
    but it is NOT the last block in the message, so git's own trailer
    parser (`%(trailers:...)`) does not recognise it as a trailer atom —
    `trailer_foreign_shas` (P1) therefore sees this commit as untrailered.

This script is committed for AUDITABLE PROVENANCE only. Re-running it would
regenerate against a DIFFERENT tree state and defeat the fixture's entire
purpose (a test that regenerates its own oracle proves only
self-consistency, not correctness) — see fork-adjudication.md § 10.4.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from coordinator_core import session_attribution  # noqa: E402
from coordinator_core.ops import review_trail_write  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

#: Must be reachable (an ancestor of, or equal to) HEAD whenever the test
#: suite later runs — history is append-only forward, so this stays true
#: unless someone rewrites history, which is out of scope for a test fixture.
PINNED_HEAD_SHA = "8e6ad1de8737f58a2d077c5ef84e9d2e222f4bc1"

#: A range spanning a merge commit. Fixed by explicit SHA (Anti-scope 19).
_MERGE_SHA = "f5952559b92e0283e73e056fecc5851555bf4bda"
_MERGE_FIRST_PARENT = "3cb008c4870a43767b212a40d3a43a988321ef18"

#: Two adjacent commits (no commit between them in `--no-merges` history)
#: both carrying a `Session-Id:` trailer naming the SAME session — the
#: all-own-trailered case.
_ADJACENT_NEWER = "c89c9d12f3b5dc93a698e435d733b926821c54f0"
_ADJACENT_OLDER = "13e56253b48e5d0d70070998615ddf192c1604cc"
_ADJACENT_SESSION = "9e2d60a7-aaaa-42cc-b688-bc117f35e25e"

#: A different real session — used to force the SAME two adjacent commits
#: to read as foreign-trailered (Case 1 of the guard) instead.
_OTHER_REAL_SESSION = "b7d4bdbf-b68a-403e-be93-0bba4a90a225"

#: The five known false-positive records replayed in fork-adjudication.md
#: § 11.1 — all `X~1..X` single-commit ranges over an UNTRAILERED commit,
#: all one session/workstream/day. § SC-DR-008: trailerlessness alone must
#: never become foreign_trailer_shas.
_SC_DR_008_SESSION = "4524bf7d-7d1e-4c5f-85ed-90f723b912a3"
_SC_DR_008_SHAS = [
    "833fa291d43f8c8880435830e2c590c3f38746c9",
    "10a9a3461d1f0310e6286f1f9955f3790cfc82c6",
    "b1fedc5756014d5d33e8e3697519213f54c00f84",
    "1761098fee0b87e9cfbda8beb23266821230acaf",
    "9ce3d77879e5b0b9199e70653f263e776f06257e",
]

#: The grep/atom divergent shape (Anti-scope 21's citation; see module
#: docstring above).
_GREP_ATOM_SHA = "d21d8b023a29d4ce5e9d64a874f5646e1cdcb43b"
_GREP_ATOM_SESSION = "22d7c42e-f7cc-47d5-8f8e-24c5599a1fd1"

#: (case_id, sha_range, own_session_id)
CASES: List[Tuple[str, str, str]] = [
    ("all_own_trailered", f"{_ADJACENT_OLDER}~1..{_ADJACENT_NEWER}", _ADJACENT_SESSION),
    ("foreign_trailered_case1", f"{_ADJACENT_OLDER}~1..{_ADJACENT_NEWER}", _OTHER_REAL_SESSION),
    ("merge_spanning", f"{_MERGE_FIRST_PARENT}..{_MERGE_SHA}", "00000000-0000-0000-0000-000000000000"),
    ("grep_atom_divergent", f"{_GREP_ATOM_SHA}~1..{_GREP_ATOM_SHA}", _GREP_ATOM_SESSION),
] + [
    (f"sc_dr_008_untrailered_{i}", f"{sha}~1..{sha}", _SC_DR_008_SESSION)
    for i, sha in enumerate(_SC_DR_008_SHAS)
]


def _run(args: List[str], cwd: Optional[str]) -> Tuple[int, str, str]:
    """The `GitRunner` contract `trailer_foreign_shas` requires from its
    caller (see session_attribution.GitRunner) — never raises."""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 2, "", str(exc)


def _trailer_result(sha_range: str, own_session_id: str) -> dict:
    try:
        result = session_attribution.trailer_foreign_shas(
            sha_range, own_session_id, str(REPO_ROOT), {}, _run,
        )
        return {"ok": True, "foreign_shas": sorted(result)}
    except session_attribution.GitLogFailed as exc:
        return {"ok": False, "error": "GitLogFailed", "message": str(exc)}


def _guard_result(sha_range: str, own_session_id: str) -> dict:
    try:
        waived = review_trail_write._guard_foreign_session_range(
            sha_range, own_session_id, REPO_ROOT,
        )
        return {"disposition": "proceed", "waived": sorted(waived)}
    except review_trail_write.ForeignSessionRangeRefused as exc:
        return {"disposition": "refused", "message": str(exc)}
    except ValueError as exc:
        return {"disposition": "error", "exception_type": "ValueError", "message": str(exc)}


def main() -> None:
    actual_head = _run(["git", "rev-parse", "HEAD"], str(REPO_ROOT))[1].strip()
    is_ancestor_rc = _run(
        ["git", "merge-base", "--is-ancestor", PINNED_HEAD_SHA, "HEAD"], str(REPO_ROOT),
    )[0]
    if is_ancestor_rc != 0:
        raise SystemExit(
            f"generate_session_attribution_golden: PINNED_HEAD_SHA={PINNED_HEAD_SHA!r} "
            f"is not an ancestor of current HEAD={actual_head!r} — the golden's basis "
            "commit is unreachable; refusing to regenerate against a diverged tree."
        )

    golden = {
        "pinned_head_sha": PINNED_HEAD_SHA,
        "generated_from": "unmodified session_attribution.trailer_foreign_shas + "
        "review_trail_write._guard_foreign_session_range",
        "cases": {},
    }
    for case_id, sha_range, own_session_id in CASES:
        golden["cases"][case_id] = {
            "sha_range": sha_range,
            "own_session_id": own_session_id,
            "trailer_foreign_shas": _trailer_result(sha_range, own_session_id),
            "guard": _guard_result(sha_range, own_session_id),
        }

    out_path = Path(__file__).resolve().parent / "session_attribution_golden.json"
    out_path.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
