"""unimatrix-roster-timing.py reports per-phase cost of roster() without writing the sessions dir."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]
SCRIPT = BIN / "unimatrix-roster-timing.py"


def _timing():
    spec = importlib.util.spec_from_file_location("unimatrix_roster_timing_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snapshot(d):
    return {p.name: p.read_bytes() for p in d.iterdir()}


def _seed(sess, monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    live = os.getpid()
    (sess / f"{live}.json").write_text(json.dumps({"pid": live, "name": "live", "kind": "interactive"}))
    (sess / f"{live}.k.key").write_text("secret")
    dead = 2 ** 22 + 12345
    (sess / f"{dead}.json").write_text(json.dumps({"pid": dead, "name": "dead"}))
    (sess / f"{dead}.k.key").write_text("secret")
    (sess / "junk.json").write_text("{}")
    (sess / "99999.json").write_text("not json")


def test_measure_reports_phases_rows_and_skips_read_only(tmp_path, monkeypatch):
    sess = tmp_path / "sessions"
    sess.mkdir()
    _seed(sess, monkeypatch, tmp_path)
    before = _snapshot(sess)
    report = _timing().measure(sessions_dir=sess)
    assert _snapshot(sess) == before
    assert report["entries_candidates"] == 3
    assert report["entries_yielded"] == 1
    assert report["entries_skipped"] == 2
    assert report["rows"] == 1
    assert report["spawns"] == 0
    assert report["process_ms"] > 0 or report["phase_calls"]["listdir"] >= 1
    for phase in ("listdir", "json_load", "entry_live", "print", "other"):
        assert phase in report["phase_ms"], phase
    assert report["phase_calls"]["entry_live"] >= 2


def test_measure_restores_patched_globals(tmp_path, monkeypatch):
    sess = tmp_path / "sessions"
    sess.mkdir()
    _seed(sess, monkeypatch, tmp_path)
    listdir, load, popen = os.listdir, json.load, subprocess.Popen
    _timing().measure(sessions_dir=sess)
    assert (os.listdir, json.load, subprocess.Popen) == (listdir, load, popen)


def test_cli_prints_json_for_an_empty_sessions_dir(tmp_path):
    sess = tmp_path / "sessions"
    sess.mkdir()
    env = {**os.environ, "COORDINATOR_SETTINGS_HOME": str(tmp_path / "home")}
    proc = subprocess.run([sys.executable, str(SCRIPT), "--sessions-dir", str(sess)],
                          capture_output=True, text=True, env=env, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["rows"] == 0 and out["entries_candidates"] == 0
    assert {"process_ms", "spawns", "phase_ms"} <= set(out)
    for phase in ("listdir", "json_load", "entry_live", "codex_checks", "print"):
        assert phase in out["phase_ms"], phase
