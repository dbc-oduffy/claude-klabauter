"""The cross-box bridge send, run in a fresh interpreter with COORDINATOR_SETTINGS_HOME removed, resolves the default
settings home under HOME and records a far-write failure as a failed send (row ok=false, sender notified)."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

# -S: a site/usercustomize shim on a dev box may inject the variable; the child must start without it.
DRIVER = textwrap.dedent(
    """
    import importlib.util, json, os, subprocess, sys, types
    from pathlib import Path
    assert "COORDINATOR_SETTINGS_HOME" not in os.environ
    spec = importlib.util.spec_from_file_location("br", sys.argv[1])
    br = importlib.util.module_from_spec(spec); spec.loader.exec_module(br)
    home = Path(sys.argv[2])
    sessions = home / "sessions"; sessions.mkdir()
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "messagingSocketPath": "/tmp/cc/m.sock", "macSocketPath": "/far/4242.sock",
         "name": "far", "unimatrixFarName": "far", "sessionId": "5a1f0c2e-1111-4222-8333-444455556666"}))
    br.SESSIONS = str(sessions)
    br._box_mux().IS_WIN = False
    br.cfg = types.SimpleNamespace(endpoint="/tmp/cc/m.sock", key="k", remote="farbox", port=None)
    br.log = open(home / "bridge.log", "a")
    notices = []
    client = []
    br.write_local = lambda ep, data: notices.append(data)
    mode = sys.argv[3]
    if mode == "far-fail":
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"far write refused")
    else:
        def boom(*a, **k):
            raise RuntimeError("channel construction failed")
        br._box_mux().channel = boom
    class Conn:
        chunks = [json.dumps({"from": "uds:/tmp/cc/s.sock", "message": "hi"}).encode() + b"\\n", b""]
        def read(self): return self.chunks.pop(0)
        def write(self, data): client.append(json.loads(data))
        def close(self): pass
    br.relay_send(Conn())
    print(json.dumps({"home": str(br.settings_home()), "notices": len(notices), "client": client}))
    """
)


def run_driver(tmp_path, mode):
    env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_SETTINGS_HOME"}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    env.pop("UNIMATRIX_TEST_HOME", None)
    done = subprocess.run([sys.executable, "-S", "-c", DRIVER, str(BIN / "unimatrix-bridge.py"), str(tmp_path), mode],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    rows = [json.loads(l) for l in (tmp_path / ".coordinator-claude-settings" / "unimatrix" / "deliveries.jsonl")
            .read_text().splitlines()]
    return out, rows


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_far_write_failure_is_a_failed_send_with_the_variable_unset(tmp_path):
    out, rows = run_driver(tmp_path, "far-fail")
    assert out["home"] == str(tmp_path / ".coordinator-claude-settings")
    assert rows and all(r["ok"] is False for r in rows)
    assert "MissingSettingsError" not in json.dumps(rows)
    assert out["notices"] >= 1
    # Custody was taken before the far leg ran: the client gets no refusal line, the failure is the row + notice.
    assert out["client"] == []
    assert any("far write refused" in (r.get("error") or "") for r in rows)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_local_fault_reads_local_never_ssh_255_with_the_variable_unset(tmp_path):
    out, rows = run_driver(tmp_path, "local-fault")
    assert out["client"] == []  # the channel fault is after custody: recorded as a failed row, never answered
    assert rows and all(r["ok"] is False for r in rows)
    text = json.dumps(rows)
    assert "local send-leg fault" in text and "255" not in text
