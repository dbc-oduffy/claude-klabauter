"""coordinator_core.install.test_resolve_publisher_root_source_override --
BV-20260927-05 fix 4.

`_resolve_publisher_root()` (the live-tree-only resolver every
`PUBLISHER_ONLY_TARGETS` member's settings-home forwarder calls through
`exec_cli`) used to consult only the DISPATCH-axis ladder
(`COORDINATOR_ENGINE_ROOT` env / registry / sentinel) -- never
`COORDINATOR_ENGINE_SOURCE_ROOT`, the LOCATOR-axis variable
`percolate-mirror.py`'s own `_bootstrap_engine` already honours and which its
OWN printed remediation text tells an operator to set. An operator who set it
per that instruction still got no effect from the settings-home launcher,
which fell back to whichever root the dispatch ladder resolved -- frequently
a published mirror missing `percolate-round.py` entirely (the live
"missing percolate-round.py" failure this closes).

Module-loading convention matches test_resolve_claude_klabauter_rename_retry.py.

Spec backlink: state/bug-backlog/BV-20260927-05-percolate-resolves-every-target.yaml
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "coordinator"
    / "lib"
    / "resolve-claude-klabauter"
    / "_resolve_claude_klabauter.py"
)

_spec = importlib.util.spec_from_file_location(
    "_resolve_claude_klabauter_under_test_publisher_root_source_override", _MODULE_PATH
)
assert _spec is not None and _spec.loader is not None
resolve_claude_klabauter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve_claude_klabauter)


def test_source_override_wins_ahead_of_the_dispatch_ladder(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_ENGINE_SOURCE_ROOT", str(tmp_path))
    # A dispatch-axis rung that WOULD resolve to something else, to prove the
    # override short-circuits before it is ever consulted.
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tmp_path / "not-this-one"))

    result = resolve_claude_klabauter._resolve_publisher_root()

    assert result == str(tmp_path)


def test_a_non_directory_source_override_is_ignored(tmp_path, monkeypatch):
    bogus = tmp_path / "does-not-exist"
    monkeypatch.setenv("COORDINATOR_ENGINE_SOURCE_ROOT", str(bogus))
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tmp_path))

    result = resolve_claude_klabauter._resolve_publisher_root()

    assert result == str(tmp_path)


def test_no_source_override_still_uses_the_dispatch_ladder(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_ENGINE_SOURCE_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tmp_path))

    result = resolve_claude_klabauter._resolve_publisher_root()

    assert result == str(tmp_path)


def test_failure_message_names_the_source_override_remedy(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_ENGINE_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setattr(resolve_claude_klabauter, "_ml_dir", lambda: tmp_path / "no-such-dir")

    try:
        resolve_claude_klabauter._resolve_publisher_root()
        assert False, "expected ClaudeKlabauterResolutionError"
    except resolve_claude_klabauter.ClaudeKlabauterResolutionError as exc:
        assert "COORDINATOR_ENGINE_SOURCE_ROOT" in str(exc)
