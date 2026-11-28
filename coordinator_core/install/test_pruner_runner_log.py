"""pruner_runner appends one line per deleted path (path, bytes, repo) to the pruner log."""

from __future__ import annotations

import json
from unittest import mock

from coordinator_core.install import pruner_runner as pr


def _repo(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    return root


def _run(roots, log, hygiene_records, pruned=()):
    hygiene = mock.Mock(return_value={"walk": {}, "records": hygiene_records})
    with mock.patch.object(pr, "run_hygiene", hygiene), \
            mock.patch("coordinator_core.ops.fleet.prune_emitted.prune_emitted_output",
                       return_value={"pruned": list(pruned)}), \
            mock.patch("coordinator_core.lifecycle.git_common_dir", lambda r: r / ".git"):
        return pr.run_once(roots, log_path=log)


def _lines(log):
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def test_one_line_per_deleted_path_with_bytes_and_repo(tmp_path):
    a, b = _repo(tmp_path, "a"), _repo(tmp_path, "b")
    log = tmp_path / "logs" / "pruner.log"
    recs = [
        {"kind": "purge", "action": "deleted", "path": "scratch/x", "bytes": 10},
        {"kind": "purge", "action": "deleted", "path": "scratch/y", "bytes": 5},
        {"kind": "purge", "action": "deleted", "path": "scratch/gone", "bytes": 1, "reason": "already-gone"},
        {"kind": "purge", "action": "skipped", "path": "scratch/kept", "bytes": 9},
    ]
    _run([a, b], log, recs, pruned=["docs/plans/p.md"])
    rows = _lines(log)
    got = {(r["repo"], r["path"], r["bytes"]) for r in rows}
    assert got == {
        (str(r), p, n) for r in (a, b) for p, n in (("scratch/x", 10), ("scratch/y", 5), ("docs/plans/p.md", None))
    }
    assert len(rows) == 6 and all(r["ts"] for r in rows)


def test_a_tick_that_deletes_nothing_writes_no_log(tmp_path):
    log = tmp_path / "logs" / "pruner.log"
    _run([_repo(tmp_path, "a")], log, [{"kind": "purge", "action": "skipped", "path": "s", "bytes": 1}])
    assert not log.exists()


def test_log_written_per_repo_before_a_later_repo_fails(tmp_path):
    a, b = _repo(tmp_path, "a"), _repo(tmp_path, "b")
    log = tmp_path / "logs" / "pruner.log"

    def fake(root):
        if root == b:
            raise KeyboardInterrupt
        return {"repo": str(root), "deleted": [{"path": "s/x", "bytes": 1}]}

    with mock.patch.object(pr, "prune_repo", fake):
        try:
            pr.run_once([a, b], log_path=log)
        except KeyboardInterrupt:
            pass
    assert [r["repo"] for r in _lines(log)] == [str(a)]


def test_oversize_log_rotates_to_one_generation(tmp_path):
    log = tmp_path / "logs" / "pruner.log"
    log.parent.mkdir()
    log.write_text("x" * (pr.MAX_LOG_BYTES + 1), encoding="utf-8")
    pr.log_deletions([{"repo": "r", "deleted": [{"path": "p", "bytes": 1}]}], log)
    assert (tmp_path / "logs" / "pruner.log.1").stat().st_size == pr.MAX_LOG_BYTES + 1
    assert len(_lines(log)) == 1


def test_second_tick_appends(tmp_path):
    a = _repo(tmp_path, "a")
    log = tmp_path / "logs" / "pruner.log"
    rec = [{"kind": "purge", "action": "deleted", "path": "scratch/x", "bytes": 1}]
    _run([a], log, rec)
    _run([a], log, rec)
    assert len(_lines(log)) == 2


def test_unwritable_log_never_stops_the_tick(tmp_path):
    a = _repo(tmp_path, "a")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    rec = [{"kind": "purge", "action": "deleted", "path": "scratch/x", "bytes": 1}]
    results = _run([a], blocker / "pruner.log", rec)
    assert results[0]["emitted_pruned"] == 0


def test_default_log_path_is_under_the_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    a = _repo(tmp_path, "a")
    _run([a], None, [{"kind": "purge", "action": "deleted", "path": "scratch/x", "bytes": 3}])
    assert _lines(tmp_path / "sh" / "logs" / "pruner.log")[0]["bytes"] == 3
