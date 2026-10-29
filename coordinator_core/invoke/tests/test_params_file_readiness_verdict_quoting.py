"""plan.readiness_verdict params carrying quotes and arrows survive `--params-file`.

The params travel as a file path in an argv list (no shell), so single quotes,
double quotes and '->' in premise rows reach the op byte-for-byte. The reply's
verdict must equal the verdict the handler returns in-process for the same params.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.plan_readiness_verdict import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[3]
_PLAN = "docs/plans/2026-10-29-plan-blitz-readiness-params-file-36b69c.md"

_PARAMS = {
    "plan": _PLAN,
    "premise": {
        "rows": [
            {
                "verdict": "RESOLVES",
                "citation": "main() 'step 1' -> \"--params-file\" -> op; it's quoted",
            },
            {
                "verdict": "UNCHECKABLE",
                "citation": 'say "hi" -> don\'t break -> done',
            },
        ]
    },
}


def test_quoted_arrow_params_through_params_file_match_in_process(tmp_path):
    params_file = tmp_path / "params.json"
    params_file.write_text(json.dumps(_PARAMS), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable, "-m", "coordinator_core.invoke",
            "plan.readiness_verdict", "--params-file", str(params_file),
        ],
        cwd=str(_REPO), capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    reply = json.loads(proc.stdout)
    reply = reply.get("result", reply)

    expected = _handler(_PARAMS, repo_root=_REPO)
    assert isinstance(expected["verdict"], str) and expected["verdict"]
    assert reply["verdict"] == expected["verdict"]
    assert reply.get("blocking") == expected.get("blocking")
