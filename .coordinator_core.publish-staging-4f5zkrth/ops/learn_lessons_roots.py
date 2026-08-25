"""
coordinator_core.ops.learn_lessons_roots — Port of: learn-lessons-roots.sh
(DoE a1a568d2, 2026-07-22).

Purpose: emit every on-disk repo root that learn-lessons should process ON THIS
MACHINE, one per line, in a stable de-duplicated order.

Contract (mirrors the bash oracle's own header verbatim):
  1. $CLAUDE_HOME (default $HOME/.claude) is always the first line -- the meta-repo
     is always a lessons source, unconditionally, before any registry call.
  2. Each machine-local repos.* key that resolves to an existing directory is
     included, EXCLUDING publish-target/mirror repos.
  3. Optional supplemental roots from the BEGIN/END learn-lessons-roots sentinel in
     <central-state>/learn-lessons-config.md are appended (empty by default).
  4. The output list is de-duplicated; every emitted line is an existing directory.
  5. Exit 0 always -- graceful degradation if machine-local is absent or returns
     nothing (OSS fresh-install: emits exactly $CLAUDE_HOME).

Companion-resolver call (C11, 2026-07-21 -- bash bridge retired): the shared
`coordinator_core.state_root.coordinator_state_root_central()` helper (centralized
out of this module's and `central_run_due`'s formerly-duplicated private copies)
previously re-derived the DoE coordinator content root and shelled out to the
DoE-resident `coordinator-state-root --central` sourced-lib. It now calls the
native `coordinator_core.state_root.coordinator_state_root(central=True)` peer
in-process (Rule 4 of that module's 5-rule routing -- no subject/artifact given,
so it resolves to `<coordinator_makima_root()>/state`, the same default the bash
`--central` form produced), folding `StateRootError` to `""` to preserve the
oracle's unconditional-concat-then-skip contract (see negative-spec below).

Public API: `resolve_roots()` returns the de-duplicated root list in-process (no
stdout parsing) for native callers, e.g. `coordinator_core.ops.central_run_due`,
that previously shelled out to this module's own bash oracle predecessor and
now import this module directly instead.

Negative-spec (faithfully reproduced from the bash oracle -- do NOT "fix" mid-port):
    - The publish-target denylist fallback only checks the REGISTRY KEYS
      `repos.coordinator_claude` / `repos.deep_research_claude`. These keys were
      retired by the 2026-06-30-registry-publish-vs-working-targets.md migration in
      favor of `publish.mirrors.<name>.path` -- the oracle's own co-located test
      (T4, now `coordinator/tests/test_learn_lessons_roots.py`) resolves via the
      migrated key precisely BECAUSE it knows this denylist fallback no longer does.
      Kept verbatim here for byte-for-byte parity; a follow-up port is a separate item.
    - `_config` (the supplemental-roots sentinel path) is built from
      `coordinator_state_root --central`'s stdout UNCONDITIONALLY, even when that
      call fails (non-empty stderr, empty/garbage stdout) -- the oracle never checked
      the resolver's exit code before string-concatenating the config path. A
      resolution failure degrades to a bogus path (e.g. "/learn-lessons-config.md"),
      which then correctly fails the `os.path.isfile` check below and is skipped.
    - Registry-repo resolution and the supplemental-sentinel read are both
      best-effort / skip-on-any-failure -- this module NEVER raises out of main();
      every failure path degrades to "skip this source", per the oracle's
      `2>/dev/null || true` shape throughout.

Spec backlink: docs/plans/2026-06-19-portability-tracked-per-machine-config.md § C1
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
OSS requirement: works for any coordinator-claude installer with zero registered repos.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Dict, List, Optional

from coordinator_core.state_root import coordinator_state_root_central
from coordinator_core.win_portability import is_executable, no_console_creationflags

# Review: code-reviewer — module-level alias (not a re-derived duplicate) so this
# module's own tests can keep monkeypatching a local name; the actual
# implementation now lives once in coordinator_core.state_root, shared with
# coordinator_core.ops.central_run_due (previously two independently
# hand-duplicated copies of the same 3-line wrapper).
_coordinator_state_root_central = coordinator_state_root_central

_SUBPROCESS_TIMEOUT_SECS = 15
_DENYLIST_KEYS = ("coordinator_claude", "deep_research_claude")
_BEGIN_SENTINEL = "BEGIN learn-lessons-roots"
_END_SENTINEL = "END learn-lessons-roots"


def _claude_home() -> str:
    """Mirror the bash oracle's `CLAUDE_HOME="${CLAUDE_HOME:-$HOME}/.claude"`.

    Note the oracle's own naming: the env var CLAUDE_HOME, when set, overrides
    $HOME (not the full .claude path) -- reproduced verbatim, not "fixed".
    """
    base = (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )
    return os.path.join(base, ".claude")


def _machine_local_run(machine_local: str, *args: str) -> str:
    """Run `machine-local <args>` and return raw stdout, "" on any failure --
    mirrors the bash oracle's `"$MACHINE_LOCAL" ... 2>/dev/null || true` skip-absent
    contract (stderr discarded, non-zero exit folds to empty rather than raising)."""
    try:
        proc = subprocess.run(
            [machine_local, *args],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _machine_local_run: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    return proc.stdout or ""


def _publish_target_dests(machine_local: str) -> List[str]:
    """`machine-local get publish.targets` lines are `name|type|source|dest` -- keep
    field 4 (dest) for any line with >=4 pipe-separated fields. Mirrors:
    `awk -F'|' 'NF>=4{print $4}'`."""
    out = _machine_local_run(machine_local, "get", "publish.targets")
    dests: List[str] = []
    for line in out.splitlines():
        fields = line.split("|")
        if len(fields) >= 4:
            dests.append(fields[3])
    return dests


def _is_publish_target(
    candidate: str, pub_dests: List[str], machine_local: str, ml_ok: bool
) -> bool:
    if candidate in pub_dests:
        return True
    if ml_ok:
        for key in _DENYLIST_KEYS:
            deny_path = _machine_local_run(machine_local, "get", f"repos.{key}").strip()
            if deny_path and deny_path == candidate:
                return True
    return False


def _machine_local_dump_repos(machine_local: str) -> Dict[str, str]:
    """Resolve every `repos.*` key in ONE `dump --prefix repos --format json`
    call — the batch counterpart to `_registry_roots`' former enumerate-then-
    `get` loop (one `keys` spawn plus one `get` spawn per key). Same
    primitive already proven in `coordinator/bin/coordinator-doc-new.py` and
    `coordinator/bin/lib/cli_shared.py::machine_local_dump_repos`.

    Fails closed to {} on any spawn failure, empty stdout, unparseable JSON,
    OR a non-zero returncode — a non-zero exit with parseable stdout is a
    partial/crashed dump, not a value to trust (this is the fixed shape;
    an earlier revision of the sibling helpers above trusted parseable
    stdout regardless of returncode, which a code review caught as a silent
    partial-table read). Callers already tolerate an empty/partial roots
    list — this degrades exactly like "no machine-local" does.
    """
    try:
        proc = subprocess.run(
            [machine_local, "dump", "--prefix", "repos", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _machine_local_dump_repos: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v}


def _registry_roots(machine_local: str) -> List[str]:
    """Resolve every `repos.*` key via ONE batched dump call, skip-absent,
    minus publish targets. See `_machine_local_dump_repos` for the batching
    rationale (T3 h4-ops-b deferred item)."""
    roots: List[str] = []
    pub_dests = _publish_target_dests(machine_local)
    for resolved in _machine_local_dump_repos(machine_local).values():
        resolved = resolved.strip()
        if not resolved:
            continue
        if not os.path.isdir(resolved):
            continue
        if _is_publish_target(resolved, pub_dests, machine_local, ml_ok=True):
            continue
        roots.append(resolved)
    return roots


def _supplemental_roots(config_path: str) -> List[str]:
    """Roots between the BEGIN/END learn-lessons-roots sentinel, `- `-prefixed lines,
    existing dirs only. Mirrors:
    `sed -n '/BEGIN.../,/END.../p' "$_config" | grep -E '^- ' | sed 's/^- //'`."""
    if not os.path.isfile(config_path):
        return []
    try:
        with open(config_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        print(f"skip: _supplemental_roots: with open(config_path, encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []

    in_block = False
    roots: List[str] = []
    for line in lines:
        if _BEGIN_SENTINEL in line:
            in_block = True
            continue
        if _END_SENTINEL in line:
            in_block = False
            continue
        if not in_block:
            continue
        if line.startswith("- "):
            candidate = line[2:]
            if candidate and os.path.isdir(candidate):
                roots.append(candidate)
    return roots


def resolve_roots() -> List[str]:
    """Public in-process API: the de-duplicated root list, one entry per source
    per the module contract (§ header). Exposed for native callers (e.g.
    `coordinator_core.ops.central_run_due`) that previously shelled out to this
    module's bash oracle predecessor and now import it directly instead."""
    claude_home = _claude_home()
    machine_local = os.path.join(claude_home, "bin", "machine-local")
    if os.name == "nt":
        # The bare shim is EXTENSION-LESS, so CreateProcess cannot exec it
        # (WinError 193) — prefer the delivered .cmd sibling on Windows.
        # Mirrors coordinator_core.install._shared.resolve_machine_local_cli.
        cmd_sibling = machine_local + ".cmd"
        if os.path.isfile(cmd_sibling):
            machine_local = cmd_sibling
    ml_ok = os.path.isfile(machine_local) and is_executable(machine_local)

    roots: List[str] = [claude_home]
    if ml_ok:
        roots.extend(_registry_roots(machine_local))

    central_state_root = _coordinator_state_root_central()
    config_path = os.path.join(central_state_root, "learn-lessons-config.md")
    roots.extend(_supplemental_roots(config_path))

    seen: set = set()
    deduped: List[str] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            deduped.append(r)
    return deduped


def main(argv: Optional[List[str]] = None) -> int:
    deduped = resolve_roots()
    if deduped:
        sys.stdout.write("\n".join(deduped) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
