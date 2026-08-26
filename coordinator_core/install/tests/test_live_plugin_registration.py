"""Plain `claude` must resolve the live clone, and keep resolving it.

The defect these pin: `claude plugin install` copies a directory-source plugin
into the plugin cache and pins a `gitCommitSha`, so plain `claude` serves a
frozen snapshot while `claude-doe` (which injects `--plugin-dir`) serves the
clone. Two sessions, two different coordinator surfaces, no warning.

Negative-spec: none of these assert the displaced cache copy is deleted. It is
inert once nothing points at it, and removing a user's bytes for tidiness is
not this module's business -- `test_displaced_cache_copy_is_reported_not_removed`
pins that deliberately.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.install.live_plugin_registration import (
    STATUS_ABSENT,
    STATUS_ALREADY_LIVE,
    STATUS_NO_ENTRY,
    STATUS_REPOINTED,
    STATUS_UNREADABLE,
    assert_live_plugin_registration,
    read_plugin_name,
)


def _clone(tmp_path: Path, name: str = "coordinator") -> Path:
    root = tmp_path / "clone" / "coordinator"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "4.0.0"}), encoding="utf-8"
    )
    return root


def _claude_home(tmp_path: Path, records: dict | None) -> Path:
    home = tmp_path / "claude-home"
    (home / "plugins").mkdir(parents=True)
    if records is not None:
        (home / "plugins" / "installed_plugins.json").write_text(
            json.dumps(records), encoding="utf-8"
        )
    return home


def _cached_record(home: Path, *, sha: str | None = "956fd5a8", name: str = "coordinator") -> dict:
    entry = {
        "scope": "user",
        "installPath": str(home / "plugins" / "cache" / "coordinator-claude" / name / "4.0.0"),
        "version": "4.0.0",
    }
    if sha:
        entry["gitCommitSha"] = sha
    return {"version": 2, "plugins": {f"{name}@coordinator-claude": [entry]}}


def _read(home: Path) -> dict:
    return json.loads((home / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))


def test_a_cached_record_is_repointed_at_the_clone(tmp_path):
    clone = _clone(tmp_path)
    home = _claude_home(tmp_path, None)
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps(_cached_record(home)), encoding="utf-8"
    )

    report = assert_live_plugin_registration(home, clone)

    assert report["status"] == STATUS_REPOINTED
    entry = _read(home)["plugins"]["coordinator@coordinator-claude"][0]
    assert entry["installPath"] == str(clone)


def test_the_pinned_sha_is_dropped_rather_than_updated(tmp_path):
    """A SHA beside a live path is a false witness: it names a commit the
    session is not running. Nothing is pinned, so nothing should claim to be."""
    clone = _clone(tmp_path)
    home = _claude_home(tmp_path, None)
    home.joinpath("plugins", "installed_plugins.json").write_text(
        json.dumps(_cached_record(home, sha="deadbeef")), encoding="utf-8"
    )

    assert_live_plugin_registration(home, clone)

    entry = _read(home)["plugins"]["coordinator@coordinator-claude"][0]
    assert "gitCommitSha" not in entry


def test_re_asserting_an_already_live_record_changes_nothing(tmp_path):
    """Idempotence is the whole design -- this runs on every install."""
    clone = _clone(tmp_path)
    home = _claude_home(
        tmp_path,
        {"version": 2, "plugins": {"coordinator@coordinator-claude": [
            {"scope": "user", "installPath": str(clone), "version": "4.0.0"}
        ]}},
    )
    before = (home / "plugins" / "installed_plugins.json").read_bytes()

    report = assert_live_plugin_registration(home, clone)

    assert report["status"] == STATUS_ALREADY_LIVE
    assert (home / "plugins" / "installed_plugins.json").read_bytes() == before


def test_a_reinstall_that_restored_a_copy_is_caught_on_the_next_run(tmp_path):
    """The failure mode this phase exists for: `claude plugin install` rewrites
    the record back to a fresh copy and nothing warns."""
    clone = _clone(tmp_path)
    home = _claude_home(tmp_path, None)
    record_file = home / "plugins" / "installed_plugins.json"
    record_file.write_text(json.dumps(_cached_record(home)), encoding="utf-8")
    assert_live_plugin_registration(home, clone)

    record_file.write_text(json.dumps(_cached_record(home, sha="newpin")), encoding="utf-8")
    report = assert_live_plugin_registration(home, clone)

    assert report["status"] == STATUS_REPOINTED
    assert _read(home)["plugins"]["coordinator@coordinator-claude"][0]["installPath"] == str(clone)


def test_displaced_cache_copy_is_reported_not_removed(tmp_path):
    clone = _clone(tmp_path)
    home = _claude_home(tmp_path, None)
    record = _cached_record(home)
    copy_dir = Path(record["plugins"]["coordinator@coordinator-claude"][0]["installPath"])
    copy_dir.mkdir(parents=True)
    (copy_dir / "marker").write_text("x", encoding="utf-8")
    (home / "plugins" / "installed_plugins.json").write_text(json.dumps(record), encoding="utf-8")

    report = assert_live_plugin_registration(home, clone)

    assert report["entries"][0]["displaced_copy"] == str(copy_dir)
    assert (copy_dir / "marker").is_file()


def test_an_install_path_outside_the_cache_is_not_called_cache_residue(tmp_path):
    """Someone's deliberate elsewhere-path is repointed, but never reported as
    a copy we made."""
    clone = _clone(tmp_path)
    elsewhere = tmp_path / "somewhere" / "coordinator"
    home = _claude_home(
        tmp_path,
        {"version": 2, "plugins": {"coordinator@coordinator-claude": [
            {"scope": "user", "installPath": str(elsewhere), "version": "4.0.0"}
        ]}},
    )

    report = assert_live_plugin_registration(home, clone)

    assert report["entries"][0]["displaced_copy"] is None


def test_every_scope_of_the_same_plugin_is_repointed(tmp_path):
    """The record holds one list per key -- a user-scope entry repointed while a
    project-scope entry still names a copy is the split this phase closes."""
    clone = _clone(tmp_path)
    home = _claude_home(tmp_path, None)
    cache = home / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "4.0.0"
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"coordinator@coordinator-claude": [
            {"scope": "user", "installPath": str(cache), "gitCommitSha": "a"},
            {"scope": "project", "projectPath": "/x", "installPath": str(cache), "gitCommitSha": "b"},
        ]}}),
        encoding="utf-8",
    )

    report = assert_live_plugin_registration(home, clone)

    assert len(report["entries"]) == 2
    assert all(r["installPath"] == str(clone)
               for r in _read(home)["plugins"]["coordinator@coordinator-claude"])


def test_another_plugins_record_is_never_touched(tmp_path):
    clone = _clone(tmp_path)
    home = _claude_home(
        tmp_path,
        {"version": 2, "plugins": {
            "coordinator@coordinator-claude": [{"scope": "user", "installPath": "/old"}],
            "example-game-repo@example-game-workbench-repo": [
                {"scope": "user", "installPath": "/cache/example-game-repo", "gitCommitSha": "keepme"}
            ],
        }},
    )

    assert_live_plugin_registration(home, clone)

    other = _read(home)["plugins"]["example-game-repo@example-game-workbench-repo"][0]
    assert other == {"scope": "user", "installPath": "/cache/example-game-repo", "gitCommitSha": "keepme"}


def test_dry_run_reports_without_writing(tmp_path):
    clone = _clone(tmp_path)
    home = _claude_home(tmp_path, None)
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps(_cached_record(home)), encoding="utf-8"
    )
    before = (home / "plugins" / "installed_plugins.json").read_bytes()

    report = assert_live_plugin_registration(home, clone, dry_run=True)

    assert report["status"] == STATUS_REPOINTED
    assert (home / "plugins" / "installed_plugins.json").read_bytes() == before


def test_a_box_with_no_record_file_is_an_ordinary_outcome(tmp_path):
    report = assert_live_plugin_registration(_claude_home(tmp_path, None), _clone(tmp_path))
    assert report["status"] == STATUS_ABSENT


def test_an_unreadable_record_degrades_rather_than_raising(tmp_path):
    home = _claude_home(tmp_path, None)
    (home / "plugins" / "installed_plugins.json").write_text("{not json", encoding="utf-8")
    report = assert_live_plugin_registration(home, _clone(tmp_path))
    assert report["status"] == STATUS_UNREADABLE


def test_the_plugin_name_is_read_from_the_clone_never_hardcoded(tmp_path):
    clone = _clone(tmp_path, name="renamed-plugin")
    assert read_plugin_name(clone) == "renamed-plugin"

    home = _claude_home(
        tmp_path,
        {"version": 2, "plugins": {"renamed-plugin@coordinator-claude": [
            {"scope": "user", "installPath": "/cache/x", "gitCommitSha": "s"}
        ]}},
    )
    report = assert_live_plugin_registration(home, clone)
    assert report["status"] == STATUS_REPOINTED


def test_a_clone_without_a_plugin_manifest_asserts_nothing(tmp_path):
    home = _claude_home(tmp_path, {"version": 2, "plugins": {}})
    report = assert_live_plugin_registration(home, tmp_path / "not-a-plugin")
    assert report["status"] == STATUS_NO_ENTRY
