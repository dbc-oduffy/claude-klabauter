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
    resolve_doe_repo_path,
    scan_vendored_schema_drift,
    vendored_schema_paths,
)

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
        """cross-repo-memo.schema.json / archived-memo.schema.json are claude-klabauter-
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
            "coordinator_core.frontmatter.schema_validate", fromlist=["check_schema_drift_advisory"]
        ).check_schema_drift_advisory

        def _advisory_without_direction(schema_path, doe_repo_path):
            result = dict(real_advisory(schema_path, doe_repo_path))
            result.pop("direction", None)
            return result

        monkeypatch.setattr(
            "coordinator_core.frontmatter.schema_drift_watch.check_schema_drift_advisory",
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
