"""
coordinator_core.ops.check_atlas_watch_drift — atlas per-system `.watch.py`
aggregator plus default-on STALE (`last_attested`) currency walk.

Purpose: single aggregation point over the atlas-watch-script-convention.
For every `docs/architecture/systems/<name>.md` page, run the sibling
`<name>.watch.py` (if present) and normalize its outcome into one contract
line; separately (default-on, suppressible), flag pages whose
`last_attested:` frontmatter date has aged past a threshold.

Spec backlink: docs/plans/2026-06-04-architecture-audit-atlas-refresh-gate.md § C2
Spec backlink: docs/plans/2026-06-08-atlas-attested-clock-split.md (last_attested
    as currency signal)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Behavior summary (always returns rc 0 — informational):
  1. Enumerate atlas pages at `docs/architecture/systems/*.md`.
  2. For each system <name>:
       - If `docs/architecture/systems/<name>.watch.py` exists:
           * Run `<sys.executable> <path>` with cwd = repo root.
           * Non-zero exit -> emit `ERROR <name>: <name>.watch.py exit=<N>`.
           * Malformed stdout (not exactly one non-empty line matching
             `^(FRESH|DRIFT|MISSING) <name>( .*)?$`) -> emit
             `MALFORMED <name>: <name>.watch.py output unparseable`.
           * Otherwise emit the line verbatim.
       - Else (no `.watch.py`): emit `FRESH <name> (no watch script)`.
  3. STALE walk (default-on, suppressible via `--no-stale-walk`):
     For each atlas page, parse frontmatter `last_attested:` date. If age in
     days exceeds `--stale-days N` (default 30), emit
     `STALE <name> last_attested=<date> age=<N>d`.

CLI: --stale-days N              (default 30; alias --last-attested-stale-days;
                                   accepts legacy --last-mapped-stale-days)
     --no-stale-walk             (suppress STALE walk entirely)

Negative-spec: does NOT modify any atlas page, does NOT trigger any audit,
does NOT read or write `Last full audit`. Pure read. Bad/missing args and
"not a git repo" / "no atlas dir" both degrade to an `ERROR ...` line (for
bad args) or silent empty output (for missing repo/dir context) — the
overall process ALWAYS returns rc 0, matching the bash oracle's
always-exit-0 informational contract (reviewers must not "fix" this into a
fail-loud exit code — it's a deliberate ported behavior, not a bug).
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ops._git_root_util import git_root as _resolve_git_root

_PROG = "check-atlas-watch-drift"
_DEFAULT_STALE_DAYS = 30
_SUBPROCESS_TIMEOUT_SECS = 15


def _parse_args(argv: List[str]) -> Tuple[Optional[int], bool, Optional[str]]:
    """Parse CLI args.

    Returns (stale_days, run_stale_walk, error_line). error_line is a
    fully-formed `ERROR check-atlas-watch-drift: ...` string on bad args
    (caller should print it to stdout and return rc 0, matching the bash
    oracle — these are informational, not fail-loud), else None.
    """
    stale_days = _DEFAULT_STALE_DAYS
    run_stale_walk = True
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in (
            "--stale-days",
            "--last-attested-stale-days",
            "--last-mapped-stale-days",
        ):
            if i + 1 >= len(argv):
                return None, True, f"ERROR {_PROG}: {arg} requires a value"
            try:
                stale_days = int(argv[i + 1])
            except ValueError:
                print(f"skip: _parse_args: stale_days = int(argv[i + 1]) failed: {sys.exc_info()[1]}", file=sys.stderr)
                return None, True, f"ERROR {_PROG}: {arg} requires a value"
            i += 2
        elif arg == "--no-stale-walk":
            run_stale_walk = False
            i += 1
        else:
            return None, True, f"ERROR {_PROG}: unknown arg '{arg}'"
    return stale_days, run_stale_walk, None


def _watch_line(repo_root: Path, systems_dir: Path, base: str) -> str:
    """Run <base>.watch.py (if present) and return its normalized contract line."""
    watch_script = systems_dir / f"{base}.watch.py"
    if not watch_script.is_file():
        return f"FRESH {base} (no watch script)"

    try:
        result = subprocess.run(
            [sys.executable, str(watch_script)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        # Review: code-reviewer — module docstring's "never block a caller
        # pipeline" contract requires a bound on this per-atlas-page shell-out;
        # an unbounded hang in one .watch.py would otherwise wedge the whole
        # /workday-start ceremony (Finding 1).
        return f"ERROR {base}: {base}.watch.py timeout"
    except OSError:
        return f"ERROR {base}: {base}.watch.py exit=127"

    if result.returncode != 0:
        return f"ERROR {base}: {base}.watch.py exit={result.returncode}"

    # Match the bash oracle exactly, including its footgun: the non-empty-line
    # COUNT uses an `awk NF` filter (blank/whitespace-only lines excluded), but
    # the line actually PARSED is `head -n1` of the RAW (unfiltered) output —
    # so a leading blank line followed by exactly one real content line still
    # counts as line_count==1 yet parses the blank first line and emits
    # MALFORMED. Reproduced verbatim, not "fixed" (see module docstring).
    raw_lines = result.stdout.splitlines()
    non_blank_count = sum(1 for ln in raw_lines if ln.strip())
    if non_blank_count != 1:
        return f"MALFORMED {base}: {base}.watch.py output unparseable"

    first_line = raw_lines[0] if raw_lines else ""
    # Bash: first_tok="${first_line%% *}"; rest="${first_line#"$first_tok"}";
    # rest="${rest# }" (strips exactly ONE leading space, not all);
    # second_tok="${rest%% *}". A double-space separator therefore yields an
    # EMPTY second_tok in the oracle (not the second word) — reproduced here.
    first_tok = first_line.split(" ", 1)[0]
    rest = first_line[len(first_tok):]
    if rest.startswith(" "):
        rest = rest[1:]
    second_tok = rest.split(" ", 1)[0] if rest else ""

    if first_tok in ("FRESH", "DRIFT", "MISSING") and second_tok == base:
        return first_line
    return f"MALFORMED {base}: {base}.watch.py output unparseable"


def _stale_line(atlas_path: Path, base: str, stale_days: int, today: date) -> Optional[str]:
    """Return a `STALE ...` line if the atlas page's last_attested is aged past
    the threshold, else None (missing/unparseable date is a silent skip,
    mirroring the bash oracle).

    Mirrors the bash `grep -m1 '^last_attested:' "$atlas"` + sed-YYYY-MM-DD
    extraction EXACTLY — including scanning the WHOLE FILE (not frontmatter-
    fence-scoped), unlike `_fm_util.extract_frontmatter_scalar`. Deliberately
    NOT reused here: a file-wide first-match is the oracle's actual behavior,
    and atlas pages only ever carry `last_attested:` in frontmatter by
    convention, so this is a faithful-not-improved port.
    """
    try:
        text = atlas_path.read_text(encoding="utf-8")
    except OSError:
        print(f"skip: _stale_line: text = atlas_path.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    line = None
    for candidate in text.splitlines():
        if candidate.startswith("last_attested:"):
            line = candidate
            break
    if line is None:
        return None

    m = re.match(r"^last_attested:\s*(\d{4}-\d{2}-\d{2})", line)
    if not m:
        return None
    last_date_str = m.group(1)
    try:
        last_dt = date.fromisoformat(last_date_str)
    except ValueError:
        print(f"skip: _stale_line: last_dt = date.fromisoformat(last_date_str) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    age_days = (today - last_dt).days
    if age_days < 0:
        age_days = 0

    if age_days > stale_days:
        return f"STALE {base} last_attested={last_date_str} age={age_days}d"
    return None


def run(argv: List[str], cwd: Optional[Path] = None) -> Tuple[List[str], int]:
    """Core logic: returns (stdout_lines, rc). rc is always 0 (informational)."""
    stale_days, run_stale_walk, error_line = _parse_args(argv)
    if error_line is not None:
        return [error_line], 0

    repo_root_str = _resolve_git_root(cwd)
    if not repo_root_str:
        return [], 0

    repo_root = Path(repo_root_str)
    systems_dir = repo_root / "docs" / "architecture" / "systems"
    if not systems_dir.is_dir():
        return [], 0

    atlas_pages = sorted(systems_dir.glob("*.md"))
    lines: List[str] = []

    for atlas in atlas_pages:
        base = atlas.stem
        lines.append(_watch_line(repo_root, systems_dir, base))

    if run_stale_walk:
        # Local calendar date — mirrors the bash oracle's `date +%s` (local
        # timezone) age-in-days computation, not UTC (see module docstring).
        today = date.today()
        for atlas in atlas_pages:
            base = atlas.stem
            stale = _stale_line(atlas, base, stale_days or _DEFAULT_STALE_DAYS, today)
            if stale is not None:
                lines.append(stale)

    return lines, 0


def main(argv: List[str]) -> int:
    """CLI entry: run the aggregator, print lines, return rc (always 0)."""
    lines, rc = run(argv)
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
