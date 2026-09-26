
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator.lib.home_resolution_lint import HomeResolutionLintEngine


def _engine_for(tmp_path: Path, source: str) -> HomeResolutionLintEngine:
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text(source, encoding="utf-8")
    return HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))


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
    engine = _engine_for(
        tmp_path,
        "import os, sys\n"
        "def f(p):\n"
        "    if not sys.platform.startswith('win'):\n"
        "        return os.access(p, os.X_OK)\n",
    )
    findings = engine.find_x_ok_checks()
    assert len(findings) == 1


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


def test_extensionless_shebang_file_is_discovered(tmp_path):
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
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "README").write_text("just some notes\n", encoding="utf-8")
    (tmp_path / "pkg" / "run-sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    discovered = {p.name for p in engine.iter_py_files()}
    assert "README" not in discovered
    assert "run-sh" not in discovered


def test_extensionless_shebang_file_under_excluded_tree_is_skipped(tmp_path):
    vendored = tmp_path / "pkg" / "pip" / "cache" / "http-v2"
    vendored.mkdir(parents=True, exist_ok=True)
    (vendored / "blob").write_text("#!/usr/bin/env python\nx = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text("import os\n", encoding="utf-8")
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    discovered = {p.name for p in engine.iter_py_files()}
    assert "blob" not in discovered


def test_unparseable_extensionless_file_is_skipped_not_raised_and_counted(tmp_path):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text("import os\n", encoding="utf-8")
    bad = tmp_path / "pkg" / "bad-cli"
    bad.write_bytes(b"#!/usr/bin/env python\nx = 1\x00\n")
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    findings = engine.run_all_rules()
    assert all(f.path != "pkg/bad-cli" for rule in findings.values() for f in rule)
    assert engine.parse_failure_count() == 1


def test_stable_sort_order_is_unaffected_by_shebang_widening(tmp_path):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "z_mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "a-cli").write_text("#!/usr/bin/env python\nx = 1\n", encoding="utf-8")
    engine = HomeResolutionLintEngine(repo_root=tmp_path, scan_roots=("pkg",))
    names = [p.name for p in engine.iter_py_files()]
    assert names == sorted(names)


def test_bare_or_chain_with_module_scope_aliased_path_home_terminal_is_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path as _Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or str(_Path.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_bare_or_chain_with_function_local_aliased_path_home_terminal_is_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    from pathlib import Path as _P\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or str(_P.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_bare_or_chain_with_unaliased_path_home_terminal_still_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or str(Path.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_bare_or_chain_variable_named_like_path_alias_is_not_a_false_exemption(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(_Path):\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or str(_Path.home())\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_bare_or_chain_with_genuine_path_home_terminal_is_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or str(Path.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_bare_or_chain_ternary_path_home_terminal_is_exempt(tmp_path):
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


def test_shape_boolop_or_chain_still_reported_when_bare(tmp_path):
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
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') if os.environ.get('CLAUDE_HOME') else ''\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_shape_ternary_standalone_with_path_home_terminal_is_exempt(tmp_path):
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


def test_c2_fixture_a_nested_or_in_join_argument_is_reported(tmp_path):
    """DoE's fixture A (2026-08-21 DoE memo, `state/cross-repo/archive/`):
    a ladder nested as a call argument of an outer or-chain --
    `COORDINATOR_SETTINGS_HOME or os.path.join(CLAUDE_HOME or expanduser('~'),
    ...)`. Before C2 the inner CLAUDE_HOME rung is swallowed by the join
    Call staying an opaque leaf, and the outer chain's own operands carry
    no home key, so the site is invisible. Must yield exactly one
    `bare_or` finding once the nested `or` is flattened through."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('COORDINATOR_SETTINGS_HOME')\n"
        "        or os.path.join(\n"
        "            os.environ.get('CLAUDE_HOME') or os.path.expanduser('~'),\n"
        "            '.claude',\n"
        "        )\n"
        "    )\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_c2_nested_or_userprofile_rung_inside_join_argument_is_exempt(tmp_path):
    """AC2's exemption side: an outer CLAUDE_HOME ladder whose USERPROFILE
    rung sits nested inside an `or` BoolOp that itself sits inside a join
    argument -- this nested-`or` shape is the one that requires C2 (the
    plain non-nested USERPROFILE-in-a-join-argument shape was already
    exempt via `_contains_userprofile_rung`'s Call-arg walk and is not by
    itself falsifying). Must not be reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.path.join(\n"
        "            os.environ.get('HOME') or os.environ.get('USERPROFILE'),\n"
        "            '.claude',\n"
        "        )\n"
        "    )\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c2_unrelated_call_with_tilde_default_stays_a_leaf(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('XDG_X', '~')\n",
    )
    assert engine.find_rung_order_violations() == []


def test_c2_dedup_test_stays_unedited_and_green(tmp_path):
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


def test_cross_repo_guard_ladder_return_path_home_is_exempt(tmp_path):
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
    """DoE shape 3: `os.environ.get('CLAUDE_HOME') or str(Path.home())`."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def resolve_home():\n"
        "    return os.environ.get('CLAUDE_HOME') or str(Path.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


# CLAUDE_HOME -> HOME -> USERPROFILE -> Path.home(). Ladder-kind-agnostic --


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
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or '~'\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1


def test_rung_order_unguarded_expanduser_is_a_warn_not_a_violation(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or os.path.expanduser('~')\n",
    )
    assert engine.find_rung_order_violations() == []
    warnings = engine.find_rung_order_warnings()
    assert len(warnings) == 1


def test_rung_order_expanduser_with_transposed_rungs_stays_a_violation(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or os.environ.get('HOME')\n"
        "        or os.path.expanduser('~')\n"
        "    )\n",
    )
    assert len(engine.find_rung_order_violations()) == 1
    assert engine.find_rung_order_warnings() == []


def test_rung_order_environ_or_chain_with_expanduser_terminal_is_warn_only(tmp_path):
    """C5d regression-bar row 1: `CLAUDE_HOME or HOME or USERPROFILE or
    os.path.expanduser("~")` -- correct rung order, expanduser terminal --
    is NOT a violation (warn at most)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return (\n"
        "        os.environ.get('CLAUDE_HOME')\n"
        "        or os.environ.get('HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or os.path.expanduser('~')\n"
        "    )\n",
    )
    assert engine.find_rung_order_violations() == []
    assert len(engine.find_rung_order_warnings()) == 1


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


def test_classify_rung_unrecognised_environ_key_does_not_fall_through_to_path_home(tmp_path):
    """Reviewer finding P2 (code-reviewer 818d3fe7): a rung whose own
    `environ.get` key is present but NOT one of `_RUNG_ORDER`'s recognised
    names (`SOME_OTHER_VAR`) is an unrelated env read and must classify as
    `None` -- NOT fall through to `_contains_path_home_call`, which has no
    key short-circuit and re-walks the SAME node's default-arg branch,
    reintroducing the C5b misclassification (the outer rung reads as
    `PATH_HOME` purely because ITS default arg nests a `Path.home()` call,
    even though the rung's own key is unrelated). Reachable via
    `_default_arg_ladder_rungs`'s any-key unwrap loop
    (`os.environ.get('CLAUDE_HOME', os.environ.get('SOME_OTHER_VAR',
    str(Path.home())))` unwraps its middle rung to exactly this node). Tests
    `_classify_rung` directly since the misclassification's downstream
    effect on `rung_order` scoring depends on adjacency-collapse and is not
    reliably visible through `find_rung_order_violations()` alone."""
    import ast

    source = "os.environ.get('SOME_OTHER_VAR', str(Path.home()))"
    node = ast.parse(source, mode="eval").body
    assert HomeResolutionLintEngine._classify_rung(node) is None


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


def test_c5c_bare_path_home_still_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or Path.home()\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c5c_str_path_home_still_exempt(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def f():\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME') or str(Path.home())\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c5c_path_home_binop_join_is_exempt(tmp_path):
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


def test_rung_order_bound_ladder_reused_across_two_returns_passes(tmp_path):
    """The `_resolve_rc_path` shape: one correct `HOME or USERPROFILE or
    expanduser('~')` ladder, bound once and referenced by two mutually
    exclusive returns. Re-expansion produced `[HOME, USERPROFILE, HOME,
    USERPROFILE]` and scored the splice as a transposition. Must NOT be
    reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(override):\n"
        "    if override:\n"
        "        return override\n"
        "    home = (\n"
        "        os.environ.get('HOME')\n"
        "        or os.environ.get('USERPROFILE')\n"
        "        or os.path.expanduser('~')\n"
        "    )\n"
        "    shell = os.path.basename(os.environ.get('SHELL', '/bin/bash'))\n"
        "    if shell == 'zsh':\n"
        "        return os.path.join(home, '.zshrc')\n"
        "    return os.path.join(home, '.bashrc')\n",
    )
    assert engine.find_rung_order_violations() == []


def test_rung_order_bound_nested_ladder_reused_across_two_returns_passes(tmp_path):
    """The `migrate_state_to_claude_klabauter.main` shape: `CLAUDE_HOME or
    join(USERPROFILE or expanduser('~'), '.claude')`, bound once and passed
    to two different call-and-return branches. Must NOT be reported."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(mode):\n"
        "    if not mode:\n"
        "        return 1\n"
        "    claude_home = os.environ.get('CLAUDE_HOME') or os.path.join(\n"
        "        os.environ.get('USERPROFILE') or os.path.expanduser('~'), '.claude'\n"
        "    )\n"
        "    if mode == 'populate':\n"
        "        return cmd_populate(claude_home)\n"
        "    return cmd_finalize(claude_home)\n",
    )
    assert engine.find_rung_order_violations() == []


def test_rung_order_transposed_bound_ladder_reused_is_still_reported(tmp_path):
    """Control: once-only expansion must not silence a genuine
    transposition. The same reuse shape, but the bound ladder itself puts
    USERPROFILE ahead of HOME -- the FIRST expansion still scores it, so the
    finding survives."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(mode):\n"
        "    if not mode:\n"
        "        return 1\n"
        "    home = (\n"
        "        os.environ.get('USERPROFILE')\n"
        "        or os.environ.get('HOME')\n"
        "        or os.path.expanduser('~')\n"
        "    )\n"
        "    if mode == 'zsh':\n"
        "        return os.path.join(home, '.zshrc')\n"
        "    return os.path.join(home, '.bashrc')\n",
    )
    assert len(engine.find_rung_order_violations()) == 1


def test_rung_order_two_literal_transposed_ladders_still_reported(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(mode):\n"
        "    if mode:\n"
        "        return os.environ.get('USERPROFILE') or os.environ.get('HOME')\n"
        "    return os.environ.get('USERPROFILE') or os.environ.get('HOME')\n",
    )
    assert len(engine.find_rung_order_violations()) == 1


@pytest.mark.designed_red
def test_rung_order_cross_branch_ladder_splice_false_positive_known_gap(tmp_path):
    """Reviewer's reproducer, verbatim (code-reviewer 818d3fe7 finding P1):
    every individual ladder here is correct/Windows-safe, but
    `_extract_guard_ladder` concatenates all top-level guard/return rungs of
    the function into one sequence with no ladder-boundary notion, so the
    classified rank sequence `[0, 1, 2, 0, 1]` (CLAUDE_HOME, HOME,
    USERPROFILE, CLAUDE_HOME, HOME) trips `_rung_order_is_violation`'s
    `collapsed_seq[i] >= collapsed_seq[i+1]` check at the `2, 0` splice --
    reporting a transposition on code with no genuine ordering defect.

    This is a KNOWN, LATENT gap (confirmed against the live corpus as of
    2026-08-08: `rung_order` reports 0 findings today, so no site hits this
    splice class currently) -- not a live false positive. The correct fix is
    scoring per-ladder rather than per-function, a restructure of
    `_iter_ladder_sites`/`_extract_guard_ladder` surfaced to the PM as its
    own plan, not patched here. This test pins the CURRENT (wrong) behaviour
    so a future per-ladder-scoring fix has a red-to-green signal, and a
    reader of the fast/full tiers sees this is excluded by the `designed_red`
    marker (`pyproject.toml`), not silently passing."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f(mode):\n"
        "    if mode == 'a':\n"
        "        return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME')\n"
        "    if os.environ.get('USERPROFILE'):\n"
        "        return os.environ.get('USERPROFILE')\n"
        "    return os.environ.get('CLAUDE_HOME') or os.environ.get('HOME')\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1


# single-level `claude_home = os.environ.get('CLAUDE_HOME', ...)` probe,


def test_c3_probe_then_guard_relaxed_body_is_clean_for_bare_or(tmp_path):
    """The relaxed guard body (more than one statement, ending in a valued
    `return`) must now be recognised at all -- the `claude_home` probe
    qualifies the function via `_extract_guard_ladder`'s relaxed predicate,
    and the post-guard `HOME or USERPROFILE or '~'` ladder carries a
    structural USERPROFILE rung, so `bare_or` must not fire."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def _claude_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
        "    if claude_home:\n"
        "        claude_home = claude_home.strip()\n"
        "        return Path(claude_home)\n"
        "    home = os.environ.get('HOME') or os.environ.get('USERPROFILE') or '~'\n"
        "    return Path(home)\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c3_probe_then_guard_tilde_terminal_reports_at_probe_line(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def _claude_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
        "    if claude_home:\n"
        "        claude_home = claude_home.strip()\n"
        "        return Path(claude_home)\n"
        "    home = os.environ.get('HOME') or os.environ.get('USERPROFILE') or '~'\n"
        "    return Path(home)\n",
    )
    findings = engine.find_rung_order_violations()
    assert len(findings) == 1
    assert findings[0].line == 4


def test_c3_probe_then_guard_no_post_guard_rung_reports_once_at_probe_line(tmp_path):
    """The same probe-then-guard shape with the post-guard rung removed
    entirely (no HOME/USERPROFILE/Path.home() fallback at all) must report
    exactly once, at the probe line -- the C3 representative-move applies
    whether the site is clean or a genuine violation."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def _claude_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
        "    if claude_home:\n"
        "        claude_home = claude_home.strip()\n"
        "        return Path(claude_home)\n"
        "    return Path('')\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1
    assert findings[0].line == 4


def test_c3_binop_transparent_wrapper_resolves_joined_terminal(tmp_path):
    """`home = HOME or USERPROFILE` then `return Path(home) / '.claude'` --
    the `BinOp` path-join in the post-guard return must be a transparent
    wrapper in `_extract_rungs` so the bound `home` Name resolves through
    to its HOME/USERPROFILE rungs instead of the whole `BinOp` staying one
    unclassifiable leaf. Yields zero `rung_order` violations (correct
    order) and no `bare_or` finding (USERPROFILE rung present)."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "def _claude_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
        "    if claude_home:\n"
        "        claude_home = claude_home.strip()\n"
        "        return Path(claude_home)\n"
        "    home = os.environ.get('HOME') or os.environ.get('USERPROFILE')\n"
        "    return Path(home) / '.claude'\n",
    )
    assert engine.find_rung_order_violations() == []
    assert engine.find_bare_home_or_chains() == []


def test_c3_shape_default_arg_ladder_bare_is_reported_stays_unedited(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return os.environ.get('HOME', '')\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


_EXAMPLE_GAME_REPO_RESOLVE_CLAUDE_HOME = (
    "import os\n"
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def resolve_home_base():\n"
    "    return os.environ.get('USERPROFILE') or Path.home()\n"
    "\n"
    "\n"
    "def _memoised(key, fn):\n"
    "    return fn()\n"
    "\n"
    "\n"
    "def _resolve_claude_home_uncached():\n"
    "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
    "    if claude_home:\n"
    "        claude_home = claude_home.strip()\n"
    "        return Path(claude_home)\n"
    "    return resolve_home_base()\n"
    "\n"
    "\n"
    "def resolve_claude_home():\n"
    "    return _memoised('claude_home', _resolve_claude_home_uncached)\n"
    "\n"
    "\n"
    "def _resolve_claude_json_uncached():\n"
    "    return resolve_claude_home().parent / '.claude.json'\n"
    "\n"
    "\n"
    "def _resolve_claude_plugins_uncached():\n"
    "    return resolve_claude_home() / 'plugins'\n"
)


def test_c4_example_game_repo_resolve_claude_home_fixture_is_clean(tmp_path):
    """The full reduced `resolve_claude_home.py` fixture is clean:
    `_resolve_claude_home_uncached`'s post-guard rung
    (`resolve_home_base()`) delegates to a directly resolution-complete
    function, so `bare_or` does not fire on it, and the other four
    functions carry no `CLAUDE_HOME`/`HOME` `environ.get` rung of their own
    at all, so they were never bare_or candidates in the first place."""
    engine = _engine_for(tmp_path, _EXAMPLE_GAME_REPO_RESOLVE_CLAUDE_HOME)
    assert engine.find_bare_home_or_chains() == []


def test_c4_delegation_to_non_complete_helper_reports(tmp_path):
    """A delegation to a same-module helper that returns a bare literal
    (no `Path.home()`/USERPROFILE rung, and no further delegation) is NOT
    resolution-complete, so the delegation does not exempt the caller -- it
    still reports."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "\n"
        "\n"
        "def _fallback():\n"
        "    return ''\n"
        "\n"
        "\n"
        "def _claude_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
        "    if claude_home:\n"
        "        return claude_home\n"
        "    return _fallback()\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_c4_delegation_to_imported_name_reports(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from otherpkg.helpers import resolve_home_base\n"
        "\n"
        "\n"
        "def _claude_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
        "    if claude_home:\n"
        "        return claude_home\n"
        "    return resolve_home_base()\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_c4_delegation_to_bare_path_home_helper_is_clean(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def _home():\n"
        "    return str(Path.home())\n"
        "\n"
        "\n"
        "def _claude_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
        "    if claude_home:\n"
        "        return claude_home\n"
        "    return _home()\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_c4_two_function_mutual_recursion_terminates(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "\n"
        "\n"
        "def f():\n"
        "    return g()\n"
        "\n"
        "\n"
        "def g():\n"
        "    return f()\n"
        "\n"
        "\n"
        "def _claude_home():\n"
        "    claude_home = os.environ.get('CLAUDE_HOME', '')\n"
        "    if claude_home:\n"
        "        return claude_home\n"
        "    return f()\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1


def test_env_read_interpolated_only_into_print_message_is_not_reported(tmp_path):
    """Example-Game-Repo site 8: a reduced copy of `machine_local_reader.py::
    _warn_legacy_claude_home_shape_once`'s `print(f"...{os.environ.get(
    'CLAUDE_HOME', '')}...", file=sys.stderr)` shape is not a home-resolution
    ladder site -- the shape-4 default-arg pass must not score it."""
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "import sys\n"
        "def _warn_legacy_claude_home_shape_once():\n"
        "    print(\n"
        "        f\"legacy CLAUDE_HOME shape: {os.environ.get('CLAUDE_HOME', '')}\",\n"
        "        file=sys.stderr,\n"
        "    )\n",
    )
    assert engine.find_bare_home_or_chains() == []


def test_env_read_assigned_from_fstring_still_reports(tmp_path):
    engine = _engine_for(
        tmp_path,
        "import os\n"
        "def f():\n"
        "    return f\"{os.environ.get('HOME', '')}/.claude\"\n",
    )
    findings = engine.find_bare_home_or_chains()
    assert len(findings) == 1
