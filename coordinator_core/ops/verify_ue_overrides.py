"""
coordinator_core.ops.verify_ue_overrides — plain module, no registered op.

Purpose: walks the machine-local-registered UE-context directories
(repos.example_game_workbench_repo, repos.example_retrieval_repo, optional
repos.example-sim-repo) and asserts each carries the expected UE plugin override keys in its
.claude/settings.json enabledPlugins object. Exits 0 on success, 1 with diagnostic
output on failure. This is a MANUAL diagnostic — per docs/wiki/per-project-plugin-
gating.md § verify-ue-overrides.sh, it is never auto-invoked by any ceremony (its peer
dirs are specific to the source author's local machine layout); run manually when UE
override drift is suspected.

Reads the machine-local registry directly, in-process, via
`machine_resolver.registry_get` (converted 2026-08-16, C7b) -- no `machine-local`
CLI subprocess, no PATH/settings-home/legacy-home/co-located-binary resolution
ladder.

machine-local keys consumed (must be set in registry.local.toml):
    repos.example_game_workbench_repo  — root of the example-game-workbench-repo repo
    repos.example_retrieval_repo             — root of the example-retrieval-repo repo
    repos.example-sim-repo                — root of the example-sim-repo UE project (optional; skipped
                                     if unset on this machine)

If a required key is not set, this fails loud with a remediation hint — a missing
registry value is a configuration gap that needs fixing, not a silent skip.

It ALSO asserts the inverse for `$HOME/.claude/settings.json`: every UE plugin key
must be OFF there. Per-project gating puts the opt-in in each UE repo and requires the
global file to stay off, so a `true` in it is the drift.

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix" mid-port):
    - `$HOME/.claude` was carried in the walked dir list, with a special-cased
      settings path, and asserted UE-ON like the genuine UE repos. CORRECTED
      2026-08-31, not preserved: it is not a UE-context dir (no `.uproject`, never
      had one), and demanding `true` there flagged correct config as drift on every
      close ceremony. See `_global_settings_is_ue_off`.
    - A missing resolved directory is a hard FAIL (not a silent skip) — the old bash
      shape silently continued past a stale/wrong registry path; this port keeps the
      fail-loud replacement behavior, not the original silent-continue.
    - example-sim-repo is optional: `repos.example-sim-repo` unset on this machine means it is simply
      omitted from the walked dir list, not a failure.

Spec backlink: docs/plans/2026-05-20-coordinator-doctor-wiki.md § Chunk 10 (MISSED-2:
    hardcoded single-machine paths + vacuous-pass on missing dirs)
Port of: verify-ue-overrides.sh (DoE b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Optional, Tuple

from coordinator_core import machine_resolver as _machine_resolver
from coordinator_core._settings_home import home_dir

# Plugins required to be enabled in every UE-context settings.json.
_EXPECTED_KEYS = (
    "example-game-repo-control@example-game-workbench-repo",
    "example-game-repo@example-game-workbench-repo",
)

# Either-vendor set: at least one entry from this set must be enabled. Avoids the
# dual-vendor conflict where two plugins claim the same agent surface (game-dev is
# vendored by both coordinator-claude and example-game-workbench-repo; only one should be
# active per machine to keep agent routing unambiguous).
_EITHER_VENDOR_GAME_DEV = (
    "game-dev@example-game-workbench-repo",
    "game-dev@coordinator-claude",
)

# The machine-local keys naming a UE-context checkout. ALL are optional: an
# unset key means "that repo is not on this machine", never drift. Ordered
# most- to least-canonical purely for readable output; nothing depends on it.
_UE_CONTEXT_REPO_KEYS = (
    "repos.example_game_workbench_repo",
    "repos.example_retrieval_repo",
    "repos.example-sim-repo",
)


def _ml_get(key: str) -> Optional[str]:
    """Resolve `key` via the direct-registry reader
    (`machine_resolver.registry_get`) -- no `machine-local` CLI subprocess,
    no PATH/settings-home/legacy-home/co-located-binary resolution ladder.

    Converted 2026-08-16 (C7b): this module's own test suite
    (`test_verify_ue_overrides.py`) now seeds the machine-local registry
    FILE instead of faking the CLI as a real subprocess-invoked script keyed
    on a resolved `ml_bin` path, so this in-process conversion no longer
    silently stops exercising the fake and reading the operator's real
    registry instead. Registry-not-found (missing key, unreadable/missing
    file) degrades to None, matching this function's existing "no signal"
    contract."""
    return _machine_resolver.registry_get(key)


def _read_enabled_plugin(settings_path: str, key: str) -> str:
    """Read enabledPlugins[key] from settings_path; 'missing' on any read/parse/key-miss."""
    try:
        with open(settings_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        print(f"skip: _read_enabled_plugin: with open(settings_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return "missing"
    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return "missing"
    val = enabled.get(key, "missing")
    if val is True:
        return "true"
    if val is False:
        return "false"
    return "missing" if val is None else str(val)


def _global_settings_is_ue_off(settings: str) -> bool:
    """The GLOBAL settings file must have every UE plugin key OFF.

    The inverse of what the rest of this module asserts, and deliberately so:
    per-project plugin gating puts the opt-in in each UE repo and requires
    `~/.claude/settings.json` to stay off, so a `true` here is the drift --
    not the compliance this verifier used to demand of it.

    Absent file or absent key is PASS: the requirement is "not enabled", and
    an unwritten key is not enabled. Only a literal `true` fails.
    """
    ok = True
    for key in _EXPECTED_KEYS + _EITHER_VENDOR_GAME_DEV:
        if _read_enabled_plugin(settings, key) == "true":
            print(
                f"WRONG: {settings} [{key}] = true (expected off -- the global "
                "settings file must not enable UE plugins; the opt-in belongs "
                "in each UE repo)",
                file=sys.stderr,
            )
            ok = False
    return ok


def main(argv: List[str], script_dir: Optional[str] = None) -> int:
    """CLI entrypoint: verify-ue-overrides (no positional args consumed; argv unused,
    kept for trampoline-call symmetry with other ported ops).

    `script_dir` is now VESTIGIAL (converted 2026-08-16, C7b): it existed
    solely as the third rung of a `machine-local` CLI-binary resolution
    ladder that this module no longer runs -- `_ml_get` reads the registry
    directly, in-process. Kept as an accepted (ignored) parameter so
    `sentinel.py`'s existing `main(..., script_dir=str(sh_bin))` call site
    does not need to change.
    """

    # AN UNREGISTERED UE REPO IS NOT DRIFT. These keys used to be REQUIRED --
    # an unset one printed a remediation and returned 1. That made this check
    # fail by construction on every machine that does not carry the source
    # author's UE layout, which is most of the fleet, and this module's own
    # docstring says as much ("its peer dirs are specific to the source
    # author's local machine layout"). Wired into doctor probe P-9, that
    # produced a permanent amber nobody could clear, and a doctor that is
    # always amber is one operators stop reading.
    #
    # A second, less obvious caller hits the same wall: the PUBLISHED engine.
    # Percolate depersonalizes private repo codenames on the way to the mirror
    # (`coordinator_core/percolate/codename_provenance_seed.py`), so the copy
    # every box actually resolves asks for `repos.<placeholder>` -- a key no
    # registry has or should have. Treating an unresolved key as "not
    # applicable" makes that case a clean skip rather than a false failure,
    # WITHOUT reaching around the redaction, which is a publish contract this
    # module has no business subverting.
    #
    # `repos.example-sim-repo` was already optional on exactly this reasoning; the
    # other two now match it. What is NOT optional is the global-settings
    # assertion below -- that invariant is machine-independent, it is the drift
    # this check exists to catch, and it runs whether or not any UE repo is
    # registered here.
    named_dirs: List[str] = []
    for key in _UE_CONTEXT_REPO_KEYS:
        root = _ml_get(key)
        if root:
            named_dirs.append(root)

    # Same Windows landmine as the legacy resolver rung above, second site:
    # native Windows shells do not set HOME, and os.environ.get("HOME", "")
    # would make the join below a RELATIVE ".claude" resolving against cwd — so
    # the walk would silently inspect the wrong directory and report a
    # misleading path. home_dir() is USERPROFILE-aware.
    home = str(home_dir())
    # `$HOME/.claude` IS NOT A UE-CONTEXT DIR, and demanding `true` there was
    # the drift engine itself. Per-project plugin gating (ratified; DoE-claude
    # `coordinator/docs/wiki/per-project-plugin-gating.md` § Files Involved)
    # requires the GLOBAL settings file to be UE-OFF and each UE repo to carry
    # its own opt-in. This verifier asserted the exact state that design
    # forbids, in the exact file it names, and it is wired into a close
    # ceremony -- so every run flagged correct config as drift and invited a
    # re-bootstrap that would undo the gating. `~/.claude` has no `.uproject`
    # and never did; it was bootstrapped into this list in 2026-05 alongside
    # the genuine UE repos, and the bash->Python port carried it faithfully.
    # Reported by doe-claude-em 2026-08-14 with the three WRONG lines it
    # produced against a correct machine.
    #
    # It moves to its own assertion below rather than being dropped, because a
    # silent skip cannot catch the re-bootstrap that turns it back on.
    fail = False
    if not _global_settings_is_ue_off(os.path.join(home, ".claude", "settings.json")):
        fail = True

    for dir_path in named_dirs:
        if not os.path.isdir(dir_path):
            print(
                f"ERROR: resolved directory '{dir_path}' does not exist on this machine",
                file=sys.stderr,
            )
            print(
                "  Check the machine-local registry value that resolved to this path.",
                file=sys.stderr,
            )
            print(
                "  Remediation: ~/.claude/plugins/coordinator-claude/coordinator/docs/wiki/"
                "machine-local-registry.md § Verifying registry health",
                file=sys.stderr,
            )
            fail = True
            continue

        settings = os.path.join(dir_path, ".claude", "settings.json")

        if not os.path.isfile(settings):
            print(
                # The bash entrypoint this used to name (~/.claude/bin/
                # claude-ue-bootstrap.sh) was retired by the C5 native port and
                # is absent on every machine — the remediation was unrunnable as
                # written. Name the surviving CLI, which resolves through the
                # settings-home forwarder like every other coordinator CLI.
                f"MISSING: {settings} — run claude-ue-bootstrap {dir_path}",
                file=sys.stderr,
            )
            fail = True
            continue

        for key in _EXPECTED_KEYS:
            val = _read_enabled_plugin(settings, key)
            if val != "true":
                print(f"WRONG: {settings} [{key}] = {val} (expected true)", file=sys.stderr)
                fail = True

        game_dev_ok = False
        for key in _EITHER_VENDOR_GAME_DEV:
            val = _read_enabled_plugin(settings, key)
            if val == "true":
                game_dev_ok = True
        if not game_dev_ok:
            vendors = ", ".join(_EITHER_VENDOR_GAME_DEV)
            print(
                f"WRONG: {settings} — no game-dev vendor enabled "
                f"(expected at least one of: {vendors})",
                file=sys.stderr,
            )
            fail = True

    if not fail:
        if named_dirs:
            print("all known UE-context dirs carry the expected override")
        else:
            print(
                "not applicable — no UE-context repo is registered on this machine "
                f"({', '.join(_UE_CONTEXT_REPO_KEYS)} all unset); the global "
                "settings.json UE-off assertion ran and passed"
            )
        return 0
    return 1
