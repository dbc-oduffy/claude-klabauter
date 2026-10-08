"""Golden baseline for `emit-wave-fire --from-sizing` on an accepted pm M sizing: stdout, script bytes, receipt."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_golden", _BIN / "emit-wave-fire.py")
ewf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ewf)

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "emit_wave_fire_from_sizing_golden"
SIZING_REL = "state/sizings/s1.yaml"
BATON_REL = "state/handoffs/b1.md"
BATON = "---\nhandoff_id: hnd-1\ntitle: Minted baton\nstatus: open\n---\n\n# body\n"
ROOT = "<ROOT>"
SESSION = "golden-session"
EMITTED_AT = "2000-01-01T00:00:00"
ENGINE_REF = {
    "repoRoot": ROOT, "workflowPath": f"{ROOT}/doctrine/coordinator/workflows/plan-blitz.mjs",
    "head": None, "workflowSha": None, "dirty": None, "reason": "golden stub",
}


class _FixedDatetime:
    @staticmethod
    def now():
        from datetime import datetime

        return datetime.fromisoformat(EMITTED_AT)


def _sizing(mode):
    return {
        "schema": "sizing-object", "status": "sized", "premise": {"provenance": "unrecorded"},
        "detents": [], "fork": None, "xl_exit": None, "appetite": "medium",
        "estimate": {"tshirt": "M", "provisional": True}, "route": "plan",
        "interaction_mode": mode, "intent": "do the thing",
        "exit_criterion": {
            "statement": "it works",
            "accepted": {"pm_quote": "ok", "on": "2026-10-01", "mode": mode},
        },
    }


def _norm(text, tmp_path):
    root = str(tmp_path)
    for variant in (json.dumps(root)[1:-1], root.replace("\\", "/"), root):
        text = text.replace(variant, ROOT)
    return text.replace("\\\\", "/").replace("\\", "/")


def _capture(tmp_path, monkeypatch, capsys, mode):
    from coordinator_core.ops.dispatch_emit import emission_receipt

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-install"))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SESSION)
    monkeypatch.setattr(emission_receipt, "datetime", _FixedDatetime)
    monkeypatch.setattr(ewf, "_engine_ref", lambda repo_root, src: dict(ENGINE_REF, repoRoot=str(repo_root),
                                                                       workflowPath=str(src)))
    monkeypatch.setattr(
        ewf, "_bind",
        lambda script_path, args, live=False: "const args = " + json.dumps(args, sort_keys=True) + ";\n",
    )
    monkeypatch.setattr(
        ewf, "_load_mint",
        lambda: lambda sizing_rel, repo_root, **kw: {
            "id": "hnd-1", "path": BATON_REL, "title": "Minted baton", "created": True},
    )
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(_sizing(mode)), encoding="utf-8")
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / BATON_REL).write_text(BATON, encoding="utf-8")
    (tmp_path / "trail").mkdir()
    (tmp_path / ".git").mkdir()
    plugin = tmp_path / "doctrine" / "coordinator"
    (plugin / "workflows").mkdir(parents=True)
    (plugin / "workflows" / "plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    (plugin / "agents").mkdir()
    rc = ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(plugin), "--from-sizing", SIZING_REL, "--live-engine-tree",
        "--provision-sidecar-cli", "golden-sidecar-cli", "--spine-check-cli", "golden-spine-check-cli",
    ])
    assert rc == ewf.EXIT_OK
    out = capsys.readouterr().out
    script_raw = (tmp_path / "trail" / "fire-0-1.mjs").read_bytes()
    receipt = json.loads((tmp_path / "trail" / "fire-0-1.mjs.emitted.json").read_text(encoding="utf-8"))
    assert receipt["sha256"] == hashlib.sha256(script_raw).hexdigest()
    script = _norm(script_raw.decode("utf-8"), tmp_path)
    receipt["sha256"] = hashlib.sha256(script.encode("utf-8")).hexdigest()
    receipt_text = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    return _norm(out, tmp_path), script, receipt_text


def _golden(name):
    return (GOLDEN / name).read_bytes().decode("utf-8")


def test_pm_m_sizing_output_matches_the_golden(tmp_path, monkeypatch, capsys):
    out, script, receipt = _capture(tmp_path, monkeypatch, capsys, "pm")
    assert receipt_has_stubbed_inputs(receipt)
    assert out == _golden("stdout.txt")
    assert script == _golden("fire-0-1.mjs")
    assert receipt == _golden("fire-0-1.mjs.emitted.json")


def receipt_has_stubbed_inputs(receipt_text):
    data = json.loads(receipt_text)
    return data["session_id"] == SESSION and data["emitted_at"] == EMITTED_AT


@pytest.mark.parametrize("mode_swap", [("pm", "ceo")])
def test_ceo_mode_differs_from_pm_in_the_mode_value_only(tmp_path, monkeypatch, capsys, mode_swap):
    out, script, receipt = _capture(tmp_path, monkeypatch, capsys, mode_swap[1])
    assert out == _golden("stdout.txt")
    assert script.replace('"interactionMode": "ceo"', '"interactionMode": "pm"') == _golden("fire-0-1.mjs")
    assert '"interactionMode": "ceo"' in script
    assert json.loads(receipt)["session_id"] == SESSION
