"""
coordinator_core.ops.session.tests.test_guard_settings_integrity_restore_rungs

Tests for the third restore rung added to `evaluate_settings_integrity`
(2026-07-31) — a known-good backup under the resolved settings home
(`coordinator_core._settings_home.settings_home()`), and the fall-through
restructure of the three-rung ladder as a whole.

Fresh-clone gap this closes: on a fresh clone of the `~/.claude` repo,
neither pre-existing rung resolves — `settings.json` is untracked there
(machine-local, cannot be synced), so `git show HEAD:./settings.json` fails,
and `.settings-last-good.json` is gitignored, so a fresh clone carries no
snapshot either. This module pins that the new rung recovers in exactly
that shape.

Coverage:
  - fresh-clone shape (no snapshot, untracked settings.json, no git-trackable
    settings.json) recovers via the new rung
  - newest-of-several known-good backups wins (sortable timestamp suffix,
    not mtime)
  - an unhealthy backup is skipped in favour of a healthy older one
  - no backups at all still yields `_BANNER_NO_RESTORE_SOURCE`
  - rung precedence: snapshot beats the new rung; git HEAD beats the new rung
  - fall-through: a snapshot that passes `_is_healthy` but fails at the
    actual `_atomic_copy` now falls through to git HEAD, rather than
    silently landing on `_BANNER_NO_RESTORE_SOURCE` (the pre-2026-07-31
    else-bound-on-precondition defect named in the dispatch brief)

Negative-spec: does NOT re-test the reconciliation lens (declared-true-but-
unreachable plugins) — that lives in test_guard_settings_integrity.py and is
unaffected by this addition.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.session import guard_settings_integrity as _gsi
from coordinator_core.ops.session.guard_settings_integrity import (
    evaluate_settings_integrity,
)


@pytest.fixture(autouse=True)
def _hook_layer_always_reachable(monkeypatch):
    """This module exercises the restore ladder, a concern orthogonal to
    hook-layer reachability (`_is_healthy`'s 2026-07-28 conjunct). None of
    this module's fixtures set up a resolvable coordinator content root or a
    settings-side `hooks` block, so force the conjunct reachable — matching
    the same override `test_guard_settings_integrity.py` uses for the same
    reason."""
    monkeypatch.setattr(_gsi, "_hook_layer_reachable", lambda settings_data: True)


_HEALTHY = {"enabledPlugins": {"foo@bar": True}}
_UNHEALTHY = {"not-enabled-plugins": "stub"}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _settings_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


def test_fresh_clone_recovers_via_known_good_backup(tmp_path, monkeypatch):
    """No snapshot (gitignored on a fresh clone), settings.json untracked
    (not even a git repo here), but a known-good backup exists under the
    settings home -> the new rung recovers it."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)

    home = _settings_home(tmp_path, monkeypatch)
    backup = home / "settings.json.known-good-20260728T211900"
    _write_json(backup, _HEALTHY)

    text = evaluate_settings_integrity(config_dir)

    assert "AUTO-RESTORED from known-good backup" in text
    assert backup.name in text
    restored = json.loads((config_dir / "settings.json").read_text())
    assert restored == _HEALTHY


def test_newest_backup_wins(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)

    home = _settings_home(tmp_path, monkeypatch)
    older = home / "settings.json.known-good-20260701T000000"
    newer = home / "settings.json.known-good-20260728T211900"
    _write_json(older, {"enabledPlugins": {"old@one": True}})
    _write_json(newer, {"enabledPlugins": {"new@one": True}})

    evaluate_settings_integrity(config_dir)

    restored = json.loads((config_dir / "settings.json").read_text())
    assert restored == {"enabledPlugins": {"new@one": True}}


def test_unhealthy_backup_skipped_for_healthy_older(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)

    home = _settings_home(tmp_path, monkeypatch)
    older_healthy = home / "settings.json.known-good-20260701T000000"
    newer_unhealthy = home / "settings.json.known-good-20260728T211900"
    _write_json(older_healthy, {"enabledPlugins": {"old@one": True}})
    _write_json(newer_unhealthy, _UNHEALTHY)

    text = evaluate_settings_integrity(config_dir)

    assert older_healthy.name in text
    restored = json.loads((config_dir / "settings.json").read_text())
    assert restored == {"enabledPlugins": {"old@one": True}}


def test_no_backups_at_all_yields_no_restore_source_banner(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)
    _settings_home(tmp_path, monkeypatch)  # empty settings home, no backups

    text = evaluate_settings_integrity(config_dir)

    assert text == _gsi._BANNER_NO_RESTORE_SOURCE


def test_snapshot_beats_known_good_backup(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)
    _write_json(
        config_dir / _gsi._SNAPSHOT_NAME,
        {"enabledPlugins": {"snap@one": True}},
    )

    home = _settings_home(tmp_path, monkeypatch)
    _write_json(
        home / "settings.json.known-good-20260728T211900",
        {"enabledPlugins": {"backup@one": True}},
    )

    text = evaluate_settings_integrity(config_dir)

    assert "snapshot (.settings-last-good.json)" in text
    restored = json.loads((config_dir / "settings.json").read_text())
    assert restored == {"enabledPlugins": {"snap@one": True}}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_git_head_beats_known_good_backup(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_path = config_dir / "settings.json"
    _write_json(settings_path, {"enabledPlugins": {"head@one": True}})

    _git("init", cwd=config_dir)
    _git("add", "settings.json", cwd=config_dir)
    _git("commit", "-m", "healthy settings", cwd=config_dir)

    # Clobber on disk after the healthy commit; no snapshot present.
    _write_json(settings_path, _UNHEALTHY)

    home = _settings_home(tmp_path, monkeypatch)
    _write_json(
        home / "settings.json.known-good-20260728T211900",
        {"enabledPlugins": {"backup@one": True}},
    )

    text = evaluate_settings_integrity(config_dir)

    assert "git HEAD" in text
    restored = json.loads(settings_path.read_text())
    assert restored == {"enabledPlugins": {"head@one": True}}


def test_snapshot_copy_failure_falls_through_to_git(tmp_path, monkeypatch):
    """Pins the fall-through restructure: a snapshot that itself passes
    `_is_healthy` but fails at the actual `_atomic_copy` write no longer
    strands the guard on `_BANNER_NO_RESTORE_SOURCE` -- it now falls through
    to try git HEAD (rung 2), which restores instead."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_path = config_dir / "settings.json"
    _write_json(settings_path, {"enabledPlugins": {"head@one": True}})

    _git("init", cwd=config_dir)
    _git("add", "settings.json", cwd=config_dir)
    _git("commit", "-m", "healthy settings", cwd=config_dir)

    _write_json(settings_path, _UNHEALTHY)
    _write_json(
        config_dir / _gsi._SNAPSHOT_NAME,
        {"enabledPlugins": {"snap@one": True}},
    )

    real_atomic_copy = _gsi._atomic_copy

    def _fail_only_for_snapshot(src, dst):
        if src == config_dir / _gsi._SNAPSHOT_NAME:
            return False
        return real_atomic_copy(src, dst)

    monkeypatch.setattr(_gsi, "_atomic_copy", _fail_only_for_snapshot)

    text = evaluate_settings_integrity(config_dir)

    assert "git HEAD" in text
    restored = json.loads(settings_path.read_text())
    assert restored == {"enabledPlugins": {"head@one": True}}


def test_settings_home_runtime_error_degrades_to_no_rung3_candidate(tmp_path, monkeypatch):
    """Review: code-reviewer (Finding 2) regression. `settings_home()`
    reaches `Path.home()`, which is documented to raise `RuntimeError` (not
    `ValueError`/`OSError`) when no home directory resolves at all (no
    HOME/USERPROFILE, no resolvable passwd entry). `_find_known_good_backup`
    must degrade to "no rung-3 candidate" rather than let the RuntimeError
    propagate past `evaluate_settings_integrity`, which has no outer
    try/except and relies on every rung individually never raising."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)
    # No snapshot, no git repo -- rungs 1/2 both fall through, forcing rung 3.

    def _raise_runtime_error():
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(_gsi, "settings_home", _raise_runtime_error)

    text = evaluate_settings_integrity(config_dir)

    assert text == _gsi._BANNER_NO_RESTORE_SOURCE


def test_malformed_known_good_suffixes_excluded_from_candidates(tmp_path, monkeypatch):
    """Review: code-reviewer (Finding 4) regression. `_known_good_backup_
    candidates` must silently exclude filenames that don't match
    `_KNOWN_GOOD_BACKUP_RE` (non-timestamp or dashed-date suffixes) rather
    than crashing the sort or letting them interfere with ordering against
    well-formed candidates."""
    home = tmp_path / "settings-home"
    home.mkdir()

    well_formed_older = home / "settings.json.known-good-20260701T000000"
    well_formed_newer = home / "settings.json.known-good-20260728T211900"
    malformed_non_timestamp = home / "settings.json.known-good-notatimestamp"
    malformed_dashed_date = home / "settings.json.known-good-2026-07-28"

    for path in (
        well_formed_older,
        well_formed_newer,
        malformed_non_timestamp,
        malformed_dashed_date,
    ):
        path.write_text("{}", encoding="utf-8")

    candidates = _gsi._known_good_backup_candidates(home)

    assert candidates == [well_formed_newer, well_formed_older]
    assert malformed_non_timestamp not in candidates
    assert malformed_dashed_date not in candidates


def test_ambient_settings_home_unrelated_to_config_dir_is_not_trusted(tmp_path, monkeypatch):
    """Scope-escape regression (2026-08-01). Pre-fix, `_find_known_good_backup`
    took no `config_dir` parameter and resolved `settings_home()` in complete
    isolation from whichever `config_dir` the caller was evaluating --
    rung 3's verdict depended on ambient machine state OUTSIDE the scoped
    root. Here `config_dir` is an unrelated tmp tree, `COORDINATOR_SETTINGS_HOME`
    is unset, and `settings_home()` is monkeypatched to a SEPARATE 'ambient
    machine home' that happens to carry a healthy backup -- mirroring the
    real example-doctrine-repo failure where only `CLAUDE_CONFIG_DIR` was set and the
    host's real `~/.coordinator-claude-settings` leaked in. Must NOT restore
    from it: rung 3 should report no candidate, same as an empty settings
    home. Fails against the pre-fix code (which restores)."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)

    ambient_home = tmp_path / "unrelated-real-home"
    ambient_settings_home = ambient_home / ".coordinator-claude-settings"
    ambient_settings_home.mkdir(parents=True)
    backup = ambient_settings_home / "settings.json.known-good-20260728T211900"
    _write_json(backup, _HEALTHY)

    monkeypatch.setattr(_gsi, "settings_home", lambda: ambient_settings_home)

    text = evaluate_settings_integrity(config_dir)

    assert text == _gsi._BANNER_NO_RESTORE_SOURCE
    assert json.loads((config_dir / "settings.json").read_text()) == _UNHEALTHY


def test_fresh_clone_gap_recovers_without_settings_home_override(tmp_path, monkeypatch):
    """The fresh-clone gap rung 3 exists for must still resolve WITHOUT an
    explicit `COORDINATOR_SETTINGS_HOME` override -- the common case, since
    the override is opt-in. Here `config_dir` is genuinely shaped like
    `<home>/.claude`, so its sibling settings home
    (`<home>/.coordinator-claude-settings`) is trusted and searched."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    config_dir = home / ".claude"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)

    settings_home_dir = home / ".coordinator-claude-settings"
    settings_home_dir.mkdir()
    backup = settings_home_dir / "settings.json.known-good-20260728T211900"
    _write_json(backup, _HEALTHY)

    monkeypatch.setattr(_gsi, "settings_home", lambda: settings_home_dir)

    text = evaluate_settings_integrity(config_dir)

    assert "AUTO-RESTORED from known-good backup" in text
    restored = json.loads((config_dir / "settings.json").read_text())
    assert restored == _HEALTHY


def test_explicit_override_pointed_at_unrelated_root_still_untrusted(tmp_path, monkeypatch):
    """Review residual (2026-08-01): an explicit `COORDINATOR_SETTINGS_HOME`
    override decides WHICH directory resolves as the settings home -- it
    does NOT, by itself, license restoring into an arbitrary `config_dir`
    the override's own placement says nothing about. Here the override is
    genuinely set (not monkeypatched away) and points at a real, populated
    settings home, but that home lives under an entirely different root
    than `config_dir` -- not a sibling -- so rung 3 must stay silent even
    though the override IS explicit and a healthy backup does exist. This
    is the regression guard for the earlier draft of this fix, which
    trusted any explicit override unconditionally regardless of
    `config_dir` and reintroduced the same ambient-host-state dependency
    the fix exists to close."""
    config_dir = tmp_path / "some" / "unrelated" / "config"
    config_dir.mkdir(parents=True)
    _write_json(config_dir / "settings.json", _UNHEALTHY)

    unrelated_root = tmp_path / "elsewhere"
    settings_home_dir = unrelated_root / "settings-home"
    settings_home_dir.mkdir(parents=True)
    backup = settings_home_dir / "settings.json.known-good-20260728T211900"
    _write_json(backup, _HEALTHY)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))

    text = evaluate_settings_integrity(config_dir)

    assert text == _gsi._BANNER_NO_RESTORE_SOURCE
    assert json.loads((config_dir / "settings.json").read_text()) == _UNHEALTHY


def test_known_good_backup_restore_banner_labels_rung3_provenance(tmp_path, monkeypatch):
    """Review: code-reviewer (Finding 3). A rung-3 restore's banner must
    visibly label the source as an operator-placed, unauthenticated file --
    distinct from rungs 1/2's identically-worded restore banner -- so an
    operator can tell which rung fired from the banner alone."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_json(config_dir / "settings.json", _UNHEALTHY)

    home = _settings_home(tmp_path, monkeypatch)
    backup = home / "settings.json.known-good-20260728T211900"
    _write_json(backup, _HEALTHY)

    text = evaluate_settings_integrity(config_dir)

    assert "operator-placed" in text.lower()
