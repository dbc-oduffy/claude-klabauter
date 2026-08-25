"""Tests for coordinator_core.install.settings_home_report (C5, docs/plans/
2026-08-17-machine-first-install-surface.md).

Mutation-test contract asserted here: the check must fail when the settings
home stops being populated (a member removed on disk), never when only the
enumeration text changes -- see `test_missing_fixed_member_is_detected` and
`test_missing_forwarder_is_detected`, which each construct a fully-populated
fixture then remove exactly one real thing and assert the report flips.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.install.settings_home_report import (
    check_settings_home,
    expected_forwarders,
    format_report_lines,
)
from coordinator_core.install.substrate import _AGENT_FORWARDER_MARKER

_REPO_ROOT = Path(__file__).resolve().parents[3]

_FOREIGN_MARKER = "from _resolve_claude_klabauter import exec_cli"


def _populate_full_settings_home(root: Path) -> Path:
    sh = root / ".coordinator-claude-settings"
    (sh / "machine-local").mkdir(parents=True)
    (sh / "machine-local" / ".makima-root").write_text("x")
    (sh / "bin").mkdir()
    (sh / "bin" / "_resolve_makima.py").write_text("x")
    (sh / "coordinator-whoami").mkdir()
    (sh / ".coordinator-venv").mkdir()
    (sh / "settings-manifest.md").write_text("x")
    (sh / ".percolate-identity").write_text("x")
    return sh


@pytest.fixture
def makima_root() -> Path:
    """The real makima checkout -- expected_forwarders() derives from its
    live coordinator/bin/ listing, so tests exercise the real generator
    rather than a synthetic fixture."""
    return Path(__file__).resolve().parents[3]


def test_fully_populated_fixed_members_report_present(tmp_path: Path, makima_root: Path) -> None:
    sh = _populate_full_settings_home(tmp_path)

    report = check_settings_home(sh, makima_root)

    assert report.fixed_missing == []


def test_missing_fixed_member_is_detected(tmp_path: Path, makima_root: Path) -> None:
    sh = _populate_full_settings_home(tmp_path)
    (sh / "coordinator-whoami").rmdir()

    report = check_settings_home(sh, makima_root)

    assert not report.complete
    labels = [m.label for m in report.fixed_missing]
    assert any("coordinator-whoami" in label for label in labels)


def test_missing_forwarder_is_detected(tmp_path: Path, makima_root: Path) -> None:
    """An empty bin/ against a real coordinator/bin/ listing must report
    every expected forwarder missing, not silently pass."""
    sh = _populate_full_settings_home(tmp_path)

    report = check_settings_home(sh, makima_root)

    assert report.forwarder_expected > 0
    assert report.forwarder_present == 0
    assert len(report.forwarder_missing) == report.forwarder_expected
    assert not report.complete


def _land_forwarders(sh: Path, makima_root: Path, *, marker: str = _AGENT_FORWARDER_MARKER) -> None:
    """Write a body-shaped forwarder per expected name, through the same
    marker + `exec_cli(target)` pair `substrate._write_agent_forwarder`
    emits. `marker` is the seam: passing `_FOREIGN_MARKER` reproduces what a
    run rooted at the published mirror leaves in the SAME shared bin/."""
    for installed_name, target in expected_forwarders(makima_root).items():
        (sh / "bin" / installed_name).write_text(
            f"{marker}\n\nexec_cli(\"{target}\")\n"
        )


def test_forwarder_present_when_landed(tmp_path: Path, makima_root: Path) -> None:
    """Landing exactly the expected forwarder files makes the check pass
    for forwarders specifically -- proves the oracle is the on-disk bin/
    listing, not a count or a self-reported manifest."""
    sh = _populate_full_settings_home(tmp_path)
    _land_forwarders(sh, makima_root)

    report = check_settings_home(sh, makima_root)

    assert report.forwarder_missing == []
    assert report.forwarder_present == report.forwarder_expected
    assert report.complete


def test_format_report_lines_flags_incomplete(tmp_path: Path, makima_root: Path) -> None:
    sh = _populate_full_settings_home(tmp_path)
    (sh / "settings-manifest.md").unlink()

    report = check_settings_home(sh, makima_root)
    lines = format_report_lines(report)

    assert any("FAIL" in line and "settings-manifest.md" in line for line in lines)


def test_check_does_not_leak_derivation_stdout(tmp_path: Path, makima_root: Path, capsys) -> None:
    """coordinator_core.install.substrate._derive_agent_helper_target_map
    print()s a WARNING on a legacy extensionless/.py-twin collision; the
    doctor probe's caller emits pure JSON on stdout by contract, so that
    print must never reach this module's stdout. Regression pin for the
    stdout-pollution bug this module's docstring on `expected_forwarders`
    names."""
    sh = _populate_full_settings_home(tmp_path)

    check_settings_home(sh, makima_root)

    assert capsys.readouterr().out == ""


def test_absent_venv_leaves_the_settings_home_complete(
    tmp_path: Path, makima_root: Path
) -> None:
    """A machine-first install never creates `.coordinator-venv` -- it is
    reachable only via the break-glass `--allow-venv-fallback`, required on
    every run with no automatic or prior-consent path (INSTALL.md
    § Dependency provisioning item 5). Demanding it made a correct install
    permanently incomplete, red in both `scripts/setup.py`'s report step and
    the `makima.settings_home.complete` doctor probe.
    """
    sh = _populate_full_settings_home(tmp_path)
    (sh / ".coordinator-venv").rmdir()

    report = check_settings_home(sh, makima_root)

    assert report.fixed_missing == [], (
        "an absent optional member must not count against the fixed inventory"
    )

    venv = next(m for m in report.members if m.path.name == ".coordinator-venv")
    assert venv.required is False
    assert venv.present is False

    line = next(
        line for line in format_report_lines(report) if ".coordinator-venv" in line
    )
    assert line.strip().startswith("SKIP"), (
        "an absent optional member renders SKIP, never FAIL"
    )


def test_foreign_root_forwarder_body_is_not_counted_present(
    tmp_path: Path, makima_root: Path
) -> None:
    """delphipro, 2026-08-22: settings-home `bin/` is one directory every
    engine root on the box installs into. A run rooted at the published
    mirror landed its own forwarder set there -- every name present, every
    body importing `_resolve_claude_klabauter`. An existence-only count
    reports that green while nothing routes through this root.
    """
    sh = _populate_full_settings_home(tmp_path)
    _land_forwarders(sh, makima_root, marker=_FOREIGN_MARKER)

    report = check_settings_home(sh, makima_root)

    assert report.forwarder_missing == [], "the files themselves are on disk"
    assert len(report.forwarder_unverified) == report.forwarder_expected, (
        "every derived name must fail body verification, with no exemptions"
    )
    assert not report.complete
    assert any(
        "body not this root's" in line for line in format_report_lines(report)
    )


def test_door_owned_coordinator_invoke_counts_present(
    tmp_path: Path, makima_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-08-22 collision fix: once the native warm-engine door claims
    the `coordinator-invoke` bare name (door_install.py), that slot's body
    is no longer the generic forwarder -- `forwarder_body_is_ours` fails
    there BY DESIGN, not by corruption. The report must count it present
    via `_is_door_owned_forwarder_slot`, not flag it `body not this root's`.
    """
    from coordinator_core.install import door_install

    sh = _populate_full_settings_home(tmp_path)
    _land_forwarders(sh, makima_root)
    bare = door_install.BARE_FORWARDER_NAME
    # Stand-in for the door's binary -- the report only reads the forwarder
    # body for the marker/exec_cli pair, so any non-matching bytes exercise
    # the same "body isn't the generic forwarder" branch the real door hits.
    (sh / "bin" / bare).write_bytes(b"\x7fELF-or-MZ-stand-in-for-the-door-binary")
    monkeypatch.setattr(
        "coordinator_core.install.settings_home_report.is_door_installed",
        lambda bin_dir: True,
    )

    report = check_settings_home(sh, makima_root)

    assert bare not in report.forwarder_unverified
    assert report.forwarder_door_owned == [bare]
    assert report.forwarder_present == report.forwarder_expected
    assert report.complete
    assert any(
        "door-owned" in line and bare in line for line in format_report_lines(report)
    )


def test_door_owned_check_does_not_cover_unrelated_corrupt_forwarders(
    tmp_path: Path, makima_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The door-owned exemption is scoped to exactly the `coordinator-invoke`
    slot -- a corrupt body anywhere else must still fail verification even
    when the door happens to be installed elsewhere in the same bin/.
    """
    from coordinator_core.install import door_install

    sh = _populate_full_settings_home(tmp_path)
    _land_forwarders(sh, makima_root)
    names = sorted(expected_forwarders(makima_root))
    other = next(n for n in names if n != door_install.BARE_FORWARDER_NAME)
    (sh / "bin" / other).write_text("garbage, not a real forwarder body")
    monkeypatch.setattr(
        "coordinator_core.install.settings_home_report.is_door_installed",
        lambda bin_dir: True,
    )

    report = check_settings_home(sh, makima_root)

    assert other in report.forwarder_unverified
    assert other not in report.forwarder_door_owned
    assert not report.complete


def test_missing_resolver_shim_is_detected(tmp_path: Path, makima_root: Path) -> None:
    """Every forwarder body imports `_resolve_makima`. The self-heal path
    writes forwarders but never the shim, so the shim's absence is a state
    where all 394 names exist and all 394 rc=1 on invocation."""
    sh = _populate_full_settings_home(tmp_path)
    _land_forwarders(sh, makima_root)
    (sh / "bin" / "_resolve_makima.py").unlink()

    report = check_settings_home(sh, makima_root)

    assert not report.complete
    assert any("_resolve_makima.py" in m.label for m in report.fixed_missing)


def _load_doctor_probe_module():
    """bin/makima-doctor-probe.py loaded by path -- it is a CLI script, not
    an importable package member."""
    probe_path = _REPO_ROOT / "bin" / "makima-doctor-probe.py"
    spec = importlib.util.spec_from_file_location(
        "_settings_home_report_contract_probe", probe_path
    )
    if spec is None or spec.loader is None:
        pytest.skip("bin/makima-doctor-probe.py not loadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda sh, root: None, id="complete"),
        pytest.param(
            lambda sh, root: (sh / "bin" / sorted(expected_forwarders(root))[0]).unlink(),
            id="one-forwarder-absent",
        ),
        pytest.param(
            lambda sh, root: _land_forwarders(sh, root, marker=_FOREIGN_MARKER),
            id="foreign-root-bodies",
        ),
        pytest.param(
            lambda sh, root: (sh / "bin" / "_resolve_makima.py").unlink(),
            id="resolver-shim-absent",
        ),
    ],
)
def test_installer_line_and_doctor_probe_cannot_disagree(
    tmp_path: Path, makima_root: Path, monkeypatch: pytest.MonkeyPatch, damage
) -> None:
    """Producer/consumer lock. `scripts/setup.py :: install_verify_settings_home`
    and the `makima.settings_home.complete` probe are two readers of one
    settings home, and on delphipro (2026-08-22) they returned opposite
    verdicts minutes apart. Neither may hold a count, a threshold, or a
    presence rule the other lacks: both must resolve to the same
    `check_settings_home` report, over every damage shape below.
    """
    sh = _populate_full_settings_home(tmp_path)
    _land_forwarders(sh, makima_root)
    damage(sh, makima_root)

    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home", lambda: sh, raising=True
    )

    report = check_settings_home(sh, makima_root)
    installer_lines = format_report_lines(report)

    probe = _load_doctor_probe_module()
    result = probe._run_probe_settings_home_complete(makima_root)

    assert result.data["settings_home"] == str(sh)
    assert result.data["forwarder_present"] == report.forwarder_present
    assert result.data["forwarder_expected"] == report.forwarder_expected
    assert result.data["forwarder_missing_count"] == len(report.forwarder_missing)
    assert result.data["forwarder_unverified_count"] == len(report.forwarder_unverified)

    probe_says_complete = result.status == probe._PASS
    installer_says_complete = report.complete
    assert probe_says_complete is installer_says_complete
    assert any("FAIL" in line for line in installer_lines) is not installer_says_complete


def test_absent_venv_is_demanded_while_an_interpreter_pin_names_it(
    tmp_path: Path, makima_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the condition: a box that DID exercise
    `--allow-venv-fallback` carries pins aimed into the venv, and there a
    missing venv is a broken interpreter pin, not a clean machine-first state.
    Flat-optional would go quiet on exactly the box where it is load-bearing.
    """
    from coordinator_core.install import settings_home_report as mod

    sh = _populate_full_settings_home(tmp_path)
    (sh / ".coordinator-venv").rmdir()

    monkeypatch.setattr(
        mod,
        "registry_get",
        lambda key: (
            str(sh / ".coordinator-venv" / "bin" / "python")
            if key == "coordinator.whoami_python"
            else None
        ),
    )

    report = check_settings_home(sh, makima_root)

    labels = [m.label for m in report.fixed_missing]
    assert any(".coordinator-venv" in label for label in labels), (
        "a pin resolving into the venv makes its absence a real gap"
    )

    line = next(
        line for line in format_report_lines(report) if ".coordinator-venv" in line
    )
    assert line.strip().startswith("FAIL")
