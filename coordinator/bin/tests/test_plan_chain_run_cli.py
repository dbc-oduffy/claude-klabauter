"""`plan-chain-run` CLI: manifest loading, printed digest, exit codes; `plan_chain.run` is stubbed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("plan_chain_run_cli", _BIN / "plan-chain-run.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

from coordinator_core.ops import plan_chain  # noqa: E402
from coordinator_core.ops.plan_chain.contract import ChainManifest  # noqa: E402


def _manifest(tmp_path: Path) -> ChainManifest:
    return ChainManifest(
        sizing_object="state/sizings/s1.yaml",
        baton="state/handoffs/b1.md",
        deliverable_id="D1",
        interaction_mode="pm",
        repo_root=str(tmp_path),
        trail_dir="trail",
        wave_args={"batons": []},
        script_source="plan-blitz.mjs",
    )


@pytest.fixture
def manifest_file(tmp_path):
    m = _manifest(tmp_path)
    p = tmp_path / "chain-1-1.json"
    p.write_text(m.to_json(), encoding="utf-8")
    return p, m


def _stub(monkeypatch, tmp_path, halted_at=None):
    seen = []

    def fake_run(manifest):
        seen.append(manifest)
        trail = tmp_path / "trail"
        trail.mkdir(exist_ok=True)
        (trail / "chain-abc.final-digest.json").write_text("{}", encoding="utf-8")
        return {"chain": {"halted_at": halted_at}}

    monkeypatch.setattr(plan_chain, "run", fake_run, raising=False)
    return seen


def test_manifest_builds_expected_manifest(monkeypatch, tmp_path, manifest_file, capsys):
    path, expected = manifest_file
    seen = _stub(monkeypatch, tmp_path)
    assert cli.main(["--manifest", str(path)]) == 0
    assert seen == [expected]
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("chain-abc.final-digest.json")
    assert json.loads("\n".join(out[1:])) == {"chain": {"halted_at": None}}


def test_halt_exits_3(monkeypatch, tmp_path, manifest_file):
    path, _ = manifest_file
    _stub(monkeypatch, tmp_path, halted_at="execute")
    assert cli.main(["--manifest", str(path)]) == 3


def test_usage_errors_exit_2(tmp_path, capsys):
    assert cli.main([]) == 2
    assert cli.main(["--manifest", str(tmp_path / "missing.json")]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cli.main(["--manifest", str(bad)]) == 2
    capsys.readouterr()
