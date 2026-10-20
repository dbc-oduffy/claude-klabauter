"""CLI maps --per-group/--group to op params and prints one run line per group in priority order."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import research_groups as rg


def _repo(tmp_path: Path, monkeypatch) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo_root)
    return repo_root


def _entry(priority: int, ref: str, tmp_path: Path) -> dict:
    return {
        "group": ref.lower(), "ref": ref, "priority": priority, "run_id": f"run-{ref.lower()}",
        "path": str(tmp_path / f"run-{ref.lower()}.workflow.mjs"), "sha256": "0" * 64,
        "receipt": None, "fanout": {}, "next_action": {"op": "research.close"},
    }


def _stub_op(monkeypatch, tmp_path: Path, seen: list) -> None:
    def _fake(params, **_kwargs):
        seen.append(dict(params))
        entries = [_entry(i, ref, tmp_path) for i, ref in enumerate(("A", "B", "C"), 1)]
        return {"ok": True, "path": entries[0]["path"], "sha256": entries[0]["sha256"],
                "per_group": True, rg.GROUPS_REPLY_KEY: entries}

    monkeypatch.setattr(cli_module, "_dispatch_emit", _fake)


def test_per_group_maps_params_and_prints_run_lines_in_priority_order(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    seen: list = []
    _stub_op(monkeypatch, tmp_path, seen)
    assert cli_module.main(["--from-sizing", "sizing.yaml", "--per-group"]) == cli_module.EXIT_OK
    assert seen[0][rg.PER_GROUP_PARAM] is True and rg.GROUPS_PARAM not in seen[0]
    out, err = capsys.readouterr()
    lines = [ln for ln in err.splitlines() if "run_id=" in ln]
    assert [ln.split("run_id=")[1].split()[0] for ln in lines] == ["run-a", "run-b", "run-c"]
    assert "each group is its own run" in err and "priority order" in err
    assert json.loads(out)[rg.GROUPS_REPLY_KEY][0]["ref"] == "A"


def test_group_implies_per_group_and_repeats(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    seen: list = []
    _stub_op(monkeypatch, tmp_path, seen)
    assert cli_module.main(["--from-sizing", "s.yaml", "--group", "B", "--group", "C"]) == cli_module.EXIT_OK
    assert seen[0][rg.PER_GROUP_PARAM] is True and seen[0][rg.GROUPS_PARAM] == ["B", "C"]


def test_stdout_is_one_json_document(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    _stub_op(monkeypatch, tmp_path, [])
    cli_module.main(["--from-sizing", "s.yaml", "--per-group"])
    json.loads(capsys.readouterr().out)


def test_per_group_without_from_sizing_is_refused(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    seen: list = []
    _stub_op(monkeypatch, tmp_path, seen)
    assert cli_module.main(["--per-group"]) == cli_module.EXIT_USAGE
    assert "needs --from-sizing" in capsys.readouterr().err
    assert cli_module.main(["--group", "A"]) == cli_module.EXIT_USAGE
    assert seen == []


def test_per_group_with_out_is_refused(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    seen: list = []
    _stub_op(monkeypatch, tmp_path, seen)
    argv = ["--from-sizing", "s.yaml", "--per-group", "--out", "x.workflow.mjs"]
    assert cli_module.main(argv) == cli_module.EXIT_USAGE
    assert "exclusive of --out" in capsys.readouterr().err
    assert seen == []


def test_without_flag_no_per_group_params(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    seen: list = []
    _stub_op(monkeypatch, tmp_path, seen)
    cli_module.main(["--from-sizing", "s.yaml"])
    assert rg.PER_GROUP_PARAM not in seen[0]
    assert "each group is its own run" not in capsys.readouterr().err
