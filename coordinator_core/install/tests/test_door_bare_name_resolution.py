"""Which file the bare name selects, pinned as logic rather than as a machine.

`test_door_bare_name_ordering.py` pins the INSTALLER: that `install_warm_door`'s
`claim_bare_name` is sequenced after the `.ps1`-emitting `install_bin_forwarders`,
as a static AST property of `scripts/setup.py`. It cannot see the environment the
installed door actually lives in, and PATH is not owned by this repo.

WHY THE MACHINE IS NOT TESTED HERE, stated so a successor does not "fix" it by
reaching for the real home. A first version of this module asserted against the
live install and SKIPPED all three of its tests: `coordinator_core`'s suite
quarantines `Path.home()` to a temp dir by design, so a hermetic suite structurally
cannot see a property about the real machine. A skip reads as coverage and is not.
The split that survives: `resolve_bare_name` is pure over PATH and PATHEXT and is
pinned HERE against injected directories; `forwarder_door_census.bare_name_door_report`
applies it to the live environment and REPORTS. Do not re-point these tests at
`Path.home()` -- the quarantine will win and the coverage will be imaginary.

THE HAZARD the logic exists to catch, found 2026-08-27 by resolving the bare name
instead of assuming it: a pip-installed console-script shim at
`<python>/Scripts/coordinator-invoke.exe` exists by construction, because this
package declares the console entry point. It loses to the settings-home door on
PATH ORDERING ALONE, with no error and no runtime signal if that flips -- only
~94ms of interpreter start plus engine import per call, against a ~2.34ms relay.

Negative-spec: does NOT test that the door works (`test_warm_door_process_time_gate.py`),
and does NOT test that the installer removes the `.ps1`
(`test_door_install.py :: _remove_shadowing_forwarder_siblings`).
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.install.forwarder_door_census import (
    bare_name_starts_an_interpreter,
    resolve_bare_name,
)

_STEM = "coordinator-invoke"
_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.PY"


def _touch(d: Path, name: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("x", encoding="utf-8")
    return p


def _resolve(dirs: "list[Path]") -> "list[Path]":
    return resolve_bare_name(_STEM, [str(d) for d in dirs], _PATHEXT)


def test_settings_home_exe_wins_over_a_later_scripts_shim(tmp_path: Path) -> None:
    """The live arrangement: the door's bin/ earlier on PATH than a Python
    `Scripts/` carrying a same-named console-script shim."""
    bin_dir = tmp_path / "settings" / "bin"
    scripts = tmp_path / "python" / "Scripts"
    door = _touch(bin_dir, f"{_STEM}.exe")
    shim = _touch(scripts, f"{_STEM}.exe")

    hits = _resolve([bin_dir, scripts])
    assert hits[0] == door, f"expected the door to win; order was {hits}"
    assert shim in hits, "the shim must still be REPORTED, so its presence is visible"


def test_an_earlier_scripts_shim_takes_the_bare_name(tmp_path: Path) -> None:
    """The regression this whole module exists for. Nothing but PATH order
    separates this case from the one above, and the flip is silent."""
    bin_dir = tmp_path / "settings" / "bin"
    scripts = tmp_path / "python" / "Scripts"
    _touch(bin_dir, f"{_STEM}.exe")
    shim = _touch(scripts, f"{_STEM}.exe")

    hits = _resolve([scripts, bin_dir])
    assert hits[0] == shim, (
        "a Scripts/ shim EARLIER on PATH must be reported as the winner -- if the "
        "resolver cannot see this, the census can never warn about it"
    )
    assert bare_name_starts_an_interpreter(hits[0]), (
        "a pip console-script shim under Scripts/ starts an interpreter per call "
        "and must be classified as such, .exe suffix notwithstanding"
    )


def test_a_ps1_sibling_beats_the_exe_in_the_same_directory(tmp_path: Path) -> None:
    """PowerShell's own rule, which PATHEXT does not describe and `shutil.which`
    cannot see. This is the original hazard `claim_bare_name` strips.

    This pins the MODELLED order `resolve_bare_name` was built to produce -- a
    unit test over a pure function cannot verify an OS behaviour, and asserting
    against the function's own construction is not evidence the model is right.
    The model's fidelity rests on the captured `Get-Command`/PATHEXT trace in
    `_POWERSHELL_FIRST_EXT`'s comment (`forwarder_door_census.py`), not on this
    test passing."""
    bin_dir = tmp_path / "settings" / "bin"
    _touch(bin_dir, f"{_STEM}.exe")
    ps1 = _touch(bin_dir, f"{_STEM}.ps1")

    hits = _resolve([bin_dir])
    assert hits[0] == ps1, (
        "a same-directory .ps1 must resolve ahead of the .exe -- modelling this is "
        "the only reason this resolver exists instead of shutil.which"
    )
    assert bare_name_starts_an_interpreter(hits[0])


def test_pathext_order_is_honoured_within_one_directory(tmp_path: Path) -> None:
    """`.EXE` ahead of `.CMD` is what keeps the door ahead of its own forwarder
    sibling, which ships beside it on every install."""
    bin_dir = tmp_path / "settings" / "bin"
    exe = _touch(bin_dir, f"{_STEM}.exe")
    _touch(bin_dir, f"{_STEM}.cmd")

    hits = _resolve([bin_dir])
    assert hits[0] == exe, f"PATHEXT ranks .EXE above .CMD; order was {hits}"


def test_extensionless_file_loses_to_every_pathext_entry(tmp_path: Path) -> None:
    """An extensionless `coordinator-invoke` ships beside the door too. cmd tries
    it only after PATHEXT is exhausted, so it must never take the name."""
    bin_dir = tmp_path / "settings" / "bin"
    exe = _touch(bin_dir, f"{_STEM}.exe")
    bare = _touch(bin_dir, _STEM)

    hits = _resolve([bin_dir])
    assert hits[0] == exe
    assert hits[-1] == bare, f"extensionless must sort last; order was {hits}"


def test_resolution_is_empty_when_nothing_matches(tmp_path: Path) -> None:
    """An empty result is the "door bin/ is not on PATH" signal the census
    reports as BROKEN -- it must not be confused with a successful resolve."""
    assert _resolve([tmp_path / "empty"]) == []


def test_a_native_exe_outside_scripts_is_not_an_interpreter_start(tmp_path: Path) -> None:
    """The predicate must not over-fire: the whole point is that the real door
    passes it."""
    door = _touch(tmp_path / "settings" / "bin", f"{_STEM}.exe")
    assert not bare_name_starts_an_interpreter(door)


def test_shell_forwarder_suffixes_are_interpreter_starts(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    for suffix in (".cmd", ".bat", ".ps1", ".py"):
        assert bare_name_starts_an_interpreter(_touch(bin_dir, f"{_STEM}{suffix}")), suffix


def test_empty_path_entries_are_skipped_not_resolved_against_cwd() -> None:
    """A trailing `;` in PATH yields an empty string, which `Path("")` turns into
    the CWD -- resolving there would let any directory a caller happens to sit in
    claim the bare name."""
    assert resolve_bare_name(_STEM, ["", os.sep], _PATHEXT) == []
