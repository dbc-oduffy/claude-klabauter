"""coordinator_core.snippet_sync.verify_registry_consistency — registry-toml
consistency verifier.

**2026-07-22 — four-script leg retired.** This module was originally a
byte-parity port of verify-snippet-registry-consistency.sh
(coordinator-claude 93887f6f, 2026-07-17, bash, 721 LoC), which cross-checked
`snippets/registry.toml` against 4
HARDCODED-shape `coordinator/bin/verify-<X>-sync.sh` scripts
(reviewer-calibration, docs-checker-consumption,
plan-coverage-check-consumption, prior-art-check-consumption). Those four
scripts are retired everywhere — deleted from coordinator-claude's `coordinator/bin` at
`b644d5a9`, never migrated here — their function was consolidated into the
registry-driven `verify-snippet-sync` / `snippet-registry` entrypoints. The
hardcoded `VERIFY_SCRIPTS` existence gate this module carried was therefore
hard-failing (exit 2) on every full run against a correct tree. Retired per
the actioned inbound memo
`cross-repo/archive/2026-07-22-claude-central-em-snippet-registry-consistency-fix-locus.md`
(claude-central-em verified `registry.toml` itself carries zero non-comment
references to the retired scripts — the fix-locus was entirely this
module's substrate, not registry content).

Consumer-set parity (registry.toml vs. each snippet's actual consumers) now
lives in the registry-driven `verify-snippet-sync` / `snippet-registry`
entrypoints, not here. What remains meaningful here is registry.toml's own
internal consistency: it parses, declares a supported schema_version, and
enrolls all 4 known snippet names as `[snippet.<name>]` tables.

This module intentionally does NOT reuse `snippet_sync.registry.load_registry`
for the top-level TOML read — that reader performs stricter per-snippet
field validation (`sentinel_begin`/`sentinel_end` presence, `consumers`
element-typing) than this verifier needs. Parses via `tomllib`/`tomli`
directly (the same underlying library `registry.py` uses, not a re-derived
regex parser).

Negative-spec (faithful oracle bug, kept intentionally): the retired bash
oracle's own header docs a 3-way exit-code table where schema_version
problems exit 3. In the actual bash, a **missing** schema_version field was
caught by a generic "any ERROR-prefixed parser output line" early-exit block
that ran BEFORE the dedicated schema_version-value check — so a missing
field yielded exit 2, not the documented 3. Only a *present-but-unsupported*
schema_version value (e.g. `99`) reached the dedicated check and exited 3.
This asymmetry is reproduced here verbatim (see `_read_registry`/
`ConsistencyError` exit codes) — do not "fix" it to exit 3 uniformly without
a cross-repo doc update, since callers may already depend on the exit-2
shape.

Op registered? NO — plain module, direct import (template-variant #1, see
`docs/plans/2026-07-16-clean-slate-recon/r1-doe-port-template.md` § 1).
Consumed by coordinator-claude-side `coordinator/bin/verify-snippet-registry-consistency`
polyglot trampoline.

Spec backlinks:
  - coordinator-claude docs/plans/2026-06-15-snippet-sync-consumer-registry.md § Dispatch Ledger C4, C8
  - coordinator-claude docs/decisions/2026-06-15-snippet-registry-shape.md § Schema amendments — the Staff Engineer C2
  - cross-repo/archive/2026-07-22-claude-central-em-snippet-registry-consistency-fix-locus.md
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# Ordered list for deterministic output — byte-parity with the bash SNIPPET_NAMES array.
SNIPPET_NAMES: list[str] = [
    "reviewer-calibration",
    "docs-checker-consumption",
    "plan-coverage-check-consumption",
    "prior-art-check-consumption",
]


@dataclass
class ConsistencyOutcome:
    """Return value of `run()` — mirrors the bash script's stdout-lines + exit-code contract."""

    exit_code: int
    lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)


class ConsistencyError(Exception):
    """Raised on a top-level (pre-loop) failure: missing registry.toml or
    parse failure. `exit_code` mirrors the bash CLI's contract (2 — missing
    file/parse-error [see module negative-spec for the missing-schema_version
    quirk]; 3 — unsupported schema_version value).
    """

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _load_toml(registry_path: Path) -> dict:
    try:
        if sys.version_info >= (3, 11):
            import tomllib

            with registry_path.open("rb") as fh:
                return tomllib.load(fh)
        else:
            import tomli  # type: ignore[import-not-found]

            with registry_path.open("rb") as fh:
                return tomli.load(fh)
    except ImportError as exc:
        # Byte-parity: the oracle's Python heredoc catches ImportError under
        # its blanket `except Exception as e: print(f"ERROR {e}")` — any
        # parser-emitted ERROR line hits the generic early-exit-2 block.
        raise ConsistencyError(
            f"registry.toml parse failed: {type(exc).__name__}: {exc}", exit_code=2
        ) from exc
    except Exception as exc:  # tomllib.TOMLDecodeError et al.
        raise ConsistencyError(
            f"registry.toml parse failed: {type(exc).__name__}: {exc}", exit_code=2
        ) from exc


def _read_registry(registry_toml: Path) -> set[str]:
    """Parse registry.toml, enforce the schema_version gate, and return the
    set of enrolled `[snippet.<name>]` table names."""
    if not registry_toml.is_file():
        raise ConsistencyError(f"registry.toml not found at {registry_toml}", exit_code=2)

    data = _load_toml(registry_toml)

    schema_version = data.get("schema_version")
    if schema_version is None:
        # Byte-parity oracle bug (see module negative-spec): a missing
        # schema_version is an "ERROR ..." parser-output line, caught by the
        # generic early-exit-2 block BEFORE the dedicated exit-3 check below
        # ever runs. Exit 2, not 3.
        raise ConsistencyError("schema_version field missing from registry.toml", exit_code=2)
    if str(schema_version) not in ("1", "2"):
        raise ConsistencyError(
            f"unknown schema_version (supports up to 2, got {schema_version!r})", exit_code=3
        )

    return set(data.get("snippet", {}).keys())


def list_checks() -> list[str]:
    """`--list` mode: one line per check, in execution order."""
    out = [
        "check:schema_version — registry.toml schema_version ∈ {1,2} (exit 3 on unknown/higher version)",
        "check:registry_exists — registry.toml exists on disk",
    ]
    for name in SNIPPET_NAMES:
        out.append(f"check:enrollment[{name}] — snippet enrolled as [snippet.{name}] in registry.toml")
    return out


def run(plugin_root: Path) -> ConsistencyOutcome:
    """Run all checks against `plugin_root` (the coordinator-claude coordinator plugin root
    — the directory containing `snippets/`)."""
    registry_toml = plugin_root / "snippets" / "registry.toml"

    enrolled = _read_registry(registry_toml)

    overall_exit = 0
    stderr_lines: list[str] = []

    for name in SNIPPET_NAMES:
        if name not in enrolled:
            stderr_lines.append(f"FAIL [enrollment] {name}: snippet not enrolled in registry.toml")
            overall_exit = 1

    lines: list[str] = []
    if overall_exit == 0:
        lines.append(
            "OK: registry.toml is consistent (schema_version valid; all 4 enrolled snippets present)"
        )

    return ConsistencyOutcome(exit_code=overall_exit, lines=lines, stderr_lines=stderr_lines)


_USAGE = (
    "Usage: verify-snippet-registry-consistency [--list]\n"
    "  (no args)  Run all checks. Exit 0 on success.\n"
    "  --list     Print one line per check in execution order.\n"
    "\n"
    "Exit codes:\n"
    "  0 — all checks pass\n"
    "  1 — consistency violation (printed to stderr)\n"
    "  2 — missing dep or file not found (ALSO: missing schema_version — see\n"
    "      module negative-spec, a faithfully-reproduced oracle quirk)\n"
    "  3 — schema_version present but unsupported (not 1 or 2)\n"
)


def main(argv: list[str]) -> int:
    """CLI entrypoint. `argv[0]` MUST be the resolved plugin_root (absolute
    path string) — injected by the coordinator-claude trampoline, which computes it exactly
    as the bash oracle did (CLAUDE_PLUGIN_ROOT env var, else its own
    script-directory-relative fallback). `argv[1:]` are the original
    user-facing CLI args (`[]` or `["--list"]`).
    """
    if not argv:
        print("verify-snippet-registry-consistency: internal error: plugin_root not supplied", file=sys.stderr)
        return 2

    plugin_root = Path(argv[0])
    user_args = argv[1:]

    if user_args and user_args[0] in ("--help", "-h"):
        sys.stdout.write(_USAGE)
        return 0

    if user_args and user_args[0] == "--list":
        for line in list_checks():
            print(line)
        return 0

    if user_args:
        print(f"ERROR: unknown argument '{user_args[0]}'", file=sys.stderr)
        print(f"Usage: verify-snippet-registry-consistency [--list]", file=sys.stderr)
        return 2

    try:
        outcome = run(plugin_root)
    except ConsistencyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code

    for line in outcome.stderr_lines:
        print(line, file=sys.stderr)
    for line in outcome.lines:
        print(line)
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
