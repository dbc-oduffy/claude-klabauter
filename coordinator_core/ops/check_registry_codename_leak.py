"""
coordinator_core.ops.check_registry_codename_leak — registry-derived novel-leak guard.

Purpose: greps a target directory for any private-repo codename derived from
the machine-local registry's `repos.*` keys, after subtracting the D1
keep-set. Used by percolate post-rsync hooks (both coordinator-claude and
deep-research-claude publish targets) to fail-closed on an accidental private
codename leaking into a publish tree.

Registry keys use underscores (`example_cockpit_repo`); prose codenames commonly
use hyphens (`example-cockpit-repo`) — this guard derives and greps BOTH forms per
slug, since grepping only the underscore form would silently miss the exact
leak class this guard exists to catch.

Port of: check-registry-codename-leak.sh (coordinator-claude b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-06-27-genericize-provenance-sweeper.md § C4 / AC11
               docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Exit codes (parity-critical — callers branch on these):
    0 — clean (no private codenames found), OR machine-local absent and no
        override supplied (registry lookup skipped, nothing to check)
    1 — leak found (file:line report written to stderr), OR scan incomplete
        (one or more files could not be read — fail-closed, see
        BEHAVIOUR CHANGE note on _grep_pattern)
    2 — usage error (missing/extra arg, target-dir not found)

Testability override: if COORDINATOR_CODENAME_REGISTRY_KEYS is set (space- or
newline-separated list of repos.* keys, e.g.
"repos.example_cockpit_repo repos.example_retrieval_repo"), those keys are used instead of
calling machine-local. This allows fixture tests to inject a synthetic
registry without a live machine-local installation.

Negative-spec (faithfully reproduces bash-oracle behavior, not "fixed"):
    - machine-local absent AND no override → WARNING to stderr, then exit 0
      (not exit 2) — this is the oracle's existing, deliberate behavior (a
      distinct message from a genuinely-empty registry per the D-note in the
      bash source, but the exit code is the same 0 in both cases).
    - Grep is case-insensitive, skips binary files, excludes `.git/`,
      `*.bak`, `*.tmp`, `*.orig` — mirrors the bash grep flags
      (`-rni -I --exclude-dir='.git' --exclude=...`) exactly.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from coordinator_core._settings_home import home_dir, settings_home
from coordinator_core.win_portability import is_executable, no_console_creationflags

# D1 keep-set — prefix-matched against slug (strip repos. prefix first).
# 'coordinator' matches 'coordinator_claude'; 'deep_research' matches
# 'deep_research_claude'; 'example_retrieval_repo' matches 'example_retrieval_repo_ue_addon'.
# 'example_doctrine_repo' kept: OSS resolve-coordinator-clone.sh reads repos.example_doctrine_repo
# at runtime (PM-ratified 2026-07-10).
KEEPSET: Sequence[str] = (
    "example_retrieval_repo",
    "deep_research",
    "game_dev",
    "web_dev",
    "data_science",
    "coordinator",
    "experiments",
    "example_doctrine_repo",
)

_EXCLUDE_DIRS = {".git"}
_EXCLUDE_SUFFIXES = (".bak", ".tmp", ".orig")

_PROG = "check-registry-codename-leak.sh"


def _usage() -> str:
    return (
        f"Usage: {_PROG} <target-dir>\n"
        "  Greps target-dir for private-repo codenames from the machine-local registry.\n"
        "  Set COORDINATOR_CODENAME_REGISTRY_KEYS to inject a synthetic key list (testing).\n"
        "  Optional: --no-exempt <slug> (repeatable, before target-dir) re-admits a\n"
        "  KEEPSET slug as leak-checkable for this target; <slug> must be an exact\n"
        "  KEEPSET member. Set COORDINATOR_CODENAME_NO_EXEMPT for the same re-admission\n"
        "  via env (space/tab/newline-separated slugs), unioned with any --no-exempt flags.\n"
    )


def _is_kept(slug: str, no_exempt: Sequence[str] = ()) -> bool:
    for entry in KEEPSET:
        if slug == entry or slug.startswith(entry + "_"):
            if entry in no_exempt:
                return False
            return True
    return False


def _validate_no_exempt(no_exempt: Sequence[str]) -> None:
    """Raise ValueError if any re-admitted slug is not an exact KEEPSET member.

    A silent no-op here would let a `coordinator-claude` (hyphen) vs `example_doctrine_repo`
    (underscore) authoring slip through as a no-op re-admission — the target
    would keep its original (unintended) exemption instead of failing loud.
    """
    invalid = [s for s in no_exempt if s not in KEEPSET]
    if invalid:
        raise ValueError(
            f"{_PROG}: --no-exempt/COORDINATOR_CODENAME_NO_EXEMPT slug(s) not in "
            f"KEEPSET: {', '.join(invalid)}. Valid KEEPSET members: "
            f"{', '.join(KEEPSET)}"
        )


def _resolve_registry_keys(env: dict) -> List[str]:
    """Resolve repos.* keys — env override first, else machine-local subprocess.

    Mirrors the bash resolution order exactly: COORDINATOR_CODENAME_REGISTRY_KEYS
    (space/tab/newline-separated) takes priority; else `machine-local keys` (on
    PATH, at the canonical settings-home install, or at the legacy
    `$HOME/.claude/bin/machine-local` fallback), filtered to lines starting
    with `repos.`.
    """
    override = env.get("COORDINATOR_CODENAME_REGISTRY_KEYS", "")
    if override:
        return [tok for tok in re.split(r"[ \t\n]+", override.strip()) if tok]

    ml_bin = shutil.which("machine-local", path=env.get("PATH"))
    if not ml_bin:
        # Settings-home is the canonical install (DR-072); ~/.claude/bin is the
        # legacy rung, retired 2026-07-28 and kept only for machines predating
        # the move. Without the settings-home rung this resolver reaches only
        # PATH — and settings-home/bin is NOT on PATH by default — so on a
        # stock machine it found nothing and the registry lookup was skipped
        # with a warning that reads like an absent registry.
        # env.get("HOME") honours test-injected HOME overrides; the
        # home_dir() fallback keeps this rung Windows-safe (USERPROFILE-aware
        # via Path.home()) when the injected env carries no HOME at all —
        # native Windows shells don't set HOME, and a raw `env.get("HOME", "")`
        # read degraded to a cwd-relative path in that case (this rung is now
        # outranked by settings-home above, but the landmine is removed
        # rather than left merely dormant).
        legacy_home = env.get("HOME") or str(home_dir())
        for candidate in (
            os.path.join(str(settings_home()), "bin", "machine-local"),
            os.path.join(legacy_home, ".claude", "bin", "machine-local"),
        ):
            if candidate and os.path.isfile(candidate) and is_executable(candidate):
                ml_bin = candidate
                break

    if not ml_bin:
        print(
            f"{_PROG}: WARNING: machine-local not found; registry lookup skipped.",
            file=sys.stderr,
        )
        print(
            "  Set COORDINATOR_CODENAME_REGISTRY_KEYS to run without machine-local.",
            file=sys.stderr,
        )
        return []

    try:
        proc = subprocess.run(
            [ml_bin, "keys"],
            capture_output=True,
            text=True,
            env=env,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _resolve_registry_keys: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []

    keys: List[str] = []
    for line in (proc.stdout or "").splitlines():
        if line.startswith("repos."):
            keys.append(line)
    return keys


def _is_probably_binary(path: Path) -> bool:
    """Approximate grep -I: treat a file as binary if it contains a NUL byte
    in its first 8KB (matches grep's own binary-detection heuristic closely
    enough for this guard's purposes — a false-negative here just means one
    extra file gets scanned, not a missed leak)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        print(f"skip: _is_probably_binary: with open(path, \"rb\") as f: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return b"\x00" in chunk


def _iter_candidate_files(target_dir: Path):
    for root, dirnames, filenames in os.walk(target_dir):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fname in filenames:
            if fname.endswith(_EXCLUDE_SUFFIXES):
                continue
            yield Path(root) / fname


def _grep_pattern(target_dir: Path, pattern: str, unreadable: List[str]) -> List[str]:
    """Case-insensitive substring search for `pattern` across target_dir,
    skipping binary files / .git / *.bak/*.tmp/*.orig. Returns
    'path:lineno:line' hit strings, matching `grep -rni` output shape.

    `unreadable` is appended to (not returned) with any file this scan could
    not open — see BEHAVIOUR CHANGE note below.

    BEHAVIOUR CHANGE (2026-07-22, break-class fix): an unreadable file was
    previously silently excluded from the scan with a bare `continue`. This
    guard's own docstring/module purpose declares fail-closed intent ("fail-
    closed on an accidental private codename leaking into a publish tree"),
    so silently narrowing the scanned set contradicted the stated contract —
    a leak in an unreadable file could ship undetected. `main()` now treats
    any unreadable file as scan-incomplete and exits non-zero.
    """
    needle = pattern.lower()
    hits: List[str] = []
    for path in _iter_candidate_files(target_dir):
        if _is_probably_binary(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if needle in line.lower():
                        hits.append(f"{path}:{lineno}:{line.rstrip(chr(10))}")
        except OSError as exc:
            print(
                f"{_PROG}: WARNING: unreadable file excluded from codename-leak scan: {path}: {exc}",
                file=sys.stderr,
            )
            unreadable.append(str(path))
            continue
    return hits


def main(argv: Optional[List[str]] = None, env: Optional[dict] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    env = dict(os.environ) if env is None else dict(env)

    no_exempt_flags: List[str] = []
    positionals: List[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--no-exempt":
            if i + 1 >= len(argv):
                sys.stderr.write(_usage())
                return 2
            no_exempt_flags.append(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--"):
            sys.stderr.write(_usage())
            return 2
        positionals.append(arg)
        i += 1

    if len(positionals) != 1:
        sys.stderr.write(_usage())
        return 2

    target_dir = Path(positionals[0])
    if not target_dir.is_dir():
        print(f"{_PROG}: target-dir not found: {positionals[0]}", file=sys.stderr)
        return 2

    env_override = env.get("COORDINATOR_CODENAME_NO_EXEMPT", "")
    env_no_exempt = (
        [tok for tok in re.split(r"[ \t\n]+", env_override.strip()) if tok]
        if env_override
        else []
    )
    no_exempt: List[str] = list(dict.fromkeys(no_exempt_flags + env_no_exempt))

    try:
        _validate_no_exempt(no_exempt)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    registry_keys = _resolve_registry_keys(env)

    leak_slugs: List[str] = []
    for key in registry_keys:
        slug = key[len("repos."):] if key.startswith("repos.") else key
        if _is_kept(slug, no_exempt=no_exempt):
            continue
        leak_slugs.append(slug)

    if not leak_slugs:
        print(
            f"{_PROG}: no private codenames to check (empty registry or all keys in keep-set).",
            file=sys.stderr,
        )
        return 0

    found_any = False
    all_hits: List[str] = []
    unreadable_files: List[str] = []

    for slug in leak_slugs:
        hyphen_form = slug.replace("_", "-")
        patterns = [slug]
        if hyphen_form != slug:
            patterns.append(hyphen_form)

        for pattern in patterns:
            hits = _grep_pattern(target_dir, pattern, unreadable_files)
            if hits:
                found_any = True
                all_hits.extend(hits)

    # BEHAVIOUR CHANGE (2026-07-22, break-class fix): restores this guard's
    # own documented fail-closed contract — an incomplete scan (any file we
    # couldn't read) is treated as non-clean rather than silently reported
    # as "clean" alongside a narrowed scanned set.
    if unreadable_files:
        uniq_unreadable = sorted(set(unreadable_files))
        print(
            f"{_PROG}: scan incomplete — {len(uniq_unreadable)} file(s) could not be read; "
            "treating as non-clean (fail-closed).",
            file=sys.stderr,
        )
        for p in uniq_unreadable:
            print(f"  {p}", file=sys.stderr)
        return 1

    if found_any:
        print(f"{_PROG}: private codename(s) found in publish tree.", file=sys.stderr)
        print(
            "  Add the codename to coordinator/bin/codename-provenance-seed.sh (rewrite) or",
            file=sys.stderr,
        )
        print(
            "  extend the D1 keep-set in this script (load-bearing system vocabulary only):",
            file=sys.stderr,
        )
        # Trailing blank line matches the bash oracle's `echo "$all_hits"`,
        # which double-terminates (each hit line already ends in a newline
        # from the accumulation loop, and `echo` appends one more).
        print("\n".join(all_hits) + "\n", file=sys.stderr)
        return 1

    print(f"{_PROG}: clean — no unregistered private codenames found.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
