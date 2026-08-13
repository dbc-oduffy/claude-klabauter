"""coordinator_core.probes.tests.test_fork_census — fixture-based tests for the
probes.fork_census op.

Purpose: pin classification behaviour (fork vs builtin, per-machine bucketing,
macOS advisory-conversion vs Windows denies) against a small, hand-computed
fixture transcript set — never against the live `~/.claude/projects` corpus,
which changes daily and would make this test flaky/non-reproducible (dispatch
instruction: "Build the fixtures; do not assert against the live corpus").

Coverage:
  (a) classify_platform — windows/macos/linux/unknown, including the
      drive-letter-vs-URL-scheme trap (`https://` must NOT classify windows).
  (b) count_command_shape — fork/builtin classification, cd+git shape
      detection, `git -C` idiomatic detection, heredoc-body exclusion.
  (c) run_fork_census over a two-machine fixture corpus — asserts the
      per-machine split is present and correct (AC-1: by_machine, never a
      single pooled number as the primary read), macOS advisory_conversion_rate
      computed while Windows's is None with denies_observed carrying its
      signal instead (AC-14).
  (d) op registration — importing the module registers "probes.fork_census";
      the registered handler produces the same result as calling
      run_fork_census directly.

Spec backlink: docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md § C1.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from coordinator_core.probes import fork_census


# ---------------------------------------------------------------------------
# (a) classify_platform
# ---------------------------------------------------------------------------


def test_classify_platform_macos_users_path():
    assert fork_census.classify_platform("/Users/alice/X/coordinator-claude") == "macos"


def test_classify_platform_macos_private_tmp():
    assert fork_census.classify_platform("/private/tmp/claude-501/x") == "macos"


def test_classify_platform_windows_drive_letter_backslash():
    assert fork_census.classify_platform("C:\\Users\\bob\\project") == "windows"


def test_classify_platform_windows_drive_letter_forward_slash():
    assert fork_census.classify_platform("C:/Users/bob/project") == "windows"


def test_classify_platform_linux_posix_non_mac():
    assert fork_census.classify_platform("/home/bob/project") == "linux"


def test_classify_platform_unknown_empty():
    assert fork_census.classify_platform("") == "unknown"
    assert fork_census.classify_platform(None) == "unknown"


def test_classify_platform_does_not_match_url_scheme():
    """Passing-side test for the drive-letter/URL-scheme trap this fleet has
    hit before (a `[A-Za-z]:[/\\]` regex matches the "s:" in "https://").
    A bare URL string is not a real cwd, but the classifier must not
    misclassify it as "windows" via the same substring-match failure mode.
    """
    assert fork_census.classify_platform("https://example.com/path") != "windows"


# ---------------------------------------------------------------------------
# (b) count_command_shape
# ---------------------------------------------------------------------------


def test_count_command_shape_single_external_binary():
    shape = fork_census.count_command_shape("git status")
    assert shape.external_forks == 1
    assert shape.builtin_invocations == 0
    assert shape.binaries == {"git": 1}
    assert shape.cd_then_git is False
    assert shape.git_dash_c is False
    assert shape.parse_failed is False


def test_count_command_shape_bare_builtin_no_fork():
    shape = fork_census.count_command_shape("echo hi")
    assert shape.external_forks == 0
    assert shape.builtin_invocations == 1


def test_count_command_shape_cd_then_git_shape():
    shape = fork_census.count_command_shape("cd /tmp && git log")
    assert shape.external_forks == 1
    assert shape.builtin_invocations == 1
    assert shape.cd_seen is True
    assert shape.cd_then_git is True
    assert shape.git_dash_c is False


def test_count_command_shape_git_dash_c_idiomatic():
    shape = fork_census.count_command_shape("git -C /tmp status")
    assert shape.external_forks == 1
    assert shape.cd_then_git is False
    assert shape.git_dash_c is True


def test_count_command_shape_pipeline_counts_each_segment():
    shape = fork_census.count_command_shape("git log | head -5")
    assert shape.external_forks == 2
    assert shape.binaries == {"git": 1, "head": 1}


def test_count_command_shape_windows_exe_suffix_normalizes():
    shape = fork_census.count_command_shape("git.exe status")
    assert shape.binaries == {"git": 1}


def test_count_command_shape_heredoc_body_excluded():
    cmd = "cat > out.txt <<'EOF'\ngit status\nEOF\n"
    shape = fork_census.count_command_shape(cmd)
    # Only "cat" forks; the heredoc body's "git status" text is stdin data,
    # never a second command.
    assert shape.external_forks == 1
    assert shape.binaries == {"cat": 1}


def test_count_command_shape_unparseable_fails_toward_zero():
    shape = fork_census.count_command_shape("echo 'unterminated")
    assert shape.parse_failed is True
    assert shape.external_forks == 0
    assert shape.builtin_invocations == 0


# ---------------------------------------------------------------------------
# (c) run_fork_census over a fixture corpus
# ---------------------------------------------------------------------------


def _bash_tool_use(tool_id: str, command: str, role: str = "assistant", cwd: str = None) -> str:
    rec = {
        "message": {
            "role": role,
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
            ],
        },
    }
    if cwd is not None:
        rec["cwd"] = cwd
    return json.dumps(rec)


def _tool_result(tool_id: str, text: str, cwd: str = None) -> str:
    rec = {
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": [{"type": "text", "text": text}]}
            ],
        },
    }
    if cwd is not None:
        rec["cwd"] = cwd
    return json.dumps(rec)


def _cwd_marker(cwd: str) -> str:
    return json.dumps({"message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}, "cwd": cwd})


@pytest.fixture()
def fixture_corpus(tmp_path: Path) -> Path:
    """Builds a two-machine fixture corpus with hand-computed expected totals.

    macOS session (proj-a/s1.jsonl), cwd=/Users/alice/X/coordinator-claude:
      t1 "git status"              -> 1 external fork (git)
      t2 "echo hi"                 -> 1 builtin, 0 forks
      t3 "cd /tmp && git log"      -> 1 builtin (cd) + 1 fork (git), cd_then_git
      t4 "git -C /tmp status"      -> 1 fork (git), git_dash_c
      t5 "rm -rf /some/scratch"    -> 1 fork (rm), DENIED (paired BLOCKED result)
    Expected macOS totals: bash_tool_calls=5, external_forks=4,
      builtin_invocations=2, cd_git_shaped=1, git_dash_c_idiomatic=1,
      denies_observed=1, advisory_conversion_rate=0.5.

    Windows session (proj-b/s2.jsonl), cwd=C:\\Users\\bob\\project:
      w1 "git status"              -> 1 external fork (git)
      w2 "cd C:\\tmp && git log"   -> 1 builtin (cd) + 1 fork (git), cd_then_git
    Expected windows totals: bash_tool_calls=2, external_forks=2,
      builtin_invocations=1, cd_git_shaped=1, git_dash_c_idiomatic=0,
      denies_observed=0, advisory_conversion_rate=None (Windows never gets
      a computed ratio here — see module docstring AC-14).
    """
    base = tmp_path / "projects"
    proj_a = base / "proj-a"
    proj_a.mkdir(parents=True)
    proj_b = base / "proj-b"
    proj_b.mkdir(parents=True)

    macos_cwd = "/Users/alice/X/coordinator-claude"
    lines_a = [
        _cwd_marker(macos_cwd),
        _bash_tool_use("t1", "git status"),
        _bash_tool_use("t2", "echo hi"),
        _bash_tool_use("t3", "cd /tmp && git log"),
        _bash_tool_use("t4", "git -C /tmp status"),
        _bash_tool_use("t5", "rm -rf /some/scratch"),
        _tool_result("t5", "BLOCKED (destructive-action guard): denied"),
    ]
    (proj_a / "s1.jsonl").write_text("\n".join(lines_a) + "\n", encoding="utf-8")

    windows_cwd = "C:\\Users\\bob\\project"
    lines_b = [
        _cwd_marker(windows_cwd),
        _bash_tool_use("w1", "git status"),
        _bash_tool_use("w2", "cd C:\\tmp && git log"),
    ]
    (proj_b / "s2.jsonl").write_text("\n".join(lines_b) + "\n", encoding="utf-8")

    return base


def test_run_fork_census_reports_per_machine_not_pooled(fixture_corpus: Path):
    result = fork_census.run_fork_census(base_dir=fixture_corpus)

    assert set(result.keys()) >= {"corpus", "by_machine", "pooled_for_reference_only"}
    assert set(result["by_machine"].keys()) == {"macos", "windows"}

    macos = result["by_machine"]["macos"]
    assert macos["bash_tool_calls"] == 5
    assert macos["external_binary_forks"] == 4
    assert macos["builtin_invocations"] == 2
    assert macos["total_process_creations"] == 6
    assert macos["advisory"]["cd_git_shaped_commands"] == 1
    assert macos["advisory"]["git_dash_c_idiomatic_commands"] == 1
    assert macos["advisory"]["advisory_conversion_rate"] == pytest.approx(0.5)
    assert macos["denies_observed"] == 1
    assert macos["top_forked_binaries"][0] == ("git", 3)

    windows = result["by_machine"]["windows"]
    assert windows["bash_tool_calls"] == 2
    assert windows["external_binary_forks"] == 2
    assert windows["builtin_invocations"] == 1
    assert windows["advisory"]["cd_git_shaped_commands"] == 1
    assert windows["advisory"]["git_dash_c_idiomatic_commands"] == 0
    # AC-14: Windows never gets a computed advisory-conversion ratio — the
    # cd+git shape hits a hard deny there, not the macOS-shaped advisory.
    assert windows["advisory"]["advisory_conversion_rate"] is None
    assert windows["denies_observed"] == 0

    assert result["corpus"]["transcript_files_scanned"] == 2
    assert result["corpus"]["bash_tool_calls_total"] == 7


def test_run_fork_census_pooled_is_sum_but_not_the_primary_read(fixture_corpus: Path):
    """Pooled is a convenience cross-check, never a substitute for the
    per-machine breakdown (AC-1) — this test only asserts internal
    consistency (pooled == sum of machines), not that pooled is sufficient.
    """
    result = fork_census.run_fork_census(base_dir=fixture_corpus)
    pooled = result["pooled_for_reference_only"]
    macos = result["by_machine"]["macos"]
    windows = result["by_machine"]["windows"]
    assert pooled["bash_tool_calls"] == macos["bash_tool_calls"] + windows["bash_tool_calls"]
    assert (
        pooled["external_binary_forks"]
        == macos["external_binary_forks"] + windows["external_binary_forks"]
    )


def test_run_fork_census_empty_base_dir_returns_empty_shape(tmp_path: Path):
    empty = tmp_path / "empty-projects"
    empty.mkdir()
    result = fork_census.run_fork_census(base_dir=empty)
    assert result["by_machine"] == {}
    assert result["corpus"]["bash_tool_calls_total"] == 0


# ---------------------------------------------------------------------------
# (d) op registration
# ---------------------------------------------------------------------------


def test_op_self_registers_and_matches_direct_call(fixture_corpus: Path):
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler("probes.fork_census")
    assert handler is not None

    direct = fork_census.run_fork_census(base_dir=fixture_corpus)
    via_op = asyncio.run(handler({"base_dir": str(fixture_corpus)}))
    assert via_op == direct
