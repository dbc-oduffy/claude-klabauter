from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def run(host, stdin, tmp_path):
    env = {**__import__("os").environ, "COORDINATOR_SETTINGS_HOME": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "coordinator_core.tui_status", host], input=stdin, text=True,
                          encoding="utf-8", capture_output=True, cwd=ROOT, env=env, timeout=30)


def test_grok_stdin_prints_name_context_and_spend(tmp_path):
    peers = tmp_path / "unimatrix" / "peers.json"
    peers.parent.mkdir()
    peers.write_text(json.dumps({"peers": [{"vendor": "grok", "target": "s1", "name": "grok-one"}]}), encoding="utf-8")
    payload = {"session_id": "s1", "context_window": {"remaining_percentage": 60},
               "auto_compact_threshold_percent": 80, "cost": {"total_cost_usd": 2}}
    done = run("grok", json.dumps(payload), tmp_path)
    assert done.returncode == 0
    assert done.stdout.strip() == "grok-one · ▰▰▱▱▱ 40% ctx · ~$2.00"


def test_agy_stdin_prints_unknown_context(tmp_path):
    done = run("agy", json.dumps({"session_id": "x", "remaining_percentage": 70, "cost": 1}), tmp_path)
    assert done.returncode == 0
    assert done.stdout.strip() == "unknown · unknown · ~$1.00"


def test_bad_json_or_unknown_host_exits_zero_with_unknown(tmp_path):
    for host, stdin in (("grok", "{not json"), ("agy", ""), ("nope", "{}"), ("grok", "[1]")):
        done = run(host, stdin, tmp_path)
        assert done.returncode == 0 and done.stdout.strip() == "unknown · unknown · unknown"
