"""
coordinator_core.ops.register_coordinator_mirror — DOE-PORT, variant #1 — pristine, no
registered op.

Purpose: idempotently register the coordinator plugin in
`registry.local.toml::plugin.mirrors.coordinator-claude`. The coordinator plugin's live
install IS the canonical source (~/.claude/) — no inward propagation step is needed.
Registering this structural fact lets bin/check-plugin-drift.sh surface it as
`n/a-by-design` rather than treating it as an unchecked entry.

Spec: docs/plans/2026-05-21-plugin-source-live-mirror-doctrine.md § Chunk 5 / AC-7
Port of: register-coordinator-mirror.sh (example-doctrine-repo 6fb5fb37, 2026-07-22)

Division of labor (DR-047 — example-doctrine-repo owns contract, claude-klabauter owns engine): the example-doctrine-repo-side
trampoline resolves the example-doctrine-repo-local "coordinator live path" fact (via
`resolve-coordinator-clone.sh --for-content`, with a `claude-home plugins` fallback —
both example-doctrine-repo-specific path computations this engine-tree module has no business
duplicating) and threads it in via `--live-path`; this module owns only the
idempotent atomic TOML-section write, which is pure and portable. The registry path
itself (`registry.local.toml` under the machine-local settings-home dir) IS resolved
natively here via `coordinator_core._settings_home.machine_local_dir()` — the bash
oracle shelled out to the `claude-home machine-local` CLI to get the identical value;
this module reuses the bootstrap-safe pure resolver the CLI itself is built on instead
of re-spawning a subprocess for a value obtainable in-process (confirmed byte-identical
output during the port's parity check).

Negative-spec:
    - Does NOT resolve the coordinator live path itself — that is a example-doctrine-repo-side concern
      (script-relative resolver lookup + claude-home-plugins fallback) threaded in via
      `--live-path`. A caller that omits `--live-path` gets a fail-loud usage error, not
      a silently-wrong registration (the bash oracle's fallback lived in the .sh; the
      analogous fallback for a DIRECT caller of this module — no example-doctrine-repo trampoline in the
      loop — is out of scope for a pristine, unregistered op).
    - Does NOT reimplement a TOML parser — plain substring section-header detection
      only, matching the bash oracle's `section_header in existing` check exactly (a
      real TOML parse was never in scope upstream either).
    - Reproduces the bash oracle's `--check-only` arg-parsing looseness verbatim: any
      argv token other than the recognized flags is silently ignored, not rejected
      (the oracle only ever inspected its literal `$1` for the `--check-only` string
      and dropped everything else on the floor — this is not a bug to "fix" mid-port).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core._settings_home import machine_local_dir

_PROG = "register-coordinator-mirror.sh"  # literal program-name prefix, matches the example-doctrine-repo filename
_SECTION_HEADER = "[plugin.mirrors.coordinator-claude]"


def _toml_escape(s: str) -> str:
    """Escape per TOML spec for basic strings (double-quoted).

    Order matters: backslash must be first to avoid double-escaping.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    s = s.replace("\t", "\\t")
    s = s.replace("\r", "\\r")
    return s


def _section_body(live_path: str) -> str:
    return (
        "\n"
        f"{_SECTION_HEADER}\n"
        "# Live install IS canonical source — registered automatically by /coordinator:install\n"
        "# Drift probe and refresh script treat this as n/a-by-design; no git/venv legs to check.\n"
        'propagation_mode = "source_is_live"\n'
        f'live_path = "{_toml_escape(live_path)}"\n'
    )


def resolve_registry_path() -> Path:
    """The registry.local.toml path — mirrors `claude-home machine-local`'s output
    (verified byte-identical against the live CLI during the port's parity check)."""
    return machine_local_dir() / "registry.local.toml"


def register(reg_path: Path, live_path: str, check_only: bool) -> int:
    """Idempotently write the `[plugin.mirrors.coordinator-claude]` TOML section.

    check_only=True: report readiness without mutating (prints one of two lines,
    exactly mirroring the bash oracle's `--check-only` branch).
    """
    existing = reg_path.read_text(encoding="utf-8") if reg_path.exists() else ""

    if check_only:
        if _SECTION_HEADER in existing:
            print("coordinator_plugin_mirrors: ready")
            return 0
        print(f"coordinator_plugin_mirrors: check failed: {_SECTION_HEADER!r} absent from {reg_path} (would write)")
        return 1

    if _SECTION_HEADER in existing:
        print("plugin.mirrors.coordinator-claude already registered — skipping.")
        return 0

    reg_path.parent.mkdir(parents=True, exist_ok=True)
    if not existing:
        existing = "schema = 1\n"

    new_text = existing.rstrip("\n") + "\n" + _section_body(live_path)
    # Use string concatenation rather than .with_suffix() — multi-dot suffixes raise
    # ValueError on Python <3.12 (CPython relaxed this in 3.12); matches the bash
    # oracle's own inline-python comment verbatim.
    tmp = reg_path.parent / (reg_path.name + f".tmp.{os.getpid()}")
    tmp.write_text(new_text, encoding="utf-8")
    if reg_path.exists():
        try:
            tmp.chmod(reg_path.stat().st_mode)
        except OSError:
            print(f"skip: register: tmp.chmod(reg_path.stat().st_mode) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
    os.replace(tmp, reg_path)
    print(
        "Coordinator plugin registered (source_is_live mode). "
        "Drift probe will skip it as n/a-by-design."
    )
    return 0


def main(argv: List[str]) -> int:
    """CLI entry: arg parse (`--check-only`, `--live-path <value>`), resolve, write."""
    check_only = False
    live_path: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--check-only":
            check_only = True
        elif arg == "--live-path":
            i += 1
            if i >= len(argv):
                print(f"{_PROG}: --live-path requires a value", file=sys.stderr)
                return 1
            live_path = argv[i]
        elif arg.startswith("--live-path="):
            live_path = arg.split("=", 1)[1]
        # else: silently ignored — matches the bash oracle's own looseness (it only
        # ever inspected argv[1] for the literal "--check-only" string).
        i += 1

    if live_path is None:
        print(f"{_PROG}: --live-path is required", file=sys.stderr)
        return 1

    reg_path = resolve_registry_path()
    return register(reg_path, live_path, check_only)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
