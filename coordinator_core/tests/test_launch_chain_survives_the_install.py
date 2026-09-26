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
    home = tmp_path / "settings-home"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setattr(install_health_run, "settings_home", lambda: home)
    return home / "bin"


def test_the_leg_is_registered():
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
    (settings_bin / "claude-doe").write_bytes(magic + b" not a trampoline")
    assert install_health_run.check_launch_chain_intact("p", "m") == 1


def test_a_readable_launcher_that_never_execs_fails(settings_bin):
    (settings_bin / "claude-doe").write_text(
        "#!/usr/bin/env python3\nprint('hello')\n", encoding="utf-8"
    )
    assert install_health_run.check_launch_chain_intact("p", "m") == 1


def test_the_generated_forwarder_is_judged_by_the_wrapper_it_execs(settings_bin, tmp_path):
    (settings_bin / "claude-doe").write_text(
        "from _resolve_claude_klabauter import exec_cli\nexec_cli(\"claude-doe.py\")\n",
        encoding="utf-8",
    )
    engine_bin = tmp_path / "engine" / "coordinator" / "bin"
    engine_bin.mkdir(parents=True)
    wrapper = engine_bin / "claude-doe.py"
    wrapper.write_text("exec claude --plugin-dir x\n", encoding="utf-8")
    engine = str(tmp_path / "engine")
    assert install_health_run.check_launch_chain_intact("p", engine) == 0

    wrapper.write_text("print('hello')\n", encoding="utf-8")
    assert install_health_run.check_launch_chain_intact("p", engine) == 1


def test_an_absent_launcher_is_not_a_failure(settings_bin):
    assert install_health_run.check_launch_chain_intact("p", "m") == 0


def test_a_passing_leg_says_nothing(settings_bin, capsys):
    assert install_health_run.check_launch_chain_intact("p", "m") == 0
    (settings_bin / "claude-doe").write_text("exec claude --plugin-dir x\n", encoding="utf-8")
    assert install_health_run.check_launch_chain_intact("p", "m") == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
