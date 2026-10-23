"""scout_assemble_flags_js: S-evidence flags flow from the scout return into route()."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SCOUT_SCHEMA, scout_assemble_flags_js
from coordinator_core.sizing_assemble import route

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BASE = {
    "tshirt": "S",
    "intent": "i",
    "name": "n",
    "premise_provenance": "read",
    "premise_evidence": "e",
    "exit_criterion": "c",
}


def _flags(scout: dict) -> list[str]:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    expr = "''" + scout_assemble_flags_js("_asmFields", "q")
    prog = (
        "const q = (v) => JSON.stringify(String(v));\n"
        f"const _asmFields = {json.dumps({**_BASE, **scout})};\n"
        f"console.log(JSON.stringify({expr}));\n"
    )
    out = subprocess.run(
        ["node", "-e", prog], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return shlex.split(json.loads(out.strip()))


def _route_tshirt(argv: list[str]) -> str:
    kw: dict = {"scout_evidence": []}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--scout-evidence":
            kw["scout_evidence"].append(argv[i + 1])
        elif tok == "--precedent":
            kw["precedent"] = argv[i + 1]
        elif tok == "--boundary-in-notch":
            kw["boundary_in_notch"] = argv[i + 1]
        elif tok == "--probe-signal":
            kw["probe_signal"] = argv[i + 1]
        i += 1
    est = {"tshirt": "S", "provisional": True}
    return route(estimate=est, **kw)["resolved_estimate"]["tshirt"]


def test_evidence_free_s_still_demotes_and_emits_no_evidence_flags():
    argv = _flags({})
    for flag in ("--precedent", "--boundary-in-notch", "--probe-signal", "--scout-evidence"):
        assert flag not in argv
    assert _route_tshirt(argv) == "XS"


@pytest.mark.parametrize(
    "scout",
    [
        {"scout_evidence": ["3 call sites read"]},
        {"boundary_in_notch": "yes"},
        {"precedent": "shipped-before"},
    ],
)
def test_scout_supported_s_stays_s(scout):
    argv = _flags(scout)
    assert _route_tshirt(argv) == "S"


def test_all_four_flags_emitted_and_empty_or_invalid_values_skipped():
    argv = _flags(
        {"scout_evidence": ["a b", "c"], "boundary_in_notch": "no", "precedent": "novel", "probe_signal": "collapse"}
    )
    assert argv.count("--scout-evidence") == 2 and "a b" in argv
    assert argv[argv.index("--precedent") + 1] == "novel"
    assert argv[argv.index("--boundary-in-notch") + 1] == "no"
    assert argv[argv.index("--probe-signal") + 1] == "collapse"
    bad = _flags({"scout_evidence": ["", "  "], "precedent": "bogus", "boundary_in_notch": 5})
    assert not {"--scout-evidence", "--precedent", "--boundary-in-notch"} & set(bad)


def test_other_scouted_variable_gets_no_evidence_flags():
    assert "--precedent" not in scout_assemble_flags_js("scouted", "_shq")


def test_ask_scout_schema_admits_fields_as_optional():
    props = ASK_SCOUT_SCHEMA["properties"]
    for f in ("scout_evidence", "boundary_in_notch", "precedent", "probe_signal"):
        assert f in props and f not in ASK_SCOUT_SCHEMA["required"]
