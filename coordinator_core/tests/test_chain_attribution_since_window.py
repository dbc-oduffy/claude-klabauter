
from __future__ import annotations

from typing import List, Optional, Tuple

from coordinator_core import chain_attribution


class _RecordingGitRunner:

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
