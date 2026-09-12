"""
test_emit_artifact_shape_contract_engine_guard — keeps the engine-mismatch
guard from degrading into an always-pass.

`emit-artifact-shape-contract.py`'s guard compares this repo's emitter
`CONTRACT_VERSION` against the one on the resolved engine, and refuses to emit
when they differ. It reads the source version as TEXT rather than importing,
deliberately: importing `coordinator_core.ops.emit_artifact_shape_contract`
hands back whichever module `sys.path` already resolved — possibly the stale
published mirror, which is the thing under test.

That makes the guard the only link in the chain that does not trust
`sys.path`, and it buys a failure mode with it (doe-claude-dc, 2026-09-12):
the regex keys on the literal spelling `CONTRACT_VERSION = "..."`. Make that a
computed value, move it to a constants module, or switch the quoting, and
`_source_contract_version()` silently returns None — at which point the guard
returns None for every input and the refusal never fires again. Nothing would
fail. The emitter would go back to silently publishing from whichever tree
`require_dispatch_engine_on_path()` happened to resolve, which is precisely
the incident the guard was written for.

So the load-bearing assertion here is the boring one: the text read still
finds something.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "emit-artifact-shape-contract.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_EMITTER = _REPO_ROOT / "coordinator_core" / "ops" / "emit_artifact_shape_contract.py"


def _load_by_path(name: str, path: Path):
    """Import a module by file path, never consulting sys.path.

    Used for both the hyphenated CLI (not an importable module name) and the
    source emitter (whose whole point is to be read independently of whatever
    sys.path resolves).
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_script_module():
    return _load_by_path("_emit_asc_cli", _SCRIPT)


@pytest.fixture(scope="module")
def cli():
    return _load_script_module()


def test_source_version_read_still_finds_a_version(cli):
    """The guard's one unguarded assumption: the literal is still readable.

    A None here does not fail loudly anywhere in production — it makes the
    refusal unreachable and hands the emitter back to whatever sys.path
    resolved. If this test goes red, the fix is to update the read (or move it
    off a text scrape), NOT to delete the assertion.
    """
    assert _SOURCE_EMITTER.is_file(), (
        f"{_SOURCE_EMITTER} not found — the guard resolves the source emitter by "
        "path, so a move breaks it silently. Update _source_contract_version()."
    )
    version = cli._source_contract_version()
    assert version is not None, (
        "_source_contract_version() found no CONTRACT_VERSION literal in "
        f"{_SOURCE_EMITTER}. The guard now returns None for every input and the "
        "refusal can never fire — an always-pass. The regex keys on the literal "
        'spelling `CONTRACT_VERSION = "..."`; a computed value, a constants-module '
        "move, or a quoting change all defeat it silently."
    )
    assert version.strip(), "CONTRACT_VERSION read as empty"


def test_source_version_matches_the_imported_module():
    """Cross-check the text scrape against the real value, importing the source
    emitter BY PATH so this never consults sys.path."""
    module = _load_by_path("_emit_asc_source", _SOURCE_EMITTER)
    cli = _load_script_module()
    assert cli._source_contract_version() == module.CONTRACT_VERSION


def test_guard_refuses_when_the_resolved_engine_differs(cli, monkeypatch):
    """Liveness, red verdict: a resolved engine on a different version refuses."""
    fake = type(sys)("coordinator_core.ops.emit_artifact_shape_contract")
    fake.CONTRACT_VERSION = "0.0.1-not-this-tree"
    fake.__file__ = "/somewhere/else/emit_artifact_shape_contract.py"
    monkeypatch.setitem(
        sys.modules, "coordinator_core.ops.emit_artifact_shape_contract", fake
    )

    message = cli._assert_resolved_engine_is_this_source()

    assert message is not None, "guard passed an engine on a different version"
    assert "0.0.1-not-this-tree" in message
    assert cli._source_contract_version() in message
    assert "CLAUDE_KLABAUTER_ROOT" in message, "refusal must carry its own remediation"


def test_guard_stays_quiet_when_the_resolved_engine_matches(cli, monkeypatch):
    """Liveness, green verdict: a guard that always fires is noise, not a guard."""
    fake = type(sys)("coordinator_core.ops.emit_artifact_shape_contract")
    fake.CONTRACT_VERSION = cli._source_contract_version()
    fake.__file__ = str(_SOURCE_EMITTER)
    monkeypatch.setitem(
        sys.modules, "coordinator_core.ops.emit_artifact_shape_contract", fake
    )

    assert cli._assert_resolved_engine_is_this_source() is None
