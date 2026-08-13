"""
coordinator_core.ops.test_probe_cwd_example_retrieval_repo_relevance — behavior-parity
tests for the naked-Python port of the coordinator-claude-owned bash script.

Port of: probe-cwd-example-retrieval-repo-relevance.sh (coordinator-claude b5a4192c, 2026-07-20).

Mirrors the AC-9 visibility-matrix coverage of the coordinator-claude-side bats-style test
(coordinator/tests/test_probe_cwd_example_retrieval_repo_relevance.sh) plus the known
pre-existing registry-path mismatch (see module docstring negative-spec in
probe_cwd_example_retrieval_repo_relevance.py) — reproduced here, not "fixed", to stay
byte-for-byte parity with the bash oracle.

No test touches the real $HOME — all fixtures are tmp_path-scoped and run via
COORDINATOR_TEST_HOME / COORDINATOR_TEST_PWD env-var overrides, exactly like
the bash test's fixture harness.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.ops import probe_cwd_example_retrieval_repo_relevance as subject  # noqa: E402


@pytest.fixture
def test_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "machine-local").mkdir(parents=True)
    (home / ".claude" / "plugins" / "coordinator-claude" / "data").mkdir(parents=True)
    (home / ".claude" / "plugins" / "example-retrieval-repo-ue-addon" / "data").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_TEST_HOME", str(home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    return home


def _fake_pwd(tmp_path, name="cwd"):
    d = tmp_path / name
    d.mkdir()
    return d


def _write_claude_json(home: Path, with_rag: bool):
    payload = (
        {"mcpServers": {"example-retrieval-repo": {"command": "python3", "args": ["/fake/cli.py"]}}}
        if with_rag
        else {"mcpServers": {"other-server": {}}}
    )
    (home / ".claude.json").write_text(json.dumps(payload))


def _write_registry_with_rag(home: Path, val: str = "/fake/example-retrieval-repo"):
    (home / ".claude" / "machine-local" / "registry.toml").write_text(
        f'[repos]\nexample_retrieval_repo = "{val}"\n'
    )


def _write_mcp_sentinel(home: Path, red: bool):
    payload = {"red_servers": ["example-retrieval-repo"] if red else []}
    (
        home
        / ".claude"
        / "plugins"
        / "coordinator-claude"
        / "data"
        / "mcp-registration-last-check.json"
    ).write_text(json.dumps(payload))


def _write_ue_addon_sentinel(home: Path, present: bool):
    if present:
        payload = {"ran_at": "2026-05-21T10:00:00Z", "verdict": "GREEN", "red_probes": [], "hint": ""}
    else:
        payload = {
            "ran_at": "2026-05-21T10:00:00Z",
            "verdict": "RED",
            "red_probes": ["engine-corpus"],
            "hint": "run /example-retrieval-repo-ue-addon:doctor to bootstrap",
        }
    (
        home / ".claude" / "plugins" / "example-retrieval-repo-ue-addon" / "data" / "doctor-last-run.json"
    ).write_text(json.dumps(payload))


def _run(monkeypatch, pwd: Path, capsys):
    monkeypatch.setenv("COORDINATOR_TEST_PWD", str(pwd))
    rc = subject.main([])
    out = capsys.readouterr().out
    return out, rc


# ---------------------------------------------------------------------------
# Test 1: no binding -> silent
# ---------------------------------------------------------------------------

def test_no_binding_is_silent(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=False)
    out, rc = _run(monkeypatch, _fake_pwd(tmp_path), capsys)
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# Test 2: non-UE, bound via registry.toml, MCP healthy.
#
# Reproduces the bash oracle's pre-existing quirk: _registry_bound() reads
# under settings_home()/machine-local (bridged, under COORDINATOR_TEST_HOME,
# to <home>/.coordinator-claude-settings/machine-local) — NOT
# <home>/.claude/machine-local, where this fixture (matching the bash test's
# write_registry_with_rag()) writes. The registry path therefore never
# resolves in this scenario and the probe stays silent — this is the SAME
# outcome the bash oracle produces (verified: its own Test 2 fails
# identically). Do not "fix" this expectation without also revisiting the
# bash contract.
# ---------------------------------------------------------------------------

def test_non_ue_bound_via_registry_reproduces_bash_oracle_quirk(test_home, tmp_path, monkeypatch, capsys):
    _write_registry_with_rag(test_home)
    _write_mcp_sentinel(test_home, red=False)
    out, rc = _run(monkeypatch, _fake_pwd(tmp_path), capsys)
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# Test 3: non-UE, bound via claude.json, MCP broken -> broken:
# ---------------------------------------------------------------------------

def test_non_ue_bound_mcp_broken_emits_broken(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=True)
    _write_mcp_sentinel(test_home, red=True)
    out, rc = _run(monkeypatch, _fake_pwd(tmp_path), capsys)
    assert rc == 0
    assert "[example-retrieval-repo-relevance] broken:" in out
    assert "healthy:" not in out


# ---------------------------------------------------------------------------
# Test 4: UE (.uproject present), bound, MCP healthy, corpus PRESENT
#         -> healthy-ue: only, no suggest-engine-corpus
# ---------------------------------------------------------------------------

def test_ue_bound_healthy_corpus_present(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=True)
    _write_mcp_sentinel(test_home, red=False)
    _write_ue_addon_sentinel(test_home, present=True)

    pwd = _fake_pwd(tmp_path)
    (pwd / "MyGame.uproject").touch()

    out, rc = _run(monkeypatch, pwd, capsys)
    assert rc == 0
    assert "[example-retrieval-repo-relevance] healthy-ue:" in out
    assert "suggest-engine-corpus:" not in out
    assert "p0-broken-ue:" not in out


# ---------------------------------------------------------------------------
# Test 5: UE, bound, MCP healthy, corpus MISSING (no sentinel)
#         -> healthy-ue: AND suggest-engine-corpus:
# ---------------------------------------------------------------------------

def test_ue_bound_healthy_corpus_missing(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=True)
    _write_mcp_sentinel(test_home, red=False)
    # No UE addon sentinel written -> MISSING.

    pwd = _fake_pwd(tmp_path)
    (pwd / "MyGame.uproject").touch()

    out, rc = _run(monkeypatch, pwd, capsys)
    assert rc == 0
    assert "[example-retrieval-repo-relevance] healthy-ue:" in out
    assert "suggest-engine-corpus:" in out


# ---------------------------------------------------------------------------
# Test 6: UE, bound, MCP broken -> p0-broken-ue: only
# ---------------------------------------------------------------------------

def test_ue_bound_mcp_broken(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=True)
    _write_mcp_sentinel(test_home, red=True)

    pwd = _fake_pwd(tmp_path)
    (pwd / "MyGame.uproject").touch()

    out, rc = _run(monkeypatch, pwd, capsys)
    assert rc == 0
    assert "[example-retrieval-repo-relevance] p0-broken-ue:" in out
    assert "healthy" not in out


# ---------------------------------------------------------------------------
# Test 7: no .uproject, whoami subprocess mock reports project_kind=ue
#         -> UE enrichment emitted
# ---------------------------------------------------------------------------

def test_whoami_mock_triggers_ue_enrichment(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=True)
    _write_mcp_sentinel(test_home, red=False)
    _write_ue_addon_sentinel(test_home, present=False)

    pwd = _fake_pwd(tmp_path)
    # Intentionally no .uproject file.

    # _resolve_py_interpreter() returns COORDINATOR_PYTHON as a single
    # executable token, invoked directly as `[py, "-c", script]`, where
    # `script` is a MULTI-LINE Python source string. A real interpreter
    # binary handles that fine, but simulating it via a fabricated
    # cross-platform executable hits a genuine Windows batch-launcher limit:
    # a `.cmd` launcher's `%*` argument-forwarding truncates at the first
    # embedded newline inside a quoted argument (verified directly — `%*`
    # loses everything after line 1 of `script`), so the mock never sees the
    # full script and can't detect the `coordinator_whoami` marker. Rather
    # than relaying through a batch shim, intercept the module's
    # `subprocess.run` call directly — this is the same "mock the runner,
    # not the platform's process-exec plumbing" approach used in
    # find_polluter's test suite for its analogous `npm` invocation.
    real_run = subprocess.run

    def _fake_whoami_run(args, **kwargs):
        if len(args) >= 2 and args[1] == "-c":
            return subprocess.CompletedProcess(args, 0, stdout="ue\n", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subject.subprocess, "run", _fake_whoami_run)
    monkeypatch.setenv("COORDINATOR_PYTHON", sys.executable)

    out, rc = _run(monkeypatch, pwd, capsys)
    assert rc == 0
    assert (
        "healthy-ue:" in out
        or "p0-broken-ue:" in out
        or "suggest-engine-corpus:" in out
    )


# ---------------------------------------------------------------------------
# Test 8: healthy UE case invoked twice -> emitted both times, identical
# (not throttled / idempotent)
# ---------------------------------------------------------------------------

def test_healthy_ue_not_throttled_and_idempotent(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=True)
    _write_mcp_sentinel(test_home, red=False)
    _write_ue_addon_sentinel(test_home, present=True)

    pwd = _fake_pwd(tmp_path)
    (pwd / "MyGame.uproject").touch()

    out_a, rc_a = _run(monkeypatch, pwd, capsys)
    out_b, rc_b = _run(monkeypatch, pwd, capsys)

    assert rc_a == 0 and rc_b == 0
    assert "[example-retrieval-repo-relevance] healthy-ue:" in out_a
    assert "[example-retrieval-repo-relevance] healthy-ue:" in out_b
    assert out_a == out_b


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------

def test_help_exits_zero_and_prints_usage(capsys):
    rc = subject.main(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Usage: probe-cwd-example-retrieval-repo-relevance.sh" in out
    assert "Exit 0 always." in out


# ---------------------------------------------------------------------------
# Negative corpus: malformed claude.json / registry / sentinels degrade
# gracefully rather than crashing (advisory contract — never gating).
# ---------------------------------------------------------------------------

def test_malformed_claude_json_does_not_crash(test_home, tmp_path, monkeypatch, capsys):
    (test_home / ".claude.json").write_text("{not valid json")
    out, rc = _run(monkeypatch, _fake_pwd(tmp_path), capsys)
    assert rc == 0
    assert out == ""


def test_malformed_mcp_sentinel_defaults_healthy(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=True)
    (
        test_home
        / ".claude"
        / "plugins"
        / "coordinator-claude"
        / "data"
        / "mcp-registration-last-check.json"
    ).write_text("{not valid json")
    out, rc = _run(monkeypatch, _fake_pwd(tmp_path), capsys)
    assert rc == 0
    assert "[example-retrieval-repo-relevance] healthy:" in out


def test_malformed_ue_addon_sentinel_treated_as_missing(test_home, tmp_path, monkeypatch, capsys):
    _write_claude_json(test_home, with_rag=True)
    _write_mcp_sentinel(test_home, red=False)
    (
        test_home / ".claude" / "plugins" / "example-retrieval-repo-ue-addon" / "data" / "doctor-last-run.json"
    ).write_text("{not valid json")

    pwd = _fake_pwd(tmp_path)
    (pwd / "MyGame.uproject").touch()

    out, rc = _run(monkeypatch, pwd, capsys)
    assert rc == 0
    assert "healthy-ue:" in out
    assert "suggest-engine-corpus:" in out
