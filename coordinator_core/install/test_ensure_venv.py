"""
coordinator_core.install.test_ensure_venv — resolution-journal wiring
coverage for coordinator_core.install.ensure_venv (C7 of
docs/research/2026-08-06-install-receipt-persistence-design.md).

This module has no pre-existing test file — `ensure_coordinator_venv`'s
subprocess-heavy mechanics (`_venv_healthy`, `_create_venv`, `_install_deps`,
`_resolve_base_python`, `_resolve_ml_cli`) are each independently
monkeypatchable per the module's own "Native test seam" docstring, so these
tests exercise only the journaling wiring around those seams, not a real
venv build.

`COORDINATOR_PLUGIN_ROOT_TRUSTED=1` bypasses `coordinator_trusted_root_guard`
(fail-loud on an untrusted root) so an arbitrary tmp_path plugin_root can be
used without registering it as a real coordinator/DoE/claude-klabauter anchor.
"""
from __future__ import annotations

import sys

import pytest

from coordinator_core.install import ensure_venv as mod
from coordinator_core.install.ensure_venv import EnsureVenvError, ensure_coordinator_venv


@pytest.fixture(autouse=True)
def _trust_any_root(monkeypatch):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")


@pytest.fixture
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as journal_mod

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_mod


def _resolved(journal_mod):
    journal = journal_mod.read_journal()
    return journal.get("ensure-venv", {}).get(mod._VENV_TREE_CLAUSE_INDEX)


def test_journal_records_already_healthy_venv(tmp_path, _journal_env, monkeypatch):
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: True)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_set_pin", lambda *a, **k: None)

    status = ensure_coordinator_venv(plugin_root, settings_home_path)

    assert status == "ready"
    resolution = _resolved(_journal_env)
    assert resolution is not None
    venv_dir = settings_home_path / ".coordinator-venv"
    assert [e.path for e in resolution.entries] == [str(venv_dir)]
    assert resolution.entries[0].kind == "file-path"


def test_journal_records_successful_rebuild(tmp_path, _journal_env, monkeypatch):
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    # False for the live venv_dir (forces the rebuild path); True for the
    # freshly-built `.build-*` tree (satisfies the pre-swap health probe so
    # this test still exercises the successful-rebuild leg).
    monkeypatch.setattr(
        mod, "_venv_healthy", lambda venv_py, *a, **k: ".build-" in venv_py.parent.parent.name
    )
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_set_pin", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_base_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(mod, "_create_venv", lambda base_py, dst: dst.mkdir(parents=True))
    monkeypatch.setattr(mod, "_resolve_whoami_pkg", lambda *a, **k: plugin_root / "whoami")
    monkeypatch.setattr(mod, "_install_deps", lambda *a, **k: None)

    status = ensure_coordinator_venv(plugin_root, settings_home_path)

    assert status == "rebuilt"
    resolution = _resolved(_journal_env)
    assert resolution is not None
    venv_dir = settings_home_path / ".coordinator-venv"
    assert [e.path for e in resolution.entries] == [str(venv_dir)]


def test_rebuild_not_swapped_when_build_fails_health_probe(tmp_path, _journal_env, monkeypatch):
    """AC (fix #1): a build whose `_create_venv`/`_install_deps` both exit
    zero but whose resulting tree fails `_venv_healthy` must NOT be
    swapped in -- a pre-existing `venv_dir` survives untouched, and the
    call raises `EnsureVenvError` exactly like a `_create_venv`/
    `_install_deps` failure would (same `except EnsureVenvError:` cleanup
    path, not a parallel copy)."""
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    venv_dir = settings_home_path / ".coordinator-venv"
    venv_dir.mkdir()
    marker = venv_dir / "marker.txt"
    marker.write_text("pre-existing tree", encoding="utf-8")

    # Always unhealthy: both the initial (pre-lock) check and the
    # re-check-after-lock must return False to reach the rebuild path at
    # all, AND the new pre-swap probe on the freshly-built tree must also
    # return False to exercise the defect this test targets.
    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: False)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_base_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(mod, "_create_venv", lambda base_py, dst: dst.mkdir(parents=True))
    monkeypatch.setattr(mod, "_resolve_whoami_pkg", lambda *a, **k: plugin_root / "whoami")
    monkeypatch.setattr(mod, "_install_deps", lambda *a, **k: None)

    swap_calls = []
    monkeypatch.setattr(
        mod, "_swap_in_new_venv", lambda *a, **k: swap_calls.append(a)
    )

    with pytest.raises(EnsureVenvError, match="health probe"):
        ensure_coordinator_venv(plugin_root, settings_home_path)

    assert swap_calls == []
    assert venv_dir.is_dir()
    assert marker.read_text(encoding="utf-8") == "pre-existing tree"

    # No leftover `.build-*` sibling from the discarded (unhealthy) tree.
    leftovers = [
        p for p in settings_home_path.iterdir()
        if p.name.startswith(".coordinator-venv.build-")
    ]
    assert leftovers == []

    # Same cleanup accounting as any other failed-rebuild leg: the tree
    # journal records the pre-existing (untouched) venv_dir, not an empty
    # tuple -- this run never destroyed it.
    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert [e.path for e in resolution.entries] == [str(venv_dir)]


def test_journal_empty_entries_on_failed_rebuild(tmp_path, _journal_env, monkeypatch):
    """A rebuild that fails removes the (partial) venv tree — the tree
    genuinely resolved to nothing this run, distinct from never having
    reached this clause at all (see `ensure_coordinator_venv`'s own comment
    at the `except EnsureVenvError` site)."""
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: False)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_base_python", lambda: "/usr/bin/python3")

    def _boom(*a, **k):
        raise EnsureVenvError("simulated pip failure")

    monkeypatch.setattr(mod, "_create_venv", _boom)

    with pytest.raises(EnsureVenvError):
        ensure_coordinator_venv(plugin_root, settings_home_path)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_unreported_on_check_only(tmp_path, _journal_env, monkeypatch):
    """check_only never reaches a mutation decision at all for this
    clause — genuinely "never got there", distinct from the empty-tuple
    "resolved to nothing" case above."""
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: False)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)

    status = ensure_coordinator_venv(plugin_root, settings_home_path, check_only=True)

    assert status == "would-rebuild"
    assert _resolved(_journal_env) is None


def test_journal_omits_entry_when_mutation_disabled(tmp_path, _journal_env, monkeypatch):
    """`ensure_venv.py` itself does not gate its own venv mutation on
    `COORDINATOR_DISABLE_MACHINE_MUTATION` (unlike e.g. shell_rc_guard.py) —
    the journal's OWN append does, via `resolution_journal.record_resolution`'s
    `_refuse_machine_mutation` guard. Setting the kill switch refuses only
    the journal row, leaving this clause UNREPORTED for this run rather than
    journaled with a phantom (or accurate-but-untracked) entry."""
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: True)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_set_pin", lambda *a, **k: None)

    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    status = ensure_coordinator_venv(plugin_root, settings_home_path)

    assert status == "ready"
    assert _resolved(_journal_env) is None


def _iter_hook_script_top_level_imports(hooks_scripts_dir):
    """Yield every module name imported at module top level (not inside a
    function/try block) across ``*.py`` in ``hooks_scripts_dir`` — a rough
    but adequate approximation of "runs unconditionally under the injected
    venv interpreter on every hook fire" (`_hook_venv_inject.py` puts this
    venv's site-packages ahead of the host on `sys.path` for every one of
    the 52 `hooks.json` entries)."""
    import ast

    for path in sorted(hooks_scripts_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:  # module top level only -- not nested in try/def/if
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield node.module.split(".")[0]


# Modules that are stdlib, this-repo-internal (`coordinator_core`), or the
# sibling repo's own first-party packages -- never candidates for
# `_VENV_IMPORT_PROBES` (that list is for THIRD-PARTY packages the venv's
# pip install must provide). Every hook script's own local sibling-module
# imports (`_foo` peers under `hooks/scripts/`) are excluded dynamically
# below, by checking whether a same-named `.py` file exists alongside it --
# a hand-maintained allowlist of those names would itself rot exactly like
# `_VENV_IMPORT_PROBES` did.
#
# `doctrine_surface_tiers` is named explicitly: it is a genuinely
# FIRST-PARTY module (`guard-doctrine-surface-ratio*.py`'s own comment:
# "coordinator/lib/ holds doctrine_surface_tiers.py"), but on THIS box's
# `coordinator-claude` live mirror the `coordinator/lib/` layout its
# `sys.path` insert expects does not exist (it lives only under the sibling
# `DoE-claude/coordinator/lib/`) -- a pre-existing layout mismatch in that
# OTHER repo, out of this repo's remit to fix, and not a pip-installable
# package this venv could ever satisfy regardless.
_NON_THIRD_PARTY_HOOK_IMPORTS = frozenset(
    {"coordinator_core", "coordinator_whoami", "doctrine_surface_tiers"}
)


@pytest.mark.real_home
def test_venv_import_probes_cover_every_hook_scripts_top_level_import():
    """A test that does NOT re-assert `_VENV_IMPORT_PROBES` against itself
    (the thing that lied): it independently derives "what the hook path
    actually needs importable" by scanning coordinator-claude's real
    `hooks/scripts/*.py` for module-level third-party imports, then asserts
    `_VENV_IMPORT_PROBES` is a superset. This is exactly the check that
    would have caught the PyYAML gap -- `enforce-agent-dispatch-mode.py`,
    `handoff-segment-inject.py`, and `_oss_operative_strings.py` all
    `import yaml` at module level, and prior to this chunk `yaml` was not in
    `_VENV_IMPORT_PROBES` at all.

    Skips (not fails) when the sibling coordinator-claude repo cannot be
    resolved on this machine -- this test asserts a cross-repo CONTRACT, not
    this repo's own internals, and a machine without that sibling cloned
    cannot evaluate it."""
    # Same resolution seam `ensure_venv.py` itself uses for registry reads
    # (`_resolve_ml_cli` + `_ml_get`), not `machine_resolver.registry_get`
    # directly -- the machine-local CLI merges layered config the flat
    # registry-file reader does not, and this repo's own registered
    # `repos.coordinator_claude` value is only visible through it.
    from pathlib import Path

    ml_cli = mod._resolve_ml_cli(Path.cwd())
    coordinator_claude_root = mod._ml_get(ml_cli, "repos.coordinator_claude") if ml_cli else None
    if not coordinator_claude_root:
        pytest.skip("repos.coordinator_claude not registered on this machine")

    hooks_scripts_dir = Path(coordinator_claude_root) / "hooks" / "scripts"
    if not hooks_scripts_dir.is_dir():
        pytest.skip(f"{hooks_scripts_dir} not found")

    third_party_imports = set()
    for name in _iter_hook_script_top_level_imports(hooks_scripts_dir):
        if name in _NON_THIRD_PARTY_HOOK_IMPORTS:
            continue
        if name in sys.stdlib_module_names:  # type: ignore[attr-defined]
            continue
        if (hooks_scripts_dir / f"{name}.py").is_file():
            continue  # local sibling module, not a pip-installed package
        # Several hook scripts add a second first-party dir to `sys.path`
        # alongside their own (e.g. a `coordinator/lib/`-style module dir)
        # for a name like `doctrine_surface_tiers` -- not a pip package
        # either. Recognised anywhere under the coordinator-claude tree
        # rather than one hardcoded relative path, so this stays robust to
        # that tree's own internal layout (out of scope for this repo to
        # police) rather than silently misclassifying a first-party module
        # as third-party the moment that layout shifts.
        if next(Path(coordinator_claude_root).rglob(f"{name}.py"), None) is not None:
            continue
        third_party_imports.add(name)

    # `yaml` is PyYAML's import name -- assert directly rather than trusting
    # any particular hook script still exists under that exact filename.
    assert "yaml" in third_party_imports, (
        "expected at least one coordinator-claude hook script to `import "
        "yaml` at module level -- if this no longer holds, the PyYAML probe "
        "may no longer be load-bearing and this test's premise should be "
        "re-checked, not silently deleted"
    )
    missing = third_party_imports - set(mod._VENV_IMPORT_PROBES)
    assert not missing, (
        f"hook scripts under {hooks_scripts_dir} import {sorted(missing)} at "
        "module level, but _VENV_IMPORT_PROBES does not probe for them -- a "
        "rebuilt venv could pass health while still breaking these hooks"
    )
