"""host_install_agy.apply: JSON merge, foreign-server preservation, drift, malformed, byte stability, check mode."""
from __future__ import annotations

import json

import pytest

from coordinator_core.install import host_install_agy as hia


def _env(tmp_path, **extra):
    env = {"PATH": str(tmp_path / "emptybin"), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    env.update(extra)
    return env


def _cfg(tmp_path):
    return tmp_path / ".gemini" / "config" / "mcp_config.json"


URL = "http://127.0.0.1:8767/mcp"


@pytest.fixture(autouse=True)
def _mutation_allowed(monkeypatch):
    # The suite sets COORDINATOR_DISABLE_MACHINE_MUTATION=1; these tests write only under tmp_path.
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


def test_mutation_switch_refuses_the_host_config_write(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    with pytest.raises(hia.HostInstallRefused):
        hia.apply(tmp_path, _env(tmp_path))
    assert not _cfg(tmp_path).exists()


def test_fresh_home_writes_entry_only(tmp_path):
    r = hia.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hia.MCP_WRITTEN
    assert json.loads(_cfg(tmp_path).read_text()) == {"mcpServers": {"project-rag": {"serverUrl": URL}}}
    assert not (tmp_path / ".coordinator-claude-settings").exists()
    assert not hasattr(r, "fragment")


def test_rerun_is_byte_identical(tmp_path):
    hia.apply(tmp_path, _env(tmp_path))
    first = _cfg(tmp_path).read_bytes()
    r = hia.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hia.MCP_CURRENT
    assert _cfg(tmp_path).read_bytes() == first


def test_other_servers_and_keys_preserved(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"other": 1, "mcpServers": {"ctx": {"command": "npx", "args": ["x"]}}}))
    r = hia.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hia.MCP_WRITTEN
    got = json.loads(cfg.read_text())
    assert got["other"] == 1
    assert got["mcpServers"]["ctx"] == {"command": "npx", "args": ["x"]}
    assert got["mcpServers"]["project-rag"] == {"serverUrl": URL}


@pytest.mark.parametrize("content", ["", "  \n"])
def test_empty_config_is_treated_as_empty_object(tmp_path, content):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_text(content)
    assert hia.apply(tmp_path, _env(tmp_path)).mcp == hia.MCP_WRITTEN
    assert json.loads(cfg.read_text())["mcpServers"]["project-rag"] == {"serverUrl": URL}


def test_equal_existing_entry_is_current_and_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    original = '{"mcpServers": {"project-rag": {"serverUrl": "%s"}}}' % URL
    cfg.write_bytes(original.encode())
    r = hia.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hia.MCP_CURRENT
    assert cfg.read_bytes() == original.encode()


def test_matching_serverurl_with_extra_keys_is_current_and_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"project-rag": {"serverUrl": URL, "disabled": False}}}).encode()
    cfg.write_bytes(original)
    r = hia.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hia.MCP_CURRENT
    assert cfg.read_bytes() == original


@pytest.mark.parametrize(
    "entry",
    [
        {"serverUrl": "http://127.0.0.1:9999/mcp"},
        {"serverUrl": "http://127.0.0.1:9999/mcp", "disabled": False},
        {"command": "project-rag", "args": ["serve"]},
        "http://elsewhere/mcp",
    ],
)
def test_differing_entry_is_drift_never_overwritten(tmp_path, entry):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"project-rag": entry}}).encode()
    cfg.write_bytes(original)
    r = hia.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hia.MCP_DRIFT and r.notes
    assert cfg.read_bytes() == original


@pytest.mark.parametrize(
    "original",
    [b"{not json", b"[1, 2]", b'{"mcpServers": []}', b"\xff\xfe\x00"],
)
def test_malformed_config_left_untouched(tmp_path, original):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(original)
    r = hia.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hia.MCP_MALFORMED and r.notes
    assert cfg.read_bytes() == original


def test_check_mode_writes_nothing(tmp_path):
    r = hia.apply(tmp_path, _env(tmp_path), check=True)
    assert r.mcp == hia.MCP_WOULD_WRITE
    assert not (tmp_path / ".gemini").exists()
    assert not (tmp_path / ".coordinator-claude-settings").exists()


def test_check_mode_on_installed_state_leaves_bytes(tmp_path):
    hia.apply(tmp_path, _env(tmp_path))
    before = _cfg(tmp_path).read_bytes()
    r = hia.apply(tmp_path, _env(tmp_path), check=True)
    assert r.mcp == hia.MCP_CURRENT
    assert _cfg(tmp_path).read_bytes() == before


def test_check_mode_with_foreign_config_leaves_bytes(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    original = b'{"mcpServers": {"ctx": {"command": "x"}}}'
    cfg.write_bytes(original)
    r = hia.apply(tmp_path, _env(tmp_path), check=True)
    assert r.mcp == hia.MCP_WOULD_WRITE
    assert cfg.read_bytes() == original


def test_hooks_json_never_written(tmp_path):
    hooks = tmp_path / ".gemini" / "config" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(b'{"hooks": {}}')
    hia.apply(tmp_path, _env(tmp_path))
    assert hooks.read_bytes() == b'{"hooks": {}}'


def test_env_overrides_url(tmp_path):
    hia.apply(tmp_path, _env(tmp_path, EXAMPLE_RETRIEVAL_REPO_HTTP_HOST="localhost", EXAMPLE_RETRIEVAL_REPO_HTTP_PORT="9001"))
    got = json.loads(_cfg(tmp_path).read_text())
    assert got["mcpServers"]["project-rag"]["serverUrl"] == "http://localhost:9001/mcp"


def test_config_path_is_the_one_hostplan_names(tmp_path):
    from coordinator_core.install.host_targets import plan

    r = hia.apply(tmp_path, _env(tmp_path))
    assert r.config_path == plan("agy", tmp_path, _env(tmp_path)).config_path
