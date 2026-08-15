"""
Tests for coordinator_core.ops.fleet.capability_index — "fleet.aggregate_capability_index".

Spec backlink: state/handoffs/2026-07-21_190702_fleet-capability-aggregation-op.md
               cross-repo/archive/2026-07-18-claude-central-em-fleet-capability-aggregation-op.md
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops.fleet import capability_index as cap_index
from coordinator_core.ops.fleet._memo_resolver import RegistryReadError


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _make_registry(tmp_path: Path, monkeypatch, repos: dict) -> None:
    """Minimal machine-local registry fixture (mirrors
    test_consumer_corpus_preflight.py's _make_registry / test_memo_resolver.py's
    factory) — _memo_resolver.read_registry_repos() reads via
    coordinator_core._settings_home.machine_local_dir(), which honours
    CLAUDE_HOME/COORDINATOR_SETTINGS_HOME, NOT MACHINE_LOCAL_REGISTRY_DIR (that
    override is explicitly out of scope for that resolver — see
    coordinator_core/_settings_home.py's own module docstring). This is why this
    module's tests use the CLAUDE_HOME-pointing pattern rather than
    coordinator_core.testing.registry_sandbox.sandbox_registry_dir, which arms
    MACHINE_LOCAL_REGISTRY_DIR and would silently not redirect this reader.
    """
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


def _no_registry(tmp_path: Path, monkeypatch) -> None:
    """Point CLAUDE_HOME at a fresh dir with no machine-local registry at all —
    the legitimate "nothing configured" degrade (registry absent, not corrupt)."""
    claude_home = tmp_path / "claude-home-empty"
    claude_home.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


def _manifest(host_repo: str, generated_at: str, refresh_cadence: str, capabilities: list) -> dict:
    return {
        "host_repo": host_repo,
        "generated_at": generated_at,
        "refresh_cadence": refresh_cadence,
        "capabilities": capabilities,
    }


def _cap(
    cap_id: str, host_repo: str, *, maturity: str = "live", provenance: str = "curated",
    consume_seam: str = "some-cli --json", cap_class: str = "store",
) -> dict:
    return {
        "capability_id": cap_id,
        "capability_class": cap_class,
        "capability_label": cap_id.replace("-", " "),
        "consume_seam": consume_seam,
        "maturity": maturity,
        "provenance": provenance,
        "host_repo": host_repo,
    }


_FRESH = "2026-07-17T00:00:00Z"


class TestDurationParsing:
    def test_parses_plain_day_duration(self) -> None:
        assert cap_index._parse_iso8601_duration("P7D") == datetime.timedelta(days=7)

    def test_parses_p1d(self) -> None:
        assert cap_index._parse_iso8601_duration("P1D") == datetime.timedelta(days=1)

    def test_human_label_is_unparseable(self) -> None:
        assert cap_index._parse_iso8601_duration("weekly, by hand") is None

    def test_empty_and_non_string_are_unparseable(self) -> None:
        assert cap_index._parse_iso8601_duration("") is None
        assert cap_index._parse_iso8601_duration(None) is None


class TestAliasedRegistryKeys:
    def test_two_keys_aliasing_one_checkout_are_read_once(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Two repos.* keys legitimately point at the SAME checkout (observed live:
        repos.doe_claude and repos.example_doctrine_repo both resolve to the
        DoE-claude clone). Enumerating per-key rather than per-path would emit that
        repo's capabilities twice — a duplicate a consumer computing host_repo
        asymmetry (F1c) cannot distinguish from two genuine offers."""
        host_root = tmp_path / "host-repo"
        host_root.mkdir()
        sibling_root = tmp_path / "sibling-repo"
        _write_json(
            sibling_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest("sibling-repo", _FRESH, "P7D", [_cap("sib-cap", "sibling-repo")]),
        )
        _make_registry(
            tmp_path, monkeypatch,
            {"sibling": sibling_root, "sibling_alias": sibling_root},
        )

        build_time = datetime.datetime.fromisoformat(_FRESH.replace("Z", "+00:00"))
        index, skipped = cap_index.build_fleet_index(host_root, build_time=build_time)

        assert skipped == []
        assert [e["capability_id"] for e in index["entries"]] == ["sib-cap"]


class TestBuildFleetIndexAggregation:
    def test_valid_multi_manifest_aggregation_includes_host(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        sibling_root = tmp_path / "sibling-repo"
        _write_json(
            host_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest("host-repo", _FRESH, "P7D", [_cap("host-cap", "host-repo")]),
        )
        _write_json(
            sibling_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest("sibling-repo", _FRESH, "P7D", [_cap("sib-cap", "sibling-repo")]),
        )
        _make_registry(tmp_path, monkeypatch, {"sibling": sibling_root})

        build_time = datetime.datetime.fromisoformat(_FRESH.replace("Z", "+00:00"))
        index, skipped = cap_index.build_fleet_index(host_root, build_time=build_time)

        assert skipped == []
        assert index["ttl"] == cap_index._DEFAULT_TTL
        host_repos = {e["host_repo"] for e in index["entries"]}
        assert host_repos == {"host-repo", "sibling-repo"}
        assert validate_frontmatter(index, cap_index._INDEX_SCHEMA_PATH) == []

    def test_repo_with_no_manifest_is_a_silent_skip(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        host_root.mkdir(parents=True)
        sibling_root = tmp_path / "sibling-no-manifest"
        sibling_root.mkdir(parents=True)
        _make_registry(tmp_path, monkeypatch, {"sibling": sibling_root})

        index, skipped = cap_index.build_fleet_index(host_root)

        assert index["entries"] == []
        assert skipped == []  # no manifest present is normal, never recorded


class TestOssSafeEmptyDegrade:
    def test_registry_absent_and_no_host_manifest_is_empty_schema_valid_index(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        host_root.mkdir(parents=True)
        _no_registry(tmp_path, monkeypatch)

        index, skipped = cap_index.build_fleet_index(host_root)

        assert index["entries"] == []
        assert skipped == []
        assert validate_frontmatter(index, cap_index._INDEX_SCHEMA_PATH) == []

    def test_op_handler_succeeds_and_writes_empty_index_when_registry_absent(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        (host_root / ".git").mkdir(parents=True)  # not a real git dir; main_worktree_root stubbed below
        _no_registry(tmp_path, monkeypatch)
        monkeypatch.setattr(cap_index, "main_worktree_root", lambda common_dir: host_root)

        result = cap_index._fleet_aggregate_capability_index({}, repo_root=host_root)

        assert result["entry_count"] == 0
        assert result["skipped"] == []
        out_path = Path(result["out"])
        assert out_path.is_file()
        on_disk = json.loads(out_path.read_text(encoding="utf-8"))
        assert on_disk["entries"] == []
        assert validate_frontmatter(on_disk, cap_index._INDEX_SCHEMA_PATH) == []


class TestCorruptRegistryPropagates:
    def test_corrupt_registry_raises_registry_read_error(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        host_root.mkdir(parents=True)
        claude_home = tmp_path / "claude-home"
        machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text("this is not valid toml [[[", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

        with pytest.raises(RegistryReadError):
            cap_index.build_fleet_index(host_root)


class TestInvalidManifestSkippedNotFatal:
    def test_invalid_manifest_is_skipped_with_reason_and_other_repos_still_aggregate(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        good_sibling = tmp_path / "good-sibling"
        bad_sibling = tmp_path / "bad-sibling"
        host_root.mkdir(parents=True)
        _write_json(
            good_sibling.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest("good-sibling", _FRESH, "P7D", [_cap("good-cap", "good-sibling")]),
        )
        # Missing required top-level "host_repo" — the DoE missing-host_repo fixture shape.
        _write_json(
            bad_sibling.joinpath(*cap_index._MANIFEST_REL_PATH),
            {
                "generated_at": _FRESH,
                "refresh_cadence": "P7D",
                "capabilities": [_cap("orphan-cap", "bad-sibling")],
            },
        )
        _make_registry(
            tmp_path, monkeypatch, {"good": good_sibling, "bad": bad_sibling},
        )

        index, skipped = cap_index.build_fleet_index(host_root)

        assert len(skipped) == 1
        assert "bad-sibling" in skipped[0] or "manifest.json" in skipped[0]
        assert {e["capability_id"] for e in index["entries"]} == {"good-cap"}


class TestMaturityDowngradeFailClosed:
    def test_stale_manifest_downgrades_live_to_unverified(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        _write_json(
            host_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest("host-repo", "2020-01-01T00:00:00Z", "P7D", [_cap("stale-cap", "host-repo")]),
        )
        _no_registry(tmp_path, monkeypatch)

        build_time = datetime.datetime.fromisoformat(_FRESH.replace("Z", "+00:00"))
        index, _ = cap_index.build_fleet_index(host_root, build_time=build_time)

        assert index["entries"][0]["maturity"] == "unverified"

    def test_fresh_curated_live_survives_unchanged(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        _write_json(
            host_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest("host-repo", _FRESH, "P7D", [_cap("fresh-cap", "host-repo")]),
        )
        _no_registry(tmp_path, monkeypatch)
        build_time = datetime.datetime.fromisoformat(_FRESH.replace("Z", "+00:00"))

        index, _ = cap_index.build_fleet_index(host_root, build_time=build_time)

        assert index["entries"][0]["maturity"] == "live"

    def test_generated_provenance_live_fails_closed_harder_even_when_fresh(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        _write_json(
            host_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest(
                "host-repo", _FRESH, "P7D",
                [_cap("gen-cap", "host-repo", maturity="live", provenance="generated")],
            ),
        )
        _no_registry(tmp_path, monkeypatch)
        build_time = datetime.datetime.fromisoformat(_FRESH.replace("Z", "+00:00"))

        index, _ = cap_index.build_fleet_index(host_root, build_time=build_time)

        assert index["entries"][0]["maturity"] == "unverified"
        assert index["entries"][0]["provenance"] == "generated"  # provenance itself never rewritten

    def test_absent_maturity_is_never_touched_even_when_stale(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        _write_json(
            host_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest(
                "host-repo", "2020-01-01T00:00:00Z", "P7D",
                [_cap("tombstone-cap", "host-repo", maturity="absent")],
            ),
        )
        _no_registry(tmp_path, monkeypatch)

        index, _ = cap_index.build_fleet_index(host_root)

        assert index["entries"][0]["maturity"] == "absent"

    def test_downgrade_never_upgrades_a_declared_stale_value(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        _write_json(
            host_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest("host-repo", _FRESH, "P7D", [_cap("declared-stale", "host-repo", maturity="stale")]),
        )
        _no_registry(tmp_path, monkeypatch)
        build_time = datetime.datetime.fromisoformat(_FRESH.replace("Z", "+00:00"))

        index, _ = cap_index.build_fleet_index(host_root, build_time=build_time)

        # Fresh + reachable + curated: no downgrade trigger fires, so the
        # author's own "stale" declaration passes through unchanged (never
        # "corrected" to unverified OR upgraded to live).
        assert index["entries"][0]["maturity"] == "stale"


class TestNoSiblingWriteInvariant:
    def test_build_fleet_index_never_writes_into_any_sibling_path(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        sibling_root = tmp_path / "sibling-repo"
        manifest_path = sibling_root.joinpath(*cap_index._MANIFEST_REL_PATH)
        _write_json(
            manifest_path,
            _manifest("sibling-repo", _FRESH, "P7D", [_cap("sib-cap", "sibling-repo")]),
        )
        _make_registry(tmp_path, monkeypatch, {"sibling": sibling_root})
        before_files = sorted(p.relative_to(sibling_root) for p in sibling_root.rglob("*") if p.is_file())
        before_mtime = manifest_path.stat().st_mtime_ns
        before_bytes = manifest_path.read_bytes()

        cap_index.build_fleet_index(host_root)

        after_files = sorted(p.relative_to(sibling_root) for p in sibling_root.rglob("*") if p.is_file())
        assert after_files == before_files
        assert manifest_path.stat().st_mtime_ns == before_mtime
        assert manifest_path.read_bytes() == before_bytes

    def test_op_handler_only_ever_writes_the_host_fleet_index_path(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        host_root = tmp_path / "host-repo"
        sibling_root = tmp_path / "sibling-repo"
        host_root.mkdir(parents=True)
        _write_json(
            sibling_root.joinpath(*cap_index._MANIFEST_REL_PATH),
            _manifest("sibling-repo", _FRESH, "P7D", [_cap("sib-cap", "sibling-repo")]),
        )
        _make_registry(tmp_path, monkeypatch, {"sibling": sibling_root})
        monkeypatch.setattr(cap_index, "main_worktree_root", lambda common_dir: host_root)
        before_sibling_files = sorted(
            p.relative_to(sibling_root) for p in sibling_root.rglob("*") if p.is_file()
        )

        result = cap_index._fleet_aggregate_capability_index({}, repo_root=host_root)

        after_sibling_files = sorted(
            p.relative_to(sibling_root) for p in sibling_root.rglob("*") if p.is_file()
        )
        assert after_sibling_files == before_sibling_files
        assert Path(result["out"]) == host_root.joinpath(*cap_index._INDEX_REL_PATH)


class TestOpHandlerParamValidation:
    def test_no_repo_root_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            cap_index._fleet_aggregate_capability_index({}, repo_root=None)

    def test_invalid_ttl_raises_value_error(self, tmp_path: Path, monkeypatch) -> None:
        host_root = tmp_path / "host-repo"
        host_root.mkdir(parents=True)
        _no_registry(tmp_path, monkeypatch)
        monkeypatch.setattr(cap_index, "main_worktree_root", lambda common_dir: host_root)

        with pytest.raises(ValueError):
            cap_index._fleet_aggregate_capability_index({"ttl": ""}, repo_root=host_root)

    def test_ttl_override_is_carried_through(self, tmp_path: Path, monkeypatch) -> None:
        host_root = tmp_path / "host-repo"
        host_root.mkdir(parents=True)
        _no_registry(tmp_path, monkeypatch)
        monkeypatch.setattr(cap_index, "main_worktree_root", lambda common_dir: host_root)

        result = cap_index._fleet_aggregate_capability_index({"ttl": "P3D"}, repo_root=host_root)

        assert result["ttl"] == "P3D"


# ---------------------------------------------------------------------------
# Real-fixture / real-exemplar tests — pinned behind skip-if-absent guards per
# the dispatch brief ("Tests must not depend on DoE or example-retrieval-repo being
# present at a hardcoded absolute path in CI").
# ---------------------------------------------------------------------------

def _candidate_repo_root(env_var: str, *guesses: Path) -> Path | None:
    override = os.environ.get(env_var)
    if override and Path(override).is_dir():
        return Path(override)
    for guess in guesses:
        if guess.is_dir():
            return guess
    return None


_THIS_REPO_ROOT = Path(__file__).resolve().parents[4]  # coordinator_core/ops/fleet/tests/ -> repo root
_DOE_ROOT = _candidate_repo_root(
    "DOE_CLAUDE_ROOT", _THIS_REPO_ROOT.parent / "DoE-claude",
)
_EXAMPLE_RETRIEVAL_REPO_ROOT = _candidate_repo_root(
    "EXAMPLE_RETRIEVAL_REPO_ROOT", _THIS_REPO_ROOT.parent / "example-retrieval-repo",
)


@pytest.mark.skipif(_DOE_ROOT is None, reason="DoE-claude checkout not found — set DOE_CLAUDE_ROOT to pin it")
class TestDoeFixtures:
    def test_doe_valid_fixture_passes_schema_and_produces_one_entry(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        fixture = json.loads(
            (_DOE_ROOT / "coordinator" / "schemas" / "fixtures" / "capability-manifest" / "valid.json")
            .read_text(encoding="utf-8")
        )
        assert validate_frontmatter(fixture, cap_index._MANIFEST_SCHEMA_PATH) == []
        host_root = tmp_path / "host-repo"
        _write_json(host_root.joinpath(*cap_index._MANIFEST_REL_PATH), fixture)
        # Isolate from the REAL machine-local registry (registry_sandbox.py's own
        # rationale applies here verbatim) — without this, any sibling actually
        # registered on the machine running this test would also get aggregated,
        # making the entry count assertion below flaky/environment-dependent.
        _no_registry(tmp_path, monkeypatch)

        index, skipped = cap_index.build_fleet_index(host_root)

        assert skipped == []
        assert len(index["entries"]) == len(fixture["capabilities"])

    def test_doe_missing_host_repo_fixture_fails_schema_and_is_skipped(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        fixture = json.loads(
            (_DOE_ROOT / "coordinator" / "schemas" / "fixtures" / "capability-manifest" / "missing-host_repo.json")
            .read_text(encoding="utf-8")
        )
        assert validate_frontmatter(fixture, cap_index._MANIFEST_SCHEMA_PATH) != []
        host_root = tmp_path / "host-repo"
        _write_json(host_root.joinpath(*cap_index._MANIFEST_REL_PATH), fixture)
        _no_registry(tmp_path, monkeypatch)  # see rationale above

        index, skipped = cap_index.build_fleet_index(host_root)

        assert index["entries"] == []
        assert len(skipped) == 1


@pytest.mark.skipif(
    _EXAMPLE_RETRIEVAL_REPO_ROOT is None, reason="example-retrieval-repo checkout not found — set EXAMPLE_RETRIEVAL_REPO_ROOT to pin it",
)
class TestProjectRagRealExemplar:
    def test_real_example_retrieval_repo_manifest_aggregates_cleanly(self, tmp_path: Path, monkeypatch) -> None:
        real_manifest_path = _EXAMPLE_RETRIEVAL_REPO_ROOT / "state" / "capabilities" / "manifest.json"
        if not real_manifest_path.is_file():
            pytest.skip("example-retrieval-repo checkout present but state/capabilities/manifest.json absent")
        manifest = json.loads(real_manifest_path.read_text(encoding="utf-8"))
        assert validate_frontmatter(manifest, cap_index._MANIFEST_SCHEMA_PATH) == []

        host_root = tmp_path / "host-repo"
        _write_json(host_root.joinpath(*cap_index._MANIFEST_REL_PATH), manifest)
        _no_registry(tmp_path, monkeypatch)  # isolate from the real machine-local registry

        index, skipped = cap_index.build_fleet_index(host_root)

        assert skipped == []
        assert len(index["entries"]) == len(manifest["capabilities"])
        assert all(e["host_repo"] == "example-retrieval-repo" for e in index["entries"])
