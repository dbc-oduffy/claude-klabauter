"""No module outside `engine_root.py` re-derives an engine clone root from
`__file__`.

Spec backlink: docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C3.

WHAT THIS PROTECTS. Seven modules each kept a local
`Path(__file__).resolve().parents[N]` copy of "the clone this process is
running from" (`coordinator_core/warm/{skew,election,supervisor,breadcrumb,
client,server}.py` and `coordinator_core/ops/session/warm_start.py`) --
this codebase's convention of not reaching into a peer module's private
name produced seven independent copies of one rule rather than one shared
definition. C3 collapses all seven onto
`coordinator_core.warm.engine_root.current_engine_clone()`. A regression
here — a future edit re-adding a local `Path(__file__).resolve().parents[N]`
computation instead of calling the shared resolver — is exactly the drift
this plan exists to make structurally impossible.

NEGATIVE-SPEC:
  - `bin/claude-klabauter-doctor-probe.py::_resolve_claude_klabauter_root` is excluded BY NAME
    (§ "The fourth site") — it must keep its own local ladder so the doctor
    stays functional on a tree where `coordinator_core` is not importable.
    It is outside `coordinator_core` entirely and this test does not scan it;
    `test_doctor_probe_ladder_parity.py` covers it instead.
  - Does NOT forbid `Path(__file__)` in general — only the specific
    "resolve an ancestor N levels up" shape this plan collapsed. A module
    computing e.g. its OWN directory (`Path(__file__).resolve().parent`,
    with no `.parents[...]` walk) is not this pattern and is not flagged.
  - Does NOT scan `engine_root.py` itself, which is the one place this
    computation is now allowed to live.
"""

from __future__ import annotations

import ast
from pathlib import Path

_WARM_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _WARM_DIR.parent.parent

# Every former duplicate site, per the plan's substrate table, plus
# `ops/session/warm_start.py` (also enumerated there).
_SCANNED_MODULES = [
    _WARM_DIR / "skew.py",
    _WARM_DIR / "election.py",
    _WARM_DIR / "supervisor.py",
    _WARM_DIR / "breadcrumb.py",
    _WARM_DIR / "client.py",
    _WARM_DIR / "server.py",
    _REPO_ROOT / "coordinator_core" / "ops" / "session" / "warm_start.py",
]


def _derives_ancestor_from_file(tree: ast.AST) -> list[int]:
    """Return the line numbers of any `Path(__file__)....parents[N]`-shaped
    call found anywhere in `tree` (module scope or nested inside a
    function) -- the collapse moved these into function bodies, so a
    module-level-only scan would miss the regression this test exists to
    catch."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        # Looking for `<expr>.parents[N]` where `<expr>` resolves through a
        # `.resolve()` call rooted at a `__file__` name reference somewhere
        # in the attribute/call chain.
        value = node.value
        if not (isinstance(value, ast.Attribute) and value.attr == "parents"):
            continue
        chain_src = ast.dump(value)
        if "__file__" in chain_src:
            hits.append(node.lineno)
    return hits


def test_no_module_outside_engine_root_rederives_a_clone_from_file():
    offenders: dict[str, list[int]] = {}
    for module_path in _SCANNED_MODULES:
        assert module_path.is_file(), f"expected file missing: {module_path}"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        hits = _derives_ancestor_from_file(tree)
        if hits:
            offenders[str(module_path)] = hits

    assert not offenders, (
        "the following modules re-derive an engine clone root via "
        "Path(__file__).resolve().parents[N] instead of calling "
        "coordinator_core.warm.engine_root.current_engine_clone() "
        f"(plan 2026-08-19-an-engine-root-is-a-stamped-build § C3): {offenders}"
    )


def test_current_engine_clone_agrees_with_every_former_local_copy():
    """Every collapsed site's public accessor now returns the SAME value as
    the shared resolver -- pins the collapse did not just remove the
    duplicated *code* while leaving a caller resolving something different."""
    from coordinator_core.warm import breadcrumb, client, election, server, skew, supervisor
    from coordinator_core.warm.engine_root import current_engine_clone

    expected = current_engine_clone()
    assert skew._default_engine_clone() == expected
    assert election._default_engine_clone() == expected
    assert supervisor._default_engine_clone() == expected
    assert breadcrumb._default_engine_clone() == expected
    assert client._engine_clone_root() == expected
    assert server._engine_clone_root() == expected

    from coordinator_core.ops.session import warm_start

    assert warm_start._engine_clone_root() == expected
