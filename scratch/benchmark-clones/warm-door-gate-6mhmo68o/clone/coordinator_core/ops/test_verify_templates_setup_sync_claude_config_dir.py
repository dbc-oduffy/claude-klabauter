"""test_verify_templates_setup_sync_claude_config_dir.py — regression coverage
for verify_templates_setup_sync.main() resolving the live `setup/` side via
coordinator_core._settings_home.claude_config_dir() instead of a hand-rolled
`CLAUDE_HOME`/`Path.home()` join.

Spec backlink: cross-repo/inbox/2026-08-14-example-retrieval-repo-em-claude-home-routing-gap-c6-claude-klabauter-sites.md
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops import verify_templates_setup_sync as vtss


def test_main_reads_live_setup_from_claude_config_dir(monkeypatch, tmp_path, capsys):
    plugin_root = tmp_path / "plugin"
    templates_setup = plugin_root / "templates" / "setup"
    templates_setup.mkdir(parents=True)
    (templates_setup / "publish.sh").write_text("same\n", encoding="utf-8")

    config_dir = tmp_path / "harness-config"
    live_setup = config_dir / "setup"
    live_setup.mkdir(parents=True)
    (live_setup / "publish.sh").write_text("same\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    rc = vtss.main([""])

    assert rc == 0
    out = capsys.readouterr().out
    assert "OK           publish.sh" in out


def test_main_flags_mismatch_against_claude_config_dir_live_setup(monkeypatch, tmp_path, capsys):
    plugin_root = tmp_path / "plugin"
    templates_setup = plugin_root / "templates" / "setup"
    templates_setup.mkdir(parents=True)
    (templates_setup / "publish.sh").write_text("template version\n", encoding="utf-8")

    config_dir = tmp_path / "harness-config"
    live_setup = config_dir / "setup"
    live_setup.mkdir(parents=True)
    (live_setup / "publish.sh").write_text("live version\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    rc = vtss.main([""])

    assert rc == 1
    out = capsys.readouterr().out
    assert "MISMATCH     publish.sh" in out
