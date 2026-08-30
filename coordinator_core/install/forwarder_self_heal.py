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
exact same writer (`_write_agent_forwarder`, or `_cut_over_to_native_door`
for a door-eligible name) `substrate.py`'s own install path uses, so there
is no second, drift-prone forwarder-body implementation.
(`_write_agent_cmd_forwarder`, this module's former second writer, is
deleted -- 91771f631d, "the cmd forwarder dies": every name gets the
native door image or the bare-Python forwarder now, never a `.cmd`.)

Why not just invoke the full `substrate.run()` when a gap is detected
--------------------------------------------------------------------
Considered and rejected. `run()` bundles far more than the forwarder loop:
a hardware audit that spawns a subprocess and writes `hardware.local.toml`
(Step 3h), an `fnm` brew/curl third-party installer step, and — the
decisive one — a legacy `.coordinator-venv` HEALTH PROBE
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
`_write_agent_forwarder` writes via a plain in-place `Path.write_text` —
not atomic-temp-and-rename — so two processes
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
import traceback
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

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            _self_heal_forwarders_inner()
    except Exception as exc:  # noqa: BLE001 -- the swallow IS the contract; see below
        _record_failure(exc, out.getvalue(), err.getvalue())
        return


#: Failure ledger this module appends to when its swallowed work raises.
#: Under `<settings-home>/state/`, never the repo: this writes on a machine
#: whose engine may be any clone, and the failure is a property of the BOX.
_FAILURE_LEDGER_RELATIVE = ("state", "forwarder-self-heal-failures.jsonl")


def _record_failure(exc: BaseException, captured_stdout: str, captured_stderr: str) -> None:
    """Appends one JSON line describing a swallowed self-heal failure.

    WHY THIS EXISTS, AND WHY IT IS A FILE RATHER THAN A PRINT. The swallow
    above is deliberate and stays -- a session must not fail to start because
    a convenience refresh could not run, and the PM ruling this module
    implements ("don't warn about it, just install it") forbids putting the
    advisory back on the boot path. But silence-to-the-operator was
    implemented as silence-to-EVERYONE: the captured stdout/stderr was
    discarded and the exception dropped with a bare `return`, leaving a
    function that WRITES TO A SHARED INSTALL SURFACE and, on failure, leaves
    no evidence anywhere on the machine that it ran at all.

    That is not a hypothetical cost. On 2026-08-30 the installed door image
    at `<settings-home>/bin/` was replaced without its provenance sidecar or
    engine-root sidecar being updated alongside it -- a partial write of the
    exact shape a failure part-way through `door_install.install_door` would
    leave. Four separate instruments (both sidecars' mtimes, every session
    transcript on the box, and the NTFS USN journal) were unable to name the
    writer, because this path is the one door-touching caller on a session
    boot that produces no output, no log, and no exit code. The provenance
    defect itself is fixed elsewhere (`build.py :: write_provenance`'s
    `image_sha256`); this closes the attribution gap that made it
    unhuntable.

    Costs nothing on the clean path: no ledger write happens unless the work
    actually raised, which is the overwhelmingly common case's opposite. Any
    failure to write the ledger is itself swallowed -- an instrument that can
    fail a session boot is worse than no instrument.
    """
    import json
    import os
    import time

    try:
        from coordinator_core._settings_home import settings_home

        ledger = Path(settings_home()).joinpath(*_FAILURE_LEDGER_RELATIVE)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pid": os.getpid(),
            "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
            "cwd": os.getcwd(),
            "exception": repr(exc),
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-4000:],
            "captured_stdout": captured_stdout[-2000:],
            "captured_stderr": captured_stderr[-2000:],
        }
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 -- an instrument must never fail a boot
        return


def _self_heal_forwarders_inner() -> None:
    from coordinator_core._settings_home import settings_home
    from coordinator_core.locked_write import LockTimeout, held_lock
    from coordinator_core.install.substrate import (
        _derive_agent_helper_target_map,
        _cut_over_to_native_door,
        _write_agent_forwarder,
        _union_native_forwarder_manifest,
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

    native_written: "set[str]" = set()
    try:
        with held_lock(bin_dst, holder_label="forwarder-self-heal", timeout=2.0):
            for name, target in sorted(missing.items()):
                if _installed_forwarder_present(bin_dst, name):
                    continue
                if door_root is not None:
                    if _cut_over_to_native_door(
                        name, bin_dst, False, engine_root=door_root
                    ) is not None:
                        native_written.add(name)
                        continue
                # Doorless root: the bare Python forwarder is all that can be
                # written. Correct on POSIX, and on Windows it is the same
                # degraded shape the installer leaves for an unstamped root --
                # not bare-name resolvable, and not papered over with a `.cmd`.
                _write_agent_forwarder(name, bin_dst / name, False, target=target)
            # Read-union-write, under the SAME held_lock this loop already
            # holds -- see `_union_native_forwarder_manifest`'s docstring for
            # why this must be a union and not `substrate.py`'s full-install
            # overwrite writer: this loop only ever sees the names missing
            # THIS invocation, never the complete set.
            _union_native_forwarder_manifest(bin_dst, native_written)
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
