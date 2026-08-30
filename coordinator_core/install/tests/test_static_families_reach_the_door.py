"""What is and is not true about `claude-home` and the warm door (C5).

Spec backlink: docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-are-thoroughly-dead.md, chunk C5

`claude-home` is the ONE name of C5's static-family six that claude-klabauter owns
outright (`coordinator/lib/claude-home/`). The other five are owned by
coordinator-claude and the example-game-repo plugin and are routed to those owners by
memo -- see the plan body's ownership table. They are not exercised here.

WHAT SHIPPED: the engine entrypoint. `coordinator/bin/claude-home.py` exists,
which is the only path any door leg resolves, so the name COULD be served by
the door once an owner is settled for `<settings-home>/bin/claude-home`.

WHAT DID NOT SHIP, AND MUST NOT BE QUIETLY RE-ATTEMPTED: the cutover itself.
An attempt to gate `_AGENT_HELPER_RESERVED_NAMES` to POSIX-only -- freeing the
generic agent-helper scan to derive `claude-home` on Windows and cut it over
-- was backed out the same day. Two findings killed it, and the last test in
this file exists to make a re-attempt fail loudly rather than silently:

  1. TWO WRITERS, ONE PATH. `ch_family` writes extensionless `claude-home`
     into `<settings-home>/bin` on EVERY platform, Windows included.
     Un-reserving the name adds the agent-helper loop as a second writer of
     that same path (ch_family runs first, the helper loop after), and BOTH
     of the loop's branches take ch_family's files:
       - UNSTAMPED engine root -- the common case, not the edge:
         `_cut_over_to_native_door` returns None, the fallback writes an
         extensionless Python forwarder at `bin_dst/claude-home`, and
         ch_family's shim is overwritten and loses its exec bit. This is the
         branch the failing test below actually observed.
       - STAMPED root: the cutover succeeds and calls
         `door_install.remove_superseded_python_forwarders`, which on Windows
         DELETES bare `claude-home` and `claude-home.cmd`. Those are
         ch_family's static-family files; that function has no exemption for
         them and no knowledge that another family owns them.
  2. RETIRING THE `.cmd` LEAVES NO NATIVE-WINDOWS LEG ON AN UNSTAMPED ROOT.
     An earlier version of this docstring claimed instead that the
     agent-helper loop "writes a `.cmd` straight back". THAT WAS FALSE and is
     retracted -- `_cut_over_to_native_door` is called FIRST and `continue`s
     on success (so the Python pair is never written for a cut-over name),
     and `_write_agent_cmd_forwarder` was deleted outright on 2026-08-29
     (gravestone in `substrate.py`, PM ruling: one native entrypoint per
     platform). No `.cmd` is generated for any derived name, ever. The real
     hazard is the inverse: the fallback's extensionless forwarder is not
     executable via PATHEXT in cmd or PowerShell, so with `claude-home.cmd`
     retired the name leaves native-Windows PATH entirely on every unstamped
     install. Correction supplied by an eng-director review, verified against
     source before being written here.

THE OWNERSHIP RULE, ruled 2026-08-30 (eng-director) — ONE PATH, ONE OWNER.
The name was never the unit of ownership; the PATH is. `named_forwarder_path`
returns `claude-home.exe` on Windows (a DIFFERENT file from the shim) and the
bare `claude-home` on POSIX (the SAME file). So:

  - `<settings-home>/bin/claude-home` (extensionless) -- `ch_family` forever,
    both platforms. It is the POSIX leg and the Windows git-bash leg. On
    native Windows it is already inert: cmd and PowerShell cannot execute an
    extensionless file, which is why `claude-home.cmd` is the leg that
    actually runs there today.
  - `<settings-home>/bin/claude-home.exe` -- the door, Windows only, additive.
  - `claude-home` STAYS reserved from the generic derivation. A Windows
    cutover, when it happens, is an explicit door-only write of the `.exe`
    that touches nothing else. Un-reservation is the wrong lever: it buys the
    cutover, the fallback, and the removal sweep as one bundle, and only the
    first is wanted.

That is not a per-platform split of one file -- it is two files with one
owner each, which is what makes it a rule rather than two bugs agreeing.

TWO PREREQUISITES before any re-attempt, both defects on their own merits and
neither specific to `claude-home`: a static-family exemption in
`remove_superseded_python_forwarders` (it must not remove a name in
`_static_bin_family_names()`), and cutover-or-defer in
`_cut_over_to_native_door` (None must mean "do nothing" for a name whose
fallback a static family already owns, not "write the Python forwarder").

Negative-spec: this file asserts nothing about a door image being installed
for `claude-home` on either platform, because none is. It pins the entrypoint,
and it pins the reservation that keeps `ch_family` the single writer.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.install import door_install, substrate

pytestmark = [pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_claude_home_py_entrypoint_exists():
    entrypoint = _REPO_ROOT / "coordinator" / "bin" / "claude-home.py"
    assert entrypoint.is_file(), (
        "coordinator/bin/claude-home.py must exist -- it is the engine "
        "entrypoint every door leg (_resolve_entrypoint_script, "
        "door.c :: fall_through, door_posix.c) resolves for the name "
        "'claude-home'."
    )


def test_claude_home_resolves_under_the_same_rule_the_door_uses():
    """`engine_carries_entrypoint_script` is the generator-side stand-in for
    the door's own resolution rule (`<engine_root>/coordinator/bin/
    <name>.py`) -- pinning it against this repo's own tree (self-as-engine)
    is the same check `launcher_is_installable`/`_cut_over_to_native_door`
    perform against a real published engine root at install time."""
    assert door_install.engine_carries_entrypoint_script(_REPO_ROOT, "claude-home"), (
        "the door's own resolution rule (<engine_root>/coordinator/bin/"
        "<name>.py) does not find claude-home.py -- the entrypoint is not "
        "where the door would look for it."
    )


def test_ch_family_still_owns_the_cmd_leg():
    """The `.cmd` retirement was backed out with the cutover. While
    `claude-home` stays reserved, `ch_family` is the only writer of this
    name, and on Windows its `.cmd` is the only leg that runs -- the
    extensionless sibling is not executable through PATHEXT."""
    names = [name for name, _exec_bit in substrate._CH_FAMILY_FILES]
    for expected in ("claude-home", "_claude_home.py", "claude-home.cmd"):
        assert expected in names, (
            f"_CH_FAMILY_FILES is missing {expected!r}. If this was removed to "
            f"cut the name over to the door, read this module's docstring "
            f"first: dropping the .cmd does not make the door the sole Windows "
            f"leg, it hands the name to the agent-helper loop instead."
        )


def test_claude_home_stays_reserved_on_every_platform():
    """The reservation is what keeps `ch_family` the SINGLE writer of
    `<settings-home>/bin/claude-home`. Gating it per-OS is the specific
    re-attempt this test exists to catch -- see finding 1 in the module
    docstring, which `test_install_substrate_uninstall_legs.py ::
    test_substrate_run_success_path_dual_anchor_populated_tree` observes as
    a lost exec bit on the installed shim."""
    assert "claude-home" in substrate._AGENT_HELPER_RESERVED_NAMES, (
        "claude-home is no longer reserved from the agent-helper derivation. "
        "That makes the agent-helper loop a second writer of the same "
        "<settings-home>/bin/claude-home path ch_family already writes, and "
        "the forwarder stub wins -- replacing a working executable shim with "
        "a non-executable one. A real cutover has to settle who owns that "
        "path BEFORE the name becomes derivable."
    )


def test_reserved_name_is_not_derived_even_with_the_entrypoint_present(tmp_path):
    """The end-to-end statement of the invariant above: `claude-home.py` now
    EXISTS in `coordinator/bin/`, so the raw directory scan would find it.
    The reservation, not the file's absence, is what keeps it out of the
    derived map. This distinction is why creating the entrypoint was safe to
    ship on its own."""
    agent_bin = tmp_path / "coordinator" / "bin"
    agent_bin.mkdir(parents=True)
    (agent_bin / "claude-home.py").write_text("def main(argv):\n    return 0\n", encoding="utf-8")
    (agent_bin / "cross-repo-memo.py").write_text("def main(argv):\n    return 0\n", encoding="utf-8")

    target_map = substrate._derive_agent_helper_target_map(agent_bin)

    assert "claude-home" not in target_map, (
        f"claude-home reached the agent-helper derivation despite being "
        f"reserved -- got {target_map.get('claude-home')!r}. ch_family is no "
        f"longer the single writer of that name."
    )
    assert "cross-repo-memo" in target_map, (
        "control: an ordinary unreserved name must still derive, or this "
        "test would pass on a scan that found nothing at all."
    )


def test_only_windows_separates_the_image_from_the_ch_family_shim(tmp_path):
    """Why the two platforms are not symmetric. `named_forwarder_path` takes
    `.exe` on Windows and NO suffix on POSIX (it branches on `sys.platform`;
    its own docstring states the POSIX path "is the SAME path the Python
    agent-helper forwarder for `name` already occupies", and ch_family's
    extensionless shim occupies it too).

    So on Windows a door image and the shim can coexist as distinct files,
    and only the ownership question in this module's docstring blocks the
    cutover. On POSIX they are one path, and there is a second blocker on top:
    `door_posix.c :: fall_through` has never been compiled (open P2 row
    state/bug-backlog/2026-08-30-door-posix-c-s-rewritten-fall-through-sh-
    152f899034f4.yaml), so the leg that would land there is unverified.

    This test asserts only the branch it can observe on the host it runs on."""
    image = door_install.named_forwarder_path(tmp_path, "claude-home")
    shim = tmp_path / "claude-home"

    if os.name == "nt":
        assert image != shim and image.suffix == ".exe", (
            f"expected a .exe image distinct from the extensionless shim, got "
            f"{image.name!r}. If Windows stopped separating them, the cutover's "
            f"collision analysis in this module's docstring changes with it."
        )
    else:
        assert image == shim, (
            f"expected the POSIX image path ({image}) to be the same path "
            f"ch_family's extensionless shim occupies ({shim}). If this no "
            f"longer holds, the POSIX collision documented in "
            f"_AGENT_HELPER_RESERVED_NAMES' comment has been resolved."
        )
