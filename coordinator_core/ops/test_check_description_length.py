"""Tests for coordinator_core.ops.check_description_length.

Golden oracle (Port of: check-description-length.sh, example-doctrine-repo b5a4192c, 2026-07-20),
snapshotted 2026-07-16 against the real $HOME/.claude/plugins tree (positive,
mixed pass/fail) and synthetic negative corpora (missing plugins dir, mixed
pass/PM-GATED/description-budget/fail skills).
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.check_description_length import main


def _write_skill(root, plugin, skill, body):
    d = root / ".claude" / "plugins" / plugin / "skills" / skill
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def test_missing_plugins_root_is_clean_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert "all SKILL descriptions within budget" in out.out
    assert out.err == ""


def test_default_budget_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_skill(
        tmp_path,
        "pluginA",
        "skillone",
        '---\nname: skillone\ndescription: "This is a short description"\n---\nbody\n',
    )
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert "\t27\t150\tpass" in out.out


def test_pm_gated_uses_175_budget(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    desc = "PM-GATED: this is a longer description that needs the 175 budget instead of default 150 chars long enough now"
    _write_skill(
        tmp_path,
        "pluginA",
        "skilltwo",
        f'---\nname: skilltwo\ndescription: "{desc}"\n---\nbody\n',
    )
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert f"\t{len(desc)}\t175\tpass" in out.out


def test_explicit_description_budget_field_wins(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    desc = (
        "This description would fail default 150 budget but has an explicit "
        "description-budget override making it pass since it is padded out "
        "extra long here for the count"
    )
    _write_skill(
        tmp_path,
        "pluginA",
        "skillthree",
        f'---\nname: skillthree\ndescription-budget: 500\ndescription: "{desc}"\n---\nbody\n',
    )
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert f"\t{len(desc)}\t500\tpass" in out.out


def test_over_budget_fails_with_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    desc = (
        "This description is deliberately way too long to exceed the default "
        "one hundred fifty character budget so it should fail the check and "
        "report accordingly here"
    )
    _write_skill(
        tmp_path,
        "pluginA",
        "skillfail",
        f'---\nname: skillfail\ndescription: "{desc}"\n---\nbody\n',
    )
    rc = main([])
    out = capsys.readouterr()
    assert rc == 1
    assert f"\t{len(desc)}\t150\tfail" in out.err
    assert "ERROR: one or more SKILL descriptions exceed their budget" in out.err


def test_cache_and_marketplaces_dirs_excluded(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_skill(
        tmp_path,
        "cache",
        "skillcached",
        '---\nname: skillcached\ndescription: "x" \n---\nbody\n',
    )
    _write_skill(
        tmp_path,
        "marketplaces",
        "skillmp",
        '---\nname: skillmp\ndescription: "x"\n---\nbody\n',
    )
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert "all SKILL descriptions within budget" in out.out
    assert "skillcached" not in out.out
    assert "skillmp" not in out.out


def test_multiline_description_warns_and_skips(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_skill(
        tmp_path,
        "pluginA",
        "skillmulti",
        '---\nname: skillmulti\ndescription: "first line"\ndescription: "second line"\n---\nbody\n',
    )
    rc = main([])
    out = capsys.readouterr()
    assert rc == 0
    assert "WARN:" in out.err
    assert "multi-line description" in out.err
