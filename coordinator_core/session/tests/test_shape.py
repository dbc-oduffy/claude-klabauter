"""
coordinator_core.session.tests.test_shape — parity tests for
coordinator_core.session.shape.

Port of: session-shape.sh (example-doctrine-repo e34f2484, 2026-07-22).

Two strategies, per the recipe:
  (a) boundary-matrix transcription of the bash oracle's contracts
      (§ _cs_shape_lock_live, § cs_session_shape_set,
      § cs_session_shape_read, § cs_session_shape_magnitude) into named
      methods, in tmp_path git repos with monkeypatch isolation;
  (b) golden-diff against reality — session_shape_magnitude is exercised
      against a REAL tmp git repo with actual commits so commits_since_start
      is checked against `git rev-list` ground truth.

Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § shape.py
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core, shape

# Every test in this file builds its repo via `_make_repo(tmp_path)`, spawning
# real git (init/config/add/commit) because the production code under test --
# `core.git_root()` -- reads real git state that no mock stands in for.
# `tmp_path` is function-scoped and tests write shape-lock state under it, so
# the repo fixture stays per-test rather than hoisted to module scope. The
# spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _sdir(repo, sid):
    d = Path(repo) / ".git" / "coordinator-sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# _shape_lock_live
# ---------------------------------------------------------------------------


class TestShapeLockLive:
    def test_absent_dir_is_stale(self, tmp_path):
        assert shape._shape_lock_live(str(tmp_path / "nope.lock")) is False

    def test_empty_arg_is_stale(self):
        assert shape._shape_lock_live("") is False

    def test_no_claimed_at_file_is_stale(self, tmp_path):
        lock = tmp_path / "x.lock"
        lock.mkdir()
        assert shape._shape_lock_live(str(lock)) is False

    def test_empty_timestamp_is_stale(self, tmp_path):
        lock = tmp_path / "x.lock"
        lock.mkdir()
        (lock / "claimed_at").write_text("")
        assert shape._shape_lock_live(str(lock)) is False

    def test_unparseable_timestamp_epoch_zero_is_stale(self, tmp_path):
        lock = tmp_path / "x.lock"
        lock.mkdir()
        (lock / "claimed_at").write_text("not-a-timestamp")
        assert shape._shape_lock_live(str(lock)) is False

    def test_fresh_claim_is_live(self, tmp_path):
        lock = tmp_path / "x.lock"
        lock.mkdir()
        (lock / "claimed_at").write_text(core.now_iso())
        assert shape._shape_lock_live(str(lock)) is True

    def test_old_claim_is_stale(self, tmp_path):
        lock = tmp_path / "x.lock"
        lock.mkdir()
        # A timestamp well beyond the 30s default bound.
        (lock / "claimed_at").write_text("2000-01-01T00:00:00Z")
        assert shape._shape_lock_live(str(lock)) is False

    def test_env_override_widens_bound(self, tmp_path, monkeypatch):
        lock = tmp_path / "x.lock"
        lock.mkdir()
        (lock / "claimed_at").write_text("2000-01-01T00:00:00Z")
        # Huge bound -> the ancient claim is now "live".
        monkeypatch.setenv("_CS_SHAPE_LOCK_STALE_SEC", str(10**12))
        assert shape._shape_lock_live(str(lock)) is True

    def test_env_override_malformed_falls_back_to_default(self, tmp_path, monkeypatch):
        lock = tmp_path / "x.lock"
        lock.mkdir()
        (lock / "claimed_at").write_text(core.now_iso())
        monkeypatch.setenv("_CS_SHAPE_LOCK_STALE_SEC", "garbage")
        # Fresh claim + default(30) bound -> still live; malformed env ignored.
        assert shape._shape_lock_live(str(lock)) is True


# ---------------------------------------------------------------------------
# session_shape_set
# ---------------------------------------------------------------------------


class TestSessionShapeSet:
    def test_requires_sid(self, tmp_path):
        with pytest.raises(ValueError):
            shape.session_shape_set("", {"pickup": {}}, cwd=str(tmp_path))

    def test_create_if_absent_writes_skeleton_plus_fragment(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "s1")
        ok = shape.session_shape_set("s1", {"pickup": {"file": "h.md"}}, cwd=str(repo))
        assert ok is True
        shape_file = Path(repo) / ".git" / "coordinator-sessions" / "s1" / "session-shape.json"
        data = json.loads(shape_file.read_text())
        assert data["schema_version"] == 1
        assert data["session_id"] == "s1"
        assert data["pickup"] == {"file": "h.md"}

    def test_accepts_json_string_fragment(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "s-str")
        ok = shape.session_shape_set("s-str", '{"plan": {"id": 7}}', cwd=str(repo))
        assert ok is True
        data = json.loads(
            (Path(repo) / ".git" / "coordinator-sessions" / "s-str" / "session-shape.json").read_text()
        )
        assert data["plan"] == {"id": 7}

    def test_invalid_json_string_fragment_returns_false(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "s-bad")
        assert shape.session_shape_set("s-bad", "{not json", cwd=str(repo)) is False

    def test_non_dict_fragment_returns_false(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "s-list")
        assert shape.session_shape_set("s-list", "[1,2,3]", cwd=str(repo)) is False
        assert shape.session_shape_set("s-list2", 42, cwd=str(repo)) is False

    def test_non_git_repo_returns_false(self, tmp_path):
        # Not a git repo -> session_dir unresolvable -> False.
        assert shape.session_shape_set("s", {"pickup": {}}, cwd=str(tmp_path)) is False

    def test_actioned_memos_are_appended_not_replaced(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "s2")
        shape.session_shape_set("s2", {"actioned_memos": [{"id": "a"}]}, cwd=str(repo))
        shape.session_shape_set("s2", {"actioned_memos": [{"id": "b"}]}, cwd=str(repo))
        data = json.loads(
            (Path(repo) / ".git" / "coordinator-sessions" / "s2" / "session-shape.json").read_text()
        )
        assert data["actioned_memos"] == [{"id": "a"}, {"id": "b"}]

    def test_non_memo_keys_are_replaced(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "s3")
        shape.session_shape_set("s3", {"pickup": {"v": 1}}, cwd=str(repo))
        shape.session_shape_set("s3", {"pickup": {"v": 2}}, cwd=str(repo))
        data = json.loads(
            (Path(repo) / ".git" / "coordinator-sessions" / "s3" / "session-shape.json").read_text()
        )
        assert data["pickup"] == {"v": 2}

    def test_base_wins_for_schema_version_and_session_id(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "s4")
        # A malicious fragment attempting to overwrite identity fields.
        shape.session_shape_set(
            "s4",
            {"schema_version": 99, "session_id": "evil", "pickup": {"ok": True}},
            cwd=str(repo),
        )
        data = json.loads(
            (Path(repo) / ".git" / "coordinator-sessions" / "s4" / "session-shape.json").read_text()
        )
        assert data["schema_version"] == 1
        assert data["session_id"] == "s4"
        assert data["pickup"] == {"ok": True}

    def test_defensively_creates_missing_session_dir(self, tmp_path):
        repo = _make_repo(tmp_path)
        # Do NOT pre-create the session dir.
        ok = shape.session_shape_set("s-new", {"pickup": {}}, cwd=str(repo))
        assert ok is True
        assert (Path(repo) / ".git" / "coordinator-sessions" / "s-new" / "session-shape.json").is_file()

    def test_lock_released_on_success(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "s5")
        shape.session_shape_set("s5", {"pickup": {}}, cwd=str(repo))
        lock = Path(repo) / ".git" / "coordinator-sessions" / "s5" / "session-shape.lock"
        assert not lock.exists()

    def test_no_temp_leftovers_after_write(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "s6")
        shape.session_shape_set("s6", {"pickup": {}}, cwd=str(repo))
        names = sorted(p.name for p in sdir.iterdir())
        assert names == ["session-shape.json"]

    def test_stale_lock_is_reaped_and_reclaimed(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "s7")
        # Plant a stale lock (ancient claimed_at).
        lock = sdir / "session-shape.lock"
        lock.mkdir()
        (lock / "claimed_at").write_text("2000-01-01T00:00:00Z")
        ok = shape.session_shape_set("s7", {"pickup": {"reclaimed": True}}, cwd=str(repo))
        assert ok is True
        data = json.loads((sdir / "session-shape.json").read_text())
        assert data["pickup"] == {"reclaimed": True}
        assert not lock.exists()

    def test_live_lock_blocks_acquisition_returns_false(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "s8")
        # Plant a fresh (live) lock that will never be reaped.
        lock = sdir / "session-shape.lock"
        lock.mkdir()
        (lock / "claimed_at").write_text(core.now_iso())
        # Neutralize the retry sleep so the test is fast.
        monkeypatch.setattr(shape.time, "sleep", lambda *_: None)
        ok = shape.session_shape_set("s8", {"pickup": {}}, cwd=str(repo))
        assert ok is False
        # The live lock is left intact (not reaped).
        assert lock.is_dir()

    def test_corrupt_existing_file_returns_false(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "s9")
        (sdir / "session-shape.json").write_text("{ this is not json")
        ok = shape.session_shape_set("s9", {"pickup": {}}, cwd=str(repo))
        assert ok is False
        # Lock still released even on the failure path.
        assert not (sdir / "session-shape.lock").exists()


# ---------------------------------------------------------------------------
# session_shape_read
# ---------------------------------------------------------------------------


class TestSessionShapeRead:
    def test_requires_sid(self):
        with pytest.raises(ValueError):
            shape.session_shape_read("")

    def test_absent_file_returns_skeleton(self, tmp_path):
        repo = _make_repo(tmp_path)
        out = shape.session_shape_read("no-such", cwd=str(repo))
        data = json.loads(out)
        assert data == {"schema_version": 1, "session_id": "no-such"}

    def test_non_git_repo_returns_skeleton_fail_open(self, tmp_path):
        out = shape.session_shape_read("sid", cwd=str(tmp_path))
        assert json.loads(out) == {"schema_version": 1, "session_id": "sid"}

    def test_reads_existing_file_verbatim(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "sr")
        shape.session_shape_set("sr", {"pickup": {"x": 1}}, cwd=str(repo))
        out = shape.session_shape_read("sr", cwd=str(repo))
        assert json.loads(out)["pickup"] == {"x": 1}
        # Verbatim: matches the on-disk bytes.
        assert out == (sdir / "session-shape.json").read_text()


# ---------------------------------------------------------------------------
# session_shape_magnitude
# ---------------------------------------------------------------------------


class TestSessionShapeMagnitude:
    def test_requires_sid(self):
        with pytest.raises(ValueError):
            shape.session_shape_magnitude("")

    def test_non_git_repo_returns_zeroes(self, tmp_path):
        out = shape.session_shape_magnitude("sid", cwd=str(tmp_path))
        assert json.loads(out) == {"commits_since_start": 0, "files_touched": 0}

    def test_absent_artefacts_zero(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "m0")
        assert json.loads(shape.session_shape_magnitude("m0", cwd=str(repo))) == {
            "commits_since_start": 0,
            "files_touched": 0,
        }

    def test_head_unknown_gives_zero_commits(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "m1")
        (sdir / "head_at_start").write_text("unknown\n")
        assert json.loads(shape.session_shape_magnitude("m1", cwd=str(repo)))["commits_since_start"] == 0

    def test_invalid_head_sha_gives_zero_commits(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "m2")
        (sdir / "head_at_start").write_text("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")
        assert json.loads(shape.session_shape_magnitude("m2", cwd=str(repo)))["commits_since_start"] == 0

    def test_commits_since_start_golden_diff_against_rev_list(self, tmp_path):
        """Golden-diff against reality: record HEAD, make N commits, assert the
        port's commits_since_start equals `git rev-list --count` ground truth."""
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "m3")
        head0 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        # \r / trailing-newline tolerance is part of the contract.
        (sdir / "head_at_start").write_text(head0 + "\r\n")
        for i in range(3):
            (repo / f"f{i}.txt").write_text(str(i))
            subprocess.run(["git", "add", "."], cwd=repo)
            subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=repo)
        ground = subprocess.run(
            ["git", "rev-list", "--count", f"{head0}..HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        out = json.loads(shape.session_shape_magnitude("m3", cwd=str(repo)))
        assert out["commits_since_start"] == int(ground) == 3

    def test_files_touched_dedups(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "m4")
        # 5 lines, 3 distinct (matches sort -u | wc -l).
        (sdir / "touched.txt").write_text("a\nb\na\nc\nb\n")
        assert json.loads(shape.session_shape_magnitude("m4", cwd=str(repo)))["files_touched"] == 3

    def test_files_touched_empty_file_zero(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "m5")
        (sdir / "touched.txt").write_text("")
        assert json.loads(shape.session_shape_magnitude("m5", cwd=str(repo)))["files_touched"] == 0

    def test_output_always_valid_json(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "m6")
        out = shape.session_shape_magnitude("m6", cwd=str(repo))
        parsed = json.loads(out)
        assert set(parsed) == {"commits_since_start", "files_touched"}
        assert all(isinstance(v, int) for v in parsed.values())


# ---------------------------------------------------------------------------
# producer_set / producer_read
# ---------------------------------------------------------------------------


class TestProducerSet:
    def test_writes_real_command_name(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "p1")
        ok = shape.producer_set(
            "p1", typed_command="/pickup", cwd=str(repo)
        )
        assert ok is True
        data = json.loads(
            (Path(repo) / ".git" / "coordinator-sessions" / "p1" / "session-shape.json").read_text()
        )
        assert data["producer"]["typed_command"] == "/pickup"
        assert "captured_at" in data["producer"]

    def test_writes_unresolved_sentinel(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "p2")
        shape.producer_set(
            "p2", typed_command="unresolved", cwd=str(repo)
        )
        data = json.loads(
            (Path(repo) / ".git" / "coordinator-sessions" / "p2" / "session-shape.json").read_text()
        )
        assert data["producer"]["typed_command"] == "unresolved"

    def test_writes_present_null_for_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "p3")
        shape.producer_set(
            "p3", typed_command=None, cwd=str(repo)
        )
        raw = (
            Path(repo) / ".git" / "coordinator-sessions" / "p3" / "session-shape.json"
        ).read_text()
        data = json.loads(raw)
        # Present key, JSON null -- not an absent key.
        assert "typed_command" in data["producer"]
        assert data["producer"]["typed_command"] is None
        assert '"typed_command":null' in raw or '"typed_command": null' in raw

    def test_three_states_distinguishable_on_disk(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "p4a")
        _sdir(repo, "p4b")
        _sdir(repo, "p4c")
        shape.producer_set("p4a", typed_command="/pickup", cwd=str(repo))
        shape.producer_set("p4b", typed_command="unresolved", cwd=str(repo))
        shape.producer_set("p4c", typed_command=None, cwd=str(repo))
        vals = {
            sid: shape.producer_read(sid, cwd=str(repo))["typed_command"]
            for sid in ("p4a", "p4b", "p4c")
        }
        assert vals == {"p4a": "/pickup", "p4b": "unresolved", "p4c": None}
        assert len(set(map(str, vals.values()))) == 3

    def test_rejects_out_of_contract_typed_command(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "p5")
        with pytest.raises(ValueError):
            shape.producer_set("p5", typed_command=123, cwd=str(repo))
        with pytest.raises(ValueError):
            shape.producer_set("p5", typed_command=["x"], cwd=str(repo))

    def test_rejects_sentinel_typo_near_miss(self, tmp_path):
        """A near-miss typo of a closed sentinel (e.g. "unresolvd") must be
        rejected rather than written silently -- it would otherwise render
        indistinguishably from an ordinary open-vocabulary command name on
        disk, defeating the three-state distinguishability the design rests
        on. An exact sentinel and an unrelated real command name must both
        still pass -- this guard is narrow, not a re-enumeration of example-doctrine-repo's
        open command vocabulary.
        """
        repo = _make_repo(tmp_path)
        _sdir(repo, "p5b")
        with pytest.raises(ValueError):
            shape.producer_set("p5b", typed_command="unresolvd", cwd=str(repo))
        with pytest.raises(ValueError):
            shape.producer_set("p5b", typed_command="other-comand", cwd=str(repo))
        # Exact sentinels and unrelated real command names are unaffected.
        assert shape.producer_set("p5b", typed_command="unresolved", cwd=str(repo))
        assert shape.producer_set("p5b", typed_command="/pickup", cwd=str(repo))

    def test_capture_record_carries_exactly_two_keys(self, tmp_path):
        """The capture-side record is `typed_command` + `captured_at`, and nothing else.

        example-doctrine-repo's landed `session-shape.schema.json` (x-schema-version 1.1.0)
        declares this object `additionalProperties: false` with both keys
        required, so an extra key here is a hard validation failure on their
        side rather than a harmless addition. `op_identity` in particular
        belongs to the RESOLVED-side record and is resolved at the creation
        seam, never written into or read back out of session state.
        """
        repo = _make_repo(tmp_path)
        _sdir(repo, "p6")
        assert shape.producer_set("p6", typed_command="/pickup", cwd=str(repo))
        record = shape.producer_read("p6", cwd=str(repo))
        assert set(record) == {"typed_command", "captured_at"}
        assert "op_identity" not in record

    def test_lock_acquisition_failure_surfaces_as_false(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "p7")
        lock = sdir / "session-shape.lock"
        lock.mkdir()
        (lock / "claimed_at").write_text(core.now_iso())
        monkeypatch.setattr(shape.time, "sleep", lambda *_: None)
        ok = shape.producer_set(
            "p7", typed_command="/pickup", cwd=str(repo)
        )
        assert ok is False


class TestProducerRead:
    def test_round_trip(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "r1")
        shape.producer_set("r1", typed_command="/pickup", cwd=str(repo))
        record = shape.producer_read("r1", cwd=str(repo))
        assert record["typed_command"] == "/pickup"
        assert "captured_at" in record

    def test_requires_sid(self):
        with pytest.raises(ValueError):
            shape.producer_read("")

    def test_missing_file_returns_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert shape.producer_read("no-such-session", cwd=str(repo)) is None

    def test_missing_key_returns_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        _sdir(repo, "r2")
        shape.session_shape_set("r2", {"pickup": {"x": 1}}, cwd=str(repo))
        assert shape.producer_read("r2", cwd=str(repo)) is None

    def test_malformed_json_file_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "r3")
        (sdir / "session-shape.json").write_text("{ not json")
        with pytest.raises(ValueError):
            shape.producer_read("r3", cwd=str(repo))

    def test_malformed_producer_value_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "r4")
        (sdir / "session-shape.json").write_text(
            json.dumps({"schema_version": 1, "session_id": "r4", "producer": "not-an-object"})
        )
        with pytest.raises(ValueError):
            shape.producer_read("r4", cwd=str(repo))

    def test_non_object_file_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _sdir(repo, "r5")
        (sdir / "session-shape.json").write_text("[1,2,3]")
        with pytest.raises(ValueError):
            shape.producer_read("r5", cwd=str(repo))
