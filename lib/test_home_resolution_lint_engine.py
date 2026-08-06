"""Engine-level unit tests for the X_OK guard-shape exemption.

Spec backlink: `home_resolution_lint.py`'s `find_x_ok_checks` /
`_guard_polarity` docstrings -- this file is the both-sides test the fix
for the "message names a remediation the engine can't accept" defect
needed and did not have (2026-07-28). Every shape enumerated in
`find_x_ok_checks`'s "recognised" and "explicitly NOT recognised" lists
gets a case here, on synthetic source written to a tmp_path repo rather
than against the live tree -- the live-tree assertions belong to
`coordinator_core/tests/test_home_resolution_lint.py`, this file is the
engine's own contract test, independent of any one caller's scan roots.
"""

from __future__ import annotations

from pathlib import Path

from coordinator.lib.home_resolution_lint import HomeResolutionLintEngine


def _engine_for(tmp_path: Path, source: str) -> HomeResolutionLintEngine:
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text(source, encoding="utf-8")
    return HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))


# ---------------------------------------------------------------------------
# Still reported -- must not regress.
# ---------------------------------------------------------------------------


def test_bare_unguarded_call_is_reported(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(p):\n"
        "    return os.access(p, os.X_OK)\n",
    )
    findings = engine.find_x_ok_checks()
    assert len(findings) == 1
    assert findings[0].line == 3


def test_call_inside_unrelated_if_is_reported(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(p, verbose):\n"
        "    if verbose:\n"
        "        return os.access(p, os.X_OK)\n",
    )
    findings = engine.find_x_ok_checks()
    assert len(findings) == 1
    assert findings[0].line == 4


def test_call_inside_windows_only_guard_is_reported(tmp_path):
    """The deliberately-covered inversion (task step 2): `os.name == "nt"`
    wraps Windows-only execution of a check meaningless on Windows -- must
    still be reported, never exempted."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(p):\n"
        "    if os.name == 'nt':\n"
        "        return os.access(p, os.X_OK)\n",
    )
    findings = engine.find_x_ok_checks()
    assert len(findings) == 1
    assert findings[0].line == 4


def test_call_inside_sys_platform_windows_only_guard_is_reported(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os, sys\n"
        "def f(p):\n"
        "    if sys.platform == 'win32':\n"
        "        return os.access(p, os.X_OK)\n",
    )
    findings = engine.find_x_ok_checks()
    assert len(findings) == 1
    assert findings[0].line == 4


def test_guarded_caller_does_not_exempt_unguarded_callee_body(tmp_path):
    """A call whose CALLER is invoked from inside a guard is still reported
    -- the guard does not propagate through a function call, only through
    lexical (syntactic) nesting."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def check(p):\n"
        "    return os.access(p, os.X_OK)\n"
        "\n"
        "def f(p):\n"
        "    if os.name != 'nt':\n"
        "        return check(p)\n",
    )
    findings = engine.find_x_ok_checks()
    assert len(findings) == 1
    assert findings[0].line == 3


def test_unrecognised_shape_startswith_win_is_not_silently_exempted(tmp_path):
    """`sys.platform.startswith("win")` is explicitly out of the recognised
    inventory (a Call, not a Compare) -- a call guarded ONLY by this shape
    must still be reported."""
    engine = _engine_for(
        tmp_path,
        "import os, sys\n"
        "def f(p):\n"
        "    if not sys.platform.startswith('win'):\n"
        "        return os.access(p, os.X_OK)\n",
    )
    findings = engine.find_x_ok_checks()
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# Now exempt -- the recognised guard shapes.
# ---------------------------------------------------------------------------


def test_os_name_not_nt_guard_is_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(p):\n"
        "    if os.name != 'nt':\n"
        "        return os.access(p, os.X_OK)\n",
    )
    assert engine.find_x_ok_checks() == []


def test_os_name_posix_guard_is_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(p):\n"
        "    if os.name == 'posix':\n"
        "        return os.access(p, os.X_OK)\n",
    )
    assert engine.find_x_ok_checks() == []


def test_sys_platform_not_win32_guard_is_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os, sys\n"
        "def f(p):\n"
        "    if sys.platform != 'win32':\n"
        "        return os.access(p, os.X_OK)\n",
    )
    assert engine.find_x_ok_checks() == []


def test_windows_only_else_branch_is_exempt(tmp_path):
    """`if os.name == "nt": ... elif <X_OK call>:` -- the `elif`/`else`
    branch of a windows-only guard is itself windows-excluded (this is the
    live shape `coordinator_core/install/_shared.py` needed)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(p):\n"
        "    if os.name == 'nt':\n"
        "        return None\n"
        "    elif os.access(p, os.X_OK):\n"
        "        return p\n",
    )
    assert engine.find_x_ok_checks() == []


def test_guard_recognised_when_nested_inside_another_block(tmp_path):
    """Required case: a recognised guard nested arbitrarily deep still
    exempts (a bare `if`/`for` wrapping the guard does not defeat it)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(paths, verbose):\n"
        "    if verbose:\n"
        "        for p in paths:\n"
        "            if os.name != 'nt':\n"
        "                if p:\n"
        "                    return os.access(p, os.X_OK)\n",
    )
    assert engine.find_x_ok_checks() == []
