"""Live ``app_session.validate`` against a stdlib http.server fixture and a real browser.

Skips with ``probe_browser``'s structured reason when the browser is absent.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops import _app_session_browser as browser
from coordinator_core.ops import _app_session_validate_contract as contract
from coordinator_core.ops import app_session

pytestmark = [pytest.mark.slow, pytest.mark.spawns_process, pytest.mark.real_home]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_KEY = "web"
_INDEX = (
    "<!doctype html><html><head><meta name='viewport' content='width=device-width'>"
    "<title>fixture</title></head><body><h1>fixture</h1></body></html>"
)
_ABOUT = "<!doctype html><html><body><p>about</p></body></html>"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _q(s: str) -> str:
    return '"' + s.replace("\\", "/") + '"'


def test_validate_sweeps_fixture_routes_and_tears_down(tmp_path, monkeypatch):
    availability = browser.probe_browser()
    if not availability.ok:
        pytest.skip(f"{availability.reason} (hint: {availability.install_hint})")

    repo = tmp_path / "consuming-repo"
    site = repo / "site"
    site.mkdir(parents=True)
    common = repo / ".git"
    common.mkdir()
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda root: common)
    monkeypatch.setenv("PYTHONPATH", str(_REPO_ROOT))
    (site / "index.html").write_text(_INDEX, encoding="utf-8")
    (site / "about.html").write_text(_ABOUT, encoding="utf-8")
    (repo / "server.py").write_text(
        "import functools, http.server, sys\n"
        "handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory='site')\n"
        "http.server.test(HandlerClass=handler, port=int(sys.argv[1]), bind='127.0.0.1')\n",
        encoding="utf-8",
    )
    (repo / "build.py").write_text("print('built')\n", encoding="utf-8")
    port = _free_port()
    (repo / "coordinator.local.md").write_text(
        "---\n"
        "app_session:\n"
        f"  {_KEY}:\n"
        f"    port: {port}\n"
        f"    build: {_q(sys.executable)} build.py\n"
        f"    start: {_q(sys.executable)} server.py {port}\n"
        "    ready_timeout_s: 30\n"
        "    build_timeout_s: 30\n"
        "    routes:\n"
        "      index: /index.html\n"
        "      about: /about.html\n"
        "---\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"

    started = app_session._validate({"key": _KEY, "repo_root": str(repo), "out_dir": str(run_dir)})
    assert started["ok"] is True and started["started"] is True, started

    deadline = time.monotonic() + float(started["wait_budget_s"])
    result = None
    while time.monotonic() < deadline:
        result = contract.read_result(run_dir)
        if result is not None:
            break
        time.sleep(0.25)
    assert result is not None, (run_dir / "runner.log").read_text(encoding="utf-8", errors="replace")
    assert result["ok"] is True, result
    assert result["defect_count"] == 0, result

    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert sorted({r["width"] for r in report["results"]}) == [390, 1440]
    assert len(report["results"]) == 4
    shots = sorted(p.name for p in (run_dir / "screenshots").iterdir())
    assert shots == ["about-1440.png", "about-390.png", "index-1440.png", "index-390.png"]
    for p in (run_dir / "screenshots").iterdir():
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    assert not _port_open(port)
    assert app_session._read_handle(str(repo), f"{_KEY}@validate") is None
