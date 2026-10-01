"""delegate_m_plus: exact emit-wave-fire argv, trail minting, caught refusals, missing engine file."""

from __future__ import annotations

import json
import types

from coordinator_core.ops.dispatch_emit import sizing_m_delegate as smd


def _engine(tmp_path, monkeypatch):
    script = tmp_path / "engine" / "coordinator" / "bin" / "emit-wave-fire.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(smd.engine_root, "coordinator_engine_root", lambda: str(tmp_path / "engine"))
    return script


def test_argv_and_reply(tmp_path, monkeypatch):
    _engine(tmp_path, monkeypatch)
    seen = {}

    def main(argv):
        seen["argv"] = argv
        print(json.dumps({"fires": [{"fire": 1, "scriptPath": "/x/fire-0-1.mjs", "batons": ["b1"]}]}))
        return 0

    monkeypatch.setattr(smd, "_load_emit_wave_fire", lambda p: types.SimpleNamespace(main=main))
    repo, trail = tmp_path / "repo", tmp_path / "trail"
    repo.mkdir()
    res = smd.delegate_m_plus(sizing_rel="state/sizings/a.yaml", repo_root=repo, trail_dir=trail)
    assert seen["argv"] == [
        "--repo-root", str(repo.resolve()), "--trail-dir", str(trail.resolve()),
        "--from-sizing", "state/sizings/a.yaml", "--json",
    ]
    assert res == {"scriptPath": "/x/fire-0-1.mjs", "exit_code": 0, "batons": ["b1"]}


def test_trail_dir_minted_when_absent(tmp_path, monkeypatch):
    _engine(tmp_path, monkeypatch)
    seen = {}

    def main(argv):
        seen["trail"] = argv[argv.index("--trail-dir") + 1]
        print(json.dumps({"fires": []}))
        return 0

    monkeypatch.setattr(smd, "_load_emit_wave_fire", lambda p: types.SimpleNamespace(main=main))
    smd.delegate_m_plus(sizing_rel="s.yaml", repo_root=tmp_path)
    trail = tmp_path.resolve() / "state" / "plan-blitz"
    assert seen["trail"].startswith(str(trail))
    assert (trail / seen["trail"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).is_dir()


def test_nonzero_and_systemexit_are_returned(tmp_path, monkeypatch):
    _engine(tmp_path, monkeypatch)

    def refuse(argv):
        print("emit-wave-fire: REFUSED — nope", file=__import__("sys").stderr)
        return 3

    monkeypatch.setattr(smd, "_load_emit_wave_fire", lambda p: types.SimpleNamespace(main=refuse))
    res = smd.delegate_m_plus(sizing_rel="s.yaml", repo_root=tmp_path, trail_dir=tmp_path / "t")
    assert res["exit_code"] == 3 and "REFUSED" in res["refusal"] and res["scriptPath"] is None

    def argparse_exit(argv):
        raise SystemExit(2)

    monkeypatch.setattr(smd, "_load_emit_wave_fire", lambda p: types.SimpleNamespace(main=argparse_exit))
    res = smd.delegate_m_plus(sizing_rel="s.yaml", repo_root=tmp_path, trail_dir=tmp_path / "t")
    assert res["exit_code"] == 2


def test_missing_engine_file_names_resolved_path(tmp_path, monkeypatch):
    monkeypatch.setattr(smd.engine_root, "coordinator_engine_root", lambda: str(tmp_path / "none"))
    res = smd.delegate_m_plus(sizing_rel="s.yaml", repo_root=tmp_path, trail_dir=tmp_path / "t")
    assert res["exit_code"] != 0
    assert str(tmp_path / "none" / "coordinator" / "bin" / "emit-wave-fire.py") in res["refusal"]
