"""Tests for C6: the fnm install step asks before it mutates the machine on
EVERY leg.

Covers the three findings from ledger item 5 (F-015):

1. The brew leg is behind the SAME consent gate as the curl leg (previously
   only ``_refuse_machine_mutation``, a test-harness switch, not human
   consent).
2. The brew invocation sets ``HOMEBREW_NO_AUTO_UPDATE=1`` and
   ``HOMEBREW_NO_INSTALL_CLEANUP=1`` so it cannot reach the operator's
   unrelated Homebrew caches.
3. ``_fnm_step`` checks the requirement (a usable ``node``), not just the
   tool (``fnm``) -- a box with node already on PATH must not trigger an
   install.

Negative-spec: a non-interactive/agent path (no tty, or
``COORDINATOR_NON_INTERACTIVE=1``) with neither env opt-in set must DECLINE
by default on both legs -- this is the "damaged the dogfooder's machine
irreversibly" finding, and the default must never flip to proceed silently.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.install import substrate


@pytest.fixture(autouse=True)
def _clean_fnm_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_INSTALL_FNM", raising=False)
    monkeypatch.delenv("COORDINATOR_NON_INTERACTIVE", raising=False)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


def _non_interactive(monkeypatch):
    monkeypatch.setattr(substrate.sys.stdin, "isatty", lambda: False)
    monkeypatch.setenv("COORDINATOR_NON_INTERACTIVE", "1")


def test_brew_leg_declined_by_default_non_interactive(monkeypatch):
    """The brew leg must decline, not proceed, with no consent signal."""
    _non_interactive(monkeypatch)
    assert substrate._fnm_brew_leg_declined() is True


def test_curl_leg_declined_by_default_non_interactive(monkeypatch):
    """Same default-decline posture on the curl leg (pre-existing behaviour,
    pinned here so the shared gate cannot regress it)."""
    _non_interactive(monkeypatch)
    monkeypatch.setattr(substrate.os, "name", "posix")
    assert substrate._fnm_curl_leg_declined() is True


def test_brew_leg_consents_via_env_opt_in(monkeypatch):
    monkeypatch.setenv("COORDINATOR_INSTALL_FNM", "1")
    assert substrate._fnm_brew_leg_declined() is False


def test_curl_leg_consents_via_env_opt_in(monkeypatch):
    monkeypatch.setattr(substrate.os, "name", "posix")
    monkeypatch.setenv("COORDINATOR_INSTALL_FNM", "1")
    assert substrate._fnm_curl_leg_declined() is False


def test_brew_leg_uses_the_same_gate_function_as_curl_leg():
    """Both legs route through the one shared consent primitive -- the
    finding was the ASYMMETRY between two independent checks, not the
    substance of either one."""
    import inspect

    brew_src = inspect.getsource(substrate._fnm_brew_leg_declined)
    curl_src = inspect.getsource(substrate._fnm_curl_leg_declined)
    assert "_fnm_mutation_declined(" in brew_src
    assert "_fnm_mutation_declined(" in curl_src


def test_fnm_step_declines_brew_leg_without_prompting_when_non_interactive(
    monkeypatch,
):
    """End-to-end through `_fnm_step`: no node, no fnm, brew present,
    non-interactive, no env opt-in -- must decline and must NOT invoke
    brew at all."""
    _non_interactive(monkeypatch)
    monkeypatch.setattr(substrate.shutil, "which", lambda name: (
        "/usr/local/bin/brew" if name == "brew" else None
    ))

    called = {}

    def _fake_run(argv, **kwargs):
        called["argv"] = argv
        called["kwargs"] = kwargs
        raise AssertionError("brew must not run without consent")

    monkeypatch.setattr(substrate, "_run", _fake_run)
    substrate._fnm_step(check_only=False)
    assert called == {}


def test_fnm_step_honours_harness_switch_even_with_consent_opt_in(monkeypatch):
    # Review: code-reviewer Finding 2 -- the harness switch
    # (COORDINATOR_DISABLE_MACHINE_MUTATION) and the human-consent gate
    # (COORDINATOR_INSTALL_FNM) are two independently-maintained checks in
    # `_fnm_step`; this pins that the harness switch is still honoured even
    # when consent has been granted, so a reorder of `_fnm_step`'s body
    # can't silently drop it.
    monkeypatch.setenv("COORDINATOR_INSTALL_FNM", "1")
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    monkeypatch.setattr(substrate.shutil, "which", lambda name: (
        "/usr/local/bin/brew" if name == "brew" else None
    ))

    def _fake_run(argv, **kwargs):
        raise AssertionError("brew must not run when the harness switch refuses mutation")

    monkeypatch.setattr(substrate, "_run", _fake_run)
    substrate._fnm_step(check_only=False)


def test_fnm_step_sets_homebrew_env_vars_on_consented_install(monkeypatch):
    """When consent IS given, the brew invocation must carry
    HOMEBREW_NO_AUTO_UPDATE=1 and HOMEBREW_NO_INSTALL_CLEANUP=1 -- `_run`
    never sets `env=` on its own, which is how a prior `brew install fnm`
    reached >50MB of the operator's unrelated caches."""
    monkeypatch.setenv("COORDINATOR_INSTALL_FNM", "1")
    monkeypatch.setattr(substrate.shutil, "which", lambda name: (
        "/usr/local/bin/brew" if name == "brew" else None
    ))

    captured = {}

    class _Proc:
        returncode = 0

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(substrate, "_run", _fake_run)
    substrate._fnm_step(check_only=False)

    assert captured["argv"] == ["brew", "install", "fnm"]
    env = captured["env"]
    assert env is not None
    assert env["HOMEBREW_NO_AUTO_UPDATE"] == "1"
    assert env["HOMEBREW_NO_INSTALL_CLEANUP"] == "1"


def test_fnm_step_skips_install_when_node_already_on_path(monkeypatch):
    """A box with node already usable on PATH must not trigger a brew/curl
    fnm install at all -- the requirement (usable node) is already met."""
    monkeypatch.setattr(substrate.shutil, "which", lambda name: (
        "/usr/local/bin/node" if name == "node" else "/usr/local/bin/brew" if name == "brew" else None
    ))

    def _fail_run(argv, **kwargs):
        raise AssertionError("must not attempt any install when node is already usable")

    monkeypatch.setattr(substrate, "_run", _fail_run)
    substrate._fnm_step(check_only=False)


def test_fnm_step_check_only_reports_node_present_as_noop(monkeypatch, capsys):
    monkeypatch.setattr(substrate.shutil, "which", lambda name: (
        "/usr/local/bin/node" if name == "node" else None
    ))
    substrate._fnm_step(check_only=True)
    out = capsys.readouterr().out
    assert "node already present" in out
    assert "no-op" in out
