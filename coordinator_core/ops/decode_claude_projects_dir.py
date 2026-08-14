"""
coordinator_core.ops.decode_claude_projects_dir — heuristic decoder for
~/.claude/projects/ encoded directory names to repo-shortname candidates.

Purpose: Claude Code encodes invocation cwds into `~/.claude/projects/` subdir
names by replacing path separators with `-` (single dash for dir separator,
doubled for drive `:`). Encoding has varied across Claude Code versions, so
this decoder is heuristic — it surfaces candidates for PM review, not
authoritative paths. Used by `/update-docs` Phase 14/15 to seed the
candidates block in `~/.claude/state/repo-registry.md`.

Output (stdout): tab-separated `shortname<TAB>candidate-path<TAB>encoded-dir-name`,
one line per deduped candidate. Empty/garbled entries (e.g. `tmp`,
`--Users--<user>--.claude`) are dropped.

Exit codes:
    0 — one or more candidates emitted (count reported on stderr)
    1 — projects dir not found, OR the pre-existing bash-oracle zero-candidates
        crash reproduced (see Negative-spec below)
    (2 is the ORIGINAL script's *intended* zero-candidates exit code — see
    Negative-spec; it is never actually reachable in the bash oracle, and this
    port reproduces that unreachability rather than "fixing" it into a live
    exit-2 path.)

Port of: decode-claude-projects-dir.sh (DoE b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
               (clean-slate residual migration, R1 DOE-PORT wave)

Negative-spec (pre-existing bash-oracle bug — reproduced verbatim, NOT fixed):
    - The original .sh's "Smoke-test footer" intends `count="${#seen[@]}"` ==
      0 to print a WARNING and exit 2. Under `set -u`, referencing
      `${#seen[@]}` on an EMPTY bash associative array raises "unbound
      variable" and the script dies with exit 1 BEFORE the WARNING/exit-2
      branch is ever reached — confirmed empirically (golden-oracle run,
      2026-07-16) for both the "PROJECTS_DIR has zero subdirs" case and the
      "PROJECTS_DIR has subdirs but every one is filtered out" case. The
      intended exit-2 WARNING path is therefore DEAD CODE in the shipped
      bash script. This port reproduces the OBSERABLE contract (zero
      candidates -> exit 1, no WARNING message reaches stderr) rather than
      the bash parser's literal "line 118: seen: unbound variable" text,
      which has no meaningful Python analog. Do NOT "fix" this into a
      working exit-2 WARNING path — that would silently change the parity
      contract callers may depend on.
    - The bash `KNOWN_SUFFIX_MARKERS` strip-first-match loop and the
      drive-prefix regex ladder (`^([A-Za-z])---(.+)$` then `^([A-Za-z])--(.+)$`)
      are reproduced exactly, including trying the triple-dash form first.
    - The `claude-central` special-case (Users--<user>---claude /
      Users-<user>--claude -> `claude-central` shortname,
      `<drive>:/Users/<user>/.claude` path) is reproduced exactly, including
      that it only fires when `"claude"` appears anywhere in `rest`.
    - The `-tasks`/`-docs` suffix-collapse (drop if base already seen) is
      reproduced exactly; first-encoding-wins dedupe on shortname likewise.
    - Line ORDER is NOT reproduced byte-for-byte against the bash oracle.
      The bash script has no explicit `sort` call — its output order is
      whatever `for entry in "${PROJECTS_DIR}"/*/` glob-expansion order the
      shell + libc + filesystem happen to produce (locale-collated glob
      matching in bash, readdir order elsewhere), which is itself
      non-portable and non-reproducible across machines/filesystems (empirically
      confirmed: bash's glob order does not match Python's raw `iterdir()`
      order, NOR a `locale.strxfrm`-keyed sort, on the same directory).
      Since the script never documents or relies on a specific order (the
      "Dedupe on shortname" comment cares about first-seen-wins for
      CONTENT, not position) and the sole consumer (`/update-docs` Phase
      14/15 candidate seeding) treats the output as an unordered candidate
      set for PM review, this port instead emits lines sorted deterministically
      by `shortname` — a portable, reproducible substitute for an ordering
      the oracle itself never specified. Content (field values, dedupe
      decisions, filtering) is reproduced exactly; only presentation ORDER
      is deliberately changed to be deterministic instead of readdir-order-dependent.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_KNOWN_SUFFIX_MARKERS = ("--tasks--", "--plugins--", "--control--", "--docs--", "--scratch--")


def _strip_known_suffix(repo_part: str) -> str:
    for marker in _KNOWN_SUFFIX_MARKERS:
        idx = repo_part.find(marker)
        if idx != -1:
            repo_part = repo_part[:idx]
    return repo_part


def _decode_drive_prefix(repo_part: str) -> Optional[Tuple[str, str]]:
    """Return (drive, rest) trying the triple-dash form before the double-dash form."""
    if len(repo_part) >= 4 and repo_part[0].isalpha() and repo_part[1:4] == "---":
        return repo_part[0], repo_part[4:]
    if len(repo_part) >= 3 and repo_part[0].isalpha() and repo_part[1:3] == "--":
        return repo_part[0], repo_part[3:]
    return None


def _is_skipped_name(name: str) -> bool:
    if name in ("tmp", "", ".", ".."):
        return True
    if name.startswith("tmp-") or name.endswith("-smoketest"):
        return True
    if name.startswith("--Users") or name.startswith("AppData-Local-Temp"):
        return True
    if "AppData" in name:
        return True
    return False


def _decode_one(name: str) -> Optional[Tuple[str, str]]:
    """Decode one encoded directory basename to (shortname, candidate_path), or None to skip."""
    if _is_skipped_name(name):
        return None

    repo_part = _strip_known_suffix(name)
    decoded = _decode_drive_prefix(repo_part)
    if decoded is None:
        return None
    drive, rest = decoded

    if not rest or rest == "-":
        return None

    shortname = rest.split("--", 1)[0]
    if len(shortname) < 2:
        return None

    drive_lower = drive.lower()
    candidate_path = f"{drive_lower}:/{shortname}"

    if shortname == "Users" and "claude" in rest:
        # Users--<user>---claude -> reproduce bash's `^Users--([^-]+)---claude` regex
        # against the raw `rest` string exactly (not a split()-based approximation).
        m = re.match(r"^Users--([^-]+)---claude", rest)
        if m and "claude" in rest:
            username = m.group(1)
            candidate_path = f"{drive_lower}:/Users/{username}/.claude"
            shortname = "claude-central"
    if shortname != "claude-central":
        m2 = re.match(r"^Users-([^-]+)$", shortname)
        if m2 and "claude" in rest:
            username = m2.group(1)
            candidate_path = f"{drive_lower}:/Users/{username}/.claude"
            shortname = "claude-central"

    return shortname, candidate_path


def _run(projects_dir: str) -> Tuple[List[str], List[str], int]:
    """Returns (stdout_lines, stderr_lines, exit_code)."""
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    root = Path(projects_dir)
    if not root.is_dir():
        stderr_lines.append(f"decode-claude-projects-dir.sh: projects dir not found: {projects_dir}")
        return stdout_lines, stderr_lines, 1

    seen: dict = {}
    try:
        entries = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        entries = []

    for name in entries:
        decoded = _decode_one(name)
        if decoded is None:
            continue
        shortname, candidate_path = decoded

        if shortname.endswith("-tasks") or shortname.endswith("-docs"):
            base = shortname
            if base.endswith("-tasks"):
                base = base[: -len("-tasks")]
            if base.endswith("-docs"):
                base = base[: -len("-docs")]
            if base in seen:
                continue

        if shortname not in seen:
            seen[shortname] = 1
            stdout_lines.append((shortname, f"{shortname}\t{candidate_path}\t{name}"))

    stdout_lines = [line for _, line in sorted(stdout_lines, key=lambda pair: pair[0])]

    if not seen:
        # Reproduces the bash oracle's pre-existing "unbound variable" crash on
        # `${#seen[@]}` for an empty associative array under `set -u` — see
        # module docstring Negative-spec. The INTENDED WARNING message below
        # is dead code in the original .sh and is never reached; kept here only
        # as documentation of intent, not as emitted output.
        # WARNING — zero candidates decoded from {projects_dir}  (unreachable in oracle)
        return stdout_lines, stderr_lines, 1

    count = len(seen)
    stderr_lines.append(f"decode-claude-projects-dir.sh: {count} candidate(s) emitted")
    return stdout_lines, stderr_lines, 0


def main(argv: List[str]) -> int:
    projects_dir = argv[0] if argv else os.path.join(str(Path.home()), ".claude", "projects")

    stdout_lines, stderr_lines, exit_code = _run(projects_dir)

    for line in stdout_lines:
        print(line)
    for line in stderr_lines:
        print(line, file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
