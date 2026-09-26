
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.plugin_health import forwarder_drift as fd


def _write_cli(agent_bin: Path, name: str) -> None:
    agent_bin.mkdir(parents=True, exist_ok=True)
    (agent_bin / f"{name}.py").write_text("#!/usr/bin/env python3\nprint('hi')\n")


def _write_forwarder(bin_dir: Path, installed_name: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / installed_name).write_text(
        f"#!/usr/bin/env python3\n"
        f"# coordinator-claude bin forwarder for {installed_name} — resolves claude-klabauter's\n"
        f"# coordinator/bin/ directory via the co-located _resolve_claude_klabauter.py shim.\n"
    )


@pytest.fixture
def two_bin_dirs(tmp_path: Path):
    return tmp_path / "settings-home-bin", tmp_path / "compat-bin"


def test_resolve_compat_bin_uses_userprofile_when_home_absent(tmp_path: Path, monkeypatch):
    """Native-Windows condition (home-resolution-lint bare_home_or_chain fix,
    2026-07-29): CLAUDE_HOME and HOME both absent, only USERPROFILE set.
    `_resolve_compat_bin` now delegates to `_settings_home.home_dir()`
    instead of a hand-rolled `CLAUDE_HOME or HOME` chain that degraded to a
    cwd-relative `.claude/bin` in exactly this condition."""
    from pathlib import Path as _Path

    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    userprofile_home = tmp_path / "winhome"
    monkeypatch.setenv("USERPROFILE", str(userprofile_home))
    # Path.home() only consults USERPROFILE on a real Windows interpreter;
    monkeypatch.setattr(_Path, "home", lambda: userprofile_home)

    assert fd._resolve_compat_bin() == userprofile_home / ".claude" / "bin"


def test_clean_match_no_drift(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "bar")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
        _write_forwarder(b, "bar")

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root")

    assert result.ok is True
    assert result.skipped is False
    assert result.lines[0].startswith("[info]")
    assert all(line.startswith(("[ok]", "[skip]")) for line in result.lines[1:])
    assert any("2 derived == 2 installed" in line for line in result.lines)


def test_derived_but_not_installed_is_named_drift(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "gen-settings-hooks")
    _write_cli(agent_bin, "run-platform-localize")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root")

    assert result.ok is False
    assert result.skipped is False
    warn_lines = [line for line in result.lines if line.startswith("[warn]")]
    assert len(warn_lines) == 1
    line = warn_lines[0]
    assert "settings-home/bin" in line
    assert "gen-settings-hooks" in line
    assert "run-platform-localize" in line
    assert "foo" not in line.split(":", 1)[1]


def test_compat_mirror_never_reports_missing_forwarders(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "review-assemble")
    _write_forwarder(settings_bin, "foo")
    _write_forwarder(settings_bin, "review-assemble")
    compat_bin.mkdir(parents=True, exist_ok=True)

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root")

    assert result.ok is True
    assert not any(line.startswith("[warn]") for line in result.lines)
    assert any("settings-home/bin" in line and "[ok]" in line for line in result.lines)
    compat_line = next(line for line in result.lines if fd._COMPAT_BIN_LABEL in line)
    assert compat_line.startswith("[ok]")
    assert "missing-check not applicable" in compat_line


def test_installed_but_not_derived_is_named_orphan(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
        _write_forwarder(b, "retired-cli")

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root")

    assert result.ok is False
    warn_lines = [line for line in result.lines if line.startswith("[warn]")]
    assert any("orphaned" in line and "retired-cli" in line for line in warn_lines)


def _write_doe_citation(doe_root: Path, skill_relpath: str, cli_name: str) -> None:
    """A DoE-claude-shaped prompt surface citing a settings-home entrypoint,
    matching the corpus's live shape (`resolve-coordinator-bin.md`'s
    Shape B fallback form) — see forwarder_drift.py's `_ENTRYPOINT_RE`."""
    path = doe_root / "coordinator" / skill_relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: fixture\n---\n\n"
        "Run:\n\n"
        "```bash\n"
        f'"${{COORDINATOR_SETTINGS_HOME:-${{CLAUDE_HOME:-$HOME}}/.coordinator-claude-settings}}/bin/{cli_name}" --root .\n'
        "```\n"
    )


def test_missing_and_cited_is_the_loud_arm(tmp_path: Path, two_bin_dirs):
    """CITED-VS-UNCITED SPLIT: a missing forwarder for a CLI a live
    DoE-claude prompt surface actually invokes gets the louder, 127-naming
    line — not today's plain "expected transient install lag" wording."""
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "check-auto-memory-drained")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
    _write_doe_citation(
        doe_root, "skills/workstream-complete/SKILL.md", "check-auto-memory-drained"
    )

    result = fd.check_forwarder_drift(
        settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root
    )

    assert result.ok is False
    warn_lines = [line for line in result.lines if line.startswith("[warn]")]
    loud = [line for line in warn_lines if "check-auto-memory-drained" in line]
    assert len(loud) == 1
    assert "cited by a live prompt-surface invocation" in loud[0]
    assert "NOTHING GATES ON IT" in loud[0]
    assert "SILENTLY" not in loud[0]  # AC4: the false "exits 127 SILENTLY" claim is gone
    assert "skills/workstream-complete/SKILL.md" in loud[0]
    assert not any(
        "have no installed forwarder —" in line and "check-auto-memory-drained" in line for line in warn_lines
    )


def test_missing_and_uncited_stays_the_plain_arm(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "some-uncited-cli")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
    # A citation for a DIFFERENT name only — proves the split discriminates
    _write_doe_citation(
        doe_root, "skills/workstream-complete/SKILL.md", "check-auto-memory-drained"
    )

    result = fd.check_forwarder_drift(
        settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root
    )

    assert result.ok is False
    warn_lines = [line for line in result.lines if line.startswith("[warn]")]
    plain = [line for line in warn_lines if "some-uncited-cli" in line]
    assert len(plain) == 1
    assert "have no installed forwarder —" in plain[0]
    assert "cited by a live prompt-surface invocation" not in plain[0]
    assert "127" not in plain[0]


def test_one_location_missing_is_reported_separately(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "baz")
    _write_forwarder(settings_bin, "foo")
    _write_forwarder(settings_bin, "baz")
    _write_forwarder(compat_bin, "foo")

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root")

    assert result.ok is True
    assert any("[ok]" in line and "settings-home/bin" in line for line in result.lines)
    compat_line = next(line for line in result.lines if fd._COMPAT_BIN_LABEL in line)
    assert compat_line.startswith("[ok]")
    assert "baz" not in compat_line


def test_unresolvable_claude_klabauter_root_is_a_clean_skip(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    _write_forwarder(settings_bin, "foo")
    _write_forwarder(compat_bin, "foo")

    def _raise():
        raise RuntimeError("repos.claude_klabauter is not set")

    monkeypatch.setattr(fd, "coordinator_engine_root", _raise)

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin)

    assert result.ok is True
    assert result.skipped is True
    assert len(result.lines) == 2
    assert result.lines[0].startswith("[info]")
    assert result.lines[1].startswith("[skip]")


def test_agent_bin_directory_missing_is_also_a_skip(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    nonexistent_root = tmp_path / "not-a-real-claude-klabauter-checkout"

    monkeypatch.setattr(fd, "coordinator_engine_root", lambda: str(nonexistent_root))

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin)

    assert result.ok is True
    assert result.skipped is True


def test_main_exits_zero_on_uncited_only_drift(tmp_path: Path, two_bin_dirs, monkeypatch, capsys):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "missing-one")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")

    monkeypatch.setattr(fd, "_resolve_agent_bin", lambda: agent_bin)
    monkeypatch.setattr(fd, "_resolve_settings_bin", lambda: settings_bin)
    monkeypatch.setattr(fd, "_resolve_compat_bin", lambda: compat_bin)
    monkeypatch.setattr(fd, "_resolve_doe_root", lambda: None)

    rc = fd.main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "missing-one" in captured.out


def test_main_exits_nonzero_on_cited_missing_set(tmp_path: Path, two_bin_dirs, monkeypatch):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "check-auto-memory-drained")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
    _write_doe_citation(
        doe_root, "skills/workstream-complete/SKILL.md", "check-auto-memory-drained"
    )

    monkeypatch.setattr(fd, "_resolve_agent_bin", lambda: agent_bin)
    monkeypatch.setattr(fd, "_resolve_settings_bin", lambda: settings_bin)
    monkeypatch.setattr(fd, "_resolve_compat_bin", lambda: compat_bin)
    monkeypatch.setattr(fd, "_resolve_doe_root", lambda: doe_root)

    rc = fd.main([])

    assert rc != 0


def test_cited_missing_field_is_empty_for_uncited_only_drift(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "some-uncited-cli")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")

    result = fd.check_forwarder_drift(
        settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root"
    )

    assert result.ok is False
    assert result.cited_missing == {}


def test_cited_missing_field_is_empty_for_orphan_only_drift(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
        _write_forwarder(b, "retired-cli")

    result = fd.check_forwarder_drift(
        settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root"
    )

    assert result.ok is False
    assert result.cited_missing == {}


def test_cited_missing_field_is_empty_on_skip(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    _write_forwarder(settings_bin, "foo")
    _write_forwarder(compat_bin, "foo")

    def _raise():
        raise RuntimeError("repos.claude_klabauter is not set")

    monkeypatch.setattr(fd, "coordinator_engine_root", _raise)

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin)

    assert result.ok is True
    assert result.skipped is True
    assert result.cited_missing == {}


def test_cited_missing_field_is_empty_for_clean_result(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "bar")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
        _write_forwarder(b, "bar")

    result = fd.check_forwarder_drift(
        settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root"
    )

    assert result.ok is True
    assert result.cited_missing == {}


def test_cited_missing_field_carries_sites_for_the_cited_set(tmp_path: Path, two_bin_dirs):
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "check-auto-memory-drained")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
    _write_doe_citation(
        doe_root, "skills/workstream-complete/SKILL.md", "check-auto-memory-drained"
    )

    result = fd.check_forwarder_drift(
        settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root
    )

    assert result.ok is False
    assert set(result.cited_missing.keys()) == {"check-auto-memory-drained"}
    assert any(
        "skills/workstream-complete/SKILL.md" in site
        for site in result.cited_missing["check-auto-memory-drained"]
    )


def _write_shape_w_citation(doe_root: Path, skill_relpath: str, cited_spelling: str, sep: str = "\\") -> None:
    """A Shape W (Windows PowerShell) settings-home entrypoint citation —
    `$env:COORDINATOR_SETTINGS_HOME\\bin\\<cited_spelling>` — matching
    `resolve-coordinator-bin.md`'s rung 0 form. ``sep`` lets a caller exercise
    the `/`-separated variant too."""
    path = doe_root / "coordinator" / skill_relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: fixture\n---\n\n"
        "Run:\n\n"
        "```powershell\n"
        f'& "$env:COORDINATOR_SETTINGS_HOME{sep}bin{sep}{cited_spelling}" push.outstanding \'{{}}\'\n'
        "```\n"
    )


def test_extension_mismatch_recorded_when_cmd_cited_but_exe_installed(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "app-session.exe").write_text("stub")
    _write_shape_w_citation(doe_root, "skills/app-session/SKILL.md", "app-session.cmd")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is False
    assert set(result.extension_mismatch.keys()) == {"app-session.cmd"}
    assert any("skills/app-session/SKILL.md" in site for site in result.extension_mismatch["app-session.cmd"])


def test_main_exits_nonzero_on_extension_mismatch(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "app-session.exe").write_text("stub")
    _write_shape_w_citation(doe_root, "skills/app-session/SKILL.md", "app-session.cmd")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)
    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fd, "_resolve_agent_bin", lambda: agent_bin)
    monkeypatch.setattr(fd, "_resolve_settings_bin", lambda: settings_bin)
    monkeypatch.setattr(fd, "_resolve_compat_bin", lambda: compat_bin)
    monkeypatch.setattr(fd, "_resolve_doe_root", lambda: doe_root)

    rc = fd.main([])

    assert rc != 0


def test_extension_clean_when_cited_spelling_matches_installed(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "app-session.exe").write_text("stub")
    _write_shape_w_citation(doe_root, "skills/app-session/SKILL.md", "app-session.exe")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root)

    assert result.extension_mismatch == {}


def test_extension_clean_for_legitimate_cmd_survivor(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "claude-home.cmd").write_text("stub")
    _write_shape_w_citation(doe_root, "snippets/resolve-coordinator-bin.md", "claude-home.cmd")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root)

    assert result.extension_mismatch == {}


def test_extension_axis_silent_when_no_sibling_installed_at_all(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "unrelated.exe").write_text("stub")
    _write_shape_w_citation(doe_root, "skills/percolate/SKILL.md", "percolate-push.cmd")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root)

    assert result.extension_mismatch == {}


def test_extension_axis_skips_on_non_windows_host(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "app-session.exe").write_text("stub")
    _write_shape_w_citation(doe_root, "skills/app-session/SKILL.md", "app-session.cmd")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: False)

    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root)

    assert result.extension_mismatch == {}
    assert any(
        "[skip]" in line and "extension axis" in line and "non-Windows" in line for line in result.lines
    )


def test_extension_axis_skip_does_not_block_exit_zero_on_would_be_mismatch(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "app-session.exe").write_text("stub")
    _write_shape_w_citation(doe_root, "skills/app-session/SKILL.md", "app-session.cmd")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: False)
    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fd, "_resolve_agent_bin", lambda: agent_bin)
    monkeypatch.setattr(fd, "_resolve_settings_bin", lambda: settings_bin)
    monkeypatch.setattr(fd, "_resolve_compat_bin", lambda: compat_bin)
    monkeypatch.setattr(fd, "_resolve_doe_root", lambda: doe_root)

    rc = fd.main([])

    assert rc == 0


def test_extension_axis_doe_root_unresolvable_is_empty_no_crash(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "app-session.exe").write_text("stub")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    result = fd.check_forwarder_drift(
        settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root"
    )

    assert result.extension_mismatch == {}


def test_check_extension_axis_direct_none_doe_root_is_empty_no_crash(tmp_path: Path, monkeypatch):
    settings_bin = tmp_path / "settings-bin"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "app-session.exe").write_text("stub")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    lines, mismatch = fd._check_extension_axis(None, settings_bin)

    assert mismatch == {}
    assert any("[skip]" in line and "extension axis" in line for line in lines)


def test_shape_w_citation_matched_with_forward_slash_separator(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "app-session.exe").write_text("stub")
    _write_shape_w_citation(doe_root, "skills/app-session/SKILL.md", "app-session.cmd", sep="/")
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root)

    assert set(result.extension_mismatch.keys()) == {"app-session.cmd"}


def test_installed_forwarder_names_matches_marker_despite_large_trailing_body(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "big-forwarder.exe"
    with open(path, "wb") as fh:
        fh.write(f"# coordinator-claude bin forwarder for big-forwarder\n".encode("utf-8"))
        fh.write(b"x" * (1024 * 1024))

    names = fd._installed_forwarder_names(bin_dir)

    assert names == {"big-forwarder.exe"}


def test_installed_forwarder_names_large_file_without_marker_is_excluded(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "big-non-forwarder.exe"
    with open(path, "wb") as fh:
        fh.write(b"just a native launcher, no marker here\n")
        fh.write(b"y" * (1024 * 1024))

    names = fd._installed_forwarder_names(bin_dir)

    assert names == set()


def test_installed_forwarder_names_marker_after_window_boundary_is_excluded(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "late-marker.exe"
    with open(path, "wb") as fh:
        fh.write(b"#" * 600)
        fh.write(b"\n# coordinator-claude bin forwarder for late-marker\n")

    names = fd._installed_forwarder_names(bin_dir)

    assert names == set()


def test_installed_forwarder_names_handles_binary_first_bytes_without_raising(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "binary-garbage.exe"
    with open(path, "wb") as fh:
        fh.write(bytes(range(256)) * 2)

    names = fd._installed_forwarder_names(bin_dir)

    assert names == set()


def test_installed_forwarder_names_still_excludes_cmd_and_ps1(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    _write_forwarder(bin_dir, "real-forwarder.exe")
    (bin_dir / "excluded.cmd").write_text(
        "# coordinator-claude bin forwarder for excluded\n"
    )
    (bin_dir / "excluded.ps1").write_text(
        "# coordinator-claude bin forwarder for excluded\n"
    )

    names = fd._installed_forwarder_names(bin_dir)

    assert names == {"real-forwarder.exe"}


def test_installed_forwarder_names_oserror_is_best_effort_skip(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    _write_forwarder(bin_dir, "ok-forwarder.exe")
    _write_forwarder(bin_dir, "unreadable.exe")

    real_open = open

    def _flaky_open(path, mode="r", *args, **kwargs):
        if "unreadable.exe" in str(path) and mode == "rb":
            raise OSError("simulated permission error")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(fd, "open", _flaky_open, raising=False)

    names = fd._installed_forwarder_names(bin_dir)

    assert names == {"ok-forwarder.exe"}


def test_shape_w_citation_trailing_period_is_stripped(tmp_path: Path, two_bin_dirs, monkeypatch):
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"
    settings_bin.mkdir(parents=True, exist_ok=True)
    (settings_bin / "workweek-complete-brief.exe").write_text("stub")
    path = doe_root / "coordinator" / "skills" / "workweek" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: fixture\n---\n\n"
        "See `$env:COORDINATOR_SETTINGS_HOME\\bin\\workweek-complete-brief.cmd`.\n"
    )
    monkeypatch.setattr(fd, "_is_windows_host", lambda: True)

    agent_bin = tmp_path / "empty-agent-bin"
    agent_bin.mkdir(parents=True, exist_ok=True)
    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=doe_root)

    assert set(result.extension_mismatch.keys()) == {"workweek-complete-brief.cmd"}


def test_remedy_no_longer_names_install_substrate():
    assert "install.substrate" not in fd._REMEDY
    assert "setup.py --i-am-agent" in fd._REMEDY
