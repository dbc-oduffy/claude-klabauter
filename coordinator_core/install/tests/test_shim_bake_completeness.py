"""Bake-completeness property for `.cmd` shims that ship the
``__PYTHON_BIN__`` placeholder.

C6 (docs/plans/2026-08-10-entrypoint-gate-launcher-and-changed-only.md)
audited a live settings home and found 6 `.cmd` files still carrying the
literal ``__PYTHON_BIN__`` token after install: ``claude-home.cmd``,
``coordinator-settings-home.cmd``, ``machine-local.cmd``,
``mint-deliverable-id.sh.cmd``, ``platform-localize.cmd``, and
``resolve-coordinator-clone.cmd``. That audit disproved the standing
hypothesis (commit ``d852809a1``) that these are all agent-helper
forwarders exempted by ``_write_agent_cmd_forwarder``'s own contract —
NONE of the 6 carry ``_AGENT_CMD_FORWARDER_MARKER``. The real contract is
one level lower:

* ``_install_one`` (the copier every STATIC bin family --
  ``ml_family``/``ml_explicit``/``ch_family``/``platform_localize`` --
  uses) is a plain byte-verbatim copy with NO substitution step at all.
  A source template that ships the literal placeholder is therefore
  structurally incapable of ever being baked through that path -- this is
  the real, durable reason 5 of the 6 (``claude-home.cmd``,
  ``coordinator-settings-home.cmd``, ``machine-local.cmd``,
  ``platform-localize.cmd``, ``resolve-coordinator-clone.cmd``) stay
  unbaked. Only ``python3.cmd`` (Step 3a) and the dynamic agent-helper
  `.cmd`/`.ps1` forwarders (Step 3b, via ``_write_agent_cmd_forwarder``/
  ``_write_agent_ps1_forwarder``) ever receive the resolved-interpreter
  substitution.
* ``mint-deliverable-id.sh.cmd`` is not a member of any static family at
  all -- it is a pre-``_LEGACY_CMD_MARKER`` orphan from the retired
  double-suffix CLI naming scheme, hand-authored before
  ``gen-launcher-shim.py`` existed to stamp the legacy marker. It carries
  NEITHER ``_AGENT_CMD_FORWARDER_MARKER`` nor ``_LEGACY_CMD_MARKER``, so
  it fell through ``_sweep_orphaned_agent_helpers``'s marker check
  silently forever -- fixed by adding it to the explicitly-named
  ``_PRE_MARKER_LEGACY_ORPHAN_NAMES`` set that check also matches on.

This module asserts the PROPERTY, not a snapshot of today's 6 names: the
static-copy path never bakes (by mechanism, not by exemption list), the
dynamic forwarder path always bakes when given a non-empty interpreter,
and the one pre-marker orphan shape is swept while an unrelated
marker-less `.cmd` (standing in for operator-authored or foreign files)
is left alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.install.substrate import (
    _PRE_MARKER_LEGACY_ORPHAN_NAMES,
    _install_one,
    _static_bin_family_names,
    _sweep_orphaned_agent_helpers,
    _write_agent_cmd_forwarder,
)


@pytest.fixture(autouse=True)
def _allow_machine_mutation_in_tmp_path(monkeypatch):
    """Mirrors `test_forwarder_trust_guard.py`'s fixture of the same name --
    this file's `_install_one`/`_sweep_orphaned_agent_helpers` coverage
    writes/deletes real files entirely within `tmp_path`, and
    `coordinator_core/conftest.py::_quarantine_real_home` sets
    `COORDINATOR_DISABLE_MACHINE_MUTATION=1` suite-wide as a
    belt-and-braces opt-out. Unset it locally so this file's own
    write/delete assertions aren't masked by that unrelated incident
    guard."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


_PLACEHOLDER_LINE = 'set "_py=__PYTHON_BIN__"\nif "%_py%"=="__PYTHON_BIN__" set "_py="\n'


def test_install_one_never_substitutes_the_placeholder(tmp_path: Path):
    """The root cause: `_install_one` is a plain copy. A source template
    shipping the literal `__PYTHON_BIN__` placeholder comes out the other
    side byte-identical -- proving the static bin families
    (`ml_family`/`ml_explicit`/`ch_family`/`platform_localize`) are
    exempt from baking by MECHANISM, not by a name list anyone has to
    keep in sync."""
    src = tmp_path / "some-shim.cmd"
    src.write_text(_PLACEHOLDER_LINE, encoding="utf-8")
    dst = tmp_path / "dst" / "some-shim.cmd"
    dst.parent.mkdir()

    _install_one(src, dst, False, "test", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == _PLACEHOLDER_LINE
    assert "__PYTHON_BIN__" in dst.read_text(encoding="utf-8")


def test_write_agent_cmd_forwarder_always_bakes_a_resolved_interpreter(tmp_path: Path):
    """The dynamic agent-helper path is the OTHER half of the property:
    unlike `_install_one`, `_write_agent_cmd_forwarder` always substitutes
    a non-empty `python3_cmd_resolved_bin` -- no shim reachable through
    Step 3b's derived-forwarder loop should ever retain the literal
    placeholder."""
    dst = tmp_path / "some-cli.cmd"

    fake_interpreter = r"C:\fake\python.exe"  # abs-path-ok: fixture value, never resolved/executed
    _write_agent_cmd_forwarder(
        "some-cli", dst, False, python3_cmd_resolved_bin=fake_interpreter,
    )

    text = dst.read_text(encoding="utf-8")
    assert "__PYTHON_BIN__" not in text
    assert fake_interpreter in text


def test_pre_marker_legacy_orphan_name_is_explicit_and_documented():
    """The exempt/orphan classification must be LEGIBLE, not derived from
    today's count of unbaked files on any one box. `mint-deliverable-id.sh.cmd`
    is the one confirmed-live pre-marker shape; the set exists precisely so
    a future name can be added the same explicit way instead of the sweep
    silently failing to see it, the way it silently failed to see this one."""
    assert _PRE_MARKER_LEGACY_ORPHAN_NAMES == frozenset({"mint-deliverable-id.sh.cmd"})
    # Never a member of the static, statically-installed-every-run set --
    # if it ever became one, `_sweep_orphaned_agent_helpers`'s `protected_names`
    # check would (correctly) keep it forever regardless of this set's
    # membership, exactly as it does for `platform-localize.cmd` and
    # `resolve-coordinator-clone.cmd` (see that function's own docstring).
    assert _PRE_MARKER_LEGACY_ORPHAN_NAMES.isdisjoint(_static_bin_family_names())


def test_sweep_removes_the_pre_marker_legacy_orphan(tmp_path: Path):
    """Regression coverage for the fix: a `.cmd` file carrying neither
    `_AGENT_CMD_FORWARDER_MARKER` nor `_LEGACY_CMD_MARKER`, but matching
    `_PRE_MARKER_LEGACY_ORPHAN_NAMES` by name, is now swept -- this is the
    exact on-disk shape C6's live audit found for
    `mint-deliverable-id.sh.cmd`."""
    orphan = tmp_path / "mint-deliverable-id.sh.cmd"
    orphan.write_text(_PLACEHOLDER_LINE, encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert not orphan.exists()


def test_sweep_leaves_an_unrelated_markerless_cmd_alone(tmp_path: Path):
    """The fix must stay a bounded, explicitly-named allowance -- never a
    blanket "any markerless .cmd is an orphan" heuristic. A markerless
    `.cmd` under a name NOT in `_PRE_MARKER_LEGACY_ORPHAN_NAMES` (standing
    in for an operator-authored or foreign file) is left untouched,
    preserving `_sweep_orphaned_agent_helpers`'s positive-identification-
    only contract."""
    foreign = tmp_path / "operator-authored.cmd"
    foreign.write_text("@echo off\necho not ours\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert foreign.exists()
