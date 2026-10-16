"""CLI prints every research part and refuses --fire on a split emit; a one-part reply still fires."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.workflow_fire.fire as fire_module
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


def _argv(tmp_path: Path, monkeypatch, *flags: str) -> list[str]:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    queue_dir = repo_root / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    (queue_dir / "row0.yaml").write_text(yaml.safe_dump({"id": "row0", "title": "Row 0"}), encoding="utf-8")
    (repo_root / "state" / "queue-grind").mkdir(parents=True)
    monkeypatch.chdir(repo_root)
    return [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(repo_root / "state" / "queue-grind" / "out.workflow.mjs"),
        *flags,
    ]


def _split_reply(monkeypatch, tmp_path: Path) -> list[str]:
    real = op_module._dispatch_emit

    def _two_parts(*args, **kwargs):
        result = real(*args, **kwargs)
        names = ["web", "notebooklm"]
        result["parts"] = [
            {"pipeline": name, "path": str(tmp_path / f"p-{name}.workflow.mjs"), "sha256": None,
             "receipt": None, "part": f"{i}/2"}
            for i, name in enumerate(names, 1)
        ]
        return result

    monkeypatch.setattr(cli_module, "_dispatch_emit", _two_parts)
    return [str(tmp_path / f"p-{n}.workflow.mjs") for n in ("web", "notebooklm")]


@pytest.fixture
def no_fire(monkeypatch):
    calls: list = []
    monkeypatch.setattr(fire_module, "fire_workflow", lambda *a, **k: calls.append(a) or {"fired": True})
    return calls


def test_split_research_reply_prints_every_part_and_the_fire_order(tmp_path, monkeypatch, capsys, no_fire):
    argv = _argv(tmp_path, monkeypatch)
    paths = _split_reply(monkeypatch, tmp_path)
    assert cli_module.main(argv) == cli_module.EXIT_OK
    err = capsys.readouterr().err
    assert all(p in err for p in paths)
    assert "split into 2 parts" in err
    assert "does not stop the next part" in err and "next_action.params.halted_segments" in err
    assert no_fire == []


def test_fire_on_a_split_research_reply_is_refused_and_fires_nothing(tmp_path, monkeypatch, capsys, no_fire):
    argv = _argv(tmp_path, monkeypatch, "--fire")
    _split_reply(monkeypatch, tmp_path)
    assert cli_module.main(argv) == cli_module.EXIT_DATA_ERROR
    assert "none fired" in capsys.readouterr().err
    assert no_fire == []


def test_one_part_reply_still_fires(tmp_path, monkeypatch, capsys, no_fire):
    argv = _argv(tmp_path, monkeypatch, "--fire")
    assert cli_module.main(argv) == cli_module.EXIT_OK
    assert len(no_fire) == 1
    assert "split into" not in capsys.readouterr().err
