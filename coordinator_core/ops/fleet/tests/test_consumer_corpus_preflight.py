"""
Fixture tests for coordinator_core.ops.fleet.consumer_corpus_preflight — the
DR-084-follow-up fleet-wide consumer-corpus vocabulary pre-flight.

Spec backlink: DoE-claude:pln-baton-kind-vocabulary-one-axis-d1ce8f § C5
Origin defect: state/improvement-queue/2026-07-23-vocabulary-retirement-needs-consumer-corpus-preflight.yaml
Hardening backlink: cross-repo/inbox/2026-07-31-doe-claude-em-consumer-corpus-preflight-blind-to-half-the-fleet.md
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import consumer_corpus_preflight as preflight


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _toml_path(repo_path) -> str:
    r"""TOML-escape a filesystem path for a basic string.

    Windows-first-class, not cosmetic: an unescaped `C:\Users\...` makes `\U`
    a TOML unicode escape, the whole registry fails to parse, and the test
    that appended it fails on an unrelated assertion. Every appender below
    routes through this rather than interpolating a path straight in.
    """
    return str(repo_path).replace("\\", "\\\\").replace('"', '\\"')


def _make_registry(tmp_path: Path, monkeypatch, repos: dict) -> None:
    """Minimal machine-local registry fixture (mirrors test_memo_resolver.py's factory)."""
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in repos.items():
        toml_val = _toml_path(repo_path)
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


def _handoff(root: Path, rel: str, kind: str | None, extra: str = "") -> None:
    fm = "---\ntitle: t\nstatus: open\n"
    if kind is not None:
        fm += f"kind: {kind}\n"
    fm += extra
    fm += "---\nBody.\n"
    _write(root / rel, fm)


def _register_all_fleet(tmp_path: Path, monkeypatch) -> dict:
    """Register every FLEET_REPO_KEYS suffix to its own tmp dir with an empty
    (but present) state/handoffs/ dir.

    Returns {display_name: repo_root_path}.
    """
    roots: dict[str, Path] = {}
    repos_for_registry: dict[str, Path] = {}
    for display_name, suffix in preflight.FLEET_REPO_KEYS.items():
        root = tmp_path / suffix
        (root / "state" / "handoffs").mkdir(parents=True)
        roots[display_name] = root
        repos_for_registry[suffix] = root
    _make_registry(tmp_path, monkeypatch, repos_for_registry)
    return roots


class TestScanRepoKindCounts:
    def test_scan_repo_kind_counts_splits_live_and_archived_exactly(self, tmp_path: Path) -> None:
        root = tmp_path / "fixture-repo"
        # Live population.
        _handoff(root, "state/handoffs/a.md", "spinoff")
        _handoff(root, "state/handoffs/b.md", "spinoff")
        _handoff(root, "state/handoffs/c.md", "session-handoff")
        _handoff(root, "state/handoffs/f.md", None)  # absent kind, live
        # Archived population — root-level archive/handoffs/.
        _handoff(root, "archive/handoffs/d.md", "spinoff-roadmap")
        # Archived — hidden nested dir under state/handoffs/.
        _handoff(root, "state/handoffs/.archive/e.md", "spinoff")
        # Archived — NON-hidden nested dir under state/handoffs/ (example-game-repo shape).
        _handoff(root, "state/handoffs/archive/i.md", "recovery")
        # Trailing-comment case, written directly to exercise comment-stripping (live).
        _write(
            root / "state" / "handoffs" / "h.md",
            "---\ntitle: t\nstatus: open\nkind: session-handoff  # reconciled\n---\nBody.\n",
        )

        live, archived = preflight.scan_repo_kind_counts(root)

        assert live == {
            "spinoff": 2,
            "session-handoff": 2,
            "<absent>": 1,
        }
        assert archived == {
            "spinoff-roadmap": 1,
            "spinoff": 1,
            "recovery": 1,
        }
        assert sum(live.values()) + sum(archived.values()) == 8


class TestLoadKindEnums:
    def test_real_vendored_live_schema_enum_matches_known_vocabulary(self) -> None:
        enum = preflight.load_live_kind_enum()
        assert set(enum) == {
            "session-handoff", "spinoff", "roadmap-baton", "goal-seed",
            "roadmap-seed", "recovery",
        }

    def test_real_vendored_archived_schema_enum_is_wider_than_live(self) -> None:
        enum = preflight.load_archived_kind_enum()
        assert set(enum) >= {
            "session-handoff", "spinoff", "spinoff-roadmap", "spinoff-goal",
            "spinoff-roadmap-creator", "recovery", "spike-result",
            "roadmap-baton", "goal-seed", "roadmap-seed",
        }

    def test_missing_live_schema_file_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(preflight.PreflightOracleError, match="live handoff schema not found"):
            preflight.load_live_kind_enum(tmp_path / "nope.schema.json")

    def test_missing_archived_schema_file_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(preflight.PreflightOracleError, match="archived handoff schema not found"):
            preflight.load_archived_kind_enum(tmp_path / "nope.schema.json")

    def test_unparseable_schema_file_fails_loud(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.schema.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(preflight.PreflightOracleError, match="unparseable"):
            preflight.load_live_kind_enum(bad)
        with pytest.raises(preflight.PreflightOracleError, match="unparseable"):
            preflight.load_archived_kind_enum(bad)

    def test_schema_with_no_kind_enum_fails_loud(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.schema.json"
        bad.write_text(json.dumps({"properties": {"kind": {"type": "string"}}}), encoding="utf-8")
        with pytest.raises(preflight.PreflightOracleError, match="no usable"):
            preflight.load_live_kind_enum(bad)

    def test_schema_missing_properties_key_entirely_fails_loud(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.schema.json"
        bad.write_text(json.dumps({"title": "no properties here"}), encoding="utf-8")
        with pytest.raises(preflight.PreflightOracleError, match="no usable"):
            preflight.load_archived_kind_enum(bad)


class TestRunPreflightRepoResolution:
    def test_run_preflight_reports_exact_counts_and_unresolvable_bucket_separately(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        doe_root = tmp_path / "doe-claude"
        claude_klabauter_root = tmp_path / "claude-klabauter"
        _handoff(doe_root, "state/handoffs/a.md", "spinoff")
        _handoff(doe_root, "state/handoffs/b.md", "session-handoff")
        _handoff(claude_klabauter_root, "archive/handoffs/2026-07/c.md", "spinoff")

        # example-retrieval-repo: registered, but the path does not exist on disk on THIS machine.
        missing_path = tmp_path / "not-cloned-here" / "example-retrieval-repo"

        # Every other fleet repo (cockpit, rag-ue-addon, example-game-repo, example-market-data-repo)
        # is deliberately NOT registered at all.
        _make_registry(tmp_path, monkeypatch, {
            "doe_claude": doe_root,
            "claude_klabauter": claude_klabauter_root,
            "example_retrieval_repo": missing_path,
        })

        report = preflight.run_preflight()

        assert report["exit_code"] == 1

        repos = report["repos"]
        assert repos["DoE-claude"]["resolved"] is True
        assert repos["DoE-claude"]["counts_live"] == {"spinoff": 1, "session-handoff": 1}
        assert repos["DoE-claude"]["counts_archived"] == {}
        assert repos["DoE-claude"]["total"] == 2

        assert repos["claude-klabauter"]["resolved"] is True
        assert repos["claude-klabauter"]["counts_live"] == {}
        assert repos["claude-klabauter"]["counts_archived"] == {"spinoff": 1}
        assert repos["claude-klabauter"]["total"] == 1

        # example-retrieval-repo: registered but path absent — UNRESOLVABLE, never a "zero records" claim.
        assert repos["example-retrieval-repo"]["resolved"] is False
        assert "does not exist on disk" in repos["example-retrieval-repo"]["reason"]

        # cockpit: key never registered — UNRESOLVABLE, never a "zero records" claim.
        assert repos["cockpit"]["resolved"] is False
        assert "not registered" in repos["cockpit"]["reason"]

        unresolvable_names = {entry["repo"] for entry in report["unresolvable"]}
        assert unresolvable_names == {
            "example-retrieval-repo", "cockpit", "example-retrieval-repo-ue-addon",
            "example-game-workbench-repo", "example-market-data-repo",
        }
        # An unresolvable repo must NEVER be silently counted as zero records — it
        # has no "counts_live"/"counts_archived"/"total" key at all, distinct from
        # a resolved repo with a genuinely empty corpus.
        assert "counts_live" not in repos["example-retrieval-repo"]
        assert "counts_live" not in repos["cockpit"]

    def test_run_preflight_all_resolvable_and_scanned_is_exit_zero(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        roots = _register_all_fleet(tmp_path, monkeypatch)
        _handoff(roots["DoE-claude"], "state/handoffs/a.md", "spinoff")

        report = preflight.run_preflight()

        assert report["exit_code"] == 0
        assert report["unresolvable"] == []
        assert report["unclassified"] == []
        assert report["off_enum_live"] == []
        assert report["off_enum_archived"] == []
        assert report["repos"]["claude-klabauter"]["resolved"] is True
        assert report["repos"]["claude-klabauter"]["counts_live"] == {}
        assert report["repos"]["claude-klabauter"]["counts_archived"] == {}
        assert report["repos"]["claude-klabauter"]["total"] == 0

    def test_registry_read_error_puts_every_repo_in_unresolvable(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        claude_home = tmp_path / "claude-home"
        machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        # Malformed TOML — a present file that fails to parse (RegistryReadError,
        # never a silent {} the way "no registry file at all" degrades).
        (machine_local / "registry.toml").write_text("this is not valid toml [[[", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

        report = preflight.run_preflight()

        assert report["exit_code"] == 1
        assert len(report["unresolvable"]) == len(preflight.FLEET_REPO_KEYS)
        for entry in report["unresolvable"]:
            assert "registry unreadable" in entry["reason"]
        # A registry that could not be read at all cannot be reconciled either —
        # unclassified must not fabricate entries from an unreadable registry.
        assert report["unclassified"] == []


class TestUnclassifiedReconciliation:
    def test_unrecognised_registered_key_trips_unclassified_and_exit_nonzero(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        _register_all_fleet(tmp_path, monkeypatch)
        # A brand-new repo registration nobody has classified yet.
        registry_local = (
            tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
            / "registry.local.toml"
        )
        new_repo_path = tmp_path / "some-new-em-tree"
        new_repo_path.mkdir()
        with open(registry_local, "a", encoding="utf-8") as fh:
            fh.write(f'"repos.brand_new_em" = "{_toml_path(new_repo_path)}"\n')

        report = preflight.run_preflight()

        assert report["exit_code"] == 1
        assert report["unresolvable"] == []
        assert report["off_enum_live"] == []
        unclassified_keys = {entry["key"] for entry in report["unclassified"]}
        assert unclassified_keys == {"repos.brand_new_em"}
        assert "classify" in report["unclassified"][0]["reason"]

    def test_known_non_fleet_excluded_key_does_not_trip_unclassified(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        _register_all_fleet(tmp_path, monkeypatch)
        registry_local = (
            tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
            / "registry.local.toml"
        )
        junk_path = tmp_path / "tmp-junk"
        junk_path.mkdir()
        with open(registry_local, "a", encoding="utf-8") as fh:
            fh.write(f'"repos.example-smoke-test-fixture" = "{_toml_path(junk_path)}"\n')

        report = preflight.run_preflight()

        assert report["exit_code"] == 0
        assert report["unclassified"] == []


class TestOffEnumDetection:
    def test_retired_kind_value_on_a_live_record_trips_off_enum_live_and_exit_nonzero(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        roots = _register_all_fleet(tmp_path, monkeypatch)
        _handoff(roots["cockpit"], "state/handoffs/a.md", "spinoff-roadmap")
        _handoff(roots["example-retrieval-repo-ue-addon"], "state/handoffs/b.md", "spinoff-roadmap")

        report = preflight.run_preflight()

        assert report["exit_code"] == 1
        assert report["unresolvable"] == []
        assert report["unclassified"] == []
        off_enum_by_repo = {(e["repo"], e["kind"]): e["count"] for e in report["off_enum_live"]}
        assert off_enum_by_repo == {
            ("cockpit", "spinoff-roadmap"): 1,
            ("example-retrieval-repo-ue-addon", "spinoff-roadmap"): 1,
        }
        assert report["off_enum_archived"] == []

    def test_retired_kind_value_on_an_archived_record_does_not_gate(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The exact defect fixed on coordinator review: an archived record on a
        retired kind is CORRECT (handoff-archived.schema.json deliberately still
        admits it) and must not trip exit_code."""
        roots = _register_all_fleet(tmp_path, monkeypatch)
        _handoff(roots["claude-klabauter"], "archive/handoffs/2026-07/c.md", "spinoff-roadmap")
        _handoff(roots["example-game-workbench-repo"], "state/handoffs/.archive/d.md", "spinoff-roadmap")
        _handoff(roots["example-game-workbench-repo"], "state/handoffs/archive/e.md", "spike-result")

        report = preflight.run_preflight()

        assert report["exit_code"] == 0
        assert report["off_enum_live"] == []
        off_enum_archived_by_repo = {(e["repo"], e["kind"]): e["count"] for e in report["off_enum_archived"]}
        # spinoff-roadmap/spike-result ARE in the archived enum, so neither trips
        # off_enum_archived either — this asserts the archived population is clean.
        assert off_enum_archived_by_repo == {}

    def test_a_value_off_even_the_archived_enum_warns_without_gating(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        roots = _register_all_fleet(tmp_path, monkeypatch)
        _handoff(roots["claude-klabauter"], "archive/handoffs/z.md", "not-a-real-kind")

        report = preflight.run_preflight()

        assert report["exit_code"] == 0, "an archived-off-enum finding must warn, not gate"
        assert report["off_enum_live"] == []
        assert report["off_enum_archived"] == [
            {"repo": "claude-klabauter", "kind": "not-a-real-kind", "count": 1}
        ]

    def test_archive_dir_variants_are_both_classified_archived(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        roots = _register_all_fleet(tmp_path, monkeypatch)
        _handoff(roots["DoE-claude"], "state/handoffs/.archive/a.md", "spinoff-roadmap")
        _handoff(roots["example-game-workbench-repo"], "state/handoffs/archive/b.md", "spinoff-roadmap")

        report = preflight.run_preflight()

        # Both land in the archived population (which admits spinoff-roadmap) —
        # neither gates, neither shows up as off_enum_live.
        assert report["exit_code"] == 0
        assert report["off_enum_live"] == []
        assert report["repos"]["DoE-claude"]["counts_archived"] == {"spinoff-roadmap": 1}
        assert report["repos"]["example-game-workbench-repo"]["counts_archived"] == {"spinoff-roadmap": 1}
        assert report["repos"]["DoE-claude"]["counts_live"] == {}
        assert report["repos"]["example-game-workbench-repo"]["counts_live"] == {}

    def test_absent_kind_never_trips_off_enum_in_either_population(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        roots = _register_all_fleet(tmp_path, monkeypatch)
        _handoff(roots["DoE-claude"], "state/handoffs/a.md", None)  # absent, live
        _handoff(roots["DoE-claude"], "state/handoffs/b.md", "session-handoff")
        _handoff(roots["DoE-claude"], "archive/handoffs/c.md", None)  # absent, archived

        report = preflight.run_preflight()

        assert report["exit_code"] == 0
        assert report["off_enum_live"] == []
        assert report["off_enum_archived"] == []

    def test_all_live_enum_values_do_not_trip_off_enum(self, tmp_path: Path, monkeypatch) -> None:
        roots = _register_all_fleet(tmp_path, monkeypatch)
        for i, kind in enumerate([
            "session-handoff", "spinoff", "roadmap-baton", "goal-seed", "roadmap-seed", "recovery",
        ]):
            _handoff(roots["DoE-claude"], f"state/handoffs/{i}.md", kind)

        report = preflight.run_preflight()

        assert report["exit_code"] == 0
        assert report["off_enum_live"] == []


class TestOracleFailurePropagates:
    def test_broken_live_schema_raises_uncaught_from_run_preflight(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        _register_all_fleet(tmp_path, monkeypatch)
        monkeypatch.setattr(
            preflight, "_HANDOFF_SCHEMA_PATH", tmp_path / "does-not-exist.schema.json"
        )
        with pytest.raises(preflight.PreflightOracleError):
            preflight.run_preflight()

    def test_broken_archived_schema_raises_uncaught_from_run_preflight(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        _register_all_fleet(tmp_path, monkeypatch)
        monkeypatch.setattr(
            preflight, "_ARCHIVED_HANDOFF_SCHEMA_PATH", tmp_path / "does-not-exist.schema.json"
        )
        with pytest.raises(preflight.PreflightOracleError):
            preflight.run_preflight()

    def test_main_catches_oracle_failure_and_returns_nonzero(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        _register_all_fleet(tmp_path, monkeypatch)

        def _boom(schema_path=None):
            raise preflight.PreflightOracleError("boom")
        monkeypatch.setattr(preflight, "load_live_kind_enum", _boom)

        rc = preflight.main([])

        assert rc == 1
        captured = capsys.readouterr()
        assert "ORACLE FAILURE" in captured.err
