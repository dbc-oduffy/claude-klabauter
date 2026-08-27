"""AC17 — the publish transform is a no-op on the new engine-root variable name.

Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C10/C14, AC17.

WHY THIS TEST EXISTS: C10's whole argument for `COORDINATOR_ENGINE_ROOT` over the retired
`CLAUDE_KLABAUTER_ROOT` spelling is that the publish depersonalization transform, which rewrites any
literal containing the repo token, is a NO-OP on a token-free name — so the live tree and the
published mirror ship the SAME spelling and a dual-read fallback is even meaningful to close.
That claim is falsifiable only by reading the mirror ON DISK, not by inspecting this tree's own
source — a repo-side diff proves what THIS tree emits, not what got published.

NEGATIVE SPEC:
  - This is read-off-disk evidence, not an assertion of C14's exit condition (that needs C10's
    observability hook — see engine_root.py's `_ENGINE_ROOT_FALLBACK_EMITTED`/AC24 — plus AC22's
    settings-home validation and AC23's sibling acknowledgement; none of those are this file's
    job).
  - Must never pass vacuously: a missing/unregistered mirror, or a mirror published before the
    pinned constants existed, SKIPS with an explicit reason rather than silently passing.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from coordinator_core import engine_root
from coordinator_core.engine_root import published_engine_mirror_path

_PINNED_NAME = "_ENGINE_ROOT_NEW_VAR"


def _module_level_str_constants(path: pathlib.Path) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments, read without importing.

    Mirrors `test_bootstrap_carveout_pinned_to_accessor.py`'s own helper — reading the mirror's
    source text, not importing it, is what lets this test inspect a foreign tree's file without
    executing it (and without needing the mirror on `sys.path`).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            found[target.id] = node.value.value
    return found


def _mirror_root() -> pathlib.Path | None:
    mirror = published_engine_mirror_path()
    if not mirror:
        return None
    return pathlib.Path(mirror)


@pytest.mark.real_home
def test_mirror_engine_root_module_ships_the_same_new_spelling():
    """AC17: `coordinator_core/engine_root.py`'s new-name constant is byte-identical between
    the live tree and the published mirror — the no-op-on-the-new-name claim, proven on disk.

    SKIPS (never fails vacuously) when there is no registered mirror, when the mirror's
    `engine_root.py` is absent, or when it predates C10 and does not yet declare the pinned
    constant at all — that is "pre-rename", not "diverged", and asserting equality against a
    file that has nothing to compare would be a false pass.

    Marked ``real_home``: this is a live parity oracle reading the actual machine-local
    registry and the real published-mirror clone on disk — the suite-root quarantine
    (`coordinator_core/conftest.py::_quarantine_real_home`) nulls the registry read otherwise,
    which would make this test skip on every box, mirror registered or not.
    """
    mirror_root = _mirror_root()
    if mirror_root is None:
        pytest.skip("no published-engine mirror registered (repos.claude_klabauter absent/unusable)")

    mirror_file = mirror_root / "coordinator_core" / "engine_root.py"
    if not mirror_file.is_file():
        pytest.skip(f"mirror has no coordinator_core/engine_root.py at {mirror_file}")

    mirror_constants = _module_level_str_constants(mirror_file)
    if _PINNED_NAME not in mirror_constants:
        pytest.skip(
            f"mirror's engine_root.py does not declare {_PINNED_NAME} — pre-rename mirror, "
            "publish round has not landed C10 yet"
        )

    live_value = getattr(engine_root, _PINNED_NAME)
    mirror_value = mirror_constants[_PINNED_NAME]
    assert mirror_value == live_value, (
        f"{_PINNED_NAME} diverged across the publish boundary: live tree says {live_value!r}, "
        f"mirror ({mirror_file}) says {mirror_value!r}. The whole premise of retiring the "
        "CLAUDE_KLABAUTER_ROOT fallback (C14) is that the new spelling survives the depersonalization "
        "transform unchanged — this is the case where it did not."
    )


@pytest.mark.real_home
def test_mirror_cc_invoke_carve_out_ships_the_same_new_spelling():
    """AC17, the AC13 bootstrap carve-out's own copy (`cc_invoke.py` cannot import the
    accessor, so it duplicates the constant by hand — see
    `test_bootstrap_carveout_pinned_to_accessor.py`'s within-tree pin). This is that pin's
    cross-tree counterpart, read off the mirror on disk.

    SKIPS when the mirror or its `cc_invoke.py` is absent, or when that file predates the
    carve-out's named constants (pre-rename) — never passes vacuously.

    Marked ``real_home`` — see the sibling test's docstring for why.
    """
    mirror_root = _mirror_root()
    if mirror_root is None:
        pytest.skip("no published-engine mirror registered (repos.claude_klabauter absent/unusable)")

    mirror_cc_invoke = mirror_root / "coordinator" / "bin" / "lib" / "cc_invoke.py"
    if not mirror_cc_invoke.is_file():
        pytest.skip(f"mirror has no coordinator/bin/lib/cc_invoke.py at {mirror_cc_invoke}")

    mirror_constants = _module_level_str_constants(mirror_cc_invoke)
    if _PINNED_NAME not in mirror_constants:
        pytest.skip(
            f"mirror's cc_invoke.py does not declare {_PINNED_NAME} — pre-rename mirror, "
            "publish round has not landed C10/C13's carve-out yet"
        )

    live_value = getattr(engine_root, _PINNED_NAME)
    mirror_value = mirror_constants[_PINNED_NAME]
    assert mirror_value == live_value, (
        f"{_PINNED_NAME} diverged across the publish boundary in the AC13 bootstrap carve-out: "
        f"live tree says {live_value!r}, mirror ({mirror_cc_invoke}) says {mirror_value!r}."
    )
