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

Port of: check-machine-local-regeneratability.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-06-22-invariant-verification-observers.md § C1
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Offer shape: exit 0 always; findings to stderr; silent on clean.

Exit codes (parity-critical — the DoE trampoline forwards this verbatim):
    0 — always. This is an offer-shaped observer; it never blocks a caller,
        regardless of parse errors, missing HOME, or missing machine-local CLI.

SUPERSEDED 2026-08-19 — Check 1 no longer iterates ``COORDINATOR_OWNED_KEYS``.
    It derives its key set from the registry instead (``_declared_owned_keys``),
    because a hardcoded key literal cannot survive publication: percolation
    depersonalizes sibling-repo codenames, so the PUBLISHED engine compared
    ``repos.example_retrieval_repo`` against an operator registry that says
    ``repos.example_retrieval_repo``. Every renamed literal missed, and Check 1 degenerated
    twice over — a WARN per renamed key on every ceremony, AND no ability to
    detect a genuinely unclassified key, since it already reported those keys as
    unclassified unconditionally. Observed live: 7 of 14 literals renamed, a
    PARTIAL mismatch, which is why it read as ordinary findings rather than as an
    obviously broken comparison. The list itself is retained below as the bash
    parity artifact it is; it is no longer load-bearing for Check 1, so the
    verbatim-reproduction rule below governs provenance only and no longer
    governs what the observer checks. See ``_declared_owned_keys``.

    What moved with it: Check 1 now answers "is every DECLARED coordinator-owned
    key classified?" and no longer claims to answer "does every key that should
    exist exist?" — the second question needs a list of things that are absent,
    which nothing derivable from the registry can supply.

    UNCOVERED, and deliberately named rather than handed off: no other check
    currently owns that absent-key question. An earlier revision of this note
    claimed ``docs/install/agent-install-manifest.json`` already owned it; a
    reviewer checked, and it does not — the manifest names one specific key in
    prose inside a doctor script, not a systematic required-key enumeration.
    Coverage here is therefore NARROWER than the pre-percolation intent, not
    equal to it. What makes that acceptable rather than a silent regression:
    post-percolation the hardcoded list answered neither question, so nothing
    that was actually working was given up — but closing this properly needs a
    publish-proof declaration of required keys, which does not exist yet. Do not
    re-add a hardcoded literal list to close it; that is the defect this
    supersession removed.

Negative-spec (do NOT "fix" while porting):
    - The bash oracle's ``COORDINATOR_OWNED_KEYS`` list is reproduced verbatim
      (13 keys, post the 2026-06-30 F5 reconciliation — no ``repos.coordinator_claude``,
      includes ``repos.example_league_data_repo`` / ``repos.example_cockpit_repo`` / ``repos.example-os-repo``).
      Do NOT "helpfully" resync this against the live registry.toml — any drift is a
      separate reconciliation change, not something this port should silently absorb.
      EXCEPTION (docs/plans/2026-08-07-two-tier-engine-root-adopt-dr132.md AC8, chunk C6b):
      ``repos.claude_klabauter`` was added deliberately — claude-klabauter's own installer writes
      this key (``register_claude_klabauter_root()``, DR-261 gives claude-klabauter klabauter publishing), so
      it is claude-klabauter-owned and belongs on the canonical list. This is the one reviewed,
      authorized reconciliation edit; it does not reopen the list to further drift-sync.
    - Check 1's key match is exact-string, plus two named family-prefix arms (see
      ``_key_matches_regen_entry`` below) — NOT a general glob. The ``plugin.mirrors``
      namespace-prefix match was the original, sole exception (bash oracle code-review F2
      removed a tautological ``repos.*`` prefix branch; do not resurrect that shape). AC8
      of the plan above (DR-132 conformance) added a second, equally bounded arm: a dotted
      key matches a family entry that is a strict dotted-prefix of it — e.g.
      ``engine.working_repos.doe_claude`` is satisfied by a bare ``"engine.working_repos"``
      entry. DoE has declared that exact family entry on their plane (delivery memo
      cross-repo/inbox/2026-08-07-doe-claude-em-dr132-conformance-fixture-delivered.md:
      "The prefix-family match arm on the consumer side remains yours."). Do NOT widen this
      into an open glob for other keys, and do NOT add ``engine.working_repos.*`` (or any
      spelling of it) to ``COORDINATOR_OWNED_KEYS`` here — that namespace is DoE-authored
      and DoE owns its regeneratability answer; this module only supplies the arm that can
      match a family entry, not the key or the classification.
      Reachability note (review-flagged 2026-08-07): the ``engine.working_repos`` arm has
      NO production call-site in claude-klabauter today. Check 1 in ``main()`` only iterates
      ``COORDINATOR_OWNED_KEYS``, and ``engine.working_repos``/``engine.working_repos.*``
      is deliberately absent from that list (see above) — so no ``canon_key`` the
      production loop produces can ever reach the ``regen_key == family`` branch for
      this family. The arm currently fires only in this module's own unit tests
      (direct calls to ``_key_matches_regen_entry``). It exists anyway because DoE
      declared the bare family entry ``"engine.working_repos" = "idempotent-regeneratable"``
      on their plane and stated the consumer-side match arm is ours to supply
      (cross-repo/inbox/2026-08-07-doe-claude-em-dr132-conformance-fixture-delivered.md)
      — deliberate forward-looking infrastructure for whichever module (presumably
      DoE's own regeneratability checker) owns a ``canon_key`` list containing
      ``engine.working_repos.*``, not dead code. Do NOT assume Check 1 exercises this
      arm today, and do NOT "helpfully" add the DoE key to ``COORDINATOR_OWNED_KEYS``
      to make it reachable — that would violate the negative-spec above.
    - Bash read the ``[regeneratability]`` table and the flat top-level tables via a
      subprocess'd Python helper (mktemp'd heredoc) because bash cannot parse TOML
      natively. This port has no subprocess boundary for TOML parsing at all — it calls
      ``tomllib``/``tomli`` in-process. The ONLY subprocess retained is the
      ``machine-local dump --prefix repos --format json`` ladder probe (Check 2's
      repos.* skip, batched W6/C6 2026-08-19 from a per-key ``get`` into one call),
      which is genuinely an external CLI call, not a TOML-parsing workaround.
    - The registry DIRECTORY resolves via the settings-home ladder (``_resolve_registry_dir``:
      ``MACHINE_LOCAL_REGISTRY_DIR`` override > ``<settings-home>/machine-local``), NOT the
      legacy ``${CLAUDE_HOME:-$HOME}/.claude/machine-local`` path — that legacy path caused
      every coordinator-owned key to false-WARN as unclassified on settings-home-native
      machines (the dir simply didn't exist there). ``--claude-dir``/``_resolve_claude_dir``
      is retained ONLY to locate the ``machine-local`` CLI *binary* for the Check 2 ladder
      probe — it no longer has anything to do with the registry directory.
"""

from __future__ import annotations

import json
import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core import _settings_home

_PROG = "check-machine-local-regeneratability"

# Coordinator-owned keys: the canonical set of keys expected classified in the
# [regeneratability] table. Matched via _key_matches_regen_entry(): exact-string,
# plus the two named family-prefix arms in FAMILY_PREFIX_ENTRIES below (plugin.mirrors
# is a per-plugin namespace set via `machine-local set`); every other key requires an
# exact match.
# PROVENANCE ONLY — not load-bearing. Check 1 derives its key set from the
# registry via `_declared_owned_keys`; see this module's docstring § SUPERSEDED
# for why a hardcoded literal list cannot survive publication. Retained as the
# bash parity artifact, so the verbatim-reproduction rule below governs this
# list's fidelity to its oracle, NOT what the observer checks.
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
    "repos.claude_klabauter",
]

# Family-prefix match arms (AC8, docs/plans/2026-08-07-two-tier-engine-root-adopt-dr132.md
# chunk C6b): a bare family entry in [regeneratability] satisfies any dotted key that has
# it as a strict dotted-prefix. Named and bounded — not a general glob (see module
# docstring negative-spec). "engine.working_repos" is DoE-declared on their plane; claude-klabauter
# only supplies the arm that can match it, not the key itself (COORDINATOR_OWNED_KEYS is
# deliberately NOT extended with any engine.working_repos.* spelling).
FAMILY_PREFIX_ENTRIES: List[str] = [
    "plugin.mirrors",
    "engine.working_repos",
]


def _key_matches_regen_entry(canon_key: str, regen_key: str) -> bool:
    """True if a [regeneratability] table entry satisfies a coordinator-owned key.

    Exact string match, or — for the named family-prefix entries — a strict
    dotted-segment prefix match. The loop tries BOTH directions for EVERY family
    in ``FAMILY_PREFIX_ENTRIES`` (either side may be the bare family entry and the
    other the dotted specific key, ``family`` + ``"."`` + more path) — it does not
    enforce a single per-family direction. In practice only one direction is ever
    satisfiable per family given the current key list: ``plugin.mirrors`` is only
    ever the bare *canonical* key satisfied by a dotted regen sub-entry
    (``plugin.mirrors.coordinator-claude``), while ``engine.working_repos`` is only
    ever the bare *regen* entry satisfying a dotted specific key
    (``engine.working_repos.doe_claude``) — but that's a property of today's data,
    not something this function enforces; the stray opposite-direction branches are
    simply always False given the current key list. A plain ``str.startswith`` would
    also match a key that merely shares a leading substring without a dotted-segment
    boundary (e.g. ``plugin.mirrorsx``); the explicit ``.`` join guards against that.
    """
    if canon_key == regen_key:
        return True
    for family in FAMILY_PREFIX_ENTRIES:
        if canon_key == family and regen_key.startswith(family + "."):
            return True
        if regen_key == family and canon_key.startswith(family + "."):
            return True
    return False

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


#: Namespaces whose declared keys this observer holds to the classification
#: requirement. NAMESPACES, not key literals, because a namespace survives
#: percolation and a codename does not — see `_declared_owned_keys`.
COORDINATOR_OWNED_NAMESPACES: List[str] = [
    "coordinator",
    "plugin",
    "publish",
    "repos",
]


def _declared_owned_keys(ml_dir: Path) -> List[str]:
    """Coordinator-owned keys actually DECLARED in the tracked registry files —
    the dotted top-level table names (``repos.example-sim-repo``, ``publish.targets``)
    whose first segment is a coordinator-owned namespace.

    Derived rather than hardcoded, because a hardcoded key list cannot survive
    publication. Percolation depersonalizes sibling-repo codenames, so the
    PUBLISHED engine carries ``repos.example_retrieval_repo`` where the
    operator's real machine-local registry says ``repos.example_retrieval_repo``. Every
    renamed literal then misses, and check 1 degenerates twice over: it emits a
    WARN per renamed key on every ceremony, and it can no longer detect a
    genuinely unclassified key, because it already reports those keys as
    unclassified unconditionally. Observed live 2026-08-19: 7 of the 14
    literals were renamed, so the mismatch was partial — which is why it read
    as ordinary findings for as long as it did, rather than as an obviously
    broken comparison. The NAMESPACE half of each key (``repos.``,
    ``publish.``) is a generic English word that percolation leaves alone, so
    deriving keys from it is stable on both sides of the transform.

    Negative-spec: this answers "is every DECLARED coordinator-owned key
    classified?", NOT "does every key that should exist exist?". A key missing
    from the registry outright is an install-surface question, owned by
    ``docs/install/agent-install-manifest.json`` and its own checks — not
    recoverable here, because an absent key leaves nothing to enumerate. The
    prior hardcoded list nominally covered that second question and, post-
    percolation, actually answered neither.

    ``engine.working_repos`` is deliberately absent from
    ``COORDINATOR_OWNED_NAMESPACES``: that namespace is DoE-authored (see this
    module's own docstring), and claude-klabauter supplies only the family-prefix match
    arm for it, never the key."""
    declared: set[str] = set()
    for toml_file in _tracked_toml_files(ml_dir):
        data = _parse_toml_file(toml_file, emit_errors=False)
        if data is None:
            continue
        for key, value in data.items():
            key = str(key)
            head, _, tail = key.partition(".")
            if head not in COORDINATOR_OWNED_NAMESPACES:
                continue
            # Both TOML spellings of the same declaration. `["repos.example-sim-repo"]`
            # (quoted) parses to a flat dotted top-level key; `[repos.example-sim-repo]`
            # (bare) parses to a NESTED table under `repos`. The machine-local
            # registry uses the quoted form today, but the classification this
            # observer checks is a property of the declaration, not of which
            # spelling the file happens to use — reading only one shape makes
            # the check silently blind to a registry written the other way.
            if tail:
                declared.add(key)
            elif isinstance(value, dict):
                # Only sub-tables are declarations. A bare `[repos]` table can
                # also carry namespace-level scalars (`[repos]` + `default = "x"`),
                # and admitting those would invent a `repos.default` key that was
                # never declared -- then report it unclassified forever, which is
                # the fabricated-finding half of the defect this function fixes,
                # rebuilt from the other side.
                declared.update(
                    f"{head}.{sub}"
                    for sub, sub_value in value.items()
                    if isinstance(sub_value, dict)
                )
    return sorted(declared)


def _tracked_toml_files(ml_dir: Path) -> List[Path]:
    if not ml_dir.is_dir():
        return []
    return sorted(f for f in ml_dir.glob("*.toml") if not f.name.endswith(".local.toml"))


def _ladder_snapshot(machine_local_bin: Path) -> Optional[Dict[str, str]]:
    """One `dump --prefix repos --format json` call resolving every repos.* key
    at once — batch counterpart to the per-key `machine-local get <key>` probe
    this Check 2 arm used to spawn once per repos.* finding candidate (W6/C6,
    2026-08-19 amplification burn-down; same primitive already proven in
    `coordinator/bin/lib/cli_shared.py::machine_local_dump_repos` and
    `coordinator_core/ops/register_discovered_repos.py::_registry_snapshot`,
    whose docstring cites `_machine_local.py::cmd_dump` sharing `resolve_one`
    with `get` — a dumped value is byte-identical to what a per-key `get`
    would print).

    Returns None (never {}) on a missing binary, timeout, or any subprocess/parse
    failure or non-zero returncode — fail-safe: `_key_ladder_resolves` below then
    treats every repos.* key as "does not resolve", so the key still gets
    evaluated by the normal tracked/local check, matching the ladder-probe's
    original `if ... ; then continue; fi` behaviour (a non-zero/errored probe
    falls through, never crashes).
    """
    if not machine_local_bin.is_file():
        return None
    try:
        result = subprocess.run(
            [str(machine_local_bin), "dump", "--prefix", "repos", "--format", "json"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _ladder_snapshot: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if isinstance(v, str) and v}


def _key_ladder_resolves(snapshot: Optional[Dict[str, str]], key: str) -> bool:
    """True if `key` resolved in the batched ladder snapshot (or is missing
    from a `None`/absent snapshot, which fails safe to "does not resolve").
    """
    if snapshot is None:
        return False
    return bool(snapshot.get(key))


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

    for canon_key in _declared_owned_keys(ml_dir):
        found = False
        for regen_key in regen_table:
            if _key_matches_regen_entry(canon_key, regen_key):
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

    # One batched dump replacing a per-repos.*-key `machine-local get` spawn —
    # see `_ladder_snapshot`. Computed unconditionally but cheaply (single
    # spawn, memoised for the whole loop below); `_key_ladder_resolves` fails
    # safe to "not resolved" if the binary is missing or the dump errors.
    ladder_snapshot = _ladder_snapshot(machine_local_bin)

    for key, val in regen_table.items():
        if val != "session-accumulated-must-survive-crash":
            continue

        in_tracked = key in tracked_keys
        in_local = local_keys.get(key)

        # For repos.* keys: consult the batched ladder snapshot before flagging as
        # a gap. Presence in the dump means rung-2 autodiscovery (or another ladder
        # rung) can derive the value on a fresh-machine clone — NOT a manual-re-entry gap.
        if key.startswith("repos.") and _key_ladder_resolves(ladder_snapshot, key):
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
