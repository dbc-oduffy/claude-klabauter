"""host_install_codex.apply: managed block, foreign-entry respect, byte stability, check mode."""
from __future__ import annotations

import pytest

from coordinator_core.install import host_install_codex as hic


def _env(tmp_path, **extra):
    env = {"PATH": str(tmp_path / "emptybin"), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    env.update(extra)
    return env


def _cfg(tmp_path):
    return tmp_path / ".codex" / "config.toml"


@pytest.fixture(autouse=True)
def _mutation_allowed(monkeypatch):
    # The suite sets COORDINATOR_DISABLE_MACHINE_MUTATION=1; these tests write only under tmp_path.
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


def test_mutation_switch_refuses_the_host_config_write(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    with pytest.raises(hic.HostInstallRefused):
        hic.apply(tmp_path, _env(tmp_path))
    assert not _cfg(tmp_path).exists()


def test_fresh_home_writes_block_only(tmp_path):
    r = hic.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hic.MCP_WRITTEN
    text = _cfg(tmp_path).read_text()
    assert text == hic.render_block("http://127.0.0.1:8767/mcp")
    assert text.count("[mcp_servers.project-rag]") == 1
    assert not (tmp_path / ".coordinator-claude-settings").exists()
    assert not hasattr(r, "fragment")


def test_rerun_is_byte_identical(tmp_path):
    hic.apply(tmp_path, _env(tmp_path))
    first = _cfg(tmp_path).read_bytes()
    r = hic.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hic.MCP_CURRENT
    assert _cfg(tmp_path).read_bytes() == first


def test_foreign_keys_preserved_and_block_appended(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    original = 'model = "x"\n\n[mcp_servers.node_repl]\ncommand = "node"\n'
    cfg.write_text(original)
    r = hic.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hic.MCP_WRITTEN
    text = cfg.read_text()
    assert text.startswith(original)
    assert text.endswith(hic.render_block("http://127.0.0.1:8767/mcp"))


def test_foreign_equivalent_http_entry_is_present_and_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    original = '[mcp_servers.project-rag]\nurl = "http://127.0.0.1:8767/mcp"\n'
    cfg.write_bytes(original.encode())
    r = hic.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hic.MCP_PRESENT
    assert cfg.read_bytes() == original.encode()


@pytest.mark.parametrize(
    "original",
    [
        '[mcp_servers.project-rag]\nurl = "http://127.0.0.1:9999/mcp"\n',
        '[mcp_servers.project-rag]\ncommand = "project-rag"\nargs = ["serve"]\n',
        '[mcp_servers]\nexample_retrieval_repo = { url = "http://elsewhere/mcp" }\n',
    ],
)
def test_foreign_differing_entry_is_drift_never_overwritten(tmp_path, original):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(original.encode())
    r = hic.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hic.MCP_DRIFT and r.notes
    assert cfg.read_bytes() == original.encode()


def test_stale_managed_block_is_rewritten_when_env_url_changes(tmp_path):
    hic.apply(tmp_path, _env(tmp_path))
    env = _env(tmp_path, EXAMPLE_RETRIEVAL_REPO_HTTP_PORT="9001")
    r = hic.apply(tmp_path, env)
    assert r.mcp == hic.MCP_WRITTEN
    text = _cfg(tmp_path).read_text()
    assert "9001" in text and "8767" not in text and text.count(hic.BLOCK_BEGIN) == 1


def test_check_mode_writes_nothing(tmp_path):
    r = hic.apply(tmp_path, _env(tmp_path), check=True)
    assert r.mcp == hic.MCP_WOULD_WRITE
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".coordinator-claude-settings").exists()


def test_check_mode_on_installed_state_leaves_bytes(tmp_path):
    hic.apply(tmp_path, _env(tmp_path))
    before = _cfg(tmp_path).read_bytes()
    r = hic.apply(tmp_path, _env(tmp_path), check=True)
    assert r.mcp == hic.MCP_CURRENT
    assert _cfg(tmp_path).read_bytes() == before


def test_malformed_toml_left_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(b"[unterminated\n")
    r = hic.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hic.MCP_MALFORMED
    assert cfg.read_bytes() == b"[unterminated\n"


def test_hooks_json_never_written(tmp_path):
    hooks = tmp_path / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(b'{"hooks": {"SessionStart": []}}')
    hic.apply(tmp_path, _env(tmp_path))
    assert hooks.read_bytes() == b'{"hooks": {"SessionStart": []}}'


def test_codex_home_env_redirects_config(tmp_path):
    ch = tmp_path / "elsewhere"
    hic.apply(tmp_path, _env(tmp_path, CODEX_HOME=str(ch)))
    assert (ch / "config.toml").exists()
    assert not (tmp_path / ".codex").exists()


def test_crlf_foreign_config_preserved(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    original = b'model = "x"\r\n'
    cfg.write_bytes(original)
    hic.apply(tmp_path, _env(tmp_path))
    assert cfg.read_bytes().startswith(original)


def test_begin_marker_without_end_is_malformed_and_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    body = f'{hic.BLOCK_BEGIN}\n[mcp_servers.{hic.MCP_SERVER_NAME}]\n'.encode()
    cfg.write_bytes(body)
    r = hic.apply(tmp_path, _env(tmp_path))
    assert r.mcp == hic.MCP_MALFORMED
    assert cfg.read_bytes() == body
