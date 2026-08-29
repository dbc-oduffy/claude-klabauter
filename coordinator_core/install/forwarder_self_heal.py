"""
coordinator_core.install.forwarder_self_heal — missing-agent-forwarder self-heal.

Purpose: `coordinator_core.install.substrate.run()` (the full install
ceremony) is the only writer of `<settings-home>/bin/`'s agent/skill
bare-name forwarders, and nothing invokes it outside a human/CI-driven
`python3 -m coordinator_core.install.substrate` run. On a dev box that runs
the engine straight out of a live working tree, a CLI added to
`coordinator/bin/` therefore has NO installed forwarder until someone
remembers to re-run the installer by hand — which nobody does. Ten CLIs
(`percolate-push` among them) drifted this way with nothing catching it.

This module is the narrow fix: a cheap, silent, best-effort check — call it
every session boot — that closes ONLY the missing-forwarder gap, using the
exact same two writers (`_write_agent_forwarder`/`_write_agent_cmd_forwarder`)
`substrate.py`'s own install path uses, so there is no second, drift-prone
forwarder-body implementation.

Why not just invoke the full `substrate.run()` when a gap is detected
--------------------------------------------------------------------
Considered and rejected. `run()` bundles far more than the forwarder loop:
a hardware audit that spawns a subprocess and writes `hardware.local.toml`
(Step 3h), an `fnm` brew/curl third-party installer step, a PowerShell
execution-policy gate that spawns `powershell.exe` (the `.ps1` forwarder
leg), and — the decisive one — a legacy `.coordinator-venv` HEALTH PROBE
AND DELETE plus a venv REBUILD (Step C10a-3,
`coordinator_core.install.ensure_venv.ensure_coordinator_venv`). Firing
that unattended, even rarely, risks racing another concurrent session's
live use of the same shared venv on a machine that runs 50-70 concurrent
LLM sessions (CLAUDE.md § Load norm) — a half-rebuilt or briefly-absent
venv breaks every other session on the box, which is a far worse failure
mode than the drift this module fixes. `run()` also hard-requires
`CLAUDE_PLUGIN_ROOT` for its DoE-side surfaces; this module's forwarder-only
concern needs no such thing, since `coordinator/bin/`/`coordinator/lib/`
resolve off `coordinator_core.engine_root.coordinator_engine_root()` alone.

So: extract (`substrate._write_agent_helper_forwarders`, a pure refactor of
`substrate.py` Step 3b) rather than invoke the whole installer.

Concurrency
-----------
`_write_agent_forwarder`/`_write_agent_cmd_forwarder` write via a plain
in-place `Path.write_text` — not atomic-temp-and-rename — so two processes
writing the SAME destination concurrently can interleave and leave a
truncated/half-written file on disk. This module therefore:

  1. Computes the derived-vs-installed diff with NO lock held at all — pure
     `Path.iterdir()`/`Path.exists()` reads, cheap, and (by design) almost
     always empty, since forwarders only go missing when a new CLI lands in
     `coordinator/bin/`.
  2. Only when that diff is non-empty does it acquire
     `coordinator_core.locked_write.held_lock` on `<settings-home>/bin`
     (the existing cross-process advisory-lock primitive — see that
     module's docstring; never a bespoke lock) with a short timeout, so a
     dozen sessions racing a genuine gap serialise onto one writer instead
     of interleaving. A failure to acquire within the timeout degrades to
     doing nothing this session — the gap is re-detected (and likely
     already closed by the winner) next session.
  3. Re-checks each individual entry's existence AFTER acquiring the lock
     (double-checked) before writing it, so a session that lost the race
     to acquire does not redundantly rewrite a forwarder a concurrent
     winner already wrote.

Never blocks or fails a session: every resolution, permission, or IO step
is wrapped so any failure degrades to a silent no-op. Silent on success
too — this replaces a removed SessionStart warning, not a new one; see
`self_heal_forwarders`'s own docstring.

Spec backlink: cross-repo/inbox (percolate-push / ten-missing-forwarders
drift incident, 2026-08-14).
"""

from __future__ import annotations

import sys
from pathlib import Path


def self_heal_forwarders() -> None:
    """Best-effort, silent, non-blocking: write any agent-helper forwarder
    missing from `<settings-home>/bin/` relative to claude-klabauter's own
    `coordinator/bin/` directory listing. Writes ONLY the missing entries —
    never rewrites a forwarder that already exists, and never touches the
    `.ps1` leg, platform-localize, ml/ch families, or the orphan sweep (all
    out of scope; see module docstring).

    Contract: returns `None` unconditionally. Never raises, never prints,
    never exits non-zero. A resolution failure (no claude-klabauter root, no
    settings-home, a permissions error, a lock timeout, anything) is
    indistinguishable from "nothing was missing" to the caller — by design,
    since this is a self-heal, not a diagnostic surface (PM ruling: "don't
    warn about it, just install it").

    The stdout/stderr capture is load-bearing, not belt-and-braces: the
    installer internals this module reuses print on their own account, and
    at least one of them fires on the CLEAN path. `_derive_agent_helper_
    target_map` emits an `[install-substrate] WARNING: duplicate CLI pair`
    line whenever `coordinator/bin/` holds an extensionless CLI beside its
    `.py` twin — a condition that is true today and independent of whether
    any forwarder is missing. Without this capture, wiring the self-heal
    into session boot would print that warning on EVERY session start,
    reinstating at boot exactly the class of advisory the PM ruling removed
    from boot (and which this module exists to make unnecessary). Captured
    output is discarded, never inspected: a self-heal that cannot fix
    something stays silent about it, and the real installer's own fail-loud
    path remains the surface for that.
    """
    import contextlib
    import io

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            _self_heal_forwarders_inner()
    except Exception:
        return


def _self_heal_forwarders_inner() -> None:
    from coordinator_core._settings_home import settings_home
    from coordinator_core.locked_write import LockTimeout, held_lock
    from coordinator_core.install.substrate import (
        _derive_agent_helper_target_map,
        _cut_over_to_native_door,
        _write_agent_forwarder,
    )
    from coordinator_core.engine_root import coordinator_engine_root_with_class

    _claude_klabauter_root_str, _resolution_class = coordinator_engine_root_with_class()
    claude_klabauter_root = Path(_claude_klabauter_root_str)
    agent_bin = claude_klabauter_root / "coordinator" / "bin"
    bin_dst = settings_home() / "bin"

    if not agent_bin.is_dir() or not bin_dst.is_dir():
        # Nowhere to derive from, or nowhere to install into (e.g. no
        # install has ever run) -- not this module's job to bootstrap a
        # fresh settings-home; a genuinely fresh box gets its forwarders
        # from the real installer, same as always.
        return

    target_map = _derive_agent_helper_target_map(agent_bin)
    if not target_map:
        return

    # HEALS THE NATIVE DOOR IMAGE, NEVER A `.cmd` (PM ruling 2026-08-29 --
    # one native entrypoint per platform). This path used to regenerate the
    # `.py`/`.cmd` pair, which made it a SECOND producer of the interpreter
    # trampolines the installer had already stopped emitting: a rename or a
    # sweep would drop a `.cmd`, and the next session boot silently put it
    # back. That is why the live box carried more `.cmd` files (399) than the
    # generator even knows names for (384). Healing the same artifact the
    # installer writes is the only shape that does not drift back.
    from coordinator_core.warm.engine_root import is_engine_root

    door_root = claude_klabauter_root if is_engine_root(claude_klabauter_root) else None

    missing: "dict[str, str]" = {}
    for name, target in target_map.items():
        if not _installed_forwarder_present(bin_dst, name):
            missing[name] = target

    if not missing:
        return

    try:
        with held_lock(bin_dst, holder_label="forwarder-self-heal", timeout=2.0):
            for name, target in sorted(missing.items()):
                if _installed_forwarder_present(bin_dst, name):
                    continue
                if door_root is not None:
                    if _cut_over_to_native_door(
                        name, bin_dst, False, engine_root=door_root
                    ) is not None:
                        continue
                # Doorless root: the bare Python forwarder is all that can be
                # written. Correct on POSIX, and on Windows it is the same
                # degraded shape the installer leaves for an unstamped root --
                # not bare-name resolvable, and not papered over with a `.cmd`.
                _write_agent_forwarder(name, bin_dst / name, False, target=target)
    except LockTimeout:
        return


def _installed_forwarder_present(bin_dst: Path, name: str) -> bool:
    """True if `name` already has a forwarder this platform can actually
    REACH by bare name -- which is a stricter question than "a file called
    `name` exists".

    On Windows only `named_forwarder_path` (`name.exe`) counts. The
    extensionless `name` sitting beside it is the POSIX Python forwarder,
    carried onto this box by a settings-home synced from a Mac, and PATHEXT
    gives it no bare-name resolution at all -- treating its presence as
    coverage is what would let this heal skip every name on a Windows box
    that has such a sync (this one has ~400 of them). On POSIX the two paths
    are the same path by construction, so the check collapses to one stat.
    """
    from coordinator_core.install.door_install import named_forwarder_path

    if sys.platform == "win32":
        return named_forwarder_path(bin_dst, name).exists()
    return named_forwarder_path(bin_dst, name).exists() or (bin_dst / name).exists()
