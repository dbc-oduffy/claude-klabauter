"""
bin/claude-klabauter-revendor-handoff-schema.py — repeatable handoff-schema re-vendor.

Purpose: fail-closed, repeatable re-vendor of example-doctrine-repo's active-handoff schema pair
(`handoff.schema.json` + `handoff-archived.schema.json`) into claude-klabauter's vendored
path at coordinator_core/frontmatter/schemas/.

MECHANISM RELOCATED (2026-07-28). Everything this script used to implement inline
now lives in `bin/claude-klabauter-revendor-schema.py`, the general named-schema re-vendor;
this file is the fixed-schema-set convenience entrypoint over it. The generalization
was forced by a real defect: the `claude-klabauter.schema.vendor_drift` doctor probe's
remediation told operators to `cp` example-doctrine-repo's file in by hand, which satisfies the
ADVISORY drift check (against example-doctrine-repo HEAD) while breaking the GATING tamper-check
(against a per-schema pinned SHA) — see
state/audits/2026-07-28-windows-install-dogfood-friction.md § F3. The probe now
names the general script, and the general script re-vendors and re-pins together.

The handoff pair is HEAD-TRACKED, not pin-tracked: there is no `_QUEUE_SCHEMA_PINS`
entry for either file, so the "pin" is whatever example-doctrine-repo HEAD resolves to at run-time,
exactly as `check_schema_drift` compares against example-doctrine-repo HEAD at test-time. The general
script discovers that classification from the pin registry itself rather than being
told, so this entrypoint's behavior is unchanged by the relocation.

Contract (unchanged, now enforced by the shared mechanism):
  - Fail-closed if the example-doctrine-repo clone is absent or `git show` fails (never silently no-ops).
  - Byte-for-byte overwrite (no reformatting) — mirrors the byte-identity contract
    the drift test enforces (`.prettierignore` guards it upstream).
  - Idempotent: re-running when already in sync makes no changes and exits 0.
  - Post-vendor verify via check_schema_drift() (the same function the test suite
    calls) — the vendor is not done until the drift check itself is green, and a
    failed verify rolls the tree back rather than leaving it half-applied.
  - example-doctrine-repo clone resolved via resolve_doe_clone() (machine-local registry), with a
    stale-local-clone warning when the clone is behind its upstream.

Usage:
    python3 bin/claude-klabauter-revendor-handoff-schema.py [--doe-clone <path>] [--dry-run]

    For any OTHER vendored schema — and for anything pin-tracked — use the general
    entrypoint directly:
        python3 bin/claude-klabauter-revendor-schema.py <name> [--reason ...] [--dry-run]

Spec backlink: docs/problems/2026-07-08-handoff-schema-revendor-investigation.md
    § Consider — durable re-vendor tooling
Precedent mirrored: bin/claude-klabauter-revendor-cockpit-contract.py
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BIN_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The general entrypoint's filename carries hyphens, so it is loaded by path rather
# than imported by name — the same importlib pattern bin/tests/ already uses to load
# these scripts. Loading it (instead of re-implementing two of its steps) is the
# point of this file: one mechanism, two entrypoints.
_GENERAL_SCRIPT = _BIN_DIR / "claude-klabauter-revendor-schema.py"


def _load_general():
    """Load bin/claude-klabauter-revendor-schema.py as a module. Fails loud if it is missing."""
    spec = importlib.util.spec_from_file_location(
        "_claude_klabauter_revendor_schema", _GENERAL_SCRIPT
    )
    if spec is None or spec.loader is None:
        print(
            f"ERROR: cannot load the general re-vendor mechanism at {_GENERAL_SCRIPT}.",
            file=sys.stderr,
        )
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    # Registered in sys.modules BEFORE exec_module: @dataclass resolves its own
    # class's __module__ through sys.modules, and an unregistered module makes that
    # lookup return None mid-decoration.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_SCHEMA_NAMES = ("handoff", "handoff-archived")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="claude-klabauter-revendor-handoff-schema.py",
        description=(
            "Re-vendor example-doctrine-repo HEAD's handoff.schema.json + handoff-archived.schema.json "
            "into claude-klabauter's vendored path. Fail-closed, byte-identical, idempotent, "
            "verified via check_schema_drift(). Thin entrypoint over "
            "bin/claude-klabauter-revendor-schema.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Standard re-vendor from example-doctrine-repo HEAD:\n"
            "  python3 bin/claude-klabauter-revendor-handoff-schema.py\n\n"
            "  # Dry-run — report what would change without writing anything:\n"
            "  python3 bin/claude-klabauter-revendor-handoff-schema.py --dry-run\n\n"
            "  # Override the example-doctrine-repo clone path:\n"
            "  python3 bin/claude-klabauter-revendor-handoff-schema.py --doe-clone /path/to/example-doctrine-repo\n\n"
            "  # Any other vendored schema (incl. every pin-tracked one):\n"
            "  python3 bin/claude-klabauter-revendor-schema.py <name> --dry-run\n"
        ),
    )
    p.add_argument(
        "--doe-clone",
        metavar="PATH",
        help=(
            "Override the example-doctrine-repo clone path. Default: resolve via machine-local registry "
            "(repos.example_doctrine_repo in registry.local.toml / registry.toml). "
            "Reuses resolve_doe_clone() — never re-implements registry parsing."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report which vendored files would change (if any) without writing "
            "anything. Resolves the example-doctrine-repo clone and diffs bytes, but never touches disk."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    general = _load_general()
    return general.run(
        schema_names=list(_SCHEMA_NAMES),
        doe_clone_arg=args.doe_clone,
        ref="HEAD",
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
