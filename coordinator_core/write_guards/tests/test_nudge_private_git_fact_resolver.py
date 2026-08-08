"""Behavioral tests for coordinator_core.write_guards.nudge_private_git_fact_resolver.

Covers docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md § D3
(AC4, AC5):

  1. FIRES on a planted private resolver for EACH of the three walk-backed
     forms (--show-toplevel, --git-dir, --git-common-dir) in a hot-path module.
  2. FIRES on the exact `--path-format=absolute --git-common-dir` membership
     shape (baseline audit scenario-iv live-measured invocation) — proves
     detection is by argv MEMBERSHIP, not position/adjacency.
  3. SILENT on a module that already imports the shared seam.
  4. SILENT on a non-hot-path module (same private-resolver content).
  5. SILENT on a planted fixture per always-spawning form (--show-prefix,
     --absolute-git-dir, --is-inside-work-tree) — one fixture EACH.
  6. Envelope shape is ADVISORY (additionalContext), never permissionDecision.
  7. Fail-open: unparseable Python source does not raise.
  8. SILENT on a test-tree site even with a fire-set flag present.
  9. Oversized content does not fire.

Grep anchors: SPAWN-STORM-CULPRIT-TAXONOMY D3
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import nudge_private_git_fact_resolver as guard


def _payload(tool_name, tool_input):
    return {"tool_name": tool_name, "tool_input": tool_input}


def _is_advisory_envelope(result: dict) -> bool:
    hso = result.get("hookSpecificOutput", {})
    return (
        hso.get("hookEventName") == "PreToolUse"
        and "additionalContext" in hso
        and "permissionDecision" not in hso
    )


_HOT_PATH_FILE = "/repo/coordinator_core/write_guards/some_new_guard.py"
_NON_HOT_PATH_FILE = "/repo/coordinator_core/ops/lifecycle.py"
_TEST_TREE_FILE = "/repo/coordinator_core/write_guards/tests/test_some_guard.py"


def _private_resolver_source(flag: str) -> str:
    return (
        "import subprocess\n"
        "def _resolve():\n"
        f"    subprocess.run(['git', 'rev-parse', '{flag}'], capture_output=True)\n"
    )


# --- FIRE SET: the three walk-backed forms ------------------------------


@pytest.mark.parametrize(
    "flag,expected_symbol",
    [
        ("--show-toplevel", "coordinator_core.git.repo_root.show_toplevel"),
        ("--git-dir", "coordinator_core.git.repo_root.git_dir"),
        ("--git-common-dir", "coordinator_core.git.repo_root.git_common_dir"),
    ],
)
def test_fires_on_each_walk_backed_form_in_hot_path_module(flag, expected_symbol):
    content = _private_resolver_source(flag)
    result = guard.check(
        _payload("Write", {"file_path": _HOT_PATH_FILE, "content": content})
    )
    assert result is not None
    assert _is_advisory_envelope(result)
    text = result["hookSpecificOutput"]["additionalContext"]
    assert expected_symbol in text
    assert flag in text
    assert "eliminates the spawn" not in text
    assert "cheaper in the ordinary case, never more expensive" in text


def test_fires_on_live_measured_path_format_absolute_git_common_dir_membership():
    """REQUIRED FIXTURE: the baseline audit's scenario-iv enumeration records
    the live-measured invocation as `git rev-parse --path-format=absolute
    --git-common-dir` -- the discriminating flag is NEITHER the second argv
    element NOR adjacent to `rev-parse`. A positional/adjacency-based
    detector would silently miss this exact, measured form."""
    content = (
        "import subprocess\n"
        "def _resolve():\n"
        "    subprocess.run(\n"
        "        ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],\n"
        "        capture_output=True,\n"
        "    )\n"
    )
    result = guard.check(
        _payload("Write", {"file_path": _HOT_PATH_FILE, "content": content})
    )
    assert result is not None
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "coordinator_core.git.repo_root.git_common_dir" in text


# --- SILENT SET: benign cases must not nag ------------------------------


def test_silent_when_module_already_imports_the_shared_seam():
    content = (
        "from coordinator_core.git.repo_root import show_toplevel\n"
        "def _resolve():\n"
        "    return show_toplevel()\n"
    )
    result = guard.check(
        _payload("Write", {"file_path": _HOT_PATH_FILE, "content": content})
    )
    assert result is None


def test_silent_on_non_hot_path_module():
    content = _private_resolver_source("--show-toplevel")
    result = guard.check(
        _payload("Write", {"file_path": _NON_HOT_PATH_FILE, "content": content})
    )
    assert result is None


@pytest.mark.parametrize(
    "flag", ["--show-prefix", "--absolute-git-dir", "--is-inside-work-tree"]
)
def test_silent_on_each_always_spawning_form(flag):
    """These three forms ALWAYS spawn per coordinator_core/git/repo_root.py's
    own docstring -- the offered seam is fork-equal or a documented semantic
    trap for each, so this guard must stay silent rather than mis-claim
    cheapness (see nudge_private_git_fact_resolver.py's AC5 HONESTY note)."""
    content = _private_resolver_source(flag)
    result = guard.check(
        _payload("Write", {"file_path": _HOT_PATH_FILE, "content": content})
    )
    assert result is None


def test_silent_on_test_tree_site_even_with_fire_flag():
    content = _private_resolver_source("--show-toplevel")
    result = guard.check(
        _payload("Write", {"file_path": _TEST_TREE_FILE, "content": content})
    )
    assert result is None


def test_silent_on_plain_non_git_subprocess_call():
    content = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['ls', '-la'])\n"
    )
    result = guard.check(
        _payload("Write", {"file_path": _HOT_PATH_FILE, "content": content})
    )
    assert result is None


def test_silent_on_non_py_files():
    content = _private_resolver_source("--show-toplevel")
    result = guard.check(
        _payload("Write", {"file_path": "/repo/coordinator_core/write_guards/notes.md", "content": content})
    )
    assert result is None


def test_fail_open_on_unparseable_source():
    content = "def f(:\n    this is not python\n"
    result = guard.check(
        _payload("Write", {"file_path": _HOT_PATH_FILE, "content": content})
    )
    assert result is None


def test_silent_on_oversized_content():
    content = _private_resolver_source("--show-toplevel") + ("# padding\n" * (guard._MAX_WHOLE_FILE_BYTES // 8))
    result = guard.check(
        _payload("Write", {"file_path": _HOT_PATH_FILE, "content": content})
    )
    assert result is None


def test_envelope_never_carries_permission_decision():
    content = _private_resolver_source("--git-dir")
    result = guard.check(
        _payload("Write", {"file_path": _HOT_PATH_FILE, "content": content})
    )
    assert result is not None
    assert "permissionDecision" not in result["hookSpecificOutput"]
