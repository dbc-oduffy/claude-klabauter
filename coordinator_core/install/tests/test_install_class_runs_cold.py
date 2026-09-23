"""Install-class CLIs run cold, every time -- never through the warm door.

PM ruling 2026-09-23: "install shouldn't route via the warm engine, because it
installs." And why, verbatim: "install can't rely on the warm engine because it
precedes it, and a re-install replaces the warm engine. it's one of the things
that makes no sense being served warm." Served warm, an installer runs inside
the server it is replacing:
`coordinator-install` came back JSON-RPC -32004 "warm dispatch indeterminate"
on machine-a the same day.

The class is DECLARED by each CLI (`INSTALL_CLASS = True|False` at module
level), not inferred from a name or a roster. What this file enforces:

  1. Every CLI that could plausibly install -- it imports `coordinator_core.install`
     or is named like an installer -- carries an explicit declaration, so a new
     one cannot land unclassified and silently go warm.
  2. Every declared install-class CLI is absent from BOTH warm allowlist keys
     (the server's own fail-closed gate), and still gets the one native door
     image -- its only Windows bare-name launcher. The door never dials the
     engine for it: door_core.c's `door_install_class_basenames` sends it
     straight to the cold leg, pinned against these declarations by
     coordinator_core/warm/door/tests/test_install_class_table_parity.py.
"""

from __future__ import annotations

import json
import re

import pytest

from coordinator_core.install import door_install

_BIN = door_install._GENERATOR_BIN_DIR
_ALLOWLIST = door_install._GENERATOR_BIN_DIR.parents[1] / "coordinator_core" / "ops" / "warm_entrypoint_allowlist.json"
_INSTALL_IMPORT = re.compile(r"^\s*(?:from|import)\s+coordinator_core\.install\b", re.M)
_INSTALLER_NAME = re.compile(r"(^|[-_])(un)?install([-_]|$)|(^|[-_])setup([-_]|$)")


def _cli_names():
    return sorted(p.stem for p in _BIN.glob("*.py") if not p.stem.startswith("test_"))


def _could_install(name: str) -> bool:
    source = (_BIN / f"{name}.py").read_text(encoding="utf-8", errors="replace")
    return bool(_INSTALL_IMPORT.search(source) or _INSTALLER_NAME.search(name))


_INSTALL_CLASS = sorted(n for n in _cli_names() if door_install.declared_install_class(n) is True)


def test_every_possible_installer_declares_its_class():
    undeclared = [
        n for n in _cli_names() if _could_install(n) and door_install.declared_install_class(n) is None
    ]
    assert not undeclared, (
        "these CLIs reach coordinator_core.install or are named like an installer but "
        "declare no `INSTALL_CLASS = True|False`; True if they write install state "
        f"(engine, forwarders, settings, registry, hooks), else False: {undeclared}"
    )


def test_the_ruling_s_named_installers_are_install_class():
    for name in ("coordinator-install", "coordinator-uninstall", "install-claude-doe-wrapper"):
        assert name in _INSTALL_CLASS, name


@pytest.mark.parametrize("name", _INSTALL_CLASS)
def test_install_class_gets_the_door_image(name):
    assert door_install.name_gets_door_image(name)


def test_install_class_is_absent_from_both_warm_allowlist_keys():
    allow = json.loads(_ALLOWLIST.read_text(encoding="utf-8"))
    for key in ("entrypoints", "door_eligible_entrypoints"):
        leaked = sorted(set(_INSTALL_CLASS) & set(allow[key]))
        assert not leaked, f"install-class names warm-servable via {key}: {leaked}"
