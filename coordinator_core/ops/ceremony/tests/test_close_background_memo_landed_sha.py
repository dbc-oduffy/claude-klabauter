"""memo-lifecycle records the run's landed sha on the resolved source memo."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta

from coordinator_core import archive_stamp
from coordinator_core.memo_corpus import memo_corpus_root
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close

RUN = "run-sha"
SHA = "abcdef0123456789abcdef0123456789abcdef01"


def _inbox_memo(root):
    inbox = root / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    assert memo_corpus_root(str(root)).replace("\\", "/").endswith("state/cross-repo")
    (inbox / "x.md").write_text("---\nstatus: open\n---\nbody\n", encoding="utf-8")


def _capture(monkeypatch):
    calls = []

    def fake(path, *args, return_result=False):
        calls.append((path, args))
        return {"exit_code": 0}

    monkeypatch.setattr(archive_stamp, "cs_resolve_memo", fake)
    return calls


def _ctx(root, sha):
    return cb.JobContext(root, root / ".git", RUN, (), "state/cross-repo/inbox/x.md", sha)


def test_sha_is_passed_as_realized_by_and_named_in_the_note(tmp_path, monkeypatch):
    _inbox_memo(tmp_path)
    calls = _capture(monkeypatch)
    spawns = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawns.append(a))

    result = cb._memo_lifecycle(_ctx(tmp_path, SHA))

    assert result.status == "ran"
    (_, args), = calls
    assert args[args.index("--realized-by") + 1] == SHA
    assert args[args.index("--actioned-note") + 1] == f"resolved by run {RUN} at {SHA}"
    assert len(spawns) <= 1


def test_absent_sha_keeps_the_old_note_and_no_realized_by(tmp_path, monkeypatch):
    _inbox_memo(tmp_path)
    calls = _capture(monkeypatch)

    assert cb._memo_lifecycle(_ctx(tmp_path, None)).status == "ran"

    (_, args), = calls
    assert args == ("--actioned-note", f"resolved by run {RUN}")


def test_run_background_reads_terminal_sha_from_the_close_job_record(tmp_path, monkeypatch):
    _inbox_memo(tmp_path)
    calls = _capture(monkeypatch)
    run_close.write_close_job(
        tmp_path, RUN, ("memo-lifecycle",), datetime.now().astimezone() + timedelta(minutes=10),
        terminal_sha=SHA,
    )

    outcomes = cb.run_background(_ctx(tmp_path, None), {"memo-lifecycle": cb._memo_lifecycle})

    assert outcomes["memo-lifecycle"].status == "ran"
    (_, args), = calls
    assert args[args.index("--realized-by") + 1] == SHA


def test_record_without_terminal_sha_resolves_with_the_old_note(tmp_path, monkeypatch):
    _inbox_memo(tmp_path)
    calls = _capture(monkeypatch)
    run_close.write_close_job(
        tmp_path, RUN, ("memo-lifecycle",), datetime.now().astimezone() + timedelta(minutes=10)
    )

    cb.run_background(_ctx(tmp_path, None), {"memo-lifecycle": cb._memo_lifecycle})

    (_, args), = calls
    assert "--realized-by" not in args
