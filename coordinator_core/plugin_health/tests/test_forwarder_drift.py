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
    assert all(line.startswith("[ok]") for line in result.lines[1:])
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
    """A example-doctrine-repo-shaped prompt surface citing a settings-home entrypoint,
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
    example-doctrine-repo prompt surface actually invokes gets the louder, 127-naming
    line — not today's plain "expected transient install lag" wording."""
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "example-doctrine-repo"

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
    example-doctrine-repo's prompt-surface corpus cites keeps today's plain wording —
    ordinary transient install lag, not an escalation."""
    agent_bin = tmp_path / "claude-klabauter-coordinator-bin"
    settings_bin, compat_bin = two_bin_dirs
    doe_root = tmp_path / "example-doctrine-repo"

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

    monkeypatch.setattr(fd, "coordinator_claude_klabauter_root", _raise)

    result = fd.check_forwarder_drift(settings_bin=settings_bin, compat_bin=compat_bin)

    assert result.ok is True
    assert result.skipped is True
    assert len(result.lines) == 2
    assert result.lines[0].startswith("[info]")  # advisory disposition, emitted even on skip
    assert result.lines[1].startswith("[skip]")


def test_agent_bin_directory_missing_is_also_a_skip(tmp_path: Path, two_bin_dirs, monkeypatch):
    """coordinator_claude_klabauter_root() resolves, but the coordinator/bin/ subpath
    itself doesn't exist (e.g. a partial/mis-pointed checkout) — same clean
    skip, not a crash."""
    settings_bin, compat_bin = two_bin_dirs
    nonexistent_root = tmp_path / "not-a-real-claude-klabauter-checkout"

    monkeypatch.setattr(fd, "coordinator_claude_klabauter_root", lambda: str(nonexistent_root))

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
    doe_root = tmp_path / "example-doctrine-repo"
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

    monkeypatch.setattr(fd, "coordinator_claude_klabauter_root", _raise)

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
    doe_root = tmp_path / "example-doctrine-repo"

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
