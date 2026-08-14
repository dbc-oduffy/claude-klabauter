"""
coordinator_core.ops.verify_parallel_review_lens_orthogonality — parallel-code-review
gate structural-invariant checker (lens-domain orthogonality + chunk disjointness).

Purpose: assert the parallel-code-review gate's two structural properties:
  (1) STATIC (no args): the orthogonal lens domains in the skill's lens-domain
      manifest (3 specialist workers + code-semantics-as-a-class) (a) have agent
      files on disk and (b) do not share any lens_domain value.
  (2) RUNTIME (--chunk-manifest <tsv>): the N code-semantics chunk partitions
      are disjoint by file-scope — no file appears in two chunks. Runs the
      static check first, then the chunk-disjointness check.

Spec backlink: docs/plans/2026-05-06-parallel-code-review-weekly-gate.md Phase 3.5
Spec backlink: docs/plans/2026-05-23-weekly-gate-restructure-and-arch-survey-audit-rename.md § Strand 1c
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Why this exists: fail loudly at /update-docs Phase 11 time if a 5th orthogonal
lens is added with an overlapping lens domain, or if an agent file goes
missing; and fail loudly at pre-dispatch time if the chunk partitioning is not
disjoint by file-scope (which would break the synthesizer's convergence
logic). The manifest is hardcoded in
coordinator/skills/parallel-code-review/SKILL.md — agent files are NOT the
source of truth.

Wire-in note: the no-arg form is wired into /update-docs Phase 11 (verify-sync
phase); the --chunk-manifest form is called by
coordinator/skills/parallel-code-review/SKILL.md at pre-dispatch time.

Chunk-manifest format (TSV, one file per line): "chunk-<k>\\t<relpath>".

Exit codes (parity-critical — both callers branch on these):
  0 — all checks passed.
  1 — one or more checks failed (diagnostic printed to stdout), OR the skill
      file / chunk manifest was not found, OR the DoE-claude repo root could
      not be resolved (cross-repo lookup failure — see below), OR a CLI usage
      error (unknown arg, missing --chunk-manifest value, empty chunk
      manifest; these print to STDERR, matching the bash oracle's `>&2`
      usage-error convention — everything else prints to stdout).

Cross-repo note: this op lives in claude-klabauter but the manifest it checks
(coordinator/skills/parallel-code-review/SKILL.md) and the agent files it
verifies live in the DoE-claude repo — the bash oracle never needed this
resolution step because it ran FROM inside that repo (SCRIPT_DIR-relative).
This port resolves the DoE-claude root via
`coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()`. A
resolution failure is folded into the same "cannot verify without the
manifest" exit-1 stdout branch the oracle uses for a missing SKILL_FILE —
semantically the same "verification could not run" outcome for callers, so
no new exit code is introduced for it.

Negative-spec: does NOT modify any files, does NOT dispatch agents, does NOT
commit. Read-only assertion only. Does NOT require bash >= 4 (the bash
oracle's associative-array version guard, exit 2, has no Python analogue —
this port has no version-guard exit path).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root as _resolve_doe_root

_PROG = "verify-parallel-review-lens-orthogonality.sh"  # literal program-name prefix, matches bash oracle's $0
_MIN_REVIEWER_COUNT = 4

_AGENT_PATH_RE = re.compile(r"`agents/([^`]+)`")


def _extract_manifest_lines(skill_text: str) -> List[str]:
    """Mirror the bash awk extraction: lines between the '## Lens-Domain
    Manifest' heading and the next '##' heading or '---' rule, filtered to
    lines starting with '|'.
    """
    lines: List[str] = []
    in_section = False
    for raw in skill_text.splitlines():
        if raw.startswith("## Lens-Domain Manifest"):
            in_section = True
            continue
        if in_section and (raw.startswith("##") or raw.startswith("---")):
            in_section = False
            continue
        if in_section and raw.startswith("|"):
            lines.append(raw)
    return lines


def _parse_row(line: str) -> Optional[Tuple[str, str]]:
    """Mirror `IFS='|' read -r _ reviewer lens_domain _ rest` + trim. Returns
    (reviewer, lens_domain) or None if the row is a header/separator/empty row
    to be skipped (mirrors the bash oracle's skip-continue chain exactly).
    """
    parts = line.split("|")
    reviewer = parts[1].strip() if len(parts) > 1 else ""
    lens_domain = parts[2].strip() if len(parts) > 2 else ""

    if not reviewer:
        return None
    if reviewer.startswith("Reviewer"):
        return None
    if reviewer.startswith("Lens"):
        return None
    if reviewer.startswith("---"):
        return None
    if lens_domain.startswith("---"):
        return None
    if lens_domain.startswith("Lens domain"):
        return None
    if not lens_domain:
        return None
    return reviewer, lens_domain


def static_check(skill_file: Path, agents_dir: Path) -> Tuple[List[str], bool]:
    """Static lens-orthogonality check. Returns (diagnostic_lines, passed)."""
    if not skill_file.is_file():
        return (
            [
                "ERROR: Skill file not found at expected path:",
                f"  {skill_file}",
                "Cannot verify lens-orthogonality without the manifest.",
            ],
            False,
        )

    skill_text = skill_file.read_text(encoding="utf-8")
    manifest_lines = _extract_manifest_lines(skill_text)

    out: List[str] = []
    passed = True
    seen_domains: Dict[str, str] = {}
    reviewer_count = 0

    for raw_line in manifest_lines:
        row = _parse_row(raw_line)
        if row is None:
            continue
        reviewer, lens_domain = row
        reviewer_count += 1

        m = _AGENT_PATH_RE.search(reviewer)
        if m:
            agent_path = agents_dir / m.group(1)
            if not agent_path.is_file():
                out.append(f"FAIL: Agent file missing for reviewer '{reviewer}':")
                out.append(f"  Expected: {agent_path}")
                passed = False
        else:
            out.append(f"FAIL: Could not parse agent path from reviewer cell: '{reviewer}'")
            passed = False

        if lens_domain in seen_domains:
            out.append(f"FAIL: Lens-domain collision — '{lens_domain}' claimed by both:")
            out.append(f"  '{seen_domains[lens_domain]}' and '{reviewer}'")
            passed = False
        else:
            seen_domains[lens_domain] = reviewer

    if reviewer_count < _MIN_REVIEWER_COUNT:
        out.append(
            f"FAIL: Expected at least {_MIN_REVIEWER_COUNT} orthogonal-lens rows in the "
            f"manifest table, found {reviewer_count}."
        )
        out.append("  Check that the lens-domain manifest table in SKILL.md is intact.")
        passed = False

    if passed:
        out.append(
            f"OK (static): {reviewer_count} orthogonal lenses — agent files exist, "
            "no lens-domain collisions."
        )
    else:
        out.append("")
        out.append("Lens-orthogonality (static) check failed. Fix the issues above before proceeding.")
        out.append(f"Manifest source: {skill_file}")

    return out, passed


def chunk_check(manifest_path: Path) -> Tuple[List[str], bool]:
    """Chunk-disjointness check over a TSV chunk manifest. Returns
    (diagnostic_lines, passed).
    """
    if not manifest_path.is_file():
        return [f"FAIL: chunk manifest not found at: {manifest_path}"], False

    text = manifest_path.read_text(encoding="utf-8")

    out: List[str] = []
    passed = True
    file_owner: Dict[str, str] = {}
    chunks_seen: set = set()
    chunk_file_count = 0

    for raw_line in text.splitlines():
        fields = raw_line.split("\t")
        chunk_id = fields[0].strip() if len(fields) > 0 else ""
        relpath = fields[1].strip() if len(fields) > 1 else ""

        if not chunk_id:
            continue
        if not relpath:
            continue
        if chunk_id.startswith("#"):
            continue

        chunks_seen.add(chunk_id)
        chunk_file_count += 1

        existing = file_owner.get(relpath)
        if existing is not None:
            if existing != chunk_id:
                out.append(f"FAIL: chunk partitions not disjoint — '{relpath}' claimed by both:")
                out.append(f"  '{existing}' and '{chunk_id}'")
                passed = False
            # Same file listed twice under the same chunk is harmless; ignore.
        else:
            file_owner[relpath] = chunk_id

    if chunk_file_count == 0:
        return (
            [
                f"FAIL: chunk manifest '{manifest_path}' contained no chunk/file rows.",
                "  Expected lines of the form 'chunk-<k><TAB><relpath>'.",
            ],
            False,
        )

    if passed:
        out.append(
            f"OK (chunks): {len(chunks_seen)} chunks over {len(file_owner)} distinct files — "
            "partitions disjoint by file-scope."
        )
    else:
        out.append("")
        out.append("Chunk-disjointness check failed. Re-chunk so no file appears in two chunks.")
        out.append(f"Chunk manifest: {manifest_path}")

    return out, passed


def run(
    argv: List[str], doe_root: Optional[str] = None
) -> Tuple[List[str], List[str], int]:
    """Core driver: parse args, resolve the DoE-claude repo root, run static
    check then (if requested) chunk check.

    Returns (stdout_lines, stderr_lines, rc). CLI usage errors (unknown arg,
    missing --chunk-manifest value) go to stderr, matching the bash oracle's
    `>&2` convention on those two branches; every other diagnostic goes to
    stdout, matching the oracle's plain `echo`.

    doe_root: injection seam for tests — when provided, skips the
    coordinator_doe_root() resolution call.
    """
    chunk_manifest: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--chunk-manifest":
            if i + 1 >= len(argv) or not argv[i + 1]:
                return [], ["ERROR: --chunk-manifest requires a path argument."], 1
            chunk_manifest = argv[i + 1]
            i += 2
        else:
            return (
                [],
                [
                    f"ERROR: unknown argument '{arg}' "
                    f"(usage: {_PROG} [--chunk-manifest <tsv-path>])"
                ],
                1,
            )

    repo_root_str = doe_root if doe_root is not None else _resolve_doe_root()
    if not repo_root_str:
        return (
            [
                "ERROR: could not resolve the DoE-claude repo root (needed to locate "
                "coordinator/skills/parallel-code-review/SKILL.md).",
                "Cannot verify lens-orthogonality without the manifest.",
            ],
            [],
            1,
        )

    repo_root = Path(repo_root_str)
    skill_file = repo_root / "coordinator" / "skills" / "parallel-code-review" / "SKILL.md"
    agents_dir = repo_root / "coordinator" / "agents"

    lines, passed = static_check(skill_file, agents_dir)
    if not passed:
        return lines, [], 1

    if chunk_manifest is None:
        return lines, [], 0

    chunk_lines, chunk_passed = chunk_check(Path(chunk_manifest))
    all_lines = lines + chunk_lines
    return all_lines, [], (0 if chunk_passed else 1)


def main(argv: List[str]) -> int:
    """CLI entry: run the check driver, print stdout/stderr lines, return rc."""
    stdout_lines, stderr_lines, rc = run(argv)
    for line in stdout_lines:
        print(line)
    for line in stderr_lines:
        print(line, file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
