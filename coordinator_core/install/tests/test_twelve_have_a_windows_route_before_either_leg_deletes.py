"""coordinator_core.install.tests.test_twelve_have_a_windows_route_before_
either_leg_deletes -- chunk C3 of docs/plans/2026-08-30-twenty-one-bin-names-
reach-the-door-or-are-thoroughly-dead.md: a real guard, not prose, against a
peer launcher plan's C3 deleting the repo-side `coordinator/bin/<name>.cmd`
for the twelve extensionless-only names before the door can serve them.

THE TWELVE are `coordinator/bin/<name>` extensionless scripts with no `.py`
twin in either tree, verified live-in-repo tools (git hooks invoke several by
name): `chunk-commits`, `with-suite-mutex`, `plan-tasks-resolve`,
`static-check`, `spawn-census`, `claim-neighbours`, `gate-validate-invocable`,
`plan-tasks-stamp`, `coordinator-ensure-hooks-fleet`,
`coordinator-postsync-marker-resync-check`,
`coordinator-precommit-foreign-platform-check`,
`coordinator-precommit-settings-tracking-check` -- see
`coordinator_core/install/door_install.py :: launcher_is_installable`'s own
"THE EXTENSIONLESS TWELVE" docstring section for this same roster.

For each of the twelve, THIS ASSERTS AT LEAST ONE WORKING WINDOWS ROUTE
EXISTS:

  (repo-side `coordinator/bin/<name>.cmd` exists)
    OR
  (the extensionless fallback is present in BOTH `_resolve_entrypoint_script`
   AND `door.c`'s/`door_posix.c`'s cold leg)

It goes red the moment either side deletes first, in whichever tree the
deletion lands -- the ordering hazard this row's own plan body names ("RACE
WITH THE PEER LAUNCHER PLAN -- A REAL GUARD, NOT PROSE"). If the `.cmd`
files are already gone when this test runs, the twelve have NO working leg
at all and a failure here is an outage report, not a nitpick.

Spec backlink: docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-
    are-thoroughly-dead.md, chunk C3
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ops.invoke_from_argv as ifa
from coordinator_core.ops.invoke_from_argv import _resolve_entrypoint_script

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_BIN = _REPO_ROOT / "coordinator" / "bin"
_DOOR_DIR = _REPO_ROOT / "coordinator_core" / "warm" / "door"

THE_TWELVE = [
    "chunk-commits",
    "with-suite-mutex",
    "plan-tasks-resolve",
    "static-check",
    "spawn-census",
    "claim-neighbours",
    "gate-validate-invocable",
    "plan-tasks-stamp",
    "coordinator-ensure-hooks-fleet",
    "coordinator-postsync-marker-resync-check",
    "coordinator-precommit-foreign-platform-check",
    "coordinator-precommit-settings-tracking-check",
]


def _resolver_learns_the_extensionless_target(monkeypatch, name: str) -> bool:
    """True iff `_resolve_entrypoint_script` resolves `name` to the real,
    on-disk extensionless script in `_REPO_BIN` -- proven against the actual
    tree, not a fixture, so this cannot pass against a resolver that merely
    LOOKS widened in source but still hardcodes `.py`."""
    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset({name}))
    monkeypatch.setattr(ifa, "_ENGINE_ROOT", _REPO_ROOT)
    try:
        resolved = _resolve_entrypoint_script(name)
    except ValueError:
        return False
    return resolved == _REPO_BIN / name


def _cold_leg_has_extensionless_fallback(source_text: str) -> bool:
    """True iff the door source builds an extensionless candidate path
    (`coordinator/bin/%s`, no `.py` suffix) alongside the `.py` candidate.
    A textual check, not a build+probe, because this test must stay cheap
    enough to run every commit -- the serving probe in C3's own acceptance
    criteria is what proves the compiled behaviour."""
    return "coordinator\\\\bin\\\\%s\"" in source_text or "coordinator/bin/%s\"" in source_text


@pytest.mark.parametrize("name", THE_TWELVE)
def test_twelve_have_a_windows_route(monkeypatch, name):
    cmd_route = (_REPO_BIN / f"{name}.cmd").is_file()

    resolver_route = _resolver_learns_the_extensionless_target(monkeypatch, name)
    door_c_route = _cold_leg_has_extensionless_fallback(
        (_DOOR_DIR / "door.c").read_text(encoding="utf-8")
    )
    door_posix_c_route = _cold_leg_has_extensionless_fallback(
        (_DOOR_DIR / "door_posix.c").read_text(encoding="utf-8")
    )
    extensionless_route = resolver_route and door_c_route and door_posix_c_route

    assert cmd_route or extensionless_route, (
        f"{name!r} has NO working Windows route: no coordinator/bin/{name}.cmd, "
        f"and the extensionless fallback is not present in all of "
        f"_resolve_entrypoint_script ({resolver_route}), door.c ({door_c_route}), "
        f"and door_posix.c ({door_posix_c_route})"
    )
