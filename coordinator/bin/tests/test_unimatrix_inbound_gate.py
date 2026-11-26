"""unimatrix-inbound-gate.py: one probe per far box, confirmed by receipts; stubs only, no live box."""
import importlib.util
import io
import os
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "unimatrix_inbound_gate_t", Path(__file__).resolve().parents[1] / "unimatrix-inbound-gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

SID = "11111111-2222-3333-4444-555555555555"
MEMBERS = [{"box": "machine-a", "host": "machine-a.local", "user": "me"},
           {"box": "mini", "host": "mini.local", "user": "me"},
           {"box": "example-fleet", "host": "example-fleet.local", "user": "me"}]
ENTRIES = [{"name": "mirror-x", "sessionId": "m1", "unimatrixRemote": "me@mini"},
           {"name": "group-em-de", "sessionId": SID}]


class World:
    """Stub ssh + receipts: `behaviour[box]` is 'ok', 'late' (receipt on the 3rd read), 'silent' or 'ssh-down'."""

    def __init__(self, behaviour):
        self.behaviour, self.rows, self.reads, self.ssh, self.armed = behaviour, [], {}, [], {}

    def run_ssh(self, member, key, command):
        box = member["box"]
        self.ssh.append((box, key, command))
        mode = self.behaviour[box]
        if mode == "ssh-down":
            return subprocess.CompletedProcess([command], 255, b"", b"Connection refused")
        if mode == "ok":
            self.rows.append({"session": SID, "digest": f"d-{box}", "box": "machine-a"})
        else:
            self.reads[box] = 0
            self.armed = {**self.armed, box: mode}
        return subprocess.CompletedProcess([command], 0, b"sent", b"")

    def fetch(self, sid, since):
        for box, mode in list(self.armed.items()):
            if mode == "late":
                self.reads[box] += 1
                if self.reads[box] >= 3 and not any(r["digest"] == f"d-{box}" for r in self.rows):
                    self.rows.append({"session": SID, "digest": f"d-{box}", "box": "machine-a"})
        return list(self.rows)


def route(m):
    return f"unimatrix-control-{m['box']}", f"key-{m['box']}"


def run_main(world, **kw):
    out = io.StringIO()
    kw.setdefault("required", None)
    rc = gate.main(["--timeout", "5", "--interval", "1"], out=out, run_ssh=world.run_ssh, fetch_receipts=world.fetch,
                   route_for=kw.pop("route_for", route), members=MEMBERS, entries=ENTRIES, local_box="machine-a",
                   sleep=lambda s: None, **kw)
    return rc, out.getvalue()


def test_every_far_box_arrives_exits_zero_and_skips_this_box():
    w = World({"mini": "ok", "example-fleet": "ok"})
    rc, out = run_main(w)
    assert rc == 0
    assert out.splitlines() == ["mini: arrived", "example-fleet: arrived"]
    assert [b for b, _, _ in w.ssh] == ["mini", "example-fleet"]
    assert all(" --to group-em-de.machine-a 'inbound-gate-probe from " in c for _, _, c in w.ssh)
    assert w.ssh[0][1] == "key-mini"


def test_a_receipt_after_the_first_poll_is_queued_then_delivered_and_still_passes():
    rc, out = run_main(World({"mini": "late", "example-fleet": "ok"}))
    assert rc == 0
    assert out.splitlines()[0] == "mini: queued-then-delivered"


def test_a_probe_that_never_lands_exits_nonzero_naming_the_box(capsys):
    rc, out = run_main(World({"mini": "silent", "example-fleet": "ok"}))
    assert rc == 1
    assert "mini: missing (hop relay-in" in out
    assert "example-fleet: arrived" in out
    assert "mini" in capsys.readouterr().err


def test_an_ssh_failure_is_missing_at_the_ssh_hop_and_every_missing_box_is_named(capsys):
    rc, out = run_main(World({"mini": "ssh-down", "example-fleet": "silent"}))
    assert rc == 1
    assert "mini: missing (hop ssh: Connection refused)" in out
    err = capsys.readouterr().err
    assert "mini" in err and "example-fleet" in err


TIMEOUT_ERR = b"ssh: connect to host 192.168.0.116 port 22: Connection timed out"


def _flaky_ssh(failures, stderr=TIMEOUT_ERR):
    calls = []

    def run_ssh(member, key, command):
        calls.append(command)
        if len(calls) <= failures:
            return subprocess.CompletedProcess([command], 255, b"", stderr)
        return subprocess.CompletedProcess([command], 0, b"sent", b"")
    return run_ssh, calls


def test_one_ssh_connect_timeout_is_retried_once_and_noted_on_the_box_line():
    run_ssh, calls = _flaky_ssh(1)
    out = io.StringIO()
    missing = gate.run_gate([{"box": "mini"}], ("n", SID), run_ssh=run_ssh, route_for=route, out=out,
                            fetch_receipts=lambda s, t: [{"digest": "d"}] if len(calls) > 1 else [])
    assert missing == [] and len(calls) == 2
    assert out.getvalue().strip() == f"mini: arrived ({gate.RETRY_NOTE})"


def test_a_box_that_times_out_twice_still_fails_after_exactly_one_retry():
    run_ssh, calls = _flaky_ssh(5)
    out = io.StringIO()
    missing = gate.run_gate([{"box": "mini"}], ("n", SID), run_ssh=run_ssh, route_for=route, out=out,
                            fetch_receipts=lambda s, t: [])
    assert missing == ["mini"] and len(calls) == 2
    assert "mini: missing (hop ssh: " in out.getvalue() and gate.RETRY_NOTE in out.getvalue()


@pytest.mark.parametrize("stderr", [b"Connection refused", b"TimeoutExpired: timed out after 60 seconds",
                                    b"Permission denied (publickey)"])
def test_only_an_ssh_connect_timeout_is_retried(stderr):
    run_ssh, calls = _flaky_ssh(5, stderr)
    _, status, hop, detail = gate.probe_box({"box": "mini"}, ("n", SID), run_ssh=run_ssh,
                                            fetch_receipts=lambda s, t: [], key="k")
    assert (status, hop, len(calls)) == (gate.MISSING, "ssh", 1)
    assert gate.RETRY_NOTE not in detail


@pytest.mark.parametrize("member_box,label", [("Machine-b", "machine-b"), ("Mac-mini", "mac-mini")])
def test_deployed_relay_shaped_rows_arrive_for_a_mixed_case_member(member_box, label):
    deployed = {"session": SID, "digest": "probe-d", "at": 100.0, "box": "machine-a", "ref": "n.machine-a",
                "name": "n", "pid": 7, "ts": "t", "label": label}
    stale = {"session": SID, "digest": "old-d", "at": 50.0, "box": "machine-a"}
    rows = [stale]

    def run_ssh(member, key, command):
        rows.append(deployed)
        return subprocess.CompletedProcess([command], 0, b"sent", b"")

    box, status, hop, _ = gate.probe_box({"box": member_box}, ("n", SID), run_ssh=run_ssh,
                                         fetch_receipts=lambda s, t: list(rows), key="k", timeout=2, interval=1,
                                         sleep=lambda s: None)
    assert (box, status) == (member_box, gate.ARRIVED)


def test_a_digest_already_in_the_baseline_is_not_the_probe():
    rows = [{"session": SID, "digest": "earlier"}]
    _, status, hop, _ = gate.probe_box({"box": "Machine-b"}, ("n", SID),
                                       run_ssh=lambda m, k, c: subprocess.CompletedProcess([c], 0, b"", b""),
                                       fetch_receipts=lambda s, t: list(rows), key="k", timeout=2, interval=1,
                                       sleep=lambda s: None)
    assert (status, hop) == (gate.MISSING, "relay-in")


def test_a_refused_qualified_name_falls_back_to_the_bare_name_in_one_ssh_call(tmp_path):
    import shutil
    bash = shutil.which("bash")
    if not bash or "system32" in bash.lower():
        pytest.skip("no bash on PATH")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "unimatrix-send").write_text('case "$2" in *.*) echo refused >&2; exit 1;; esac\necho SENT "$2"\n')
    seen = []

    def run_ssh(member, key, command):
        seen.append(command)
        return subprocess.run([bash, "-c", command], capture_output=True,
                              env={**os.environ, "COORDINATOR_SETTINGS_HOME": tmp_path.as_posix()})

    gate.probe_box({"box": "mini"}, ("n", SID), run_ssh=run_ssh, fetch_receipts=lambda s, t: [], key="k",
                   timeout=1, interval=1, sleep=lambda s: None, local_box="machine-a")
    assert len(seen) == 1 and " --to n.machine-a " in seen[0] and " || s --to n " in seen[0]
    res = run_ssh({}, "k", seen[0])
    assert res.returncode == 0 and res.stdout.decode().strip() == "SENT n"


def test_the_default_target_prefers_a_session_name_no_other_session_shares():
    dup = [{"name": "dup", "sessionId": "a"}, {"name": "dup", "sessionId": "b", "unimatrixMirror": True},
           {"name": "solo", "sessionId": "c"}]
    assert gate.local_target(dup) == ("solo", "c")
    assert gate.local_target(dup[:2]) == ("dup", "a")


def test_the_default_target_prefers_a_claude_session_over_an_external_vendor_leg():
    rows = [{"name": "agy-x-on-machine-a", "sessionId": "a", "vendor": "agy", "leg_mode": "pending"},
            {"name": "grok-y", "sessionId": "b", "vendor": "grok"},
            {"name": "em-z", "sessionId": "c"}]
    assert gate.local_target(rows) == ("em-z", "c")
    assert gate.local_target(rows[:2]) == ("grok-y", "b")


def test_the_far_snippet_runs_a_native_launcher_directly(tmp_path):
    import shutil
    bash = shutil.which("bash")
    if not bash or "system32" in bash.lower():
        pytest.skip("no bash on PATH")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "unimatrix-send").write_text("echo NATIVE \"$@\"\n")
    res = subprocess.run([bash, "-c", f"{gate.far_send_command()} --to n.machine-a x"], capture_output=True, text=True,
                         env={**os.environ, "COORDINATOR_SETTINGS_HOME": tmp_path.as_posix()})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "NATIVE --to n.machine-a x"


def test_a_receipt_predating_the_probe_does_not_count():
    w = World({"mini": "silent", "example-fleet": "ok"})
    w.rows.append({"session": SID, "digest": "stranger", "box": "machine-a"})
    box, status, hop, _ = gate.probe_box({"box": "mini"}, ("group-em-de", SID), run_ssh=w.run_ssh,
                                         fetch_receipts=w.fetch, key="k", timeout=2, interval=1, sleep=lambda s: None)
    assert status == gate.MISSING and hop == "relay-in"


def test_a_failed_send_with_no_receipt_is_missing_at_the_send_hop():
    def run_ssh(member, key, command):
        return subprocess.CompletedProcess([command], 1, b"", b"undelivered: group-em-de: router not reachable")

    _, status, hop, detail = gate.probe_box({"box": "mini"}, ("group-em-de", SID), run_ssh=run_ssh,
                                            fetch_receipts=lambda s, t: [], key="k", timeout=2, interval=1,
                                            sleep=lambda s: None)
    assert (status, hop) == (gate.MISSING, "send") and "router not reachable" in detail


def test_the_poll_budget_is_receipts_reads_not_a_clock():
    reads = []
    gate.probe_box({"box": "mini"}, ("n", SID), run_ssh=lambda m, k, c: subprocess.CompletedProcess([c], 0, b"", b""),
                   fetch_receipts=lambda s, t: reads.append(1) or [], key="k", timeout=4, interval=1,
                   sleep=lambda s: None)
    assert len(reads) == 1 + 4


def test_no_local_target_session_is_a_usage_failure_not_a_pass(capsys):
    out = io.StringIO()
    rc = gate.main([], out=out, run_ssh=None, fetch_receipts=lambda s, t: [], route_for=route,
                   members=MEMBERS, entries=[ENTRIES[0]], local_box="machine-a", required=None)
    assert rc == 2
    assert "no live local session" in capsys.readouterr().err


def test_target_default_skips_mirrors_and_an_explicit_pair_wins():
    assert gate.local_target(ENTRIES) == ("group-em-de", SID)
    assert gate.local_target(ENTRIES, "other", "sid-9") == ("other", "sid-9")
    assert gate.local_target([ENTRIES[0]]) is None


def test_unusable_membership_exits_two(monkeypatch, capsys):
    mem = gate._sibling("unimatrix-membership.py", "unimatrix_inbound_gate_membership")

    def boom(home):
        raise mem.MembershipError("snapshot present but no signer pin")

    monkeypatch.setattr(mem, "verified_members", boom)
    assert gate.main([], out=io.StringIO(), entries=ENTRIES, local_box="machine-a", required=None) == 2
    assert "membership unusable" in capsys.readouterr().err


def test_the_ssh_argv_dials_the_control_alias_with_the_pinned_key_and_no_agent(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    gate._ssh_runner()({"box": "mini", "alias": "unimatrix-control-mini"}, "/k/id", "unimatrix-send --to a b")
    argv = seen["argv"]
    assert argv[0] == "ssh" and argv[-2:] == ["unimatrix-control-mini", "unimatrix-send --to a b"]
    assert argv[argv.index("-i") + 1] == "/k/id"
    assert "IdentitiesOnly=yes" in argv and "IdentityAgent=none" in argv and "BatchMode=yes" in argv
    assert not any(a.startswith("HostKeyAlias") for a in argv)


def test_route_comes_from_the_control_alias_and_key_never_unimatrix_zero(tmp_path):
    (tmp_path / "unimatrix_control_macmini").write_text("k")
    (tmp_path / "unimatrix_control_gipsy").write_text("k")
    assert gate.control_route("Mac-mini", tmp_path) == ("unimatrix-control-macmini", str(tmp_path / "unimatrix_control_macmini"))
    assert gate.control_route("gipsy", tmp_path) == ("unimatrix-control-gipsy", str(tmp_path / "unimatrix_control_gipsy"))
    assert "zero" not in "".join(gate.control_route("gipsy", tmp_path))


def test_a_box_with_no_control_key_is_missing_at_the_route_hop_never_a_fake_arrive(tmp_path):
    reason = gate.control_route("Machine-b", tmp_path)
    assert isinstance(reason, str) and "no control key" in reason and "--control-key Machine-b" in reason
    w = World({"mini": "ok", "example-fleet": "ok"})
    rc, out = run_main(w, route_for=lambda m: reason if m["box"] == "example-fleet" else route(m))
    assert rc == 1 and "example-fleet: missing (hop route: no control key" in out and "mini: arrived" in out
    assert [b for b, _, _ in w.ssh] == ["mini"]


def test_the_probe_command_runs_unimatrix_send_by_path_over_the_control_route():
    w = World({"mini": "ok", "example-fleet": "ok"})
    run_main(w)
    cmd = w.ssh[0][2]
    assert w.ssh[0][1] == "key-mini"
    assert 'exec "$PY" "$F" "$@"' in cmd and "/bin/unimatrix-send" in cmd and "; s --to group-em-de.machine-a " in cmd
    assert not cmd.startswith("unimatrix-send")
    assert cmd.index("/opt/homebrew/bin/python3") < cmd.index("/usr/bin/python3")


def test_an_explicit_send_command_override_is_used_verbatim_and_unqualified_without_a_local_box():
    seen = []

    def run_ssh(member, key, command):
        seen.append(command)
        return subprocess.CompletedProcess([command], 0, b"", b"")

    gate.probe_box({"box": "mini"}, ("n", SID), run_ssh=run_ssh, fetch_receipts=lambda s, t: [], key="k",
                   send_command="my-send", timeout=1, interval=1, sleep=lambda s: None)
    assert seen[0].startswith("my-send --to n ")


def _bash_with_python():
    import shutil
    bash = shutil.which("bash")
    if not bash or "system32" in bash.lower():
        return None
    probe = subprocess.run([bash, "-c", 'command -v python3 >/dev/null 2>&1 && python3 -c "import sys; sys.exit(sys.version_info < (3, 11))"'],
                           capture_output=True)
    return bash if probe.returncode == 0 else None


def test_the_far_snippet_resolves_an_interpreter_and_runs_the_script_by_path(tmp_path):
    bash = _bash_with_python()
    if not bash:
        pytest.skip("no bash with python >= 3.11 on PATH")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "unimatrix-send").write_text("#!/usr/bin/env python3\nimport sys\nprint('ARGS', *sys.argv[1:])\n")
    cmd = f"{gate.far_send_command()} --to n.machine-a 'a b'"
    res = subprocess.run([bash, "-c", cmd], capture_output=True, text=True,
                         env={**os.environ, "COORDINATOR_SETTINGS_HOME": tmp_path.as_posix()})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ARGS --to n.machine-a a b"


def test_the_far_snippet_with_no_python_311_is_a_typed_refusal_not_an_old_python_run(tmp_path):
    import shutil
    bash = shutil.which("bash")
    if not bash or "system32" in bash.lower():
        pytest.skip("no bash on PATH")
    if any(os.path.exists(p) for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3")):
        pytest.skip("an absolute interpreter candidate exists on this box")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "unimatrix-send").write_text("#!/usr/bin/env python3\nprint('RAN')\n")
    (tmp_path / "py").mkdir()
    fake = tmp_path / "py" / "python3"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    env = {**os.environ, "COORDINATOR_SETTINGS_HOME": tmp_path.as_posix(), "HOME": tmp_path.as_posix(),
           "PATH": tmp_path.joinpath("py").as_posix() + os.pathsep + os.environ.get("PATH", "")}
    res = subprocess.run([bash, "-c", f"{gate.far_send_command()} --to n.machine-a x"], capture_output=True, text=True,
                         env=env)
    assert res.returncode == 127 and "no python3>=3.11" in res.stderr and "RAN" not in res.stdout

    def run_ssh(member, key, command):
        return subprocess.CompletedProcess([command], 127, b"", b"no python3>=3.11 on this box")

    _, status, hop, detail = gate.probe_box({"box": "mini"}, ("n", SID), run_ssh=run_ssh,
                                            fetch_receipts=lambda s, t: [], key="k", timeout=1, interval=1,
                                            sleep=lambda s: None, local_box="machine-a")
    assert (status, hop) == (gate.MISSING, "send") and "no python3>=3.11" in detail


def test_the_target_suffix_is_the_lowercased_local_box_id():
    seen = []

    def run_ssh(member, key, command):
        seen.append(command)
        return subprocess.CompletedProcess([command], 0, b"", b"")

    gate.probe_box({"box": "mini"}, ("n", SID), run_ssh=run_ssh, fetch_receipts=lambda s, t: [], key="k",
                   timeout=1, interval=1, sleep=lambda s: None, local_box="Machine-a")
    assert "; s --to n.machine-a " in seen[0] and "n.Machine-a" not in seen[0]


def test_only_required_boxes_fail_the_exit_code(capsys):
    rc, out = run_main(World({"mini": "ok", "example-fleet": "silent"}), required={"mini"})
    assert rc == 0
    assert "example-fleet: missing (hop relay-in: no receipt for " in out and out.splitlines()[-1].endswith("[not required]")
    rc, _ = run_main(World({"mini": "ok", "example-fleet": "silent"}), required={"mini", "example-fleet"})
    assert rc == 1 and "example-fleet" in capsys.readouterr().err


def test_a_required_box_absent_from_the_roster_fails(capsys):
    rc, out = run_main(World({"mini": "ok", "example-fleet": "ok"}), required={"mini", "ghost"})
    assert rc == 1 and "ghost: missing (hop roster" in out
    assert "ghost" in capsys.readouterr().err


def _main_with_unset_key(monkeypatch, world):
    mux = gate._sibling("lib/unimatrix_box_mux.py", "unimatrix_inbound_gate_mux")
    monkeypatch.setattr(mux, "machine_local_value", lambda name: "")
    return gate.main(["--timeout", "5", "--interval", "1"], out=io.StringIO(), run_ssh=world.run_ssh,
                     fetch_receipts=world.fetch, route_for=route, members=MEMBERS, entries=ENTRIES,
                     local_box="machine-a", sleep=lambda s: None)


def test_an_unset_required_key_requires_every_far_box_and_names_the_key(monkeypatch, capsys):
    assert _main_with_unset_key(monkeypatch, World({"mini": "ok", "example-fleet": "silent"})) == 1
    err = capsys.readouterr().err
    assert "unimatrix.inbound_gate_boxes" in err and "example-fleet" in err
    assert _main_with_unset_key(monkeypatch, World({"mini": "ok", "example-fleet": "ok"})) == 0


def test_the_required_key_parses_commas_and_spaces_case_folded():
    assert gate.required_boxes(lambda n: "Mac-mini, gipsy Machine-b" if n == gate.REQUIRED_KEY else "") == {
        "mac-mini", "gipsy", "machine-b"}
    assert gate.required_boxes(lambda n: "") is None


def test_the_oracle_ignores_any_sender_field_a_row_may_or_may_not_carry():
    def probe(extra):
        rows = []

        def run_ssh(member, key, command):
            rows.append({"session": SID, "digest": "new", **extra})
            return subprocess.CompletedProcess([command], 0, b"", b"")

        return gate.probe_box({"box": "mini"}, ("n", SID), run_ssh=run_ssh, fetch_receipts=lambda s, t: list(rows),
                              key="k", timeout=2, interval=1, sleep=lambda s: None)[1]

    assert probe({}) == gate.ARRIVED
    assert probe({"label": "10.0.0.5"}) == gate.ARRIVED
