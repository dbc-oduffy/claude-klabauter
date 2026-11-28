"""check-project-type CLI: single-root verdicts, --apply, --json and the --fleet registry walk."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.complete

_BIN = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cli(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(_BIN))
    spec = importlib.util.spec_from_file_location("_check_project_type_cli", _BIN / "check-project-type.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(base: Path, name: str, *, uproject: bool = False, local_md: str | None = None) -> Path:
    root = base / name
    root.mkdir()
    if uproject:
        (root / "Game").mkdir()
        (root / "Game" / "Game.uproject").write_text("{}", encoding="utf-8")
    if local_md is not None:
        (root / "coordinator.local.md").write_text(local_md, encoding="utf-8")
    return root


def _four(base: Path) -> dict[str, Path]:
    return {
        "ok": _repo(base, "ok", local_md="---\nproject_type: general\n---\n"),
        "missing": _repo(base, "missing"),
        "seedable": _repo(base, "seedable", uproject=True),
        "conflict": _repo(base, "conflict", uproject=True, local_md="---\nproject_type: meta\n---\n"),
    }


@pytest.mark.parametrize(("name", "code"), [("ok", 0), ("missing", 1), ("seedable", 1), ("conflict", 1)])
def test_default_invocation_reports_each_verdict(
    cli: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, code: int
) -> None:
    root = _four(tmp_path)[name]
    assert cli.main([str(root)]) == code
    assert capsys.readouterr().out.startswith(f"{name}  ")


def test_conflict_line_carries_the_dr_1210_hint(
    cli: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main([str(_four(tmp_path)["conflict"])])
    assert "map to {general, game-dev, web-dev, data-science}" in capsys.readouterr().out


def test_apply_seeds_seedable_and_the_next_default_run_is_ok(
    cli: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _four(tmp_path)["seedable"]
    assert cli.main([str(root), "--apply"]) == 0
    assert "applied, now ok" in capsys.readouterr().out
    assert cli.main([str(root)]) == 0
    assert capsys.readouterr().out.startswith("ok  ")


def test_apply_on_conflict_exits_one_and_keeps_the_file(cli: ModuleType, tmp_path: Path) -> None:
    root = _four(tmp_path)["conflict"]
    before = (root / "coordinator.local.md").read_bytes()
    assert cli.main([str(root), "--apply"]) == 1
    assert (root / "coordinator.local.md").read_bytes() == before


def test_json_single_root_shape(cli: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main([str(_four(tmp_path)["seedable"]), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "seedable"
    assert data["markers"] == ["Game/Game.uproject"]
    assert data["repair"]["writes"] == {"project_type": "game-dev", "project_subtypes": ["unreal"]}
    assert (data["applied"], data["after"]) == (False, None)


def test_usage_errors_exit_two(cli: ModuleType, tmp_path: Path) -> None:
    assert cli.main([]) == 2
    assert cli.main([str(tmp_path / "absent")]) == 2
    assert cli.main([str(tmp_path), "--fleet"]) == 2
    with pytest.raises(SystemExit) as exc:
        cli.main([str(tmp_path), "--bogus"])
    assert exc.value.code == 2


def _registry(home: Path, roots: dict[str, Path]) -> None:
    reg = home / "machine-local"
    reg.mkdir(parents=True)
    lines = [f'"repos.{slug}" = "{root.as_posix()}"' for slug, root in roots.items()]
    (reg / "registry.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_fleet_reports_a_verdict_per_registry_root(
    cli: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = _four(tmp_path) | {"gone": tmp_path / "gone"}
    _registry(tmp_path / "home", roots)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    assert cli.main(["--fleet", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert [r["slug"] for r in data["repos"]] == sorted(roots)
    assert {r["slug"]: r["verdict"] for r in data["repos"]} == {
        "ok": "ok", "missing": "missing", "seedable": "seedable", "conflict": "conflict", "gone": "norepo",
    }
    assert data["counts"] == {"ok": 1, "missing": 1, "seedable": 1, "conflict": 1, "norepo": 1, "repaired": 0}


def test_fleet_apply_repairs_per_repo_and_skips_conflict_and_norepo(
    cli: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = _four(tmp_path) | {"gone": tmp_path / "gone"}
    _registry(tmp_path / "home", roots)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    conflict_before = (roots["conflict"] / "coordinator.local.md").read_bytes()
    assert cli.main(["--fleet", "--apply", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["repaired"] == 2
    assert (roots["conflict"] / "coordinator.local.md").read_bytes() == conflict_before
    assert not roots["gone"].exists()
    assert cli.main(["--fleet", "--json"]) == 1
    after = json.loads(capsys.readouterr().out)
    assert {r["slug"]: r["verdict"] for r in after["repos"]}["seedable"] == "ok"


def test_fleet_with_only_clean_repos_exits_zero(
    cli: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry(tmp_path / "home", {"ok": _four(tmp_path)["ok"]})
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    assert cli.main(["--fleet"]) == 0
    assert "1 repos, 0 repaired, 0 conflicts" in capsys.readouterr().out


def test_fleet_reads_a_nested_repos_table(
    cli: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reg = tmp_path / "home" / "machine-local"
    reg.mkdir(parents=True)
    absent = (tmp_path / "absent").as_posix()
    (reg / "registry.local.toml").write_text(f'[repos]\nx = "{absent}"\n', encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    assert cli.main(["--fleet"]) == 0
    assert "x" in capsys.readouterr().out
