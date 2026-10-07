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


def _read_registry(reg_dir: Path) -> dict:
    from coordinator_core.machine_resolver import merged_flat_registry

    return {k: v for k, v in merged_flat_registry().items() if k.startswith("repos.")}


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
    reg_dir = tmp_path / "registry-sandbox"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    from coordinator_core.install import resolution_journal as _journal_mod

    monkeypatch.setenv(
        _journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(tmp_path / "resolution-journal.jsonl")
    )
    return lib_dir, reg_dir


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

    _seed(bin_dir, {"repos.repo_alpha": "/manually/overridden/path"})

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


def test_no_machine_local_subprocess_for_n_repos(env, tmp_path, monkeypatch):
    """Registration, prune and only-if-absent are in-process: zero spawns
    regardless of how many repos discovery surfaces."""
    import subprocess

    lib_dir, reg_dir = env
    spawns = []

    def _trap(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", _trap)
    monkeypatch.setattr(subprocess, "Popen", _trap)
    repos = [str(tmp_path / "dev" / f"repo-{i}") for i in range(6)]
    _stub_discover(monkeypatch, repos)

    assert main(["--non-interactive"], self_dir=lib_dir) == 0

    assert spawns == []
    assert _read_registry(reg_dir) == {f"repos.repo_{i}": repos[i] for i in range(6)}


def _seed(reg_dir: Path, entries: dict) -> None:
    from coordinator_core.machine_resolver import registry_set

    for k, v in entries.items():
        registry_set(k, v)


@pytest.fixture
def standing(tmp_path, monkeypatch):
    cache = tmp_path / "claude" / "plugins" / "cache"
    cache.mkdir(parents=True)
    monkeypatch.setattr(rdr.repo_standing, "plugin_cache_root", lambda: cache)
    monkeypatch.setattr(rdr.repo_standing, "_registered_key", lambda p: None)

    def _profile(name):
        monkeypatch.setattr(rdr.machine_profile, "machine_profile", lambda: name)

    _profile("author")
    return cache, _profile


def test_install_clone_candidate_skipped_quietly(env, tmp_path, monkeypatch, capsys, standing):
    lib_dir, bin_dir = env
    cache, _ = standing
    clone = cache / "mkt" / "coordinator" / "1.0"
    clone.mkdir(parents=True)
    _stub_discover(monkeypatch, [str(clone)])

    assert main(["--non-interactive"], self_dir=lib_dir) == 0

    out = capsys.readouterr()
    assert "install clone, not a working repo" in out.out
    assert "install clone" not in out.err
    assert _read_registry(bin_dir) == {}


def test_cache_path_key_pruned_on_author(env, tmp_path, monkeypatch, capsys, standing):
    lib_dir, bin_dir = env
    cache, _ = standing
    stale = cache / "mkt" / "coordinator" / "1.0"
    stale.mkdir(parents=True)
    keep = tmp_path / "dev" / "real"
    keep.mkdir(parents=True)
    _seed(bin_dir, {"repos.coordinator": str(stale), "repos.real": str(keep)})
    _stub_discover(monkeypatch, [])

    assert main(["--non-interactive"], self_dir=lib_dir) == 0

    assert _read_registry(bin_dir) == {"repos.real": str(keep)}
    assert "pruned repos.coordinator" in capsys.readouterr().out


def test_missing_path_pruned_on_consumer(env, tmp_path, monkeypatch, capsys, standing):
    lib_dir, bin_dir = env
    _, profile = standing
    profile("consumer")
    _seed(bin_dir, {"repos.gone": str(tmp_path / "nowhere")})
    _stub_discover(monkeypatch, [])

    assert main(["--non-interactive"], self_dir=lib_dir) == 0

    assert _read_registry(bin_dir) == {}
    assert "pruned repos.gone" in capsys.readouterr().out


def test_missing_path_kept_on_author(env, tmp_path, monkeypatch, standing):
    lib_dir, bin_dir = env
    _seed(bin_dir, {"repos.gone": str(tmp_path / "nowhere")})
    _stub_discover(monkeypatch, [])

    assert main(["--non-interactive"], self_dir=lib_dir) == 0

    assert _read_registry(bin_dir) == {"repos.gone": str(tmp_path / "nowhere")}
