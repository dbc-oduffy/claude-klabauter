"""
coordinator_core.ops.validate_install_contract — packageability contract validator.

Purpose: reads a repo's agent-install-manifest.json and validates it against the
packageability contract (docs/wiki/agent-install-contract.md § Packageability) —
the per-point completeness rules that JSON-schema `required`/`type` shape alone
can't express: functional-probe present per system_prerequisites/direct_deps
entry, required_env_vars present-even-if-empty, entry-point contract properties
declared on standalone_setup_script/programmatic_entry_point, tested_platforms
declared, every configurable_locations[] entry carries discovery.candidates +
default + override.

COVERAGE (deliberate, not accidental): this module checks points 1/2/3/4/6.
Point 5 (stay-in-shape / no packageability regression over time) is
DELIBERATELY NOT a check here — it's a process invariant enforced by the
code-reviewer lens on every diff, not a one-shot manifest-completeness gate
this validator can express.

SCOPE (hard, do not widen): validates ONLY the manifest passed in (a repo's own
manifest, at that repo's own request). Not auto-wired into any cross-repo/
fleet-shared hook — wiring this into a hook that fires against a sibling repo's
not-yet-compliant manifest would regress that sibling.

OPT-IN GATE: completeness checks fire ONLY when the manifest carries
`packageability_compliance.declared === true` (type-strict — a schema-invalid
string `"true"` is NOT accepted as opt-in). A manifest that omits
`packageability_compliance` entirely, or sets `declared: false`, SKIPS CLEAN
(exit 0, "not opted in") rather than failing.

Port of: validate-install-contract.sh (coordinator-claude b5a4192c, 2026-07-20; 303 lines, bash + jq)
Spec backlink: docs/plans/2026-07-11-packageability-contract-fleet-doctrine.md § C4
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec (deliberate behavior differences from the bash+jq oracle):
    - No `jq` / `bash >= 4.3` runtime precondition: this is a pure-Python
      JSON-module port, so the oracle's bash-version guard and `command -v jq`
      guard are gone entirely — an implementation-precondition removal, not a
      dropped BUSINESS behavior (the packageability CHECKS themselves, points
      1/2/3/4/6, are unchanged byte-for-byte in outcome).
    - A malformed CLI invocation (`--manifest-path`/`--repo-root` given with no
      following value) is a hard usage error here (dedicated, readable stderr
      message, exit 1) — the bash oracle also exits 1 on this input (its
      `set -uo pipefail` nounset guard aborts on the unbound `$2`), so the
      EXIT CODE already matched without any deliberate change; only the
      stderr TEXT differs (a raw `line 68: $2: unbound variable` bash
      interpreter error vs. this module's `--manifest-path requires a value`),
      which is an improvement in message clarity on an already-1 exit code,
      not a business-outcome change.
    - A manifest that parses as valid JSON but is not a top-level JSON object
      (e.g. a bare array or scalar) is treated the SAME as the oracle: every
      dotted-path lookup on it comes back "absent", which lands on the
      opt-in-gate SKIP CLEAN branch (exit 0) — mirroring jq's own behavior of
      raising a runtime type error on `.foo` against a non-object, which
      `jq -e` reports as a nonzero/no-output failure and the bash `if !
      jq -e ...` treats identically to "not opted in".
    - `--help`/`-h` output gained an explicit "Exit codes:" section (the bash
      oracle's `-h`/`--help` text never documented its own exit codes) — pure
      additive documentation, not a behavior change; addendum rule 3 requires
      the Usage block to match `main()`'s actual returns, which the oracle's
      own help text did not attempt.
    - Exit-code contract (BUSINESS codes, this module's `main()` only):
        0 — compliant, OR not opted into the contract, OR no manifest declared
        1 — one or more packageability findings, OR a CLI usage error
      This 0/1 duality on rc=1 (CLI-usage-error sharing a code with
      business-fail) is INHERITED faithfully from the bash oracle; this port
      does not fix that overload, only declines to add a NEW third meaning
      onto it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List, Optional

_DEFAULT_MANIFEST_RELPATH = "coordinator/docs/install/agent-install-manifest.json"

_USAGE = """Usage: validate-install-contract.sh [--manifest-path <path>] [--repo-root <path>]

Validates THIS repo's own agent-install-manifest.json against the fleet
packageability contract. Invoke only against a repo's own manifest, at that
repo's own request — never auto-wire into a cross-repo/fleet-shared hook.

Exit codes:
  0 — compliant, OR not opted into the contract, OR no manifest declared
  1 — one or more packageability findings, OR a CLI usage error
"""


# ---------------------------------------------------------------------------
# jq-semantics helpers — mirror `jq -e <path>` / `jq -e 'has(...)'` / `//`
# truthiness exactly (jq -e fails ONLY on a last-output of `null` or `false`;
# 0, "", [], {} are all truthy) so the ported point-checks read the same
# manifest the same way the bash+jq oracle did.
# ---------------------------------------------------------------------------


def _get(obj: Any, *path: str) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _truthy(obj: Any, *path: str) -> bool:
    """Mirror `jq -e <path>`: fails only on missing/null/false."""
    val = _get(obj, *path)
    return val is not None and val is not False


def _has_key(obj: Any, key: str) -> bool:
    """Mirror `jq -e 'has(key)'`: true iff the key is PRESENT, regardless of value."""
    return isinstance(obj, dict) and key in obj


def _jq_or(val: Any, fallback: str) -> Any:
    """Mirror jq's `//` operator: fallback iff val is null or false."""
    return fallback if (val is None or val is False) else val


def _array_len_at(obj: Any, *path: str) -> int:
    val = _get(obj, *path)
    return len(val) if isinstance(val, list) else 0


class _Failures:
    """Accumulates FAIL findings, mirroring the bash `fail()` helper's stderr shape."""

    def __init__(self) -> None:
        self.count = 0

    def add(self, point: str, msg: str, fix: str) -> None:
        print(f"FAIL [{point}]: {msg}", file=sys.stderr)
        print(f"  Fix: {fix}", file=sys.stderr)
        self.count += 1


# ---------------------------------------------------------------------------
# Point checks
# ---------------------------------------------------------------------------


def _check_point1(manifest: dict, failures: _Failures) -> None:
    """Point 1: functional probes on direct_deps/system_prerequisites;
    required_env_vars present even when empty."""
    if not _has_key(manifest, "required_env_vars"):
        failures.add(
            "point-1",
            "required_env_vars is missing entirely",
            'add "required_env_vars": [] (or the real list) — the field must be '
            "PRESENT even when empty, not omitted.",
        )

    direct_deps = manifest.get("direct_deps")
    if isinstance(direct_deps, list):
        for i, dep in enumerate(direct_deps):
            dep = dep if isinstance(dep, dict) else {}
            # Two conforming shapes coexist by design (2026-07-19, W0.5 Option
            # B+C): a nested {"functional_probe": {"kind": ...}} object (used
            # by e.g. Claude-klabauter's OWN manifest, walked by a different
            # consumer) and a FLAT "functional_probe_kind" string (used by
            # coordinator-claude's own manifest — the shape its actual parser,
            # coordinator_core.ops.setup_chain_walker.dep_probe, reads).
            # agent-install-manifest.schema.json's DirectDep sub-schema was
            # corrected to the flat shape for coordinator-claude specifically;
            # this checker accepts either so it stays correct for both.
            has_nested = _truthy(dep, "functional_probe", "kind")
            has_flat = isinstance(dep.get("functional_probe_kind"), str) and bool(
                dep.get("functional_probe_kind")
            )
            if not (has_nested or has_flat):
                dep_id = _jq_or(dep.get("id"), f"(unnamed, index {i})")
                failures.add(
                    "point-1",
                    f"direct_deps[{i}] ('{dep_id}') has no functional_probe.kind / functional_probe_kind",
                    "add a functional probe (nested functional_probe.kind, or flat "
                    "functional_probe_kind — kind: sibling_dir_exists|file_exists|"
                    "python_import|command_succeeds|claude_klabauter_seam_resolvable) — a "
                    "presence check alone (sibling dir exists) is not a functional probe.",
                )

    system_prereqs = manifest.get("system_prerequisites")
    if isinstance(system_prereqs, list):
        for i, prereq in enumerate(system_prereqs):
            prereq = prereq if isinstance(prereq, dict) else {}
            if not _truthy(prereq, "probe", "cmd"):
                prereq_id = _jq_or(prereq.get("id"), f"(unnamed, index {i})")
                failures.add(
                    "point-1",
                    f"system_prerequisites[{i}] ('{prereq_id}') has no probe.cmd",
                    "add a probe.cmd that runs the tool (not just command -v) "
                    "plus an install.mode (auto-with-confirmation or manual).",
                )


def _check_point2(manifest: dict, failures: _Failures) -> None:
    """Point 2: entry-point contract properties.

    Witness precedence: when both programmatic_entry_point.entry_point_contract
    and standalone_setup_script.entry_point_contract are present,
    programmatic_entry_point is the authoritative Point-2 witness;
    standalone_setup_script.entry_point_contract is the Point-2 fallback only
    when programmatic_entry_point is absent.
    """
    if _has_key(manifest, "programmatic_entry_point"):
        if not _truthy(manifest, "programmatic_entry_point", "entry_point_contract"):
            failures.add(
                "point-2",
                "programmatic_entry_point.entry_point_contract is missing",
                "add an entry_point_contract object on programmatic_entry_point "
                "(non_interactive_flag, check_only_flag, deterministic_exit) — "
                "programmatic_entry_point is the authoritative Point-2 witness "
                "once declared.",
            )
            return
        has_noninteractive = _truthy(
            manifest, "programmatic_entry_point", "entry_point_contract", "non_interactive_flag"
        )
        has_checkonly = _truthy(
            manifest, "programmatic_entry_point", "entry_point_contract", "check_only_flag"
        )
        if not has_noninteractive and not has_checkonly:
            failures.add(
                "point-2",
                "programmatic_entry_point.entry_point_contract declares neither "
                "non_interactive_flag nor check_only_flag",
                "declare at least one on programmatic_entry_point.entry_point_contract "
                "— e.g. non_interactive_flag: '--non-interactive', check_only_flag: '--check-only'.",
            )
        return

    if not _truthy(manifest, "standalone_setup_script", "entry_point_contract"):
        failures.add(
            "point-2",
            "standalone_setup_script.entry_point_contract is missing",
            "add an entry_point_contract object on standalone_setup_script "
            "(non_interactive_flag, check_only_flag, deterministic_exit) "
            "documenting the chain-walk invocation, or declare a top-level "
            "programmatic_entry_point instead.",
        )
        return
    has_noninteractive = _truthy(
        manifest, "standalone_setup_script", "entry_point_contract", "non_interactive_flag"
    )
    has_checkonly = _truthy(
        manifest, "standalone_setup_script", "entry_point_contract", "check_only_flag"
    )
    if not has_noninteractive and not has_checkonly:
        failures.add(
            "point-2",
            "entry_point_contract declares neither non_interactive_flag nor check_only_flag",
            "declare at least one — e.g. non_interactive_flag: '--non-interactive', "
            "check_only_flag: '--check-only'.",
        )


def _check_point3(manifest: dict, failures: _Failures) -> None:
    """Point 3: installer_floor.floor_prerequisite_ids[] referential integrity
    against system_prerequisites[].id — a documented MUST the schema itself
    cannot express (no cross-array $data reference in JSON Schema 2020-12)."""
    if not _has_key(manifest, "installer_floor"):
        return

    known_ids = set()
    system_prereqs = manifest.get("system_prerequisites")
    if isinstance(system_prereqs, list):
        for prereq in system_prereqs:
            if isinstance(prereq, dict) and "id" in prereq:
                known_ids.add(prereq["id"])

    floor_ids = _get(manifest, "installer_floor", "floor_prerequisite_ids")
    if not isinstance(floor_ids, list):
        return

    for orphan_id in floor_ids:
        if orphan_id in known_ids:
            continue
        failures.add(
            "point-3",
            f"installer_floor.floor_prerequisite_ids contains '{orphan_id}', which "
            "has no matching system_prerequisites[].id",
            f"either add a system_prerequisites[] entry with id: '{orphan_id}', or "
            f"remove '{orphan_id}' from installer_floor.floor_prerequisite_ids — "
            "every floor id MUST match a declared system_prerequisites[].id.",
        )


def _check_point4(manifest: dict, failures: _Failures) -> None:
    """Point 4: tested_platforms declared as an array (may be empty).

    An empty array is valid — it honestly declares "no platform has an
    execution-verified record yet" (the correct day-one state for a
    greenfield repo whose tested_platforms is coordinator-claude-derived from
    state/platform-outcomes/ records, not hand-asserted). Only a missing
    or non-array value fails this check.
    """
    tested = manifest.get("tested_platforms")
    if not isinstance(tested, list):
        failures.add(
            "point-4",
            "tested_platforms is missing or empty",
            'declare tested_platforms as an array, e.g. "tested_platforms": '
            '["macos"] once verified, or [] if none are yet — do not conflate '
            "with present_platforms (shipped-but-unverified).",
        )


def _check_point6(manifest: dict, failures: _Failures) -> None:
    """Point 6: every configurable_locations[] entry has candidates + default +
    override; dependency-root entries additionally require BOTH override.flag
    and override.env."""
    locations = manifest.get("configurable_locations")
    locations = locations if isinstance(locations, list) else []

    for i, loc in enumerate(locations):
        loc = loc if isinstance(loc, dict) else {}
        name = _jq_or(loc.get("name"), f"(unnamed, index {i})")
        has_candidates = _array_len_at(loc, "discovery", "candidates") > 0
        has_default = _truthy(loc, "default")
        override = loc.get("override")
        override = override if isinstance(override, dict) else {}
        has_override = _truthy(override, "flag") or _truthy(override, "env")
        is_dep_root = _truthy(loc, "dependency_id")

        if not has_candidates:
            failures.add(
                "point-6",
                f"configurable_locations[{i}] ('{name}') has no ranked discovery.candidates",
                "add discovery.candidates: [ ... ] with at least one ranked "
                "candidate path/rung (machine-local-registry.md § 4 ladder vocabulary).",
            )
        if not has_default:
            failures.add(
                "point-6",
                f"configurable_locations[{i}] ('{name}') has no default",
                "add a default path/rule so the install flow can proceed silently "
                "when no candidate is confirmed.",
            )
        if not has_override:
            failures.add(
                "point-6",
                f"configurable_locations[{i}] ('{name}') has no override.flag or override.env",
                'add an override with a flag and/or env, e.g. override: { "flag": '
                '"--install-root", "env": "INSTALL_ROOT" }, so a human/GUI can inject '
                "an explicit choice.",
            )
        # Dep/sibling-root class (dependency_id present): the per-dep override is
        # the load-bearing knob — it MUST carry BOTH a flag and an env
        # (2026-07-11 claude-klabauter point-6 ruling). A single-root derivation is only a
        # subordinate default candidate.
        if is_dep_root:
            has_flag = _truthy(override, "flag")
            has_env = _truthy(override, "env")
            if not (has_flag and has_env):
                failures.add(
                    "point-6",
                    f"configurable_locations[{i}] ('{name}') is a dependency root "
                    "(dependency_id set) but its override lacks a flag and/or env",
                    "a dependency/sibling root must expose BOTH override.flag "
                    "('--<dep>-root') and override.env ('<DEP>_ROOT') — the per-dep "
                    "knob is load-bearing; a single install-root derivation is "
                    "admissible only as a subordinate default candidate.",
                )

    if len(locations) == 0:
        failures.add(
            "point-6",
            "configurable_locations is missing or empty",
            "declare at least one configurable_locations[] entry (install_root, "
            "projects_root, etc.) with discovery.candidates + default + "
            "override.flag or override.env — or, if this repo genuinely resolves "
            "no configurable locations, do not set packageability_compliance.declared "
            "until it does.",
        )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    """CLI entry: arg parsing, manifest load, opt-in gate, points 1/2/3/4/6.

    See module docstring's Exit-code contract paragraph. Never raises for a
    normal usage/data error — every foreseeable bad-input path returns 0 or 1.
    """
    manifest_path: Optional[str] = None
    repo_root = str(Path.cwd())

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--manifest-path":
            if i + 1 >= len(argv):
                print("ERROR: --manifest-path requires a value", file=sys.stderr)
                return 1
            manifest_path = argv[i + 1]
            i += 2
        elif arg == "--repo-root":
            if i + 1 >= len(argv):
                print("ERROR: --repo-root requires a value", file=sys.stderr)
                return 1
            repo_root = argv[i + 1]
            i += 2
        elif arg in ("-h", "--help"):
            print(_USAGE)
            return 0
        else:
            print(f"ERROR: unrecognized argument: {arg}", file=sys.stderr)
            return 1

    if not manifest_path:
        manifest_path = str(Path(repo_root) / _DEFAULT_MANIFEST_RELPATH)

    manifest_file = Path(manifest_path)
    if not manifest_file.is_file():
        print("SKIP: no agent-install-manifest.json declared at:", file=sys.stderr)
        print(f"      {manifest_path}", file=sys.stderr)
        print(
            "  This repo has not opted into the install manifest contract — nothing to validate.",
            file=sys.stderr,
        )
        print(
            "  Fix: if this repo SHOULD declare a manifest, see docs/install/AGENT.md and seed one",
            file=sys.stderr,
        )
        print(
            "  from coordinator/docs/install/agent-install-manifest.json as a starting template.",
            file=sys.stderr,
        )
        return 0

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"ERROR: {manifest_path} is not valid JSON.", file=sys.stderr)
        print(
            f'  Fix: run \'jq . "{manifest_path}"\' to see the parse error and correct it.',
            file=sys.stderr,
        )
        return 1

    # Opt-in gate: type-strict `=== true` (a schema-invalid string "true" is
    # NOT accepted as opt-in). A non-object manifest (or one missing the key)
    # falls through _get's isinstance guard to None, which is not `True` —
    # lands here, matching the bash oracle's jq-runtime-error-on-non-object
    # behavior (see module Negative-spec).
    declared = _get(manifest, "packageability_compliance", "declared")
    if declared is not True:
        print(f"SKIP CLEAN: {manifest_path} has not opted into the packageability contract.")
        print('  Fix: add "packageability_compliance": { "declared": true } to the manifest once')
        print("  this repo is ready to be held to points 1/2/3/4/6 — see")
        print("  docs/wiki/agent-install-contract.md § Packageability.")
        return 0

    failures = _Failures()
    _check_point1(manifest, failures)
    _check_point2(manifest, failures)
    _check_point3(manifest, failures)
    _check_point4(manifest, failures)
    _check_point6(manifest, failures)

    if failures.count > 0:
        print("", file=sys.stderr)
        print(
            f"validate-install-contract.sh: {failures.count} packageability finding(s) "
            f"against {manifest_path}",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {manifest_path} is packageability-compliant (points 1/2/3/4/6 checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
