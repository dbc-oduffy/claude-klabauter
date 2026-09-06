"""Tests for the per-box worker-cap derivation and the resolver seam.

The load-bearing property is not "the formula is right" -- that is
``compute_parallelism_cap``'s own test's job -- but that the number a test run
actually obeys is an *output* of the formula, resolved against the host that
will run it rather than read from a constant every clone shares.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from coordinator_core.conservatism.verify import assert_safe_direction_holds
from coordinator_core.install import derive_worker_cap as dwc
from coordinator_core.install.derive_worker_cap import (
    apply_cap_to_command,
    cap_command_for_this_box,
    derive_cap,
    main,
)
from coordinator_core.resolve_validation_cmd import (
    cs_resolve_fast_test_cmd,
    cs_resolve_full_test_cmd,
)

FAST = "python3 -m pytest -m 'not cadence' -n auto --maxprocesses=7 --timeout=300"


# --- the derivation itself ---------------------------------------------------


# --- command rewriting -------------------------------------------------------


def test_existing_ceiling_is_replaced():
    assert "--maxprocesses=12" in apply_cap_to_command(FAST, 12)
    assert "--maxprocesses=7" not in apply_cap_to_command(FAST, 12)


def test_missing_ceiling_is_inserted_next_to_the_worker_request():
    out = apply_cap_to_command("pytest -n auto --timeout=300", 6)
    assert out == "pytest -n auto --maxprocesses=6 --timeout=300"


def test_logical_is_an_auto_detected_request_too():
    out = apply_cap_to_command("pytest -n logical", 6)
    assert out == "pytest -n logical --maxprocesses=6"


def test_a_command_with_no_worker_request_is_left_alone():
    """`--maxprocesses` is a one-sided min(); without an auto-detected request
    it means nothing, and inventing one would change what the command runs."""
    cmd = "python3 -m pytest -m 'not pending_fix' --timeout=300"
    assert apply_cap_to_command(cmd, 12) == cmd


def test_an_explicit_worker_count_is_not_a_request_for_the_host_core_count():
    cmd = "pytest -n 4 --timeout=300"
    assert apply_cap_to_command(cmd, 12) == cmd


def test_a_duplicated_ceiling_flag_cannot_defeat_the_cap():
    """argparse is last-wins, so rewriting only the first `--maxprocesses`
    would leave the command running at the second one and the ceiling
    silently defeated -- the one failure this module exists to prevent."""
    out = apply_cap_to_command("pytest -n auto --maxprocesses=7 --maxprocesses=9", 2)
    assert out == "pytest -n auto --maxprocesses=2 --maxprocesses=2"


def test_rewriting_is_idempotent():
    once = apply_cap_to_command(FAST, 12)
    assert apply_cap_to_command(once, 12) == once


def test_a_worker_request_inside_a_quoted_marker_expression_is_not_a_real_request():
    """Review: code-reviewer P2 — a literal `-n auto` inside a quoted `-k`/`-m`
    expression is not a worker request and must not be misdetected or rewritten."""
    cmd = 'pytest -k "not -n auto" -m "not cadence"'
    assert apply_cap_to_command(cmd, 12) == cmd


def test_an_existing_maxprocesses_inside_a_quoted_expression_is_not_touched():
    cmd = 'pytest -n auto -k "text with --maxprocesses=7 inside"'
    out = apply_cap_to_command(cmd, 12)
    assert "--maxprocesses=12" in out
    assert '"text with --maxprocesses=7 inside"' in out


def test_an_unterminated_quote_fails_open_not_closed():
    """Review: code-reviewer P2 -- `_QUOTE_SPAN` requires a closing quote to
    match, so a stray/unbalanced opening quote leaves everything after it
    scanned as if unquoted: a `--maxprocesses=` occurrence a human would read
    as still inside the quote gets detected and rewritten anyway. Pinned
    deliberately, not fixed: a command with an unbalanced quote is already
    malformed and will fail at the shell regardless, so this is the
    documented (fail-open) behaviour, not a defect this test guards against."""
    cmd = 'pytest -n auto -k "unterminated --maxprocesses=7'
    out = apply_cap_to_command(cmd, 12)
    assert out == 'pytest -n auto -k "unterminated --maxprocesses=12'


# --- the box-resolved seam ---------------------------------------------------


def test_changed_hardware_resolves_to_a_different_number(monkeypatch):
    """AC: re-deriving on a box whose hardware changed yields a different cap,
    with nobody editing a file."""
    for cores, ram, expected in ((24, 96.0, 12), (4, 15.0, 2), (12, 24.0, 6)):
        monkeypatch.setattr(dwc, "default_physical_cores", lambda c=cores: c)
        monkeypatch.setattr(dwc, "default_usable_ram_gb", lambda r=ram: r)
        resolved, cap = cap_command_for_this_box(FAST)
        assert cap == expected
        assert f"--maxprocesses={expected}" in resolved


# --- declared fail direction -------------------------------------------------


def _no_ram_figure():
    """What `default_usable_ram_gb` does on a host without psutil."""
    raise ImportError("psutil is required for the usable-RAM figure")


@contextmanager
def _machine_unreadable():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dwc, "default_usable_ram_gb", _no_ram_figure)
        yield


def test_an_unreadable_host_keeps_the_committed_ceiling():
    """Declared FALL_BACK: the fast tier must still run on a host this module
    cannot read, and it runs with the ceiling every host obeyed before --
    bare `-n auto` has killed a session (docs/reference/test-tiers.md)."""
    assert_safe_direction_holds(
        cap_command_for_this_box,
        invoke=lambda: cap_command_for_this_box(FAST),
        undeterminable=_machine_unreadable,
    )
    with _machine_unreadable():
        assert cap_command_for_this_box(FAST) == (FAST, None)


# --- the resolver consumes it ------------------------------------------------


def _repo_with(tmp_path, fast: str):
    (tmp_path / "coordinator.local.md").write_text(
        f'---\nproject_type: general\nfast_test_cmd: "{fast}"\n---\n',
        encoding="utf-8",
        newline="\n",
    )
    return tmp_path


def test_the_resolver_hands_back_this_box_s_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    monkeypatch.setattr(dwc, "default_physical_cores", lambda: 4)
    monkeypatch.setattr(dwc, "default_usable_ram_gb", lambda: 15.0)
    resolved = cs_resolve_fast_test_cmd(str(_repo_with(tmp_path, FAST)))
    assert resolved.exit_code == 0
    assert "--maxprocesses=2" in resolved.cmd


def test_the_tracked_config_is_not_rewritten(tmp_path, monkeypatch):
    """The committed value is one constant shared by every clone; the ceiling
    resolves per box precisely so this file never has to carry a box's answer."""
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    root = _repo_with(tmp_path, FAST)
    before = (root / "coordinator.local.md").read_bytes()
    cs_resolve_fast_test_cmd(str(root))
    assert (root / "coordinator.local.md").read_bytes() == before


def test_the_env_var_escape_hatch_is_capped_too(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", FAST)
    monkeypatch.setattr(dwc, "default_physical_cores", lambda: 12)
    monkeypatch.setattr(dwc, "default_usable_ram_gb", lambda: 24.0)
    resolved = cs_resolve_fast_test_cmd(str(tmp_path))
    assert "--maxprocesses=6" in resolved.cmd


def test_a_configured_command_without_a_worker_request_is_untouched(tmp_path, monkeypatch):
    """Parity: the resolver returns non-xdist commands exactly as before."""
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    cmd = "python3 -m pytest --timeout=300"
    resolved = cs_resolve_fast_test_cmd(str(_repo_with(tmp_path, cmd)))
    assert resolved.cmd == cmd


def _repo_with_full(tmp_path, full: str):
    (tmp_path / "coordinator.local.md").write_text(
        f'---\nproject_type: general\nfull_test_cmd: "{full}"\n---\n',
        encoding="utf-8",
        newline="\n",
    )
    return tmp_path


def test_full_tier_env_var_step_applies_the_cap(monkeypatch):
    """Review: code-reviewer P1 — `cs_resolve_full_test_cmd`'s own env-var
    step must apply the cap directly, not only via its Step-3 fast fallback."""
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", FAST)
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    monkeypatch.setattr(dwc, "default_physical_cores", lambda: 12)
    monkeypatch.setattr(dwc, "default_usable_ram_gb", lambda: 24.0)
    resolved = cs_resolve_full_test_cmd()
    assert resolved.exit_code == 0
    assert "--maxprocesses=6" in resolved.cmd


def test_full_tier_local_md_step_applies_the_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    monkeypatch.setattr(dwc, "default_physical_cores", lambda: 4)
    monkeypatch.setattr(dwc, "default_usable_ram_gb", lambda: 15.0)
    root = _repo_with_full(tmp_path, FAST)
    resolved = cs_resolve_full_test_cmd(str(root))
    assert resolved.exit_code == 0
    assert "--maxprocesses=2" in resolved.cmd


def test_an_unreadable_host_still_resolves_a_command(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    root = _repo_with(tmp_path, FAST)
    with _machine_unreadable():
        resolved = cs_resolve_fast_test_cmd(str(root))
    assert resolved.exit_code == 0
    assert resolved.cmd == FAST


# --- the runnable surface ----------------------------------------------------


def test_main_reports_the_derived_cap(capsys):
    assert main([]) == 0
    assert "derived cap:" in capsys.readouterr().out


def test_main_reports_an_underivable_host_and_exits_nonzero(capsys):
    with _machine_unreadable():
        assert main([]) == 2
    assert "cannot derive a cap" in capsys.readouterr().err
