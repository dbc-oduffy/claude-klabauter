"""
coordinator_core.ops.list_reverse_drift_cmds — emit registered reverse-drift
commands for the /workweek-complete Step 4g merge gate.

Purpose: read plugin.mirrors from the machine-local registry and emit one
line per copy_install plugin that has a non-empty reverse_drift_cmd:

    <plugin_name>|<source_path>|<reverse_drift_cmd>

Consumed by /workweek-complete Step 4g, which cd's to <source_path> and runs
<reverse_drift_cmd> as a blocking merge gate.

Per-repo scoping (--scope-repo <repo-root>):
  Without --scope-repo, EVERY copy_install row is emitted (legacy behavior;
  preserved for direct callers and tests). Step 4g always passes its repo root
  so a CONSUMER repo's release never gates on a SIBLING plugin's live-install
  drift. The meta-repo (${HOME}/.claude, the coordinator home) is the explicit
  check-all case — releasing it covers every copy_install plugin on the
  machine. Any other repo emits only rows whose source_path IS that repo.
  Paths are normalized before comparison (Windows X:/ vs MSYS /x/ vs $HOME /c/)
  so the meta-repo and source_path matches survive cross-platform path forms.
  Spec backlink: cross-repo/inbox/2026-06-01-reverse-drift-gate-per-repo-scoping.md

Exit codes encode the difference between "gate is genuinely N/A" and "gate is
blind because of misconfiguration" — so Step 4g never silently passes when it
should be running but cannot:
  0  — emitted >=1 runnable row, OR no copy_install plugins exist at all (N/A).
  3  — copy_install plugins ARE registered but NONE carry a reverse_drift_cmd.
       The gate is structurally blind (the bug-equivalent state). Fail loud.
  2  — invocation/parse error.

Registry read delegates to coordinator_core.plugin_health.drift.read_merged_mirrors
(the already-ported Python home of the former coordinator/bin/lib/read-mirrors.sh
TOML parser — this module does NOT re-implement TOML parsing). Per-key precedence:
registry.local.toml wins per key, tracked registry.toml fills gaps — replaces the
first-FILE-wins read under which a plugin registered only in the tracked file was
invisible whenever a registry.local.toml existed.

Port of: list-reverse-drift-cmds.sh (coordinator-claude b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Spec backlink: docs/plans/2026-05-28-reverse-drift-gate-meta-repo-coverage.md §Chunk 3

Negative-spec:
  - Does NOT resolve reverse_drift_cmd shell-safety — the value is a raw
    string the CALLER (Step 4g) later shell-evaluates via `bash -c`. This
    module only warns (does not block) on a literal double-quote in the
    value, mirroring the bash oracle's advisory-only stance.
  - Does NOT filter --scope-repo by anything but source_path equality (after
    normalization) — a plugin whose live_path happens to match is NOT scoped
    in; only source_path is the scoping key (matches the bash oracle).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.plugin_health.drift import read_merged_mirrors

_PROG = "list-reverse-drift-cmds"

_USAGE = """\
Usage: list-reverse-drift-cmds [--scope-repo <repo-root>]
Emits "<plugin>|<source_path>|<reverse_drift_cmd>" per copy_install plugin with
a reverse_drift_cmd registered.
  --scope-repo <repo-root>  Restrict emission to the releasing repo. The meta-repo
                            (${HOME}/.claude) emits ALL rows (check-all); any other
                            repo emits only rows whose source_path is that repo.
                            Omitted → all rows (legacy / direct-caller behavior).
Exit: 0 = runnable rows emitted, scoped to none, or no copy_install plugins (N/A);
      3 = copy_install plugins exist but none have reverse_drift_cmd (misconfig);
      2 = error.
Environment: MACHINE_LOCAL_REGISTRY_DIR, HOME
"""


# ---------------------------------------------------------------------------
# Cross-platform path normalization — ported verbatim from the bash oracle's
# `_norm_path`. Windows drive `X:/`, `git rev-parse --show-toplevel`'s `C:/` form, and
# `$HOME`-derived MSYS `/c/` form must all converge for a match. Windows
# filesystems are case-insensitive -> the whole Windows-shaped path folds to
# lowercase; a genuine POSIX path (/Users/..., /home/...) stays case-preserved
# (code-reviewer F1-F4 fixes from the bash oracle are preserved here).
# ---------------------------------------------------------------------------

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MSYS_DRIVE_RE = re.compile(r"^/[A-Za-z]/")


def _norm_path(p: str, *, ostype: str = "") -> str:
    """Normalize a path for cross-platform comparison.

    `ostype` mirrors bash's ${OSTYPE:-} — the MSYS-drive-mount fold (branch 2)
    is gated on it exactly as the bash oracle gates on $OSTYPE, so callers on
    non-Windows hosts (empty/unset ostype) never fold a genuine POSIX
    single-letter top-level dir (e.g. /a/foo on Linux).
    """
    if _WINDOWS_DRIVE_RE.match(p):
        # Windows drive form. Separator normalized AFTER stripping "X:" —
        # never fold a backslash into a regex bracket expression (bash 3.2 ERE
        # undefined behavior; code-reviewer F1 — preserved as a design note,
        # not applicable to Python's re, but the STRIP-THEN-NORMALIZE order is
        # preserved for oracle fidelity).
        drive = p[0].lower()
        rest = p[2:]
        rest = rest.replace("\\", "/")  # backslashes -> forward
        if not rest.startswith("/"):
            rest = "/" + rest  # drive-relative X:foo -> /x/foo (F2)
        p = f"/{drive}{rest}".lower()  # Windows FS is case-insensitive -> fold whole path
    elif _MSYS_DRIVE_RE.match(p) and ostype.startswith(("msys", "cygwin", "win")):
        # MSYS drive-mount form (/x/Foo) under a Windows shell only (F3).
        p = p.lower()
    # Strip trailing separators LAST: a trailing backslash (X:\foo\) becomes a
    # trailing slash post-normalization and must not defeat the == compare (F4).
    while p.endswith("/") and p != "/":
        p = p[:-1]
    return p


# ---------------------------------------------------------------------------
# Registry dir resolution — mirrors the bash oracle's
# MACHINE_LOCAL_REGISTRY_DIR -> _coordinator_settings_home() precedence.
# ---------------------------------------------------------------------------


def _resolve_registry_dir() -> Path:
    override = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
    if override:
        return Path(override)
    from coordinator_core._settings_home import settings_home

    return settings_home() / "machine-local"


def _resolve_home() -> str:
    return os.environ.get("HOME") or str(Path.home())


# ---------------------------------------------------------------------------
# Core scan — used by both main() (CLI) and (future) an IPC op wrapper.
# ---------------------------------------------------------------------------


def _run(scope_repo: Optional[str]) -> Tuple[List[str], List[str], int]:
    """Returns (stdout_lines, stderr_lines, exit_code)."""
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    registry_dir = _resolve_registry_dir()
    # Per-key precedence (local wins per key, tracked fills gaps) — NOT
    # first-file-wins; see drift.read_merged_mirrors.
    registry_files = [
        p
        for p in (registry_dir / "registry.local.toml", registry_dir / "registry.toml")
        if p.is_file()
    ]

    # No registry -> nothing to emit (not an error; matches check-plugin-drift.py).
    if not registry_files:
        return stdout_lines, stderr_lines, 0

    try:
        mirrors = read_merged_mirrors(registry_files)
    except Exception as exc:  # noqa: BLE001 — mirrors bash's ERROR-and-exit-2
        stderr_lines.append(f"{_PROG}: failed to read registry: {exc}")
        return stdout_lines, stderr_lines, 2

    if not mirrors:
        return stdout_lines, stderr_lines, 0

    # Resolve scoping decision once.
    #   all  — emit every copy_install row (no --scope-repo, or scope == meta-repo).
    #   own  — emit only rows whose source_path == scope_norm.
    scope_mode = "all"
    scope_norm = ""
    ostype = os.environ.get("OSTYPE", "")
    if scope_repo:
        scope_norm = _norm_path(scope_repo, ostype=ostype)
        # Deliberately NOT `Path(_resolve_home()) / ".claude"` — on Windows,
        # pathlib treats a leading "/" as an absolute root and re-anchors it
        # with a native backslash, silently destroying an MSYS-style HOME
        # (e.g. "/c/Users/operator") before `_norm_path` ever sees it, so the
        # MSYS-drive-mount fold (_MSYS_DRIVE_RE) never fires and the meta-repo
        # comparison spuriously fails. Plain string concatenation preserves
        # HOME's original separator/drive form verbatim, exactly as the bash
        # oracle's `"${HOME}/.claude"` did.
        meta_norm = _norm_path(_resolve_home().rstrip("/\\") + "/.claude", ostype=ostype)
        if scope_norm != meta_norm:
            scope_mode = "own"

    copy_install_seen = False
    runnable_emitted = False

    for plugin_name, entry in mirrors.items():
        if not plugin_name:
            continue
        prop_mode = entry.get("propagation_mode", "")
        if prop_mode != "copy_install":
            continue

        source_path = entry.get("source_path", "")
        # Per-repo scoping BEFORE the misconfig counter: a plugin a consumer
        # repo does not source is not "this repo's gate" — it must not count
        # toward copy_install_seen, else a clean consumer no-op would falsely
        # trip the rc=3 "gate is blind" signal. The meta-repo (scope_mode=all)
        # checks every copy_install plugin.
        if scope_mode == "own" and _norm_path(source_path, ostype=ostype) != scope_norm:
            continue

        copy_install_seen = True
        reverse_drift_cmd = entry.get("reverse_drift_cmd", "")
        if not reverse_drift_cmd:
            continue

        # Advisory: the value is shell-evaluated once by `bash -c` in Step 4g,
        # and the documented convention is to single-quote it in
        # registry.local.toml. A literal double-quote almost always signals a
        # misconfiguration. Warn but do not block — exotic single-quoted
        # values are legitimate. Deliberately do NOT flag `$` ($(...) is a
        # valid idiom).
        if '"' in reverse_drift_cmd:
            stderr_lines.append(
                f"{_PROG}: WARNING: {plugin_name}.reverse_drift_cmd contains a double-quote "
                "— single-quote the value in registry.local.toml "
                "(see docs/wiki/machine-local-registry.md § reverse_drift_cmd)."
            )

        stdout_lines.append(f"{plugin_name}|{source_path}|{reverse_drift_cmd}")
        runnable_emitted = True

    # copy_install plugins exist but none carry a reverse_drift_cmd -> gate is blind.
    if copy_install_seen and not runnable_emitted:
        stderr_lines.append(
            f"{_PROG}: copy_install plugins are registered but none have a reverse_drift_cmd "
            "— the reverse-drift gate cannot run. Register with: "
            "bin/machine-local set plugin.mirrors.<name>.reverse_drift_cmd '<invocation>'. "
            "See docs/wiki/machine-local-registry.md § reverse_drift_cmd."
        )
        return stdout_lines, stderr_lines, 3

    # Either runnable rows were emitted, or there are no copy_install plugins (N/A).
    return stdout_lines, stderr_lines, 0


def main(argv: List[str]) -> int:
    scope_repo: Optional[str] = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            print(_USAGE)
            return 0
        if arg == "--scope-repo":
            if i + 1 >= len(argv) or not argv[i + 1]:
                print(f"{_PROG}: --scope-repo requires a path argument", file=sys.stderr)
                return 2
            scope_repo = argv[i + 1]
            i += 2
            continue
        print(f"{_PROG}: unknown argument '{arg}'", file=sys.stderr)
        return 2

    stdout_lines, stderr_lines, exit_code = _run(scope_repo)
    for line in stdout_lines:
        print(line)
    for line in stderr_lines:
        print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
