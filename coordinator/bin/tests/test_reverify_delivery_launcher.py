"""test_reverify_delivery_launcher.py — the `reverify-delivery` bin trampoline.

Covers: `record --help` lists the flags the dispatch skill cites; `record`
through the launcher writes the same delivery-verdict record as the engine's
`-m` invocation; the .cmd/.ps1 twins target the .py entrypoint.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN = Path(__file__).resolve().parent.parent
_REPO = _BIN.parent.parent
_LAUNCHER = _BIN / "reverify-delivery.py"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW
    )


def test_record_help_lists_flags(tmp_path):
    proc = _run([sys.executable, str(_LAUNCHER), "record", "--help"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "--run-record" in proc.stdout
    assert "--result-json" in proc.stdout


def _record(prefix: list[str], repo: Path) -> dict:
    result = {
        "reverify_delivery": {"plan_id": "p1", "head_sha": "abc123"},
        "verdict": "PASS",
        "claims_unbacked": [],
    }
    repo.mkdir(parents=True)
    proc = _run(
        prefix + ["record", "--run-record", "state/runs/r1.md", "--result-json", json.dumps(result),
                  "--repo-root", str(repo)],
        repo,
    )
    assert proc.returncode == 0, proc.stderr
    rel = proc.stdout.strip()
    assert (repo / rel).is_file()
    text = (repo / rel).read_text(encoding="utf-8")
    return {"rel": rel, "body": re.sub(r"recorded_at: .*\n", "", text)}


def test_record_matches_module_invocation(tmp_path):
    via_launcher = _record([sys.executable, str(_LAUNCHER)], tmp_path / "a")
    via_module = _record([sys.executable, "-m", "coordinator_core.ops.dispatch_emit.reverify_delivery"], tmp_path / "b")
    assert via_launcher["body"] == via_module["body"]
    assert Path(via_launcher["rel"]).parent == Path(via_module["rel"]).parent


def test_windows_twins_target_py_entrypoint():
    for ext in (".cmd", ".ps1"):
        text = (_BIN / f"reverify-delivery{ext}").read_text(encoding="utf-8")
        assert "reverify-delivery.py" in text
