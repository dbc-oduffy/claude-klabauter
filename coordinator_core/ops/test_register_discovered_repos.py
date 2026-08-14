"""Characterization + parity tests for coordinator_core.ops.register_discovered_repos.

Port of: register-discovered-repos.sh (DoE b644d5a9, 2026-07-22).
Spec backlink: F16 (install discovers working repos but never registers them into
the machine-local repos.* registry).

Discovery is stubbed via monkeypatching `register_discovered_repos._discover_working_repos_main`
(an in-process call, 2026-07-21 C18 sh_argv-class retirement) rather than shelling
out to a synthetic `discover-working-repos.sh`.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

import coordinator_core.ops.register_discovered_repos as rdr
from coordinator_core.install.resolution_journal import read_journal
from coordinator_core.install.write_surface import ShapedClause, validate
from coordinator_core.ops.register_discovered_repos import _derive_key, main


def _stub_discover(monkeypatch, repo_paths: list[str]) -> None:
    def _fake_discover(argv):
        for p in repo_paths:
            print(p)
        return 0

    monkeypatch.setattr(rdr, "_discover_working_repos_main", _fake_discover)


def _make_fake_machine_local(bin_dir: Path, registry: dict) -> Path:
    """A fake `machine-local` backed by a plain text file (one KEY=VALUE per line)
    so `has`/`get`/`set` all round-trip within a single test process without a real
    TOML registry.

    Windows note: `shutil.which("machine-local")` (the resolver `main()` uses) only
    matches names ending in one of PATHEXT's extensions (.COM/.EXE/.BAT/.CMD/...) on
    native Windows -- an extensionless script (correct for the bash/git-bash oracle
    on POSIX) is invisible to it there, which would leave the REAL operator-installed
    `machine-local.cmd` as the only match on PATH and re-open the leak this fixture
    exists to prevent. Mirror the real wrapper's own location-independent-forwarder
    pattern: emit an extensionless POSIX script on non-Windows, and a `.cmd` launcher
    on Windows (subprocess.run() execs a bare `.cmd` path directly -- CreateProcess
    special-cases .bat/.cmd -- so no interpreter-prefix wrapping is needed, unlike the
    `#!/usr/bin/env bash` discover-working-repos.sh case). The `.cmd` launcher below
    invokes `sys.executable` -- the same interpreter pytest is already running under
    -- purely to run a short-lived registry read/write and exit; test-only fixture
    code, not a long-lived interactive console spawn.  # popup-intentional-last-resort
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    reg_file = bin_dir / "_fake_registry.txt"
    reg_file.write_text("")
    impl = bin_dir / "_fake_machine_local_impl.py"
    impl.write_text(
        "import sys, pathlib\n"
        f"reg = pathlib.Path({str(reg_file)!r})\n"
        "lines = [l for l in reg.read_text().splitlines() if l.strip()]\n"
        "kv = dict(l.split('=', 1) for l in lines)\n"
        "cmd = sys.argv[1]\n"
        "key = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "if cmd == 'has':\n"
        "    sys.exit(0 if key in kv else 1)\n"
        "elif cmd == 'get':\n"
        "    if key in kv:\n"
        "        print(kv[key], end='')\n"
        "        sys.exit(0)\n"
        "    sys.exit(1)\n"
        "elif cmd == 'set':\n"
        "    val = sys.argv[3]\n"
        "    kv[key] = val\n"
        "    reg.write_text('\\n'.join(f'{k}={v}' for k, v in kv.items()) + '\\n')\n"
        "    sys.exit(0)\n"
        "sys.exit(9)\n"
    )
    if sys.platform.startswith("win"):
        script = bin_dir / "machine-local.cmd"
        script.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{impl}" %*\r\n'
        )
    else:
        script = bin_dir / "machine-local"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import runpy\n"
            f"runpy.run_path({str(impl)!r}, run_name='__main__')\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _read_registry(bin_dir: Path) -> dict:
    reg_file = bin_dir / "_fake_registry.txt"
    if not reg_file.exists():
        return {}
    return dict(l.split("=", 1) for l in reg_file.read_text().splitlines() if l.strip())


class TestDeriveKey:
    def test_lowercases_and_collapses(self):
        assert _derive_key("Repo-Alpha") == "repo_alpha"

    def test_strips_leading_trailing_underscore(self):
        assert _derive_key("-repo-") == "repo"

    def test_collapses_runs(self):
        assert _derive_key("a..b__c") == "a_b_c"


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    bin_dir = tmp_path / "bin"
    _make_fake_machine_local(bin_dir, {})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Belt-and-braces: even if PATH resolution mistakenly falls through to a
    # real `machine-local` binary (e.g. a future PATH-join bug), redirect its
    # registry writes into the tmp_path sandbox instead of the operator's
    # real machine-local registry. See coordinator_core.machine_resolver
    # (`MACHINE_LOCAL_REGISTRY_DIR` env override, "test isolation").
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry-sandbox"))
    # Isolate the resolution journal into the sandbox too -- `main()` now
    # calls `record_resolution` at its exit points, and without this the
    # default journal path would land under the real operator's
    # settings-home.
    from coordinator_core.install import resolution_journal as _journal_mod

    monkeypatch.setenv(
        _journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(tmp_path / "resolution-journal.jsonl")
    )
    return lib_dir, bin_dir


def test_check_only_reports_and_writes_nothing(env, tmp_path, monkeypatch):
    lib_dir, bin_dir = env
    repo_a = str(tmp_path / "dev" / "repo-alpha")
    repo_b = str(tmp_path / "dev" / "repo-beta")
    _stub_discover(monkeypatch, [repo_a, repo_b])

    rc = main(["--check-only"], self_dir=lib_dir)

    assert rc == 0
    assert _read_registry(bin_dir) == {}


def test_non_interactive_registers_absent_keys(env, tmp_path, monkeypatch, capsys):
    lib_dir, bin_dir = env
    repo_a = str(tmp_path / "dev" / "repo-alpha")
    repo_b = str(tmp_path / "dev" / "repo-beta")
    _stub_discover(monkeypatch, [repo_a, repo_b])

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    reg = _read_registry(bin_dir)
    assert reg["repos.repo_alpha"] == repo_a
    assert reg["repos.repo_beta"] == repo_b


def test_only_if_absent_never_clobbers(env, tmp_path, monkeypatch):
    lib_dir, bin_dir = env
    repo_a = str(tmp_path / "dev" / "repo-alpha")
    _stub_discover(monkeypatch, [repo_a])

    reg_file = bin_dir / "_fake_registry.txt"
    reg_file.write_text("repos.repo_alpha=/manually/overridden/path\n")

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert _read_registry(bin_dir)["repos.repo_alpha"] == "/manually/overridden/path"


def test_idempotent_once_fully_registered(env, tmp_path, monkeypatch, capsys):
    lib_dir, bin_dir = env
    repo_a = str(tmp_path / "dev" / "repo-alpha")
    _stub_discover(monkeypatch, [repo_a])

    main(["--non-interactive"], self_dir=lib_dir)
    capsys.readouterr()
    rc = main(["--check-only"], self_dir=lib_dir)

    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_unknown_argument_exits_1(env, tmp_path, capsys):
    lib_dir, _bin_dir = env
    rc = main(["--bogus"], self_dir=lib_dir)
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown argument: --bogus" in err


def test_discover_failure_skips_cleanly(env, tmp_path, monkeypatch, capsys):
    """In-process discovery raising an unexpected error degrades to exit 0
    with a stderr WARNING, not a crash -- mirrors the never-block posture of
    discover_working_repos.main() itself and the prior subprocess-failure
    disposition this replaces (formerly `test_missing_discover_script_skips_cleanly`,
    which tested an external-file gate that no longer exists post-C18)."""
    lib_dir, _bin_dir = env

    def _raising_discover(argv):
        raise RuntimeError("boom")

    monkeypatch.setattr(rdr, "_discover_working_repos_main", _raising_discover)

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert "working-repo discovery failed" in capsys.readouterr().err


def test_resolve_machine_local_fallback_uses_userprofile_when_home_absent(tmp_path, monkeypatch):
    """Native-Windows condition (home-resolution-lint bare_home_or_chain fix,
    2026-07-29): CLAUDE_HOME/HOME both absent, only USERPROFILE-equivalent
    home resolvable. `_resolve_machine_local`'s fallback rung now delegates
    to `_settings_home.home_dir()` instead of a hand-rolled `CLAUDE_HOME or
    HOME` chain that degraded to a cwd-relative `.claude/bin/machine-local`
    in exactly this condition."""
    from pathlib import Path as _Path

    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    userprofile_home = tmp_path / "winhome"
    fallback = userprofile_home / ".claude" / "bin" / "machine-local"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o755)
    # Path.home() only consults USERPROFILE on a real Windows interpreter;
    # simulate that resolution here so the test proves the delegation shape.
    monkeypatch.setattr(_Path, "home", lambda: userprofile_home)

    assert rdr._resolve_machine_local(tmp_path / "lib") == str(fallback)


def test_missing_machine_local_skips_cleanly(tmp_path, monkeypatch):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    _stub_discover(monkeypatch, [str(tmp_path / "dev" / "repo-x")])
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry-sandbox"))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "no-claude-home"))

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0


def test_empty_discovery_output_is_a_noop(env, tmp_path, monkeypatch):
    lib_dir, bin_dir = env
    _stub_discover(monkeypatch, [])

    rc = main(["--non-interactive"], self_dir=lib_dir)

    assert rc == 0
    assert _read_registry(bin_dir) == {}


class TestResolutionJournalWiring:
    """Spec backlink: docs/research/2026-08-06-install-receipt-persistence-design.md,
    chunk C8. `main()` journals what this ShapedClause actually resolved to
    at each of its exit points -- never what it merely declared."""

    def test_performed_registration_journals_resolved_entries(
        self, env, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
        lib_dir, bin_dir = env
        repo_a = str(tmp_path / "dev" / "repo-alpha")
        repo_b = str(tmp_path / "dev" / "repo-beta")
        _stub_discover(monkeypatch, [repo_a, repo_b])

        rc = main(["--non-interactive"], self_dir=lib_dir)

        assert rc == 0
        journal = read_journal()
        resolution = journal[rdr.WRITE_SURFACE.writer_id][rdr._SHAPED_CLAUSE_INDEX]
        keys = {entry.key for entry in resolution.entries}
        assert keys == {"repos.repo_alpha", "repos.repo_beta"}
        assert all(entry.kind == "machine-local-key" for entry in resolution.entries)

    def test_empty_discovery_journals_empty_resolution_not_no_row(
        self, env, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
        lib_dir, bin_dir = env
        _stub_discover(monkeypatch, [])

        rc = main(["--non-interactive"], self_dir=lib_dir)

        assert rc == 0
        journal = read_journal()
        # The writer/clause pairing IS present -- a known, empty resolution
        # -- distinct from being absent entirely (see next test).
        assert rdr.WRITE_SURFACE.writer_id in journal
        resolution = journal[rdr.WRITE_SURFACE.writer_id][rdr._SHAPED_CLAUSE_INDEX]
        assert resolution.entries == ()

    def test_op_not_running_journals_no_row(self, tmp_path, monkeypatch):
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        journal_path = tmp_path / "resolution-journal.jsonl"
        monkeypatch.setenv(
            "COORDINATOR_INSTALL_RESOLUTION_JOURNAL", str(journal_path)
        )
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry-sandbox"))
        _stub_discover(monkeypatch, [str(tmp_path / "dev" / "repo-x")])

        rc = main(["--non-interactive"], self_dir=lib_dir)

        assert rc == 0
        # machine-local was never found -- registration was never
        # attempted or determined this run, so nothing is journaled.
        journal = read_journal()
        assert rdr.WRITE_SURFACE.writer_id not in journal

    def test_round_trip_via_read_journal_matches_derive_receipt_entries_shape(
        self, env, tmp_path, monkeypatch
    ):
        """Proves `read_journal()`'s output is exactly what
        `receipt.derive_receipt_entries` needs for this clause index: a
        `{clause_index: ClauseResolution}` mapping keyed by this writer_id."""
        monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
        lib_dir, bin_dir = env
        repo_a = str(tmp_path / "dev" / "repo-alpha")
        _stub_discover(monkeypatch, [repo_a])

        main(["--non-interactive"], self_dir=lib_dir)

        from coordinator_core.install.receipt import derive_receipt_entries

        journal = read_journal()
        resolutions = journal[rdr.WRITE_SURFACE.writer_id]
        entries = derive_receipt_entries(rdr.WRITE_SURFACE, resolutions)
        assert len(entries) == 1
        assert entries[0].writer_id == rdr.WRITE_SURFACE.writer_id
        assert entries[0].key == "repos.repo_alpha"


class TestWriteSurfaceDeclaration:
    """Spec backlink: pln-writer-declared-write-surface-49d3bd,
    chunk C2c. The runtime-computed set of `repos.*` keys this writer
    registers cannot be flattened into a static list — it depends on
    whatever `discover_working_repos` finds on the machine running the
    test. These tests assert the declaration stays SHAPED and machine-
    independent, never that it matches any particular machine's registry.
    """

    def test_declaration_is_valid(self) -> None:
        assert validate(rdr.WRITE_SURFACE) == ()

    def test_declaration_names_the_writer_and_module(self) -> None:
        assert rdr.WRITE_SURFACE.writer_id == "register-discovered-repos"
        assert (
            rdr.WRITE_SURFACE.source_module
            == "coordinator_core.ops.register_discovered_repos"
        )

    def test_surface_is_a_single_shaped_clause_not_a_static_list(self) -> None:
        assert len(rdr.WRITE_SURFACE.clauses) == 1
        clause = rdr.WRITE_SURFACE.clauses[0]
        assert isinstance(clause, ShapedClause)
        # A shaped clause never carries an enumerable entries list — the
        # round-trip proof that this isn't the static form in disguise.
        assert not hasattr(clause, "entries")

    def test_discovered_by_names_the_actual_discovery_function(self) -> None:
        clause = rdr.WRITE_SURFACE.clauses[0]
        assert isinstance(clause, ShapedClause)
        assert clause.discovered_by
        assert clause.discovered_by == "discover_working_repos"

    def test_entry_template_carries_a_key_placeholder_not_a_resolved_key(
        self,
    ) -> None:
        clause = rdr.WRITE_SURFACE.clauses[0]
        assert isinstance(clause, ShapedClause)
        assert clause.entry_template.kind == "machine-local-key"
        assert clause.entry_template.key == "repos.<derived-key>"
        # Never a resolved key like "repos.claude_klabauter" — that would be
        # flattening a runtime-computed surface into a static example.
        assert "<" in clause.entry_template.key and ">" in clause.entry_template.key

    def test_declaration_is_independent_of_this_machines_actual_registry(
        self, monkeypatch
    ) -> None:
        # Simulate two different machine states (nothing discovered vs.
        # several repos discovered) and assert the declaration object is
        # identical either way — it describes the mechanism, not any run's
        # output.
        before = rdr.WRITE_SURFACE
        _stub_discover(monkeypatch, [])
        main(["--check-only"], self_dir=Path("."))
        _stub_discover(monkeypatch, ["/tmp/repo-a", "/tmp/repo-b"])
        main(["--check-only"], self_dir=Path("."))
        assert rdr.WRITE_SURFACE is before
        assert rdr.WRITE_SURFACE.clauses[0].entry_template.key == "repos.<derived-key>"
