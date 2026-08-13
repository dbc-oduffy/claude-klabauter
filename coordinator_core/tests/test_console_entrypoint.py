"""
test_console_entrypoint — guard that the `coordinator-invoke` console script
is declared and its target is importable.

Purpose: coordinator_core.invoke is claude-klabauter's generic in-process op dispatcher
(coordinator_core/invoke/__main__.py). A downstream caller outside a claude-klabauter
checkout (e.g. Coordinator-claude's cc_invoke) must be able to reach it by spawning a
command, never by resolving claude-klabauter's interpreter and importing its
internals (DR-215 — command-type, spawn-per-call engine, no resident
daemon). This test covers the two things collectable from inside a checkout:
the entry is declared in pyproject.toml's [project.scripts] and its target
attribute (coordinator_core.invoke.__main__:main) actually resolves to a
callable. It deliberately does NOT cover the from-outside-a-checkout,
freshly-installed console-script resolution itself — that requires an
actual package install into a separate environment and is exercised as live
evidence at dispatch/AC-verification time, not as a unit test here.

Spec backlink: pln-claude-klabauter-ize-the-survey-census-c-2a0dfd § C4
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — repo floor is >=3.11 (pyproject.toml requires-python)
    import tomli as tomllib  # type: ignore[no-redef]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_coordinator_invoke_script_declared_in_pyproject() -> None:
    pyproject = _repo_root() / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["coordinator-invoke"] == "coordinator_core.invoke.__main__:main"


def test_coordinator_invoke_script_target_is_importable_callable() -> None:
    from coordinator_core.invoke.__main__ import main

    assert callable(main)
