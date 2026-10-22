"""Tests for canvas_harvest.probe_stamp: only `probe` changes, stale reads and mismatches refuse."""

import json
import os
import subprocess

from coordinator_core.win_portability import no_console_creationflags

import pytest

from coordinator_core.canvas_harvest import model, probe_stamp
from coordinator_core.session import declared_writes
from coordinator_core.win_portability import no_console_creationflags

AID = "abc123"
T_HARVEST = "2026-01-01T10:00:00Z"
T_AFTER = "2026-01-01T11:00:00Z"
NOW = "2026-01-01T11:05:00Z"


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    return cfg


def _login(cfg, email):
    (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}), encoding="utf-8")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )


def _committed_repo(tmp_path, account="a@x"):
    """A git repo whose HEAD holds the full record under harvest/ (frame a.html listed, thread t1)."""
    rec = _record()
    rec["harvest"]["account"] = account
    rec["harvest"]["frames"] = [{"path": "a.html", "sha256": "0" * 64}]
    out = _setup(tmp_path, rec)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "m")
    return out


def _unreadable(out, kind=model.NOT_FOUND):
    probe_stamp.stamp_unreadable(model.ReadError(kind, "l", NOW), AID, out, "s1", NOW)
    return json.loads((out / "harvest.json").read_text())["probe"]


def test_readable_probe_stamps_account_and_never_backfills(tmp_path, _config_dir):
    _login(_config_dir, "me@x")
    out = _setup(tmp_path, _record())
    probe_stamp.stamp_probe(_lifted(), out, "s1", NOW)
    after = json.loads((out / "harvest.json").read_text())
    assert after["probe"]["account"] == "me@x"
    assert "record_committed" not in after["probe"]
    assert "account" not in after["harvest"]


@pytest.mark.parametrize(
    "held,login,kind,expected",
    [
        ("a@x", "b@x", model.NOT_FOUND, "other-account"),
        ("a@x", "a@x", model.NOT_FOUND, "not-found"),
        (None, "b@x", model.NOT_FOUND, "not-found"),
        ("a@x", None, model.NOT_FOUND, "not-found"),
        ("", "b@x", model.NOT_FOUND, "not-found"),
        ("a@x", "b@x", model.NEEDS_APPROVAL, "needs-approval"),
    ],
)
def test_unreadable_reason_by_accounts(tmp_path, _config_dir, held, login, kind, expected):
    if login:
        _login(_config_dir, login)
    rec = _record()
    rec["harvest"]["account"] = held
    out = _setup(tmp_path, rec)
    probe = _unreadable(out, kind)
    assert probe["unreadable_reason"] == expected
    assert probe["account"] == login


def test_record_committed_true_when_head_matches(tmp_path):
    out = _committed_repo(tmp_path)
    assert _unreadable(out)["record_committed"] is True


def test_record_committed_ignores_prior_probe_key(tmp_path):
    out = _committed_repo(tmp_path)
    _unreadable(out)
    assert _unreadable(out)["record_committed"] is True


@pytest.mark.parametrize("rel", ["SETTLED.md", "frames/a.html", "threads/t1.json"])
def test_record_committed_false_when_listed_file_differs(tmp_path, rel):
    out = _committed_repo(tmp_path)
    (out / rel).write_bytes(b"changed\n")
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_false_when_listed_file_missing(tmp_path):
    out = _committed_repo(tmp_path)
    (out / "frames" / "a.html").unlink()
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_false_when_record_body_differs(tmp_path):
    out = _committed_repo(tmp_path)
    rec = json.loads((out / "harvest.json").read_text())
    rec["harvest"]["version"] = "v9"
    (out / "harvest.json").write_bytes(model.dump_json(rec))
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_false_when_not_in_git(tmp_path):
    out = _setup(tmp_path, _record())
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_false_when_file_untracked(tmp_path):
    out = _committed_repo(tmp_path)
    (out / "threads" / "t2.json").write_bytes(b"{}\n")
    rec = json.loads((out / "harvest.json").read_text())
    rec["harvest"]["thread_ids"] = ["t1", "t2"]
    (out / "harvest.json").write_bytes(model.dump_json(rec))
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_spawns_no_process(tmp_path, monkeypatch):
    out = _committed_repo(tmp_path)

    def boom(*a, **k):
        raise AssertionError("record_committed must not spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert _unreadable(out)["record_committed"] is True


def _record(probe=None, n_files=0):
    rec = {
        "schema": model.SCHEMA,
        "artifact": {"url": f"claude.ai/artifact/{AID}", "id": AID, "title": "T", "type": "canvas"},
        "harvest": {
            "harvested_at": T_HARVEST,
            "version": "v1",
            "thread_ids": ["t1"],
            "latest_comment_at": "2026-01-01T09:00:00Z",
            "frames": [{"path": f"f{i}.html", "sha256": "0" * 64} for i in range(n_files)],
        },
    }
    if probe is not None:
        rec["probe"] = probe
    return rec


def _setup(tmp_path, record=None, raw=None):
    out = tmp_path / "harvest"
    (out / "frames").mkdir(parents=True)
    (out / "threads").mkdir()
    (out / "frames" / "a.html").write_bytes(b"<p>x</p>")
    (out / "threads" / "t1.json").write_bytes(b"{}\n")
    (out / "SETTLED.md").write_bytes(b"# s\n")
    if raw is not None:
        (out / "harvest.json").write_bytes(raw)
    elif record is not None:
        (out / "harvest.json").write_bytes(model.dump_json(record))
    return out


def _lifted(version_at=T_AFTER, comments_at=T_AFTER):
    threads = [
        {"id": "t1", "comments": [{"created_at": "2026-01-01T10:30:00Z"}]},
        {"id": "t2", "comments": [{"created_at": "2026-01-01T10:45:00Z"}]},
    ]
    return model.Lifted(
        artifact_read=model.ArtifactRead(f"claude.ai/artifact/{AID}", AID, "T", "canvas", "v2", (), T_AFTER),
        comments=model.CommentsRead(threads, "txt", comments_at),
        version=model.VersionRead("v2", version_at),
        error=None,
        transcript_dir=None,
        session_id="s1",
    )


def _snapshot(out):
    snap = {}
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "harvest.json":
            snap[p.relative_to(out).as_posix()] = (p.read_bytes(), p.stat().st_mtime_ns)
    return snap


def _refused(reason, fn, *args, **kwargs):
    with pytest.raises(model.HarvestError) as exc:
        fn(*args, **kwargs)
    assert exc.value.reason == reason


def test_only_probe_differs(tmp_path):
    out = _setup(tmp_path, _record())
    before = json.loads((out / "harvest.json").read_text())
    snap = _snapshot(out)
    written = probe_stamp.stamp_probe(_lifted(), out, "s1", NOW)
    assert written == out / "harvest.json"
    after = json.loads((out / "harvest.json").read_text())
    probe = after.pop("probe")
    assert after == before and list(after) == list(before)
    assert probe == {
        "probed_at": NOW,
        "session_id": "s1",
        "account": None,
        "readable": True,
        "unreadable_reason": None,
        "version": "v2",
        "thread_ids": ["t1", "t2"],
        "latest_comment_at": "2026-01-01T10:45:00Z",
    }
    assert _snapshot(out) == snap


def test_second_probe_replaces_first(tmp_path):
    out = _setup(tmp_path, _record())
    probe_stamp.stamp_probe(_lifted(), out, "s1", NOW)
    later = "2026-01-01T12:00:00Z"
    probe_stamp.stamp_probe(_lifted(later, later), out, "s2", "2026-01-01T12:01:00Z")
    probe = json.loads((out / "harvest.json").read_text())["probe"]
    assert probe["session_id"] == "s2" and probe["probed_at"] == "2026-01-01T12:01:00Z"


def test_second_probe_with_old_reads_is_stale(tmp_path):
    out = _setup(tmp_path, _record())
    probe_stamp.stamp_probe(_lifted(), out, "s1", NOW)
    raw = (out / "harvest.json").read_bytes()
    _refused(model.PROBE_READS_STALE, probe_stamp.stamp_probe, _lifted("2026-01-01T11:05:00Z", "2026-01-01T11:06:00Z"), out, "s1", NOW)
    assert (out / "harvest.json").read_bytes() == raw


@pytest.mark.parametrize("which", ["version", "comments"])
def test_stale_read_refused_and_record_untouched(tmp_path, which):
    out = _setup(tmp_path, _record())
    raw = (out / "harvest.json").read_bytes()
    kw = {"version_at": T_HARVEST} if which == "version" else {"comments_at": "2026-01-01T09:00:00Z"}
    _refused(model.PROBE_READS_STALE, probe_stamp.stamp_probe, _lifted(**kw), out, "s1", NOW)
    assert (out / "harvest.json").read_bytes() == raw


def test_no_record(tmp_path):
    out = _setup(tmp_path)
    _refused(model.NO_HARVEST_RECORD, probe_stamp.stamp_probe, _lifted(), out, "s1", NOW)
    assert not (out / "harvest.json").exists()


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b"{}"])
def test_unparseable_record(tmp_path, raw):
    out = _setup(tmp_path, raw=raw)
    _refused(model.RECORD_UNPARSEABLE, probe_stamp.stamp_probe, _lifted(), out, "s1", NOW)
    _refused(model.RECORD_UNPARSEABLE, probe_stamp.stamp_unreadable, model.ReadError("not-found", "x", NOW), AID, out, "s1", NOW)
    assert (out / "harvest.json").read_bytes() == raw


def test_artifact_mismatch_both_functions(tmp_path):
    out = _setup(tmp_path, _record())
    raw = (out / "harvest.json").read_bytes()
    other = _lifted()
    other = model.Lifted(
        model.ArtifactRead("u", "other", "T", "canvas", "v2", (), T_AFTER),
        other.comments, other.version, None, None, "s1",
    )
    _refused(model.RECORD_ARTIFACT_MISMATCH, probe_stamp.stamp_probe, other, out, "s1", NOW)
    _refused(model.RECORD_ARTIFACT_MISMATCH, probe_stamp.stamp_unreadable, model.ReadError("not-found", "x", NOW), "other", out, "s1", NOW)
    assert (out / "harvest.json").read_bytes() == raw


def test_no_session_id(tmp_path):
    out = _setup(tmp_path, _record())
    raw = (out / "harvest.json").read_bytes()
    _refused(model.NO_SESSION_ID, probe_stamp.stamp_probe, _lifted(), out, "", NOW)
    _refused(model.NO_SESSION_ID, probe_stamp.stamp_unreadable, model.ReadError("not-found", "x", NOW), AID, out, None, NOW)
    assert (out / "harvest.json").read_bytes() == raw


@pytest.mark.parametrize("kind", [model.NOT_FOUND, model.NEEDS_APPROVAL])
def test_unreadable_over_record(tmp_path, kind):
    prior = {"probed_at": T_AFTER, "session_id": "old", "readable": True, "unreadable_reason": None,
             "version": "v1", "thread_ids": ["t1"], "latest_comment_at": None}
    out = _setup(tmp_path, _record(probe=prior))
    before = json.loads((out / "harvest.json").read_text())
    snap = _snapshot(out)
    written = probe_stamp.stamp_unreadable(model.ReadError(kind, "line", NOW), AID, out, "s1", NOW)
    assert written == out / "harvest.json"
    after = json.loads((out / "harvest.json").read_text())
    probe = after.pop("probe")
    before.pop("probe")
    assert after == before
    assert probe == {"probed_at": NOW, "session_id": "s1", "account": None, "readable": False,
                     "unreadable_reason": kind, "record_committed": False,
                     "version": "", "thread_ids": [], "latest_comment_at": None}
    assert _snapshot(out) == snap


def test_unreadable_unclassified_writes_nothing(tmp_path):
    out = _setup(tmp_path, _record())
    raw = (out / "harvest.json").read_bytes()
    _refused(model.READ_ERROR_UNCLASSIFIED, probe_stamp.stamp_unreadable, model.ReadError(None, "odd", NOW), AID, out, "s1", NOW)
    assert (out / "harvest.json").read_bytes() == raw


def test_unreadable_without_record_creates_nothing(tmp_path):
    out = tmp_path / "harvest"
    assert probe_stamp.stamp_unreadable(model.ReadError("not-found", "x", NOW), AID, out, "s1", NOW) is None
    assert not out.exists()
    out.mkdir()
    assert probe_stamp.stamp_unreadable(model.ReadError("not-found", "x", NOW), AID, out, "s1", NOW) is None
    assert os.listdir(out) == []


def test_each_declares_exactly_harvest_json_at_max_record(tmp_path):
    out = _setup(tmp_path, _record(n_files=model.MAX_RECORD_FILES))
    target = str(out / "harvest.json")
    with declared_writes.collecting() as got:
        probe_stamp.stamp_probe(_lifted(), out, "s1", NOW)
    assert got == [target]
    with declared_writes.collecting() as got:
        probe_stamp.stamp_unreadable(model.ReadError("not-found", "x", NOW), AID, out, "s1", NOW)
    assert got == [target]
