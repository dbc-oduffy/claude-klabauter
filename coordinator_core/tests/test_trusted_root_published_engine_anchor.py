"""The stamped published engine root (registry repos.claude_klabauter) is a trust anchor."""
from __future__ import annotations

from coordinator_core import trusted_root_guard as g


def _env(tmp_path, registry_lines):
    sh = tmp_path / "settings-home"
    (sh / "machine-local").mkdir(parents=True)
    (sh / "machine-local" / "registry.toml").write_text("\n".join(registry_lines) + "\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return {"COORDINATOR_SETTINGS_HOME": str(sh), "HOME": str(home)}


def _mirror(tmp_path, stamp="sha:abc\n"):
    m = tmp_path / "klabauter"
    (m / "coordinator").mkdir(parents=True)
    (m / "coordinator_core").mkdir()
    if stamp is not None:
        (m / "coordinator_core" / "_engine_stamp").write_text(stamp, encoding="utf-8")
    return m


def _klabauter_env(tmp_path, m):
    return _env(
        tmp_path,
        ["[repos]", f'claude_klabauter = "{m.as_posix()}"', "[engine]", 'target = "claude-klabauter"'],
    )


def test_stamped_mirror_is_trusted(tmp_path):
    m = _mirror(tmp_path)
    env = _klabauter_env(tmp_path, m)
    assert g.is_trusted(str(m / "coordinator"), env=env) is True
    assert g.is_trusted(str(m), env=env) is True


def test_unstamped_mirror_is_not_trusted(tmp_path):
    m = _mirror(tmp_path, stamp=None)
    env = _klabauter_env(tmp_path, m)
    assert g._published_engine_root(env) == ""
    assert g.is_trusted(str(m / "coordinator"), env=env) is False


def test_empty_stamp_is_not_trusted(tmp_path):
    m = _mirror(tmp_path, stamp="")
    env = _klabauter_env(tmp_path, m)
    assert g.is_trusted(str(m / "coordinator"), env=env) is False


def test_registry_key_absent_gives_empty(tmp_path):
    env = _env(tmp_path, ["[engine]", 'target = "x"'])
    assert g._published_engine_root(env) == ""
    assert g._published_engine_root_rungs(env)[0][1] == "<absent>"


def test_traversal_under_mirror_still_rejected(tmp_path):
    m = _mirror(tmp_path)
    env = _klabauter_env(tmp_path, m)
    assert g.is_trusted(str(m / "coordinator" / ".." / "x"), env=env) is False


def test_pure_klabauter_registry_trusts_stamped_mirror(tmp_path, monkeypatch):
    # Pure klabauter install: no claude_klabauter keys, no .claude-klabauter-live-root. Self-location
    # is neutralised so the anchor under test is the only thing that can answer.
    monkeypatch.setattr(g, "_self_located_engine_root", lambda: "")
    m = _mirror(tmp_path)
    env = _klabauter_env(tmp_path, m)
    assert g._claude_klabauter_root(env) == ""
    assert g.is_trusted(str(m / "coordinator"), env=env) is True


def test_diagnosis_names_published_engine_rung(tmp_path):
    m = _mirror(tmp_path)
    env = _klabauter_env(tmp_path, m)
    text = g._diagnose_untrusted("/elsewhere", env)
    assert "published engine anchor" in text
    assert "engine stamp: 'present'" in text
