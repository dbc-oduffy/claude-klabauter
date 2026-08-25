"""
coordinator_core.frontmatter.tests.test_schema_drift_watch — tests for the
vendored-schema drift watch.

Covers the three verdicts the cadence gate must never confuse:
  no drift        -> MATCH   (clean)
  drift present   -> DRIFT   (surfaced, names the schema)
  DoE unreadable  -> INDETERMINATE, never MATCH and never DRIFT
  DoE absent      -> UNRESOLVED (not applicable — fresh machine / CI without sibling)

Every test builds its own throwaway git repo + schema dir under tmp_path. NOTHING here
touches the real vendored schemas under coordinator_core/frontmatter/schemas/ or the
real DoE clone — a drift test that perturbs the artifact it watches is a test that
leaves the tree dirty.

Negative-spec asserted throughout: scan_vendored_schema_drift NEVER raises. That is
the whole design of check_schema_drift_advisory (non-gating counterpart to the gating
check_schema_drift) and the property this wiring had to preserve.

Spec backlink: coordinator_core/frontmatter/schema_drift_watch.py module docstring.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_drift_watch import (
    STATUS_DRIFT,
    STATUS_INDETERMINATE,
    STATUS_MATCH,
    STATUS_UNRESOLVED,
    check_source_drift_advisory,
    check_source_drift_advisory_batch,
    resolve_doe_repo_path,
    resolve_opticon_repo_path,
    scan_vendored_schema_drift,
    vendored_schema_paths,
    vendored_source_paths,
)
from coordinator_core.frontmatter.schema_validate import check_schema_drift_advisory_batch
from coordinator_core.git_scope import reset_foreign_repo_probe_memo

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SCHEMA_A = "handoff.schema.json"
_SCHEMA_B = "improvement-queue.schema.json"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )


def _schema_body(marker: str) -> str:
    return json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "title": marker}, indent=2) + "\n"


@pytest.fixture()
def fake_doe(tmp_path: Path) -> Path:
    """A throwaway git repo shaped like a DoE clone: coordinator/schemas/*.schema.json at HEAD."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "DoE-fake"
    schemas = repo / "coordinator" / "schemas"
    schemas.mkdir(parents=True)
    for name in (_SCHEMA_A, _SCHEMA_B):
        (schemas / name).write_text(_schema_body(name), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "drift watch test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed schemas")
    return repo


@pytest.fixture()
def vendored_dir(tmp_path: Path) -> Path:
    """A throwaway vendored-schema dir, byte-identical to fake_doe's HEAD by default."""
    directory = tmp_path / "vendored"
    directory.mkdir()
    for name in (_SCHEMA_A, _SCHEMA_B):
        (directory / name).write_text(_schema_body(name), encoding="utf-8")
    return directory


class TestVendoredSchemaPaths:
    """The coverage set is globbed from disk, not hand-listed."""

    def test_globs_every_schema_file(self, vendored_dir: Path) -> None:
        names = [p.name for p in vendored_schema_paths(vendored_dir)]
        assert names == sorted([_SCHEMA_A, _SCHEMA_B])

    def test_new_schema_is_covered_without_a_code_change(self, vendored_dir: Path) -> None:
        """A newly vendored schema joins the watch automatically — the whole point."""
        (vendored_dir / "brand-new.schema.json").write_text(_schema_body("new"), encoding="utf-8")
        names = [p.name for p in vendored_schema_paths(vendored_dir)]
        assert "brand-new.schema.json" in names

    def test_missing_directory_returns_empty_not_raise(self, tmp_path: Path) -> None:
        assert vendored_schema_paths(tmp_path / "nope") == []

    def test_ignores_non_schema_files(self, vendored_dir: Path) -> None:
        (vendored_dir / "README.md").write_text("not a schema\n", encoding="utf-8")
        names = [p.name for p in vendored_schema_paths(vendored_dir)]
        assert "README.md" not in names

    def test_excludes_generated_not_vendored_memo_schemas(self, vendored_dir: Path) -> None:
        """cross-repo-memo.schema.json / archived-memo.schema.json are makima-
        GENERATED projections (Decision-0, C5), never DoE-vendored copies — if one
        ever lands in this dir by mistake it must not silently join the watch."""
        (vendored_dir / "cross-repo-memo.schema.json").write_text(_schema_body("x"), encoding="utf-8")
        (vendored_dir / "archived-memo.schema.json").write_text(_schema_body("y"), encoding="utf-8")
        names = [p.name for p in vendored_schema_paths(vendored_dir)]
        assert "cross-repo-memo.schema.json" not in names
        assert "archived-memo.schema.json" not in names


class TestNoDrift:
    """Vendored copies byte-identical to DoE HEAD -> MATCH, clean."""

    def test_all_match_yields_match(self, fake_doe: Path, vendored_dir: Path) -> None:
        report = scan_vendored_schema_drift(fake_doe, vendored_dir)
        assert report["status"] == STATUS_MATCH
        assert report["drifted"] == []
        assert report["indeterminate"] == []
        assert report["checked"] == 2
        assert sorted(report["matched"]) == sorted([_SCHEMA_A, _SCHEMA_B])


class TestDriftSurfaced:
    """A diverged vendored copy is surfaced and named."""

    def test_single_drift_is_surfaced(self, fake_doe: Path, vendored_dir: Path) -> None:
        (vendored_dir / _SCHEMA_B).write_text(_schema_body("LOCALLY CHANGED"), encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        assert [d["schema"] for d in report["drifted"]] == [_SCHEMA_B]
        assert report["matched"] == [_SCHEMA_A]
        assert _SCHEMA_B in report["summary"]
        assert "re-vendor" in report["summary"].lower()

    def test_doe_moving_forward_is_surfaced(self, fake_doe: Path, vendored_dir: Path) -> None:
        """The real-world shape: DoE commits a change, our pin stays put."""
        (fake_doe / "coordinator" / "schemas" / _SCHEMA_B).write_text(
            _schema_body("DoE MOVED"), encoding="utf-8"
        )
        _git(fake_doe, "add", "-A")
        _git(fake_doe, "commit", "-q", "-m", "DoE evolves the schema")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        assert [d["schema"] for d in report["drifted"]] == [_SCHEMA_B]

    def test_drift_outranks_indeterminate(self, fake_doe: Path, vendored_dir: Path) -> None:
        """One observed divergence must not be masked by another schema being unreadable."""
        (vendored_dir / _SCHEMA_B).write_text(_schema_body("LOCALLY CHANGED"), encoding="utf-8")
        # A vendored file with no counterpart at DoE HEAD -> indeterminate.
        (vendored_dir / "orphan.schema.json").write_text(_schema_body("orphan"), encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        assert [d["schema"] for d in report["drifted"]] == [_SCHEMA_B]
        assert [d["schema"] for d in report["indeterminate"]] == ["orphan.schema.json"]


class TestDriftDirection:
    """The `direction` key on each drifted entry, and its rendering in `summary`."""

    def test_local_addition_is_we_are_ahead(self, fake_doe: Path, vendored_dir: Path) -> None:
        vendored = json.loads((vendored_dir / _SCHEMA_B).read_text(encoding="utf-8"))
        vendored["extra_local_field"] = "only on our side"
        (vendored_dir / _SCHEMA_B).write_text(json.dumps(vendored, indent=2) + "\n", encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        assert report["drifted"][0]["direction"] == "we-are-ahead"
        assert f"{_SCHEMA_B} [we-are-ahead]" in report["summary"]

    def test_doe_addition_is_we_are_behind(self, fake_doe: Path, vendored_dir: Path) -> None:
        doe_schema_path = fake_doe / "coordinator" / "schemas" / _SCHEMA_B
        doe_schema = json.loads(doe_schema_path.read_text(encoding="utf-8"))
        doe_schema["extra_upstream_field"] = "only on DoE's side"
        doe_schema_path.write_text(json.dumps(doe_schema, indent=2) + "\n", encoding="utf-8")
        _git(fake_doe, "add", "-A")
        _git(fake_doe, "commit", "-q", "-m", "DoE adds a field")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        assert report["drifted"][0]["direction"] == "we-are-behind"
        assert f"{_SCHEMA_B} [we-are-behind]" in report["summary"]

    def test_independent_changes_on_both_sides_is_both(self, fake_doe: Path, vendored_dir: Path) -> None:
        doe_schema_path = fake_doe / "coordinator" / "schemas" / _SCHEMA_B
        doe_schema = json.loads(doe_schema_path.read_text(encoding="utf-8"))
        doe_schema["title"] = "DoE renamed it"
        doe_schema_path.write_text(json.dumps(doe_schema, indent=2) + "\n", encoding="utf-8")
        _git(fake_doe, "add", "-A")
        _git(fake_doe, "commit", "-q", "-m", "DoE renames")

        vendored = json.loads((vendored_dir / _SCHEMA_B).read_text(encoding="utf-8"))
        vendored["title"] = "we renamed it differently"
        (vendored_dir / _SCHEMA_B).write_text(json.dumps(vendored, indent=2) + "\n", encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        assert report["drifted"][0]["direction"] == "both"
        assert f"{_SCHEMA_B} [both]" in report["summary"]

    def test_missing_direction_renders_direction_unknown(
        self, fake_doe: Path, vendored_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An older advisory build that omits `direction` degrades to a legible
        placeholder rather than a bare KeyError or a blank segment."""
        real_advisory = __import__(
            "coordinator_core.frontmatter.schema_validate",
            fromlist=["check_schema_drift_advisory_batch"],
        ).check_schema_drift_advisory_batch

        def _advisory_without_direction(schema_paths, doe_repo_path):
            results = real_advisory(schema_paths, doe_repo_path)
            for result in results:
                result.pop("direction", None)
            return results

        monkeypatch.setattr(
            "coordinator_core.frontmatter.schema_drift_watch.check_schema_drift_advisory_batch",
            _advisory_without_direction,
        )
        (vendored_dir / _SCHEMA_B).write_text(_schema_body("LOCALLY CHANGED"), encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        assert report["drifted"][0]["direction"] is None
        assert f"{_SCHEMA_B} [direction unknown]" in report["summary"]


class TestDriftSchemaVersions:
    """local_version/doe_version threaded onto each drifted[] entry — passed through
    verbatim from check_schema_drift_advisory, never re-parsed by this module (see
    the module docstring's "SHAPE TO AVOID" note).

    Spec backlink: cross-repo/inbox/2026-07-26-doe-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md
    """

    def test_drifted_entry_carries_both_versions(self, fake_doe: Path, vendored_dir: Path) -> None:
        doe_schema_path = fake_doe / "coordinator" / "schemas" / _SCHEMA_B
        doe_schema = json.loads(doe_schema_path.read_text(encoding="utf-8"))
        doe_schema["x-schema-version"] = "2.0.0"
        doe_schema_path.write_text(json.dumps(doe_schema, indent=2) + "\n", encoding="utf-8")
        _git(fake_doe, "add", "-A")
        _git(fake_doe, "commit", "-q", "-m", "DoE bumps x-schema-version")

        vendored = json.loads((vendored_dir / _SCHEMA_B).read_text(encoding="utf-8"))
        vendored["x-schema-version"] = "1.0.0"
        (vendored_dir / _SCHEMA_B).write_text(json.dumps(vendored, indent=2) + "\n", encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        entry = report["drifted"][0]
        assert entry["local_version"] == "1.0.0"
        assert entry["doe_version"] == "2.0.0"

    def test_missing_version_keys_render_none(self, fake_doe: Path, vendored_dir: Path) -> None:
        """Neither fixture schema declares x-schema-version by default -> both None,
        not an exception or a fabricated value."""
        (vendored_dir / _SCHEMA_A).write_text(_schema_body("LOCALLY CHANGED"), encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        entry = report["drifted"][0]
        assert entry["local_version"] is None
        assert entry["doe_version"] is None


class TestDriftBumpClass:
    """local_bump_class/doe_bump_class/doe_bump_note threaded onto each drifted[]
    entry — passed through verbatim from check_schema_drift_advisory, never
    re-parsed by this module (see the module docstring's "SHAPE TO AVOID" note),
    and surfaced next to the direction marker in the DRIFT summary line. This
    module never derives a hold/no-hold verdict from the class (DR-097 §
    Reconciliation — holding is axis-dependent and out of scope here).

    Spec backlink: cross-repo/inbox/2026-07-27-doe-claude-em-bump-class-shipped-and-a-correction.md
    """

    def test_drifted_entry_carries_bump_class_and_note(self, fake_doe: Path, vendored_dir: Path) -> None:
        doe_schema_path = fake_doe / "coordinator" / "schemas" / _SCHEMA_B
        doe_schema = json.loads(doe_schema_path.read_text(encoding="utf-8"))
        doe_schema["x-bump-class"] = "nested-field-additive"
        doe_schema["x-bump-note"] = "added an optional field"
        doe_schema_path.write_text(json.dumps(doe_schema, indent=2) + "\n", encoding="utf-8")
        _git(fake_doe, "add", "-A")
        _git(fake_doe, "commit", "-q", "-m", "DoE declares a bump class")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        entry = report["drifted"][0]
        assert entry["local_bump_class"] is None
        assert entry["doe_bump_class"] == "nested-field-additive"
        assert entry["doe_bump_note"] == "added an optional field"

    def test_summary_line_carries_bump_class_next_to_direction(
        self, fake_doe: Path, vendored_dir: Path
    ) -> None:
        doe_schema_path = fake_doe / "coordinator" / "schemas" / _SCHEMA_B
        doe_schema = json.loads(doe_schema_path.read_text(encoding="utf-8"))
        doe_schema["x-bump-class"] = "major"
        doe_schema_path.write_text(json.dumps(doe_schema, indent=2) + "\n", encoding="utf-8")
        _git(fake_doe, "add", "-A")
        _git(fake_doe, "commit", "-q", "-m", "DoE declares a major bump")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        assert f"{_SCHEMA_B} [we-are-behind, bump-class major]" in report["summary"]

    def test_missing_bump_class_keys_render_none_and_no_summary_segment(
        self, fake_doe: Path, vendored_dir: Path
    ) -> None:
        """Neither fixture schema declares x-bump-class by default -> None on both
        sides, and the summary line's bracket carries no bump-class segment."""
        (vendored_dir / _SCHEMA_B).write_text(_schema_body("LOCALLY CHANGED"), encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_DRIFT
        entry = report["drifted"][0]
        assert entry["local_bump_class"] is None
        assert entry["doe_bump_class"] is None
        assert entry["doe_bump_note"] is None
        assert f"{_SCHEMA_B} [{entry['direction']}]" in report["summary"]
        assert "bump-class" not in report["summary"]


class TestIndeterminate:
    """Unreadable DoE side -> INDETERMINATE. Never MATCH (silent green), never DRIFT (false alarm)."""

    def test_not_a_git_repo_is_indeterminate(self, tmp_path: Path, vendored_dir: Path) -> None:
        not_a_repo = tmp_path / "not-a-repo"
        (not_a_repo / "coordinator" / "schemas").mkdir(parents=True)

        report = scan_vendored_schema_drift(not_a_repo, vendored_dir)

        assert report["status"] == STATUS_INDETERMINATE
        assert report["drifted"] == []
        assert len(report["indeterminate"]) == 2
        assert "INDETERMINATE" in report["summary"]
        assert "did not run" in report["summary"]

    def test_schema_absent_at_doe_head_is_indeterminate(
        self, fake_doe: Path, vendored_dir: Path
    ) -> None:
        (vendored_dir / "orphan.schema.json").write_text(_schema_body("orphan"), encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, vendored_dir)

        assert report["status"] == STATUS_INDETERMINATE
        assert [d["schema"] for d in report["indeterminate"]] == ["orphan.schema.json"]
        assert report["drifted"] == []

    def test_empty_vendored_set_is_indeterminate_not_vacuous_match(
        self, fake_doe: Path, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty-vendored"
        empty.mkdir()

        report = scan_vendored_schema_drift(fake_doe, empty)

        assert report["status"] == STATUS_INDETERMINATE
        assert report["checked"] == 0


class TestDoeCloneAbsent:
    """No DoE clone at all -> UNRESOLVED. Graceful, not an explosion, not a fault."""

    def test_unresolvable_doe_root_yields_unresolved(
        self, tmp_path: Path, vendored_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "coordinator_core.frontmatter.schema_drift_watch.resolve_doe_repo_path",
            lambda: None,
        )
        report = scan_vendored_schema_drift(None, vendored_dir)

        assert report["status"] == STATUS_UNRESOLVED
        assert report["doe_repo_path"] is None
        assert report["checked"] == 0
        assert report["drifted"] == []

    def test_nonexistent_path_resolves_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ladder rungs that do not carry coordinator/schemas/ are rejected, not returned."""
        monkeypatch.setenv("REPO_DOE_CLAUDE", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr(
            "coordinator_core.frontmatter.schema_drift_watch.read_doe_root_pointer",
            lambda: str(tmp_path / "also-not-there"),
        )
        # The sibling-layout rung may legitimately resolve on a dev machine; only assert
        # that a bogus env/pointer never wins.
        resolved = resolve_doe_repo_path()
        assert resolved != tmp_path / "does-not-exist"
        assert resolved != tmp_path / "also-not-there"

    def test_env_override_wins_when_valid(
        self, fake_doe: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REPO_DOE_CLAUDE", str(fake_doe))
        assert resolve_doe_repo_path() == fake_doe


class TestNeverRaises:
    """The non-gating contract: no input shape may produce an exception."""

    @pytest.mark.parametrize(
        "doe, vendored",
        [
            ("/definitely/not/here", "/also/not/here"),
            ("", ""),
            ("/dev/null", "/dev/null"),
        ],
    )
    def test_hostile_inputs_return_a_verdict(self, doe: str, vendored: str) -> None:
        report = scan_vendored_schema_drift(doe or None, vendored or None)
        assert report["status"] in {
            STATUS_MATCH,
            STATUS_DRIFT,
            STATUS_INDETERMINATE,
            STATUS_UNRESOLVED,
        }
        assert isinstance(report["summary"], str) and report["summary"]

    def test_indeterminate_is_never_reported_as_drift(
        self, tmp_path: Path, vendored_dir: Path
    ) -> None:
        """Regression guard on the exact confusion the advisory was designed to avoid."""
        broken_doe = tmp_path / "broken"
        (broken_doe / "coordinator" / "schemas").mkdir(parents=True)

        report = scan_vendored_schema_drift(broken_doe, vendored_dir)

        assert report["drifted"] == []
        assert report["status"] != STATUS_DRIFT
        assert report["status"] != STATUS_MATCH


class TestAdvisoryDeterminateKey:
    """The discriminator the watch relies on to tell 'matches' from 'could not read'."""

    def test_match_is_determinate(self, fake_doe: Path, vendored_dir: Path) -> None:
        from coordinator_core.frontmatter.schema_validate import check_schema_drift_advisory

        result = check_schema_drift_advisory(vendored_dir / _SCHEMA_A, fake_doe)
        assert result["diverged"] is False
        assert result["determinate"] is True

    def test_unreadable_is_not_determinate(self, tmp_path: Path, vendored_dir: Path) -> None:
        from coordinator_core.frontmatter.schema_validate import check_schema_drift_advisory

        result = check_schema_drift_advisory(vendored_dir / _SCHEMA_A, tmp_path / "not-a-repo")
        assert result["diverged"] is False, "unreadable must never be reported as drift"
        assert result["determinate"] is False

    def test_drift_is_determinate(self, fake_doe: Path, vendored_dir: Path) -> None:
        from coordinator_core.frontmatter.schema_validate import check_schema_drift_advisory

        (vendored_dir / _SCHEMA_A).write_text(_schema_body("CHANGED"), encoding="utf-8")
        result = check_schema_drift_advisory(vendored_dir / _SCHEMA_A, fake_doe)
        assert result["diverged"] is True
        assert result["determinate"] is True


class TestSchemaAdvisoryBatch:
    """`check_schema_drift_advisory_batch` — the DoE-side twin of the opticon batch below.

    Same two properties: the process count does not grow with N, and per-entry
    verdicts survive the batching."""

    def test_results_align_with_inputs_and_mix_verdicts(
        self, fake_doe: Path, vendored_dir: Path
    ) -> None:
        (vendored_dir / _SCHEMA_A).write_text(_schema_body("CHANGED"), encoding="utf-8")
        absent = vendored_dir / "not-vendored-in-doe.schema.json"
        absent.write_text(_schema_body("absent"), encoding="utf-8")
        paths = [vendored_dir / _SCHEMA_A, vendored_dir / _SCHEMA_B, absent]

        results = check_schema_drift_advisory_batch(paths, fake_doe)

        assert [r["schema"] for r in results] == [p.name for p in paths]
        assert (results[0]["diverged"], results[0]["determinate"]) == (True, True)
        assert (results[1]["diverged"], results[1]["determinate"]) == (False, True)
        assert (results[2]["diverged"], results[2]["determinate"]) == (False, False), (
            "a schema absent from DoE HEAD is indeterminate for THAT entry only"
        )

    def test_process_count_does_not_grow_with_the_set(
        self, fake_doe: Path, vendored_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawns: list[list[str]] = []
        real_run = subprocess.run

        def counting_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
            spawns.append(list(argv))
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", counting_run)

        reset_foreign_repo_probe_memo()
        check_schema_drift_advisory_batch([vendored_dir / _SCHEMA_A], fake_doe)
        after_one = len(spawns)
        reset_foreign_repo_probe_memo()
        two = check_schema_drift_advisory_batch(
            [vendored_dir / _SCHEMA_A, vendored_dir / _SCHEMA_B], fake_doe
        )

        assert len(two) == 2
        assert len(spawns) - after_one == after_one, (
            f"two schemas cost {len(spawns) - after_one} spawns against {after_one} for one: "
            f"{spawns[after_one:]}"
        )

    def test_empty_set_spawns_nothing(self, fake_doe: Path) -> None:
        assert check_schema_drift_advisory_batch([], fake_doe) == []

    def test_unreadable_doe_repo_is_indeterminate_for_every_entry(
        self, tmp_path: Path, vendored_dir: Path
    ) -> None:
        results = check_schema_drift_advisory_batch(
            [vendored_dir / _SCHEMA_A, vendored_dir / _SCHEMA_B], tmp_path / "not-a-repo"
        )
        assert all(r["determinate"] is False for r in results)
        assert all(r["diverged"] is False for r in results), (
            "unreadable must never be reported as drift"
        )


# ---------------------------------------------------------------------------
# Parallel `*.vendor.ts`-vs-opticon-HEAD comparison (C7, 2026-08-19) — the
# durability gap C1's parity check does not close on its own: this watch is
# what surfaces opticon editing `mintContributorId` out from under makima's
# transcribed derivation. Same verdict vocabulary, same non-gating negative-
# spec, glob-derived coverage set, deliberately mirrored test shape.
# ---------------------------------------------------------------------------

_VENDOR_SOURCE_NAME = "contributor-id.vendor.ts"
_OPTICON_SOURCE_REL_PATH = "src/lib/identity/contributor-id.ts"


def _vendor_source_body(marker: str) -> str:
    return (
        f"// opticon-source-path: {_OPTICON_SOURCE_REL_PATH}\n"
        f"// opticon-source-pin: deadbeef (test fixture)\n"
        f"export function mintContributorId(x) {{ return {marker!r}; }}\n"
    )


def _opticon_upstream_body(marker: str) -> str:
    return f"export function mintContributorId(x) {{ return {marker!r}; }}\n"


@pytest.fixture()
def fake_opticon(tmp_path: Path) -> Path:
    """A throwaway git repo shaped like an opticon clone: src/lib/identity/*.ts at HEAD."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "opticon-fake"
    source_dir = repo / "src" / "lib" / "identity"
    source_dir.mkdir(parents=True)
    (source_dir / "contributor-id.ts").write_text(_opticon_upstream_body("SAME"), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "drift watch test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed opticon source")
    return repo


@pytest.fixture()
def vendored_source_dir(tmp_path: Path) -> Path:
    """A throwaway vendored-source dir, matching fake_opticon's HEAD body by default."""
    directory = tmp_path / "vendored-source"
    directory.mkdir()
    (directory / _VENDOR_SOURCE_NAME).write_text(_vendor_source_body("SAME"), encoding="utf-8")
    return directory


class TestVendoredSourcePaths:
    """The opticon-source coverage set is globbed from disk, same posture as the schema one."""

    def test_globs_every_vendor_ts_file(self, vendored_source_dir: Path) -> None:
        names = [p.name for p in vendored_source_paths(vendored_source_dir)]
        assert names == [_VENDOR_SOURCE_NAME]

    def test_new_vendor_ts_is_covered_without_a_code_change(self, vendored_source_dir: Path) -> None:
        (vendored_source_dir / "brand-new.vendor.ts").write_text(
            _vendor_source_body("new"), encoding="utf-8"
        )
        names = [p.name for p in vendored_source_paths(vendored_source_dir)]
        assert "brand-new.vendor.ts" in names

    def test_missing_directory_returns_empty_not_raise(self, tmp_path: Path) -> None:
        assert vendored_source_paths(tmp_path / "nope") == []

    def test_ignores_non_vendor_ts_files(self, vendored_source_dir: Path) -> None:
        (vendored_source_dir / "README.md").write_text("not a vendor file\n", encoding="utf-8")
        names = [p.name for p in vendored_source_paths(vendored_source_dir)]
        assert "README.md" not in names


class TestSourceDriftAdvisory:
    """check_source_drift_advisory: the header-strip comparison, and its negative-spec."""

    def test_match_when_body_equals_opticon_head(
        self, fake_opticon: Path, vendored_source_dir: Path
    ) -> None:
        result = check_source_drift_advisory(
            vendored_source_dir / _VENDOR_SOURCE_NAME, fake_opticon
        )
        assert result["diverged"] is False
        assert result["determinate"] is True

    def test_drift_when_opticon_moves(self, fake_opticon: Path, vendored_source_dir: Path) -> None:
        (fake_opticon / "src" / "lib" / "identity" / "contributor-id.ts").write_text(
            _opticon_upstream_body("CHANGED"), encoding="utf-8"
        )
        _git(fake_opticon, "add", "-A")
        _git(fake_opticon, "commit", "-q", "-m", "opticon evolves mintContributorId")

        result = check_source_drift_advisory(
            vendored_source_dir / _VENDOR_SOURCE_NAME, fake_opticon
        )
        assert result["diverged"] is True
        assert result["determinate"] is True

    def test_unreadable_opticon_repo_is_indeterminate_never_match(
        self, tmp_path: Path, vendored_source_dir: Path
    ) -> None:
        result = check_source_drift_advisory(
            vendored_source_dir / _VENDOR_SOURCE_NAME, tmp_path / "not-a-repo"
        )
        assert result["diverged"] is False, "unreadable must never be reported as drift"
        assert result["determinate"] is False

    def test_missing_header_is_indeterminate(self, fake_opticon: Path, tmp_path: Path) -> None:
        headerless_dir = tmp_path / "headerless"
        headerless_dir.mkdir()
        headerless = headerless_dir / _VENDOR_SOURCE_NAME
        headerless.write_text(_opticon_upstream_body("SAME"), encoding="utf-8")

        result = check_source_drift_advisory(headerless, fake_opticon)

        assert result["diverged"] is False
        assert result["determinate"] is False

    def test_header_annotation_is_not_itself_drift(
        self, fake_opticon: Path, vendored_source_dir: Path
    ) -> None:
        """The makima-local header lines must not register as a divergence from
        opticon's own file, which never carries them."""
        result = check_source_drift_advisory(
            vendored_source_dir / _VENDOR_SOURCE_NAME, fake_opticon
        )
        assert result["diverged"] is False


class TestSourceDriftAdvisoryBatch:
    """check_source_drift_advisory_batch: the batched primary the per-file seam delegates to.

    The per-file form spawned twice PER FILE — a loop-invariant `foreign_repo_unusable_reason`
    probe plus a `git show HEAD:<path>` — so a vendored set of N cost 2N processes on a daily
    cadence surface. These pin the two properties that buy: the process count does not grow with
    N, and per-entry verdicts survive the batching (one unresolvable file must not blind the
    rest, and results must stay aligned to their inputs)."""

    @staticmethod
    def _multi(vendored_source_dir: Path, fake_opticon: Path) -> list[Path]:
        """Three vendored files against one opticon HEAD: match, drift, headerless."""
        drifted = vendored_source_dir / "drifted.vendor.ts"
        drifted.write_text(
            f"// opticon-source-path: {_OPTICON_SOURCE_REL_PATH}\n"
            f"// opticon-source-pin: deadbeef (test fixture)\n"
            f"export function mintContributorId(x) {{ return 'MOVED'; }}\n",
            encoding="utf-8",
        )
        headerless = vendored_source_dir / "headerless.vendor.ts"
        headerless.write_text(_opticon_upstream_body("SAME"), encoding="utf-8")
        return [vendored_source_dir / _VENDOR_SOURCE_NAME, drifted, headerless]

    def test_results_align_with_inputs_and_mix_verdicts(
        self, fake_opticon: Path, vendored_source_dir: Path
    ) -> None:
        paths = self._multi(vendored_source_dir, fake_opticon)
        results = check_source_drift_advisory_batch(paths, fake_opticon)

        assert [r["schema"] for r in results] == [p.name for p in paths]
        assert (results[0]["diverged"], results[0]["determinate"]) == (False, True)
        assert (results[1]["diverged"], results[1]["determinate"]) == (True, True)
        assert (results[2]["diverged"], results[2]["determinate"]) == (False, False), (
            "an unresolvable header must be indeterminate for THAT entry only"
        )

    def test_process_count_does_not_grow_with_the_set(
        self, fake_opticon: Path, vendored_source_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The amplification property itself, guarded here as well as by the repo-wide
        collector: comparing three files must cost the same spawns as comparing one.

        The probe memo is dropped before each measurement so this pins the BATCH
        property alone. Left in place the second call would be cheaper still (the
        repo probe is memoised per process) — a real saving, measured by
        `test_repeat_batch_reuses_the_memoised_repo_probe`, but not the property
        under test here."""
        spawns: list[list[str]] = []
        real_run = subprocess.run

        def counting_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
            spawns.append(list(argv))
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", counting_run)

        reset_foreign_repo_probe_memo()
        one = check_source_drift_advisory_batch(
            [vendored_source_dir / _VENDOR_SOURCE_NAME], fake_opticon
        )
        after_one = len(spawns)
        reset_foreign_repo_probe_memo()
        three = check_source_drift_advisory_batch(
            self._multi(vendored_source_dir, fake_opticon), fake_opticon
        )

        assert len(one) == 1 and len(three) == 3
        assert len(spawns) - after_one == after_one, (
            f"three files cost {len(spawns) - after_one} spawns against {after_one} for one: "
            f"{spawns[after_one:]}"
        )

    def test_empty_set_spawns_nothing(self, fake_opticon: Path) -> None:
        assert check_source_drift_advisory_batch([], fake_opticon) == []

    def test_repeat_batch_reuses_the_memoised_repo_probe(
        self, fake_opticon: Path, vendored_source_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second batch against the same clone costs ONE spawn, not two.

        `git_scope.foreign_repo_unusable_reason` is memoised per (realpath, pid), so
        the loop-invariant repo probe is paid once per process however many surfaces
        ask. Without it the same clone is re-probed from scratch by every caller.
        """
        spawns: list[list[str]] = []
        real_run = subprocess.run

        def counting_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
            spawns.append(list(argv))
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", counting_run)

        paths = [vendored_source_dir / _VENDOR_SOURCE_NAME]
        reset_foreign_repo_probe_memo()
        check_source_drift_advisory_batch(paths, fake_opticon)
        cold = len(spawns)
        check_source_drift_advisory_batch(paths, fake_opticon)

        assert cold == 2, f"cold batch should be probe + cat-file: {spawns[:cold]}"
        assert len(spawns) - cold == 1, (
            f"warm batch re-probed the clone instead of reusing the memo: {spawns[cold:]}"
        )

    def test_unreadable_opticon_repo_is_indeterminate_for_every_entry(
        self, tmp_path: Path, vendored_source_dir: Path, fake_opticon: Path
    ) -> None:
        results = check_source_drift_advisory_batch(
            self._multi(vendored_source_dir, fake_opticon), tmp_path / "not-a-repo"
        )
        assert all(r["determinate"] is False for r in results)
        assert all(r["diverged"] is False for r in results), (
            "unreadable must never be reported as drift"
        )


class TestOpticonRepoPathResolution:
    """resolve_opticon_repo_path ladder: REPO_PROJECT_OPTICON, then registry."""

    def test_no_rungs_resolve_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("REPO_PROJECT_OPTICON", raising=False)
        monkeypatch.setattr(
            "coordinator_core.frontmatter.schema_drift_watch.registry_get",
            lambda key: None,
        )
        assert resolve_opticon_repo_path() is None

    def test_env_override_wins_when_valid(
        self, fake_opticon: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REPO_PROJECT_OPTICON", str(fake_opticon))
        assert resolve_opticon_repo_path() == fake_opticon

    def test_bogus_env_override_does_not_win(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REPO_PROJECT_OPTICON", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr(
            "coordinator_core.frontmatter.schema_drift_watch.registry_get",
            lambda key: None,
        )
        assert resolve_opticon_repo_path() is None


class TestAggregateIncludesOpticonSource:
    """scan_vendored_schema_drift's aggregate verdict includes the opticon-source row."""

    def test_source_match_folds_into_overall_match(
        self, fake_doe: Path, fake_opticon: Path, tmp_path: Path
    ) -> None:
        directory = tmp_path / "combined"
        directory.mkdir()
        for name in (_SCHEMA_A, _SCHEMA_B):
            (directory / name).write_text(_schema_body(name), encoding="utf-8")
        (directory / _VENDOR_SOURCE_NAME).write_text(_vendor_source_body("SAME"), encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, directory, fake_opticon)

        assert report["status"] == STATUS_MATCH
        assert report["checked"] == 3
        assert _VENDOR_SOURCE_NAME in report["matched"]
        assert report["opticon_repo_path"] == str(fake_opticon)

    def test_opticon_drift_surfaces_even_when_doe_side_matches(
        self, fake_doe: Path, fake_opticon: Path, tmp_path: Path
    ) -> None:
        directory = tmp_path / "combined"
        directory.mkdir()
        for name in (_SCHEMA_A, _SCHEMA_B):
            (directory / name).write_text(_schema_body(name), encoding="utf-8")
        (directory / _VENDOR_SOURCE_NAME).write_text(_vendor_source_body("SAME"), encoding="utf-8")

        (fake_opticon / "src" / "lib" / "identity" / "contributor-id.ts").write_text(
            _opticon_upstream_body("CHANGED"), encoding="utf-8"
        )
        _git(fake_opticon, "add", "-A")
        _git(fake_opticon, "commit", "-q", "-m", "opticon moves mintContributorId")

        report = scan_vendored_schema_drift(fake_doe, directory, fake_opticon)

        assert report["status"] == STATUS_DRIFT
        assert [d["schema"] for d in report["drifted"]] == [_VENDOR_SOURCE_NAME]
        assert sorted(report["matched"]) == sorted([_SCHEMA_A, _SCHEMA_B])

    def test_opticon_unresolved_is_indeterminate_not_masked(
        self, fake_doe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = tmp_path / "combined"
        directory.mkdir()
        for name in (_SCHEMA_A, _SCHEMA_B):
            (directory / name).write_text(_schema_body(name), encoding="utf-8")
        (directory / _VENDOR_SOURCE_NAME).write_text(_vendor_source_body("SAME"), encoding="utf-8")

        report = scan_vendored_schema_drift(fake_doe, directory, tmp_path / "no-opticon-here")

        assert report["status"] == STATUS_INDETERMINATE
        assert [d["schema"] for d in report["indeterminate"]] == [_VENDOR_SOURCE_NAME]
        assert sorted(report["matched"]) == sorted([_SCHEMA_A, _SCHEMA_B])
