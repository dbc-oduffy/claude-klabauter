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


# ---------------------------------------------------------------------------
# AC1/AC2/AC3 -- extensionless-shebang discovery, vendored-tree exclusion,
# and a countable parse-failure path. Spec:
# `docs/plans/2026-08-07-home-resolution-gate-family-reference-rule.md`,
# `## Tasks` / `- id: C1`.
# ---------------------------------------------------------------------------


def test_extensionless_shebang_file_is_discovered(tmp_path):
    """`coordinator/bin/archive-stamp-cli`-shaped: no `.py` suffix, but the
    first line names a Python interpreter -- the exact miss named in
    `state/lessons/2026-07-28-grep-include-py-hides-this-repo-s-extens-e85a40277f72.yaml`."""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text("import os\n", encoding="utf-8")
    script = tmp_path / "pkg" / "some-cli"
    script.write_text(
        "#!/usr/bin/env python3\nimport os\ndef f(p):\n    return os.access(p, os.X_OK)\n",
        encoding="utf-8",
    )
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    discovered = {p.name for p in engine.iter_py_files()}
    assert "some-cli" in discovered
    findings = engine.find_x_ok_checks()
    assert any(f.path == "pkg/some-cli" for f in findings)


def test_extensionless_non_shebang_file_is_not_discovered(tmp_path):
    """An extensionless file with no `#!` first line, or a shebang that does
    not name Python, must not be swept in -- the widening is shebang-scoped,
    not "every extensionless file"."""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "README").write_text("just some notes\n", encoding="utf-8")
    (tmp_path / "pkg" / "run-sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    discovered = {p.name for p in engine.iter_py_files()}
    assert "README" not in discovered
    assert "run-sh" not in discovered


def test_extensionless_shebang_file_under_excluded_tree_is_skipped(tmp_path):
    """AC2: the vendored `pip` tree is excluded for BOTH populations, not
    just the `*.py` glob -- an extensionless shebang file under a `pip/`
    path component must not surface."""
    vendored = tmp_path / "pkg" / "pip" / "cache" / "http-v2"
    vendored.mkdir(parents=True, exist_ok=True)
    (vendored / "blob").write_text("#!/usr/bin/env python\nx = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text("import os\n", encoding="utf-8")
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    discovered = {p.name for p in engine.iter_py_files()}
    assert "blob" not in discovered


def test_unparseable_extensionless_file_is_skipped_not_raised_and_counted(tmp_path):
    """AC3: a shebang-sniffed file that fails to parse (embedded null byte --
    `SyntaxError` on this box's Python 3.13, `ValueError` on the repo's 3.11
    floor) is skipped, never raised, and shows up in `parse_failure_count()`
    -- the skip is countable, not silent."""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text("import os\n", encoding="utf-8")
    bad = tmp_path / "pkg" / "bad-cli"
    bad.write_bytes(b"#!/usr/bin/env python\nx = 1\x00\n")
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    findings = engine.run_all_rules()  # must not raise
    assert all(f.path != "pkg/bad-cli" for rule in findings.values() for f in rule)
    assert engine.parse_failure_count() == 1


def test_stable_sort_order_is_unaffected_by_shebang_widening(tmp_path):
    """`iter_py_files()` still returns a stably-sorted sequence with the
    widened population mixed in -- baseline keys must not churn on ordering
    alone."""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "z_mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "a-cli").write_text("#!/usr/bin/env python\nx = 1\n", encoding="utf-8")
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    names = [p.name for p in engine.iter_py_files()]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# C2 -- structural terminal-rung detection for `find_bare_home_or_chains`.
# Spec: `docs/plans/2026-08-07-home-resolution-gate-family-reference-rule.md`,
# `## Tasks` / `- id: C2`.
# ---------------------------------------------------------------------------


def test_bare_or_chain_with_genuine_path_home_terminal_is_exempt(tmp_path):
    """The still-recognised correct shape: a real `Path.home()` call as the
    chain's final rung -- must not regress."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or str(Path.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_bare_or_chain_ternary_path_home_terminal_is_exempt(tmp_path):
    """A `Path.home()` reached through a ternary (`X if cond else
    Path.home()`) is a correct terminal rung -- required cross-repo shape
    (example-doctrine-repo `host_probes.py:1118`)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f(claude_home):\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or (\n"
        "        Path(claude_home) if claude_home else Path.home()\n"
        "    )\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_bare_or_chain_variable_named_home_is_not_a_false_exemption(tmp_path):
    """Regression fixture: `Path(home).is_absolute()` false-positived the
    old substring-based `_chain_has_windows_rung` (`"Path" in dumped and
    "home" in dumped.lower()`) purely on the local variable name `home` --
    with no genuine `Path.home()` call anywhere in the chain, this must
    still be reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f(home):\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or (\n"
        "        str(home) if Path(home).is_absolute() else home\n"
        "    )\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_bare_or_chain_with_path_home_and_variable_named_home_is_exempt(tmp_path):
    """The exact regression this chunk exists to prevent: a chain containing
    BOTH a `Path(home).is_absolute()`-shaped naming coincidence AND a
    genuine `Path.home()` terminal (reached through a ternary) -- the
    fleet's MOST correct site shape. Structural matching finds the real
    `Path.home()` call directly, independent of the unrelated `home`-named
    local elsewhere in the same file. NO finding expected."""
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def helper(home):\n"
        "    if Path(home).is_absolute():\n"
        "        pass\n"
        "def resolve(claude_home):\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or (\n"
        "        Path(claude_home) if claude_home else Path.home()\n"
        "    )\n",
        encoding="utf-8",
    )
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    assert engine.find_bare_home_or_chains() == []


def test_bare_or_chain_literal_tilde_is_reported(tmp_path):
    """A literal `"~"` is a violation, never a terminal rung -- it requires
    a subsequent `expanduser`/environment-variable lookup to become a real
    path, and is not itself Windows-safe."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or '~'\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_bare_or_chain_unguarded_expanduser_is_no_longer_exempt(tmp_path):
    """Dropped exemption (this chunk's title): `os.path.expanduser` does NOT
    consult `HOME` on Windows at all -- it reads `USERPROFILE`, then
    `HOMEDRIVE`+`HOMEPATH`, and returns the literal unexpanded `"~"` when
    neither is set -- so a mere `expanduser` call/mention is not proof of
    Windows-safety and must no longer exempt the chain."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or os.path.expanduser('~')\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_bare_or_chain_nearby_expanduser_mention_is_no_longer_exempt(tmp_path):
    """Same drop applied to the nearby-source-window fallback: a comment
    mentioning `expanduser` a few lines away must no longer exempt the
    chain either."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    # falls back to expanduser semantics\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or ''\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_bare_or_chain_nearby_unrelated_userprofile_mention_is_now_reported(tmp_path):
    """C3 regression fixture: this chunk exists to close exactly this gap --
    a genuinely bare chain with an UNRELATED `USERPROFILE` mention (a
    comment) two lines away must no longer be exempted by the old raw
    source-text window match. The exemption is now scoped to the ladder's
    own rungs, not nearby text, so this must be REPORTED."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    # USERPROFILE is set by the caller in this codepath\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or ''\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_bare_or_chain_with_genuine_userprofile_rung_is_exempt(tmp_path):
    """The structural replacement for the dropped nearby-text exemption: a
    chain whose OWN rung is a genuine `environ.get('USERPROFILE')` call is
    still exempt -- an explicit fallback rung, not a coincidental mention."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or ''\n"
        "    )\n",
    )
    assert engine.find_bare_home_or_chains() == []


# ---------------------------------------------------------------------------
# C4 -- ladder-extraction seam (`_iter_ladder_sites`) across four shapes,
# deduped per site. Spec: `docs/plans/2026-08-07-home-resolution-gate-family-
# reference-rule.md`, `## Tasks` / `- id: C4`.
# ---------------------------------------------------------------------------


def test_shape_boolop_or_chain_still_reported_when_bare(tmp_path):
    """Shape 1 (BoolOp `or`-chain) -- unchanged behavior, no Windows rung."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or ''\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_shape_guard_ladder_bare_is_reported(tmp_path):
    """Shape 2 (`if`/`return` guard-ladder) -- the DOMINANT fleet shape, no
    Windows rung: must now be VISIBLE to the rule at all (previously
    invisible -- only BoolOp was walked)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    if os.environ.get('CLAUDE_HOME'):\n"
        "        return os.environ.get('CLAUDE_HOME')\n"
        "    if os.environ.get('HOME'):\n"
        "        return os.environ.get('HOME')\n"
        "    return ''\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_shape_guard_ladder_with_path_home_terminal_is_exempt(tmp_path):
    """Shape 2, correct terminal -- must not regress into a false positive
    now that the guard-ladder is extracted at all."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    if os.environ.get('CLAUDE_HOME'):\n"
        "        return os.environ.get('CLAUDE_HOME')\n"
        "    return Path.home()\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_shape_ternary_standalone_bare_is_reported(tmp_path):
    """Shape 3 (standalone ternary, not nested inside a BoolOp) -- no
    Windows rung."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') if os.environ.get('CLAUDE_HOME') else ''\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_shape_ternary_standalone_with_path_home_terminal_is_exempt(tmp_path):
    """Shape 3, correct terminal reached directly (no local name binding)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') if os.environ.get('CLAUDE_HOME') else Path.home()\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_shape_default_arg_ladder_with_userprofile_rung_is_exempt(tmp_path):
    """Shape 4 (EM ruling -- in scope): the nested default-arg ladder
    `os.environ.get('HOME', os.environ.get('USERPROFILE', ''))` -- the
    literal `USERPROFILE` fallback key is itself an explicit rung of the
    ladder, so the structural `_contains_userprofile_rung` exemption
    applies here with no shape-specific casing needed."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('HOME', os.environ.get('USERPROFILE', ''))\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_shape_default_arg_ladder_bare_is_reported(tmp_path):
    """Shape 4, bare -- no USERPROFILE rung anywhere in the nested
    default-arg chain, no Windows rung: must be visible and reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('HOME', '')\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_dedup_function_reported_once_not_twice(tmp_path):
    """Dedup is a rule-contract obligation, not a nicety (spec): the spike's
    prototype reported the same function twice -- once as an expression
    (the BoolOp/ternary walk) and once as a function-body guard-ladder. A
    function combining both an `if`/`return` guard AND a trailing bare-or
    chain as its fallback must yield exactly ONE finding, not two."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    if os.environ.get('CLAUDE_HOME'):\n"
        "        return os.environ.get('CLAUDE_HOME')\n"
        "    return os.environ.get('HOME') or ''\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# Cross-repo fixtures (example-doctrine-repo@9e0fb5c44 shapes, verbatim) -- required so
# C2's structural terminal detection is proven against them before this
# ladder-extraction widening lands. Every one terminates in `Path.home()`
# and must stay exempt against an EMPTY baseline.
# ---------------------------------------------------------------------------


def test_cross_repo_guard_ladder_return_path_home_is_exempt(tmp_path):
    """example-doctrine-repo shape 1: a guard-ladder `return Path.home()`, with each guard's
    test/return value bound to a preceding local variable -- exercises
    `_extract_guard_ladder`'s name-binding resolution, not just the trivial
    direct-call form."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def resolve_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME')\n"
        "    if claude_home:\n"
        "        return Path(claude_home)\n"
        "    home = os.environ.get('HOME')\n"
        "    if home:\n"
        "        return Path(home)\n"
        "    return Path.home()\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_cross_repo_ternary_over_locally_bound_env_read_is_exempt(tmp_path):
    """example-doctrine-repo shape 2: a ternary over a locally-bound env read terminating in
    `Path.home()` -- NOT extracted as a ladder site at all (the declared
    known miss), so it naturally produces no finding and "stays exempt" by
    virtue of being invisible to this rule, not by being classified as
    correct."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def resolve_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME')\n"
        "    return (\n"
        "        Path(claude_home) if claude_home else Path.home()\n"
        "    )\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_cross_repo_boolop_or_str_path_home_is_exempt(tmp_path):
    """example-doctrine-repo shape 3: `os.environ.get('CLAUDE_HOME') or str(Path.home())`."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def resolve_home():\n"
        "    return os.environ.get('CLAUDE_HOME') or str(Path.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


# ---------------------------------------------------------------------------
# C5 -- `rung_order`: subsequence test against the master ordering
# CLAUDE_HOME -> HOME -> USERPROFILE -> Path.home(). Ladder-kind-agnostic --
# no fixture here branches on bootstrap-vs-contents kind before scoring
# order. Spec: `docs/plans/2026-08-07-home-resolution-gate-family-reference-
# rule.md`, `## Tasks` / `- id: C5`; transcribed from
# `example-doctrine-repo@coordinator/docs/wiki/portability-gates-spec.md` spec_version
# 1.3.0 Home-resolution gate family (read at `example-doctrine-repo@9e0fb5c44`).
# ---------------------------------------------------------------------------


def test_rung_order_transposed_rungs_is_reported(tmp_path):
    """Transposed rungs (spike fixture table): USERPROFILE checked before
    HOME -- out of master order, must be reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or os.environ.get('HOME')\n"
        "        or ''\n"
        "    )\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1


def test_rung_order_claude_home_userprofile_home_is_reported(tmp_path):
    """Spike fixture table: `CLAUDE_HOME -> USERPROFILE -> HOME` FAILS --
    USERPROFILE (order 2) precedes HOME (order 1)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or os.environ.get('HOME')\n"
        "    )\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1


def test_rung_order_literal_tilde_terminal_is_reported(tmp_path):
    """Spike fixture table: a literal `"~"` terminal FAILS -- never a valid
    terminal rung regardless of order."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or '~'\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1


def test_rung_order_unguarded_expanduser_is_reported(tmp_path):
    """Spike fixture table: an unguarded `expanduser` rung WARNs (reported
    via this rule's single findings list, same mechanism as FAIL)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or os.path.expanduser('~')\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1


def test_rung_order_absent_rung_mid_ladder_passes(tmp_path):
    """Ruled, not open (brief): a skipped rung mid-ladder PASSES --
    `CLAUDE_HOME -> USERPROFILE -> Path.home()` is valid and
    Windows-correct. Must NOT be reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or Path.home()\n"
        "    )\n",
    )
    assert engine.find_rung_order_violations() == []


def test_rung_order_canonical_contents_ladder_passes(tmp_path):
    """Spike fixture table: the canonical contents ladder (all four rungs,
    in master order) PASSES."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or Path.home()\n"
        "    )\n",
    )
    assert engine.find_rung_order_violations() == []


def test_rung_order_bootstrap_ladder_passes(tmp_path):
    """Spike fixture table + brief's ladder-kind-agnostic requirement: the
    bootstrap ladder is the contents ladder minus its first (CLAUDE_HOME)
    rung -- still a subsequence of the master order, so it PASSES with no
    kind-branching needed."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or Path.home()\n"
        "    )\n",
    )
    assert engine.find_rung_order_violations() == []


def test_rung_order_unrelated_chain_is_not_a_site(tmp_path):
    """A chain with no CLAUDE_HOME/HOME rung at all is not a home-resolution
    ladder site -- must not be scored (same gate as `bare_or`)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('SOME_OTHER_VAR') or os.environ.get('USERPROFILE') or ''\n",
    )
    assert engine.find_rung_order_violations() == []


# ---------------------------------------------------------------------------
# C5b -- false-positive fix: a shape-4 default-arg ladder's OUTER rung (the
# whole `environ.get(key, default)` call) must classify by its OWN key, not
# by a `Path.home()` call nested inside its default arg. Spec: chunk C5b
# dispatch brief, root-caused against `_classify_rung`'s check order.
# ---------------------------------------------------------------------------


def test_rung_order_default_arg_ladder_with_nested_path_home_default_passes(tmp_path):
    """C5b regression table row 1: `os.environ.get("CLAUDE_HOME",
    str(Path.home()))` is correct code -- the outer rung is the CLAUDE_HOME
    key itself, not a `PATH_HOME` rung, so scoring it against the nested
    `str(Path.home())` terminal must not read as a same-rank transposition.
    Must NOT be reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME', str(Path.home()))\n",
    )
    assert engine.find_rung_order_violations() == []


def test_rung_order_boolop_all_four_rungs_in_order_passes(tmp_path):
    """C5b regression table row 2 (control): a BoolOp chain visiting all
    four rungs in master order stays clean -- must not regress."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or str(Path.home())\n"
        "    )\n",
    )
    assert engine.find_rung_order_violations() == []


def test_rung_order_boolop_userprofile_before_home_is_reported(tmp_path):
    """C5b regression table row 3 (control): USERPROFILE transposed before
    HOME must still be reported -- proves the fix does not over-correct
    into silencing genuine transpositions."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or os.environ.get('HOME')\n"
        "        or str(Path.home())\n"
        "    )\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# C5c -- `_contains_path_home_call` blind to `Path.home() / "suffix"`
# (`ast.BinOp` never recursed into). Spec: chunk C5c dispatch brief, the
# nine-row regression table -- all nine held simultaneously.
# ---------------------------------------------------------------------------


def test_c5c_bare_path_home_still_exempt(tmp_path):
    """Row 1 (control): `Path.home()` alone stays a correct terminal."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or Path.home()\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c5c_str_path_home_still_exempt(tmp_path):
    """Row 2 (control): `str(Path.home())` stays a correct terminal."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or str(Path.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c5c_path_home_binop_join_is_exempt(tmp_path):
    """Row 3 -- the defect: `Path.home() / ".claude"` must now be recognised
    as a correct terminal rung, not silently missed via `ast.BinOp`."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or (Path.home() / '.claude')\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c5c_bare_or_with_binop_join_rung_is_exempt(tmp_path):
    """Row 4: `os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude")`
    -- BoolOp shape with a BinOp-wrapped terminal rung."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or (Path.home() / '.claude')\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c5c_guard_ladder_binop_join_terminal_is_exempt(tmp_path):
    """Row 5: guard-ladder ending `return Path.home() / ".claude"`."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    if os.environ.get('CLAUDE_HOME'):\n"
        "        return os.environ.get('CLAUDE_HOME')\n"
        "    return Path.home() / '.claude'\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c5c_default_arg_str_path_home_still_exempt(tmp_path):
    """Row 6 (control -- C5b's fix must stay fixed):
    `os.environ.get("CLAUDE_HOME", str(Path.home()))`."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME', str(Path.home()))\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c5c_claude_home_or_home_no_terminal_still_reported(tmp_path):
    """Row 7 (control): `CLAUDE_HOME or HOME` with no Windows rung at all --
    must still be reported, not silenced by the BinOp fix."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME')\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_c5c_claude_home_home_expanduser_still_reported(tmp_path):
    """Row 8 (control): `CLAUDE_HOME or HOME or expanduser("~")` -- the
    genuine defect class, must stay reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('HOME')\n"
        "        or os.path.expanduser('~')\n"
        "    )\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_c5c_transposed_userprofile_before_path_home_still_reported(tmp_path):
    """Row 9 (control): `CLAUDE_HOME or USERPROFILE or HOME or str(Path.home())`
    -- a genuine transposition (`rung_order`, not `bare_or` -- USERPROFILE
    presence exempts `bare_or`, so this row is scored via
    `find_rung_order_violations`), must stay reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or os.environ.get('HOME')\n"
        "        or str(Path.home())\n"
        "    )\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1


def test_self_scan_is_clean_and_terminates(tmp_path):
    """Self-measuring-gate check (`state/lessons/2026-08-07-a-gate-that-measures-a-corpus-must-not-l-dec459bb6300.yaml`):
    `home_resolution_lint.py`'s own file scores clean under the widened
    discovery and the run completes -- no circular re-invocation hang."""
    repo_root = Path(__file__).resolve().parents[2]
    engine = HomeResolutionLintEngine(repo_root=repo_root, scan_roots=("coordinator/lib",))
    findings = engine.run_all_rules()
    self_hits = [
        f
        for rule_findings in findings.values()
        for f in rule_findings
        if f.path == "coordinator/lib/home_resolution_lint.py"
    ]
    assert self_hits == []
