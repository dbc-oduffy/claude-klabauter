"""
coordinator_core.ops.check_description_length — assert each enabled SKILL.md
description fits its budget.

Purpose: scan `$HOME/.claude/plugins/**/SKILL.md` (excluding `cache/` and
`marketplaces/` subtrees) and check each skill's frontmatter `description`
field character count against a three-tier limit:
    - `description-budget: <N>` frontmatter field present -> use N
    - PM-gated skill (description starts with "PM-GATED" or "**PM-GATED")
      -> use 175
    - default -> 150

Wired into: /workweek-complete (advisory only — non-blocking; see
coordinator/docs/wiki/workday-workweek-cadence.md).

Port of: check-description-length.sh (DoE b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Exit codes (parity-critical):
    0 — all descriptions within budget
    1 — one or more descriptions exceed their budget

Output format: mirrors the bash oracle — a per-skill `<path>\\t<count>\\t<limit>\\t<pass|fail>`
line on stdout for passes, the same line shape on stderr for fails (plus a
final `ERROR: ...` line on stderr on any failure, or `all SKILL descriptions
within budget` on stdout when everything passes). Multi-line descriptions are
warned-and-skipped (WARN line to stderr), not counted.

Negative-spec (do NOT "fix" while porting):
    - The bash oracle's `sed` strip of leading/trailing `"` is a heuristic,
      not a real YAML parser — a description value containing an escaped
      `\\"` mid-string is NOT correctly unescaped (the oracle's own header
      comment documents this as a known, accepted limitation for the
      current skill corpus). This port reproduces that exact heuristic
      (strip one leading `"` and one trailing `"` only) rather than doing
      real YAML parsing, to preserve char-count parity with the pre-existing
      counts already baked into skills' `description-budget` fields.
    - Frontmatter is extracted as "everything between the first two `---`
      lines" (line-based, not a YAML frontmatter parser) — same as the
      bash oracle's awk extraction.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_PROG = "check-description-length"
_DEFAULT_LIMIT = 150
_PM_GATED_LIMIT = 175

_DESC_RE = re.compile(r'^description:\s*(.*)$')
_BUDGET_RE = re.compile(r'^description-budget:\s*(\d+)')


def _find_skill_md_files(plugins_root: Path) -> List[Path]:
    """Mirror `find "$PLUGINS_ROOT" -name "SKILL.md" -type f -not -path "*/cache/*" -not -path "*/marketplaces/*"`."""
    results: List[Path] = []
    if not plugins_root.is_dir():
        return results
    for dirpath, dirnames, filenames in os.walk(plugins_root):
        parts = Path(dirpath).parts
        if "cache" in parts or "marketplaces" in parts:
            dirnames[:] = []
            continue
        if "SKILL.md" in filenames:
            results.append(Path(dirpath) / "SKILL.md")
    return sorted(results)


def _extract_frontmatter(text: str) -> str:
    """Mirror `awk '/^---$/{n++; next} n==1'` — lines strictly between the
    first two `---`-only lines, exclusive of both delimiters."""
    lines = text.splitlines()
    n = 0
    out: List[str] = []
    for line in lines:
        if line == "---":
            n += 1
            continue
        if n == 1:
            out.append(line)
    return "\n".join(out)


def _strip_quotes(value: str) -> str:
    """Mirror `sed -E 's/^description:[[:space:]]*"?(.*)"?$/\\1/' | sed 's/"$//'`
    — strip at most one leading and one trailing double-quote, heuristically
    (not a real YAML unescape)."""
    v = value
    if v.startswith('"'):
        v = v[1:]
    if v.endswith('"'):
        v = v[:-1]
    if v.endswith('"'):
        v = v[:-1]
    return v


def _parse_frontmatter(frontmatter: str) -> Tuple[Optional[str], int, Optional[int]]:
    """Return (desc, desc_line_count, budget) from a frontmatter block."""
    desc_lines_raw: List[str] = []
    budget: Optional[int] = None
    for line in frontmatter.splitlines():
        m = _DESC_RE.match(line)
        if m:
            desc_lines_raw.append(m.group(1))
            continue
        m2 = _BUDGET_RE.match(line)
        if m2:
            budget = int(m2.group(1))
    desc = _strip_quotes(desc_lines_raw[0]) if desc_lines_raw else None
    return desc, len(desc_lines_raw), budget


def main(argv: List[str]) -> int:
    plugins_root = Path(os.environ.get("HOME", str(Path.home()))) / ".claude" / "plugins"

    fail = False
    for skill_md in _find_skill_md_files(plugins_root):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"{_PROG}: cannot read {skill_md}: {exc}", file=sys.stderr)
            continue

        frontmatter = _extract_frontmatter(text)
        desc, desc_line_count, budget = _parse_frontmatter(frontmatter)

        if desc_line_count > 1:
            print(
                f"WARN: {skill_md} has multi-line description; skipping "
                "(validator assumes single-line)",
                file=sys.stderr,
            )
            continue

        desc = desc or ""

        if budget is not None:
            limit = budget
        elif desc.startswith("PM-GATED") or desc.startswith("**PM-GATED"):
            limit = _PM_GATED_LIMIT
        else:
            limit = _DEFAULT_LIMIT

        count = len(desc)
        if count > limit:
            print(f"{skill_md}\t{count}\t{limit}\tfail", file=sys.stderr)
            fail = True
        else:
            print(f"{skill_md}\t{count}\t{limit}\tpass")

    if fail:
        print("ERROR: one or more SKILL descriptions exceed their budget", file=sys.stderr)
        return 1

    print("all SKILL descriptions within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
