"""
coordinator_core.tests.test_chain_attribution_since_window

Path-scoped tests for `chain_attribution.bulk_grep_attributed_shas`'s new
optional `since=` window bound (task C1, pln-the-completion-entry-computes-
a1780d). Uses a recording fake GitRunner rather than a real git fixture —
this file asserts the exact argv `bulk_grep_attributed_shas` builds, which a
real subprocess round-trip cannot make an assertion about (git's own
`--since` semantics are not under test here; the module's argv-construction
is).

Coverage:
  - `since=None` produces argv identical to the pre-C1 shape.
  - `since="2026-09-01"` appends exactly one `--since=2026-09-01` element,
    nothing else.
  - A malformed `session_id` still short-circuits to `[]` BEFORE any git
    call, with `since` supplied — the `_UUID_RE` guard is not reachable-
    around via the new parameter.

Spec backlink: pln-the-completion-entry-computes-a1780d task C1.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from coordinator_core import chain_attribution


class _RecordingGitRunner:
    """Fake `GitRunner`: records every call's argv, returns a canned result.

    Never touches a real process — this module's contract under test is
    "what argv did `bulk_grep_attributed_shas` build", not "what does git do
    with it".
    """

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.calls: List[Tuple[List[str], Optional[str]]] = []

    def __call__(self, argv: List[str], cwd: Optional[str]) -> Tuple[int, str, str]:
        self.calls.append((list(argv), cwd))
        return self.rc, self.stdout, self.stderr


_SID = "abc12345-dead-beef-0000-1234567890ab"


def test_since_none_produces_byte_identical_argv_to_pre_c1_shape():
    runner = _RecordingGitRunner(stdout="sha1\nsha2\n")

    result = chain_attribution.bulk_grep_attributed_shas(
        "HEAD", _SID, "/repo", runner,
    )

    assert result == ["sha1", "sha2"]
    assert len(runner.calls) == 1
    argv, cwd = runner.calls[0]
    assert argv == [
        "git", "log", "--no-merges",
        f"--grep=^Session-Id: {_SID}$",
        "--format=%H",
        "HEAD",
    ]
    assert cwd == "/repo"


def test_since_appends_exactly_one_since_argv_element():
    runner = _RecordingGitRunner(stdout="sha1\n")

    result = chain_attribution.bulk_grep_attributed_shas(
        "HEAD", _SID, "/repo", runner, since="2026-09-01",
    )

    assert result == ["sha1"]
    assert len(runner.calls) == 1
    argv, _cwd = runner.calls[0]
    assert argv == [
        "git", "log", "--no-merges",
        f"--grep=^Session-Id: {_SID}$",
        "--format=%H",
        "--since=2026-09-01",
        "HEAD",
    ]
    assert argv.count("--since=2026-09-01") == 1
    assert sum(1 for a in argv if a.startswith("--since=")) == 1


def test_malformed_session_id_short_circuits_before_any_git_call_even_with_since():
    runner = _RecordingGitRunner(stdout="sha1\n")

    result = chain_attribution.bulk_grep_attributed_shas(
        "HEAD", "not-a-uuid!!", "/repo", runner, since="2026-09-01",
    )

    assert result == []
    assert runner.calls == []
