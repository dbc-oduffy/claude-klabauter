"""
coordinator_core.ops.verify_dist_publish_repo_sync — byte-identity check
between Claude Central dist/ source-of-truth and the coordinator-claude
publish repo, for the two flat-mirror targets (toplevel + docs).

ONE-WAY check only. Source (coordinator/dist/publish-repo-{toplevel,docs}/)
IS the truth; the publish repo is derivative. There is no --fix mode — the
fix for an out-of-sync publish repo is always: python coordinator/bin/publish.py <target>.

Why no --fix mode? Authority cite: plugin-extraction-and-distribution.md
§ Persona-Name Guard on Percolation — "publish.sh is the authority for
percolation — manual cp is wrong." A script-driven copy would bypass
depersonalize hooks and content-leakage scans that run as post-rsync hooks
in the publish.sh pipeline.

.percolate-ignore semantics (each flat-mirror target): files listed in a
target's dist/.percolate-ignore are NOT verified — they are publish-repo-
owned infrastructure (CLAUDE.md, .gitignore, etc.) that Claude Central
deliberately does not author.

Output format (one line per file, to stdout):
    OK        <rel-path>   — byte-identical between source and publish repo
    MISMATCH  <rel-path>   — content differs
    MISSING   <rel-path>   — file absent in publish repo (new source file not yet published)
    IGNORED   <rel-path>   — excluded by .percolate-ignore

Exit codes (parity-critical — business codes, matching the bash oracle
byte-for-byte):
    0 — all verified files are byte-identical
    1 — one or more MISMATCH or MISSING files detected
    2 — source dir(s)/publish-repo dir(s) not found, OR
        COORDINATOR_CLAUDE_PUBLISH_REPO/machine-local resolution failed
        (environment problem)

Transport-failure exit code (trampoline-only, NOT returned by main() here):
    3 — reserved by the DoE-side trampoline for an engine-root
        resolution / import failure (never collides with this module's own
        0/1/2 business codes).

Requires CLAUDE_PLUGIN_ROOT to be set in the environment — this module has
no way to derive "the DoE coordinator/bin directory" on its own (it does not
live in that tree). The trampoline sets this before calling main() (env var
already set wins; else the trampoline derives it from its own file location,
mirroring the bash oracle's `dirname "$0"/..` fallback). A bare `python -m
coordinator_core.ops.verify_dist_publish_repo_sync` invocation without
CLAUDE_PLUGIN_ROOT set fails loud with exit 2, matching the "environment
problem" bucket the bash oracle itself used for missing source dirs.

Port of: verify-dist-publish-repo-sync.sh (DoE b5a4192c, 2026-07-20).
Spec backlink: docs/plans/2026-05-21-back-percolate-publish-repo-orphans.md
§ Chunk 4 (DoE-claude repo) and docs/plans/2026-06-30-registry-publish-vs-working-targets.md § C8 (DoE-claude repo).

Negative-spec (faithfully reproduced from the .sh original):
    - Only maxdepth-1 (non-recursive) file scan of each dist/ target — mirrors
      the bash oracle's `find "$DIST_*" -maxdepth 1 -type f`.
    - MISMATCH is a byte diff only — no path-rewrite-aware comparison. The
      oracle's own stderr note (reproduced verbatim in the drift-detected
      block) documents that publish-time-transform.sh rewrites dev-tree
      plugin paths at publish time, so some MISMATCH lines are expected
      false-positives a human must reconcile by hand — this module does not
      attempt to auto-distinguish real drift from path-rewrite delta.
    - COUNT_CHECKED increments for both OK and MISMATCH (not just OK) — this
      is the corrected counter semantics per code-reviewer Slice-B (F4); the
      old COUNT_VERIFIED name/semantics (implying content-identical) was
      already fixed in the bash oracle before this port, not a fix made here.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core._settings_home import home_dir, settings_home
from coordinator_core.win_portability import is_executable, no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

_MACHINE_LOCAL_KEY = "publish.mirrors.coordinator_claude.path"
_SUBPROCESS_TIMEOUT_SECS = 15


def _load_percolate_ignore(path: Path) -> set:
    """Read a .percolate-ignore file into a set of relative-path strings.

    Skips blank lines and '#'-prefixed comment lines, mirroring the bash
    oracle's `case "$line" in ''|'#'*) continue ;; esac` filter. A Python
    set replaces the oracle's tempfile+grep-qxF workaround (that workaround
    existed only to emulate an associative array on bash 3.2 — an
    implementation detail, not observable behavior; set membership is the
    same outward semantics as `grep -qxF`).
    """
    ignored: set = set()
    if not path.is_file():
        return ignored
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line == "" or line.startswith("#"):
            continue
        ignored.add(line)
    return ignored


def _run_machine_local(binary: str, key: str) -> Optional[str]:
    """Run `<binary> get <key>` with a hard timeout and no stdin; return
    stripped stdout, or None on any failure (nonzero exit, timeout, empty
    output) — mirrors the bash oracle's `... 2>/dev/null || true` fail-soft
    shape, which the caller then re-checks for emptiness.
    """
    try:
        result = subprocess.run(
            [binary, "get", key],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _run_machine_local: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _resolve_publish_repo_root() -> Optional[str]:
    """Resolve PUBLISH_REPO_ROOT: env var -> machine-local (PATH) ->
    machine-local (settings-home, then legacy ~/.claude/bin) -> None (caller fails loud).

    Mirrors coordinator/bin/verify-dist-publish-repo-sync.sh's three-rung
    resolution ladder exactly, including which remediation text accompanies
    which failure rung.
    """
    env_val = os.environ.get("COORDINATOR_CLAUDE_PUBLISH_REPO")
    if env_val:
        return env_val

    path_binary = shutil.which("machine-local")
    if path_binary:
        value = _run_machine_local(path_binary, _MACHINE_LOCAL_KEY)
        if value:
            return value
        print(
            f"ERROR  cannot resolve publish repo root: machine-local key '{_MACHINE_LOCAL_KEY}' is unset",
            file=sys.stderr,
        )
        print("       Fix one of:", file=sys.stderr)
        print(
            "         (a) export COORDINATOR_CLAUDE_PUBLISH_REPO=/path/to/coordinator-claude",
            file=sys.stderr,
        )
        print(
            f"         (b) machine-local set {_MACHINE_LOCAL_KEY} /path/to/coordinator-claude",
            file=sys.stderr,
        )
        return None

    # Settings-home is the canonical install (DR-072); the legacy ~/.claude/bin
    # rung below it was retired 2026-07-28 and is kept only for machines that
    # predate the move. settings-home/bin is not on PATH by default, so without
    # this rung the ladder above reached only a directory that no longer exists.
    settings_binary = os.path.join(str(settings_home()), "bin", "machine-local")
    # home_dir() (CLAUDE_HOME, else Path.home()) rather than os.path.expanduser("~")
    # directly, for consistency with the other two resolvers on this same rung
    # — both resolve to the same Windows-safe (USERPROFILE-aware) value here.
    home_binary = os.path.join(str(home_dir()), ".claude", "bin", "machine-local")
    fallback_binary = next(
        (candidate for candidate in (settings_binary, home_binary) if is_executable(candidate)),
        None,
    )
    if fallback_binary:
        value = _run_machine_local(fallback_binary, _MACHINE_LOCAL_KEY)
        if value:
            return value
        print(
            f"ERROR  cannot resolve publish repo root: machine-local key '{_MACHINE_LOCAL_KEY}' is unset",
            file=sys.stderr,
        )
        print("       Fix one of:", file=sys.stderr)
        print(
            "         (a) export COORDINATOR_CLAUDE_PUBLISH_REPO=/path/to/coordinator-claude",
            file=sys.stderr,
        )
        print(
            f"         (b) {fallback_binary} set {_MACHINE_LOCAL_KEY} /path/to/coordinator-claude",
            file=sys.stderr,
        )
        return None

    print(
        "ERROR  cannot resolve publish repo root: COORDINATOR_CLAUDE_PUBLISH_REPO is unset and machine-local is not available",
        file=sys.stderr,
    )
    print("       Fix one of:", file=sys.stderr)
    print(
        "         (a) export COORDINATOR_CLAUDE_PUBLISH_REPO=/path/to/coordinator-claude",
        file=sys.stderr,
    )
    print(
        f"         (b) install machine-local and run: machine-local set {_MACHINE_LOCAL_KEY} /path/to/coordinator-claude",
        file=sys.stderr,
    )
    return None


class _Counters:
    def __init__(self) -> None:
        self.checked = 0
        self.mismatch = 0
        self.missing = 0


def _check_pair(src: Path, dst: Path, rel: str, counters: _Counters, out: List[str]) -> None:
    """Compare one source file against its publish-repo counterpart.

    Byte-identity check (not a content-aware diff) — mirrors the bash
    oracle's `diff -q "$src" "$dst"`. Uses filecmp.cmp(shallow=False) to
    force a full content comparison (shallow=True would trust os.stat
    metadata, which a publish.sh rsync run could touch without changing
    content, producing a false OK).
    """
    if not dst.is_file():
        out.append(f"{'MISSING':<10} {rel}")
        counters.missing += 1
        return

    if filecmp.cmp(str(src), str(dst), shallow=False):
        out.append(f"{'OK':<10} {rel}")
    else:
        out.append(f"{'MISMATCH':<10} {rel}")
        counters.mismatch += 1
    counters.checked += 1


def _scan_target(
    dist_dir: Path,
    pub_dir: Path,
    ignore_set: set,
    label_prefix: str,
    counters: _Counters,
    out: List[str],
) -> None:
    if not dist_dir.is_dir():
        return
    entries = sorted(p for p in dist_dir.iterdir() if p.is_file())
    for src_file in entries:
        rel = src_file.name
        if rel in ignore_set:
            out.append(f"{'IGNORED':<10} {label_prefix}/{rel}")
            continue
        dst_file = pub_dir / rel
        _check_pair(src_file, dst_file, f"{label_prefix}/{rel}", counters, out)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry: resolve roots, verify both flat-mirror targets, print
    report + summary, return the business exit code (0/1/2 — see module
    docstring; never returns anything else).
    """
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root_env:
        print(
            "verify-dist-publish-repo-sync.sh: CLAUDE_PLUGIN_ROOT is unset — "
            "cannot resolve the coordinator plugin root",
            file=sys.stderr,
        )
        return 2
    plugin_root = Path(plugin_root_env)

    publish_repo_root_str = _resolve_publish_repo_root()
    if not publish_repo_root_str:
        return 1

    dist_toplevel = plugin_root / "dist" / "publish-repo-toplevel"
    dist_docs = plugin_root / "dist" / "publish-repo-docs"
    pub_toplevel = Path(publish_repo_root_str)
    pub_docs = pub_toplevel / "docs"

    missing_dirs = False
    if not dist_toplevel.is_dir():
        print(f"ERROR  source dir not found: {dist_toplevel}", file=sys.stderr)
        missing_dirs = True
    if not dist_docs.is_dir():
        print(f"ERROR  source dir not found: {dist_docs}", file=sys.stderr)
        missing_dirs = True
    if not pub_toplevel.is_dir():
        print(f"ERROR  publish repo root not found: {pub_toplevel}", file=sys.stderr)
        print("       Is COORDINATOR_CLAUDE_PUBLISH_REPO set correctly?", file=sys.stderr)
        missing_dirs = True
    if not pub_docs.is_dir():
        print(f"ERROR  publish repo docs dir not found: {pub_docs}", file=sys.stderr)
        print("       Is COORDINATOR_CLAUDE_PUBLISH_REPO set correctly?", file=sys.stderr)
        missing_dirs = True
    if missing_dirs:
        return 2

    toplevel_ignore = _load_percolate_ignore(dist_toplevel / ".percolate-ignore")
    docs_ignore = _load_percolate_ignore(dist_docs / ".percolate-ignore")

    counters = _Counters()
    out: List[str] = []

    out.append("--- target: coordinator-claude-publish-repo-toplevel ---")
    _scan_target(dist_toplevel, pub_toplevel, toplevel_ignore, "toplevel", counters, out)

    out.append("")
    out.append("--- target: coordinator-claude-publish-repo-docs ---")
    _scan_target(dist_docs, pub_docs, docs_ignore, "docs", counters, out)

    out.append("")
    out.append(f"checked: {counters.checked}, mismatched: {counters.mismatch}, missing: {counters.missing}")

    print("\n".join(out))

    if counters.mismatch > 0 or counters.missing > 0:
        print("")
        print("DRIFT DETECTED — run: python coordinator/bin/publish.py <target>")
        print("  Do NOT copy files manually. publish.py runs depersonalize hooks")
        print("  and content-leakage scans that a manual cp bypasses.")
        print("")
        print("  Note on MISMATCH false-positives:")
        print("    publish-time-transform.sh rewrites dev-tree plugin paths to")
        print("    publish-tree form at publish time (e.g. plugins/coordinator/")
        print("    → plugins/coordinator/). Files whose source contains such paths will")
        print("    report MISMATCH by design — the publish-tree version is the intended")
        print("    OSS-facing shape. To distinguish real drift from path-rewrite delta:")
        print("    diff the file by hand against the source, then mentally apply the")
        print("    path-rewrite table. Real drift is content changes beyond the rewrite.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
