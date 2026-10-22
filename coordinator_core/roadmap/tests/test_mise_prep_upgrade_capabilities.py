"""mise-prep-upgrade derives `capabilities: []` only for a spined plan whose body names no UI consumer."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import yaml

_CLI_PATH = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "mise-prep-upgrade.py"


def _load():
    name = "mise_prep_upgrade_capabilities_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load()

_SPINE = "```yaml plan-tasks\n- id: C1\n  writes: [a.py]\n```\n"


def _plan(tmp_path: Path, fm_extra: str, body: str) -> Path:
    path = tmp_path / "2026-10-22-cap.md"
    path.write_text(f'---\ntitle: "A"\nstatus: draft\n{fm_extra}---\n\n{body}', encoding="utf-8", newline="\n")
    return path


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


def test_keyless_spined_plan_without_ui_tokens_derives_empty_capabilities(tmp_path):
    path = _plan(tmp_path, "", "# A\n\n" + _SPINE)
    assert "capabilities" in _cli.upgrade_plan(path, check=True)["derived"]
    assert "capabilities" not in _fm(path)
    assert _cli.upgrade_plan(path, check=False)["changed"] is True
    assert _fm(path)["capabilities"] == []
    assert "capabilities" not in _cli.upgrade_plan(path, check=True)["derived"]


def test_body_naming_ui_consumer_is_residue_and_writes_nothing(tmp_path):
    path = _plan(tmp_path, "", "# A\n\nThe ui_consumer row is pending.\n\n" + _SPINE)
    result = _cli.upgrade_plan(path, check=False)
    assert "capabilities" not in result["derived"]
    assert any(r.startswith("capabilities: body names a UI consumer (ui_consumer)") for r in result["residue"])
    assert "capabilities" not in _fm(path)


def test_ui_token_inside_code_fence_is_ignored(tmp_path):
    path = _plan(tmp_path, "", "# A\n\n```\nclick_path\n```\n\n" + _SPINE)
    assert "capabilities" in _cli.upgrade_plan(path, check=True)["derived"]


def test_no_spine_is_residue(tmp_path):
    path = _plan(tmp_path, "", "# A\n")
    result = _cli.upgrade_plan(path, check=False)
    assert "capabilities" not in result["derived"]
    assert any(r.startswith("capabilities: no `yaml plan-tasks` block") for r in result["residue"])


def test_plan_already_carrying_the_key_is_untouched(tmp_path):
    path = _plan(tmp_path, "capabilities: []\n", "# A\n\nui_consumer\n\n" + _SPINE)
    result = _cli.upgrade_plan(path, check=True)
    assert "capabilities" not in result["derived"]
    assert not any(r.startswith("capabilities:") for r in result["residue"])
