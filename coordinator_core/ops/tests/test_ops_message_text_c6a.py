"""
coordinator_core.ops.tests.test_ops_message_text_c6a — pins C6a's rewrite.

C6a — session-integrity and anchor/pointer ops error text stops naming the
private coordinator-content-repo repo as a place to go, while the resolver's own
`repos.content_root` machine-local registry key stays verbatim (functional
identifier the operator types, exempt from the rewrite).

Covers the four write-scope modules for this chunk:
  - coordinator_core.ops.session.guard_settings_integrity (kill-switch
    banner's historical-disarm-status line, and the inline-install banner)
  - coordinator_core.ops.verify_skill_anchor_links (unresolved-root stderr)
  - coordinator_core.ops.init_anchor_injection_state (unresolved-root
    RuntimeError)
  - coordinator_core.ops.gen_content_root_pointer (unresolved/missing-root
    stderr + skipped-row messages)

Spec backlink: pln-message-text-stops-naming-a-re-5c92dd
    chunk C6a. Negative-spec: does not touch
    coordinator_core.ops.coordinator_content_root — that module's `_REMEDIATION`
    strings are claimed by a different, PM-authorized plan (HARD STOP).
"""

from __future__ import annotations

import pytest

from coordinator_core.ops import init_anchor_injection_state as init_mod
from coordinator_core.ops import verify_skill_anchor_links as verify_mod
from coordinator_core.ops import gen_content_root_pointer as gen_mod
from coordinator_core.ops.session import guard_settings_integrity as guard_mod


def test_init_anchor_injection_state_unresolved_root_names_no_repo(monkeypatch):
    monkeypatch.setattr(init_mod, "read_content_root", lambda: "")

    with pytest.raises(RuntimeError) as excinfo:
        init_mod._handler({})

    message = str(excinfo.value)
    assert "coordinator-content-repo" not in message
    assert "cannot resolve the coordinator root" in message
    assert "repos.content_root" in message


def test_verify_skill_anchor_links_unresolved_root_names_no_repo(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(verify_mod, "coordinator_content_root", lambda: None)

    with pytest.raises(SystemExit) as excinfo:
        verify_mod._plugin_root()

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "coordinator-content-repo" not in err
    assert "cannot resolve the coordinator root" in err
    assert "repos.content_root" in err


def test_gen_content_root_pointer_root_not_found_names_no_repo(monkeypatch, capsys, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(missing))

    rc = gen_mod.main([])

    assert rc == 1
    err = capsys.readouterr().err
    assert "DoE" not in err
    assert "resolved root not found" in err
    assert "repos.content_root" in err


def test_gen_content_root_pointer_coordinator_subdir_absent_names_no_repo(monkeypatch, capsys, tmp_path):
    content_root = tmp_path / "clone"
    content_root.mkdir()
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(content_root))

    rc = gen_mod.main([])

    assert rc == 1
    err = capsys.readouterr().err
    assert "DoE clone" not in err
    assert "repos.content_root" in err


def test_gen_content_root_pointer_graceful_skip_names_no_repo(monkeypatch, capsys):
    monkeypatch.setenv("REPO_CONTENT_ROOT", "")
    monkeypatch.setattr(gen_mod, "_registry_get", lambda key: None)
    monkeypatch.setattr(gen_mod, "_resolve_machine_local", lambda: None)

    rc = gen_mod.main(["--graceful-skip-unresolved"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "DoE clone" not in out
    assert "repos.content_root" in out


def test_kill_switch_historical_disarm_status_drops_repo_citation():
    assert "coordinator-content-repo" not in guard_mod._HISTORICAL_DISARM_STATUS
    assert "MET as of 2026-07-28" in guard_mod._HISTORICAL_DISARM_STATUS


def test_inline_install_banner_drops_repo_codename():
    assert "DoE" not in guard_mod._BANNER_INLINE_INSTALL
    assert "INLINE" in guard_mod._BANNER_INLINE_INSTALL
    assert "--plugin-dir" in guard_mod._BANNER_INLINE_INSTALL
