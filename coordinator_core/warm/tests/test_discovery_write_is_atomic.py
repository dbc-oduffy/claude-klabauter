"""`write_discovery` replaces the record atomically, so a lock-free reader
never observes a truncated one.

WHY THIS FILE EXISTS -- a measured incident, not a hypothetical. `read_discovery`
takes NO lock and must not: it sits on the hook path, where a lock acquisition is
the cost this transport exists to remove. `write_discovery` used to call
`path.write_text`, which truncates before writing. A reader landing inside that
window read an empty or partial file, failed to parse, and got `None` -- which
every consumer correctly interprets as "no listener" while the listener is up.

The evidence (doe-claude-5a's availability sink, 2026-08-25, n=445): two isolated
`no_listener` samples at 19:57:00.560Z and 19:58:00.562Z with `probe_latency_ms`
of **0.037 and 0.031 ms**, against 116-167ms for healthy neighbours. Thirty-odd
microseconds is three orders of magnitude short of one round trip, so nothing was
dialled and no timeout was hit -- the record simply wasn't there to read. Both
coincide with an `engine_token` rotation (a publish rewriting this record), and
OS process-table evidence showed the listener pid ran continuously across both.
The record went away; the process never did.

NEGATIVE SPEC. These tests say nothing about listener availability, health
checking, or `ensure_listener`'s spawn path. They pin one property: a concurrent
reader sees either the whole old record or the whole new one, never neither and
never half.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from coordinator_core.warm import supervisor


def _write(root: Path, port: int, sha: str) -> None:
    supervisor.write_discovery(
        port=port,
        pid=4321,
        stable_pid_start_epoch=99,
        engine_sha=sha,
        engine_root=root,
    )


class TestAtomicReplace:
    def test_a_reader_racing_many_writes_never_sees_a_partial_record(
        self, tmp_path: Path
    ) -> None:
        """The regression test proper. Under the old truncate-then-write this
        fails with `None` observations; the whole point is that a reader
        hammering the file during a rewrite storm sees only whole records.
        """
        _write(tmp_path, 5000, "a" * 16)

        stop = threading.Event()
        misses: list[str] = []

        def reader() -> None:
            # A raise in here used to be SWALLOWED by the thread and the test
            # still passed green -- which is how a NameError in the retry branch
            # survived a full run. Record it as a miss so the assertion sees it.
            try:
                while not stop.is_set():
                    rec = supervisor.read_discovery(tmp_path)
                    if rec is None:
                        misses.append("read_discovery returned None mid-rewrite")
                    elif "port" not in rec or "engine_sha" not in rec:
                        misses.append("partial record observed: %r" % (rec,))
            except BaseException as exc:  # noqa: BLE001 -- surfacing, not handling
                misses.append("reader raised %r" % (exc,))

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            for i in range(150):
                _write(tmp_path, 5000 + i, ("%016x" % i))
        finally:
            stop.set()
            t.join(timeout=10)

        assert not misses, misses[:5]

    def test_the_record_is_never_left_empty_on_disk(self, tmp_path: Path) -> None:
        _write(tmp_path, 5100, "b" * 16)
        path = supervisor.discovery_path(tmp_path)
        for i in range(25):
            _write(tmp_path, 5100 + i, "c" * 16)
            assert path.read_text(encoding="utf-8").strip(), "record observed empty"

    def test_content_survives_the_replace_intact(self, tmp_path: Path) -> None:
        _write(tmp_path, 5200, "d" * 16)
        rec = supervisor.read_discovery(tmp_path)
        assert rec is not None
        assert rec["port"] == 5200
        assert rec["engine_sha"] == "d" * 16
        assert rec["health_path"] == supervisor.HEALTH_PATH
        assert rec["hook_path"] == supervisor.HOOK_PATH

    def test_a_rewrite_is_visible_to_the_next_read(self, tmp_path: Path) -> None:
        """Atomicity must not be bought with staleness."""
        _write(tmp_path, 5300, "e" * 16)
        _write(tmp_path, 5301, "f" * 16)
        rec = supervisor.read_discovery(tmp_path)
        assert rec is not None and rec["port"] == 5301


class TestNoTempFileResidue:
    def test_no_temp_files_are_left_beside_the_record(self, tmp_path: Path) -> None:
        """A `.discovery-*.tmp` accumulating once per boot would be a slow leak
        in a per-clone runtime dir, and `svc_dir()` is shared with the pipe
        transport's own breadcrumb.
        """
        for i in range(12):
            _write(tmp_path, 5400 + i, "0" * 16)
        parent = supervisor.discovery_path(tmp_path).parent
        leftovers = [p.name for p in parent.iterdir() if p.name.startswith(".discovery-")]
        assert leftovers == [], leftovers

    def test_the_temp_file_is_created_in_the_targets_own_directory(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`os.replace` is atomic only within a filesystem. A temp file created
        anywhere but the target's own directory degrades the replace to a copy
        and silently reopens the window this fix closes.
        """
        seen: list[str] = []
        real_mkstemp = supervisor.tempfile.mkstemp

        def spy(*args, **kwargs):
            seen.append(str(kwargs.get("dir")))
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(supervisor.tempfile, "mkstemp", spy)
        _write(tmp_path, 5500, "9" * 16)

        assert seen, "mkstemp was not used -- the write is not going through a temp file"
        assert seen[0] == str(supervisor.discovery_path(tmp_path).parent)


class TestContentionFallback:
    """The budget-exhaustion path, which no ordinary run reaches and which
    therefore has to be driven deliberately -- an untested fallback is the
    branch that fails the one time it matters.
    """

    def test_exhaustion_publishes_when_there_is_no_record_to_damage(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """With NO prior record, losing every replace must still publish. A raise
        here would leave the clone with no discovery file at all, so every hook
        fire falls open until the next boot -- strictly worse than the torn-read
        window the atomic write exists to close.
        """
        monkeypatch.setattr(supervisor, "_replace_with_retry", lambda *_a, **_kw: False)
        _write(tmp_path, 5601, "2" * 16)

        rec = supervisor.read_discovery(tmp_path)
        assert rec is not None, "the record was lost when the replace never won"
        assert rec["port"] == 5601

    def test_exhaustion_leaves_an_existing_record_intact_rather_than_tearing_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """With a record ALREADY on disk, exhaustion must skip the rotation
        rather than fall back to a truncating in-place write.

        This inverts the pre-2026-08-25 behaviour deliberately, and the reason is
        that the two failure modes are not equally loud. A STALE record names a
        listener whose `engine_sha` no longer matches, so `_serve_line` answers
        ENGINE_SKEW and evicts -- loud, self-correcting on the next fire. A TORN
        record reads as `None`, which every consumer correctly reads as "no
        listener" -- silent, and on the hook path that is a guard that did not run
        and said nothing.

        Not hypothetical: two record-rewrite events survived the atomic-write fix
        (2026-08-25T21:22:00.639Z and T21:58:30.667Z, both k=1), and both were
        `engine_token` rotations -- i.e. rewrites of a record that already existed,
        which is precisely the case this test now pins.
        """
        _write(tmp_path, 5600, "1" * 16)

        monkeypatch.setattr(supervisor, "_replace_with_retry", lambda *_a, **_kw: False)
        _write(tmp_path, 5601, "2" * 16)

        rec = supervisor.read_discovery(tmp_path)
        assert rec is not None, "the prior record was destroyed -- the torn-read window"
        assert rec["port"] == 5600, (
            "exhaustion overwrote a live record in place; a skipped rotation leaves "
            "a stale record that skews loudly, which beats a torn one that reads absent"
        )

    def test_the_fallback_leaves_no_temp_file_behind(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write(tmp_path, 5700, "3" * 16)
        monkeypatch.setattr(supervisor, "_replace_with_retry", lambda *_a, **_kw: False)
        _write(tmp_path, 5701, "4" * 16)

        parent = supervisor.discovery_path(tmp_path).parent
        assert [p.name for p in parent.iterdir() if p.name.startswith(".discovery-")] == []

    def test_supervisor_reuses_the_shared_primitive(self) -> None:
        """Not a second hand-rolled copy. If this drifts back to a local
        implementation, `locked_rmw`'s identical fix and this one diverge.
        """
        from coordinator_core import locked_write

        assert supervisor._replace_with_retry is locked_write.replace_with_retry

    def test_replace_with_retry_reports_loss_rather_than_raising(self) -> None:
        """The contract the caller depends on: False, never an exception."""
        calls = {"n": 0}

        def denied(*_a, **_kw):
            calls["n"] += 1
            raise PermissionError(5, "denied")

        import os as _os

        real = _os.replace
        _os.replace = denied
        try:
            won = supervisor._replace_with_retry(
                "irrelevant", "also-irrelevant", budget_secs=0.01, sleep=lambda _s: None
            )
        finally:
            _os.replace = real
        assert won is False
        assert calls["n"] >= 2, "the budget was never actually retried against"
