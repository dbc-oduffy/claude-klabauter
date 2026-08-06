"""
coordinator_core.ops.central_run_due

Port of: central-run-due.sh (example-doctrine-repo b5a4192c, 2026-07-20).

Purpose: surface a "central learn-lessons run due (by VOLUME)" nudge when [universal]
entries accrued since the last COMPLETE central run exceed a threshold. Companion to
the date-based lesson-triage-recheck marker: a fixed cadence over-runs in quiet weeks
and under-runs in busy ones. This bounds the floor ADAPTIVELY. Read-only; surfaces,
never dispatches. Surfaced by `/workday-start` Step 1.75.

Cutoff = last central run that wrote a COMPLETE sentinel (learn-lessons Phase 8). An
in-progress/aborted run (no sentinel) is correctly ignored.

Output: a single `CENTRAL_RUN_DUE` line on stdout when over threshold; an informational
under-threshold / no-baseline line on stderr otherwise. Exit 0 always (it is a nudge,
not a gate) — this module NEVER raises out of main(); every failure path is a stderr
skip line + return 0, faithfully reproducing the bash oracle's `errexit`-omitted,
fail-open shape.

Companion-script resolution: the bash oracle located its example-doctrine-repo-resident sibling scripts
(coordinator-state-root.sh, resolve-coordinator-clone.sh, learn-lessons-roots.sh,
extract-lessons.py) via its OWN on-disk location (BASH_SOURCE[0]-relative), since it
lived inside coordinator/bin/ itself. Ported into the claude-klabauter engine tree, that anchor is
gone — this module re-derives the example-doctrine-repo coordinator content root via `_resolve_doe_content_root()`
(env override → `~/.claude/.doe-root` pointer → machine-local registry → the SAME
unconditional flat-layout fallback the oracle used) for the one remaining subprocess
boundary (`extract-lessons.py`, already-Python, invoked via `sys.executable` — a
subprocess-boundary reuse, not a bash bridge, out of C11's bash-retirement scope).

Bash bridges retired (C11, 2026-07-21): `coordinator_state_root_central` (shared
helper in `coordinator_core.state_root`, since centralized out of this module's own
copy) now calls the native `coordinator_core.state_root.coordinator_state_root(
central=True)` peer in-process instead of shelling to `coordinator-state-root.sh
--central`.
`_learn_lessons_roots` now calls the native `coordinator_core.ops.learn_lessons_roots`
peer's `resolve_roots()` in-process instead of shelling to `learn-lessons-roots.sh`
(that peer is authored and ported native in this same repo/wave — a reuse, not a
re-derivation). The bash-executable-presence skip-gate this second bridge used to need
("roots helper not executable — skipping") is removed: it existed only to guard against
the bash script's absence, a failure mode the native in-process call cannot have.

Negative-spec (faithfully reproduced from the bash oracle — do NOT "fix" mid-port):
    - `CONFIG` is built from `coordinator_state_root --central`'s stdout UNCONDITIONALLY,
      even when that call fails (non-empty stderr, empty stdout) — the oracle never
      checked its exit code before string-concatenating the config path. A resolution
      failure silently degrades to a bogus path (e.g. "/learn-lessons-config.md"), which
      then correctly fails the `os.path.isfile` check below and skips. Reproduced here,
      not corrected.
    - An invalid `--threshold` positional arg is reported and skipped (exit 0), not a
      hard error.
    - `_count_universals` folds any extraction failure or missing record_count into 0,
      per the oracle's `|| continue` / missing-record_count → 0 fallback.

Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import List, Optional

from coordinator_core.ops import learn_lessons_roots as _learn_lessons_roots_mod
from coordinator_core.state_root import coordinator_state_root_central
from coordinator_core.doe_root_pointer import read_doe_root_pointer_file
from coordinator_core.win_portability import is_executable

# Review: code-reviewer — module-level alias (not a re-derived duplicate) so this
# module's own tests can keep monkeypatching a local name; the actual
# implementation now lives once in coordinator_core.state_root, shared with
# coordinator_core.ops.learn_lessons_roots (previously two independently
# hand-duplicated copies of the same 3-line wrapper).
_coordinator_state_root_central = coordinator_state_root_central

_DEFAULT_THRESHOLD = 150
_CUTOFF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SUBPROCESS_TIMEOUT_SECS = 15


def _claude_home() -> str:
    """Mirror the bash oracle's `CLAUDE_HOME="${CLAUDE_HOME:-$HOME}/.claude"`.

    Note the oracle's own naming: the env var CLAUDE_HOME, when set, overrides
    $HOME (not the full .claude path) — reproduced verbatim, not "fixed".
    """
    base = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~")
    return os.path.join(base, ".claude")


def _resolve_doe_content_root(claude_home: str) -> str:
    """Resolve the example-doctrine-repo coordinator content root (coordinator/bin, coordinator/lib).

    Rungs (best-effort, non-fatal — mirrors the bash oracle's `2>/dev/null || true`
    sourcing of resolve-coordinator-clone.sh, plus its own unconditional final
    fallback when COORDINATOR_CONTENT_ROOT comes back empty):
      1. COORDINATOR_ROOT / CLAUDE_PLUGIN_ROOT env override.
      2. `~/.claude/.doe-root` pointer file → `<doe-root>/coordinator`.
      3. machine-local registry `plugin.mirrors.coordinator-claude.live_path`.
      4. `<claude_home>/plugins/coordinator-claude/coordinator` (unconditional
         fallback — used as-is even if it does not exist, matching the oracle).
    """
    override = os.environ.get("COORDINATOR_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if override:
        return override

    doe_root = read_doe_root_pointer_file(os.path.expanduser("~"))
    if doe_root:
        candidate = os.path.join(doe_root, "coordinator")
        if os.path.isdir(candidate):
            return candidate

    machine_local = os.path.join(claude_home, "bin", "machine-local")
    if os.path.isfile(machine_local) and is_executable(machine_local):
        try:
            proc = subprocess.run(
                [machine_local, "get", "plugin.mirrors.coordinator-claude.live_path"],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            candidate = (proc.stdout or "").strip()
            if proc.returncode == 0 and candidate and os.path.isdir(candidate):
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            print(f"skip: _resolve_doe_content_root: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass

    return os.path.join(claude_home, "plugins", "coordinator-claude", "coordinator")


def _resolve_threshold(argv: List[str], config_path: str) -> Optional[int]:
    """Threshold: arg > config (`central_volume_threshold: N`) > default 150.

    Returns None (caller prints the invalid-threshold skip line) when a positional
    arg is present but not a bare non-negative integer — mirrors the oracle's
    `[[ "$THRESHOLD" =~ ^[0-9]+$ ]]` gate.
    """
    threshold_str = argv[0] if argv else ""
    if not threshold_str and os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        m = re.search(r"central_volume_threshold:\s*(\d+)", text)
        if m:
            threshold_str = m.group(1)

    if not threshold_str:
        return _DEFAULT_THRESHOLD

    if not re.fullmatch(r"\d+", threshold_str):
        return None

    return int(threshold_str)


def _find_cutoff(claude_home: str) -> str:
    """Last COMPLETE central run (dirs sort lexically == chronologically for YYYY-MM-DD)."""
    tasks_dir = os.path.join(claude_home, "tasks")
    cutoff = ""
    try:
        entries = sorted(os.listdir(tasks_dir))
    except OSError:
        print(f"skip: _find_cutoff: entries = sorted(os.listdir(tasks_dir)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    for name in entries:
        if not name.startswith("learn-lessons-20"):
            continue
        d = os.path.join(tasks_dir, name)
        if not os.path.isdir(d):
            continue
        if os.path.isfile(os.path.join(d, "COMPLETE")):
            cutoff = name[len("learn-lessons-"):]
    return cutoff


def _learn_lessons_roots() -> List[str]:
    """In-process call to the native `coordinator_core.ops.learn_lessons_roots`
    peer, which is authored (and ported native) in this same repo/wave.

    Retired bash bridge (C11, 2026-07-21): previously shelled out to the
    example-doctrine-repo-resident `learn-lessons-roots.sh`. That script's own native Python port
    now lives at `coordinator_core.ops.learn_lessons_roots` -- calling its public
    `resolve_roots()` in-process is a reuse of the already-ported peer, not a
    re-derivation, and needed no separate bash bridge or subprocess spawn.
    """
    return _learn_lessons_roots_mod.resolve_roots()


def _count_universals(extract_script: str, lessons_path: str, cutoff: str) -> int:
    """Shell out to the example-doctrine-repo-resident `extract-lessons.py` (already Python — invoked as
    a subprocess, not imported, matching the oracle's own subprocess-boundary shape and
    avoiding a direct example-doctrine-repo→claude-klabauter Python import edge). Returns 0 on any failure, matching
    the oracle's `|| continue` / missing-record_count → 0 fallback.
    """
    try:
        proc = subprocess.run(
            [
                sys.executable,
                extract_script,
                "extract",
                lessons_path,
                "--shortname",
                "vol",
                "--since",
                cutoff,
                "--require-tag",
                "universal",
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _count_universals: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0
    if proc.returncode != 0:
        return 0
    out = proc.stdout or ""
    m = re.search(r"#\s*record_count:\s*(\d+)", out)
    if not m:
        return 0
    return int(m.group(1))


def main(argv: List[str]) -> int:
    claude_home = _claude_home()
    doe_content_root = _resolve_doe_content_root(claude_home)

    config_path = os.path.join(
        _coordinator_state_root_central(), "learn-lessons-config.md"
    )
    extract_script = os.path.join(doe_content_root, "bin", "extract-lessons.py")

    threshold = _resolve_threshold(argv, config_path)
    if threshold is None:
        raw = argv[0] if argv else ""
        print(f"central-run-due: invalid threshold '{raw}' — skipping", file=sys.stderr)
        return 0

    if not os.path.isfile(config_path):
        print(f"central-run-due: no config at {config_path} — skipping", file=sys.stderr)
        return 0
    if not os.path.isfile(extract_script):
        print(f"central-run-due: extractor missing at {extract_script} — skipping", file=sys.stderr)
        return 0

    cutoff = _find_cutoff(claude_home)
    if not cutoff:
        print(
            "central-run-due: no COMPLETE central-run sentinel found — skipping volume check",
            file=sys.stderr,
        )
        return 0
    if not _CUTOFF_RE.match(cutoff):
        print(f"central-run-due: unrecognised cutoff '{cutoff}' — skipping", file=sys.stderr)
        return 0

    home_norm = claude_home.replace("\\", "/").lower()

    total = 0
    detail: List[str] = []
    for root in _learn_lessons_roots():
        if not root:
            continue
        root_norm = root.replace("\\", "/").lower()
        if root_norm == home_norm or root_norm.endswith("/.claude"):
            continue  # self-exclude (~/.claude is the promotion destination)
        lessons = os.path.join(root, "state", "lessons.md")
        if not os.path.isfile(lessons):
            continue
        n = _count_universals(extract_script, lessons, cutoff)
        total += n
        if n > 0:
            detail.append(f"{os.path.basename(root)}:{n}")

    if total >= threshold:
        breakdown = f" Breakdown: {' '.join(detail)}" if detail else ""
        print(
            f"CENTRAL_RUN_DUE volume: {total} [universal] entries accrued since last "
            f"central run ({cutoff}) >= threshold {threshold}.{breakdown}. "
            "Consider /learn-lessons central."
        )
    else:
        print(
            f"central-run-due: {total}/{threshold} universals since {cutoff} — below threshold",
            file=sys.stderr,
        )
    return 0
