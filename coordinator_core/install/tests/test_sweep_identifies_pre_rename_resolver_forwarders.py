"""Coverage for the resolver-rename orphan class: an extensionless Python
forwarder whose body names a resolver module basename OTHER than the one the
running image was built with.

`percolate`'s rename map rewrites `resolve-claude-klabauter/_resolve_claude_klabauter.py` to
`resolve-claude-klabauter/_resolve_claude_klabauter.py` on publish, so
`_AGENT_FORWARDER_MARKER` is a different string in the authoring tree than in
the published image that actually runs installs. Exact-substring matching
made every forwarder generated under the other name silently unsweepable --
four such orphans, targets already deleted from both trees, were confirmed on
a live settings-home 2026-08-30. See `_AGENT_FORWARDER_MARKER_RE`'s comment in
`coordinator_core/install/substrate.py`.
"""
from __future__ import annotations

import pytest

from coordinator_core.install import substrate
from coordinator_core.install.substrate import _sweep_orphaned_agent_helpers


def _forwarder_body(name: str, resolver: str) -> str:
    return (
        "#!/usr/bin/env python3\n"
        f"# coordinator-claude bin forwarder for {name}\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
        f"from {resolver} import exec_cli  # noqa: E402\n"
        "\n"
        f'exec_cli("{name}.py")\n'
    )


@pytest.fixture
def sweepable(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(
        substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root")
    )
    return tmp_path


@pytest.mark.parametrize(
    "resolver",
    ["_resolve_claude_klabauter", "_resolve_claude_klabauter", "_resolve_some_future_name"],
)
def test_sweep_removes_orphan_under_any_resolver_basename(sweepable, resolver):
    orphan = sweepable / "handoff-reconcile-close-terminal"
    orphan.write_text(
        _forwarder_body("handoff-reconcile-close-terminal", resolver), encoding="utf-8"
    )

    _sweep_orphaned_agent_helpers(sweepable, {}, {}, False)

    assert not orphan.exists(), (
        f"a forwarder body naming {resolver} must be identified regardless of which "
        "resolver basename the running image was built with"
    )


def test_current_marker_constant_matches_the_agnostic_pattern():
    assert substrate._AGENT_FORWARDER_MARKER_RE.search(substrate._AGENT_FORWARDER_MARKER)


def test_sweep_protects_a_current_forwarder_with_a_renamed_resolver(sweepable):
    """Condition 2 still gates the pattern match: a name in this run's write
    set survives even though its body matches."""
    name = "still-installed"
    kept = sweepable / name
    kept.write_text(_forwarder_body(name, "_resolve_claude_klabauter"), encoding="utf-8")

    _sweep_orphaned_agent_helpers(sweepable, {name: f"{name}.py"}, {}, False)

    assert kept.exists()


def test_sweep_leaves_an_unrelated_python_script_alone(sweepable):
    """Positive identification, never absence-implies-orphan -- a hand-authored
    script that does not carry this module's generated import line survives."""
    decoy = sweepable / "someones-own-tool"
    decoy.write_text("#!/usr/bin/env python3\nprint('not ours')\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(sweepable, {}, {}, False)

    assert decoy.exists()


def test_sweep_leaves_a_lookalike_import_of_another_symbol_alone(sweepable):
    """The pattern is the generated import LINE, not the resolver name alone --
    importing something else from a resolver module is not this family."""
    decoy = sweepable / "imports-something-else"
    decoy.write_text(
        "#!/usr/bin/env python3\nfrom _resolve_claude_klabauter import claude_klabauter_root\n", encoding="utf-8"
    )

    _sweep_orphaned_agent_helpers(sweepable, {}, {}, False)

    assert decoy.exists()
