"""test_shebang_removal_ordering_ratchet.py — encodes the ordering constraint
from the 2026-07-31 exec_cli POSIX-leg convergence plan
(docs/plans/2026-07-31-exec-cli-posix-leg-convergence.md, AC9): a
`coordinator/bin/` entrypoint's shebang may only be removed AFTER
`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`'s POSIX leg has
converged to interpreter-targeted exec (`os.execv(sys.executable,
[sys.executable, target_path, *argv])`). While the resolver still carries
the pre-convergence body (`os.execv(target_path, ...)` — exec'ing the
target directly, which relies on the target's OWN shebang to pick an
interpreter), a shebang-less entrypoint is fleet-fatal: every forwarder on
the machine dies with `Exec format error`. This must not live only in plan
prose someone has to remember — encoding it here converts a memory item
into an artifact the suite enforces (CLAUDE.md § North star, discharge
test).

SELF-INVERTING BY DESIGN: the convergence already landed on this branch
(the resolver now calls `os.execv(sys.executable, ...)`), so the guarded
condition — pre-convergence body + a shebang-less entrypoint — can no
longer both hold, and `test_ordering_guard_holds` is green by construction
today. That is the intended end state, NOT a sign this test is dead
weight: it is a tripwire against reintroduction (a revert of the resolver,
or a cherry-pick that lands the old body onto a tree with already-stripped
shebangs). Do not delete it as vacuous — a future regression is exactly
what it exists to catch.

`test_logic_discriminates_on_fixture` proves the assertion logic itself is
not vacuously true — it exercises `_has_pre_convergence_body` and
`_shebangless_entrypoints` against synthetic fixture strings/dicts (never
against the real resolver file or real bin/ entries), and asserts the
combined check WOULD fail if both conditions held. Without this, a checker
that can never evaluate its own falsy branch is worse than no checker.

`test_candidate_selection_includes_py_entrypoints` closes a separate gap:
proves candidate SELECTION itself (which files become offender-candidates
in the first place) is not silently narrowed to extensionless files only.
Review 2026-07-31 (Finding 1) caught this test's candidate list excluding
every `.py`-suffixed entrypoint — 9 of the 13 this same diff stripped
shebangs from, including `doctor.py` and `break_glass.py` — which
`test_logic_discriminates_on_fixture` could not have caught, since that
test only exercises the predicates in isolation, never the candidate list.

COVERAGE SCOPE: every tracked, top-level `coordinator/bin/` file that is
either extensionless or `.py`-suffixed, with the SAME `.py` exclusions
`coordinator_core/test_bin_launcher_parity.py`'s `_py_entrypoints()`
applies (`test_*.py` suites, `conftest.py`, `_`-prefixed shared modules,
`PY_ENTRYPOINT_EXEMPTIONS`) — reused via import rather than re-derived, so
the two enumerations cannot drift apart again.

Run: python3 -m pytest coordinator/bin/tests/test_shebang_removal_ordering_ratchet.py -q
"""
from __future__ import annotations

import os

from ._polyglot_git_scan import blob_first_line, blob_full_text, tracked_bin_direct_children

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_RESOLVER_PATH = "coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py"


def _has_pre_convergence_body(resolver_text: str) -> bool:
    """True iff *resolver_text* still carries the pre-convergence POSIX
    body (bare ``os.execv(target_path, ...)``, no interpreter-targeted
    ``os.execv(sys.executable, ...)`` present). Requires the interpreter-
    targeted call to be ABSENT, not just the substring present, because the
    converged file's own docstring quotes ``os.execv(target_path, ...)`` in
    prose (a benchmark-timing aside) — a bare substring match would false-
    positive on that quote forever."""
    return "os.execv(target_path" in resolver_text and "os.execv(sys.executable" not in resolver_text


def _shebangless_entrypoints(first_lines_by_path: dict) -> list:
    return [p for p, line in first_lines_by_path.items() if not line.startswith("#!")]


def _candidate_shebang_paths(bin_children: list, py_stems: list) -> list:
    """Combine extensionless direct children of `coordinator/bin/` with
    `.py`-suffixed entrypoint stems (as already filtered by
    `coordinator_core/test_bin_launcher_parity.py`'s `_py_entrypoints()`)
    into the full candidate list for the shebang check. Extracted so the
    candidate-selection step itself — not just the predicates it feeds —
    is fixture-testable (see `test_candidate_selection_includes_py_entrypoints`)."""
    extensionless = [f for f in bin_children if "." not in os.path.basename(f)]
    py_paths = [f"coordinator/bin/{stem}.py" for stem in py_stems]
    return extensionless + py_paths


def test_ordering_guard_holds() -> None:
    """FAILS if any tracked coordinator/bin/ entrypoint (extensionless OR
    `.py`-suffixed, per `_candidate_shebang_paths`) lacks a shebang while
    _resolve_claude_klabauter.py still has the pre-convergence body."""
    resolver_text = blob_full_text(_RESOLVER_PATH)
    if not _has_pre_convergence_body(resolver_text):
        return  # converged — guarded condition cannot fire (see module docstring)

    from coordinator_core.test_bin_launcher_parity import _py_entrypoints

    candidates = _candidate_shebang_paths(tracked_bin_direct_children(), _py_entrypoints())
    first_lines = {f: blob_first_line(f) for f in candidates}
    offenders = _shebangless_entrypoints(first_lines)

    assert not offenders, (
        f"{len(offenders)} shebang-less coordinator/bin/ entrypoint(s) while "
        f"{_RESOLVER_PATH} still has the pre-convergence POSIX body — this is "
        f"fleet-fatal (Exec format error on every forwarder). Offenders: {offenders}"
    )


def test_logic_discriminates_on_fixture() -> None:
    """Sanity check: proves the check above is not vacuously true. Uses
    synthetic fixtures only — never edits the real resolver or bin/ tree."""
    pre_convergence_fixture = "os.execv(target_path, [target_path, *argv])"
    converged_fixture = "os.execv(sys.executable, [sys.executable, target_path, *argv])"
    assert _has_pre_convergence_body(pre_convergence_fixture) is True
    assert _has_pre_convergence_body(converged_fixture) is False

    offenders = _shebangless_entrypoints({"coordinator/bin/fake-tool": "print('no shebang')"})
    assert offenders == ["coordinator/bin/fake-tool"]


def test_candidate_selection_includes_py_entrypoints() -> None:
    """Proves candidate SELECTION is not silently narrowed to extensionless
    files (review 2026-07-31, Finding 1: this exact gap let `doctor.py` and
    `break_glass.py` go uncounted while their docstrings claimed blanket
    coverage). Uses synthetic fixtures only, never the real bin/ tree —
    would fail if `_candidate_shebang_paths` dropped its `py_stems` leg."""
    bin_children = ["coordinator/bin/scoped-git-commit", "coordinator/bin/doctor.py"]
    py_stems = ["doctor"]
    candidates = _candidate_shebang_paths(bin_children, py_stems)
    assert "coordinator/bin/doctor.py" in candidates
    assert "coordinator/bin/scoped-git-commit" in candidates
    # A py_stem NOT present in bin_children still surfaces as a candidate --
    # proves the .py leg is additive, not merely re-filtering bin_children.
    assert "coordinator/bin/break_glass.py" in _candidate_shebang_paths(
        bin_children, ["doctor", "break_glass"]
    )
