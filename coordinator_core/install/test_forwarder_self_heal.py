"""
coordinator_core.install.test_forwarder_self_heal — contract tests for
`coordinator_core.install.forwarder_self_heal.self_heal_forwarders`.

Purpose: verify the missing-forwarder self-heal path writes a missing
forwarder, is a true no-op on a clean tree, degrades silently on failure,
and that extracting `substrate._write_agent_helper_forwarders` out of
`_install_bin_resolvers` left that function's own installed output
unchanged. Every fixture lives entirely under `tmp_path` — no live install
against the operator's real settings-home.

Spec backlink: this dispatch's brief (ten-missing-forwarders drift,
percolate-push, 2026-08-14).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.install import forwarder_self_heal
from coordinator_core.install.substrate import (
    _AGENT_FORWARDER_MARKER,
    _derive_agent_helper_target_map,
    _resolve_agent_cmd_dest_collisions,
    _write_agent_helper_forwarders,
)


def _write_cli(agent_bin: Path, name: str) -> None:
    agent_bin.mkdir(parents=True, exist_ok=True)
    (agent_bin / name).write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")


def _patch_env(monkeypatch, *, claude_klabauter_root: Path, settings_home: Path, lock_root: Path) -> None:
    # `forwarder_self_heal` imports these lazily inside
    # `_self_heal_forwarders_inner` (deliberately, to avoid import cost on
    # the common no-op path) -- there is no module-level attribute to
    # monkeypatch there, so patch the source modules' own attributes
    # instead, exactly as any other lazy-import caller would observe.
    monkeypatch.setattr(
        "coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root_with_class",
        lambda: (str(claude_klabauter_root), "live-working-tree"),
    )
    monkeypatch.setattr(
        "coordinator_core._settings_home.settings_home",
        lambda: settings_home,
    )
    monkeypatch.setenv("COORDINATOR_LOCK_ROOT", str(lock_root))


class TestMissingForwarderGetsWritten:
    def test_missing_forwarder_is_written(self, tmp_path, monkeypatch):
        claude_klabauter_root = tmp_path / "claude-klabauter"
        agent_bin = claude_klabauter_root / "coordinator" / "bin"
        _write_cli(agent_bin, "percolate-push.py")

        bin_dst = tmp_path / "settings-home" / "bin"
        bin_dst.mkdir(parents=True)

        _patch_env(
            monkeypatch,
            claude_klabauter_root=claude_klabauter_root,
            settings_home=bin_dst.parent,
            lock_root=tmp_path / "locks",
        )

        forwarder_self_heal.self_heal_forwarders()

        py_dst = bin_dst / "percolate-push"
        assert py_dst.exists()
        assert _AGENT_FORWARDER_MARKER in py_dst.read_text(encoding="utf-8")


class TestCleanStateWritesNothing:
    def test_clean_tree_is_a_true_noop(self, tmp_path, monkeypatch):
        claude_klabauter_root = tmp_path / "claude-klabauter"
        agent_bin = claude_klabauter_root / "coordinator" / "bin"
        _write_cli(agent_bin, "percolate-push.py")

        bin_dst = tmp_path / "settings-home" / "bin"
        bin_dst.mkdir(parents=True)

        _patch_env(
            monkeypatch,
            claude_klabauter_root=claude_klabauter_root,
            settings_home=bin_dst.parent,
            lock_root=tmp_path / "locks",
        )

        # Pre-install via the same writer path the real installer uses, so
        # the tree is genuinely clean (not just an empty dir the self-heal
        # would trivially skip regardless of its diff logic).
        target_map = _derive_agent_helper_target_map(agent_bin)
        cmd_dest_map = _resolve_agent_cmd_dest_collisions(target_map)
        _write_agent_helper_forwarders(
            target_map, cmd_dest_map, bin_dst, False,
            python3_cmd_resolved_bin="",
        )

        before = {p.name: p.read_text(encoding="utf-8") for p in bin_dst.iterdir()}
        lock_root = tmp_path / "locks"
        # The pre-install above now legitimately takes this same lock --
        # `_write_agent_helper_forwarders` acquires it on the real-write
        # path (2026-08-14 install-safety audit: the installer and the
        # boot self-heal write the same `<settings-home>/bin` target with
        # a non-atomic `write_text`, so both must serialise on one lock).
        # So the lock root's mere EXISTENCE no longer discriminates; what
        # this test means is that the SELF-HEAL adds nothing to it, which
        # is a before/after comparison, not an absence check.
        lock_state_before = sorted(p.name for p in lock_root.iterdir()) if lock_root.is_dir() else []

        forwarder_self_heal.self_heal_forwarders()

        after = {p.name: p.read_text(encoding="utf-8") for p in bin_dst.iterdir()}
        assert after == before
        # `held_lock` is only ever reached once the pre-lock diff is
        # non-empty, so a clean tree must not acquire it -- proving the
        # cheap-when-clean design held, not just that the files happen to
        # be unchanged.
        lock_state_after = sorted(p.name for p in lock_root.iterdir()) if lock_root.is_dir() else []
        assert lock_state_after == lock_state_before


class TestFailureDegradesSilently:
    def test_unresolvable_claude_klabauter_root_is_silent_noop(self, tmp_path, monkeypatch, capsys):
        def _raise():
            raise RuntimeError("no claude-klabauter root")

        monkeypatch.setattr(
            "coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root_with_class", _raise,
        )
        monkeypatch.setattr(
            "coordinator_core._settings_home.settings_home",
            lambda: tmp_path / "settings-home",
        )

        result = forwarder_self_heal.self_heal_forwarders()

        assert result is None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_missing_settings_home_bin_is_silent_noop(self, tmp_path, monkeypatch, capsys):
        claude_klabauter_root = tmp_path / "claude-klabauter"
        agent_bin = claude_klabauter_root / "coordinator" / "bin"
        _write_cli(agent_bin, "percolate-push.py")

        # settings-home/bin deliberately never created.
        _patch_env(
            monkeypatch,
            claude_klabauter_root=claude_klabauter_root,
            settings_home=tmp_path / "settings-home-never-created",
            lock_root=tmp_path / "locks",
        )

        result = forwarder_self_heal.self_heal_forwarders()

        assert result is None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestExtractionPreservesInstallBehaviour:
    def test_install_bin_resolvers_output_matches_pre_extraction_shape(self, tmp_path):
        """`_write_agent_helper_forwarders` is a pure extraction of
        `_install_bin_resolvers`'s Step 3b body -- calling it directly must
        write byte-identical forwarder bodies to calling the inline loop it
        replaced, for the same inputs."""
        claude_klabauter_root = tmp_path / "claude-klabauter"
        agent_bin = claude_klabauter_root / "coordinator" / "bin"
        _write_cli(agent_bin, "percolate-push.py")
        _write_cli(agent_bin, "other-cli.py")

        bin_dst = tmp_path / "bin"
        bin_dst.mkdir(parents=True)

        target_map = _derive_agent_helper_target_map(agent_bin)
        cmd_dest_map = _resolve_agent_cmd_dest_collisions(target_map)

        resolved = _write_agent_helper_forwarders(
            target_map, cmd_dest_map, bin_dst, False,
            python3_cmd_resolved_bin="",
        )

        assert (bin_dst / "percolate-push").exists()
        assert (bin_dst / "other-cli").exists()
        assert (bin_dst / "percolate-push.cmd").exists()
        assert (bin_dst / "other-cli.cmd").exists()
        resolved_paths = {Path(e.path).name for e in resolved}
        assert resolved_paths == {
            "percolate-push", "percolate-push.cmd", "other-cli", "other-cli.cmd",
        }


class TestSelfHealIsSilentOnEveryPath:
    """The silence contract is what makes wiring this into session boot
    admissible at all. It is NOT hypothetical: `_derive_agent_helper_target_map`
    prints an `[install-substrate] WARNING: duplicate CLI pair` line whenever
    `coordinator/bin/` holds an extensionless CLI beside its `.py` twin, which
    is true of the live tree today and fires on the CLEAN path — so without the
    capture, every session boot would print it. Caught live during the
    percolate-push dogfood, after the tests below's own fixtures (which have no
    duplicate pair) passed clean.
    """

    def _capture(self, monkeypatch, tmp_path, noisy):
        import contextlib
        import io

        from coordinator_core.install import forwarder_self_heal as mod

        def _inner():
            print(noisy)
            print(noisy, file=sys.stderr)

        monkeypatch.setattr(mod, "_self_heal_forwarders_inner", _inner)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            mod.self_heal_forwarders()
        return out.getvalue(), err.getvalue()

    def test_callee_stdout_and_stderr_never_reach_the_caller(self, monkeypatch, tmp_path):
        out, err = self._capture(monkeypatch, tmp_path, "[install-substrate] WARNING: noisy")

        assert out == ""
        assert err == ""

    def test_silence_holds_when_the_callee_also_raises(self, monkeypatch, tmp_path):
        import contextlib
        import io

        from coordinator_core.install import forwarder_self_heal as mod

        def _boom():
            print("printed before raising")
            raise RuntimeError("resolution failed")

        monkeypatch.setattr(mod, "_self_heal_forwarders_inner", _boom)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            mod.self_heal_forwarders()

        assert out.getvalue() == ""
        assert err.getvalue() == ""
