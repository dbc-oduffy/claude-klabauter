"""A row whose return never arrived is named `<id> returned no result` in the composed delivery notes."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_OPEN = "  _deliveryNotes = (await Promise.all(_manifest.rows"
_CLOSE = ".filter((x) => x).join(' | ');"


def _site(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )
    start = script.index(_OPEN)
    return script[start : script.index(_CLOSE, start) + len(_CLOSE)]


def _notes(site: str, rows: dict) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    manifest = {"rows": [{"id": k, "agent_type": "coordinator:executor"} for k in rows]}
    prog = (
        "(async () => {\n"
        f"let _deliveryNotes = ''; const _manifest = {json.dumps(manifest)};\n"
        f"const _rows = {{}}; for (const [k, v] of Object.entries({json.dumps(rows)})) _rows[k] = Promise.resolve(v);\n"
        + site
        + "\nconsole.log(JSON.stringify(_deliveryNotes));\n})();"
    )
    proc = subprocess.run(
        [node, "-e", prog], capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.splitlines()[-1])


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_a_row_with_no_return_is_named_never_gate_blocker_none(monkeypatch: pytest.MonkeyPatch, missing) -> None:
    out = _notes(_site(monkeypatch), {"C1": missing, "C2": "DONE: C2 landed"})
    assert "C1 returned no result" in out
    assert "gate-blocker: none" not in out
    assert "C2: DONE: C2 landed" in out


def test_a_row_that_returned_keeps_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _notes(_site(monkeypatch), {"C1": "DONE: ok"})
    assert out == "C1: DONE: ok"
