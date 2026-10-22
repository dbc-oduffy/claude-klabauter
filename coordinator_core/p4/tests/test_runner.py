"""
test_runner.py — pytest coverage for coordinator_core.p4.runner.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § D2.

Classifier cases named by the plan spine row C1: the exit-0 lock refusal, a
plain-text connect-failure arm (P4CONFIG-beats-env: an explicit -p/-u/-c
identity that still hits a server the ambient P4CONFIG would have pointed
elsewhere never falls back to that ambient config — it just fails the
connect classifiably, same as any other unreachable port), and a blackholed
port timing out through the runner's own bounded timeout + process-tree
kill.

Recorded p4 error text is not vendored into this repo (the cockpit spike
lives in example-cockpit-repo); the fixture strings below are representative p4
`-s` script-mode output, matched by the classifier's substring rules, not a
byte-for-byte spike replay.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.p4 import runner


class TestClassifyError:
    def test_success_no_error_lines(self):
        assert runner.classify_error("info: //depot/foo#1 - opened for edit\n", "") is None

    def test_exit_0_lock_refusal_classifies_as_lock_held(self):
        stdout = "error: //depot/foo.uasset - locked by otheruser@otherclient\n"
        result = runner.classify_error(stdout, "")
        assert result is not None
        assert result.kind == "lock_held"
        assert result.holder == "otheruser@otherclient"

    def test_ticket_expired(self):
        stdout = "error: Your session has expired, please login again.\n"
        result = runner.classify_error(stdout, "")
        assert result is not None
        assert result.kind == "ticket_expired"

    def test_plain_text_connect_error_no_error_prefix(self):
        stderr = "Connect to server failed; check $P4PORT.\nTCP connect to ssl:p4.example.com:1666 failed.\n"
        result = runner.classify_error("", stderr)
        assert result is not None
        assert result.kind == "refused"

    def test_unclassified_error_folds_into_refused(self):
        stdout = "error: something the classifier has never seen before\n"
        result = runner.classify_error(stdout, "")
        assert result is not None
        assert result.kind == "refused"

    def test_default_change_conflict_folds_into_refused(self):
        stdout = "error: Change 41 unknown.\n"
        result = runner.classify_error(stdout, "")
        assert result is not None
        assert result.kind == "refused"


class _FakePopen:

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.pid = 4242
        self._kwargs = kwargs
        self._calls = 0

    def communicate(self, input=None, timeout=None):  # noqa: A002 — mirrors Popen's own name
        self._calls += 1
        self.input_seen = input
        if getattr(self, "_raise_timeout", False) and self._calls == 1:
            raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)
        return (self._stdout, self._stderr)


class TestRun:
    def test_passes_explicit_identity_flags_never_ambient_env(self, monkeypatch):
        # D1/D2: the runner always passes -p/-u/-c explicitly — this is
        # what makes "P4CONFIG-beats-env" a non-issue: the runner never
        # consults P4CONFIG or ambient P4* env at all.
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            fake = _FakePopen(cmd, **kwargs)
            fake._stdout = "info: ok\n"
            fake._stderr = ""
            return fake

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        result = runner.run("ssl:p4.example.com:1666", "agent", "agent-ws", ["info"])
        assert result.ok is True
        cmd = captured["cmd"]
        assert cmd[:8] == [
            "p4",
            "-p",
            "ssl:p4.example.com:1666",
            "-u",
            "agent",
            "-c",
            "agent-ws",
            "-s",
        ]

    def test_stdin_is_devnull(self, monkeypatch):
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["kwargs"] = kwargs
            fake = _FakePopen(cmd, **kwargs)
            fake._stdout = ""
            fake._stderr = ""
            return fake

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        runner.run("p4:1666", "u", "c", ["info"])
        assert captured["kwargs"]["stdin"] == subprocess.DEVNULL

    def test_spec_input_pipes_the_spec_and_leaves_every_other_verb_on_devnull(self, monkeypatch):
        seen = {}

        def fake_popen(cmd, **kwargs):
            seen["kwargs"] = kwargs
            fake = _FakePopen(cmd, **kwargs)
            fake._stdout = ""
            fake._stderr = ""
            seen["fake"] = fake
            return fake

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

        spec = "Change: new\nClient: c\nDescription:\n\tsid-abc\n"
        runner.run("p4:1666", "u", "c", ["change", "-i"], spec_input=spec)
        assert seen["kwargs"]["stdin"] == subprocess.PIPE
        assert seen["fake"].input_seen == spec

        runner.run("p4:1666", "u", "c", ["opened"])
        assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
        assert seen["fake"].input_seen is None

    def test_exit_code_ignored_lock_refusal_still_classified(self, monkeypatch):
        def fake_popen(cmd, **kwargs):
            fake = _FakePopen(cmd, **kwargs)
            fake._stdout = "error: //depot/foo.uasset - locked by other@other-ws\n"
            fake._stderr = ""
            return fake

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        result = runner.run("p4:1666", "u", "c", ["edit", "//depot/foo.uasset"])
        assert result.ok is False
        assert result.error.kind == "lock_held"

    def test_blackholed_port_times_out_and_kills_process_tree(self, monkeypatch):
        killed = {}

        def fake_popen(cmd, **kwargs):
            fake = _FakePopen(cmd, **kwargs)
            fake._raise_timeout = True
            fake._stdout = ""
            fake._stderr = ""
            return fake

        def fake_kill_tree(pid):
            killed["pid"] = pid

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(runner, "_kill_process_tree", fake_kill_tree)
        result = runner.run("p4:1666", "u", "c", ["info"], timeout=0.01)
        assert result.ok is False
        assert result.error.kind == "refused"
        assert result.error.raw == "timeout"
        assert killed["pid"] == 4242

    def test_never_retries_on_timeout(self, monkeypatch):
        spawn_count = {"n": 0}

        def fake_popen(cmd, **kwargs):
            spawn_count["n"] += 1
            fake = _FakePopen(cmd, **kwargs)
            fake._raise_timeout = True
            fake._stdout = ""
            fake._stderr = ""
            return fake

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(runner, "_kill_process_tree", lambda pid: None)
        runner.run("p4:1666", "u", "c", ["info"], timeout=0.01)
        assert spawn_count["n"] == 1


class TestRunDiscovery:
    def _present(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/p4")

    def test_non_allowlisted_verb_raises_value_error(self, monkeypatch):
        self._present(monkeypatch)
        for verb in ("submit", "edit", "set"):
            with pytest.raises(ValueError):
                runner.run_discovery([verb])
        with pytest.raises(ValueError):
            runner.run_discovery([])

    def test_absent_binary_is_not_installed_without_spawning(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name: None)

        def boom(*a, **k):
            raise AssertionError("spawned")

        monkeypatch.setattr(runner.subprocess, "Popen", boom)
        result = runner.run_discovery(["info"])
        assert result.ok is False
        assert result.error.kind == "refused"
        assert result.error.raw == "not_installed"

    def test_popen_oserror_is_not_installed_not_a_raise(self, monkeypatch):
        self._present(monkeypatch)

        def boom(*a, **k):
            raise FileNotFoundError("p4")

        monkeypatch.setattr(runner.subprocess, "Popen", boom)
        result = runner.run_discovery(["clients", "-u", "me"])
        assert result.ok is False
        assert result.error.raw == "not_installed"

    def test_argv_is_ambient_with_script_and_ztag(self, monkeypatch):
        self._present(monkeypatch)
        seen = {}

        def fake_popen(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            fake = _FakePopen(cmd, **kwargs)
            fake._stdout = "... Client c1\n"
            fake._stderr = ""
            return fake

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        result = runner.run_discovery(["clients", "-u", "me"])
        assert result.ok is True
        cmd = seen["cmd"]
        assert cmd[:3] == ["p4", "-s", "-ztag"]
        assert cmd[3:] == ["clients", "-u", "me"]
        assert not {"-p", "-c"} & set(cmd[:3])
        assert seen["kwargs"]["stdin"] == subprocess.DEVNULL

    def test_ticket_error_classifies_as_ticket_expired(self, monkeypatch):
        self._present(monkeypatch)

        def fake_popen(cmd, **kwargs):
            fake = _FakePopen(cmd, **kwargs)
            fake._stdout = "error: Your session has expired, please login again.\n"
            fake._stderr = ""
            return fake

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        result = runner.run_discovery(["info"])
        assert result.ok is False
        assert result.error.kind == "ticket_expired"

    def test_timeout_kills_tree_and_fails_closed(self, monkeypatch):
        self._present(monkeypatch)
        killed = {}

        def fake_popen(cmd, **kwargs):
            fake = _FakePopen(cmd, **kwargs)
            fake._raise_timeout = True
            fake._stdout = ""
            fake._stderr = ""
            return fake

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(runner, "_kill_process_tree", lambda pid: killed.setdefault("pid", pid))
        result = runner.run_discovery(["info"], timeout=0.01)
        assert result.ok is False
        assert result.error.raw == "timeout"
        assert killed["pid"] == 4242

    def test_single_popen_site_in_module(self):
        import inspect

        source = inspect.getsource(runner)
        assert source.count("subprocess.Popen(") == 1
