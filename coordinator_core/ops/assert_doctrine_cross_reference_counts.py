"""
coordinator_core.ops.assert_doctrine_cross_reference_counts — "doctrine.assert_cross_reference_counts" op.

Purpose: native replacement for the `grep -c` doctrine cross-reference assertions
embedded as a markdown fence in the caller's own doctrine tree (the oracle's
verbatim example: `skills/plan/SKILL.md`'s Branch B "eighth dimension"
trampoline ⇄ spike back-edge parity check — a fence asserting that specific
literal tokens appear at least N times in specific skill/wiki files, to catch a
doctrine cross-reference silently regressing when one side of an edge is
edited without the other). This op generalizes that one fence into a reusable,
registered, tested count-assertion primitive: given a caller-supplied map of
`"<repo-relative-path>::<pattern>"` -> expected minimum line-count, it reads
each named file under the caller's OWN doctrine tree and reports every entry
whose actual grep-`-c`-equivalent count falls short.

Wire contract (per state/audits/2026-07-22-command-payload-inventory/
op-classification.tsv row "assert-doctrine-cross-reference-counts"):
    params:   {repo_root: str (optional D3 consistency check),
               expected: dict[str, int] (required — keys are
                         "<repo-relative-path>::<pattern>", values are the
                         minimum expected occurrence count)}
    response: {ok: bool, mismatches: list[{file: str, expected: int, actual: int}]}

Threshold semantics (design decision — the oracle fence's own asserts are all
`grep -c ... # expect >= N`, never `== N`): a row is a MISMATCH iff
`actual < expected`. More occurrences than expected is never flagged — the
fence exists to catch a cross-reference silently disappearing, not to pin an
exact count that would make every additional legitimate reference a build
break. `mismatches` therefore lists only failing rows; an empty list means
`ok: True`.

Count semantics (grep -c parity, not raw substring count): a match is a LINE
containing the (literal, non-regex) pattern at least once — mirroring `grep -c`,
which counts matching LINES, not total substring occurrences. A line with the
pattern twice still counts once. This matters for the oracle's own back-edge
check, whose self-referential `Test Surface` block relies on line-counting to
tell a real citation apart from the assert lines that mention the token.

Scope: registered "common_dir" (per the manifest's scope-verdict column) — this
op reads the CALLER's own skills/**/*.md + docs/wiki/**/*.md doctrine tree (a
self-referential doctrine check), so the worktree root MUST be derived from the
engine-supplied common_dir (main_worktree_root), never from params.repo_root —
a cross-repo caller would otherwise silently read claude-klabauter's own tree instead of
the caller's, exactly the failure mode the manifest's scope-verdict column
names.

Spec backlinks:
  - Plan (Wave 2): docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Mandated resolvers
  - Oracle: state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv row "assert-doctrine-cross-reference-counts"
  - Classification: state/audits/2026-07-22-command-payload-inventory/op-classification.tsv row "assert-doctrine-cross-reference-counts"
  - Fence example (read-only reference, not ported verbatim):
    coordinator/skills/plan/SKILL.md § Test Surface (example-doctrine-repo, AC3 grep-assert)

Idempotency (AC7): read-only count assertion, no state mutation — a repeat
invocation with identical params re-reads the same files and returns the same
result by construction (manifest idempotency-hazard: none). No DEC-7 note
required (idempotency- and platform-hazard both rate below medium).

Negative-spec:
  - Does NOT use `subprocess`/`grep`/`bash` — pure Python string/line counting,
    replacing the oracle's `grep -c` shellout entirely (manifest platform-hazard
    note: "closed by replacing grep -c with native Python").
  - Does NOT treat `pattern` as a regex — literal substring containment per
    line only, matching plain (non `-E`/`-P`) `grep -c` semantics used by the
    oracle fence.
  - Does NOT write to disk anywhere — assertion-only, no archive/commit path.
  - Does NOT use params.repo_root as the worktree source — worktree is derived
    via main_worktree_root(repo_root) (the engine-injected common_dir handler
    arg); params.repo_root is the D3 consistency check only.
  - Does NOT allow an arbitrary caller-supplied path outside the doctrine tree
    — each resolved file must be contained under the worktree's `skills/` or
    `docs/wiki/` directory (the two doctrine roots named in the manifest's
    scope-verdict column); an escaping path is a usage error, not a silent
    skip.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

#: Delimiter between the repo-relative path and the literal pattern in each
#: `expected` dict key. Split on the FIRST occurrence only, so a pattern
#: containing "::" itself (unlikely, but not forbidden) still round-trips.
_KEY_DELIM = "::"


def _count_matching_lines(text: str, pattern: str) -> int:
    """Return the number of lines in ``text`` containing ``pattern`` literally.

    Mirrors ``grep -c PATTERN FILE`` — a line matches at most once regardless
    of how many times the pattern occurs within it. ``text.splitlines()``
    handles all of \\n, \\r\\n, and \\r line endings uniformly (Windows-safe).
    """
    return sum(1 for line in text.splitlines() if pattern in line)


def _reply(*, ok: bool, mismatches: List[Dict[str, object]], error: Optional[str] = None) -> dict:
    """Build the {ok, mismatches, error} response envelope.

    ``error`` is only present (non-None) on a usage/setup error, distinct from
    a normal ``ok: False`` count-mismatch result — callers that only branch on
    ``ok`` still see every mismatch; callers that need to tell "assertions
    failed" apart from "the request itself was malformed" can check ``error``.
    """
    result: Dict[str, object] = {"ok": ok, "mismatches": mismatches}
    if error is not None:
        result["error"] = error
    return result


@register_op("doctrine.assert_cross_reference_counts")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """doctrine.assert_cross_reference_counts — assert doctrine cross-reference counts.

    For each ``"<path>::<pattern>"`` key in ``params["expected"]``, reads
    ``<path>`` (repo-relative, resolved against the caller's own worktree) and
    counts lines containing ``<pattern>`` literally. Any key whose actual count
    is less than the expected value is reported in ``mismatches``; ``ok`` is
    True iff ``mismatches`` is empty.

    repo_root arg: the git common dir delivered by _OP_KEY_SCOPE="common_dir".
                   Handlers MUST NOT use ctx.repo_root (None in the global
                   service). Worktree root is derived via
                   main_worktree_root(repo_root). params["repo_root"] is the
                   D3 consistency check only — NOT the worktree-root
                   resolution source.
    """
    expected = params.get("expected")
    param_repo_root = params.get("repo_root")

    if not isinstance(expected, dict) or not expected:
        return _reply(
            ok=False, mismatches=[],
            error="'expected' must be a non-empty dict[str, int]",
        )

    if repo_root is None:
        _LOG.error(
            "doctrine.assert_cross_reference_counts: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production"
        )
        return _reply(
            ok=False, mismatches=[],
            error="repo_root is None; cannot derive worktree root (keying-table misconfiguration)",
        )

    common_dir = Path(repo_root)
    mismatch_reason = check_repo_root(param_repo_root, common_dir)
    if mismatch_reason:
        return _reply(ok=False, mismatches=[], error=mismatch_reason)

    worktree = main_worktree_root(common_dir)
    allowed_roots = [worktree / "skills", worktree / "docs" / "wiki"]

    mismatches: List[Dict[str, object]] = []

    for key, expected_count in expected.items():
        if not isinstance(key, str) or _KEY_DELIM not in key:
            return _reply(
                ok=False, mismatches=[],
                error=f"'expected' key {key!r} must be '<path>{_KEY_DELIM}<pattern>'",
            )
        if not isinstance(expected_count, int):
            return _reply(
                ok=False, mismatches=[],
                error=f"'expected' value for {key!r} must be int, got {type(expected_count).__name__!r}",
            )

        rel_path, pattern = key.split(_KEY_DELIM, 1)
        candidate = Path(rel_path)
        if not candidate.is_absolute():
            candidate = worktree / candidate

        contained = contained_path(candidate, allowed_roots)
        if contained is None:
            return _reply(
                ok=False, mismatches=[],
                error=f"path escapes skills/ and docs/wiki/: {rel_path!r}",
            )

        try:
            text = contained.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _reply(
                ok=False, mismatches=[],
                error=f"could not read {rel_path!r}: {exc}",
            )

        actual_count = _count_matching_lines(text, pattern)
        if actual_count < expected_count:
            mismatches.append({"file": key, "expected": expected_count, "actual": actual_count})

    return _reply(ok=not mismatches, mismatches=mismatches)
