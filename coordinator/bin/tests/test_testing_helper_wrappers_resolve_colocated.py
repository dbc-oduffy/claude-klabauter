"""A wrapper importing the engine's own test helper resolves COLOCATED, never env-first.

`with-tier-t-slot` and `with-suite-mutex` import `coordinator_core.testing.*` --
the test helper belonging to the checkout they live in. The right copy is
therefore the one self-location finds, and an environment variable cannot name
it: `COORDINATOR_ENGINE_ROOT` exists to point at a PUBLISHED engine, which is a
different tree on a different cadence.

THE FAILURE THIS PINS, observed 2026-09-22. Both wrappers used the env-first
`require_engine_on_path`. In a cloud container `COORDINATOR_ENGINE_ROOT` names a
depth-1 clone of the published mirror, pinned at boot -- there, 266
engine-touching commits behind the checkout under test -- so the wrapper imported
that checkout's test helper from a two-day-old tree and died on
`ImportError: cannot import name 'tier_t_slots'`. The operator's workaround was
to set `COORDINATOR_ENGINE_ROOT` to the live tree for test runs, which then has
to be UNSET again for op dispatch, because an op resolving to a live working tree
is refused by ruling (DR-315 s2). One variable, two opposite correct values, and
a session that carried the test-side value to an op concluded the whole venue was
broken.

Colocated resolution is correct in BOTH venues rather than merely better in one:
`resolve_colocated_claude_klabauter_root`'s two-marker probe (coordinator_core/ plus
pyproject.toml) hits in the published mirror too, so the published copy resolves
to the mirror, which is what it should do there.

Guarding the axis by test rather than by docstring, for the same reason
`test_require_dispatch_engine_on_path` guards its own: the two resolvers are the
same shape at the call site and differ only in ladder order, so a later edit that
"unifies" them reintroduces this silently.
"""

from __future__ import annotations

import pathlib

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent

#: The wrappers whose import target is the engine's own test helper. A wrapper
#: added here must import `coordinator_core.testing.*`; one that reaches for a
#: published engine's runtime surface instead belongs on the env-first ladder and
#: does not go in this list.
_TESTING_HELPER_WRAPPERS = ("with-tier-t-slot", "with-suite-mutex")


@pytest.mark.parametrize("name", _TESTING_HELPER_WRAPPERS)
def test_wrapper_resolves_colocated_not_env_first(name):
    path = _BIN / name
    assert path.is_file(), f"{name} is missing from {_BIN}"
    source = path.read_text(encoding="utf-8")

    assert "require_colocated_engine_on_path(__file__)" in source, (
        f"{name} must bootstrap via require_colocated_engine_on_path: it imports the "
        "engine's own test helper, which belongs to the checkout this file lives in."
    )
    assert "require_engine_on_path(__file__)" not in source, (
        f"{name} calls the env-first require_engine_on_path. That resolves to whatever "
        "COORDINATOR_ENGINE_ROOT names -- a published mirror on any box that sets it -- "
        "and imports this checkout's test helper from a different tree."
    )


@pytest.mark.parametrize("name", _TESTING_HELPER_WRAPPERS)
def test_wrapper_actually_imports_a_testing_helper(name):
    """The list above stays honest: membership is a property of the file, not a label.

    Without this, a wrapper could be renamed or repurposed away from
    `coordinator_core.testing` and keep a guard that no longer describes it.
    """
    source = (_BIN / name).read_text(encoding="utf-8")
    assert "from coordinator_core.testing import" in source, (
        f"{name} no longer imports a coordinator_core.testing helper, so it does not "
        "belong in _TESTING_HELPER_WRAPPERS -- remove it rather than leaving a guard "
        "that describes a file that has moved on."
    )
