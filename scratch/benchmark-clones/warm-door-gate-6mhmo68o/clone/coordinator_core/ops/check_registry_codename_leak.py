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

Port of: check-registry-codename-leak.sh (DoE b5a4192c, 2026-07-20)
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
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from coordinator_core.machine_resolver import merged_flat_registry as _merged_flat_registry

# D1 keep-set — prefix-matched against slug (strip repos. prefix first).
# 'coordinator' matches 'coordinator_claude'; 'deep_research' matches
# 'deep_research_claude'; 'example_retrieval_repo' matches 'example_retrieval_repo_ue_addon'.
# 'doe_claude' kept: OSS resolve-coordinator-clone.sh reads repos.doe_claude
# at runtime (PM-ratified 2026-07-10).
# 'example_doctrine_repo' kept: a SECOND machine-local registry alias for the
# same DoE-claude clone (`machine-local get repos.example_doctrine_repo` ==
# `machine-local get repos.doe_claude`, both resolving to this machine's
# DoE-claude clone path, verified 2026-08-13) -- it was this machine's
# anonymizing scrub placeholder
# for `doe_claude` before the 2026-08-13 PM ruling (571a4d78f535) stopped
# scrubbing the DoE family. Removing that `doe_claude -> example_doctrine_repo`
# depersonalize mapping collaterally removed the ONLY thing that had been
# excluding this alias's own text from the no-residual-pattern leak-check --
# `example_doctrine_repo` was never itself KEEPSET, only ever exempted as
# "this row's own placeholder output". Once nothing produces it as placeholder
# output anymore, the guard correctly starts treating it as a bare registered
# `repos.*` slug and flags every source-comment citation of the incident it
# documents (e.g. coordinator_core/ops/percolate_run.py's own docstring,
# coordinator_core/ops/coordinator_doe_root.py). Same sibling, same ruling,
# same disclosure -- KEEPSET is the narrow, named fix; not a pattern loosen.
# 'fleet_root' kept: `repos.fleet_root` is not a private repo codename -- it
# names the CONTAINER directory the fleet's repos live under.
# `git_hook_install.py`'s own `_CONTAINER_REGISTRY_KEYS` comment says these
# "are not unclassifiable repos; they are not repos at all, and never reach a
# verdict." Same generic/public-slug class as 'game_dev', 'web_dev',
# 'data_science', 'coordinator', 'experiments' above.
KEEPSET: Sequence[str] = (
    "example_retrieval_repo",
    "deep_research",
    "game_dev",
    "web_dev",
    "data_science",
    "coordinator",
    "experiments",
    "doe_claude",
    "example_doctrine_repo",
    "fleet_root",
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

    A silent no-op here would let a `doe-claude` (hyphen) vs `doe_claude`
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
    """Resolve repos.* keys — env override first, else in-process registry read.

    COORDINATOR_CODENAME_REGISTRY_KEYS (space/tab/newline-separated) takes
    priority; else `merged_flat_registry` reads the same registry.local.toml
    over registry.toml chain `machine-local keys` would enumerate, in-process
    (zero-spawn — see `coordinator_core.machine_resolver.merged_flat_registry`),
    filtered to keys starting with `repos.`. `repos.*` is a confirmed
    root-namespace-only key (never a promoted concern-file namespace), so no
    `machine-local` binary presence/resolution rung is needed first.
    """
    override = env.get("COORDINATOR_CODENAME_REGISTRY_KEYS", "")
    if override:
        return [tok for tok in re.split(r"[ \t\n]+", override.strip()) if tok]

    return sorted(key for key in _merged_flat_registry() if key.startswith("repos."))


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
