"""test_publish_provenance_record — direct unit coverage for
`write_publish_provenance_record`/`_publish_provenance_record_path`, added by
docs/plans/2026-08-19-the-published-engine-says-what-it-was-published-from.md
(C1). `_round_pin_source_sha` already resolves and prints the round-pinned
sha per contributing git toplevel; nothing durable survived the round, so no
consumer could later answer "which revision of claude-klabauter is actually running in
the published build?" (the DR-326 stale-dispatch incident the plan
documents). This is the durable record that closes that gap.

Run: python -m pytest coordinator/bin/tests/test_publish_provenance_record.py -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Spawns real git subprocesses; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_provenance_record_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(*args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit_all(path: Path, message: str) -> str:
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _make_target(name: str, dest_dir: Path, source_dir: Path, *, mode: str = "mirror"):
    return publish.ResolvedTarget(
        name=name,
        mode=mode,
        source_dir=source_dir,
        dest_dir=dest_dir,
    )


def _settings_home(monkeypatch, tmp_path) -> Path:
    settings_home = tmp_path / "settings_home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    return settings_home


def test_publish_provenance_record_path_resolves_under_settings_home(monkeypatch, tmp_path):
    settings_home = _settings_home(monkeypatch, tmp_path)
    path = publish._publish_provenance_record_path()
    assert path == settings_home / "machine-local" / "publish-provenance.json"


def test_successful_round_writes_record_with_pinned_sha(monkeypatch, tmp_path):
    """AC1: after a round, the record exists and its sha equals the
    `Round source pinned` sha for that toplevel."""
    settings_home = _settings_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    head = _commit_all(repo, "v1")

    target = _make_target("sample", tmp_path / "dest", repo)
    pinned: "dict[str, str]" = {}
    publish._round_pin_source_sha(repo, pinned, out=io.StringIO(), late=False)

    publish.write_publish_provenance_record(
        succeeded_row_names=["sample"],
        failed_row_names=[],
        skipped_row_names=[],
        rows_by_name={"sample": target},
        round_pinned_shas=pinned,
    )

    record_path = settings_home / "machine-local" / "publish-provenance.json"
    assert record_path.is_file()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    row = record["rows"]["sample"]
    assert row["published"] is True
    assert row["toplevels"][str(repo)] == head
    assert "completed_at" in record


def test_failed_row_contributes_no_published_from_claim(monkeypatch, tmp_path):
    """AC2/Anti-scope 3: a row that failed must be recorded as not-published,
    with no sha presented as live for it."""
    settings_home = _settings_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    _commit_all(repo, "v1")

    ok_target = _make_target("ok-row", tmp_path / "dest-ok", repo)
    pinned: "dict[str, str]" = {}
    publish._round_pin_source_sha(repo, pinned, out=io.StringIO(), late=False)

    publish.write_publish_provenance_record(
        succeeded_row_names=["ok-row"],
        failed_row_names=["broken-row"],
        skipped_row_names=[],
        rows_by_name={"ok-row": ok_target},
        round_pinned_shas=pinned,
    )

    record_path = settings_home / "machine-local" / "publish-provenance.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["rows"]["ok-row"]["published"] is True
    broken = record["rows"]["broken-row"]
    assert broken["published"] is False
    assert "toplevels" not in broken
    assert "sha" not in broken


def test_skipped_row_contributes_no_published_from_claim(monkeypatch, tmp_path):
    settings_home = _settings_home(monkeypatch, tmp_path)
    publish.write_publish_provenance_record(
        succeeded_row_names=[],
        failed_row_names=[],
        skipped_row_names=["skipped-row"],
        rows_by_name={},
        round_pinned_shas={},
    )
    record_path = settings_home / "machine-local" / "publish-provenance.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    # The obligation is that a skipped row asserts NO published-from claim —
    # not that the entry has exactly one key. `last_attempt_at` was added
    # alongside merge-forward and says when the row was last reached, which
    # is the opposite of a published-from claim.
    entry = record["rows"]["skipped-row"]
    assert entry["published"] is False
    assert "toplevels" not in entry
    assert "sha" not in entry
    assert "last_published" not in entry


def test_a_row_absent_from_this_round_keeps_its_last_published_sha(monkeypatch, tmp_path):
    """The merge-forward obligation. A row the round did not reach used to
    disappear from the record, which reads identically to a row that has
    never published — so a mirror lagging source raised nothing anywhere
    (found 2026-08-26 by hand-diffing two mirrors)."""
    settings_home = _settings_home(monkeypatch, tmp_path)
    record_path = settings_home / "machine-local" / "publish-provenance.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "completed_at": "2026-08-26T13:19:02+00:00",
                "rows": {
                    "absent-next-round": {
                        "published": True,
                        "toplevels": {"X:\repo": "a" * 40},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    publish.write_publish_provenance_record(
        succeeded_row_names=[],
        failed_row_names=[],
        skipped_row_names=["some-other-row"],
        rows_by_name={},
        round_pinned_shas={},
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    carried = record["rows"]["absent-next-round"]
    assert carried["published"] is True
    assert carried["toplevels"] == {"X:\repo": "a" * 40}
    assert record["rows_in_last_round"] == ["some-other-row"]


def test_a_row_that_fails_after_publishing_keeps_the_earlier_success(monkeypatch, tmp_path):
    """Anti-scope 3 forbids asserting a sha for THIS round's outcome. It does
    not require forgetting that the row ever published — forgetting is what
    made the lag invisible."""
    settings_home = _settings_home(monkeypatch, tmp_path)
    record_path = settings_home / "machine-local" / "publish-provenance.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "completed_at": "2026-08-26T13:19:02+00:00",
                "rows": {
                    "row": {
                        "published": True,
                        "toplevels": {"X:\repo": "b" * 40},
                        "published_at": "2026-08-26T13:19:02+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    publish.write_publish_provenance_record(
        succeeded_row_names=[],
        failed_row_names=["row"],
        skipped_row_names=[],
        rows_by_name={},
        round_pinned_shas={},
    )
    entry = json.loads(record_path.read_text(encoding="utf-8"))["rows"]["row"]
    assert entry["published"] is False
    assert "toplevels" not in entry
    assert entry["last_published"]["toplevels"] == {"X:\repo": "b" * 40}
    assert entry["last_published"]["at"] == "2026-08-26T13:19:02+00:00"


def test_unwritable_target_warns_without_raising(monkeypatch, tmp_path):
    """Failure posture: an unwritable path warns to stderr and leaves the
    caller's control flow (and therefore the round's exit code) untouched —
    this function must never raise out to `main`."""
    settings_home = _settings_home(monkeypatch, tmp_path)

    def _boom():
        raise OSError("simulated unwritable machine-local path")

    monkeypatch.setattr(publish, "_publish_provenance_record_path", _boom)

    err = io.StringIO()
    # Must not raise.
    publish.write_publish_provenance_record(
        succeeded_row_names=[],
        failed_row_names=[],
        skipped_row_names=[],
        rows_by_name={},
        round_pinned_shas={},
        err=err,
    )
    assert "WARNING" in err.getvalue()
    assert not (settings_home / "machine-local" / "publish-provenance.json").exists()


def test_does_not_touch_delta_record_helpers(monkeypatch, tmp_path):
    """Anti-scope 1 — a smoke check that writing the provenance record never
    calls into the delta-record optimization cache."""
    _settings_home(monkeypatch, tmp_path)
    for fn_name in (
        "write_delta_record",
        "_delta_state_path",
        "delta_row_unchanged",
        "_delta_row_source_sha",
    ):

        def _fail(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError(f"{fn_name} must not be called by write_publish_provenance_record")

        monkeypatch.setattr(publish, fn_name, _fail)

    publish.write_publish_provenance_record(
        succeeded_row_names=[],
        failed_row_names=[],
        skipped_row_names=["x"],
        rows_by_name={},
        round_pinned_shas={},
    )
