"""test_cc_invoke_no_ambient_live_tree.py — AC for C6, "Close cc_invoke's two
ambient live-tree rungs".

Chunk: docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C6

C5 gave `coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root_with_class()`
the DR-132/stamp-gated ladder: the single place that answers "which engine
executes?" for every caller. Before C6, two rungs inside
`cc_invoke._resolve_claude_klabauter_root()` (the DISPATCH axis — consumed by
`route()`/`cc_invoke()`) answered that question themselves, bypassing the
gate entirely:
  Rung 1 — an explicit `CLAUDE_KLABAUTER_ROOT` env var, fast-path return.
  Rung 3 — terminal self-location via `_walk_up_to_checkout(__file__)`.
Every fired session inherits its environment, so Rung 1 was ambient by
construction — "oops, wrong var set" reaching the live tree with no gate in
the loop. Rung 3 was documented gate-blind outright.

C6's fix: both rungs now supply a CANDIDATE only, delegated through the same
nested `_delegate_to_gate()` helper Rung 2 (registry) already used — the
single source of truth for the DISPATCH answer is
`coordinator_claude_klabauter_root_with_class()`, never re-derived inline. This file
pins that structural closure two ways: a source-level guard that neither rung
answers directly any more, and a behavioral case where delegation's answer
genuinely diverges from what the closed-over rung would have returned
verbatim — proving delegation is real, not a no-op wrapper.

Run: pytest coordinator/bin/tests/test_cc_invoke_no_ambient_live_tree.py -q
"""
from __future__ import annotations

import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"
_CC_INVOKE_PY = _LIB_DIR / "cc_invoke.py"
_MLIR_PY = _LIB_DIR / "machine_local_impl_resolve.py"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)


# Declared, not excused: the behavioral case below spawns a real subprocess
# because the behaviour under test IS the spawn (a hermetic, no-ambient-state
# child) — mirrors test_cc_invoke_self_location_rung.py's own declared
# rationale (test_no_new_spawning_tests.py Rule 2).
pytestmark = [pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Source-level guard — neither Rung 1 nor Rung 3 answers the DISPATCH
# question directly any more; both must route through `_delegate_to_gate`.
# ---------------------------------------------------------------------------


def test_rung1_and_rung3_no_longer_return_their_candidate_directly():
    """Static guard: `_resolve_claude_klabauter_root()`'s own source must not contain a
    bare `return existing` (Rung 1) or `return _self_located` (Rung 3) — both
    would be the exact ambient-live-tree pattern C6 closes. The only
    acceptable form is delegating through `_delegate_to_gate(...)`."""
    source = inspect.getsource(_mod._resolve_claude_klabauter_root)
    assert "return existing\n" not in source, (
        "Rung 1 (CLAUDE_KLABAUTER_ROOT env) must not fast-path return its candidate "
        "verbatim — it must delegate through _delegate_to_gate()."
    )
    assert "return _self_located\n" not in source, (
        "Rung 3 (self-location) must not return its candidate verbatim — it "
        "must delegate through _delegate_to_gate()."
    )
    assert source.count("_delegate_to_gate(") >= 3, (
        "expected every DISPATCH-axis candidate rung (env, registry, "
        "self-location) to call _delegate_to_gate() — found fewer call sites "
        "than the three rungs this function documents."
    )


def test_pointer_rungs_are_the_only_surviving_direct_return():
    """Rung 1.5 (`.claude-klabauter-root` / `.claude-klabauter-root` pointer files)
    deliberately stays a direct return — its own docstring note explains why
    that is not gate-blind in the direction that matters (it already gives
    the gate's own answer). This is the ONE remaining exception; pinning it
    here keeps a future edit from silently adding a second one."""
    source = inspect.getsource(_mod._resolve_claude_klabauter_root)
    assert "return _published_pointer_val" in source
    assert "return _pointer_val" in source


# ---------------------------------------------------------------------------
# Behavioral guard — self-location's answer can now genuinely diverge from
# the self-located path, proving delegation is real dispatch, not a
# pass-through wrapper around the same verbatim value.
# ---------------------------------------------------------------------------


_DROP_PREFIXES = ("REPO_", "CLAUDE", "COORDINATOR_")
_DROP_EXACT = ("CLAUDE_KLABAUTER_ROOT", "DOE_ROOT")


def _hermetic_child_env(isolated_home: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Same hermetic-env recipe as test_cc_invoke_self_location_rung.py — a
    from-scratch env dict, never `dict(os.environ)` + strip, so there is
    nothing ambient to leak in the first place."""
    env: dict[str, str] = {
        "HOME": isolated_home,
        "USERPROFILE": isolated_home,
        "CLAUDE_HOME": isolated_home,
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    for key in list(env):
        assert not any(key.startswith(p) for p in _DROP_PREFIXES) or key in ("CLAUDE_HOME",)
    for key in _DROP_EXACT:
        assert key not in env
    if extra:
        env.update(extra)
    return env


def _build_self_locatable_checkout_with_gate_stub(root: Path, *, gate_answer: str) -> tuple[Path, Path]:
    """Build a synthetic checkout at `root / "flat-checkout"` that
    `_walk_up_to_checkout` can self-locate, whose stub
    `coordinator_core/claude_klabauter_root.py` answers `gate_answer` — deliberately
    NOT the checkout's own path, so a passing test proves the final answer
    came from delegation, not from trusting self-location verbatim.

    Returns (checkout_root, cc_invoke_copy_path).
    """
    checkout_root = root / "flat-checkout"
    lib_dir = checkout_root / "coordinator" / "bin" / "lib"
    lib_dir.mkdir(parents=True)
    (checkout_root / "coordinator_core").mkdir(parents=True)
    (checkout_root / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (checkout_root / "pyproject.toml").write_text("[project]\nname = \"stub\"\n", encoding="utf-8")
    (checkout_root / "coordinator_core" / "claude_klabauter_root.py").write_text(
        "def coordinator_claude_klabauter_root_with_class():\n"
        f"    return ({gate_answer!r}, 'resolved-engine')\n",
        encoding="utf-8",
    )

    cc_invoke_copy = lib_dir / "cc_invoke.py"
    shutil.copyfile(_CC_INVOKE_PY, cc_invoke_copy)
    shutil.copyfile(_MLIR_PY, lib_dir / "machine_local_impl_resolve.py")
    return checkout_root, cc_invoke_copy


_RESOLVE_SNIPPET = textwrap.dedent(
    """\
    import sys
    sys.path.insert(0, {lib_dir!r})
    import cc_invoke
    print(cc_invoke._resolve_claude_klabauter_root())
    """
)


def test_self_location_answer_can_diverge_from_the_self_located_path():
    """The closed hole, demonstrated: on a box where the gate would redirect
    to a published engine mirror (simulated here by the stub's own
    `coordinator_claude_klabauter_root_with_class()` answering a DIFFERENT path than
    the checkout self-location found), `_resolve_claude_klabauter_root()`'s terminal
    rung now returns the GATE's answer, not the self-located tree's own path.
    Before C6 this was structurally impossible — Rung 3 always returned
    exactly `_walk_up_to_checkout(__file__)`'s value."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        published_root = str(tmp_path / "published-engine-mirror")
        checkout_root, cc_invoke_copy = _build_self_locatable_checkout_with_gate_stub(
            tmp_path, gate_answer=published_root
        )
        isolated_home = tmp_path / "empty-home"
        isolated_home.mkdir()

        env = _hermetic_child_env(str(isolated_home))
        snippet = _RESOLVE_SNIPPET.format(lib_dir=str(cc_invoke_copy.parent))
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, f"expected success; stderr:\n{result.stderr}"
        resolved = result.stdout.strip()
        assert resolved == published_root, (
            f"expected the terminal self-location rung to return the GATE's "
            f"answer ({published_root!r}), not the self-located checkout's own "
            f"path ({checkout_root!r}); got {resolved!r}"
        )
        assert os.path.realpath(resolved) != os.path.realpath(str(checkout_root)), (
            "the whole point of this test is that the two paths differ — a "
            "fixture bug made them coincide, proving nothing"
        )
