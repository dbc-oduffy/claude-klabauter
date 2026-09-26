"""coordinator_core.snippet_sync.verify_registry_consistency — registry-toml
consistency verifier.

**2026-07-22 — four-script leg retired.** This module was originally a
byte-parity port of verify-snippet-registry-consistency.sh
(DoE 93887f6f, 2026-07-17, bash, 721 LoC), which cross-checked
`snippets/registry.toml` against 4
HARDCODED-shape `coordinator/bin/verify-<X>-sync.sh` scripts
(reviewer-calibration, docs-checker-consumption,
plan-coverage-check-consumption, prior-art-check-consumption). Those four
scripts are retired everywhere — deleted from DoE's `coordinator/bin` at
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

`snippet_sync.registry` is THE schema authority this module reads through.
Every per-row rule — supported `schema_version` set, required fields
(including `delivery` from v3 onward), `consumers` element-typing,
`conditional_consumer` shape, the v4 `excluded_consumer` / `eligible_glob`
pair, and the enumerated-axis values — is validated by `registry.load_registry`
and `registry.get_snippet_meta`, with the filesystem completeness check by
`registry.eligible_glob_gaps`. Nothing here re-derives a rule.

NEGATIVE SPEC — do not reintroduce a local schema gate. This module once
carried its own `schema_version in ("1", "2")` test while `registry.py`
already read 1-4. DoE's registry went to v4 on 2026-08-03 and this verifier
refused every run (rc=3) for six weeks, checking nothing. Two readers of one
schema is the defect; a widened local tuple would only make the dead guard
silent instead of loud. The version set, and every field rule behind it,
comes from `registry.py` or from nowhere.

The top-level file-exists / parse / missing-`schema_version` handling stays
local because those three carry this CLI's own exit codes (2), which differ
from `registry.RegistryError`'s.

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
Consumed by DoE-side `coordinator/bin/verify-snippet-registry-consistency`
polyglot trampoline.

Spec backlinks:
  - DoE docs/plans/2026-06-15-snippet-sync-consumer-registry.md § Dispatch Ledger C4, C8
  - DoE docs/decisions/2026-06-15-snippet-registry-shape.md § Schema amendments — the Staff Engineer C2
  - cross-repo/archive/2026-07-22-claude-central-em-snippet-registry-consistency-fix-locus.md
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coordinator_core.snippet_sync import registry as _registry

# Ordered list for deterministic output — byte-parity with the bash SNIPPET_NAMES array.
SNIPPET_NAMES: list[str] = [
    "reviewer-calibration",
    "docs-checker-consumption",
    "plan-coverage-check-consumption",
    "prior-art-check-consumption",
]


@dataclass
class ConsistencyOutcome:

    exit_code: int
    lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)


class ConsistencyError(Exception):
    """Raised on a top-level (pre-loop) failure: missing registry.toml or
    parse failure. `exit_code` mirrors the bash CLI's contract (2 — missing
    file/parse-error [see module negative-spec for the missing-schema_version
    quirk]; 1 — malformed row, surfaced from `registry.RegistryError`;
    3 — schema_version value outside `registry._SUPPORTED_SCHEMA_VERSIONS`).
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
        raise ConsistencyError(
            f"registry.toml parse failed: {type(exc).__name__}: {exc}", exit_code=2
        ) from exc
    except Exception as exc:
        raise ConsistencyError(
            f"registry.toml parse failed: {type(exc).__name__}: {exc}", exit_code=2
        ) from exc


_SUPPORTED_VERSIONS_TEXT = ",".join(str(v) for v in _registry._SUPPORTED_SCHEMA_VERSIONS)


def _read_registry(registry_toml: Path) -> dict[str, Any]:
    if not registry_toml.is_file():
        raise ConsistencyError(f"registry.toml not found at {registry_toml}", exit_code=2)

    data = _load_toml(registry_toml)

    schema_version = data.get("schema_version")
    if schema_version is None:
        raise ConsistencyError("schema_version field missing from registry.toml", exit_code=2)
    if schema_version not in _registry._SUPPORTED_SCHEMA_VERSIONS:
        raise ConsistencyError(
            f"unknown schema_version (supports {_SUPPORTED_VERSIONS_TEXT}, "
            f"got {schema_version!r})",
            exit_code=3,
        )

    try:
        return _registry.load_registry(registry_toml)
    except _registry.RegistryError as exc:
        raise ConsistencyError(str(exc), exit_code=exc.exit_code) from exc


def _check_rows(data: dict[str, Any], plugin_root: Path) -> list[str]:
    fails: list[str] = []
    for name in _registry.list_snippets(data):
        try:
            _registry.get_snippet_meta(data, name)
        except _registry.RegistryError as exc:
            fails.append(f"FAIL [fields] {name}: {exc}")
            continue
        for gap in _registry.eligible_glob_gaps(data, name, plugin_root):
            fails.append(
                f"FAIL [eligible_glob] {name}: '{gap}' matches the row's eligible_glob but "
                f"appears in neither 'consumers' nor an excluded_consumer entry"
            )
    return fails


def list_checks() -> list[str]:
    out = [
        f"check:schema_version — registry.toml schema_version ∈ {{{_SUPPORTED_VERSIONS_TEXT}}} "
        f"(exit 3 on unknown/higher version)",
        "check:registry_exists — registry.toml exists on disk",
        "check:row_fields — per row: required fields (delivery REQUIRED from schema_version 3), "
        "consumers typing, conditional_consumer shape, and the schema_version-4 "
        "excluded_consumer / eligible_glob pair (both FORBIDDEN on a consumer_source=\"scan\" row, "
        "both rejected below v4)",
        "check:row_axis_values — per row: delivery/header_style/consumer_source/search_scope "
        "against their enumerated values",
        "check:eligible_glob_complete — per row declaring eligible_glob: every glob member lands "
        "in consumers or excluded_consumer",
    ]
    for name in SNIPPET_NAMES:
        out.append(f"check:enrollment[{name}] — snippet enrolled as [snippet.{name}] in registry.toml")
    return out


def run(plugin_root: Path) -> ConsistencyOutcome:
    registry_toml = plugin_root / "snippets" / "registry.toml"

    data = _read_registry(registry_toml)
    enrolled = set(data.get("snippet", {}).keys())

    overall_exit = 0
    stderr_lines: list[str] = []

    for name in SNIPPET_NAMES:
        if name not in enrolled:
            stderr_lines.append(f"FAIL [enrollment] {name}: snippet not enrolled in registry.toml")
            overall_exit = 1

    row_fails = _check_rows(data, plugin_root)
    stderr_lines.extend(row_fails)
    if row_fails:
        overall_exit = 1

    lines: list[str] = []
    if overall_exit == 0:
        lines.append(
            f"OK: registry.toml is consistent (schema_version {data['schema_version']}; "
            f"{len(enrolled)} rows field-checked; all {len(SNIPPET_NAMES)} enrolled snippets present)"
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
    f"  3 — schema_version present but unsupported (not one of {_SUPPORTED_VERSIONS_TEXT})\n"
)


def main(argv: list[str]) -> int:
    """CLI entrypoint. `argv[0]` MUST be the resolved plugin_root (absolute
    path string) — injected by the DoE trampoline, which computes it exactly
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
