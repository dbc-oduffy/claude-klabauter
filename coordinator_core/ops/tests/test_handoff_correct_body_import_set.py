"""
coordinator_core.ops.tests.test_handoff_correct_body_import_set --
``handoff.correct_body`` on a claimed baton loads none of the heavy modules it once pulled in
for one value each.

Purpose: in a fresh interpreter, a body correction applied through
``handoff_correct_body.correct_body_sync`` leaves ``coordinator_core.baton_assemble``,
``coordinator_core.pickup_assemble`` and ``coordinator_core.session.claims`` absent from
``sys.modules``. The same probe pins the ``pickup_assemble`` re-export identities of the
``session.claim_self_held`` leaf, and that a session-id drift between the labelling resolver and
the canonical one still refuses.

Negative-spec:
    - Does NOT cover ``handoff_append_session_ledger``, whose own module level loads
      ``session.claims`` through ``ops.fleet._common``.
    - Does NOT use ``git``: the probe builds a bare ``.git`` directory by hand.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[3]

_ABSENT = (
    "coordinator_core.baton_assemble",
    "coordinator_core.pickup_assemble",
    "coordinator_core.session.claims",
)

_PROBE = """
import json, os, sys
from pathlib import Path

SID = "9220b333-be7f-4707-b5f2-9ebedf3e28ce"
os.environ["COORDINATOR_SESSION_ID"] = SID
repo = Path(sys.argv[1])
(repo / ".git").mkdir(parents=True)
hp = repo / "state" / "handoffs" / "h.md"
hp.parent.mkdir(parents=True)
hp.write_text(
    "---\\ntitle: T\\nstatus: claimed\\nclaimed_at: 2026-07-05T12:00:00Z\\n"
    "authoring_session: " + SID + "\\n---\\n\\n# Body\\n\\nThe count was 29.\\n\\n"
    + "Filler prose so the replaced snippet is a small share of the body. " * 8 + "\\n",
    encoding="utf-8",
)

from coordinator_core.ops import handoff_correct_body as hcb
params = {"handoff_path": str(hp), "old_string": "The count was 29.", "new_string": "The count was 25."}
res = hcb.correct_body_sync(params, repo / ".git")
loaded = [m for m in json.loads(sys.argv[2]) if m in sys.modules]

calls = iter([SID, "other-id"])
hcb.resolve_current_session_id = lambda: next(calls)
params2 = dict(params, old_string="The count was 25.", new_string="The count was 26.")
drift = hcb.correct_body_sync(params2, repo / ".git")

import coordinator_core.pickup_assemble as pa
from coordinator_core.session import claim_self_held as leaf
print("RESULT=" + json.dumps({
    "applied": res.get("applied"),
    "error": res.get("error", ""),
    "loaded": loaded,
    "drift_exit": drift.get("exit_code"),
    "drift_error": drift.get("error", ""),
    "same_claim": pa._claim_already_self_held is leaf._claim_already_self_held,
    "same_scoped": pa._explicitly_scoped_session_id is leaf._explicitly_scoped_session_id,
}))
"""


def _run_probe(tmp_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path / "repo"), json.dumps(_ABSENT)],
        capture_output=True, text=True, timeout=120, cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr[-1200:]
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


def test_correct_body_loads_no_cut_module_and_keeps_its_contracts(tmp_path):
    out = _run_probe(tmp_path)
    assert out["applied"] is True, out
    assert out["loaded"] == [], f"correct_body loaded cut modules: {out['loaded']}"
    assert out["drift_exit"] == 1 and "precedence chain drift" in out["drift_error"], out
    assert out["same_claim"] is True
    assert out["same_scoped"] is True
