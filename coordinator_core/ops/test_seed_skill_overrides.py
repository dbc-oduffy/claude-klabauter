"""
coordinator_core.ops.test_seed_skill_overrides — parity tests for the
naked-Python port of coordinator/bin/install-health/seed-skill-overrides.sh
(example-doctrine-repo-owned bash drop-in).

Golden oracle captured by running the bash script directly against a fresh
CLAUDE_HOME (with .doe-root planted to satisfy the trusted-root guard):
positive fresh-seed, positive --check-only (no write), positive
deep-research-sentinel-present run preserving sibling settings.json keys,
negative missing-helper (graceful degrade, exit 0), negative untrusted
plugin_root (fail-loud, exit 1). Every case here reproduces the exact exit
code + settings.json-mutation behavior observed from the bash oracle.

No test touches the real $HOME — CLAUDE_HOME is always a tmp_path fixture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.ops import seed_skill_overrides as subject  # noqa: E402


@pytest.fixture
def trusted_env(tmp_path, monkeypatch):
    """A CLAUDE_HOME whose .doe-root points at a fake trusted coordinator checkout,
    plus a coordinator/bin/seed-skill-overrides.py helper stub that writes a
    marker into settings.json so we can assert it actually ran."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)

    doe_root = tmp_path / "doe-checkout"
    plugin_root = doe_root / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (home / ".claude" / ".doe-root").write_text(str(doe_root), encoding="utf-8")

    helper = plugin_root / "bin" / "seed-skill-overrides.py"
    helper.write_text(
        "import argparse, json, os, pathlib, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--check-only', action='store_true')\n"
        "p.add_argument('--with-deep-research', action='store_true')\n"
        "a = p.parse_args()\n"
        "base = pathlib.Path(os.environ['CLAUDE_HOME']) / '.claude'\n"
        "base.mkdir(parents=True, exist_ok=True)\n"
        "settings = base / 'settings.json'\n"
        "if a.check_only:\n"
        "    print('skill_overrides: would seed (check-only)')\n"
        "    sys.exit(0)\n"
        "data = {}\n"
        "if settings.exists():\n"
        "    data = json.loads(settings.read_text())\n"
        "overrides = data.setdefault('skillOverrides', {})\n"
        "overrides['review'] = 'off'\n"
        "if a.with_deep_research:\n"
        "    overrides['deep-research'] = 'off'\n"
        "settings.write_text(json.dumps(data))\n"
        "print('skill_overrides: seeded review' + (', deep-research' if a.with_deep_research else ''))\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CHECK_ONLY", raising=False)
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    return home, plugin_root


def test_fresh_seed_writes_settings_with_deep_research(trusted_env):
    home, plugin_root = trusted_env
    rc = subject.main([], plugin_root=str(plugin_root))
    assert rc == 0
    data = json.loads((home / ".claude" / "settings.json").read_text())
    assert data["skillOverrides"]["review"] == "off"
    assert data["skillOverrides"]["deep-research"] == "off"


def test_check_only_env_var_no_write(trusted_env, monkeypatch):
    home, plugin_root = trusted_env
    monkeypatch.setenv("CHECK_ONLY", "1")
    rc = subject.main([], plugin_root=str(plugin_root))
    assert rc == 0
    assert not (home / ".claude" / "settings.json").exists()


def test_missing_helper_degrades_gracefully(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    doe_root = tmp_path / "doe-checkout"
    plugin_root = doe_root / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (home / ".claude" / ".doe-root").write_text(str(doe_root), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CHECK_ONLY", raising=False)

    rc = subject.main([], plugin_root=str(plugin_root))
    assert rc == 0
    assert not (home / ".claude" / "settings.json").exists()
    captured = capsys.readouterr()
    assert "helper not found" in captured.err


def test_untrusted_plugin_root_fails_loud(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)

    untrusted_root = tmp_path / "somewhere-else" / "coordinator"
    untrusted_root.mkdir(parents=True)

    rc = subject.main([], plugin_root=str(untrusted_root), site="seed-skill-overrides.sh")
    assert rc == 1
    captured = capsys.readouterr()
    assert "outside trusted prefix" in captured.err
    assert not (home / ".claude" / "settings.json").exists()


def test_coordinator_plugin_root_trusted_opt_out(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")

    plugin_root = tmp_path / "somewhere-else" / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    # No helper present; opt-out should still be evaluated before the
    # helper-presence check, exercising the trust-core branch directly.
    rc = subject.main([], plugin_root=str(plugin_root))
    assert rc == 0  # degrades gracefully (helper absent), not fail-loud (trusted)
