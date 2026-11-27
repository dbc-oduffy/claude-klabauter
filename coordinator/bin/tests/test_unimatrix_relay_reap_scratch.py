"""Behavior pins for the unimatrix relay `reap-scratch` verb."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_reap_scratch", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load()


def make_leg(root, *parts, codex=".codex"):
    leg = root.joinpath(*parts)
    (leg / "home" / codex).mkdir(parents=True)
    (leg / "marker.txt").write_text("x")
    return leg


def test_parse_mode_takes_no_argument():
    assert R.parse_mode("reap-scratch") == ("reap-scratch", None)
    assert R.parse_mode("reap-scratch /tmp") is None
    assert R.parse_mode("reap-scratch; rm -rf /") is None


def test_codex_legs_are_the_parent_of_home_at_any_depth(tmp_path):
    deep = make_leg(tmp_path, "astra-all-sizings", "qual", "fresh-host-abc", codex="codex")
    shallow = make_leg(tmp_path, "f90214-staged.XyZ")
    (tmp_path / "plain" / "home" / "other").mkdir(parents=True)
    assert sorted(R._codex_legs(tmp_path)) == sorted([deep, shallow])


def test_leg_that_is_the_root_itself_is_never_a_leg(tmp_path):
    (tmp_path / "home" / "codex").mkdir(parents=True)
    assert R._codex_legs(tmp_path) == []


def test_reaped_leg_is_removed_and_unreaped_leg_survives(tmp_path):
    reaped = make_leg(tmp_path, "fresh-host-1")
    quiet = make_leg(tmp_path, "fresh-host-2")
    calls = []

    def reap(leg):
        calls.append(leg)
        return [4242] if leg == reaped else []

    reply = R.reap_scratch(roots=[tmp_path], reap=reap, alive_under=lambda leg: False)
    assert reply == {"ok": True, "killed": [4242], "removed": [str(reaped)]}
    assert not reaped.exists()
    assert quiet.exists()
    assert sorted(calls) == sorted([reaped, quiet])
    again = R.reap_scratch(roots=[tmp_path], reap=lambda leg: [], alive_under=lambda leg: False)
    assert again == {"ok": True, "killed": [], "removed": []}


def test_leg_with_a_surviving_process_is_not_removed(tmp_path):
    leg = make_leg(tmp_path, "fresh-host-1")
    reply = R.reap_scratch(roots=[tmp_path], reap=lambda l: [7], alive_under=lambda l: True)
    assert reply["killed"] == [7] and reply["removed"] == []
    assert leg.exists()


def test_reaper_failure_replies_not_ok_without_raising(tmp_path):
    make_leg(tmp_path, "fresh-host-1")

    def boom(leg):
        raise RuntimeError("x")

    reply = R.reap_scratch(roots=[tmp_path], reap=boom, alive_under=lambda l: False)
    assert reply["ok"] is False and reply["reason"] == "reap_failed:RuntimeError"


def test_scratch_roots_include_repo_scratch_and_configured_root(monkeypatch, tmp_path):
    import types
    fake = types.SimpleNamespace(registry_get=lambda key: str(tmp_path) if key == "paths.scratch_root" else None)
    monkeypatch.setitem(__import__("sys").modules, "coordinator_core", types.SimpleNamespace(machine_resolver=fake))
    monkeypatch.setitem(__import__("sys").modules, "coordinator_core.machine_resolver", fake)
    repo = RELAY.resolve().parents[2]
    assert R._scratch_roots() == [repo / "scratch", tmp_path / repo.name]


def test_reap_scratch_reply_is_under_the_500ms_process_time_bar(tmp_path):
    for i in range(20):
        make_leg(tmp_path, "nest", f"fresh-host-{i}")
    (tmp_path / "node_modules" / "x" / "home" / ".codex").mkdir(parents=True)
    start = time.process_time()
    reply = R.reap_scratch(roots=[tmp_path], reap=lambda leg: [], alive_under=lambda leg: False)
    assert reply == {"ok": True, "killed": [], "removed": []}
    assert time.process_time() - start < 0.5


def test_real_reaper_kills_a_codex_shaped_process_and_removes_its_leg(tmp_path):
    leg = make_leg(tmp_path, "fresh-host-live")
    script = leg / "home" / ".codex" / "codex"
    script.write_text("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, str(script), "app-server", "--managed-daemon"],
                            env={**os.environ, "CODEX_HOME": str(leg / "home" / ".codex")})
    try:
        reply = R.reap_scratch(roots=[tmp_path])
        proc.wait(timeout=10)
        assert reply["ok"] is True and proc.pid in reply["killed"]
        assert reply["removed"] == [str(leg)] and not leg.exists()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_main_dispatches_reap_scratch(monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "reap-scratch")
    monkeypatch.setattr("sys.argv", ["unimatrix-relay.py"])
    monkeypatch.setattr(R, "reap_scratch", lambda: {"ok": True, "killed": [1], "removed": ["d"]})
    with pytest.raises(SystemExit) as exc:
        R.main()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "killed": [1], "removed": ["d"]}
