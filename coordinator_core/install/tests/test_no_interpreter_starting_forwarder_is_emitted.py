"""The negative that outlives this plan: no *agent-helper forwarder* install
pass emits a launcher whose PRIMARY PATH starts an interpreter (DR-365,
docs/decisions/DR-365-ruling-2-governs-every-managed-launcher-class.md;
docs/plans/2026-08-26-every-forwarder-that-can-reach-the-door-does.md C12).

Scope, stated plainly: this guard calls `_write_agent_helper_forwarders`
directly, not `_install_bin_resolvers`/`run()`. The five hand-authored
static-family `.cmd` shims that `_install_bin_resolvers` also drives
(machine-local.cmd, coordinator-settings-home.cmd, platform-localize.cmd,
resolve-coordinator-clone.cmd, claude-home.cmd) are therefore NOT covered
by this guard -- they are a distinct writer family with their own
primary-path shape, out of scope for this test on purpose.

That exclusion was never ratified for any of the five names individually
(DR-365 condemns exactly this shape), and it remains unratified. It was
briefly narrowed to four on 2026-08-30 -- `claude-home` removed, on the
premise that C5 had stopped `_CH_FAMILY_FILES` emitting `claude-home.cmd`.
That cutover was backed out the same day (two writers of one
`<settings-home>/bin/claude-home` path; see
`test_static_families_reach_the_door.py`'s module docstring), the `.cmd`
entry came back, and so did this name. THE GUARD FOLLOWS THE GENERATOR AND
NEVER LEADS IT: do not narrow this list again until `_CH_FAMILY_FILES`
actually stops writing the entry. The remaining four are routed to their
owning repos by memo and are not this test's to widen.

`example-game-repo-control` was never a member of this list at all. Unlike the other
five it was not an unratified exclusion -- it was simply never considered.
Recorded because those are different defects and only the first is what
DR-365 condemns.

Worded PRIMARY PATH, deliberately, not a flat "never starts an
interpreter": the door itself degrades to that name's own Python CLI on a
warm miss, by PM ruling (DR-367, "cold succeeds, loudly") -- a flat
assertion would red on the very artifact this plan installs 393 copies of.
A native image whose PRIMARY path is native and whose DEGRADE starts an
interpreter satisfies DR-365; a `.cmd`/`.ps1` trampoline, whose only path
IS an interpreter start, does not.

Two legs:

  1. STRUCTURAL -- the two writers this property used to depend on
     (`_write_agent_cmd_forwarder`, `_write_agent_ps1_forwarder`) are gone
     outright, not merely unreachable. A regression that reintroduces
     either (even dead code, never called) is exactly the shape that let
     393 `.cmd` launchers and a full `.ps1` leg accumulate silently before
     this plan (see the plan's Problem section, "Why nobody noticed").
  2. BEHAVIORAL -- a real (non-check-only) forwarder-write pass, over BOTH
     a door-eligible name and a doorless/non-eligible one, leaves no
     `.cmd`/`.ps1` file anywhere under the destination directory.

Spec backlink: docs/plans/2026-08-26-every-forwarder-that-can-reach-the-door-does.md, C12.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.install import door_install, substrate

pytestmark = [pytest.mark.cadence]


def _stamp_engine_root(root: Path, *entrypoints: str) -> None:
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")

    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in entrypoints:
        (bin_dir / f"{name}.py").write_text("def main(argv):\n    return 0\n", encoding="utf-8")


def _skip_if_no_prebuilt() -> None:
    # Skipping here drops the guard's entire BEHAVIORAL leg on a
    # platform/checkout with no committed prebuilt door, leaving only leg 1's
    # structural (hasattr) coverage in place.
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")


# --- Leg 1: structural -- the two condemned writers are gone, not merely
# unreachable. A reintroduction (even unused) is the drift this test exists
# to catch before it becomes a live emission again. ------------------------


def test_cmd_and_ps1_forwarder_writers_do_not_exist():
    assert not hasattr(substrate, "_write_agent_cmd_forwarder"), (
        "_write_agent_cmd_forwarder reappeared on coordinator_core.install.substrate. "
        "DR-365 condemns the .cmd launcher class outright (deleted 91771f631d, "
        "'the cmd forwarder dies') -- reintroducing this writer, even as dead code, "
        "is the exact silent-accumulation shape this plan exists to close."
    )
    assert not hasattr(substrate, "_write_agent_ps1_forwarder"), (
        "_write_agent_ps1_forwarder reappeared on coordinator_core.install.substrate. "
        "DR-365 condemns the .ps1 launcher class outright (deleted with C12 of "
        "docs/plans/2026-08-26-every-forwarder-that-can-reach-the-door-does.md) -- "
        "reintroducing this writer, even as dead code, is the exact silent-"
        "accumulation shape this plan exists to close."
    )
    assert not hasattr(substrate, "_emit_and_verify_ps1_forwarders")


def test_policy_gate_module_does_not_exist():
    """Per DR-365, `policy_gate` goes with the `.ps1` leg -- once nothing
    emits `.ps1` launchers the gate has nothing to gate (C12's own body)."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("coordinator_core.install.policy_gate")


# --- Leg 2: behavioral -- a real write pass over a mixed eligible/doorless
# population leaves no .cmd/.ps1 anywhere under the destination. ----------


def _all_cmd_or_ps1_files(bin_dst: Path) -> "list[Path]":
    return sorted(
        p for p in bin_dst.rglob("*")
        if p.is_file() and p.suffix in (".cmd", ".ps1")
    )


def test_no_cmd_or_ps1_emitted_for_a_mixed_eligible_and_doorless_population(tmp_path):
    """A single write pass over BOTH a door-eligible name (real prebuilt
    door, stamped engine root) and a doorless/non-eligible one (no engine
    stamp) -- neither leaves a `.cmd` or `.ps1` behind. The doorless name's
    PRIMARY path is the bare extensionless Python forwarder (not itself an
    interpreter-starting LAUNCHER on Windows -- it is not PATHEXT-resolvable
    at all without a native image; see `_write_agent_helper_forwarders`'s
    own docstring for that named, accepted consequence of an unstamped
    root), never a `.cmd`/`.ps1` trampoline."""
    _skip_if_no_prebuilt()
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root, "cross-repo-memo")
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir(parents=True, exist_ok=True)

    target_map = {
        "cross-repo-memo": "cross-repo-memo",
        "some-doorless-cli": "some-doorless-cli",
    }

    substrate._write_agent_helper_forwarders(
        target_map, bin_dst, False,
        engine_root=engine_root,
    )

    leftover = _all_cmd_or_ps1_files(bin_dst)
    assert not leftover, (
        f"install pass emitted interpreter-starting launcher(s) on their "
        f"PRIMARY path: {leftover}. DR-365 permits no .cmd/.ps1 output from "
        f"the generator, cutover or not."
    )
