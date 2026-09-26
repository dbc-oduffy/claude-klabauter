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
Considered and rejected — but not for the reason this paragraph used to
give. It used to name a legacy `.coordinator-venv` HEALTH PROBE AND DELETE
plus a venv REBUILD (Step C10a-3,
`coordinator_core.install.ensure_venv.ensure_coordinator_venv`) as "the
decisive one" — that stopped being true once `run()` grew
`allow_venv_fallback: bool = False`
(docs/plans/2026-08-18-retire-coordinator-venv.md chunk C4, AC5). Step
C10a-3 is gated behind that flag (`substrate.py`, the `if not
allow_venv_fallback:` guard ahead of the venv-rebuild call): a default
`run()` prints "Step C10a-3 (venv rebuild) skipped" and returns without ever
reaching `ensure_coordinator_venv`. It is therefore NOT a reason to avoid
`run()` — only `--allow-venv-fallback` break-glass callers ever hit it, and
this module never passes that flag.

On 2026-08-30 an EM refused a PM-requested `install-substrate` run by
quoting this paragraph's old claim; the run turned out to be both safe and
necessary. See docs/plans/2026-08-18-retire-coordinator-venv.md.

The remaining reasons still stand on their own. `run()` bundles far more
than the forwarder loop: a hardware audit that spawns a subprocess and
writes `hardware.local.toml` (Step 3h), an `fnm` brew/curl third-party
installer step, and a hard requirement on `CLAUDE_PLUGIN_ROOT` for its
DoE-side surfaces that this module's forwarder-only concern needs no part
of, since `coordinator/bin/`/`coordinator/lib/` resolve off
`coordinator_core.engine_root.coordinator_engine_root()` alone. Any one of
those is enough reason for this module to stay narrow.

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

from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="forwarder-self-heal",
    source_module="coordinator_core.install.forwarder_self_heal",
    clauses=(
        ShapedClause(
            discovered_by="_self_heal_forwarders_inner (diff against coordinator/bin/)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<settings_home>/bin/<name>",
                reason="missing agent/skill forwarder, native door image or bare-Python fallback",
            ),
        ),
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<settings_home>/bin/_native-forwarder-manifest.json",
                    reason="read-union-write: records which of clause 1's writes were native door images",
                ),
            ),
        ),
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<settings_home>/state/forwarder-self-heal-failures.jsonl",
                    reason="_record_failure: appends one JSON line describing a swallowed self-heal failure",
                ),
            ),
        ),
    ),
)

GENERATES = []


def self_heal_forwarders() -> None:
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            _self_heal_forwarders_inner()
    except Exception as exc:  # noqa: BLE001 -- the swallow IS the contract; see below
        _record_failure(exc, out.getvalue(), err.getvalue())
        return


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
        _NO_LAUNCHER_FOR_THIS_NAME,
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
        return

    target_map = _derive_agent_helper_target_map(agent_bin)
    if not target_map:
        return

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
                    cutover = _cut_over_to_native_door(
                        name, bin_dst, False, engine_root=door_root
                    )
                    if cutover is _NO_LAUNCHER_FOR_THIS_NAME:
                        continue
                    if cutover is not None:
                        native_written.add(name)
                        continue
                _write_agent_forwarder(name, bin_dst / name, False, target=target)
            _union_native_forwarder_manifest(bin_dst, native_written)
    except LockTimeout:
        return


def _installed_forwarder_present(bin_dst: Path, name: str) -> bool:
    from coordinator_core.install.door_install import named_forwarder_path

    if sys.platform == "win32":
        return named_forwarder_path(bin_dst, name).exists()
    return named_forwarder_path(bin_dst, name).exists() or (bin_dst / name).exists()
