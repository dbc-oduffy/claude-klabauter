"""claude-klabauter owns the working-repos.yaml writer: append-only, idempotent, gated by the kill switch."""

from __future__ import annotations

import yaml

from coordinator_core.ops import discover_working_repos as dwr


def test_creates_a_loadable_file_with_one_entry_per_path(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    target = tmp_path / ".claude" / "working-repos.yaml"
    added = dwr.write_working_repos_yaml(["C:/coordinator-content-repo", "/home/u/claude-klabauter"], target)
    assert added == ["C:/coordinator-content-repo", "/home/u/claude-klabauter"]
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert [r["path"] for r in loaded["repos"]] == added


def test_rerun_is_a_noop_and_preserves_operator_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    target = tmp_path / "working-repos.yaml"
    target.write_text('repos:\n  - path: "/a"\n    github: o/a\n', encoding="utf-8")
    assert dwr.write_working_repos_yaml(["/a"], target) == []
    assert target.read_text(encoding="utf-8") == 'repos:\n  - path: "/a"\n    github: o/a\n'
    assert dwr.write_working_repos_yaml(["/a", "/b"], target) == ["/b"]
    text = target.read_text(encoding="utf-8")
    assert "github: o/a" in text and text.endswith('  - path: "/b"\n')


def test_kill_switch_refuses_the_write(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    target = tmp_path / "working-repos.yaml"
    assert dwr.write_working_repos_yaml(["/a"], target) == []
    assert not target.exists()


def test_main_write_flag_persists_discovered_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(dwr, "discover_repo_paths", lambda: ["/r1"])
    assert dwr.main(["--write"]) == 0
    assert (tmp_path / ".claude" / "working-repos.yaml").read_text(encoding="utf-8") == 'repos:\n  - path: "/r1"\n'
    assert capsys.readouterr().out == "/r1\n"
    assert dwr.main([]) == 0
