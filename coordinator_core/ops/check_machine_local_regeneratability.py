"""
coordinator_core.ops.check_machine_local_regeneratability — machine-local registry
regeneratability observer.

Purpose: POST-HOC OFFER (exit 0 always). Reads the ``[regeneratability]`` TOML table
from the machine-local registry and flags:
  (1) Any session-accumulated-must-survive-crash entry that lives ONLY in a gitignored
      ``*.local.toml`` with no tracked baseline or idempotent regenerator — this is an
      install-surface-completeness defect.
  (2) Any coordinator-owned key absent from the ``[regeneratability]`` table
      (unclassified-key warning).

Wired into: /workstream-complete Step 2.95 (`coordinator/skills/workstream-complete/SKILL.md`).

Port of: check-machine-local-regeneratability.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-06-22-invariant-verification-observers.md § C1
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Offer shape: exit 0 always; findings to stderr; silent on clean.

Exit codes (parity-critical — the example-doctrine-repo trampoline forwards this verbatim):
    0 — always. This is an offer-shaped observer; it never blocks a caller,
        regardless of parse errors, missing HOME, or missing machine-local CLI.

Negative-spec (do NOT "fix" while porting):
    - The bash oracle's ``COORDINATOR_OWNED_KEYS`` list is reproduced verbatim
      (13 keys, post the 2026-06-30 F5 reconciliation — no ``repos.coordinator_claude``,
      includes ``repos.example_league_data_repo`` / ``repos.example_cockpit_repo`` / ``repos.example-os-repo``).
      Do NOT "helpfully" resync this against the live registry.toml — any drift is a
      separate reconciliation change, not something this port should silently absorb.
    - The ``plugin.mirrors`` namespace-prefix match (Check 1) is intentionally the ONLY
      prefix-match case; every other coordinator-owned key requires an exact match. Do not
      generalize this into a glob for other keys — that was reviewed and rejected upstream
      (bash oracle code-review F2 removed a tautological ``repos.*`` prefix branch).
    - Bash read the ``[regeneratability]`` table and the flat top-level tables via a
      subprocess'd Python helper (mktemp'd heredoc) because bash cannot parse TOML
      natively. This port has no subprocess boundary for TOML parsing at all — it calls
      ``tomllib``/``tomli`` in-process. The ONLY subprocess retained is the
      ``machine-local get`` ladder probe (Check 2's repos.* skip), which is genuinely an
      external CLI call, not a TOML-parsing workaround.
    - The registry DIRECTORY resolves via the settings-home ladder (``_resolve_registry_dir``:
      ``MACHINE_LOCAL_REGISTRY_DIR`` override > ``<settings-home>/machine-local``), NOT the
      legacy ``${CLAUDE_HOME:-$HOME}/.claude/machine-local`` path — that legacy path caused
      every coordinator-owned key to false-WARN as unclassified on settings-home-native
      machines (the dir simply didn't exist there). ``--claude-dir``/``_resolve_claude_dir``
      is retained ONLY to locate the ``machine-local`` CLI *binary* for the Check 2 ladder
      probe — it no longer has anything to do with the registry directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core import _settings_home

_PROG = "check-machine-local-regeneratability"

# Coordinator-owned keys: the canonical set of keys expected classified in the
# [regeneratability] table. plugin.mirrors.* is a namespace (per-plugin, set via
# `machine-local set`); every other key requires an exact match.
COORDINATOR_OWNED_KEYS: List[str] = [
    "coordinator.python",
    "plugin.mirrors",
    "publish.targets",
    "repos.example-sim-repo",
    "repos.example_retrieval_repo",
    "repos.example_retrieval_repo_ue_addon",
    "repos.example_game_workbench_repo",
    "repos.example_repo",
    "repos.example_stats_repo",
    "repos.example_league_data_repo",
    "repos.experiments",
    "repos.example_cockpit_repo",
    "repos.example-os-repo",
]

USAGE = f"""\
usage: {_PROG} [--claude-dir PATH]

Machine-local registry regeneratability observer (offer-shaped, exit 0 always).
Reads the [regeneratability] TOML table from the machine-local registry and flags:
  (1) session-accumulated-must-survive-crash entries with no tracked baseline
  (2) coordinator-owned keys absent from the [regeneratability] table

  --claude-dir PATH   Override CLAUDE_HOME/HOME/USERPROFILE resolution (test seam).

Exit codes: 0 — always (post-hoc offer; findings go to stderr, never blocks).
"""


def _resolve_claude_dir() -> str:
    """Resolve claude home: CLAUDE_HOME > HOME > USERPROFILE. Empty string on failure.

    Used ONLY to locate the ``machine-local`` CLI binary (``<claude_dir>/bin/machine-local``)
    for the Check 2 ladder probe — NOT the registry directory (see ``_resolve_registry_dir``).
    """
    home_base = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not home_base:
        print("ERROR: Cannot resolve HOME for machine-local registry lookup", file=sys.stderr)
        return ""
    return f"{home_base}/.claude"


def _resolve_registry_dir() -> Path:
    """Resolve the machine-local registry directory via the canonical ladder.

    Mirrors _machine_local.py::_registry_dir() (the resolution `machine-local get`
    itself uses) so this checker reads the SAME registry the CLI probe in Check 2
    shells out to:
        1. MACHINE_LOCAL_REGISTRY_DIR env — explicit registry-dir override
           (test isolation + the 4 production readers named in _machine_local.py).
        2. <settings-home>/machine-local — via coordinator_core._settings_home,
           itself COORDINATOR_SETTINGS_HOME when set, else
           .coordinator-claude-settings under CLAUDE_HOME or the home directory.

    Pre-migration this read .claude/machine-local under CLAUDE_HOME or the home
    directory (the bug this
    closes): on a settings-home-native machine that path does not exist, so every
    coordinator-owned key was reported unclassified.
    """
    override = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
    if override:
        return Path(override)
    return _settings_home.machine_local_dir()


def _parse_toml_file(path: Path, emit_errors: bool) -> Optional[dict]:
    """Parse a TOML file in-process. Returns None (never raises) on any failure.

    emit_errors mirrors the bash oracle's mode-gated stderr behavior: the "regen"
    read (Check 1's table load) emits parse/import errors; the flat_str/flat_nondict
    reads (Check 2's tracked/local key inventories) stay silent on the same failures,
    exactly as the bash helper's `if mode == "regen":` gate did.
    """
    if not path.is_file():
        return None
    try:
        import tomllib as _tomllib  # type: ignore[import-not-found]
    except ImportError:
        try:
            import tomli as _tomllib  # type: ignore[no-redef]
        except ImportError:
            if emit_errors:
                print(
                    "ERROR: tomllib not available (Python 3.11+ required, or install tomli)",
                    file=sys.stderr,
                )
            return None
    try:
        with open(path, "rb") as f:
            return _tomllib.load(f)
    except Exception as exc:  # noqa: BLE001 — faithful catch-all, matches bash `|| true` swallow
        if emit_errors:
            print(f"ERROR: Failed to parse {path}: {exc}", file=sys.stderr)
        return None


def _regen_table_from_file(path: Path) -> Dict[str, str]:
    data = _parse_toml_file(path, emit_errors=True)
    if data is None:
        return {}
    regen = data.get("regeneratability", {})
    if not isinstance(regen, dict):
        return {}
    return {str(k): str(v) for k, v in regen.items()}


def _flat_str_from_file(path: Path) -> Dict[str, str]:
    data = _parse_toml_file(path, emit_errors=False)
    if data is None:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _flat_nondict_keys_from_file(path: Path) -> List[str]:
    data = _parse_toml_file(path, emit_errors=False)
    if data is None:
        return []
    return [k for k, v in data.items() if not isinstance(v, dict)]


def _tracked_toml_files(ml_dir: Path) -> List[Path]:
    if not ml_dir.is_dir():
        return []
    return sorted(f for f in ml_dir.glob("*.toml") if not f.name.endswith(".local.toml"))


def _ladder_resolves(machine_local_bin: Path, key: str) -> bool:
    """Probe the machine-local path-resolution ladder for a repos.* key.

    rc=0 means the ladder (autodiscovery or another rung) can derive the value on a
    fresh-machine clone — NOT an install-surface-completeness gap. A missing binary,
    timeout, or any subprocess error is treated as "does not resolve" (fail-safe: the
    key still gets evaluated by the normal tracked/local check below, matching the
    bash oracle's `if ... ; then continue; fi` — a non-zero/errored probe falls through).
    """
    if not machine_local_bin.is_file():
        return False
    try:
        result = subprocess.run(
            [str(machine_local_bin), "get", key],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _ladder_resolves: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False


def main(argv: List[str]) -> int:
    if argv and argv[0] in ("--help", "-h"):
        print(USAGE, end="")
        return 0

    claude_dir_override: Optional[str] = None
    idx = 0
    rest = list(argv)
    while idx < len(rest):
        if rest[idx] == "--claude-dir" and idx + 1 < len(rest):
            claude_dir_override = rest[idx + 1]
            idx += 2
            continue
        idx += 1

    claude_dir = claude_dir_override if claude_dir_override is not None else _resolve_claude_dir()
    if not claude_dir:
        print(
            f"{_PROG}: CLAUDE_DIR is empty (HOME/CLAUDE_HOME/USERPROFILE unresolved) — skipping checks",
            file=sys.stderr,
        )
        return 0

    ml_dir = _resolve_registry_dir()
    local_registry = ml_dir / "registry.local.toml"

    # ------------------------------------------------------------------
    # Collect ALL [regeneratability] table entries across tracked files
    # ------------------------------------------------------------------
    regen_table: Dict[str, str] = {}
    for f in _tracked_toml_files(ml_dir):
        regen_table.update(_regen_table_from_file(f))

    # ------------------------------------------------------------------
    # Check 1: Unclassified coordinator-owned keys
    # ------------------------------------------------------------------
    findings = 0

    for canon_key in COORDINATOR_OWNED_KEYS:
        found = False
        for regen_key in regen_table:
            if regen_key == canon_key or (
                canon_key == "plugin.mirrors" and regen_key.startswith("plugin.mirrors.")
            ):
                found = True
                break
        if not found:
            print(
                f"WARN [{_PROG}]: Key '{canon_key}' is coordinator-owned but absent from [regeneratability] table.",
                file=sys.stderr,
            )
            print(
                "     Offer: add an entry to the [regeneratability] table in registry.toml:",
                file=sys.stderr,
            )
            print(
                f'       "{canon_key}" = "idempotent-regeneratable|session-accumulated-must-survive-crash|ephemeral"',
                file=sys.stderr,
            )
            print(
                "     See docs/wiki/machine-local-registry.md § Regeneratability Classification for the three-value enum.",
                file=sys.stderr,
            )
            findings += 1

    # ------------------------------------------------------------------
    # Check 2: session-accumulated-must-survive-crash entries in gitignored
    # .local.toml only (no tracked baseline) — install-surface-completeness defect.
    # ------------------------------------------------------------------
    local_keys = _flat_str_from_file(local_registry)

    tracked_keys: set = set()
    for f in _tracked_toml_files(ml_dir):
        tracked_keys.update(_flat_nondict_keys_from_file(f))

    machine_local_bin = Path(claude_dir) / "bin" / "machine-local"
    if os.name == "nt":
        # The bare shim is EXTENSION-LESS, so CreateProcess cannot exec it
        # (WinError 193) — prefer the delivered .cmd sibling on Windows.
        # Mirrors coordinator_core.install._shared.resolve_machine_local_cli.
        cmd_sibling = machine_local_bin.with_suffix(".cmd")
        if cmd_sibling.is_file():
            machine_local_bin = cmd_sibling

    for key, val in regen_table.items():
        if val != "session-accumulated-must-survive-crash":
            continue

        in_tracked = key in tracked_keys
        in_local = local_keys.get(key)

        # For repos.* keys: probe the path-resolution ladder before flagging as a gap.
        # rc=0 from `machine-local get` means rung-2 autodiscovery (or another ladder
        # rung) can derive the value on a fresh-machine clone — NOT a manual-re-entry gap.
        if key.startswith("repos.") and _ladder_resolves(machine_local_bin, key):
            continue

        if not in_tracked and in_local is not None:
            print(
                f"WARN [{_PROG}]: '{key}' is classified session-accumulated-must-survive-crash",
                file=sys.stderr,
            )
            print(
                "     but has no declaration in any tracked registry file — only in gitignored registry.local.toml.",
                file=sys.stderr,
            )
            print(
                "     This is an install-surface-completeness defect: a fresh-machine clone will not have this value.",
                file=sys.stderr,
            )
            print(
                "     Offer: Add a tracked baseline declaration in registry.toml (empty-value is sufficient):",
                file=sys.stderr,
            )
            print(
                f'       machine-local set --global "{key}" ""  # then document the regeneration path',
                file=sys.stderr,
            )
            print(
                "     See docs/wiki/install-surface-completeness.md § Bootstrap gap: machine-local/ is not created",
                file=sys.stderr,
            )
            print("     by any current installer.", file=sys.stderr)
            findings += 1

    # Exit 0 always (offer-shaped, never blocking)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
