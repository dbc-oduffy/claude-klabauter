"""test_cli_shared_dump_repos_parity.py — dump-vs-get equivalence for
`machine_local_dump_repos` (cli_shared.py) and `_machine_local_dump_repos`
(coordinator-doc-new.py).

Finding 2 -- these batch counterparts to the old enumerate-then-`get` path
had zero test coverage. Both functions now call `_machine_local.py`'s own
in-process kernel (`resolve_one`, `_build_resolution_layers`, `_all_keys`)
directly (P055-C3, converting the prior `subprocess.run([python, impl,
...])` spawn) via each module's own `_load_machine_local_kernel()`. Neither
this test nor a human reviewer can read `_machine_local.py`'s code from
this repo -- it's a discovery-resolved surface owned by coordinator-claude
-- so a stubbed-kernel behavioural parity test against a shared fixture
registry is still the only available guard for the byte-identical-to-`get`
equivalence claim, unchanged from the pre-conversion shape. Only the seam
being stubbed moved: `_load_machine_local_kernel()` (a fake kernel object)
instead of `subprocess.run` (a fake `CompletedProcess`).

Both functions' calls into the fake kernel are checked against the SAME
fixture registry (`_FIXTURE_REGISTRY`) so the two code paths are proven
equivalent for:
  - present keys (ordinary case)
  - an ABSENT key that is NOT `repos.doe_claude` (the one key with an
    explicit `setdefault(...)` backstop in `resolve_from_repo` --
    precisely because the authors worried about a default-on-absent gap
    for it specifically; every other `repos.*` key has no such backstop,
    so this test deliberately covers one of those instead)
  - a key resolved OK but with a non-string/None value (type coercion:
    `machine_local_dump_repos` filters non-str/falsy values the same way
    `machine_local_get` degrades a falsy resolved value to None)
  - one key hitting EXIT_OPERATIONAL: the whole dump must fail closed to
    `{}` (matches `machine_local_get`'s existing fail-closed contract --
    an operationally-failed batch is a partial/crashed dump, never a
    value to trust)

Anti-scope: this is a stubbed-kernel unit test, never a live
`_machine_local.py` invocation (per this repo's own boundary doc --
`_machine_local.py` is not vendored here to read or run against).

Run: python -m pytest coordinator/bin/tests/test_cli_shared_dump_repos_parity.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cli_shared  # noqa: E402

_FIXTURE_REGISTRY = {
    "repos.claude_klabauter": "/machine/claude-klabauter",
    "repos.doe_claude": "/machine/doe-claude",
    "repos.some_other_repo": "/machine/some-other-repo",
}


class _FakeKernel:
    """Stands in for the in-process `_machine_local.py` module object
    `_load_machine_local_kernel()` returns -- `EXIT_OK`/`EXIT_OPERATIONAL`
    plus the three kernel primitives `machine_local_dump_repos` and
    `machine_local_get` (and their coordinator-doc-new.py twins) call.
    """

    EXIT_OK = 0
    EXIT_NOT_FOUND = 1
    EXIT_OPERATIONAL = 2

    def __init__(self, registry, operational_keys=frozenset()):
        self._registry = registry
        self._operational_keys = frozenset(operational_keys)

    def _registry_dir(self):
        return "/fake/registry"

    def _build_resolution_layers(self, reg_dir):
        return ["fake-layer"]

    def _all_keys(self, layers):
        return list(self._registry) + list(self._operational_keys)

    def resolve_one(self, key, layers):
        if key in self._operational_keys:
            return (self.EXIT_OPERATIONAL, f"machine-local: fake operational failure for {key}")
        if key in self._registry:
            return (self.EXIT_OK, self._registry[key])
        return (self.EXIT_NOT_FOUND, None)


def _load_doc_new_module():
    spec = importlib.util.spec_from_file_location(
        "coordinator_doc_new_dump_parity", _BIN_DIR / "coordinator-doc-new.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_doc_new = _load_doc_new_module()


def test_cli_shared_dump_matches_per_key_get_for_present_keys(monkeypatch):
    monkeypatch.setattr(cli_shared, "_load_machine_local_kernel", lambda: _FakeKernel(_FIXTURE_REGISTRY))

    dumped = cli_shared.machine_local_dump_repos()
    per_key = {
        key: cli_shared.machine_local_get(key) for key in _FIXTURE_REGISTRY
    }

    assert dumped == per_key == _FIXTURE_REGISTRY


def test_cli_shared_dump_and_get_agree_on_absent_non_doe_claude_key(monkeypatch):
    monkeypatch.setattr(cli_shared, "_load_machine_local_kernel", lambda: _FakeKernel(_FIXTURE_REGISTRY))

    dumped = cli_shared.machine_local_dump_repos()
    got = cli_shared.machine_local_get("repos.absent_repo")

    assert "repos.absent_repo" not in dumped
    assert got is None


def test_cli_shared_dump_type_coercion_matches_get_degrade_to_none(monkeypatch):
    registry_with_null = dict(_FIXTURE_REGISTRY, **{"repos.broken_entry": None})
    monkeypatch.setattr(cli_shared, "_load_machine_local_kernel", lambda: _FakeKernel(registry_with_null))

    dumped = cli_shared.machine_local_dump_repos()
    assert "repos.broken_entry" not in dumped

    got = cli_shared.machine_local_get("repos.broken_entry")
    assert got is None


def test_cli_shared_dump_fails_closed_on_operational_failure_for_any_key(monkeypatch):
    """Mutation-verify (Finding 1, this review; re-pinned P055-C3 for the
    in-process kernel shape): a dump where every OTHER key resolves fine but
    ONE key hits EXIT_OPERATIONAL (an ambiguous autodiscovery match, a
    malformed registry entry) must be REJECTED as a whole -- matching
    `machine_local_get`'s existing fail-closed contract. Pre-fix (spawn
    shape), this was pinned as "nonzero returncode with parseable stdout";
    the in-process kernel's equivalent failure mode is one key's
    `EXIT_OPERATIONAL`, which this test now drives directly rather than via
    a stubbed subprocess returncode."""
    monkeypatch.setattr(
        cli_shared,
        "_load_machine_local_kernel",
        lambda: _FakeKernel({"repos.claude_klabauter": "/machine/claude-klabauter"}, operational_keys={"repos.crashed_key"}),
    )
    assert cli_shared.machine_local_dump_repos() == {}


def test_doc_new_dump_matches_per_key_get_for_present_keys(monkeypatch):
    monkeypatch.setattr(_doc_new, "_load_machine_local_kernel", lambda: _FakeKernel(_FIXTURE_REGISTRY))

    dumped = _doc_new._machine_local_dump_repos()
    per_key = {
        key: _doc_new._machine_local_get(key) for key in _FIXTURE_REGISTRY
    }

    assert dumped == per_key == _FIXTURE_REGISTRY


def test_doc_new_dump_and_get_agree_on_absent_non_doe_claude_key(monkeypatch):
    monkeypatch.setattr(_doc_new, "_load_machine_local_kernel", lambda: _FakeKernel(_FIXTURE_REGISTRY))

    dumped = _doc_new._machine_local_dump_repos()
    got = _doc_new._machine_local_get("repos.absent_repo")

    assert "repos.absent_repo" not in dumped
    assert got is None


def test_doc_new_dump_fails_closed_on_operational_failure_for_any_key(monkeypatch):
    monkeypatch.setattr(
        _doc_new,
        "_load_machine_local_kernel",
        lambda: _FakeKernel({"repos.claude_klabauter": "/machine/claude-klabauter"}, operational_keys={"repos.crashed_key"}),
    )
    assert _doc_new._machine_local_dump_repos() == {}
