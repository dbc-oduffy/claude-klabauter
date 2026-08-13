"""Tests for coordinator_core.ops.check_posix_exec_assumptions.

Covers all five BLOCKING violation classes with RED-case fixtures (a temp
git repo seeded with a deliberately violating file, per DR: "a guard that
has never been observed to fail is not verified" -- 2026-06-17's
check-windows-python-shebang.sh shipped globbing *.py only and never once
caught the extensionless CLIs it existed to protect; this suite exercises
every detector against real violating fixtures, not just the clean
real-tree case) plus the two REPORT-ONLY classes, plus the ratchet
(ok/new-violation) and append-forbidden (baseline growth) contracts, plus a
real-tree wiring check against this repo's own committed baseline.

Classes 4-5 (path_separator, posix_mode_bits) and the report-only classes
(unresolved_cross_path, unclassified) are AST-scanned Python-code patterns,
so each gets BOTH a red-case fixture (detector fires) AND a false-positive
fixture proving the correct/idiomatic form does NOT fire (os.sep,
os.path.join, pathlib.Path, a Windows-guarded os.access call is still
flagged as debt -- see module docstring -- but the *fixed* form using
os.path.join must not be).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.ops import check_posix_exec_assumptions  # noqa: E402
from coordinator_core.ops.check_posix_exec_assumptions import (  # noqa: E402
    ALL_CLASSES,
    CLASSES,
    EXEMPT_PREFIXES,
    EXEMPTIONS,
    IMPLICIT_ENCODING_CLASSES,
    PREFIX_EXCLUDABLE_CLASSES,
    REPO_EXAMPLE_DOCTRINE_REPO,
    REPO_CLAUDE_KLABAUTER,
    REPORT_ONLY_CLASSES,
    TIER_A_CLASSES,
    assert_baseline_not_grown,
    check_against_baseline,
    check_implicit_encoding_zero_tolerance,
    check_no_stale_baseline_entries,
    check_no_stale_exempt_prefixes,
    check_tier_a_zero_tolerance,
    is_prefix_excluded,
    main,
    repo_key_for_root,
    scan,
)
from coordinator_core.testing import symlink_capability

# Declared, not excused: this file spawns a real git process because the
# detectors under test read real git-index state -- mode bits (100755 vs
# 100644), symlink blobs, and case-collision entries -- that cannot be
# produced by a mock filesystem, only by a real `git add`/index write. Each
# test seeds its own repo with a specific violating (or non-violating)
# fixture via `_init_repo`, so the fixture is not hoisted to module scope --
# per-test isolation across dozens of distinct violation-class scenarios.
# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root)] + list(args),
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit_all(repo: Path, message: str = "seed") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


# ---------------------------------------------------------------------------
# Class 1: env-stripped shebang.
# ---------------------------------------------------------------------------

def test_scan_detects_env_shebang_violation(tmp_path):
    repo = _init_repo(tmp_path)
    victim = repo / "tool.sh"
    victim.write_text("#!/usr/bin/env bash\necho hi\n")
    _commit_all(repo)

    result = scan(repo)

    assert "tool.sh" in result["env_shebang"]


def test_scan_does_not_flag_direct_shebang(tmp_path):
    """A direct (non-env) shebang like `#!/bin/bash` is NOT this class --
    negative control proving the detector isn't over-broad."""
    repo = _init_repo(tmp_path)
    (repo / "tool.sh").write_text("#!/bin/bash\necho hi\n")
    _commit_all(repo)

    result = scan(repo)

    assert "tool.sh" not in result["env_shebang"]


# ---------------------------------------------------------------------------
# Class 2: extensionless #!-executable.
# ---------------------------------------------------------------------------

def test_scan_detects_extensionless_exec_violation(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "my-cli").write_text("#!/bin/sh\necho hi\n")
    _commit_all(repo)

    result = scan(repo)

    assert "my-cli" in result["extensionless_exec"]


def test_scan_does_not_flag_extensioned_shebang_file(tmp_path):
    """A `.py`/`.sh`-suffixed shebang file is NOT this class -- negative
    control: this is exactly the gap check-windows-python-shebang.sh's
    *.py-only glob left open (it never scanned extensionless CLIs), inverted
    here to prove THIS detector's boundary is drawn on purpose."""
    repo = _init_repo(tmp_path)
    (repo / "my-cli.sh").write_text("#!/bin/sh\necho hi\n")
    _commit_all(repo)

    result = scan(repo)

    assert "my-cli.sh" not in result["extensionless_exec"]


def test_scan_does_not_flag_extensionless_non_shebang_file(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "README").write_text("just a readme, no shebang\n")
    _commit_all(repo)

    result = scan(repo)

    assert "README" not in result["extensionless_exec"]


# ---------------------------------------------------------------------------
# Class 3: git mode 100755.
# ---------------------------------------------------------------------------

def test_scan_detects_mode_100755_violation(tmp_path):
    repo = _init_repo(tmp_path)
    victim = repo / "data.txt"
    victim.write_text("not even a script\n")
    victim.chmod(0o755)
    _commit_all(repo)

    result = scan(repo)

    assert "data.txt" in result["mode_100755"]


def test_scan_does_not_flag_mode_100644(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "data.txt").write_text("not a script\n")
    _commit_all(repo)

    result = scan(repo)

    assert "data.txt" not in result["mode_100755"]


def test_scan_all_three_classes_are_independent_tells(tmp_path):
    """A single file can trip more than one class at once (e.g. an
    extensionless env-shebang executable at mode 100755) -- classes are not
    mutually exclusive categories, matching the module docstring."""
    repo = _init_repo(tmp_path)
    victim = repo / "combo-cli"
    victim.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    victim.chmod(0o755)
    _commit_all(repo)

    result = scan(repo)

    assert "combo-cli" in result["env_shebang"]
    assert "combo-cli" in result["extensionless_exec"]
    assert "combo-cli" in result["mode_100755"]


# ---------------------------------------------------------------------------
# Tier A: checkout-breaker classes (exact, zero-tolerance, no baseline).
# ---------------------------------------------------------------------------

@symlink_capability.requires_symlink_capability
def test_scan_detects_symlink_in_index(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "target.txt").write_text("real file\n")
    (repo / "link.txt").symlink_to("target.txt")
    _commit_all(repo)

    result = scan(repo)

    assert "link.txt" in result["symlink_in_index"]


def test_scan_does_not_flag_regular_file_as_symlink(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "plain.txt").write_text("not a symlink\n")
    _commit_all(repo)

    result = scan(repo)

    assert "plain.txt" not in result["symlink_in_index"]


def test_scan_detects_case_collision(tmp_path):
    """Two blobs added directly to the git index under case-differing
    names -- writing both through the filesystem doesn't work as a fixture
    on a case-insensitive host filesystem (macOS/Windows), where the second
    write silently collapses onto the first; the git index itself has no
    such restriction, which is exactly why this class exists."""
    repo = _init_repo(tmp_path)
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="collision fixture\n",
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    ).stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},Readme.md")
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},readme.md")
    _git(repo, "commit", "-q", "-m", "seed case collision")

    result = scan(repo)

    assert "Readme.md" in result["case_collision"]
    assert "readme.md" in result["case_collision"]


def test_scan_does_not_flag_distinct_names_as_case_collision(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "readme.md").write_text("one\n")
    (repo / "changelog.md").write_text("two\n")
    _commit_all(repo)

    result = scan(repo)

    assert "readme.md" not in result["case_collision"]
    assert "changelog.md" not in result["case_collision"]


def test_scan_detects_reserved_device_name(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "con.txt").write_text("reserved on Windows\n")
    _commit_all(repo)

    result = scan(repo)

    assert "con.txt" in result["reserved_filename"]


def test_scan_detects_reserved_device_name_in_subdirectory(tmp_path):
    repo = _init_repo(tmp_path)
    sub = repo / "logs"
    sub.mkdir()
    (sub / "aux").write_text("reserved component, not just basename\n")
    _commit_all(repo)

    result = scan(repo)

    assert "logs/aux" in result["reserved_filename"]


def test_scan_detects_forbidden_path_character(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "report<final>.txt").write_text("forbidden char on Windows\n")
    _commit_all(repo)

    result = scan(repo)

    assert "report<final>.txt" in result["reserved_filename"]


def test_scan_does_not_flag_ordinary_filename_as_reserved(tmp_path):
    """Negative control: a name merely CONTAINING a reserved word (e.g.
    'console.py') must not be flagged -- only the exact stem matches."""
    repo = _init_repo(tmp_path)
    (repo / "console.py").write_text("not reserved -- 'con' is a substring, not the stem\n")
    _commit_all(repo)

    result = scan(repo)

    assert "console.py" not in result["reserved_filename"]


def test_scan_detects_path_too_long(tmp_path):
    repo = _init_repo(tmp_path)
    deep = repo
    long_name = "x" * 220
    (repo / long_name).write_text("too long once a clone root is prepended\n")
    _commit_all(repo)

    result = scan(repo)

    assert long_name in result["path_too_long"]


def test_scan_does_not_flag_short_path(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "short.txt").write_text("fine\n")
    _commit_all(repo)

    result = scan(repo)

    assert "short.txt" not in result["path_too_long"]


def test_check_tier_a_zero_tolerance_passes_on_clean_repo(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "fine.txt").write_text("nothing wrong here\n")
    _commit_all(repo)

    ok, msg = check_tier_a_zero_tolerance(repo)

    assert ok is True
    assert "OK" in msg


def test_check_tier_a_zero_tolerance_fails_on_any_violation_no_baseline_needed(tmp_path):
    """Tier A has NO baseline concept -- a single violation fails
    immediately, unlike CLASSES/REPORT_ONLY_CLASSES which compare against a
    frozen baseline file."""
    repo = _init_repo(tmp_path)
    (repo / "con.txt").write_text("reserved\n")
    _commit_all(repo)

    ok, msg = check_tier_a_zero_tolerance(repo)

    assert ok is False
    assert "con.txt" in msg
    assert "reserved_filename" in msg


# ---------------------------------------------------------------------------
# implicit_encoding: zero-tolerance, separate failure axis from Tier A
# (data corruption, not checkout-breaking), deliberately scoped to bare
# open() only -- NOT .open()/.read_text()/.write_text() attribute calls,
# which need receiver-type tracking to avoid firing on any object exposing
# those method names (a ~400-hit false-positive probe on this fleet).
# ---------------------------------------------------------------------------

def test_scan_detects_implicit_encoding(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def read(p):\n    with open(p, 'r') as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["implicit_encoding"]


def test_scan_detects_implicit_encoding_default_mode(tmp_path):
    """No mode argument at all -- default 'r' is text, still a violation."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def read(p):\n    with open(p) as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["implicit_encoding"]


def test_scan_does_not_flag_open_with_encoding(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def read(p):\n    with open(p, 'r', encoding='utf-8') as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["implicit_encoding"]


def test_scan_does_not_flag_binary_mode_open(tmp_path):
    """Negative control: 'rb'/'wb' are byte streams -- encoding is
    meaningless there, not a violation."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def read(p):\n    with open(p, 'rb') as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["implicit_encoding"]


def test_scan_does_not_flag_pathlib_open_attribute_call(tmp_path):
    """Deliberate scope boundary: `Path(...).open()` (an attribute call,
    not a bare `open()` Name call) is NOT matched -- this class is
    scoped to the builtin `open()` form only, precisely because matching
    on the attribute name alone (with no receiver-type confirmation)
    produced a ~400-hit false-positive bucket in a probe of this fleet."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "from pathlib import Path\n\n"
        "def read(p):\n    with Path(p).open('r') as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["implicit_encoding"]


def test_scan_does_not_flag_open_with_variable_mode_argument(tmp_path):
    """Negative-spec pinning the 2026-08-03 narrowing: a mode passed through
    a variable cannot be classified as binary or text at all, so it must
    never produce a finding -- the pre-fix behaviour treated an unresolved
    mode as "not binary" and false-fired on a caller genuinely passing a
    binary mode through. A future widen that goes back to guessing must fail
    this test loudly."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def read(p, mode):\n    with open(p, mode) as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["implicit_encoding"]


def test_scan_does_not_flag_open_with_variable_mode_keyword(tmp_path):
    """Same narrowing, keyword-argument spelling (`open(p, mode=m)`)."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def read(p, m):\n    with open(p, mode=m) as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["implicit_encoding"]


def test_check_implicit_encoding_zero_tolerance_passes_on_clean_repo(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def read(p):\n    with open(p, 'r', encoding='utf-8') as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    ok, msg = check_implicit_encoding_zero_tolerance(repo)

    assert ok is True
    assert "OK" in msg


def test_check_implicit_encoding_zero_tolerance_fails_on_any_violation(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def read(p):\n    with open(p) as f:\n        return f.read()\n"
    )
    _commit_all(repo)

    ok, msg = check_implicit_encoding_zero_tolerance(repo)

    assert ok is False
    assert "tool.py" in msg
    assert "implicit_encoding" in msg


# ---------------------------------------------------------------------------
# Class 4: hardcoded path separator.
# ---------------------------------------------------------------------------

def test_scan_detects_path_separator_replace_normalization_hack(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def norm(p):\n    return p.replace('/', '\\\\')\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["path_separator"]


def test_scan_detects_path_separator_literal_backslash_concat(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def build(a, b):\n    return a + '\\\\' + b\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["path_separator"]


def test_scan_detects_path_separator_manual_split(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def parts(p):\n    return p.split('\\\\')\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["path_separator"]


def test_scan_does_not_flag_os_path_join(tmp_path):
    """Negative control: os.path.join is THE FIX, not the violation -- must
    be structurally invisible to the path_separator detector."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef build(a, b):\n    return os.path.join(a, b)\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["path_separator"]


def test_scan_does_not_flag_pathlib_usage(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "from pathlib import Path\n\ndef build(a, b):\n    return Path(a) / b\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["path_separator"]


def test_scan_does_not_flag_os_sep(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef build(a, b):\n    return a + os.sep + b\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["path_separator"]


# ---------------------------------------------------------------------------
# Class 5: runtime POSIX permission-bit reasoning.
# ---------------------------------------------------------------------------

def test_scan_detects_os_access_x_ok_bare_name_form(tmp_path):
    """`from os import X_OK` -> bare-`Name` `X_OK` reference, distinct from
    the `os.X_OK` attribute form already covered above -- both are matched
    by `_is_owner_attr`'s sibling `isinstance(arg, ast.Name)` check."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\nfrom os import X_OK\n\n"
        "def check(path):\n    return os.access(path, X_OK)\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["posix_mode_bits"]


def test_scan_detects_os_access_x_ok(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef check(p):\n    return os.access(p, os.X_OK)\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["posix_mode_bits"]


def test_scan_detects_os_chmod_with_exec_bit(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef make_exec(p):\n    os.chmod(p, 0o755)\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["posix_mode_bits"]


def test_scan_detects_st_mode_exec_bit_test(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef is_exec(p):\n    return os.stat(p).st_mode & 0o111 != 0\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["posix_mode_bits"]


def test_scan_detects_stat_module_exec_constant(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\nimport stat\n\ndef make_exec(p):\n    os.chmod(p, stat.S_IRWXU | stat.S_IXGRP)\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["posix_mode_bits"]


def test_scan_does_not_flag_os_chmod_without_exec_bit(tmp_path):
    """Negative control: 0o644 (rw-r--r--) sets no exec bit -- correct form,
    not a violation."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef make_readonly(p):\n    os.chmod(p, 0o644)\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["posix_mode_bits"]


def test_scan_does_not_flag_unrelated_bitand(tmp_path):
    """Negative control: a bitwise-AND that has nothing to do with
    .st_mode must not trip the detector."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def mask(flags):\n    return flags & 0o111\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["posix_mode_bits"]


# ---------------------------------------------------------------------------
# Platform-guard recognition (2026-07-28 precision fix): posix_mode_bits and
# path_separator must not fire on code that is structurally unreachable on
# Windows -- that is the correct cross-platform shape, not debt.
# ---------------------------------------------------------------------------

def test_scan_does_not_flag_os_access_inside_nested_if_guard(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\n"
        "def check(p):\n"
        "    if os.name != 'nt':\n"
        "        return os.access(p, os.X_OK)\n"
        "    return True\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["posix_mode_bits"]


def test_scan_does_not_flag_os_access_inside_and_chain_guard(tmp_path):
    """The real shape found in retire-claude-bin.py: the platform check and
    the guarded call are short-circuit operands of the SAME boolean
    expression, not a separate nested If."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\n"
        "def check(p, suffix):\n"
        "    if os.name != 'nt' and not suffix and not os.access(p, os.X_OK):\n"
        "        return False\n"
        "    return True\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["posix_mode_bits"]


def test_scan_still_flags_os_access_when_windows_branch_calls_it(tmp_path):
    """Positive control: if the platform check is inverted so the guarded
    call is in the WINDOWS-only branch, it's real debt -- the guard must
    not blanket-suppress every branch of an if/else."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\n"
        "def check(p):\n"
        "    if os.name == 'nt':\n"
        "        return os.access(p, os.X_OK)\n"
        "    return True\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["posix_mode_bits"]


def test_scan_does_not_flag_path_separator_replace_inside_guard(tmp_path):
    """Covers the explicit if/else branch shape. NOTE a real, stated
    limitation: an early-return guard clause (`if sys.platform.startswith
    ('win'): return p` followed by the guarded code as the NEXT sibling
    statement, no `else:`) is NOT caught -- that idiom is not a nested
    `If.orelse` in the AST at all, just a subsequent statement, and
    detecting it needs real control-flow/reachability analysis rather than
    a parent-chain walk. `_is_windows_guarded` only recognizes the explicit
    branch shape below; the early-return idiom remains a false positive
    until a future pass adds reachability analysis."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import sys\n\n"
        "def norm(p):\n"
        "    if sys.platform.startswith('win'):\n"
        "        return p\n"
        "    else:\n"
        "        return p.replace('/', '\\\\')\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["path_separator"]


def test_scan_still_flags_ungated_os_access(tmp_path):
    """Positive control: the original unguarded fixture must still fire --
    the platform-guard fix narrows the detector, it must not disable it."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef check(p):\n    return os.access(p, os.X_OK)\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["posix_mode_bits"]


# ---------------------------------------------------------------------------
# Report-only class: unresolved_cross_path.
# ---------------------------------------------------------------------------

def test_scan_detects_unresolved_cross_path_current_machine_home_macos(
    tmp_path, monkeypatch
):
    """A literal matching THIS machine's operator-home (macOS shape) is the
    class's genuine subject -- narrowed 2026-08-13, Q2.1 ruling."""
    monkeypatch.setenv("HOME", "/Users/alice")
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        'def default_root():\n    return "/Users/alice/repo"\n'
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["unresolved_cross_path"]


def test_scan_does_not_flag_synthetic_home_literal_off_current_machine(
    tmp_path, monkeypatch
):
    """A synthetic `/Users/alice` fixture literal that does NOT match the
    CURRENT machine's own `$HOME` is test data, not a leak -- the narrowing
    this ruling adds, distinct from the drive-letter drop. Same shape as
    `test_scan_detects_unresolved_cross_path_current_machine_home_macos`
    except `$HOME` is a different user, proving the current-machine
    discrimination actually gates the finding."""
    monkeypatch.setenv("HOME", "/Users/bob")
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        'def default_root():\n    return "/Users/alice/repo"\n'
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["unresolved_cross_path"]


def test_scan_does_not_flag_windows_drive_letter_literal(tmp_path, monkeypatch):
    """The `<drive-letter>:[\\/]` arm was DROPPED ENTIRELY 2026-08-13
    (Q2.1 ruling): a live re-measurement found it responsible for 127/178
    findings, almost all this repo's own correct Windows-portability code
    (`pyresolve.py`'s interpreter-discovery ladder, `break_glass.py`'s
    Windows temp-path syntax recognition) being flagged for existing.
    `$HOME` is pinned to the literal's own value so this negative result
    is provably about the drive-letter drop, not an accidental miss on the
    current-machine-home narrowing."""
    monkeypatch.setenv("HOME", "C:\\Users\\alice")
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        'def default_root():\n    return "C:\\\\Users\\\\alice\\\\repo"\n'
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["unresolved_cross_path"]


def test_scan_does_not_flag_settings_home_resolved_path(tmp_path):
    """Negative control: resolving via COORDINATOR_SETTINGS_HOME / os.environ
    is the fix -- no hardcoded cross-machine literal appears in the AST."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\n"
        "def settings_home():\n"
        "    return os.environ.get('COORDINATOR_SETTINGS_HOME') or "
        "os.path.join(os.path.expanduser('~'), '.coordinator-claude-settings')\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["unresolved_cross_path"]


def test_scan_does_not_flag_cross_path_literal_in_docstring(tmp_path, monkeypatch):
    """Negative control: a documented example path in a module docstring is
    documentation, not a hardcoded runtime assumption. `$HOME` is pinned to
    match the literal so this is a genuine test of the docstring exclusion,
    not an incidental miss from the current-machine-home narrowing."""
    monkeypatch.setenv("HOME", "/Users/alice")
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        '"""Example: /Users/alice/repo/file.txt is the macOS layout."""\n'
        "\n\ndef noop():\n    pass\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["unresolved_cross_path"]


def test_scan_detects_unresolved_cross_path_current_machine_home_linux(
    tmp_path, monkeypatch
):
    """A literal matching THIS machine's operator-home (Linux shape)."""
    monkeypatch.setenv("HOME", "/home/alice")
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        'def default_root():\n    return "/home/alice/repo"\n'
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["unresolved_cross_path"]


def test_scan_does_not_flag_unc_share_literal(tmp_path):
    """UNC (`\\\\server\\share`) is not a home-rooted literal and was never
    part of the narrowed, kept "home-rooted arm" (Q2.1 ruling) -- it never
    starts with a `$HOME` value either, so it cannot pass the
    current-machine narrowing regardless."""
    repo = _init_repo(tmp_path)
    literal = repr(r"\\server\share\repo")
    (repo / "tool.py").write_text(f"def default_root():\n    return {literal}\n")
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["unresolved_cross_path"]


def test_scan_does_not_flag_url_with_single_letter_looking_scheme(tmp_path):
    """Negative control: a naive UNANCHORED `[A-Za-z]:[/\\\\]` matches the
    "s:" inside "https://" -- a drive-letter false-positive this fleet has
    hit before. The detector's pattern is anchored to the absolute start of
    the string (`^`, no re.MULTILINE), so it requires the constant to
    ITSELF begin with a single letter, colon, slash -- no real URL scheme
    is one character, so a full URL string must never fire."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        'def endpoint():\n    return "https://example.com/api/v1"\n'
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["unresolved_cross_path"]


def test_scan_does_not_flag_short_scheme_url_variants(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "def endpoints():\n"
        '    a = "ws://localhost:9000"\n'
        '    b = "s3://bucket/key"\n'
        '    return a, b\n'
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["unresolved_cross_path"]


# ---------------------------------------------------------------------------
# Report-only class: unclassified residual.
# ---------------------------------------------------------------------------

def test_scan_detects_unclassified_os_fork(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef spawn():\n    return os.fork()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["unclassified"]


def test_scan_detects_unclassified_posix_signal(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import signal\n\ndef handler():\n    return signal.SIGHUP\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["unclassified"]


@pytest.mark.parametrize(
    "call_expr",
    [
        "os.uname()",
        "os.geteuid()",
        "os.getegid()",
        "os.setuid(0)",
        "os.setgid(0)",
        "pwd.getpwnam('root')",
        "pwd.getpwuid(0)",
        "grp.getgrnam('root')",
        "grp.getgrgid(0)",
    ],
)
def test_scan_detects_unclassified_call_targets_beyond_os_fork(tmp_path, call_expr):
    """`_UNCLASSIFIED_CALL_TARGETS` had every entry beyond `os.fork` with
    zero fixture coverage -- each of these is one of the untested arms
    (review 2026-07-28 Finding 4)."""
    repo = _init_repo(tmp_path)
    module = call_expr.split(".", 1)[0]
    (repo / "tool.py").write_text(
        f"import {module}\n\ndef call():\n    return {call_expr}\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["unclassified"]


@pytest.mark.parametrize("signal_name", ["SIGUSR1", "SIGUSR2", "SIGCHLD"])
def test_scan_detects_unclassified_signal_names_beyond_sighup(tmp_path, signal_name):
    """`_UNCLASSIFIED_SIGNAL_NAMES` had only `SIGHUP` exercised -- the other
    three names were untested detector arms (review 2026-07-28 Finding 4)."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        f"import signal\n\ndef handler():\n    return signal.{signal_name}\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["unclassified"]


def test_scan_detects_unclassified_hardcoded_tmp(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        'def scratch():\n    return "/tmp/scratch.txt"\n'
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" in result["unclassified"]


def test_scan_does_not_flag_tempfile_gettempdir(tmp_path):
    """Negative control: tempfile.gettempdir() is the fix, not a
    violation -- no hardcoded /tmp/ literal appears in the AST."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import tempfile\n\ndef scratch():\n    return tempfile.gettempdir()\n"
    )
    _commit_all(repo)

    result = scan(repo)

    assert "tool.py" not in result["unclassified"]


# ---------------------------------------------------------------------------
# Report-only contract: never gates the build.
# ---------------------------------------------------------------------------

def test_report_only_classes_never_fail_check_against_baseline(tmp_path):
    """A report-only violation with NOTHING in the baseline must still
    report ok=True -- report-only classes are scanned/printed but never
    gate check_against_baseline.

    Uses `unclassified` (a `/tmp/`-prefixed literal), not
    `unresolved_cross_path` -- that class was promoted to BLOCKING
    2026-08-13 (Q2.1 ruling) and is `unclassified`'s only remaining
    report-only sibling."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        'def default_root():\n    return "/tmp/scratch/repo"\n'
    )
    _commit_all(repo)

    baseline_path = repo / "state" / "posix-exec-baseline.json"  # absent

    ok, msg = check_against_baseline(repo, baseline_path)

    assert ok is True
    assert "tool.py" in msg  # still visible in the report-only summary
    assert "unclassified" in msg


def test_report_only_classes_excluded_from_baseline_growth_check(tmp_path):
    """Growing a report-only class's count between scans must never trip
    assert_baseline_not_grown -- only the blocking CLASSES are diffed, and
    report-only classes are never persisted into the baseline file at all.

    Uses `unclassified`, not `unresolved_cross_path` -- see
    `test_report_only_classes_never_fail_check_against_baseline`'s
    docstring for why."""
    repo = _init_repo(tmp_path)
    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}))
    _commit_all(repo, "introduce baseline")

    # Baseline file has no report-only keys at all -- adding one, even a
    # populated list under a report-only class name, must not read as growth
    # because assert_baseline_not_grown only inspects CLASSES.
    widened = {cls: [] for cls in CLASSES}
    widened["unclassified"] = ["new-finding.py"]
    baseline_path.write_text(json.dumps(widened))

    ok, msg = assert_baseline_not_grown(repo, "state/posix-exec-baseline.json")

    assert ok is True


def test_unresolved_cross_path_is_not_report_only():
    """Anti-regrowth lock (2026-08-13, Q2.1 ruling): `unresolved_cross_path`
    was promoted to BLOCKING (`CLASSES`) and MUST NOT be in
    `REPORT_ONLY_CLASSES`. Nothing procedural stops a future author
    demoting it back to report-only the way it lived for two weeks
    unread -- this assertion is the artifact that does: a demotion fails
    this test red instead of merging quietly."""
    assert "unresolved_cross_path" in CLASSES
    assert "unresolved_cross_path" not in REPORT_ONLY_CLASSES


def test_scan_returns_every_class(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "unrelated.txt").write_text("nothing to see here\n")
    _commit_all(repo)

    result = scan(repo)

    assert set(result.keys()) == set(ALL_CLASSES)
    assert (
        set(CLASSES) | set(REPORT_ONLY_CLASSES) | set(TIER_A_CLASSES) | set(IMPLICIT_ENCODING_CLASSES)
        == set(ALL_CLASSES)
    )


# ---------------------------------------------------------------------------
# Ratchet: check_against_baseline.
# ---------------------------------------------------------------------------

def test_check_against_baseline_reds_on_new_violation_with_empty_baseline(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.sh").write_text("#!/usr/bin/env bash\n")
    _commit_all(repo)

    baseline_path = repo / "state" / "posix-exec-baseline.json"

    ok, msg = check_against_baseline(repo, baseline_path)

    assert ok is False
    assert "tool.sh" in msg
    assert "NEW POSIX-exec-assumption violation" in msg


def test_check_against_baseline_leads_with_early_return_escape_for_guard_aware_classes(tmp_path):
    """Design-as-offers (PM ask): a NEW posix_mode_bits/path_separator
    violation's failure message must LEAD with the known early-return-gap
    EXEMPTIONS escape, not just the generic port-away-from-POSIX text --
    an author hitting the rare false positive should see "known gap, named
    exit" before anything else."""
    repo = _init_repo(tmp_path)
    (repo / "tool.py").write_text(
        "import os\n\ndef check(p):\n    return os.access(p, os.X_OK)\n"
    )
    _commit_all(repo)

    baseline_path = repo / "state" / "posix-exec-baseline.json"

    ok, msg = check_against_baseline(repo, baseline_path)

    assert ok is False
    idx_escape = msg.find("KNOWN, NAMED gap")
    idx_generic_fix = msg.find("Fix: port posix mode bits")
    assert idx_escape != -1 and idx_generic_fix != -1
    assert idx_escape < idx_generic_fix, "the EXEMPTIONS escape must come BEFORE the generic fix text"


def test_check_against_baseline_does_not_lead_with_escape_for_non_guard_aware_classes(tmp_path):
    """Negative control: env_shebang (no platform-guard concept at all)
    must not get the early-return escape hint -- it's specific to the two
    AST-scanned classes that actually have a recognized guard gap."""
    repo = _init_repo(tmp_path)
    (repo / "tool.sh").write_text("#!/usr/bin/env bash\n")
    _commit_all(repo)

    baseline_path = repo / "state" / "posix-exec-baseline.json"

    ok, msg = check_against_baseline(repo, baseline_path)

    assert ok is False
    assert "KNOWN, NAMED gap" not in msg


def test_check_against_baseline_passes_when_violation_is_baselined(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.sh").write_text("#!/usr/bin/env bash\n")
    _commit_all(repo)

    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(
        json.dumps({cls: (["tool.sh"] if cls == "env_shebang" else []) for cls in CLASSES})
    )

    ok, msg = check_against_baseline(repo, baseline_path)

    assert ok is True
    assert "OK" in msg


def test_check_against_baseline_reds_on_violation_outside_baselined_set(tmp_path):
    """A NEW violation must fail even when OTHER violations are baselined --
    the ratchet is per-violation, not a total-count threshold."""
    repo = _init_repo(tmp_path)
    (repo / "old.sh").write_text("#!/usr/bin/env bash\n")
    (repo / "new.sh").write_text("#!/usr/bin/env bash\n")
    _commit_all(repo)

    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(
        json.dumps({cls: (["old.sh"] if cls == "env_shebang" else []) for cls in CLASSES})
    )

    ok, msg = check_against_baseline(repo, baseline_path)

    assert ok is False
    assert "- new.sh" in msg
    assert "- old.sh" not in msg


# ---------------------------------------------------------------------------
# Append-forbidden: assert_baseline_not_grown.
# ---------------------------------------------------------------------------

def test_assert_baseline_not_grown_passes_on_first_introduction(tmp_path):
    repo = _init_repo(tmp_path)
    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}))
    # Deliberately NOT committed -- first introduction, no HEAD to diff against.

    ok, msg = assert_baseline_not_grown(repo, "state/posix-exec-baseline.json")

    assert ok is True
    assert "first introduction" in msg


def test_assert_baseline_not_grown_reds_when_baseline_widened(tmp_path):
    repo = _init_repo(tmp_path)
    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}))
    _commit_all(repo, "introduce baseline")

    # Simulate a maintainer widening the baseline to hide a new violation.
    widened = {cls: [] for cls in CLASSES}
    widened["env_shebang"] = ["sneaky-new-violation.sh"]
    baseline_path.write_text(json.dumps(widened))

    ok, msg = assert_baseline_not_grown(repo, "state/posix-exec-baseline.json")

    assert ok is False
    assert "BASELINE GREW" in msg
    assert "sneaky-new-violation.sh" in msg


def test_assert_baseline_not_grown_reds_even_after_widening_is_committed(tmp_path):
    """Proves the fix for the 2026-07-28 review finding: a `HEAD`-relative
    diff is structurally unable to catch a widening once it has landed in
    its own commit, because at that point `current == HEAD` trivially. This
    test COMMITS the widening (unlike
    test_assert_baseline_not_grown_reds_when_baseline_widened, which leaves
    it uncommitted) -- against the pre-fix `git show HEAD:...` comparison
    this fails to detect anything (`ok` comes back True), and only the
    fixed introducing-commit anchor (`_baseline_introducing_sha`) still
    catches it."""
    repo = _init_repo(tmp_path)
    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}))
    _commit_all(repo, "introduce baseline")  # the fixed anchor commit

    widened = {cls: [] for cls in CLASSES}
    widened["env_shebang"] = ["sneaky-new-violation.sh"]
    baseline_path.write_text(json.dumps(widened))
    _commit_all(repo, "widen baseline (regression hidden here)")  # HEAD moves past it

    ok, msg = assert_baseline_not_grown(repo, "state/posix-exec-baseline.json")

    assert ok is False
    assert "BASELINE GREW" in msg
    assert "sneaky-new-violation.sh" in msg


def test_assert_baseline_not_grown_honors_explicit_frozen_as_of_sha_override(tmp_path):
    """An explicit `_frozen_as_of_sha` metadata key re-anchors the growth
    check to that commit instead of the file's original introducing commit
    -- the re-freeze mechanism landing this fix used on both repos' real
    baselines (see module docstring § Anchor choice) to avoid retroactively
    flagging pre-fix historical growth that predates the mechanism actually
    working."""
    repo = _init_repo(tmp_path)
    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}))
    _commit_all(repo, "introduce baseline")

    # Simulate pre-existing (pre-fix) growth that already landed, unnoticed,
    # in an earlier commit -- this would be flagged as "grown" if the
    # introducing commit were used as the anchor.
    already_grown = {cls: [] for cls in CLASSES}
    already_grown["env_shebang"] = ["old-pre-fix-debt.sh"]
    baseline_path.write_text(json.dumps(already_grown))
    _commit_all(repo, "pre-fix growth, now to be re-frozen")
    reanchor_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Record the explicit re-freeze point and commit it.
    with_anchor = dict(already_grown)
    with_anchor["_frozen_as_of_sha"] = reanchor_sha
    baseline_path.write_text(json.dumps(with_anchor))
    _commit_all(repo, "record re-freeze anchor")

    # No NEW growth since the re-freeze point -- must pass.
    ok, msg = assert_baseline_not_grown(repo, "state/posix-exec-baseline.json")
    assert ok is True

    # Genuine new growth AFTER the re-freeze point must still be caught.
    further_widened = dict(with_anchor)
    further_widened["env_shebang"] = further_widened["env_shebang"] + ["new-sneaky-violation.sh"]
    baseline_path.write_text(json.dumps(further_widened))
    _commit_all(repo, "widen again after re-freeze")

    ok, msg = assert_baseline_not_grown(repo, "state/posix-exec-baseline.json")
    assert ok is False
    assert "new-sneaky-violation.sh" in msg
    assert "old-pre-fix-debt.sh" not in msg


def test_assert_baseline_not_grown_passes_when_baseline_shrinks(tmp_path):
    repo = _init_repo(tmp_path)
    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(
        json.dumps({cls: (["fixed-me.sh"] if cls == "env_shebang" else []) for cls in CLASSES})
    )
    _commit_all(repo, "introduce baseline")

    # Simulate the violation getting fixed and removed from the baseline.
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}))

    ok, msg = assert_baseline_not_grown(repo, "state/posix-exec-baseline.json")

    assert ok is True


def test_assert_baseline_not_grown_passes_when_baseline_file_absent(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "unrelated.txt").write_text("no baseline file in this repo\n")
    _commit_all(repo, "repo with no baseline file at all")

    ok, msg = assert_baseline_not_grown(repo, "state/posix-exec-baseline.json")

    assert ok is True


# ---------------------------------------------------------------------------
# EXEMPTIONS are repo-scoped: an exemption granted for repo A must never
# exempt the same relpath in repo B. Before 2026-08-03 the dict was keyed by
# bare relpath and `scan()` subtracted it regardless of which root was being
# scanned -- a fleet-global over-exemption that silently hid a sibling repo's
# genuinely-defective file at, e.g., `coordinator/scripts/setup.py`.
# ---------------------------------------------------------------------------

_EXEMPTED_IN_CLAUDE_KLABAUTER = "coordinator/scripts/setup.py"


def _repo_named(tmp_path: Path, dir_name: str) -> Path:
    """A git repo whose DIRECTORY NAME (the thing `repo_key_for_root` reads)
    is caller-chosen, seeded with two identical env-shebang mode-755 files:
    one at a relpath claude-klabauter holds an exemption for, one at a relpath
    no repo holds an exemption for (the in-fixture control)."""
    repo = tmp_path / dir_name
    (repo / "coordinator" / "scripts").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    for relpath in (_EXEMPTED_IN_CLAUDE_KLABAUTER, "coordinator/scripts/control.py"):
        target = repo / relpath
        target.write_text("#!/usr/bin/env python3\nprint('hi')\n")
        target.chmod(0o755)
    _commit_all(repo)
    return repo


def test_exemption_granted_for_one_repo_does_not_exempt_that_relpath_in_another(tmp_path):
    """The break-class case: a sibling repo's own defective file at a relpath
    claude-klabauter happens to hold an exemption for must still be REPORTED."""
    sibling = _repo_named(tmp_path, "some-unrelated-sibling")

    result = scan(sibling)

    assert repo_key_for_root(sibling) == "some_unrelated_sibling"
    assert _EXEMPTED_IN_CLAUDE_KLABAUTER in result["env_shebang"]
    assert _EXEMPTED_IN_CLAUDE_KLABAUTER in result["mode_100755"]


def test_exemption_granted_for_one_repo_still_applies_in_that_repo(tmp_path):
    """The other half of the pair: repo-scoping must not silently revoke the
    grant in the repo it was actually granted for. The control file at a
    non-exempted relpath proves the detector still fires in this fixture."""
    claude_klabauter_like = _repo_named(tmp_path, "claude-klabauter")

    result = scan(claude_klabauter_like)

    assert repo_key_for_root(claude_klabauter_like) == REPO_CLAUDE_KLABAUTER
    assert _EXEMPTED_IN_CLAUDE_KLABAUTER not in result["env_shebang"]
    assert _EXEMPTED_IN_CLAUDE_KLABAUTER not in result["mode_100755"]
    assert "coordinator/scripts/control.py" in result["env_shebang"]
    assert "coordinator/scripts/control.py" in result["mode_100755"]


def test_every_exemption_repo_key_is_a_canonical_fleet_repo_key():
    """Repo keys are the fleet's `repos.<key>` machine-local vocabulary, not
    a private spelling -- a key that is not the normalized form of some clone
    directory name would be silently inert (it can never match a scanned
    root), which is the failure mode repo-scoping exists to prevent."""
    for cls, per_repo in EXEMPTIONS.items():
        for repo_key in per_repo:
            assert repo_key == repo_key_for_root(Path(repo_key)), (
                f"EXEMPTIONS[{cls!r}] key {repo_key!r} is not a normalized "
                "fleet repo key and can never match a scanned root"
            )


def test_every_exemption_carries_a_written_reason():
    """Same bar as EXEMPT_PREFIXES' analogous
    `test_every_exempt_prefix_carries_a_written_reason`: a per-file grant
    must state which admission test it passes, not merely exist."""
    for cls, per_repo in EXEMPTIONS.items():
        for repo_key, relpaths in per_repo.items():
            for relpath, reason in relpaths.items():
                assert isinstance(reason, str) and len(reason.split()) >= 25, (
                    f"EXEMPTIONS[{cls!r}][{repo_key!r}][{relpath!r}] needs a "
                    f"real reason, got {reason!r}"
                )


def test_module_docstring_states_the_third_admission_test():
    """The permanent-artifact-shape-irreducibility admission test
    (2026-08-13 eng-director ruling) must be discoverable in the module's
    own docstring, the same place the two-leg-pair and EXEMPT_PREFIXES
    admission tests already live -- not merely in a comment beside one
    dict entry, where a future reader granting a fourth row would never
    find it."""
    doc = " ".join(check_posix_exec_assumptions.__doc__.split())
    assert "permanent artifact-shape irreducibility" in doc
    assert "restatement" in doc.lower()
    assert "elimination-target" in doc.lower()
    assert "standing reduction target" in doc


def test_real_tree_every_claude_klabauter_exemption_is_actually_subtracted():
    """Repo-scoping must leave claude-klabauter's OWN scan result unchanged:
    every relpath exempted under the claude_klabauter key is still invisible in
    a real-tree scan of this repo. Guards the ratchet from the other side to
    `test_real_tree_ratchets_clean_against_committed_baseline` -- that test
    would also catch a lost exemption, but only as an opaque 'new violation'
    count, never naming repo-scoping as the cause."""
    result = scan(_REPO_ROOT)

    for cls, per_repo in EXEMPTIONS.items():
        for relpath in per_repo.get(REPO_CLAUDE_KLABAUTER, {}):
            assert relpath not in result[cls], (
                f"{relpath} is exempted for claude_klabauter in class {cls} but "
                "appears in this repo's own scan -- the exemption was lost"
            )


# ---------------------------------------------------------------------------
# Real-tree wiring: this repo's own committed baseline must ratchet clean.
# ---------------------------------------------------------------------------

def test_real_tree_ratchets_clean_against_committed_baseline():
    """Wiring proof for claude-klabauter's own test tier: scanning THIS repo
    against its own committed state/posix-exec-baseline.json must currently
    pass (no new violations beyond what the baseline enumerates) and the
    baseline itself must not have grown relative to HEAD.

    If this test ever fails, it means either (a) a genuinely new POSIX-exec
    violation landed and needs fixing (or, rarely, a named EXEMPTIONS entry),
    or (b) the baseline file was hand-widened -- see the printed message for
    which."""
    baseline_path = _REPO_ROOT / "state" / "posix-exec-baseline.json"
    if not baseline_path.is_file():
        pytest.fail(
            f"{baseline_path} is missing -- the ratchet has nothing to check "
            "against; regenerate it via check_posix_exec_assumptions.scan()."
        )

    ok, msg = check_against_baseline(_REPO_ROOT, baseline_path)
    assert ok, msg

    grown_ok, grown_msg = assert_baseline_not_grown(
        _REPO_ROOT, "state/posix-exec-baseline.json"
    )
    assert grown_ok, grown_msg


# ---------------------------------------------------------------------------
# EXEMPT_PREFIXES: directory-scope exclusion for verbatim-preserved foreign
# trees and frozen evidence snapshots. Built 2026-08-03 as the mechanism
# _M8_REVIEW_TRAIL_SNAPSHOT_REASON's own text had named as the correct remedy
# to hand-listing one snapshot file at a time.
# ---------------------------------------------------------------------------

_CLAUDE_KLABAUTER_PREFIX = "dist/mirror-native/"
_DOE_PREFIX = "state/review-trail/diffs/m8-baseline/"

#: A file that trips several PREFIX_EXCLUDABLE classes at once (env_shebang,
#: path_separator, implicit_encoding) -- one specimen proving the exclusion is
#: wholesale, not per-class.
_MULTI_CLASS_VIOLATOR = (
    "#!/usr/bin/env python3\n"
    "def norm(p):\n"
    "    return p.replace('/', '\\\\')\n"
    "def read(p):\n"
    "    return open(p).read()\n"
)


def _repo_with_preserved_tree(tmp_path: Path, dir_name: str, prefix: str) -> Path:
    """A git repo whose DIRECTORY NAME is caller-chosen (that is what
    `repo_key_for_root` reads), seeded with two byte-identical violating
    files: one INSIDE the preserved-tree prefix, one at a deliberately
    similar-looking sibling path just OUTSIDE it."""
    repo = tmp_path / dir_name
    inside = repo / f"{prefix}nested/check-thing.py"
    outside = repo / f"{prefix.rstrip('/')}-scratch/check-thing.py"
    for target in (inside, outside):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_MULTI_CLASS_VIOLATOR, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit_all(repo)
    return repo


def test_prefix_exclusion_hides_a_preserved_tree_from_every_non_tier_a_class(tmp_path):
    """The 8 klabauter CI scripts' shape: a verbatim foreign tree trips
    env_shebang (and path_separator, and implicit_encoding) in files nobody
    here may edit. One prefix must cover all of it -- that wholesale property
    is the whole point over hand-listing per file per class."""
    repo = _repo_with_preserved_tree(tmp_path, "claude-klabauter", _CLAUDE_KLABAUTER_PREFIX)
    inside = f"{_CLAUDE_KLABAUTER_PREFIX}nested/check-thing.py"

    result = scan(repo)

    assert repo_key_for_root(repo) == REPO_CLAUDE_KLABAUTER
    for cls in PREFIX_EXCLUDABLE_CLASSES:
        assert inside not in result[cls], f"{inside} leaked into class {cls}"


def test_prefix_exclusion_does_not_swallow_a_similarly_named_sibling_path(tmp_path):
    """The narrowness proof, and why every prefix must end in `/`: a
    non-snapshot file one character outside the preserved tree
    (`...-scratch/`) is ordinary live code and stays fully visible. Without
    the trailing slash a bare `startswith` would swallow it silently."""
    repo = _repo_with_preserved_tree(tmp_path, "claude-klabauter", _CLAUDE_KLABAUTER_PREFIX)
    outside = f"{_CLAUDE_KLABAUTER_PREFIX.rstrip('/')}-scratch/check-thing.py"

    result = scan(repo)

    assert outside in result["env_shebang"]
    assert outside in result["path_separator"]
    assert outside in result["implicit_encoding"]
    assert not is_prefix_excluded(outside, REPO_CLAUDE_KLABAUTER)


def test_prefix_exclusion_never_suppresses_tier_a_checkout_breakers(tmp_path):
    """The deliberate scope limit: a MAX_PATH-busting path inside a frozen
    snapshot tree still breaks `git clone` on Windows for the WHOLE repo. That
    failure is about the repo, not about the file's own executability, so no
    argument about the content being un-editable evidence changes it -- Tier A
    is outside PREFIX_EXCLUDABLE_CLASSES."""
    repo = _repo_with_preserved_tree(tmp_path, "claude-klabauter", _CLAUDE_KLABAUTER_PREFIX)
    long_rel = _CLAUDE_KLABAUTER_PREFIX + "/".join(["preserved-deep-directory-segment"] * 7) + ".txt"
    assert len(long_rel) > 200
    target = repo / long_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("snapshot\n", encoding="utf-8")
    _commit_all(repo, "deep snapshot path")

    ok, msg = check_tier_a_zero_tolerance(repo)

    assert ok is False, msg
    assert long_rel in msg


def test_prefix_exclusion_is_repo_scoped(tmp_path):
    """Same repo-scoping contract as EXEMPTIONS: `state/review-trail/` and
    `state/` paths generally exist in more than one fleet repo, so a grant to
    one must never silently cover a sibling's genuinely-live file."""
    sibling = _repo_with_preserved_tree(tmp_path, "some-unrelated-sibling", _CLAUDE_KLABAUTER_PREFIX)
    inside = f"{_CLAUDE_KLABAUTER_PREFIX}nested/check-thing.py"

    result = scan(sibling)

    assert inside in result["env_shebang"]
    assert inside in result["path_separator"]


def test_migrated_m8_snapshot_entries_are_still_excluded_after_migration(tmp_path):
    """Migration proof: the three hand-listed example-doctrine-repo m8-baseline `path_separator`
    entries were deleted from EXEMPTIONS in favour of one prefix. The class
    they named must still be invisible -- migrating a carve-out must not
    quietly re-expose what it covered."""
    repo = _repo_with_preserved_tree(tmp_path, "example-doctrine-repo", _DOE_PREFIX)
    for name in (
        "block_subagent_commit.py",
        "block_subagent_destructive_action.py",
        "dispatch_checks.py",
    ):
        (repo / _DOE_PREFIX / name).write_text(_MULTI_CLASS_VIOLATOR, encoding="utf-8")
    _commit_all(repo, "m8 snapshot")

    result = scan(repo)

    assert repo_key_for_root(repo) == REPO_EXAMPLE_DOCTRINE_REPO
    for name in (
        "block_subagent_commit.py",
        "block_subagent_destructive_action.py",
        "dispatch_checks.py",
    ):
        assert f"{_DOE_PREFIX}{name}" not in result["path_separator"]


def test_stale_exempt_prefix_is_red(tmp_path):
    """Same discipline as `test_no_parity_exemption_is_stale`: a prefix
    matching no tracked file excuses nothing and would silently cover whatever
    lands at that path next -- strictly worse for a directory-wide grant than
    for a one-file one, since the successor content need not resemble what the
    grant was written about at all. Fixture is a repo that derives the
    claude_klabauter key but has no preserved tree."""
    repo = tmp_path / "claude-klabauter"
    (repo / "coordinator").mkdir(parents=True)
    (repo / "coordinator" / "live.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit_all(repo)

    ok, msg = check_no_stale_exempt_prefixes(repo)

    assert ok is False, msg
    assert _CLAUDE_KLABAUTER_PREFIX in msg
    assert "shrink-only" in msg


def test_stale_prefix_check_is_green_when_the_preserved_tree_exists(tmp_path):
    """Positive control for the staleness gate: it must not fire merely
    because prefixes are declared. `check_no_stale_exempt_prefixes` checks
    EVERY prefix registered under the scanned repo's key, not just the one
    this fixture was originally built around -- so a repo claiming to BE
    claude-klabauter must carry a matching tracked file under every
    claude_klabauter-keyed EXEMPT_PREFIXES entry, not only `_CLAUDE_KLABAUTER_PREFIX`,
    or this positive control would false-fire the moment a second grant
    (e.g. the frozen-fixture-bytes prefixes) is added."""
    repo = _repo_with_preserved_tree(tmp_path, "claude-klabauter", _CLAUDE_KLABAUTER_PREFIX)
    for prefix in EXEMPT_PREFIXES.get(REPO_CLAUDE_KLABAUTER, {}):
        if prefix == _CLAUDE_KLABAUTER_PREFIX:
            continue
        target = repo / prefix / "specimen.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_MULTI_CLASS_VIOLATOR, encoding="utf-8")
    _commit_all(repo, "seed remaining claude_klabauter prefixes")

    ok, msg = check_no_stale_exempt_prefixes(repo)

    assert ok is True, msg


def test_every_exempt_prefix_is_directory_scoped_and_repo_relative():
    """Shape bar. A prefix without a trailing `/` swallows sibling paths that
    merely share a name stem; a backslash or leading-slash spelling can never
    match a `git ls-files` relpath and is silently inert."""
    for repo_key, prefixes in EXEMPT_PREFIXES.items():
        for prefix in prefixes:
            assert prefix.endswith("/"), f"{repo_key}: {prefix!r} must end in '/'"
            assert not prefix.startswith("/"), f"{repo_key}: {prefix!r} must be repo-relative"
            assert "\\" not in prefix, f"{repo_key}: {prefix!r} must be forward-slash spelled"
            assert ".." not in prefix, f"{repo_key}: {prefix!r} must not escape the repo"


def test_every_exempt_prefix_carries_a_written_reason():
    """A directory-scope exclusion is a bigger hammer than a per-file one, so
    the rationale bar is higher than EXEMPTIONS' -- it has to state which
    admission test the tree passes, not merely assert it does."""
    for repo_key, prefixes in EXEMPT_PREFIXES.items():
        for prefix, reason in prefixes.items():
            assert isinstance(reason, str) and len(reason.split()) >= 25, (
                f"EXEMPT_PREFIXES[{repo_key!r}][{prefix!r}] needs a real "
                f"reason, got {reason!r}"
            )


def test_every_exempt_prefix_repo_key_is_a_canonical_fleet_repo_key():
    """Same inert-key trap EXEMPTIONS' own key test closes: a key that is not
    the normalized form of a clone directory name can never match a scanned
    root, so the grant would silently never apply."""
    for repo_key in EXEMPT_PREFIXES:
        assert repo_key == repo_key_for_root(Path(repo_key)), (
            f"EXEMPT_PREFIXES key {repo_key!r} is not a normalized fleet repo "
            "key and can never match a scanned root"
        )


def test_real_tree_mirror_native_files_are_prefix_excluded():
    """Real-tree wiring for the case that motivated this mechanism: the
    klabauter CI harness homed under dist/mirror-native/ by 720d56204, whose 8
    `#!/usr/bin/env python3` scripts (also 100755, deliberately — the mirror's
    own check-exec-bit.py gate requires it) landed as new env_shebang and
    mode_100755 violations. Asserted against `git ls-files` (the index), not
    the working tree, so the result does not depend on which files a
    concurrent session happens to have checked out on disk."""
    tracked = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", _CLAUDE_KLABAUTER_PREFIX],
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    ).stdout.split()
    assert tracked, f"{_CLAUDE_KLABAUTER_PREFIX} has no tracked files -- prefix is stale"

    shebanged = [
        f
        for f in tracked
        if subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "show", f":{f}"],
            capture_output=True,
            text=True,
            check=True,
            creationflags=_NO_WINDOW,
        ).stdout.startswith("#!/usr/bin/env")
    ]
    assert len(shebanged) == 8, (
        f"expected the 8 preserved klabauter CI scripts, got {shebanged}"
    )

    result = scan(_REPO_ROOT)
    for relpath in tracked:
        assert is_prefix_excluded(relpath, REPO_CLAUDE_KLABAUTER)
        for cls in PREFIX_EXCLUDABLE_CLASSES:
            assert relpath not in result[cls]


def test_no_per_file_exemption_is_subsumed_by_a_prefix():
    """The two mechanisms must not silently overlap. A hand-listed EXEMPTIONS
    relpath sitting under one of its own repo's prefixes is already invisible,
    so its written reason is never read and never re-examined -- the residue
    that migrating the m8-baseline entries removed, and that would grow back
    one entry at a time without this."""
    for cls, per_repo in EXEMPTIONS.items():
        for repo_key, relpaths in per_repo.items():
            for relpath in relpaths:
                assert not (
                    cls in PREFIX_EXCLUDABLE_CLASSES
                    and is_prefix_excluded(relpath, repo_key)
                ), (
                    f"EXEMPTIONS[{cls!r}][{repo_key!r}][{relpath!r}] is already "
                    "covered by an EXEMPT_PREFIXES entry -- delete the "
                    "redundant per-file grant so one mechanism owns this path"
                )


# ---------------------------------------------------------------------------
# FILE-based prefix exclusion (`state/posix-exec-exempt-prefixes.json`,
# 2026-08-03, example-retrieval-repo thread): the same EXEMPT_PREFIXES admission test,
# granted per-repo from a file instead of an in-code dict this module's own
# maintainer must edit.
# ---------------------------------------------------------------------------

_FILE_PREFIX_REASON = (
    "Verbatim third-party vendor tree, byte-identical to its upstream "
    "publisher and never imported, executed, or installed by this repo -- "
    "satisfies the same admission test as EXEMPT_PREFIXES, granted here via "
    "the file mechanism instead of an in-code entry."
)


def _write_file_exempt_prefixes(repo: Path, entries: dict) -> None:
    prefixes_path = repo / "state" / "posix-exec-exempt-prefixes.json"
    prefixes_path.parent.mkdir(parents=True, exist_ok=True)
    prefixes_path.write_text(json.dumps(entries), encoding="utf-8")


def test_file_exempt_prefix_hides_matching_tracked_file_from_scan(tmp_path):
    repo = _init_repo(tmp_path)
    victim = repo / "vendor/thing/tool.py"
    victim.parent.mkdir(parents=True)
    victim.write_text("#!/usr/bin/env python3\nopen('x').read()\n", encoding="utf-8")
    _write_file_exempt_prefixes(repo, {"vendor/thing/": _FILE_PREFIX_REASON})
    _commit_all(repo)

    result = scan(repo)

    assert "vendor/thing/tool.py" not in result["env_shebang"]
    assert "vendor/thing/tool.py" not in result["implicit_encoding"]


def test_file_exempt_prefix_absent_leaves_scan_unaffected(tmp_path):
    """Positive control: no declared file means no additional exclusions --
    the common case, since most repos declare none."""
    repo = _init_repo(tmp_path)
    victim = repo / "vendor/thing/tool.py"
    victim.parent.mkdir(parents=True)
    victim.write_text("#!/usr/bin/env python3\nopen('x').read()\n", encoding="utf-8")
    _commit_all(repo)

    result = scan(repo)

    assert "vendor/thing/tool.py" in result["env_shebang"]


def test_file_exempt_prefix_malformed_json_degrades_to_no_exclusions(tmp_path):
    """Negative-spec: corrupt JSON must never crash the scan, and must never
    silently exclude anything -- a broken declaration widens visible debt,
    it does not swallow it."""
    repo = _init_repo(tmp_path)
    victim = repo / "vendor/thing/tool.py"
    victim.parent.mkdir(parents=True)
    victim.write_text("#!/usr/bin/env python3\nopen('x').read()\n", encoding="utf-8")
    prefixes_path = repo / "state" / "posix-exec-exempt-prefixes.json"
    prefixes_path.parent.mkdir(parents=True, exist_ok=True)
    prefixes_path.write_text("{not valid json", encoding="utf-8")
    _commit_all(repo)

    result = scan(repo)

    assert "vendor/thing/tool.py" in result["env_shebang"]


def test_file_exempt_prefix_missing_trailing_slash_is_ignored(tmp_path):
    """Same shape bar as EXEMPT_PREFIXES: a prefix without a trailing `/`
    would swallow a similarly-named sibling, so a malformed entry is dropped
    rather than honored."""
    repo = _init_repo(tmp_path)
    victim = repo / "vendor/thing/tool.py"
    victim.parent.mkdir(parents=True)
    victim.write_text("#!/usr/bin/env python3\nopen('x').read()\n", encoding="utf-8")
    _write_file_exempt_prefixes(repo, {"vendor/thing": _FILE_PREFIX_REASON})
    _commit_all(repo)

    result = scan(repo)

    assert "vendor/thing/tool.py" in result["env_shebang"]


def test_file_exempt_prefix_empty_reason_is_ignored(tmp_path):
    repo = _init_repo(tmp_path)
    victim = repo / "vendor/thing/tool.py"
    victim.parent.mkdir(parents=True)
    victim.write_text("#!/usr/bin/env python3\nopen('x').read()\n", encoding="utf-8")
    _write_file_exempt_prefixes(repo, {"vendor/thing/": "   "})
    _commit_all(repo)

    result = scan(repo)

    assert "vendor/thing/tool.py" in result["env_shebang"]


def test_file_exempt_prefix_never_suppresses_tier_a(tmp_path):
    """Same scope limit as the in-code mechanism: Tier A is a fact about the
    whole repo's checkout, never suppressed by a prefix of either kind."""
    repo = _init_repo(tmp_path)
    long_rel = "vendor/thing/" + "/".join(["segment"] * 40) + ".txt"
    assert len(long_rel) > 200
    target = repo / long_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n", encoding="utf-8")
    _write_file_exempt_prefixes(repo, {"vendor/thing/": _FILE_PREFIX_REASON})
    _commit_all(repo)

    ok, msg = check_tier_a_zero_tolerance(repo)

    assert ok is False, msg
    assert long_rel in msg


def test_file_exempt_prefix_is_scoped_to_its_own_root(tmp_path):
    """The file lives inside the repo it exempts -- scanning a DIFFERENT
    root must never pick up another repo's declared file."""
    (tmp_path / "repo-a").mkdir()
    (tmp_path / "repo-b").mkdir()
    repo_a = _init_repo(tmp_path / "repo-a")
    _write_file_exempt_prefixes(repo_a, {"vendor/thing/": _FILE_PREFIX_REASON})
    (repo_a / "vendor" / "thing").mkdir(parents=True, exist_ok=True)
    (repo_a / "vendor" / "thing" / "tool.py").write_text(
        "#!/usr/bin/env python3\nopen('x').read()\n", encoding="utf-8"
    )
    _commit_all(repo_a)

    repo_b = _init_repo(tmp_path / "repo-b")
    (repo_b / "vendor" / "thing").mkdir(parents=True, exist_ok=True)
    (repo_b / "vendor" / "thing" / "tool.py").write_text(
        "#!/usr/bin/env python3\nopen('x').read()\n", encoding="utf-8"
    )
    _commit_all(repo_b)

    result_b = scan(repo_b)

    assert "vendor/thing/tool.py" in result_b["env_shebang"]


def test_is_prefix_excluded_backward_compatible_without_root():
    """Every pre-existing call site (this suite's own EXEMPT_PREFIXES tests
    among them) calls `is_prefix_excluded(relpath, repo_key)` with no `root`
    -- that must keep answering from the in-code EXEMPT_PREFIXES alone."""
    assert is_prefix_excluded("dist/mirror-native/thing.py", REPO_CLAUDE_KLABAUTER)


def test_check_no_stale_exempt_prefixes_flags_stale_file_based_prefix(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "coordinator").mkdir()
    (repo / "coordinator" / "live.py").write_text("x = 1\n", encoding="utf-8")
    _write_file_exempt_prefixes(repo, {"vendor/gone/": _FILE_PREFIX_REASON})
    _commit_all(repo)

    ok, msg = check_no_stale_exempt_prefixes(repo)

    assert ok is False, msg
    assert "vendor/gone/" in msg


# ---------------------------------------------------------------------------
# --json CLI mode (2026-08-03, example-retrieval-repo thread): machine-readable output
# for CI tooling that would otherwise have to text-scrape stdout.
# ---------------------------------------------------------------------------

def test_main_json_flag_emits_valid_json_with_expected_top_level_keys(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo)
    baseline_path = repo / "state" / "posix-exec-baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}), encoding="utf-8")

    exit_code = main(["--root", str(repo), "--baseline", str(baseline_path), "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert set(payload["checks"]) == {
        "baseline",
        "baseline_growth",
        "tier_a",
        "implicit_encoding",
        "stale_exempt_prefixes",
        "stale_baseline_entries",
    }
    assert "scan" in payload
    assert "env_shebang" in payload["scan"]


def test_main_json_flag_reports_new_violation_as_not_ok(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    (repo / "tool.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    _commit_all(repo)
    baseline_path = repo / "state" / "posix-exec-baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}), encoding="utf-8")

    exit_code = main(["--root", str(repo), "--baseline", str(baseline_path), "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["checks"]["baseline"]["ok"] is False
    assert "tool.sh" in payload["scan"]["env_shebang"]


def test_main_without_json_flag_prints_human_readable_text_not_json(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo)
    baseline_path = repo / "state" / "posix-exec-baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps({cls: [] for cls in CLASSES}), encoding="utf-8")

    exit_code = main(["--root", str(repo), "--baseline", str(baseline_path)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "OK" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_real_tree_has_no_stale_exempt_prefix():
    ok, msg = check_no_stale_exempt_prefixes(_REPO_ROOT)
    assert ok, msg


def test_real_tree_passes_tier_a_zero_tolerance():
    ok, msg = check_tier_a_zero_tolerance(_REPO_ROOT)
    assert ok, msg


def test_real_tree_passes_implicit_encoding_zero_tolerance():
    ok, msg = check_implicit_encoding_zero_tolerance(_REPO_ROOT)
    assert ok, msg


# ---------------------------------------------------------------------------
# Debt-line desync: current scan vs frozen baseline must be printed as two
# distinct, unmistakable numbers -- never one bare count a reader could
# conflate with the baseline file's own total.
# ---------------------------------------------------------------------------

def test_debt_line_states_current_and_baseline_totals_distinctly_on_desync(tmp_path):
    """Crafted baseline (2 entries, one of them stale) vs crafted scan (1
    live violation, 1 NEW) driven through `check_against_baseline` -- the
    same code path `main()` uses. The debt line must name BOTH totals
    explicitly and not collapse them into a single ambiguous number."""
    repo = _init_repo(tmp_path)
    (repo / "new.sh").write_text("#!/usr/bin/env bash\n")
    _commit_all(repo)

    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                cls: (["gone.sh", "also-gone.sh"] if cls == "env_shebang" else [])
                for cls in CLASSES
            }
        )
    )

    ok, msg = check_against_baseline(repo, baseline_path)

    assert ok is False
    assert "current scan 1 violation(s)" in msg
    assert "frozen baseline 2 entry(ies)" in msg
    # The bare pre-fix phrasing must be gone -- a reader must not be able to
    # mistake the current-scan number for the baseline's own count.
    assert "pre-existing violation(s)" not in msg
    assert "baseline-frozen; shrinks" not in msg
    assert "1 NEW violation(s) not yet in the baseline" in msg
    assert "stale baseline entry(ies)" in msg


def test_debt_line_omits_desync_explanation_when_totals_match(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.sh").write_text("#!/usr/bin/env bash\n")
    _commit_all(repo)

    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(
        json.dumps({cls: (["tool.sh"] if cls == "env_shebang" else []) for cls in CLASSES})
    )

    ok, msg = check_against_baseline(repo, baseline_path)

    assert ok is True
    assert "current scan 1 violation(s)" in msg
    assert "frozen baseline 1 entry(ies)" in msg
    assert "Difference explained by" not in msg


# ---------------------------------------------------------------------------
# check_no_stale_baseline_entries: stale grandfather slots.
# ---------------------------------------------------------------------------

def test_check_no_stale_baseline_entries_is_green_when_baseline_is_subset_of_scan(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "tool.sh").write_text("#!/usr/bin/env bash\n")
    _commit_all(repo)

    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(
        json.dumps({cls: (["tool.sh"] if cls == "env_shebang" else []) for cls in CLASSES})
    )

    ok, msg = check_no_stale_baseline_entries(repo, baseline_path)

    assert ok is True, msg
    assert "OK" in msg


def test_check_no_stale_baseline_entries_reds_on_path_absent_from_scan(tmp_path):
    """A baseline entry naming a path the current scan does not produce for
    that class -- deleted, renamed, or fixed -- is a dead grandfather slot
    that must be surfaced RED, named, and attributed to its class."""
    repo = _init_repo(tmp_path)
    (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo)

    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline_path.write_text(
        json.dumps(
            {cls: (["deleted-tool.sh"] if cls == "env_shebang" else []) for cls in CLASSES}
        )
    )

    ok, msg = check_no_stale_baseline_entries(repo, baseline_path)

    assert ok is False, msg
    assert "STALE BASELINE ENTRIES" in msg
    assert "env_shebang" in msg
    assert "deleted-tool.sh" in msg
    assert "shrink-only" in msg


def test_check_no_stale_baseline_entries_isolates_per_class(tmp_path):
    """A stale entry in one class must not be misattributed to another, and
    a class with no stale entries must not appear in the stale listing."""
    repo = _init_repo(tmp_path)
    (repo / "tool.sh").write_text("#!/usr/bin/env bash\n")
    _commit_all(repo)

    baseline_dir = repo / "state"
    baseline_dir.mkdir()
    baseline_path = baseline_dir / "posix-exec-baseline.json"
    baseline = {cls: [] for cls in CLASSES}
    baseline["env_shebang"] = ["tool.sh"]  # still live -- not stale
    baseline["mode_100755"] = ["nonexistent-mode-entry.sh"]  # stale
    baseline_path.write_text(json.dumps(baseline))

    ok, msg = check_no_stale_baseline_entries(repo, baseline_path)

    assert ok is False, msg
    assert "mode_100755" in msg
    assert "nonexistent-mode-entry.sh" in msg
    # env_shebang's still-live entry must not be listed as stale.
    env_shebang_idx = msg.find("env_shebang:")
    assert env_shebang_idx == -1 or "stale entry" not in msg[env_shebang_idx:env_shebang_idx + 40]
