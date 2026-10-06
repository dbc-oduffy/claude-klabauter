"""Tests for the test_verdict.record op handler and its CLI door."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import verdict_record_op as op

SIDECAR_REL = ".coordinator-local/subagent-share/s/p.test-runner.aid.md"


def _sidecar(root: Path, text: str = "---\nstatus: open\nagent_type: coordinator:test-runner\n---\n\nbody\n") -> Path:
    p = root / SIDECAR_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _result(**kw) -> dict:
    base = {"status": "pass", "tests_run": 124, "tests_failed": 0, "sidecar_path": SIDECAR_REL}
    base.update(kw)
    return base


def test_handler_records_verdict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(op, "main_worktree_root", lambda r: Path(r))
    sc = _sidecar(tmp_path)
    out = op._record_handler({"result": _result()}, repo_root=tmp_path)
    assert out == {"status": "recorded", "sidecar": SIDECAR_REL, "test_verdict": "pass"}
    text = sc.read_text(encoding="utf-8")
    assert "test_verdict: pass" in text and "run: 124" in text and "failed: 0" in text
    assert "status: open" in text


def test_handler_refusal_propagates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(op, "main_worktree_root", lambda r: Path(r))
    sc = _sidecar(tmp_path)
    before = sc.read_bytes()
    with pytest.raises(op.TestVerdictRefused):
        op._record_handler({"result": _result(tests_run=0)}, repo_root=tmp_path)
    assert sc.read_bytes() == before


def test_cli_contradiction_refused(tmp_path: Path, capsys) -> None:
    import json

    sc = _sidecar(tmp_path)
    before = sc.read_bytes()
    code = op.main(["record", "--repo-root", str(tmp_path), "--result-json", json.dumps(_result(tests_failed=3))])
    assert code == 1
    assert "contradicts" in capsys.readouterr().err
    assert sc.read_bytes() == before


def test_cli_end_to_end_zero_spawns(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    spawns: list = []
    real = subprocess.Popen.__init__

    def rec(self, *a, **kw):
        spawns.append(a)
        return real(self, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", rec)
    sc = _sidecar(tmp_path)
    rpath = tmp_path / "result.json"
    rpath.write_text(json.dumps(_result()), encoding="utf-8")
    code = op.main(["record", "--repo-root", str(tmp_path), "--result-json", str(rpath)])
    assert code == 0
    assert capsys.readouterr().out.strip() == SIDECAR_REL
    assert "test_verdict: pass" in sc.read_text(encoding="utf-8")
    assert spawns == []


def test_cli_exposes_no_verdict_flag() -> None:
    with pytest.raises(SystemExit):
        op.main(["record", "--result-json", "{}", "--verdict", "pass"])
