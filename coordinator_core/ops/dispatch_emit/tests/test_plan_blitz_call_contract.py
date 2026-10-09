"""Every ask route that embeds a planBlitz branch emits a call carrying plan-blitz's required keys.

plan-blitz refuses every baton without provisionSidecarCli, so a call missing a key halts every
M+ run at "no ready plan" rather than at emit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import PurePath

import pytest

from coordinator_core.ops.dispatch_emit import cli, plan_blitz_args
from coordinator_core.ops.dispatch_emit.tests.test_op_sizing_emit import REL, _mjs, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

# Real git repo built in _arrange; needs a real process.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REQUIRED = (
    "provisionSidecarCli",
    "spineCheckCli",
    "armingCheckCli",
    "pluginAgentsAvailable",
    "gateReportPath",
)

_ROUTES = {
    "ask": lambda repo: ["--ask", "add a thing", "--repo-root", str(repo)],
    "sizing": lambda repo: ["--sizing", REL, "--repo-root", str(repo)],
    "ask-sizing": lambda repo: ["--ask", "--sizing", REL, "--repo-root", str(repo)],
}


# The sizing routes mint a baton at emit, which reads the git common dir; the raw ask mints in-run.
_SPAWNS = (pytest.mark.cadence, pytest.mark.spawns_process)
_ROUTE_PARAMS = [
    pytest.param("ask", id="ask"),
    pytest.param("sizing", id="sizing", marks=_SPAWNS),
    pytest.param("ask-sizing", id="ask-sizing", marks=_SPAWNS),
]


def _arrange(repo, route):
    if route == "ask":
        return
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, "M")


@pytest.fixture
def resolvable(monkeypatch):
    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: "/x/provision-sidecar")
    monkeypatch.setattr(plan_blitz_args, "_default_spine_check_cli", lambda *a: "/x/plan-spine-check")
    monkeypatch.setattr(plan_blitz_args, "_default_arming_check_cli", lambda *a: "/x/can-report-red")


def _blitz_call(capsys) -> str:
    out = capsys.readouterr().out
    reply = json.loads(out[out.index("{") : out.rindex("}") + 1])
    text = open(reply["path"], encoding="utf-8").read()
    return text.split("await planBlitz(", 1)[1].split("\n", 1)[0]


@pytest.mark.parametrize("route", _ROUTE_PARAMS)
def test_planblitz_call_carries_every_required_key(repo, capsys, resolvable, route):  # noqa: F811
    _arrange(repo, route)
    assert cli.main(_ROUTES[route](repo)) == 0
    call = _blitz_call(capsys)
    for key in _REQUIRED:
        assert key in call, f"{route}: planBlitz call lacks {key}"
    # Bound after the spread from the runtime sizing, which a raw ask mints in-run.
    assert call.rindex("gateReportPath: REPO_ROOT + '/' + _sizingRel") > call.index("...{")


@pytest.mark.parametrize("route", _ROUTE_PARAMS)
def test_unresolvable_sidecar_refuses_at_emit_on_every_route(repo, capsys, monkeypatch, route):  # noqa: F811
    monkeypatch.setattr(plan_blitz_args, "_default_sidecar_cli", lambda *a, **k: None)
    _arrange(repo, route)
    assert cli.main(_ROUTES[route](repo)) != 0
    assert "provisionSidecarCli" in capsys.readouterr().err
    assert [p for p in _mjs(repo) if PurePath(p).suffix == ".mjs"] == []
