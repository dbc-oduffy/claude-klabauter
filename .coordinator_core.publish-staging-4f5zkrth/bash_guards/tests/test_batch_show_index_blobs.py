"""Direct multi-item coverage for `_batch_show_index_blobs`
(`coordinator_core.bash_guards.dispatch_checks`), added per review finding
amp-s1 #5 (WARN: none of the four new batched helpers from
docs/plans/2026-08-19-burn-down-the-amplification-hitlist.md C5 had direct
tests -- only indirect coverage through `check_validate_commit`'s call
site).

Both tests below are genuinely multi-item: a single-path test through this
function is indistinguishable from a single-path test through the pre-batch
per-file `git show :<path>` it replaced, and would pass identically before
and after batching landed -- exactly the non-test finding #5 names. These
instead exercise (a) multi-record byte-offset parsing with a miss in the
middle of the stream, which only exists once >1 path shares one `cat-file
--batch` feed, and (b) the amp-s1 #2 fix itself: a non-zero returncode after
partial stdout must fail the WHOLE batch closed, never let the byte-offset
parser slice a wrong/truncated blob out of stdout that happens to look
well-formed. Neither scenario is reachable through the single-path
`git show` path this function replaced, so both fail against that
pre-batch shape (the function/behavior did not exist there at all) as well
as against this file's own pre-fix state for (b).
"""

from __future__ import annotations

from coordinator_core.bash_guards import dispatch_checks


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def _batch_stdout(sha_a: str, sha_c: str) -> bytes:
    # Mirrors real `git cat-file --batch` framing: `<sha> blob <size>\n`
    # followed by exactly `<size>` content bytes and a trailing `\n`, or
    # `<requested-object> missing\n` for an absent path -- constructed by
    # hand rather than run against a real repo so the miss sits in the
    # MIDDLE of a 3-path stream, proving byte-offset resync past it.
    header_a = f"{sha_a} blob 5\n".encode("utf-8")
    content_a = b"hello\n"
    missing_b = b":paths/b missing\n"
    header_c = f"{sha_c} blob 3\n".encode("utf-8")
    content_c = b"xyz\n"
    return header_a + content_a + missing_b + header_c + content_c


def test_multi_path_batch_resolves_present_and_missing_by_own_slot(monkeypatch):
    """3-path batch with the miss in the middle: each path's own slot is
    resolved from its own byte range, not shifted by the missing record."""
    sha_a = "1" * 40
    sha_c = "2" * 40
    stdout = _batch_stdout(sha_a, sha_c)

    monkeypatch.setattr(
        dispatch_checks.subprocess, "run",
        lambda *a, **k: _FakeCompleted(0, stdout),
    )

    results = dispatch_checks._batch_show_index_blobs(
        ["paths/a", "paths/b", "paths/c"], cwd=None,
    )

    assert results == {"paths/a": "hello", "paths/b": None, "paths/c": "xyz"}


def test_nonzero_returncode_fails_the_whole_batch_closed(monkeypatch):
    """Regression pin for review finding amp-s1 #2: a non-zero exit after
    well-formed-looking partial stdout must never be parsed as a successful
    read. Before the fix, `proc.returncode` was never inspected, so this
    exact stdout would have decoded to {"paths/a": "hello", "paths/b": None,
    "paths/c": "xyz"} -- a WRONG (not missing) blob presented as
    successfully read. After the fix every path must come back None."""
    sha_a = "1" * 40
    sha_c = "2" * 40
    stdout = _batch_stdout(sha_a, sha_c)

    monkeypatch.setattr(
        dispatch_checks.subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stdout),
    )

    results = dispatch_checks._batch_show_index_blobs(
        ["paths/a", "paths/b", "paths/c"], cwd=None,
    )

    assert results == {"paths/a": None, "paths/b": None, "paths/c": None}


def test_empty_input_short_circuits_without_spawning(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not spawn for an empty path list")

    monkeypatch.setattr(dispatch_checks.subprocess, "run", _fail)
    assert dispatch_checks._batch_show_index_blobs([], cwd=None) == {}
