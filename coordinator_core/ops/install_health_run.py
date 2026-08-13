"""
coordinator_core.ops.install_health_run — drop-in install-health orchestrator.

Purpose: mechanizes the `bin/install-health-run.sh` orchestrator that iterates
every `bin/install-health/*.sh` drop-in script (lexicographic order), runs it
via `bash <script>` with stdout/stderr passthrough, continues past individual
failures, and aggregates a non-zero exit iff any sub-script failed. Adding a
new install-health script is a directory drop, not a doc/orchestrator edit —
see the coordinator-claude trampoline's own header note for the anti-pattern this collapses
(2026-06-16 PM callout: one inline bash block per script in commands/install.md).

Dual-anchor discovery (2026-07-22, closes the plugin_root-coupling defect):
  coordinator-claude deleted its entire `coordinator/bin/install-health/` drop-in directory
  (the trio: `ensure-python3-exe-shim.sh`, `check-windows-ssh-binary.sh`,
  plus the claude-klabauter-root sourced-lib `coordinator-claude-klabauter-root.sh`) under a
  PM kill-first ruling — cross-repo/inbox/2026-07-22-claude-central-em-
  install-health-trio-deleted-kill-first.md. The drop-in directory now
  lives (if at all) under claude-klabauter's OWN tree
  (`<claude_klabauter_root>/coordinator/bin/install-health/`), alongside the
  orchestrator itself (`<claude_klabauter_root>/coordinator/bin/install-health-run.py`,
  a thin CLI trampoline over this module). Resolving EITHER off
  `CLAUDE_PLUGIN_ROOT`/`plugin_root` (the coordinator-claude-side invoking-harness root)
  was therefore silently wrong the moment coordinator-claude's directory was gone — the
  glob would look in a directory that no longer exists on that side at all,
  even for a still-live claude-klabauter-owned drop-in.

  Fix: `plugin_root` remains the anchor ONLY for the top-level trust gate
  (`_trusted_root`, below) — that check validates the invoking harness root
  itself, a coordinator-claude-side concept unrelated to where leg content lives. Every
  claude-klabauter-side surface (the drop-in directory, and the `seed-skill-overrides`
  helper lookup) instead resolves off `coordinator_claude_klabauter_root()`
  (`coordinator_core.claude_klabauter_root`) — the canonical claude-klabauter-root resolver
  (env var -> settings-home pointer file -> machine-local registry -> raise,
  never silent). This is the same dual-anchor split already applied
  elsewhere in this port (e.g. `coordinator_core.install.substrate`'s
  `_write_agent_forwarder` cmd-twin source repoint).

Native leg registry (`_NATIVE_LEGS`) — no globbing for claude-klabauter-owned legs:
  All four claude-klabauter-owned legs (`ensure_python3_exe_shim`,
  `check_windows_ssh_binary`, `seed_skill_overrides`,
  `check_bareword_path_provisioning`) run UNCONDITIONALLY in `main()`,
  in-process, via an explicit registry — never discovered through
  a `bin/install-health/*.sh` glob-and-basename-intercept, and never
  requiring a bash veneer front door. This collapses the prior two-tier
  split (a `_NATIVE_PROBES` registry for coordinator-claude-deleted siblings, run
  unconditionally, plus a `_NATIVE_ENTRYPOINTS` glob-intercept map for
  `seed-skill-overrides` whose `.sh` sibling still existed) into one
  registry once `seed-skill-overrides`' own `.sh` sibling ALSO stopped being
  a reliable discovery anchor for the same directionality reason (coordinator-claude-side
  glob's own drop-in directory is gone; claude-klabauter's own copy under
  `coordinator/bin/install-health/` is a static artifact, not something a
  process needs to glob to find its OWN already-known native module).
  `check_bareword_path_provisioning` (added
  `docs/plans/2026-07-25-posix-bareword-path-provisioning.md` C3) has no
  `.sh`-drop-in ancestor at all — it is a claude-klabauter-native addition to this
  same registry, not a port. Nothing about any of the four legs' execution
  depends on a directory's contents, a basename match, or a subprocess/
  interpreter resolution ever again — the class of bug this closes (a
  sibling repo's routine housekeeping silently disabling a claude-klabauter-owned
  health check) is structurally impossible to repeat for any of them.

  The `bin/install-health/*.sh` glob + shebang-dispatch subprocess path
  below is retained ONLY as a residual extensibility hook for a
  hypothetical FUTURE foreign drop-in with no native peer — it is not
  required for any leg this module currently owns, and every currently
  known basename is excluded from it via `_decoupled_basenames` (skip, not
  double-run, if one is ever reintroduced on disk).

Spec backlink: cross-repo/inbox/2026-07-21-claude-central-em-dr079-doe-dispositions-and-install-health-defect.md
Spec backlink: cross-repo/inbox/2026-07-22-claude-central-em-install-health-trio-deleted-kill-first.md

Trust gate: the resolved plugin root is checked against the canonical
`coordinator_core.trusted_root_guard.is_trusted` (fail-loud call-site
shape — see that module for the full anchor list). Untrusted root is
fail-loud (exit 1), matching the original script's
own `--mode=fail-loud` call — install-health-run.sh is a REQUIRED install
step (`coordinator/scripts/install-maximalist.py` calls it via `run_required`),
not an advisory hook. `coordinator_claude_klabauter_root()` failing to resolve is likewise fail-loud
(exit 1) — every leg below needs a resolved claude-klabauter root to run correctly
(the seed-skill-overrides helper lookup and the residual glob directory both
live under it), so an unresolvable root cannot be silently downgraded to
"no legs ran."

Port of: install-health-run.sh (coordinator-claude 290997c7, 2026-07-22)
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec (deliberately reproduced, not "fixed"):
  - No `-e`-equivalent bail on the first failing sub-script — the loop must
    continue past sub-script failures (mirrors `set -uo pipefail`, no `-e`,
    in the bash oracle).
  - Absent `bin/install-health/` dir is a VALID no-scripts state for the
    residual glob loop (exit 0, not an error) — but this no longer means "no
    legs ran": `_NATIVE_LEGS` always runs first, dir-glob result
    notwithstanding (see registry note above).
  - Sub-script exit codes are aggregated only as a failure COUNT — the
    orchestrator's own exit code is 0 or 1, never the sub-script's raw rc.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.install._shared import env_overlay
from coordinator_core.install.shell_rc_guard import write_path_entry_guard_blocks
from coordinator_core.install.wrapper_onto_path import _on_path as _bin_dst_on_path
from coordinator_core.launchable import resolve_by_shebang
from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root
from coordinator_core.ops import check_windows_ssh_binary, ensure_python3_exe_shim, seed_skill_overrides
from coordinator_core.trusted_root_guard import is_trusted as _trusted_root
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Ordered list of (display_name, entrypoint) for every claude-klabauter-owned
# install-health leg. Each entrypoint is normalized to a single
# ``(plugin_root, claude_klabauter_root) -> int`` shape regardless of the underlying
# module's own main() signature, and runs UNCONDITIONALLY in `main()` below
# — no dependency on any file existing in coordinator-claude's (or any) drop-in directory,
# and no dependency on `bin/install-health/` existing at all. `display_name`
# is used only for log/failure messages — it is NOT looked up anywhere.
#
# `seed-skill-overrides` needs `claude_klabauter_root` to locate its coordinator-claude-resident-
# named-but-now-claude-klabauter-resident helper (`<claude_klabauter_root>/coordinator/bin/
# seed-skill-overrides.py`) via the `helper_root` param — kept separate from
# `plugin_root` (still the trust-check anchor for that module's OWN
# `_trusted_root`, unchanged) per the dual-anchor split (see module
# docstring). `ensure_python3_exe_shim` and `check_windows_ssh_binary` need
# neither root — both are pure OS-gated probes.
#
# Ordering note for `ensure-python3-exe-shim`'s ordering constraint: its bash
# oracle sourced coordinator-trusted-root-guard.sh before doing anything
# else. That invariant is preserved structurally, not by list order — see
# `main()`, which runs the trust gate (`is_trusted`) before this registry
# is ever reached.
#
# `check-bareword-path-provisioning` needs neither `plugin_root` nor
# `claude_klabauter_root` either — like the two OS-gated probes above, it derives
# everything it needs (settings-home, the operator's own rc files) from the
# ambient environment. See `check_bareword_path_provisioning`'s own
# docstring for why it carries two assertions of different epistemic status
# rather than one report-only probe.
_NATIVE_LEGS = [
    ("ensure-python3-exe-shim", lambda plugin_root, claude_klabauter_root: ensure_python3_exe_shim.main([])),
    ("check-windows-ssh-binary", lambda plugin_root, claude_klabauter_root: check_windows_ssh_binary.main([])),
    (
        "seed-skill-overrides",
        lambda plugin_root, claude_klabauter_root: seed_skill_overrides.main(
            [], plugin_root=plugin_root, helper_root=os.path.join(claude_klabauter_root, "coordinator")
        ),
    ),
    (
        "check-bareword-path-provisioning",
        lambda plugin_root, claude_klabauter_root: check_bareword_path_provisioning(plugin_root, claude_klabauter_root),
    ),
]

_BIN_DST_KNOWN_FORWARDER = "machine-local"

# Review: code-reviewer (Finding 3) -- this used to be a byte-for-byte copy
# of `wrapper_onto_path._on_path()` (same PATH-membership predicate,
# same docstring). Imported directly instead (see the module import block
# above) so a future fix to the PATH-comparison logic doesn't need a second,
# independently-drifting edit here -- this repo's plan-level convention is
# that this class of logic exists exactly once.


def check_bareword_path_provisioning(plugin_root: str, claude_klabauter_root: str) -> int:
    """Native install-health leg asserting the C1 ``settings-home/bin``
    PATH-provisioning write actually took effect on disk.

    Two assertions of DIFFERENT epistemic status, deliberately not
    collapsed into one probe (`docs/plans/2026-07-25-posix-bareword-path-
    provisioning.md` C3) — a check that cries wolf on every correct install
    is read past by the second install, a worse signal-to-noise position
    than the self-check this plan retires elsewhere (C5):

      (a) DETERMINISTIC, FAIL-ABLE, NO FALSE POSITIVES — read every
          applicable POSIX profile/rc file back from disk (via
          ``write_path_entry_guard_blocks(..., check_only=True)``, the same
          canonical writer C1 uses to WRITE the block, so there is no
          second, independently-buggy sentinel-matching implementation to
          drift from it — see `shell_rc_guard`'s own "logic exists exactly
          once" negative-spec) and assert the ``SETTINGS_HOME_BIN`` guard
          block is present in every one of them; assert ``settings-home/bin``
          exists; assert it contains a known forwarder (``machine-local``).
          All three are fully knowable at install time without depending on
          the invoking shell's own environment — a failure here is a
          genuine, actionable defect, and this is the ONLY half of this leg
          that may fail the install.

      (b) INFORMATIONAL ONLY, NEVER A FAILURE — PATH-string comparison
          (``_bin_dst_on_path``, modeled on
          ``wrapper_onto_path._on_path()``) against the CURRENT shell's live
          PATH. A freshly-written rc block is by definition not yet active in
          the already-running install shell, so "not on PATH yet" is the
          EXPECTED outcome on literally every fresh install, not a defect —
          reported as a NOTE, never counted toward this leg's return code.
          <!-- Review: code-reviewer (Finding 2) -- this paragraph previously
          said `shutil.which()`, which the code never called (and `shutil`
          itself was a dead import); the plan's own C3 text conflated the
          two idioms, the code correctly picked PATH-string comparison. -->

    POSIX only: native Windows has no rc-file equivalent to check (rc files
    are a POSIX shell artifact; Windows PATH provisioning is
    `_windows_health_steps`'s job, unchanged by this leg) — an explicit
    ``os.name == "nt"`` no-op, never a spurious failure.
    """
    del plugin_root, claude_klabauter_root

    if os.name == "nt":
        print(
            "[bareword-path] native Windows has no POSIX shell-rc equivalent "
            "to check here -- see _windows_health_steps for Windows PATH health"
        )
        return 0

    bin_dst = settings_home() / "bin"
    home = Path(os.environ.get("CLAUDE_HOME") or os.environ.get("HOME", ""))

    failures = 0

    result = write_path_entry_guard_blocks(
        path_entry=str(bin_dst),
        sentinel_id="SETTINGS_HOME_BIN",
        position="append",
        home=home,
        check_only=True,
    )
    missing_files = sorted(
        rc_path
        for rc_path, per_file in result.get("results", {}).items()
        if not per_file.get("already_present")
    )
    if missing_files:
        print(
            "[bareword-path] FAIL: SETTINGS_HOME_BIN guard block missing from: "
            + ", ".join(missing_files),
            file=sys.stderr,
        )
        failures += 1

    if not bin_dst.is_dir():
        print(f"[bareword-path] FAIL: {bin_dst} does not exist", file=sys.stderr)
        failures += 1
    elif not (bin_dst / _BIN_DST_KNOWN_FORWARDER).is_file():
        print(
            f"[bareword-path] FAIL: {bin_dst} exists but has no "
            f"{_BIN_DST_KNOWN_FORWARDER!r} forwarder",
            file=sys.stderr,
        )
        failures += 1

    if not _bin_dst_on_path(bin_dst):
        print(
            f"[bareword-path] NOTE: {bin_dst} is not yet on PATH in this shell "
            "-- open a new terminal (or re-source your rc files) for it to take effect"
        )

    return 1 if failures else 0


def _default_plugin_root(script_path: Optional[str]) -> str:
    """Derive the plugin root the same way the bash oracle's BASH_SOURCE
    fallback does: two levels up from the orchestrator script (bin/ -> root).
    Falls back to cwd if no script_path is available (should not happen in
    practice — the trampoline always passes its own __file__)."""
    if script_path:
        return os.path.dirname(os.path.dirname(os.path.abspath(script_path)))
    return os.getcwd()


def main(argv: List[str], script_path: Optional[str] = None) -> int:
    """CLI entry.

    ``--check-only`` (also accepted via a pre-set ``CHECK_ONLY`` env var, e.g. from a
    caller already inside a check-only pass) propagates into ``os.environ["CHECK_ONLY"]``
    for the whole process BEFORE any leg runs, exactly mirroring the coordinator-claude-side doc block's
    own ``export CHECK_ONLY=1`` / ``export CHECK_ONLY=`` behavior it collapses: every
    drop-in and native leg that self-gates on ``$CHECK_ONLY`` sees the identical signal
    it always has, whether the flag arrived via argv or an inherited env var. Any other
    argv token is silently ignored (matches the pass-through-tolerant convention used by
    sibling install ops, e.g. ``register_coordinator_mirror`` — a caller forwarding a
    blob of unrelated install flags must not fail this orchestrator)."""
    check_only = "--check-only" in argv or os.environ.get("CHECK_ONLY", "").strip() not in ("", "0")

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or _default_plugin_root(script_path)

    if not _trusted_root(plugin_root):
        site = script_path or "install-health-run.sh"
        print(
            f"ERROR: {site} '{plugin_root}' outside trusted prefix — "
            "refusing to source; re-run coordinator:install (or set "
            "COORDINATOR_PLUGIN_ROOT_TRUSTED=1 for a sanctioned --plugin-dir spike)",
            file=sys.stderr,
        )
        return 1

    try:
        claude_klabauter_root = coordinator_claude_klabauter_root()
    except RuntimeError as exc:
        print(f"ERROR: install-health-run.py: {exc}", file=sys.stderr)
        return 1

    # Scoped, not process-wide: a bare `os.environ["CHECK_ONLY"] = ...` here would
    # leak past this call for the life of the interpreter (2026-07-21
    # interpreter-global-state sweep) — every drop-in/native leg below still SEES
    # the identical env-var signal the coordinator-claude doc block's own `export CHECK_ONLY=1` /
    # `export CHECK_ONLY=` used to set, just scoped to this run.
    with env_overlay({"CHECK_ONLY": "1" if check_only else ""}):
        return _run_legs(plugin_root, claude_klabauter_root, script_path)


def _run_legs(plugin_root: str, claude_klabauter_root: str, script_path: Optional[str]) -> int:
    """Run each install-health leg script, tallying failures.

    Deliberate isolation boundary — do not convert leg subprocess spawns to
    in-process imports. Mechanism: crash containment — install-health legs
    are third-party/user drop-ins whose crash must not take down the
    caller. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    failures = 0

    # Native legs run UNCONDITIONALLY here — before any glob, regardless of
    # whether bin/install-health/ exists under claude_klabauter_root at all. The trust
    # gate above has already validated plugin_root before this point, which
    # is what preserves ensure-python3-exe-shim's ordering constraint (its
    # bash oracle sourced coordinator-trusted-root-guard.sh before doing
    # anything else; calling the native op after the same gate here keeps
    # that invariant true without re-sourcing anything). See `_NATIVE_LEGS`
    # module-level comment for why none of these three depend on glob
    # discovery or basename interception.
    for leg_name, native_entrypoint in _NATIVE_LEGS:
        try:
            rc = native_entrypoint(plugin_root, claude_klabauter_root)
        except Exception as exc:  # pragma: no cover - defensive parity with the OSError branch below
            # Review: code-reviewer (Finding 3) — distinct "raised" prefix so
            # operators can tell a native-leg crash apart from a clean
            # non-zero return (below) without reading code.
            print(f"[install-health] FAIL: {leg_name} raised: {exc}", file=sys.stderr)
            failures += 1
            continue
        if rc != 0:
            print(f"[install-health] FAIL: {leg_name} exit={rc}", file=sys.stderr)
            failures += 1

    # Residual extensibility hook for a hypothetical FUTURE foreign drop-in
    # with no native peer — resolved off claude_klabauter_root (coordinator_claude_klabauter_root()),
    # NOT plugin_root: this directory is claude-klabauter's own tree
    # (<claude_klabauter_root>/coordinator/bin/install-health/), not a coordinator-claude-side
    # surface, per the dual-anchor split (see module docstring).
    health_dir = os.path.join(claude_klabauter_root, "coordinator", "bin", "install-health")

    # Basenames of the three decoupled `_NATIVE_LEGS` entries' `.sh` names.
    # They already ran unconditionally above; if any were ever reintroduced
    # as a file in the drop-in directory, skip it here to avoid a double
    # execution rather than silently re-running it via subprocess.
    _decoupled_basenames = frozenset(f"{name}.sh" for name, _ in _NATIVE_LEGS)

    for script in sorted(glob.glob(os.path.join(health_dir, "*.sh"))) if os.path.isdir(health_dir) else []:
        if os.path.basename(script) in _decoupled_basenames:
            continue

        # Review: code-reviewer (Finding 4) — resolve_by_shebang now folds
        # the script into its return value unconditionally (matching
        # resolve_launchable's convention), so no caller-side discriminator
        # between a "prefix" shape and a "complete argv" shape is needed.
        launch_argv = resolve_by_shebang(script)
        try:
            rc = subprocess.call(launch_argv, **no_console_passthrough_kwargs())
        except OSError as exc:
            # Review: code-reviewer (Finding 1) — a resolved-but-nonexistent
            # interpreter (e.g. a shebang naming a version-pinned Python or
            # any name not on this machine's PATH) previously raised
            # unhandled out of subprocess.call, aborting the whole
            # install-health run instead of counting one script as failed —
            # contradicting this module's own "continue past sub-script
            # failures" negative-spec. Treat a spawn failure identically to
            # a nonzero exit.
            print(
                f"[install-health] FAIL: {os.path.basename(script)} {exc}",
                file=sys.stderr,
            )
            failures += 1
            continue
        if rc != 0:
            print(f"[install-health] FAIL: {os.path.basename(script)} exit={rc}", file=sys.stderr)
            failures += 1

    if failures > 0:
        print(
            f"[install-health] {failures} health script(s) failed; install is incomplete.",
            file=sys.stderr,
        )
        return 1

    return 0


# Review: code-reviewer (2026-07-17 Finding 3) — every sibling op module in this
# slice ends with a __main__ guard, making it directly CLI-runnable/testable as a
# script; this one lacked it, an inconsistency against the slice's own convention.
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], script_path=sys.argv[0]))
