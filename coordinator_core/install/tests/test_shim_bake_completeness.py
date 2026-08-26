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
one pre-marker orphan shape is swept while an unrelated marker-less
`.cmd` (standing in for operator-authored or foreign files) is left
alone, and the dynamic forwarder path always bakes when given a
non-empty interpreter.

**Superseded 2026-08-16 by registry-read-stops-costing-a-process (C2,
C3):** the "static-copy path never bakes" half of the property above was
disproved, not merely a stale detail — DoE-claude
`coordinator/docs/wiki/machine-local-registry.md` §4.3 says baking
`__PYTHON_BIN__` for the static bin families is worth doing, and this
2026-08-10 plan's own C6 called the unbaked shims "benign", never a
deliberately pinned invariant. C2 gave `_install_one` an explicit,
caller-supplied `python_bin_substitution: Optional[str] = None` kwarg —
`_install_bin_resolvers` now passes a resolved interpreter for the
static families (`ml_family`/`ml_explicit`/`ch_family`/
`platform_localize`) on Windows, and fails the install loudly
(`SubstrateFatalError`) rather than shipping an unbaked shim when no
interpreter can be resolved. The inverted property, asserted below: a
static-family template shipping the placeholder comes out BAKED when a
substitution value is supplied, byte-verbatim ONLY when the caller
passes the (still-legitimate) default `None`, and an unresolvable
interpreter is a hard `SubstrateFatalError`, never a silent unbaked
shim.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.install.substrate import (
    _PRE_MARKER_LEGACY_ORPHAN_NAMES,
    SubstrateFatalError,
    _install_bin_resolvers,
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


def test_install_one_default_substitution_is_still_byte_verbatim(tmp_path: Path):
    """Post-C2, `_install_one`'s `python_bin_substitution` kwarg defaults to
    `None` -- this is a statement about that DEFAULT, not about the static
    bin families as a class (see the module docstring's supersession note):
    a caller that does not pass a substitution value still gets a plain
    byte-verbatim copy, so every non-static-family `_install_one` call site
    (and any static-family call made without the kwarg) is unaffected by
    C2's inversion."""
    src = tmp_path / "some-shim.cmd"
    src.write_text(_PLACEHOLDER_LINE, encoding="utf-8")
    dst = tmp_path / "dst" / "some-shim.cmd"
    dst.parent.mkdir()

    _install_one(src, dst, False, "test", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == _PLACEHOLDER_LINE
    assert "__PYTHON_BIN__" in dst.read_text(encoding="utf-8")


def test_install_one_bakes_when_given_a_substitution_value(tmp_path: Path):
    """The inverted property (C2/C3): a static-family template shipping the
    literal `__PYTHON_BIN__` placeholder comes out BAKED when the caller
    supplies a non-`None` `python_bin_substitution` -- this is what
    `_install_bin_resolvers` now does for `ml_family`/`ml_explicit`/
    `ch_family`/`platform_localize` on Windows via
    `static_python_bin_substitution`. A byte substring replace, not
    template-aware: every `__PYTHON_BIN__` occurrence is replaced, nothing
    else changes."""
    src = tmp_path / "some-shim.cmd"
    src.write_text(_PLACEHOLDER_LINE, encoding="utf-8")
    dst = tmp_path / "dst" / "some-shim.cmd"
    dst.parent.mkdir()
    resolved_interpreter = r"C:\fake\python.exe"  # abs-path-ok: fixture value, never resolved/executed

    _install_one(
        src, dst, False, "test", False, force_overwrite=True,
        python_bin_substitution=resolved_interpreter,
    )

    text = dst.read_text(encoding="utf-8")
    assert "__PYTHON_BIN__" not in text
    assert resolved_interpreter in text


def test_install_bin_resolvers_fails_loudly_on_unresolvable_interpreter(monkeypatch):
    """AC8's other half, corrected 2026-08-16 (docs/plans/2026-08-16-
    registry-read-stops-costing-a-process.md AC8): on Windows, an
    interpreter that is genuinely UNRESOLVED -- `resolve_python_bin()`
    itself returns `("", [])`, meaning no interpreter is reachable by any
    route -- must fail the install loudly via `SubstrateFatalError`, never
    silently leave the static families unbaked and move on. This goes
    through the real resolver (`_resolve_baked_python_bin_detail`) rather
    than hand-passing `python3_cmd_resolved_bin=""` directly: passing ""
    alone is ambiguous (see `_BakedPythonBinReason`) and a bare-emptiness
    gate is exactly the bug this test now guards against -- see the sibling
    py-launcher-only test below for the case that must NOT fail.
    `_install_bin_resolvers` raises this before any per-family write loop
    runs (immediately after loading the bin-templates manifest), so this is
    reachable with placeholder `ml_bin`/`ch_bin`/`bin_dst` paths that are
    never actually read on this path -- only exercised on `os.name == "nt"`,
    matching the source check this guards (`_install_bin_resolvers` itself,
    non-Windows hosts never bake this family at all). `coordinator_claude_klabauter_
    root_with_class` is stubbed out because the interpreter check runs
    after it and this test's own home-quarantine fixture (see
    `conftest.py`) makes THAT resolution fail first with an unrelated
    `SubstrateFatalError` -- stubbing it isolates the property under test
    from that unrelated failure mode."""
    if os.name != "nt":
        pytest.skip("static bin-resolver baking is a Windows-only rung (_install_bin_resolvers)")

    import coordinator_core.install.substrate as substrate_mod
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(
        substrate_mod, "coordinator_engine_root_with_class",
        lambda: (str(Path(__file__).resolve().parents[3]), "sentinel"),
    )
    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: ("", []))

    python3_cmd_resolved_bin, reason = substrate_mod._resolve_baked_python_bin_detail()
    assert python3_cmd_resolved_bin == ""
    assert reason is substrate_mod._BakedPythonBinReason.UNRESOLVED

    with pytest.raises(SubstrateFatalError, match="could not resolve an absolute Python interpreter"):
        _install_bin_resolvers(
            Path("unused-ml-bin"), Path("unused-ch-bin"), Path("unused-bin-dst"),
            False,
            python3_cmd_resolved_bin=python3_cmd_resolved_bin,
            python3_cmd_bake_reason=reason,
        )


def test_install_bin_resolvers_succeeds_when_only_the_py_launcher_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The AC8 gate's corrected half: a box whose only Python entry point is
    the `py`/`pyw` launcher (`_BakedPythonBinReason.LAUNCHER_ONLY`) is a
    normal, supported Windows configuration -- the python.org installer
    registers `py` on PATH by default even with "Add python.exe to PATH"
    left unchecked -- and must NOT fail the install. Before C2 introduced
    AC8's gate, this degraded gracefully via `_install_one`'s byte-verbatim
    copy (the `__PYTHON_BIN__` placeholder left unsubstituted for the
    shim's own documented `py -3` runtime fallback); C2's original,
    over-broad gate regressed that to a hard install failure on this exact,
    ordinary configuration. This is the regression test."""
    if os.name != "nt":
        pytest.skip("static bin-resolver baking is a Windows-only rung (_install_bin_resolvers)")

    import coordinator_core.install.substrate as substrate_mod
    import coordinator_core.pyresolve as pyresolve
    from coordinator_core.install.test_forwarder_trust_guard import (
        _make_install_bin_resolvers_fixture,
    )

    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: ("py", ["-3"]))

    claude_klabauter_root, ml_bin, ch_bin = _make_install_bin_resolvers_fixture(tmp_path)
    # C14 retired CLAUDE_KLABAUTER_ROOT from Rung 1 of the resolver chain; deleted
    # rather than left alone so an inherited ancestor-process value cannot
    # reintroduce the retired-name advisory.
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    python3_cmd_resolved_bin, reason = substrate_mod._resolve_baked_python_bin_detail()
    assert python3_cmd_resolved_bin == ""
    assert reason is substrate_mod._BakedPythonBinReason.LAUNCHER_ONLY

    # Must complete without raising -- the regression under test.
    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        False,
        python3_cmd_resolved_bin=python3_cmd_resolved_bin,
        python3_cmd_bake_reason=reason,
    )

    # The write loop ran to completion (not aborted at the AC8 gate before
    # ever reaching Step 3's per-family writers).
    assert (bin_dst / "machine-local.cmd").is_file()


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
