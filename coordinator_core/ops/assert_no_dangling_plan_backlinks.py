"""
coordinator_core.ops.assert_no_dangling_plan_backlinks — AC9 gate for
programmatic terminal-plan archival.

Purpose: after the C4 backfill moves a terminal plan
docs/plans/<name>.md -> archive/specs/YYYY-MM/<name>.md, every spec_backlink
that still cites the old docs/plans/ path dangles. This gate asserts ZERO
such dangling backlinks in active doctrine surfaces, and --fix repoints them
to the archive location.

Scope (both detect and --fix):
  - spec_backlink CONVENTION lines only -- a line matching /spec.?backlink/i.
    Incidental prose mentions ("Phase 2d reverses docs/plans/X") are NOT
    rewritten; only the citation convention is healed.
  - A cited plan counts as "moved" iff it exists under archive/specs/ AND no
    longer exists at docs/plans/<name>.md.
  - Excluded trees: archive/ (terminal paper trail) and dist/ (generated
    publish artifacts) anywhere in the path, tasks/ and state/ (ephemera
    swept by /distill) at the path root, and plan review sidecars
    (<plan-stem>.<tag>.md, detected by an extra dot in the stem).

Exit codes (parity-critical):
  0 -- no dangling backlinks (or nothing to check / --fix healed everything)
  1 -- >=1 dangling backlink found, --fix not requested, OR the scan could
       not read every candidate file (fail-closed — see _scan_dangling)
  2 -- --fix requested but the `perl` binary is not on PATH

Port of: assert-no-dangling-plan-backlinks.sh (example-doctrine-repo b5a4192c, 2026-07-20).
Spec backlink: archive/specs/2026-06/2026-06-23-programmatic-terminal-plan-archival.md § AC9 / C6
               docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec:
    - stdout/stderr split is preserved from the bash oracle: the "OK: ..."
      short-circuit messages and the --fix "healed N ..." summary go to
      stdout; the per-file "DANGLING in: ..." detail lines and the final
      "FAIL: N dangling ..." summary go to stderr. Callers that only capture
      stdout (as the bash oracle's own callers do) rely on this split.
    - Known bash-oracle quirk NOT reproduced here: under `set -u`, a bash
      associative array (`declare -A MVPATH`) that is declared but never
      assigned an element is treated by some bash builds as "unbound" when
      `${#MVPATH[@]}` is referenced, which silently skips the intended
      "OK: no moved plans" early-return message (the script still reaches
      the correct final "OK: no dangling ..." / exit 0 outcome, just via a
      different code path and a different message). This is an accidental
      artifact of bash's empty-associative-array/`set -u` interaction, not
      designed behavior -- the `[[ "${#MVPATH[@]}" -eq 0 ]] && { echo "OK:
      no moved plans"; exit 0; }` line's own intent is unambiguous. This
      Python port implements the DESIGNED behavior (prints "OK: no moved
      plans" and returns 0) rather than the bash accident; the two paths are
      exit-code- and dangling-count-identical (both 0 / no dangling), only
      the human-facing message text differs in this one edge case.
    - The candidate-line filter is two-stage, matching the bash oracle's
      two-stage grep exactly: a line must match /spec.?backlink/i (any
      single character, including none, between "spec" and "backlink")
      AND contain the literal substring "docs/plans/". Only lines passing
      BOTH stages are scanned for the stricter dated-plan-path regex
      (`docs/plans/YYYY-MM-DD-slug.md`) that actually extracts candidates.
    - Exclusion checks run on already-matched hits, never on the recursive
      file walk itself -- mirroring the bash oracle's own file-then-line
      order (broad grep first, exclusion filter second) so a symlinked or
      oddly-named file that WOULD match the walk is still excluded exactly
      the same way the bash version excludes it post-grep.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from coordinator_core.session.declared_writes import declare_write

_PLAN_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9.-]+$")
_BACKLINK_LINE_RE = re.compile(r"spec.?backlink", re.IGNORECASE)
_PLAN_PATH_RE = re.compile(r"docs/plans/[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9.-]+\.md")

_EXCLUDED_ROOT_PREFIXES = ("tasks/", "state/")


def _is_sidecar(basename_md: str) -> bool:
    """A plan-shaped basename with an extra dot in its stem is a sidecar
    (<plan-stem>.<tag>.md), not a plan itself."""
    stem = basename_md[:-3] if basename_md.endswith(".md") else basename_md
    return "." in stem


def _is_excluded_path(rel_path: str) -> bool:
    """Mirrors the bash oracle's two exclusion checks:
    `case "/$file" in */archive/*|*/dist/*) continue;; esac` (anywhere in
    path) and `case "$file" in tasks/*|state/*) continue;; esac` (root only)."""
    slashed = "/" + rel_path
    if "/archive/" in slashed or "/dist/" in slashed:
        return True
    if rel_path.startswith(_EXCLUDED_ROOT_PREFIXES):
        return True
    return False


def _find_all_md_files(root: str) -> List[str]:
    """Root-relative, forward-slash, sorted .md paths under `root` (all
    trees included -- exclusion is applied by the caller per-hit, matching
    the bash oracle's grep-then-filter order)."""
    out: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append(rel)
    out.sort()
    return out


def _build_moved_plan_map(root: str) -> Dict[str, str]:
    """basename -> root-relative archive/specs/... path, for every plan that
    now lives under archive/specs/ AND no longer lives at docs/plans/<base>."""
    archive_specs = os.path.join(root, "archive", "specs")
    mvpath: Dict[str, str] = {}
    for rel in _find_all_md_files(archive_specs):
        full_rel = "archive/specs/" + rel
        base = os.path.basename(rel)
        if _is_sidecar(base):
            continue
        if os.path.exists(os.path.join(root, "docs", "plans", base)):
            continue  # still live -> not moved
        mvpath[base] = full_rel
    return mvpath


def _resolve_root(explicit_root: Optional[str]) -> Optional[str]:
    if explicit_root:
        return explicit_root
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _resolve_root: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _scan_dangling(
    root: str, mvpath: Dict[str, str], unreadable: List[str]
) -> List[Tuple[str, str, str, str]]:
    """Returns a list of (rel_file, matched_ref, base, dest) tuples, ONE per
    unique (file, base) pair -- mirrors the bash oracle's SEEN_PAIRS dedup
    ("a plan cited on several spec_backlink lines in one file is one fix,
    not N").

    `unreadable` is appended to (not returned) with any candidate file this
    scan could not open — see BEHAVIOUR CHANGE note below.

    BEHAVIOUR CHANGE (2026-07-22, break-class fix): an unopenable file was
    previously silently excluded from the scan via a bare `continue`, so this
    AC9 commit gate could return 0 ("no dangling backlinks") while a dangling
    backlink existed in the unreadable file. `main()` now treats any
    unreadable file as scan-incomplete and fails loud.
    """
    results: List[Tuple[str, str, str, str]] = []
    seen_pairs = set()
    for rel_file in _find_all_md_files(root):
        if _is_excluded_path(rel_file):
            continue
        base_name = os.path.basename(rel_file)
        if _is_sidecar(base_name):
            continue
        full_path = os.path.join(root, rel_file)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            print(
                f"WARNING: unreadable file excluded from dangling-backlink scan: {full_path}: {exc}",
                file=sys.stderr,
            )
            unreadable.append(rel_file)
            continue
        for line in lines:
            if not _BACKLINK_LINE_RE.search(line):
                continue
            if "docs/plans/" not in line:
                continue
            for match in _PLAN_PATH_RE.findall(line):
                base = os.path.basename(match)
                dest = mvpath.get(base)
                if not dest:
                    continue  # cited plan not moved
                key = (rel_file, base)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                results.append((rel_file, match, base, dest))
    return results


def _fix_file(full_path: str, src: str, dst: str) -> bool:
    """Literal-replace docs/plans/<base> -> <dest>, ONLY on spec_backlink
    convention lines, via the same perl one-liner the bash oracle uses
    (perl quotemeta on both env-supplied strings)."""
    perl = shutil.which("perl")
    if not perl:
        return False
    env = dict(os.environ)
    env["SRC"] = src
    env["DST"] = dst
    proc = subprocess.run(
        [perl, "-i", "-pe", r's/\Q$ENV{SRC}\E/$ENV{DST}/g if /spec.?backlink/i', full_path],
        env=env, capture_output=True, text=True,
    )
    if proc.returncode == 0:
        # DR-276: declared AFTER the in-place perl edit lands.
        declare_write(full_path)
    return proc.returncode == 0


def main(argv: List[str]) -> int:
    root_arg: Optional[str] = None
    fix = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            root_arg = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg == "--fix":
            fix = True
            i += 1
            continue
        i += 1

    root = _resolve_root(root_arg)
    if not root:
        return 0

    if not os.path.isdir(os.path.join(root, "archive", "specs")):
        print("OK: no archive/specs/ — nothing to heal")
        return 0

    mvpath = _build_moved_plan_map(root)
    if not mvpath:
        print("OK: no moved plans")
        return 0

    unreadable: List[str] = []
    hits = _scan_dangling(root, mvpath, unreadable)

    # BEHAVIOUR CHANGE (2026-07-22, break-class fix): restores this AC9 gate's
    # intended assertion — an incomplete scan can never be reported as "no
    # dangling backlinks" (see _scan_dangling docstring).
    if unreadable:
        for rel_file in unreadable:
            print(f"UNSCANNABLE: {rel_file}", file=sys.stderr)
        print(
            f"FAIL: {len(unreadable)} file(s) could not be scanned for dangling plan "
            "backlinks — assertion cannot be made (fail-closed)",
            file=sys.stderr,
        )
        return 1

    if fix:
        if hits and shutil.which("perl") is None:
            print("ERROR: --fix needs perl; not found", file=sys.stderr)
            return 2
        fixed = 0
        for rel_file, _match, base, dest in hits:
            full_path = os.path.join(root, rel_file)
            src = "docs/plans/" + base
            if _fix_file(full_path, src, dest):
                fixed += 1
        print(f"healed {fixed} dangling backlink citation(s)")
        return 0

    dangling = len(hits)
    if dangling > 0:
        seen_files = []
        for rel_file, match, _base, dest in hits:
            if rel_file not in seen_files:
                print(f"DANGLING in: {rel_file}", file=sys.stderr)
                seen_files.append(rel_file)
            print(f"  {match} → {dest}", file=sys.stderr)
        print(
            f"FAIL: {dangling} dangling plan backlink(s) to moved docs/plans/ paths "
            "— run with --fix",
            file=sys.stderr,
        )
        return 1

    print("OK: no dangling plan backlinks to moved docs/plans/ paths")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
