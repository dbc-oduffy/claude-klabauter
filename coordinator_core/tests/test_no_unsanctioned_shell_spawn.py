"""Standing regrowth gate: every shell-shaped spawn in the repo must be sanctioned.

Walks the repo via `coordinator_core.spawn_policy.walk_repo`, loads the
carve-out register via `load_allowlist`, and fails loudly on any
shell-shaped call-site (`coordinator/bin/` included — its omission is
exactly what let this regrow, AC7) that is not an EXACT structural +
digest match against the register.

Negative-spec (RAG-bait): this gate does not tolerate an unparseable
file. `test_operator_cli_engine_contact.py`'s `except SyntaxError: return
False` reports an unparseable file as clean — this gate's core dependency
(`sites_in_source`) instead raises `SpawnParseError`, and this module
propagates that as a loud test failure rather than swallowing it.

Spec backlink: docs/plans/2026-08-06-shell-spawn-regrowth-gate.md, C3.
See also tasks/shell-spawn-regrowth-gate/PINNED-API.md.
"""

from __future__ import annotations

import pathlib

import pytest

from coordinator_core.spawn_policy import (
    SpawnKind,
    SpawnParseError,
    is_sanctioned,
    is_test_tree_site,
    load_allowlist,
    site_key,
    sites_in_source,
    unpinned_entries,
    walk_repo,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_comment_only_mention_is_not_counted():
    source = "# use bash to run this: bash script.sh\n"
    assert sites_in_source(source, "x.py") == []


def test_docstring_only_mention_is_not_counted():
    source = '"""This module used to shell out to bash script.sh."""\n'
    assert sites_in_source(source, "x.py") == []


def test_string_literal_mention_is_not_counted():
    source = '_NOTE = "run bash script.sh by hand"\n'
    assert sites_in_source(source, "x.py") == []


def test_allowlist_token_in_comment_does_not_exempt_real_call():
    source = (
        "import subprocess\n"
        "# sanctioned: cls a, this is fine\n"
        "def f():\n"
        "    subprocess.run(['bash', 'script.sh'])\n"
    )
    sites = sites_in_source(source, "x.py")
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert not is_sanctioned(sites[0], [])


def test_genuine_shell_true_is_counted():
    source = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run('echo hi', shell=True)\n"
    )
    sites = sites_in_source(source, "x.py")
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_TRUE


def test_powershell_and_pwsh_are_counted():
    source = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['powershell.exe', '-Command', 'x'])\n"
        "def g():\n"
        "    subprocess.run(['pwsh.exe', '-Command', 'x'])\n"
    )
    sites = sites_in_source(source, "x.py")
    assert len(sites) == 2
    assert all(s.kind == SpawnKind.SHELL_BINARY for s in sites)


def test_line_number_moved_site_still_matches():
    source_a = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['bash', 'script.sh'])\n"
    )
    source_b = (
        "import subprocess\n"
        "\n\n\n\n\n"
        "def f():\n"
        "    subprocess.run(['bash', 'script.sh'])\n"
    )
    sites_a = sites_in_source(source_a, "x.py")
    sites_b = sites_in_source(source_b, "x.py")
    assert len(sites_a) == 1 and len(sites_b) == 1
    assert site_key(sites_a[0]) == site_key(sites_b[0])
    assert sites_a[0].lineno != sites_b[0].lineno


def test_unparseable_file_fails_loudly():
    with pytest.raises(SpawnParseError):
        sites_in_source("def f(:\n    pass\n", "broken.py")


def test_indirection_via_local_helper_is_counted():
    source = (
        "import subprocess\n"
        "def _run(a):\n"
        "    subprocess.run(a)\n"
        "def f(script):\n"
        "    _run(['bash', script])\n"
    )
    sites = sites_in_source(source, "x.py")
    # The direct `subprocess.run(a)` inside `_run` is its own site (argv0
    # unresolvable there); the indirected call-site through the helper at
    # `f`'s call to `_run(['bash', script])` is a second, distinct site.
    assert len(sites) == 2
    indirected = [s for s in sites if s.enclosing == "f"]
    assert len(indirected) == 1
    assert indirected[0].argv0 == "bash"
    assert indirected[0].kind == SpawnKind.SHELL_BINARY


def test_argv0_from_module_constant_list_concat_and_fstring():
    source = (
        "import subprocess\n"
        "_BASH = 'bash'\n"
        "def f(script):\n"
        "    subprocess.run([_BASH, script])\n"
        "def g(extra, script):\n"
        "    subprocess.run(['ba' + 'sh'] + extra)\n"
        "def h(script):\n"
        "    subprocess.run([f'bash', script])\n"
    )
    sites = sites_in_source(source, "x.py")
    # All three sites must be counted (present in the corpus, never silently
    # dropped); the module-constant and f-string forms resolve cleanly to
    # "bash", the concat form resolves to its statically-provable prefix.
    assert len(sites) == 3
    by_enclosing = {s.enclosing: s for s in sites}
    assert by_enclosing["f"].argv0 == "bash"
    assert by_enclosing["f"].kind == SpawnKind.SHELL_BINARY
    assert by_enclosing["h"].argv0 == "bash"
    assert by_enclosing["h"].kind == SpawnKind.SHELL_BINARY
    assert by_enclosing["g"].argv0 != "<dynamic>"


def test_shell_bound_to_variable_and_starred_kwargs_are_counted():
    # `**opts` forwarding is opaque -- whether `shell=True` ends up set is
    # statically unknowable, so C1b's SHELL_UNKNOWN bucket (the honest
    # "can't tell") applies to `g`, not SHELL_TRUE. Only the local-variable
    # `shell=use_shell` binding in `f` resolves concretely to SHELL_TRUE.
    source = (
        "import subprocess\n"
        "def f(cmd):\n"
        "    use_shell = True\n"
        "    subprocess.run(cmd, shell=use_shell)\n"
        "def g(cmd, opts):\n"
        "    subprocess.run(cmd, **opts)\n"
    )
    sites = sites_in_source(source, "x.py")
    assert len(sites) == 2
    by_enclosing = {s.enclosing: s for s in sites}
    assert by_enclosing["f"].kind == SpawnKind.SHELL_TRUE
    assert by_enclosing["g"].kind == SpawnKind.SHELL_UNKNOWN


def test_non_subprocess_exec_surfaces_are_counted():
    source = (
        "import os, pty, asyncio\n"
        "def f():\n"
        "    os.execv('/bin/bash', ['bash'])\n"
        "def g():\n"
        "    os.execvp('bash', ['bash'])\n"
        "def h():\n"
        "    os.popen('bash script.sh')\n"
        "def i():\n"
        "    pty.spawn(['bash'])\n"
        "async def j():\n"
        "    await asyncio.create_subprocess_shell('bash script.sh')\n"
    )
    sites = sites_in_source(source, "x.py")
    assert len(sites) == 5


def test_ordinal_reshuffle_interloper_does_not_inherit_allowlist_entry():
    original_source = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['bash', 'sanctioned.sh'])\n"
    )
    original_sites = sites_in_source(original_source, "x.py")
    assert len(original_sites) == 1
    original_site = original_sites[0]

    from coordinator_core.spawn_policy import AllowlistEntry

    entry = AllowlistEntry(
        cls="a",
        path=original_site.path,
        enclosing=original_site.enclosing,
        argv0=original_site.argv0,
        ordinal=original_site.ordinal,
        argv_digest=original_site.argv_digest,
        reason="test fixture",
        ruled_on="2026-08-06",
    )
    assert is_sanctioned(original_site, [entry])

    reshuffled_source = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['bash', 'interloper.sh'])\n"
        "    subprocess.run(['bash', 'sanctioned.sh'])\n"
    )
    reshuffled_sites = sites_in_source(reshuffled_source, "x.py")
    assert len(reshuffled_sites) == 2
    interloper, moved_original = reshuffled_sites
    assert interloper.ordinal == 0
    assert moved_original.ordinal == 1

    assert not is_sanctioned(interloper, [entry])
    assert not is_sanctioned(moved_original, [entry])


def test_meta_fixture_kind_taxonomy_is_exhaustive_over_corpus():
    corpus = (
        "import subprocess, os, pty, asyncio\n"
        "def a():\n"
        "    subprocess.run(['bash', 'x.sh'])\n"
        "def b():\n"
        "    subprocess.run('echo hi', shell=True)\n"
        "def c():\n"
        "    subprocess.run(['python3', 'x.py'])\n"
        "def d():\n"
        "    os.system('echo hi')\n"
        "def e():\n"
        "    os.execv('/bin/ls', ['ls'])\n"
    )
    sites = sites_in_source(corpus, "x.py")
    assert sites
    for site in sites:
        assert isinstance(site.kind, SpawnKind)
        assert site.kind in set(SpawnKind)

    kinds_seen = {s.kind for s in sites}
    assert kinds_seen == {
        SpawnKind.SHELL_BINARY,
        SpawnKind.SHELL_TRUE,
        SpawnKind.PLAIN_SPAWN,
    }


def test_allowlist_is_fully_pinned():
    assert unpinned_entries(load_allowlist()) == []


_SHELL_SHAPED = frozenset({SpawnKind.SHELL_BINARY, SpawnKind.SHELL_TRUE})


def _scan_repo():
    """One `walk_repo(REPO_ROOT)` call, partitioned into reporting buckets.

    Relies on the detector's own default exclude vocabulary
    (`spawn_policy.detect.DEFAULT_EXCLUDE`) rather than re-listing
    directory names here -- it already covers scratch/scratchpad plus
    vendored/generated trees (`.venv`, `venv`, `node_modules`, `dist`,
    `build`, `.git`, `__pycache__`), and re-deriving that list in this
    gate would drift from the shared detector the moment either changes.
    `ExcludedReport.paths`/`suppressed_site_count` are surfaced verbatim
    in the failure/summary message rather than a hardcoded pair, so an
    exclusion nobody names here is impossible.

    Returns (production_shell_shaped, test_tree_count, excluded_report,
    shell_unknown_count).
    """
    sites, excluded = walk_repo(REPO_ROOT)

    production_shell_shaped = []
    test_tree_count = 0
    shell_unknown_count = 0
    for site in sites:
        if is_test_tree_site(site.path):
            test_tree_count += 1
            continue
        if site.kind == SpawnKind.SHELL_UNKNOWN:
            shell_unknown_count += 1
            continue
        if site.kind in _SHELL_SHAPED:
            production_shell_shaped.append(site)

    return production_shell_shaped, test_tree_count, excluded, shell_unknown_count


def _unsanctioned_sites() -> list[str]:
    production_shell_shaped, _test_tree_count, _excluded, _shell_unknown_count = _scan_repo()
    entries = load_allowlist()
    bad = []
    for site in production_shell_shaped:
        if not is_sanctioned(site, entries):
            bad.append(
                f"{site.path}:{site.lineno} ({site.enclosing}, ordinal={site.ordinal}, "
                f"argv0={site.argv0}, kind={site.kind.value})"
            )
    return sorted(bad)


# Sites the gate must NOT fail on despite being unsanctioned production
# shell-shaped spawns -- structural identity, never a path substring or
# regex, so a *different* spawn appearing in the same function does not
# inherit the exemption.
#
# substrate.py `_powershell` fronts eight Windows machine-mutation call
# sites (user-PATH registry writes via [Environment]::SetEnvironmentVariable,
# the orphan AppX stub delete, `py -0p` resolution). Converting it means
# winreg plus a ctypes WM_SETTINGCHANGE broadcast, none of it verifiable off
# Windows -- materially larger and riskier than the four sites this plan
# scoped, so it is with the PM as its own sized ask rather than converted
# blind or waved through with a silent carve-out. Closes when that PM
# ruling lands and `_powershell` is converted off `powershell.exe`; the
# companion staleness assertion below fires the day that happens so this
# entry cannot silently outlive the site it exempts.
KNOWN_OPEN = frozenset(
    {
        ("coordinator_core/install/substrate.py", "_powershell", "powershell.exe", 0),
    }
)


def _site_identity(site) -> tuple[str, str, str, int]:
    return (site.path, site.enclosing, site.argv0, site.ordinal)


def test_repo_wide_no_unsanctioned_shell_spawn():
    """The gate itself.

    Scope: `coordinator_core/` and `coordinator/` production trees,
    `coordinator/bin/` included (AC7) -- a new `shell=True` there fails
    this gate. Only `SHELL_BINARY`/`SHELL_TRUE` sites count as failures;
    `SHELL_UNKNOWN` (opaque `**kwargs` forwarding -- "can't tell", not
    "shell-shaped") never fails the gate but its count is surfaced below
    so it stays visible. Excludes: the test tree (files under a `tests/`
    directory, or with a `test_*` basename -- out of scope per the plan, a
    different remedy; 2,400 call-sites at this measurement, re-derived live
    each run via `is_test_tree_site` rather than pinned to a stale count)
    and everything in the detector's own `DEFAULT_EXCLUDE` (scratch/scratchpad
    plus vendored/generated trees such as `.venv`, `node_modules`, `dist`,
    `build`).

    The ONE named exception is `KNOWN_OPEN` above (the substrate.py
    `_powershell` site, with the PM as its own sized ask) -- matched by
    exact structural identity, not path substring, so a new spawn landing
    in the same function does not inherit the exemption.
    """
    production_shell_shaped, test_tree_count, excluded, shell_unknown_count = _scan_repo()
    entries = load_allowlist()
    bad = sorted(
        f"{site.path}:{site.lineno} ({site.enclosing}, ordinal={site.ordinal}, "
        f"argv0={site.argv0}, kind={site.kind.value})"
        for site in production_shell_shaped
        if not is_sanctioned(site, entries) and _site_identity(site) not in KNOWN_OPEN
    )
    summary = (
        f"[scope] production shell-shaped sites scanned={len(production_shell_shaped)}, "
        f"test-tree excluded={test_tree_count}, "
        f"detector-default-excluded (scratch/vendored/generated)={excluded.suppressed_site_count} "
        f"(dirs={excluded.paths}), "
        f"SHELL_UNKNOWN (not gated)={shell_unknown_count}, "
        f"KNOWN_OPEN exempted={len(KNOWN_OPEN)}"
    )
    assert not bad, (
        f"{len(bad)} unsanctioned shell-shaped spawn site(s) found:\n"
        + "\n".join(f"  {b}" for b in bad)
        + f"\n\n{summary}"
        + "\n\nFix: either remove the shell-shaped call, or add an exact "
        "structural+digest entry to the docs/reference/shell-out-carve-outs.md "
        "register with a named PM ruling."
    )
    print(summary)


def test_known_open_is_not_stale():
    """KNOWN_OPEN entries must still correspond to a real detected site.

    When the PM's ruling lands and `_powershell` is converted off
    `powershell.exe`, this exemption becomes stale -- this test then fails
    loudly instead of the exemption silently carrying a dead entry forever.
    """
    production_shell_shaped, _test_tree_count, _excluded, _shell_unknown_count = _scan_repo()
    entries = load_allowlist()
    still_unsanctioned_identities = {
        _site_identity(site)
        for site in production_shell_shaped
        if not is_sanctioned(site, entries)
    }
    stale = sorted(KNOWN_OPEN - still_unsanctioned_identities)
    assert not stale, (
        f"KNOWN_OPEN contains {len(stale)} stale entry(ies) that no longer match "
        f"a real unsanctioned production shell-shaped site: {stale}\n"
        "Remove the entry -- the site it exempted was converted or removed."
    )
