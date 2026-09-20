"""
coordinator_core.ops.install_health_run — declared-launch install-health orchestrator.

Purpose: runs every install-health leg declared in `_NATIVE_LEGS`, in declared
order, continuing past individual failures, and aggregates a non-zero exit
iff any leg failed. Adding a new install-health leg is a `_NATIVE_LEGS` row
edit, never a directory drop: every leg's launch is STATED, not inferred from
a file's bytes, its filename, or a default. An undeclared file sitting in the
drop-in directory is refused by name, loud, and never launched — see
`_run_legs` for the refusal.

Dual-anchor discovery (2026-07-22, closes the plugin_root-coupling defect):
  DoE deleted its entire `coordinator/bin/install-health/` drop-in directory
  (the trio: `ensure-python3-exe-shim.sh`, `check-windows-ssh-binary.sh`,
  plus the claude-klabauter-live-root sourced-lib `coordinator-claude-klabauter-root.sh`) under a
  PM kill-first ruling — cross-repo/inbox/2026-07-22-claude-central-em-
  install-health-trio-deleted-kill-first.md. The drop-in directory now
  lives (if at all) under claude-klabauter's OWN tree
  (`<claude_klabauter_root>/coordinator/bin/install-health/`), alongside the
  orchestrator itself (`<claude_klabauter_root>/coordinator/bin/install-health-run.py`,
  a thin CLI trampoline over this module). Resolving EITHER off
  `CLAUDE_PLUGIN_ROOT`/`plugin_root` (the DoE-side invoking-harness root)
  was therefore silently wrong the moment DoE's directory was gone — the
  glob would look in a directory that no longer exists on that side at all,
  even for a still-live claude-klabauter-owned drop-in.

  Fix: `plugin_root` remains the anchor ONLY for the top-level trust gate
  (`_trusted_root`, below) — that check validates the invoking harness root
  itself, a DoE-side concept unrelated to where leg content lives. Every
  claude-klabauter-side surface (the drop-in directory, and the `seed-skill-overrides`
  helper lookup) instead resolves off `coordinator_engine_root()`
  (`coordinator_core.engine_root`) — the canonical claude-klabauter-live-root resolver
  (env var -> settings-home pointer file -> machine-local registry -> raise,
  never silent). This is the same dual-anchor split already applied
  elsewhere in this port (e.g. `coordinator_core.install.substrate`'s
  `_write_agent_forwarder` cmd-twin source repoint).

Native leg registry (`_NATIVE_LEGS`) — no globbing for claude-klabauter-owned legs:
  All six claude-klabauter-owned legs (`ensure_python3_exe_shim`,
  `check_windows_ssh_binary`, `seed_skill_overrides`,
  `check_bareword_path_provisioning`, `check_door_provenance`,
  `check_launch_chain_intact`) run UNCONDITIONALLY in `main()`,
  in-process, via an explicit registry — never discovered through
  a `bin/install-health/*.sh` glob-and-basename-intercept, and never
  requiring a bash veneer front door. This collapses the prior two-tier
  split (a `_NATIVE_PROBES` registry for DoE-deleted siblings, run
  unconditionally, plus a `_NATIVE_ENTRYPOINTS` glob-intercept map for
  `seed-skill-overrides` whose `.sh` sibling still existed) into one
  registry once `seed-skill-overrides`' own `.sh` sibling ALSO stopped being
  a reliable discovery anchor for the same directionality reason (DoE-side
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

  A hypothetical FUTURE foreign drop-in with no native peer is onboarded by
  adding a `DeclaredLaunch` row to `_NATIVE_LEGS` — its argv is stated there,
  never inferred from a file already sitting in the drop-in directory. Any
  `.sh`/`.py` file in `<claude_klabauter_root>/coordinator/bin/install-health/` whose
  stem is not a declared leg name, and whose path is not a declared
  `DeclaredLaunch.script`/`nt_launcher`, is refused by name (loud, not
  launched, not counted as a failure) — see `_run_legs`.

Spec backlink: cross-repo/inbox/2026-07-21-claude-central-em-dr079-doe-dispositions-and-install-health-defect.md
Spec backlink: cross-repo/inbox/2026-07-22-claude-central-em-install-health-trio-deleted-kill-first.md

Trust gate: the resolved plugin root is checked against the canonical
`coordinator_core.trusted_root_guard.is_trusted` (fail-loud call-site
shape — see that module for the full anchor list). Untrusted root is
fail-loud (exit 1), matching the original script's
own `--mode=fail-loud` call — install-health-run.sh is a REQUIRED install
step (`coordinator/scripts/install-maximalist.py` calls it via `run_required`),
not an advisory hook. `coordinator_engine_root()` failing to resolve is likewise fail-loud
(exit 1) — every leg below needs a resolved claude-klabauter root to run correctly
(the seed-skill-overrides helper lookup and the residual glob directory both
live under it), so an unresolvable root cannot be silently downgraded to
"no legs ran."

Port of: install-health-run.sh (DoE 290997c7, 2026-07-22)
Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee

Negative-spec (deliberately reproduced, not "fixed"):
  - No `-e`-equivalent bail on the first failing sub-script — the loop must
    continue past sub-script failures (mirrors `set -uo pipefail`, no `-e`,
    in the bash oracle).
  - Absent `bin/install-health/` dir is a VALID no-undeclared-files state
    (exit 0, not an error) — `_NATIVE_LEGS` always runs first regardless of
    whether that directory exists at all (see registry note above).
  - Sub-leg exit codes are aggregated only as a failure COUNT — the
    orchestrator's own exit code is 0 or 1, never the leg's raw rc.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from coordinator_core._settings_home import settings_home
from coordinator_core.install import door_install, door_route_signal
from coordinator_core.install._shared import env_overlay
from coordinator_core.install.engine_root_for_install import resolve_engine_root_for_install
from coordinator_core.install.settings_home_report import (
    _names_the_installer_gives_an_image,
    expected_forwarders,
)
from coordinator_core.install.shell_rc_guard import write_path_entry_guard_blocks
from coordinator_core.install.wrapper_onto_path import _on_path as _bin_dst_on_path
from coordinator_core.launchable import _is_windows
from coordinator_core.engine_root import coordinator_engine_root
from coordinator_core.ops import check_windows_ssh_binary, ensure_python3_exe_shim, seed_skill_overrides
from coordinator_core.trusted_root_guard import is_trusted as _trusted_root
from coordinator_core.warm.settings import is_warm_enabled
from coordinator_core.warm.supervisor import read_discovery_with_cause
from coordinator_core.win_portability import no_console_passthrough_kwargs


@dataclasses.dataclass(frozen=True)
class DeclaredLaunch:
    """A declared, out-of-process install-health leg's complete argv.

    Purpose: replace `resolve_by_shebang`'s sniff-the-bytes launch resolution
    with a stated one. Each field closes one corner `resolve_by_shebang`'s
    own docstring admitted it cut open -- see the design table in
    `docs/plans/2026-09-11-install-health-legs-declare-how-they-launch.md`
    (§ Design) for the one-to-one mapping.

    Negative-spec (deliberately NOT done here): no suffix of `script` is ever
    read to pick an interpreter; no shebang line is ever read; no probe for a
    same-stem `.cmd`/`.ps1` twin on disk; no `bash`/`sh`/`node` interpreter is
    ever produced by this class -- `interpreter` is a closed vocabulary of
    exactly one value.
    """

    script: str
    interpreter: str = "python"
    interpreter_flags: Tuple[str, ...] = ()
    nt_launcher: Optional[str] = None

    def __post_init__(self) -> None:
        if self.interpreter != "python":
            raise ValueError(
                f"DeclaredLaunch.interpreter must be 'python' (closed vocabulary); got {self.interpreter!r}"
            )
        for flag in self.interpreter_flags:
            if not flag.startswith("-"):
                raise ValueError(
                    f"DeclaredLaunch.interpreter_flags entries must start with '-'; got {flag!r}"
                )

    def argv(self, claude_klabauter_root: str) -> List[str]:
        """Return the complete, ready-to-spawn argv for this leg.

        On Windows, when `nt_launcher` is declared, that path IS the whole
        argv (never combined with `interpreter`/`script`) -- it is never
        probed for on disk, only ever used because it was declared.
        """
        if _is_windows() and self.nt_launcher is not None:
            return [os.path.join(claude_klabauter_root, self.nt_launcher)]
        return [
            sys.executable,
            *self.interpreter_flags,
            os.path.join(claude_klabauter_root, self.script),
        ]


# Ordered list of (display_name, entrypoint) for every claude-klabauter-owned
# install-health leg. `entrypoint` is one of two declared kinds: a callable
# ``(plugin_root, claude_klabauter_root) -> int`` (in-process, as every leg below is
# today), or a `DeclaredLaunch` (out-of-process, its complete argv stated
# rather than inferred). Every leg runs UNCONDITIONALLY in `main()` below —
# no dependency on any file existing in a drop-in directory, and no
# dependency on `bin/install-health/` existing at all. `display_name`
# is used only for log/failure messages — it is NOT looked up anywhere.
_LegEntrypoint = Union[Callable[[str, str], int], DeclaredLaunch]
#
# `seed-skill-overrides` needs `claude_klabauter_root` to locate its DoE-resident-
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
_NATIVE_LEGS: List[Tuple[str, _LegEntrypoint]] = [
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
    (
        "check-door-provenance",
        lambda plugin_root, claude_klabauter_root: check_door_provenance(plugin_root, claude_klabauter_root),
    ),
    (
        "check-door-route",
        lambda plugin_root, claude_klabauter_root: check_door_route(plugin_root, claude_klabauter_root),
    ),
    # LAST, DELIBERATELY. Every leg above can change what this one reads --
    # the door legs most of all -- so it runs after them and reports on the
    # settings-home the whole install actually left behind, not an
    # intermediate state.
    (
        "check-launch-chain-intact",
        lambda plugin_root, claude_klabauter_root: check_launch_chain_intact(plugin_root, claude_klabauter_root),
    ),
]

# Extensions the undeclared-drop-in refusal scans for in the drop-in
# directory. `.sh`/`.py` are the only extensions the retired glob-and-
# shebang contract ever ran, so a leg authored against that old contract is
# the only thing that can be silently orphaned by this change (see § Design,
# "Undeclared drop-ins", in the plan that retired the glob).
_DROP_IN_LEG_EXTENSIONS = (".sh", ".py")

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


def check_door_provenance(plugin_root: str, claude_klabauter_root: str) -> int:
    """Native install-health leg asserting the installed door's provenance
    sidecar still describes the binary sitting beside it.

    Delegates entirely to `door_install.verify_installed_provenance` -- see
    that function's own docstring for why the record's `image_sha256`
    field is the oracle rather than mtime. This leg only translates its
    seven-way verdict into this module's print register and return code:

      - `"ok"` -> 0.
      - `"stale"` -> 1. The sidecar and the binary agree with each other
        and the binary is still a build behind -- the shape that let a
        door image predating the `COORDINATOR_DOOR_STDIN_MODE` gate read
        as healthy here while `cross-repo-memo` hung on every invocation
        (2026-09-01). Same remediation as `"mismatch"`.
      - `"mismatch"` -> 1, printing both hashes plus a runnable
        remediation (`python scripts/setup.py`, never a slash command --
        cold-path remediation must name a runnable script).
      - `"unrecorded"` -> 0, NOTE only. A sidecar written before
        `image_sha256` existed cannot be checked, and failing every box
        whose door predates this change would fail the fleet, not the
        defect.
      - `"unverifiable"` -> 0, NOTE only, on the same reasoning: the
        checkout ships no readable prebuilt for this platform, so currency
        could not be ASKED. Reported rather than passed silently, because
        indistinguishable-from-verified is the defect this leg exists to
        catch.
      - `"absent"` -> 1. A door with no readable provenance at all is
        unverifiable, and that unverifiability IS the defect this leg
        exists to catch.
      - `"no-door"` -> 0, NOTE only. The door install is advisory in
        `scripts/setup.py`; its absence is a different leg's concern, not
        this one's failure.
    """
    del plugin_root, claude_klabauter_root

    bin_dst = settings_home() / "bin"
    verdict = door_install.verify_installed_provenance(bin_dst)

    if verdict.status == "ok":
        print(f"[door-provenance] {verdict.detail}")
        return 0
    if verdict.status in ("unrecorded", "unverifiable"):
        print(f"[door-provenance] NOTE: {verdict.detail}")
        return 0
    if verdict.status == "no-door":
        print(f"[door-provenance] NOTE: {verdict.detail}")
        return 0
    if verdict.status in ("mismatch", "stale"):
        print(f"[door-provenance] FAIL: {verdict.detail}", file=sys.stderr)
        print(
            "[door-provenance] remediation: run `python scripts/setup.py` "
            "to reinstall the door and its provenance sidecar together",
            file=sys.stderr,
        )
        return 1
    # "absent"
    print(f"[door-provenance] FAIL: {verdict.detail}", file=sys.stderr)
    return 1


#: `ping` is the same op `README-posix.md`'s own manual oracle invokes
#: (`./door ping`) and the same one `docs/research/2026-09-10-post-install-
#: door-routing-premises.md` (C1) used for every live measurement this leg's
#: verdict table is built on.
_DOOR_ROUTE_OP = "ping"

#: This leg owns its own timeout rather than inheriting
#: `door_route_signal._DOOR_TIMEOUT_SECS` (30s) -- it sits inside
#: `maximalist.py`'s required Phase 3 Step 1b, and a hung door must not hold
#: that phase for half a minute. Chosen so a genuine hang still fails fast
#: relative to the rest of the install, per C2's own BUDGET AND TIMEOUT
#: paragraph.
_DOOR_ROUTE_TIMEOUT_SECS = 5.0


def check_door_route(plugin_root: str, claude_klabauter_root: str) -> int:
    """Native install-health leg asking WHICH PATH SERVED the invocation,
    not merely whether the door answered.

    C1(d) (`docs/research/2026-09-10-post-install-door-routing-premises.md`)
    reproduced the F-022 memo's ordering (a warm listener resident BEFORE
    the door image was replaced under it, then a forwarder dispatched
    name-blind) and found the box's route stamp still reads `WARM_SERVER`
    -- not `IN_PROCESS` -- on a content-identical rebuild. Neither of the
    spec's two named candidate discriminators (name-fidelity mismatch,
    resident-image-predates-door-build) fired on that run. The
    discriminator C1(d) actually found and measured directly: the live
    install ships ~386 forwarder names as HARDLINKS to one shared inode,
    and a plain file write/replace at the door's own installed path (what
    `door_install_posix_build.build_or_advise` does) does not preserve that
    hardlink set -- `coordinator-invoke` alone moves to a new inode while
    every other forwarder name keeps serving the pre-rebuild one. This leg
    is therefore built on THAT discriminator (hardlink-set divergence,
    read via `door_install.audit_installed_image_currency` -- the same
    oracle `settings_home_report.check_settings_home`'s `door_image_stale`
    field already uses, not a second, independently-drifting reimplementation),
    per C1's decision rule instruction that C2 rewrites its verdict table
    around whatever (d) actually found. The route stamp itself demotes to a
    NOTE, printed for observability, never a verdict.

    AC2 (the hole): `repo_root` is resolved exactly once, via
    `engine_root_for_install.resolve_engine_root_for_install()` (C1(b)'s
    confirmed sink root, the same one `install_warm_door` already threads
    to both `read_door_route` and `run_cold_control_invocation` per that
    function's own docstring), and threaded to BOTH calls below so the
    write and the read land in one file.

    Verdict translation, in this module's existing print register:
      - No door installed -> 0, NOTE only (matches `check_door_provenance`'s
        "no-door" posture -- the door install is advisory, another leg's
        concern).
      - `repo_root` unresolvable (`resolve_engine_root_for_install()` finds
        neither a published nor a self-stamped root) -> 0, distinct
        `DISCRIMINATOR_UNAVAILABLE` NOTE -- there is no sink to read.
      - The door invocation reads back `UNRESOLVED` -> run
        `run_cold_control_invocation`; if that ALSO comes back
        `UNRESOLVED`, print a distinct `DISCRIMINATOR_UNAVAILABLE` NOTE and
        return 0 -- never folded into the fall-through's message or return
        code.
      - The forwarder-name hardlink-set audit cannot be run at all (the
        expected-forwarder derivation or the image-currency audit itself
        raises) -> 0, distinct `DISCRIMINATOR_UNAVAILABLE` NOTE.
      - No name in the audit reads `stale` (the discriminator does not
        fire) -> 0. The route stamp read above is printed as a NOTE, never
        consulted for the verdict.
      - At least one name reads `stale` (the discriminator fires) AND
        warmth is expected (`warm.settings.is_warm_enabled()` is true AND
        `warm.supervisor.read_discovery_with_cause()` reads
        `record_present` for this same `repo_root`) -> 1, with a runnable
        remediation naming a script, never a slash command (cold-path
        remediation rule; guard `coordinator/tests/
        test_cold_path_remediation_is_runnable.py`).
      - At least one name reads `stale` AND warmth is NOT expected (warm
        disabled, or no discovery record for this root) -> 0, NOTE only:
        an optional performance feature never fails an install.

    BUDGET AND TIMEOUT: 200ms process time for the whole leg, spawn count
    <= 2 (one door subprocess via `read_door_route`; one in-process control
    dispatch via `run_cold_control_invocation`, which spawns nothing). The
    door subprocess is bounded by `_DOOR_ROUTE_TIMEOUT_SECS` (5s), not
    `door_route_signal`'s own 30s default -- see that constant's docstring.
    On a door subprocess timeout, `read_door_route` never raises; the
    result reads back `UNRESOLVED` and falls through the branch above like
    any other unresolved read, which -- per that branch -- returns 0 at
    worst (a hung probe is a defect report about the probe, not a positive
    routing measurement, so it never returns 1).

    Does NOT add a routing check to `settings_home_report.check_settings_home`
    itself -- that report is presence-only by design and is read on paths
    where a door subprocess is not affordable; this leg only reuses its
    `audit_installed_image_currency` oracle, in-process, from its own call
    site.
    """
    del plugin_root

    bin_dst = settings_home() / "bin"
    if not door_install.is_door_installed(bin_dst):
        print("[door-route] NOTE: no door installed -- advisory, another leg's concern")
        return 0

    resolved_root = resolve_engine_root_for_install()
    if resolved_root.root is None:
        print(
            "[door-route] NOTE: DISCRIMINATOR_UNAVAILABLE -- no engine root resolved "
            f"({resolved_root.remediation})"
        )
        return 0
    repo_root = resolved_root.root

    door_path = bin_dst / door_install.DOOR_INSTALLED_NAME
    result = door_route_signal.read_door_route(
        door_path,
        _DOOR_ROUTE_OP,
        repo_root=repo_root,
        timeout=_DOOR_ROUTE_TIMEOUT_SECS,
    )

    route = result.route
    if route == door_route_signal.UNRESOLVED:
        control = door_route_signal.run_cold_control_invocation(_DOOR_ROUTE_OP, repo_root=repo_root)
        if control.route == door_route_signal.UNRESOLVED:
            print(
                "[door-route] NOTE: DISCRIMINATOR_UNAVAILABLE -- the op-latency sink "
                "is inert on this box; a door-route verdict cannot be read here"
            )
            return 0
        route = control.route
    print(f"[door-route] NOTE: route={route} (observational -- see hardlink-set audit below for the verdict)")

    try:
        expected = expected_forwarders(Path(claude_klabauter_root))
        audit = door_install.audit_installed_image_currency(
            bin_dst, _names_the_installer_gives_an_image(expected.keys(), bin_dst)
        )
    except (OSError, door_install.DoorInstallError) as exc:
        print(f"[door-route] NOTE: DISCRIMINATOR_UNAVAILABLE -- hardlink-set audit failed: {exc}")
        return 0

    if not audit.stale:
        return 0

    _, cause = read_discovery_with_cause(repo_root)
    warmth_expected = is_warm_enabled() and cause == "record_present"
    if not warmth_expected:
        print(
            "[door-route] NOTE: hardlink-set divergence on "
            f"{sorted(audit.stale)} -- warmth is not expected on this box, "
            "so an optional performance feature is not failing the install"
        )
        return 0

    print(
        "[door-route] FAIL: hardlink-set divergence -- "
        f"{sorted(audit.stale)} still serve a pre-rebuild image inode while "
        "the door itself moved to a new one",
        file=sys.stderr,
    )
    print(
        "[door-route] remediation: run `python scripts/setup.py` "
        "to reinstall the door and every hardlinked forwarder name together",
        file=sys.stderr,
    )
    return 1


#: The one launcher on the interactive chain, and the string that proves the
#: installed copy is still the trampoline rather than something wearing its
#: name. `claude-doe`'s entire job is to `exec claude --plugin-dir <clone>/
#: coordinator`; anything that cannot reach that line cannot start a session.
_LAUNCH_CHAIN_NAME = "claude-doe"
_LAUNCH_CHAIN_PROOF = "exec claude"
_LAUNCH_CHAIN_SOURCE = "claude-doe.py"
_LAUNCH_CHAIN_FORWARD = f'exec_cli("{_LAUNCH_CHAIN_SOURCE}")'


def check_launch_chain_intact(plugin_root: str, claude_klabauter_root: str) -> int:
    """Native install-health leg asserting the install did not just break the
    way a session is started.

    WHY THIS LEG EXISTS AND WHY IT IS ITS OWN. Every other leg here checks a
    thing the install was TRYING to do. This one checks the thing an install
    keeps doing BY ACCIDENT: `claude-doe` is an ordinary name in
    `coordinator/bin/`, so every roster, glob, allowlist and cutover that
    enumerates names has swept it up at least once, and each time the box
    lost its ability to start a session until someone noticed by failing to
    start one. The 2026-09-02 instance hardlinked the native door over it
    (fixed at the source in `door_install._EXEC_SHAPED_NAMES`); the failure
    before that had a different cause and the same symptom. A per-cause fix
    cannot close a class whose members keep arriving from new directions --
    what closes it is asserting the END STATE, from the same install that
    would have broken it, before the operator's next launch discovers it.

    TWO FAILURES, ONE MESSAGE. A compiled image under this name (magic bytes
    -- the door, or any future native launcher) and a readable file that
    never reaches its exec line are the same defect to the person who cannot
    start a session, so they report identically and remediate identically.

    ABSENT IS NOT THIS LEG'S FAILURE. `scripts/setup.py` installs the wrapper
    advisorily, and a settings-home that never had one is a different leg's
    concern -- the same posture `check_door_provenance` takes for "no-door".
    What this leg refuses is a launcher that EXISTS and cannot launch.

    No subprocess: the leg reads one file, or two when the installed one is
    the generated forwarder and the proof lives in the wrapper it execs. Running the trampoline to see
    whether it runs would put an interpreter start on an install-health leg
    to learn strictly less than its own bytes already say."""
    del plugin_root

    launcher = settings_home() / "bin" / _LAUNCH_CHAIN_NAME
    if not launcher.is_file():
        return 0

    body = launcher.read_bytes()
    if _LAUNCH_CHAIN_FORWARD.encode("utf-8") in body:
        # The substrate's generated forwarder, which install-substrate writes
        # before Step 3.5b lays the wrapper bytes over it; it launches by
        # exec'ing the engine's own wrapper, so that file carries the proof.
        try:
            body = (Path(claude_klabauter_root) / "coordinator" / "bin" / _LAUNCH_CHAIN_SOURCE).read_bytes()
        except OSError:
            body = b""
    if body.startswith(door_install.NATIVE_IMAGE_MAGIC):
        detail = "it is a compiled native image, not the Python trampoline"
    elif _LAUNCH_CHAIN_PROOF.encode("utf-8") not in body:
        detail = f"it never reaches its `{_LAUNCH_CHAIN_PROOF}` line"
    else:
        return 0

    print(
        f"[launch-chain] FAIL: {launcher} cannot start a session -- {detail}. "
        "This install replaced the one launcher the interactive chain runs.",
        file=sys.stderr,
    )
    print(
        "[launch-chain] remediation: run `python coordinator/bin/"
        "install-claude-doe-wrapper.py` from the engine clone to restore it",
        file=sys.stderr,
    )
    return 1


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
    for the whole process BEFORE any leg runs, exactly mirroring the DoE-side doc block's
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
        claude_klabauter_root = coordinator_engine_root()
    except RuntimeError as exc:
        print(f"ERROR: install-health-run.py: {exc}", file=sys.stderr)
        return 1

    # Scoped, not process-wide: a bare `os.environ["CHECK_ONLY"] = ...` here would
    # leak past this call for the life of the interpreter (2026-07-21
    # interpreter-global-state sweep) — every drop-in/native leg below still SEES
    # the identical env-var signal the DoE doc block's own `export CHECK_ONLY=1` /
    # `export CHECK_ONLY=` used to set, just scoped to this run.
    with env_overlay({"CHECK_ONLY": "1" if check_only else ""}):
        return _run_legs(plugin_root, claude_klabauter_root, script_path)


def _run_legs(plugin_root: str, claude_klabauter_root: str, script_path: Optional[str]) -> int:
    """Run each declared install-health leg, tallying failures.

    Isolation boundary — still intact, in one line: a `DeclaredLaunch` leg
    still runs as its own child process, so a crash in foreign code is
    contained exactly as it was before this leg's launch became declared;
    an in-process (callable) leg was in-process already. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    failures = 0

    # Every leg runs UNCONDITIONALLY here, in declared order, regardless of
    # whether bin/install-health/ exists under claude_klabauter_root at all. The trust
    # gate above has already validated plugin_root before this point, which
    # is what preserves ensure-python3-exe-shim's ordering constraint (its
    # bash oracle sourced coordinator-trusted-root-guard.sh before doing
    # anything else; calling the native op after the same gate here keeps
    # that invariant true without re-sourcing anything).
    declared_scripts: set = set()
    for leg_name, entrypoint in _NATIVE_LEGS:
        if isinstance(entrypoint, DeclaredLaunch):
            declared_scripts.add(entrypoint.script)
            if entrypoint.nt_launcher is not None:
                declared_scripts.add(entrypoint.nt_launcher)
            try:
                rc = subprocess.call(
                    [*entrypoint.argv(claude_klabauter_root)], **no_console_passthrough_kwargs()
                )
            except OSError as exc:
                print(f"[install-health] FAIL: {leg_name} {exc}", file=sys.stderr)
                failures += 1
                continue
            if rc != 0:
                print(f"[install-health] FAIL: {leg_name} exit={rc}", file=sys.stderr)
                failures += 1
            continue

        try:
            rc = entrypoint(plugin_root, claude_klabauter_root)
        except Exception as exc:  # pragma: no cover - defensive parity with the OSError branch above
            # Review: code-reviewer (Finding 3) — distinct "raised" prefix so
            # operators can tell a native-leg crash apart from a clean
            # non-zero return (below) without reading code.
            print(f"[install-health] FAIL: {leg_name} raised: {exc}", file=sys.stderr)
            failures += 1
            continue
        if rc != 0:
            print(f"[install-health] FAIL: {leg_name} exit={rc}", file=sys.stderr)
            failures += 1

    # Undeclared drop-ins — resolved off claude_klabauter_root (coordinator_engine_root()),
    # NOT plugin_root: this directory is claude-klabauter's own tree
    # (<claude_klabauter_root>/coordinator/bin/install-health/), not a DoE-side
    # surface, per the dual-anchor split (see module docstring). Listed once
    # (no spawn) and, for every `.sh`/`.py` file whose stem is not a declared
    # leg name and whose path is not a declared `script`/`nt_launcher`,
    # refused by name — loud, but NOT counted as a failure: one stray file
    # must not abort the whole install (see § Design, "Undeclared drop-ins").
    health_dir = os.path.join(claude_klabauter_root, "coordinator", "bin", "install-health")

    declared_names = frozenset(name for name, _ in _NATIVE_LEGS)

    if os.path.isdir(health_dir):
        for entry in sorted(os.scandir(health_dir), key=lambda e: e.name):
            if not entry.is_file():
                continue
            basename = entry.name
            stem, ext = os.path.splitext(basename)
            if ext not in _DROP_IN_LEG_EXTENSIONS:
                continue
            if stem in declared_names:
                continue
            if entry.path in declared_scripts or os.path.relpath(entry.path, claude_klabauter_root) in declared_scripts:
                continue
            print(
                f"[install-health] REFUSED: {basename} is not a declared leg -- "
                "declare it in install_health_run._NATIVE_LEGS",
                file=sys.stderr,
            )

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
