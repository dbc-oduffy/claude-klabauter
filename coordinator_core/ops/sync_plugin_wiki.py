"""
coordinator_core.ops.sync_plugin_wiki — verify no plugin-doctrine wiki accidentally
has a dev-side mirror (DOE-PORT clean-slate migration, variant #1 — direct-import
trampoline, no registered op).

Purpose: verify no plugin-doctrine wiki accidentally has a dev-side mirror. Under
Option B (adopted 2026-05-15, docs/plans/2026-05-15-plugin-wiki-write-direction-trap.md
§ Phase 4), plugin-doctrine wikis live ONLY at <plugin>/docs/wiki/<name>.md — there is
no dev-side authoring copy. This module confirms the invariant: for each wiki name
cited from plugin files, check that no mirror exists at ~/.claude/docs/wiki/<name>.md.

If a dev-side mirror is found, the single-tree invariant is broken — the write-direction
trap has been re-introduced. Exit 5 with a structured error naming both paths, mtimes,
and a remediation instruction.

Exit codes (verbatim from the bash oracle, plus a fail-closed addition marked Tier 2 below):
    0 — clean: no dev-side mirrors found for any plugin-cited wiki
    1 — PLUGIN_ROOT resolution failure, OR (Tier 2, behaviour change) wiki-name
        discovery scan was incomplete because one or more files could not be
        read -- a missed file could be exactly the mirror/orphan the tool
        exists to catch, so this is reported as non-clean rather than silently
        excluded from the scan
    2 — usage error (unknown flag)
    5 — dev-side mirror of plugin-doctrine wiki detected; single-tree invariant broken

Missing-bundled policy: if a plugin file references docs/wiki/<name>.md but <name>.md
is absent from the bundled wiki, warn and continue (doc-link-checker's job, not this
script's).

PLUGIN_ROOT resolution: `CLAUDE_PLUGIN_ROOT` env var is rung 1 (matches the oracle's
own rung 1); past that, resolution delegates to the shared native port of
`resolve-coordinator-clone.sh --content-root` —
`coordinator_core.resolve_coordinator_clone.resolve_content_root` (C11 resolver
centralization; `coordinator_core.ops.fan_out_integrator._resolve_plugin_root`
consumes the same shared resolver rather than a second independent ladder). On any
resolution failure, `_resolve_plugin_root` returns "" — the caller treats this as the
oracle's own fail-loud ERROR path.

Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
Port of: sync-plugin-wiki.sh (example-doctrine-repo b5a4192c, 2026-07-20)

Negative-spec (faithfully reproduced from the bash oracle — do NOT "fix" mid-port):
    - `--check-only` is accepted but is a pure no-op (kept only for caller
      compatibility) — the oracle's own comment says "new semantics always
      report-only (no writes to avoid)".
    - Placeholder filter list is a fixed set (foo/bar/baz/qux/example/name/
      your-wiki-name/wiki-name/held-out-slice-sealing-doctrine/
      lfs-gitattributes-discipline) — not configurable, ported verbatim.
    - `--help` in THIS module is an authored usage string, not a grep-and-strip
      of this file's own leading `#` comment block — the oracle's `grep '^#' "$0"`
      self-introspection trick is bash-source-file-specific and has no faithful
      analog for a Python engine module invoked via direct import (there is no
      "$0" pointing at commented CLI source at the trampoline's call site). The
      exit code (0) and general content (flags + exit codes) are preserved.
    - Mtime formatting shells out to the real `stat` binary (GNU `-c "%y"` first,
      BSD `-f "%Sm"` fallback, "unknown" on both failing) rather than
      reimplementing either format in Python — this is a direct byte-for-byte
      reuse of the oracle's own dual-form invocation, not a re-derivation.
    - A wiki name cited from a top-level file (CLAUDE.md, README.md, ...) that
      does not exist on disk is silently skipped (mirrors `2>/dev/null` on the
      oracle's grep of a missing file) rather than erroring.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import List

import coordinator_core.resolve_coordinator_clone as rcc

_SUBPROCESS_TIMEOUT_SECS = 15

_PLACEHOLDER_NAMES = frozenset(
    {
        "foo",
        "bar",
        "baz",
        "qux",
        "example",
        "name",
        "your-wiki-name",
        "wiki-name",
        "held-out-slice-sealing-doctrine",
        "lfs-gitattributes-discipline",
    }
)

_WIKI_CITATION_RE = re.compile(r"docs/wiki/[a-zA-Z0-9_-]+\.md")

_TOP_LEVEL_FILES = (
    "CLAUDE.md",
    "README.md",
    "em-operating-model.md",
    "capability-catalog.md",
)

_SCAN_DIRS = (
    "agents",
    "commands",
    "skills",
    "snippets",
    "pipelines",
    "hooks",
)

_HELP_TEXT = """\
sync-plugin-wiki.sh — verify no plugin-doctrine wiki accidentally has a dev-side mirror.

Under Option B (adopted 2026-05-15), plugin-doctrine wikis live ONLY at
<plugin>/docs/wiki/<name>.md. There is no dev-side authoring copy. This
script confirms the invariant: for each wiki name cited from plugin files,
check that no mirror exists at ~/.claude/docs/wiki/<name>.md.

If a dev-side mirror is found, the single-tree invariant is broken — the
write-direction trap has been re-introduced. Exit 5 with a structured error
message naming both paths, their mtimes, and a remediation instruction.

Exit codes:
  0 — clean: no dev-side mirrors found for any plugin-cited wiki
  2 — usage error / unexpected
  5 — dev-side mirror of plugin-doctrine wiki detected; single-tree invariant broken

Missing-bundled policy: if a plugin file references docs/wiki/<name>.md but
<name>.md is absent from the bundled wiki, warn and continue. That is a
doc-link issue (handled by the doc-link-checker), not a mirror issue.

Flags:
  --check-only  report status without exiting non-zero on warnings; exit 5 still fires on mirrors
  --quiet       suppress per-file output
  COORDINATOR_OVERRIDE_WIKI_MIRROR=1  suppress exit-5 and warn instead (rare-use; e.g., authoring
                a dev-side wiki that genuinely should not exist in plugin tree)
"""


def _home_dir() -> str:
    """POSIX-parity ``$HOME`` resolution: env var first, THEN ``expanduser("~")``.

    ``os.path.expanduser("~")`` on Windows prefers ``USERPROFILE`` (then
    ``HOMEDRIVE``+``HOMEPATH``) and only falls back to ``HOME`` when all of
    those are absent -- so a test/caller that sets only ``HOME`` (the bash
    oracle's own override var, and the only one `sandbox_home`-style test
    fixtures set) is silently ignored on Windows, resolving into the real
    user profile instead of the intended override. Reading ``HOME`` first
    matches the bash oracle's literal ``$HOME`` semantics on every platform.
    """
    return os.environ.get("HOME") or os.path.expanduser("~")


def _resolve_plugin_root() -> str:
    """PLUGIN_ROOT resolution — see module docstring. `CLAUDE_PLUGIN_ROOT` env var
    wins immediately (matches the oracle's own rung 1); everything past it
    (COORDINATOR_ROOT, registry live_path, versioned cache, .doe-root pointer,
    flat-layout manifest) is delegated to the shared native port of
    `resolve-coordinator-clone.sh --content-root`
    (`coordinator_core.resolve_coordinator_clone.resolve_content_root`). Returns
    "" on total resolution failure — caller treats this as the oracle's own
    fail-loud ERROR path.
    """
    env_override = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_override:
        return env_override

    try:
        return rcc.resolve_content_root()
    except rcc.ResolveCoordinatorCloneError:
        return ""


def _iter_scan_files(plugin_root: str, bundled_wiki: str) -> List[str]:
    """Enumerate files the oracle's `grep -rhoE ... --include='*.md' --include='*.sh'`
    would have scanned: the 4 top-level files (read directly, any extension —
    --include only gates recursion), the 6 scan-dirs recursed for *.md/*.sh,
    and BUNDLED_WIKI itself recursed the same way."""
    files: List[str] = []

    for name in _TOP_LEVEL_FILES:
        path = os.path.join(plugin_root, name)
        if os.path.isfile(path):
            files.append(path)

    scan_roots = [os.path.join(plugin_root, d) for d in _SCAN_DIRS] + [bundled_wiki]
    for root in scan_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                if fname.endswith(".md") or fname.endswith(".sh"):
                    if fname.endswith(".test.js"):
                        continue
                    files.append(os.path.join(dirpath, fname))

    return files


def _discover_wiki_names(plugin_root: str, bundled_wiki: str) -> "tuple[List[str], List[str]]":
    """Port of the oracle's grep+sed+sort+placeholder-filter pipeline.

    Returns (wiki_names, unreadable_files). A file that cannot be opened is a
    hole in the scan, not a clean miss -- it could be exactly the file citing
    the mirror/orphan doc this tool exists to catch, so the caller must treat
    a non-empty unreadable_files as scan-incomplete rather than folding it
    silently into "no citations found here"."""
    names = set()
    unreadable: List[str] = []
    for path in _iter_scan_files(plugin_root, bundled_wiki):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            unreadable.append(path)
            # --- end Tier 2 ---
            continue
        for match in _WIKI_CITATION_RE.findall(text):
            name = match[len("docs/wiki/") : -len(".md")]
            names.add(name)

    filtered = sorted(n for n in names if n not in _PLACEHOLDER_NAMES)
    return filtered, unreadable


def _stat_mtime(path: str) -> str:
    """Shell out to `stat` for a byte-identical mtime string to the oracle's
    `stat -c "%y" ... || stat -f "%Sm" ... || echo "unknown"` dual-form."""
    for args in (["-c", "%y", path], ["-f", "%Sm", path]):
        try:
            proc = subprocess.run(
                ["stat", *args],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                return (proc.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            # Mirrors the oracle's `stat -c ... || stat -f ... || echo "unknown"`
            # dual-form fallback -- try the next form.
            continue
    return "unknown"


def main(argv: List[str]) -> int:
    quiet = False
    for arg in argv:
        if arg == "--check-only":
            continue
        elif arg == "--quiet":
            quiet = True
        elif arg in ("-h", "--help"):
            print(_HELP_TEXT, end="")
            return 0
        else:
            print(f"unknown flag: {arg}", file=sys.stderr)
            return 2

    def log(*parts: str) -> None:
        if not quiet:
            print(*parts)

    plugin_root = _resolve_plugin_root()
    if not plugin_root:
        print(
            "ERROR: ~/.claude/.doe-root missing/invalid — re-run coordinator:install",
            file=sys.stderr,
        )
        return 1

    dev_wiki = os.path.join(_home_dir(), ".claude", "docs", "wiki")
    bundled_wiki = os.path.join(plugin_root, "docs", "wiki")

    names, unreadable_files = _discover_wiki_names(plugin_root, bundled_wiki)

    if unreadable_files:
        log("")
        log(
            f"UNREADABLE: {len(unreadable_files)} file(s) could not be read during "
            "wiki-name discovery -- scan is incomplete and may have missed a "
            "docs/wiki/ citation."
        )
        for path in unreadable_files:
            log(f"  {path}")

    if not names:
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        if unreadable_files:
            log(
                "No docs/wiki/ references found among readable files, but the "
                "scan was incomplete (see UNREADABLE above) -- cannot confirm "
                "the single-tree invariant holds."
            )
            return 1
        # --- end Tier 2 ---
        log("No docs/wiki/ references found in plugin files. Nothing to validate.")
        return 0

    mirror_count = 0
    mirror_files: List[str] = []
    missing_bundled = 0
    validated = 0

    override_active = os.environ.get("COORDINATOR_OVERRIDE_WIKI_MIRROR", "0") == "1"

    for name in names:
        dev = os.path.join(dev_wiki, f"{name}.md")
        bundled = os.path.join(bundled_wiki, f"{name}.md")

        if os.path.isfile(dev):
            mirror_count += 1
            mirror_files.append(f"{name}.md")
            dev_mtime = _stat_mtime(dev)
            bundled_mtime = _stat_mtime(bundled) if os.path.isfile(bundled) else "(not present)"

            if override_active:
                log(
                    f"WARN (override): dev-side mirror exists for {name}.md — "
                    "single-tree invariant suppressed by COORDINATOR_OVERRIDE_WIKI_MIRROR=1"
                )
                log(f"  dev-side mirror: {dev} (mtime: {dev_mtime})")
                log(f"  bundled:         {bundled} (mtime: {bundled_mtime})")
            else:
                log(f"MIRROR DETECTED: {name}.md — dev-side mirror breaks the single-tree invariant")
                log(f"  dev-side mirror: {dev}")
                log(f"  bundled (canonical): {bundled}")
                log(f"  dev-side mtime:  {dev_mtime}")
                log(f"  bundled mtime:   {bundled_mtime}")
                log(
                    "  Remediation: bundled is canonical; if dev-side mirror contains "
                    "intentional content, port it to bundled and delete dev-side."
                )
            continue

        if not os.path.isfile(bundled):
            log(f"WARN: {name}.md referenced by plugin file but absent from bundled wiki ({bundled})")
            missing_bundled += 1
            continue

        validated += 1

    if mirror_count > 0 and not override_active:
        log("")
        log(f"SINGLE-TREE INVARIANT BROKEN: {mirror_count} plugin-doctrine wiki(s) have dev-side mirrors.")
        log(f"Files: {' '.join(mirror_files)}")
        log("Doctrine: plugin-doctrine wikis live ONLY at <plugin>/docs/wiki/<name>.md.")
        log("See docs/plans/2026-05-15-plugin-wiki-write-direction-trap.md § Phase 4.")
        return 5

    if missing_bundled > 0:
        log(f"Note: {missing_bundled} referenced wiki(s) missing from bundled tree. doc-link-checker handles broken links separately.")

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    if unreadable_files:
        log(
            f"Plugin-bundled wiki: INCOMPLETE -- {len(unreadable_files)} file(s) "
            f"unreadable during wiki-name discovery ({validated} validated, "
            f"{missing_bundled} missing-bundled warnings among readable files)."
        )
        return 1
    # --- end Tier 2 ---

    log(f"Plugin-bundled wiki: clean ({validated} validated, {missing_bundled} missing-bundled warnings).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
