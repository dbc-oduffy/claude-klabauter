"""Ratchet: every guard name reaching ``apply_guard_level`` is classified in ``deny_registry``.

Names come from two sources, both resolved here rather than listed: the ``GUARD_NAME``-style
constants of modules that call ``apply_guard_level``, and the guard tables the bash dispatcher and
the write-guard engine pass a variable ``name`` over. A guard in none of ``GUARD_DENY_CLASS``,
``SUBAGENT_DENY_CLASS`` or ``ADVISORY_BY_RULING`` fails.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from coordinator_core import deny_registry as dr
from coordinator_core import guard_census as gc

_ROOT = Path(dr.__file__).resolve().parent
_CALL = re.compile(r"(def\s+\w+\s*\(\s*|\b)apply_guard_level\(\s*(?:[\"']([a-z0-9-]+)[\"']|(\w+))")
_CONST = re.compile(r"^(\w*GUARD\w*)\s*=\s*[\"']([a-z0-9-]+)[\"']", re.M)


def _static_names() -> dict[str, str]:
    """Guard name -> module path, for every call whose first argument is a resolvable constant."""
    found: dict[str, str] = {}
    for path in sorted(_ROOT.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if "apply_guard_level(" not in src:
            continue
        consts = dict(_CONST.findall(src))
        module = ".".join(("coordinator_core", *path.relative_to(_ROOT).with_suffix("").parts))
        for m in _CALL.finditer(src):
            if m.group(1).startswith("def"):
                continue
            arg = m.group(3)
            if m.group(2):
                found[m.group(2)] = module
            elif arg in consts:
                found[consts[arg]] = module
            elif arg.endswith("GUARD_NAME"):
                found[getattr(importlib.import_module(module), arg)] = module
    return found


def _table_names() -> set[str]:
    """Names the dispatchers pass as a variable: the bash roster and the write-guard modules."""
    return (set(gc.enumerate_bash_guards()) | set(gc.enumerate_write_guards())) - {"engine"}


def _classified() -> set[str]:
    return (
        set(dr.GUARD_DENY_CLASS)
        | set(dr.SUBAGENT_DENY_CLASS)
        | set(dr.ADVISORY_BY_RULING)
    )


def test_census_finds_the_guards():
    static = _static_names()
    assert len(static) >= 25
    assert {"block-worktree-tool", "submit-engine-ask", "guard-turn-intent"} <= set(static)
    assert "destructive-rm" in _table_names()


def test_every_guard_reaching_apply_guard_level_is_classified():
    reaching = set(_static_names()) | _table_names()
    assert sorted(reaching - _classified()) == []


def test_removing_an_advisory_ruling_fails_naming_the_guard(monkeypatch):
    monkeypatch.delitem(dr.ADVISORY_BY_RULING, "guard-turn-intent")
    assert sorted((set(_static_names()) | _table_names()) - _classified()) == ["guard-turn-intent"]


def test_the_two_registered_guards_deny_with_their_class():
    assert dr.GUARD_DENY_CLASS["runaway-find"] == dr.BOX_DEGRADING
    assert dr.GUARD_DENY_CLASS["block-worktree-tool"] == dr.BOX_DEGRADING
    assert dr.GUARD_DENY_CLASS["block-workflow-foreign-emission"] == dr.DISRUPTIVE


def test_a_guard_has_one_classification():
    registered = set(dr.GUARD_DENY_CLASS) | set(dr.SUBAGENT_DENY_CLASS)
    advisory = set(dr.ADVISORY_BY_RULING)
    assert not registered & advisory


def test_rulings_names_are_real_and_reasons_are_one_line():
    reaching = set(_static_names()) | _table_names()
    ruled = set(dr.ADVISORY_BY_RULING)
    assert sorted(ruled - reaching) == []
    for name, reason in dr.ADVISORY_BY_RULING.items():
        assert reason.strip() and "\n" not in reason, name
