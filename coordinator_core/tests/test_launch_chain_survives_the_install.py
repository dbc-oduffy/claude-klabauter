"""The install ends by asserting a session can still be started.

WHAT THIS CLOSES, AND WHY IT IS AN END-STATE CHECK RATHER THAN ANOTHER
PER-CAUSE ONE. `claude-doe` is the only launcher on the interactive chain
and, on disk, an unremarkable `coordinator/bin/<name>.py` -- so every roster,
glob, allowlist and cutover that enumerates names by shape has swept it up,
and each time the box lost the ability to start a session until a human
found out by failing to start one. Each cause got fixed where it belonged
and the class stayed open, because the next member arrives from a direction
no existing roster describes.

`install_health_run.check_launch_chain_intact` is the artifact that
discharges it: the same install that would break the launcher is the thing
that reports it broke, at install time, with a runnable remediation --
rather than the operator's next launch being the detector. This file is that
leg's guard, including the registration itself (a leg nothing calls is the
same as no leg, and the registry is a list a future edit can reorder or
drop).
"""

from __future__ import annotations

import pytest

from coordinator_core.ops import install_health_run


@pytest.fixture
def settings_bin(tmp_path, monkeypatch):
    """Points the leg at a throwaway settings-home. The leg reads
    `settings_home()` at call time, so patching the name this module
    imported is what redirects it."""
    home = tmp_path / "settings-home"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setattr(install_health_run, "settings_home", lambda: home)
    return home / "bin"


def test_the_leg_is_registered():
    """An unregistered leg is not a check. It runs LAST, after every leg
    that can change what it reads."""
    names = [name for name, _ in install_health_run._NATIVE_LEGS]
    assert "check-launch-chain-intact" in names
    assert names[-1] == "check-launch-chain-intact"


def test_a_healthy_trampoline_passes(settings_bin):
    (settings_bin / "claude-doe").write_text(
        '#!/usr/bin/env python3\nos.execv(claude, ["exec claude --plugin-dir x"])\n',
        encoding="utf-8",
    )
    assert install_health_run.check_launch_chain_intact("p", "m") == 0


def test_a_native_image_wearing_the_name_fails(settings_bin, capsys):
    """The 2026-09-02 shape: the multi-name forwarder install hardlinked the
    native door over the trampoline, and `claude --dangerously-skip-
    permissions` stopped working box-wide."""
    (settings_bin / "claude-doe").write_bytes(b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01 a door image")

    assert install_health_run.check_launch_chain_intact("p", "m") == 1
    err = capsys.readouterr().err
    assert "cannot start a session" in err
    assert "install-claude-doe-wrapper.py" in err, (
        "cold-path remediation must name a runnable script -- what fires "
        "before a session exists cannot be fixed by a slash command"
    )


@pytest.mark.parametrize(
    "magic",
    [b"\x7fELF\x02\x01\x01", b"MZ\x90\x00", b"\xca\xfe\xba\xbe\x00\x00"],
)
def test_every_native_image_format_is_caught(settings_bin, magic):
    """Windows is first-class and Linux is on the roadmap -- a check that
    only knows Mach-O would pass on the platforms it was not written on."""
    (settings_bin / "claude-doe").write_bytes(magic + b" not a trampoline")
    assert install_health_run.check_launch_chain_intact("p", "m") == 1


def test_a_readable_launcher_that_never_execs_fails(settings_bin):
    """The other half of the same defect: a text file under the right name
    that cannot reach a launch is no better than a binary one."""
    (settings_bin / "claude-doe").write_text(
        "#!/usr/bin/env python3\nprint('hello')\n", encoding="utf-8"
    )
    assert install_health_run.check_launch_chain_intact("p", "m") == 1


def test_an_absent_launcher_is_not_a_failure(settings_bin):
    """The wrapper install is advisory in `scripts/setup.py`, so a
    settings-home that never had one is a different leg's concern -- the
    same posture `check_door_provenance` takes for "no-door". This leg
    refuses a launcher that EXISTS and cannot launch."""
    assert install_health_run.check_launch_chain_intact("p", "m") == 0


def test_a_passing_leg_says_nothing(settings_bin, capsys):
    """Silent on success, both branches of it -- an orchestrator whose
    healthy legs each announce themselves is one whose failing leg is read
    past. `test_empty_dir_exits_zero_silent` holds the whole orchestrator to
    this, and it is the register `docs/wiki/guard-messaging.md` asks for:
    one fact, once, and only when there is a fact."""
    assert install_health_run.check_launch_chain_intact("p", "m") == 0
    (settings_bin / "claude-doe").write_text("exec claude --plugin-dir x\n", encoding="utf-8")
    assert install_health_run.check_launch_chain_intact("p", "m") == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
