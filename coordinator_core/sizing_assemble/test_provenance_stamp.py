"""stamp() shape, DoE sha fallback, and the schema's provenance field."""

import json
from pathlib import Path

import jsonschema

from coordinator_core.sizing_assemble import provenance

SCHEMA = (
    Path(__file__).resolve().parent.parent
    / "frontmatter/schemas/sizing-object.schema.json"
)
SHA = "a" * 40


def test_stamp_shape_and_null_passthrough(monkeypatch):
    monkeypatch.setattr(
        provenance, "engine_build", lambda: {"engine_sha": None, "engine_dirty": None}
    )
    monkeypatch.setattr(provenance, "resolve_doe_plugin_sha", lambda: None)
    assert provenance.stamp() == {
        "engine_sha": None,
        "engine_dirty": None,
        "doe_plugin_sha": None,
    }


def test_stamp_spawns_no_subprocess(monkeypatch):
    import subprocess

    def refuse(*a, **k):
        raise AssertionError("stamp() spawned a subprocess")

    monkeypatch.setattr(subprocess.Popen, "__init__", refuse)
    stamped = provenance.stamp()
    assert set(stamped) == {"engine_sha", "engine_dirty", "doe_plugin_sha"}
    assert stamped["engine_dirty"] is None


def test_stamp_spawns_nothing_when_registry_and_env_rungs_fail(tmp_path, monkeypatch):
    import subprocess

    from coordinator_core import _claude_klabauter_root, resolve_coordinator_clone as rcc

    for key in ("CLAUDE_PLUGIN_ROOT", "COORDINATOR_ROOT", "COORDINATOR_SOURCE_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(rcc, "registry_get", lambda key: None)
    monkeypatch.setattr(rcc, "_read_content_root", lambda: None)

    def refuse(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(_claude_klabauter_root, "_machine_local_get", refuse)
    monkeypatch.setattr(rcc, "_machine_local_get", refuse)
    assert provenance.resolve_doe_plugin_sha() is None
    assert provenance.stamp()["doe_plugin_sha"] is None


def test_version_file_fallback_and_unresolvable(tmp_path, monkeypatch):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text(json.dumps({"version": "9.9.9"}))
    monkeypatch.setattr(
        "coordinator_core.resolve_coordinator_clone.resolve_content_root_spawn_free",
        lambda: str(tmp_path),
    )
    assert provenance.resolve_doe_plugin_sha() == "9.9.9"

    monkeypatch.setattr(
        "coordinator_core.resolve_coordinator_clone.resolve_content_root_spawn_free",
        lambda: None,
    )
    assert provenance.resolve_doe_plugin_sha() is None


def test_schema_accepts_provenance_and_refuses_unknown_key():
    schema = json.loads(SCHEMA.read_text())
    prov = schema["properties"]["provenance"]
    v = jsonschema.Draft202012Validator(prov)
    assert not list(v.iter_errors({"engine_sha": SHA, "engine_dirty": False, "doe_plugin_sha": None}))
    assert list(v.iter_errors({"engine_sha": SHA, "extra": 1}))
