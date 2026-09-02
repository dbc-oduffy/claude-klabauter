"""
coordinator_core.plugin_health.tests.test_forwarder_drift

Coverage for the forwarder-drift staleness probe (see forwarder_drift.py's own
module docstring for the 2026-07-23 incident this closes: `gen-settings-hooks`
and `run-platform-localize` landed in claude-klabauter's coordinator/bin/ with no
installed forwarder in either write location, because no install had run
since they landed).

Every scenario uses tmp_path fixtures standing in for the real settings-home
bin/, ~/.claude/bin compat mirror, and claude-klabauter's coordinator/bin/ — never the
operator's actual settings home (see forwarder_drift.check_forwarder_drift's
explicit-override params, added exactly so tests never need env/monkeypatch
gymnastics around the real resolution ladder for the two bin dirs).

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-claude-klabauter-pickup-assemble-heads-up.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.plugin_health import forwarder_drift as fd


def _write_cli(agent_bin: Path, name: str) -> None:
    """A bare-name CLI landing in claude-klabauter's coordinator/bin/ (the source of
    truth `_derive_agent_helper_target_map` scans) — matches the shape a real
    `<name>.py` entry takes for the derive function's stem-stripping rule."""
    agent_bin.mkdir(parents=True, exist_ok=True)
    (agent_bin / f"{name}.py").write_text("#!/usr/bin/env python3\nprint('hi')\n")


def _write_forwarder(bin_dir: Path, installed_name: str) -> None:
    """An installed forwarder file carrying the exact marker line
    `_write_agent_forwarder` (coordinator_core/install/substrate.py) emits —
    forwarder_drift identifies forwarders by this content, not by name."""
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
    # simulate that resolution here so the test proves the delegation shape
    # (not stdlib platform behavior this test host can't exercise directly).
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
    assert result.lines[0].startswith("[info]")  # advisory disposition, emitted every run
    # `[skip]` is a clean outcome, not drift: the extension axis checks a
    # Windows-only citation shape and skips itself on every other host, so
    # demanding `[ok]` on every line made this assertion pass only on Windows.
    assert all(line.startswith(("[ok]", "[skip]")) for line in result.lines[1:])
    assert any("2 derived == 2 installed" in line for line in result.lines)


def test_derived_but_not_installed_is_named_drift(tmp_path: Path, two_bin_dirs):
    """The 2026-07-23 incident shape: a CLI lands in coordinator/bin/, no
    install has run since, so no forwarder exists for it yet.

    Only settings-home/bin reports this direction — see
    `test_compat_mirror_never_reports_missing_forwarders` for why the
    retired compat mirror does not (and must not) also warn here."""
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "gen-settings-hooks")
    _write_cli(agent_bin, "run-platform-localize")
    # Only "foo" ever got a forwarder written — the other two are the
    # incident's missing pair.
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root")

    assert result.ok is False
    assert result.skipped is False
    # Named individually — settings-home/bin ONLY (compat mirror's missing
    # direction is deliberately suppressed, see below).
    warn_lines = [line for line in result.lines if line.startswith("[warn]")]
    assert len(warn_lines) == 1
    line = warn_lines[0]
    assert "settings-home/bin" in line
    assert "gen-settings-hooks" in line
    assert "run-platform-localize" in line
    assert "foo" not in line.split(":", 1)[1]  # "foo" itself is not reported missing


def test_compat_mirror_never_reports_missing_forwarders(tmp_path: Path, two_bin_dirs):
    """Regression net for the 2026-07-27 alarm-fatigue fix: the retired
    ~/.claude/bin compat mirror gets ZERO new writes from
    `_install_bin_resolvers` (retired 2026-07-24) — every derived CLI is
    permanently "missing" there by construction, so reporting that
    direction there is guaranteed, unfixable noise on every single run,
    forever. This is exactly the noise that buried the real
    `review-assemble` gap in settings-home/bin (undetected 2026-07-26 to
    2026-07-27) inside 71 always-present, unactionable names. The compat
    mirror is entirely empty here (a fresh/never-installed compat dir,
    the realistic state on any machine post-retirement) — if the missing
    direction were still checked there, EVERY derived name would warn."""
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "review-assemble")
    _write_forwarder(settings_bin, "foo")
    _write_forwarder(settings_bin, "review-assemble")
    compat_bin.mkdir(parents=True, exist_ok=True)  # exists but empty — no forwarders at all

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root")

    assert result.ok is True  # settings-home/bin clean, compat's missing direction suppressed
    assert not any(line.startswith("[warn]") for line in result.lines)
    assert any("settings-home/bin" in line and "[ok]" in line for line in result.lines)
    compat_line = next(line for line in result.lines if fd._COMPAT_BIN_LABEL in line)
    assert compat_line.startswith("[ok]")
    assert "missing-check not applicable" in compat_line


def test_installed_but_not_derived_is_named_orphan(tmp_path: Path, two_bin_dirs):
    """Opposite direction: a forwarder survives after its source CLI was
    deleted from coordinator/bin/ — an orphan that 127s just as loudly."""
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
    # The plain wording is NOT used for the cited name.
    assert not any(
        "have no installed forwarder —" in line and "check-auto-memory-drained" in line for line in warn_lines
    )


def test_missing_and_uncited_stays_the_plain_arm(tmp_path: Path, two_bin_dirs):
    """The other half of the split: a missing forwarder for a CLI nothing in
    DoE-claude's prompt-surface corpus cites keeps today's plain wording —
    ordinary transient install lag, not an escalation."""
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "doe-claude"

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "some-uncited-cli")
    for b in (settings_bin, compat_bin):
        _write_forwarder(b, "foo")
    # A citation for a DIFFERENT name only — proves the split discriminates
    # by name, not by "any citation exists on this machine".
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
    """settings-home/bin and the ~/.claude/bin compat mirror are checked
    independently, each getting its own line — but only settings-home/bin's
    missing direction is actionable (see
    `test_compat_mirror_never_reports_missing_forwarders`), so a "baz"
    forwarder present at settings-home/bin but absent from the compat mirror
    must warn on settings-home/bin's ORPHAN side (the shapes are symmetric:
    a name present in one location and absent in the other is an orphan
    from whichever side has it), not silently vanish."""
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs

    _write_cli(agent_bin, "foo")
    _write_cli(agent_bin, "baz")
    _write_forwarder(settings_bin, "foo")
    _write_forwarder(settings_bin, "baz")
    _write_forwarder(compat_bin, "foo")  # compat mirror never got "baz" — not reported (missing suppressed there)

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root")

    assert result.ok is True
    assert any("[ok]" in line and "settings-home/bin" in line for line in result.lines)
    compat_line = next(line for line in result.lines if fd._COMPAT_BIN_LABEL in line)
    assert compat_line.startswith("[ok]")
    assert "baz" not in compat_line


def test_unresolvable_claude_klabauter_root_is_a_clean_skip(tmp_path: Path, two_bin_dirs, monkeypatch):
    """No repos.claude_klabauter registered anywhere (OSS consumer with no
    claude-klabauter checkout, or an unconfigured machine) must SKIP, never fail —
    this persona legitimately has nothing to compare."""
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
    assert result.lines[0].startswith("[info]")  # advisory disposition, emitted even on skip
    assert result.lines[1].startswith("[skip]")


def test_agent_bin_directory_missing_is_also_a_skip(tmp_path: Path, two_bin_dirs, monkeypatch):
    """coordinator_engine_root() resolves, but the coordinator/bin/ subpath
    itself doesn't exist (e.g. a partial/mis-pointed checkout) — same clean
    skip, not a crash."""
    settings_bin, compat_bin = two_bin_dirs
    nonexistent_root = tmp_path / "not-a-real-claude-klabauter-checkout"

    monkeypatch.setattr(fd, "coordinator_engine_root", lambda: str(nonexistent_root))

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin)

    assert result.ok is True
    assert result.skipped is True


def test_main_exits_zero_on_uncited_only_drift(tmp_path: Path, two_bin_dirs, monkeypatch, capsys):
    """AC2/AC7: uncited-only drift stays exit 0 — the WARN-only population
    is unaffected by the C1/C2 exit-contract split (matches
    scan-addon-health.py's own 'advisory, never gating' convention for that
    population)."""
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
    """AC2/AC7: a non-empty cited-but-missing set is the one population that
    gates — main() must return non-zero."""
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
    """AC1/AC7: the machine-readable field stays empty when the only drift is
    uncited — distinct from the rendered warn lines, which do exist here."""
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
    """AC7 regression guard: an orphaned forwarder alone must never populate
    `cited_missing` — orphan drift is a different axis (installed-but-not-
    derived), not a missing-and-cited one."""
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
    """AC6/AC7: an unresolvable claude-klabauter root is a clean skip, and must never
    populate `cited_missing` — the non-zero exit path fires only on a
    positively-computed non-empty set, never on 'could not determine'."""
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
    """AC7: a fully clean, no-drift result carries an empty `cited_missing`
    field too."""
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
    """AC1/AC7: the non-empty case — `cited_missing` names the CLI and the
    citing site, the same fact the rendered warn line carries, in
    machine-readable form."""
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
    """The six pre-engine bootstrap resolvers still ship as `.cmd` — a
    citation of the installed spelling must stay clean, never hardcoding
    that survivor list, letting the actual install be the oracle."""
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
    """A cited base name with NO installed sibling under any extension is the
    NAME axis' `cited_missing` population, not this axis' — must not appear
    in `extension_mismatch`."""
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
    # A nonexistent path (not None) — passing None here would fall through to
    # the real resolution ladder and pick up this machine's actual DoE-claude
    # checkout, defeating the point of this test (an unresolvable doe_root).
    result = fd.check_forwarder_drift(
        settings_bin=settings_bin, compat_bin=compat_bin, agent_bin=agent_bin, doe_root=tmp_path / "no-doe-root"
    )

    assert result.extension_mismatch == {}


def test_check_extension_axis_direct_none_doe_root_is_empty_no_crash(tmp_path: Path, monkeypatch):
    """Direct unit coverage of `_check_extension_axis(doe_root=None, ...)` —
    the actual "doe_root unresolvable" contract, exercised without routing
    through `check_forwarder_drift`'s own doe_root=None fallback (which would
    instead invoke the real resolution ladder)."""
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
    """Behavioural statement of "we read the head, not the file": a forwarder
    whose marker sits on its first line, followed by ~1 MB of trailing bytes
    (standing in for a real multi-hundred-KB native launcher image), is still
    identified — the bounded read must not silently stop matching."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "big-forwarder.exe"
    with open(path, "wb") as fh:
        fh.write(f"# coordinator-claude bin forwarder for big-forwarder\n".encode("utf-8"))
        fh.write(b"x" * (1024 * 1024))

    names = fd._installed_forwarder_names(bin_dir)

    assert names == {"big-forwarder.exe"}


def test_installed_forwarder_names_large_file_without_marker_is_excluded(tmp_path: Path):
    """A large file that never carries the marker is not identified."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "big-non-forwarder.exe"
    with open(path, "wb") as fh:
        fh.write(b"just a native launcher, no marker here\n")
        fh.write(b"y" * (1024 * 1024))

    names = fd._installed_forwarder_names(bin_dir)

    assert names == set()


def test_installed_forwarder_names_marker_after_window_boundary_is_excluded(tmp_path: Path):
    """The 512-byte window is a deliberate boundary, not an accident: a marker
    that appears only AFTER byte 512 must NOT be matched."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "late-marker.exe"
    with open(path, "wb") as fh:
        fh.write(b"#" * 600)  # padding well past the 512-byte window
        fh.write(b"\n# coordinator-claude bin forwarder for late-marker\n")

    names = fd._installed_forwarder_names(bin_dir)

    assert names == set()


def test_installed_forwarder_names_handles_binary_first_bytes_without_raising(tmp_path: Path):
    """Non-UTF-8/binary bytes in the first 512 bytes must not raise — matches
    the old `errors="ignore"` best-effort posture: the scan completes and
    simply does not match."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "binary-garbage.exe"
    with open(path, "wb") as fh:
        fh.write(bytes(range(256)) * 2)  # includes invalid UTF-8 byte sequences

    names = fd._installed_forwarder_names(bin_dir)

    assert names == set()


def test_installed_forwarder_names_still_excludes_cmd_and_ps1(tmp_path: Path):
    """The existing `.cmd`/`.ps1` exclusion still holds under the bounded
    binary-read implementation."""
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
    """The `except OSError: continue` best-effort posture still holds — an
    unreadable file is skipped, never a hard failure."""
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
