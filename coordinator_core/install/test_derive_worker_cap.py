
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
    cmd = "python3 -m pytest -m 'not pending_fix' --timeout=300"
    assert apply_cap_to_command(cmd, 12) == cmd


def test_an_explicit_worker_count_is_not_a_request_for_the_host_core_count():
    cmd = "pytest -n 4 --timeout=300"
    assert apply_cap_to_command(cmd, 12) == cmd


def test_a_duplicated_ceiling_flag_cannot_defeat_the_cap():
    out = apply_cap_to_command("pytest -n auto --maxprocesses=7 --maxprocesses=9", 2)
    assert out == "pytest -n auto --maxprocesses=2 --maxprocesses=2"


def test_rewriting_is_idempotent():
    once = apply_cap_to_command(FAST, 12)
    assert apply_cap_to_command(once, 12) == once


def test_a_worker_request_inside_a_quoted_marker_expression_is_not_a_real_request():
    cmd = 'pytest -k "not -n auto" -m "not cadence"'
    assert apply_cap_to_command(cmd, 12) == cmd


def test_an_existing_maxprocesses_inside_a_quoted_expression_is_not_touched():
    cmd = 'pytest -n auto -k "text with --maxprocesses=7 inside"'
    out = apply_cap_to_command(cmd, 12)
    assert "--maxprocesses=12" in out
    assert '"text with --maxprocesses=7 inside"' in out


def test_an_unterminated_quote_fails_open_not_closed():
    """`_QUOTE_SPAN` requires a closing quote to
    match, so a stray/unbalanced opening quote leaves everything after it
    scanned as if unquoted: a `--maxprocesses=` occurrence a human would read
    as still inside the quote gets detected and rewritten anyway. Pinned
    deliberately, not fixed: a command with an unbalanced quote is already
    malformed and will fail at the shell regardless, so this is the
    documented (fail-open) behaviour, not a defect this test guards against."""
    cmd = 'pytest -n auto -k "unterminated --maxprocesses=7'
    out = apply_cap_to_command(cmd, 12)
    assert out == 'pytest -n auto -k "unterminated --maxprocesses=12'


def _headroom(monkeypatch, ram_gb: float, load: float, cpus: int) -> None:
    monkeypatch.setattr(dwc, "read_measured_headroom", lambda: (ram_gb, load, cpus))


def test_changed_headroom_resolves_to_a_different_number(monkeypatch):
    for ram, load, cpus, expected in (
        (96.0, 0.0, 24, 24),
        (96.0, 0.5, 24, 12),
        (15.0, 0.5, 4, 2),
        (0.6, 0.0, 24, 4),
        (96.0, 0.99, 24, 1),
    ):
        _headroom(monkeypatch, ram, load, cpus)
        resolved, cap = cap_command_for_this_box(FAST)
        assert cap == expected
        assert f"--maxprocesses={expected}" in resolved


def test_the_same_box_derives_fewer_workers_when_busier(monkeypatch):
    _headroom(monkeypatch, 64.0, 0.1, 16)
    quiet = derive_cap()
    _headroom(monkeypatch, 64.0, 0.8, 16)
    assert derive_cap() < quiet


def test_the_derivation_reads_available_not_total_memory(monkeypatch):
    from coordinator_core.telemetry import host_sampler

    monkeypatch.setattr(
        host_sampler,
        "read_load",
        lambda: {"cpu_load": 0.0, "mem_avail_mb": 300, "mem_total_mb": 96 * 1024, "platform": "linux"},
    )
    monkeypatch.setattr("os.cpu_count", lambda: 24)
    assert derive_cap() == 2


def test_the_windows_load_path_feeds_the_cap(monkeypatch):
    """Windows load comes from host_sampler's GetSystemTimes reader, not psutil."""
    from coordinator_core.telemetry import host_sampler

    monkeypatch.setattr(host_sampler, "_IS_WINDOWS", True)
    monkeypatch.setattr(host_sampler, "_windows_cpu_load", lambda: 0.75)
    monkeypatch.setattr(host_sampler, "avail_mb", lambda: (32 * 1024, 64 * 1024))
    monkeypatch.setattr("os.cpu_count", lambda: 16)
    assert derive_cap() == 4


def test_a_psutil_absent_host_still_derives_a_cap(monkeypatch):
    import builtins

    from coordinator_core.telemetry import host_sampler

    real_import = builtins.__import__

    def _no_psutil(name, *a, **kw):
        if name == "psutil" or name.startswith("psutil."):
            raise ImportError("psutil absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    monkeypatch.setattr(
        host_sampler,
        "read_load",
        lambda: {"cpu_load": 0.5, "mem_avail_mb": 8192, "mem_total_mb": 16384, "platform": "linux"},
    )
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    assert derive_cap() == 4


def test_an_unreadable_load_figure_raises_not_guesses(monkeypatch):
    from coordinator_core.telemetry import host_sampler

    monkeypatch.setattr(
        host_sampler,
        "read_load",
        lambda: {"cpu_load": None, "mem_avail_mb": 8192, "mem_total_mb": 16384, "platform": "other"},
    )
    with pytest.raises(RuntimeError):
        derive_cap()


def _no_headroom_figure():
    raise RuntimeError("host load unreadable")


@contextmanager
def _machine_unreadable():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dwc, "read_measured_headroom", _no_headroom_figure)
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


def _repo_with(tmp_path, fast: str):
    (tmp_path / "coordinator.local.md").write_text(
        f'---\nproject_type: general\nfast_test_cmd: "{fast}"\n---\n',
        encoding="utf-8",
        newline="\n",
    )
    return tmp_path


def test_the_resolver_hands_back_this_box_s_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    _headroom(monkeypatch, 15.0, 0.5, 4)
    resolved = cs_resolve_fast_test_cmd(str(_repo_with(tmp_path, FAST)))
    assert resolved.exit_code == 0
    assert "--maxprocesses=2" in resolved.cmd


def test_the_tracked_config_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    root = _repo_with(tmp_path, FAST)
    before = (root / "coordinator.local.md").read_bytes()
    cs_resolve_fast_test_cmd(str(root))
    assert (root / "coordinator.local.md").read_bytes() == before


def test_the_env_var_escape_hatch_is_capped_too(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", FAST)
    _headroom(monkeypatch, 24.0, 0.5, 12)
    resolved = cs_resolve_fast_test_cmd(str(tmp_path))
    assert "--maxprocesses=6" in resolved.cmd


def test_a_configured_command_without_a_worker_request_is_untouched(tmp_path, monkeypatch):
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
    monkeypatch.setenv("COORDINATOR_FULL_TEST_CMD", FAST)
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    _headroom(monkeypatch, 24.0, 0.5, 12)
    resolved = cs_resolve_full_test_cmd()
    assert resolved.exit_code == 0
    assert "--maxprocesses=6" in resolved.cmd


def test_full_tier_local_md_step_applies_the_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    _headroom(monkeypatch, 15.0, 0.5, 4)
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


def test_main_reports_the_derived_cap(capsys):
    assert main([]) == 0
    assert "derived cap:" in capsys.readouterr().out


def test_main_reports_an_underivable_host_and_exits_nonzero(capsys):
    with _machine_unreadable():
        assert main([]) == 2
    assert "cannot derive a cap" in capsys.readouterr().err
