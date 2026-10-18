from __future__ import annotations

import importlib.machinery
import importlib.util

import pytest

from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "session_liveness_cli", str(_BIN_DIR / "session-liveness-cli.py")
    )
    spec = importlib.util.spec_from_loader("session_liveness_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _StubLiveness:

    def __init__(self, *, session_live=None, session_verdict=None):
        self.session_live = session_live or (lambda *a, **k: False)
        self.session_verdict = session_verdict or (lambda *a, **k: None)


@pytest.fixture()
def stub_import_module():
    orig = _cli._import_module

    def _apply(stub):
        _cli._import_module = lambda: stub

    yield _apply
    _cli._import_module = orig


class _Holder:
    def __init__(self, sid, address, key="k", note="n"):
        self.session_id, self.address, self.key, self.note = sid, address, key, note
        self.claimed_at = "2026-10-01T00:00:00Z"


class _StubIC:
    def __init__(self, peers=(), raises=None):
        self.peers, self.raises, self.calls = list(peers), raises, []

    def set_claim(self, repo, key, note=None):
        self.calls.append(("set", repo, key, note))
        if self.raises:
            raise self.raises
        return type("R", (), {"peers": self.peers})()

    def release_claim(self, repo, key):
        self.calls.append(("release", repo, key))
        return True

    def list_peers(self, repo, key=None):
        self.calls.append(("list", repo, key))
        return self.peers


@pytest.fixture()
def stub_incident():
    orig_m, orig_i = _cli._import_module, _cli._import_incident

    def _apply(ic):
        core = type("C", (), {"git_root": staticmethod(lambda: "/cwd-root")})()
        _cli._import_module = lambda: object()
        _cli._import_incident = lambda: (core, ic)

    yield _apply
    _cli._import_module, _cli._import_incident = orig_m, orig_i


def test_incident_claim_prints_peer_lines_and_exits_0(stub_incident, capsys):
    ic = _StubIC([_Holder("s1", "addr1"), _Holder("s2", None)])
    stub_incident(ic)
    assert _cli.main(["incident-claim", "k", "--note", "hi", "--repo", "/r"]) == 0
    assert ic.calls == [("set", "/r", "k", "hi")]
    assert capsys.readouterr().out.splitlines() == [
        "s1 addr1 2026-10-01T00:00:00Z k -- n",
        "s2 unreachable 2026-10-01T00:00:00Z k -- n",
    ]


def test_incident_claim_defaults_repo_to_git_root_and_releases(stub_incident):
    ic = _StubIC()
    stub_incident(ic)
    assert _cli.main(["incident-claim", "k", "--release"]) == 0
    assert ic.calls == [("release", "/cwd-root", "k")]


def test_incident_claim_key_refusal_exits_2(stub_incident, capsys):
    stub_incident(_StubIC(raises=ValueError("bad key")))
    assert _cli.main(["incident-claim", "k"]) == 2
    assert "bad key" in capsys.readouterr().err


def test_incident_claim_usage_errors_exit_2(stub_incident):
    stub_incident(_StubIC())
    assert _cli.main(["incident-claim"]) == 2
    assert _cli.main(["incident-claim", "k", "--note"]) == 2
    assert _cli.main(["incident-claim", "k", "--bogus"]) == 2
    assert _cli.main(["incident-peers", "a", "b"]) == 2


def test_incident_peers_lists_with_optional_key(stub_incident, capsys):
    ic = _StubIC([_Holder("s1", "a1")])
    stub_incident(ic)
    assert _cli.main(["incident-peers"]) == 0
    assert _cli.main(["incident-peers", "k", "--repo", "/r"]) == 0
    assert ic.calls == [("list", "/cwd-root", None), ("list", "/r", "k")]
    assert capsys.readouterr().out.count("s1 a1 ") == 2


def test_incident_import_failure_exits_3(stub_incident):
    stub_incident(_StubIC())

    def _boom():
        raise ImportError("nope")

    _cli._import_incident = _boom
    assert _cli.main(["incident-peers"]) == 3


def test_all_zeros_sid_unknown_exits_1(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(session_live=lambda *a, **k: False, session_verdict=lambda *a, **k: None)
    )
    rc = _cli.main(["session-live", "00000000-0000-0000-0000-000000000000"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "unknown"


def test_ancient_session_dir_dead_exits_1(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: False,
            session_verdict=lambda *a, **k: (False, "recency-window", 999999),
        )
    )
    rc = _cli.main(["session-live", "ancient-sid"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "dead (recency-window)"


def test_live_foreign_repo_session_exits_4_distinct_from_dead(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: False,
            session_verdict=lambda *a, **k: (True, "harness-registry-elsewhere", "/some/other/repo"),
        )
    )
    rc = _cli.main(["session-live", "peer-sid"])
    assert rc == _cli._EXIT_LIVE_ELSEWHERE
    assert rc not in (0, 1)
    out = capsys.readouterr().out.strip()
    assert out == "live-elsewhere: /some/other/repo"


def test_live_foreign_repo_session_no_cwd_still_reports_elsewhere(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: False,
            session_verdict=lambda *a, **k: (True, "harness-registry-elsewhere", None),
        )
    )
    rc = _cli.main(["session-live", "peer-sid-no-cwd"])
    assert rc == _cli._EXIT_LIVE_ELSEWHERE
    assert capsys.readouterr().out.strip() == "live-elsewhere"


def test_live_same_repo_session_exits_0_no_longer_silent(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: True,
            session_verdict=lambda *a, **k: (True, "stable-pid", None),
        )
    )
    rc = _cli.main(["session-live", "my-sid"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "live (stable-pid)"
    assert out != ""


def test_toctou_window_true_flagged_verdict_never_prints_dead(stub_import_module, capsys):
    stub_import_module(
        _StubLiveness(
            session_live=lambda *a, **k: False,
            session_verdict=lambda *a, **k: (True, "stable-pid", None),
        )
    )
    rc = _cli.main(["session-live", "toctou-sid"])
    assert rc == 1
    out = capsys.readouterr().out.strip()
    assert out == "unknown"
    assert "dead" not in out


def test_live_true_still_exits_0_when_verdict_raises(stub_import_module, capsys):
    def _raise(*a, **k):
        raise RuntimeError("verdict computation failed")

    stub_import_module(
        _StubLiveness(session_live=lambda *a, **k: True, session_verdict=_raise)
    )
    rc = _cli.main(["session-live", "some-sid"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "live (unknown)"


def test_dead_false_still_exits_1_when_verdict_raises(stub_import_module, capsys):
    def _raise(*a, **k):
        raise RuntimeError("verdict computation failed")

    stub_import_module(
        _StubLiveness(session_live=lambda *a, **k: False, session_verdict=_raise)
    )
    rc = _cli.main(["session-live", "some-sid"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "unknown"


def test_missing_sid_exits_2(stub_import_module):
    stub_import_module(_StubLiveness())
    rc = _cli.main(["session-live"])
    assert rc == 2


def test_no_output_case_is_gone_every_arm_prints(stub_import_module, capsys):
    cases = [
        (False, None),
        (False, (False, "recency-window", 5)),
        (False, (True, "harness-registry-elsewhere", "/peer/repo")),
        (True, (True, "stable-pid", None)),
    ]
    for live, verdict in cases:
        def _live(*a, _l=live, **k):
            return _l

        def _verdict(*a, _v=verdict, **k):
            return _v

        stub_import_module(_StubLiveness(session_live=_live, session_verdict=_verdict))
        _cli.main(["session-live", "some-sid"])
        assert capsys.readouterr().out.strip() != ""


# --- pid-present ---------------------------------------------------------

import os
import subprocess
import sys

import psutil


@pytest.fixture()
def real_core(monkeypatch):
    import coordinator_core.session.core as core

    monkeypatch.setattr(_cli, "_import_core", lambda: core)
    return core


def test_pid_present_self_exits_0(real_core):
    assert _cli.main(["pid-present", str(os.getpid())]) == 0


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_pid_present_reaped_child_exits_1(real_core):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    if psutil.pid_exists(child.pid):
        pytest.skip("PID reused")
    assert _cli.main(["pid-present", str(child.pid)]) == 1


def test_pid_present_cmd_match_mismatch_exits_1_and_match_exits_0(real_core):
    pid = str(os.getpid())
    assert _cli.main(["pid-present", pid, "--cmd-match", "^definitely-not-this-zzq$"]) == 1
    assert _cli.main(["pid-present", pid, "--cmd-match", "."]) == 0


def test_pid_present_usage_errors_exit_2(real_core):
    assert _cli.main(["pid-present", "abc"]) == 2
    assert _cli.main(["pid-present", "0"]) == 2
    assert _cli.main(["pid-present", "-5"]) == 2
    assert _cli.main(["pid-present"]) == 2
    assert _cli.main(["pid-present", str(os.getpid()), "--cmd-match"]) == 2


def test_pid_present_bad_regex_exits_2(real_core):
    assert _cli.main(["pid-present", str(os.getpid()), "--cmd-match", "("]) == 2


def test_pid_present_does_not_shell_out(real_core, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("shell-out")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "system", _boom)
    assert _cli.main(["pid-present", str(os.getpid())]) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="resource module is POSIX-only")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_pid_present_process_time_under_500ms():
    import resource

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    proc = subprocess.run(
        [sys.executable, str(_BIN_DIR / "session-liveness-cli.py"), "pid-present", str(os.getpid())],
        capture_output=True,
    )
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    assert proc.returncode == 0, proc.stderr
    cpu = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    assert cpu < 0.5
