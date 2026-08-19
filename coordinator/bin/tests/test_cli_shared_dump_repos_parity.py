"""test_cli_shared_dump_repos_parity.py — dump-vs-get equivalence for
`machine_local_dump_repos` (cli_shared.py) and `_machine_local_dump_repos`
(coordinator-doc-new.py).

Review: state/subagent-share/a3d742ff-223c-4133-aedd-ed60ce61b558/amp-review-s6.md
Finding 2 -- these batch counterparts to the old enumerate-then-`get` path
had zero test coverage, and the underlying `dump --prefix repos --format
json` implementation (`_machine_local.py::cmd_dump`) does NOT live in this
repo -- it's a discovery-resolved surface owned by coordinator-claude, so
neither this test nor a human reviewer can verify the docstring's
byte-identical-to-`get` equivalence claim by reading its code. A stubbed
behavioural parity test against a shared fixture registry is the only
available guard for that claim in THIS repo, which is why it matters more
than usual here, not less.

Both `subprocess.run` call sites (the `dump` batch call and the per-key
`get` call) are monkeypatched against the SAME fixture registry
(`_FIXTURE_REGISTRY`) so the two code paths are proven equivalent for:
  - present keys (ordinary case)
  - an ABSENT key that is NOT `repos.doe_claude` (the one key with an
    explicit `setdefault(...)` backstop in `resolve_from_repo` --
    precisely because the authors worried about a default-on-absent gap
    for it specifically; every other `repos.*` key has no such backstop,
    so this test deliberately covers one of those instead)
  - a key present in the dump JSON but with a non-string value (type
    coercion: `machine_local_dump_repos` filters non-str/falsy values the
    same way `machine_local_get` degrades an empty/failed `get` to None)

Anti-scope: this is a stubbed-subprocess unit test, never a live
`_machine_local.py` invocation (per this repo's own boundary doc --
`_machine_local.py` is not vendored here to read or run against).

Run: python -m pytest coordinator/bin/tests/test_cli_shared_dump_repos_parity.py -q
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
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

# Fixture registry shared by both the `dump` stub and the per-key `get`
# stub -- the single source of truth both code paths are checked against.
# `repos.absent_repo` is deliberately NOT present here at all (simulating
# an unregistered repo key) -- neither the dump JSON nor a `get` call ever
# succeeds for it, and it is NOT `repos.doe_claude` (the one key with its
# own setdefault backstop elsewhere in cli_shared.py).
_FIXTURE_REGISTRY = {
    "repos.claude_klabauter": "/machine/claude-klabauter",
    "repos.doe_claude": "/machine/doe-claude",
    "repos.some_other_repo": "/machine/some-other-repo",
}


def _fake_dump_run(cmd, **_kwargs):
    """Stands in for `python _machine_local.py dump --prefix repos --format
    json` -- emits exactly the fixture registry as JSON, rc=0."""
    assert "dump" in cmd
    return subprocess.CompletedProcess(cmd, 0, json.dumps(_FIXTURE_REGISTRY), "")


def _fake_get_run(cmd, **_kwargs):
    """Stands in for `python _machine_local.py get <key>` -- looks the key
    up in the SAME fixture registry, rc=1/empty stdout on a miss (the real
    `get` CLI's contract for an unregistered key)."""
    key = cmd[-1]
    if key in _FIXTURE_REGISTRY:
        return subprocess.CompletedProcess(cmd, 0, _FIXTURE_REGISTRY[key] + "\n", "")
    return subprocess.CompletedProcess(cmd, 1, "", "")


def _fake_dispatch_run(cmd, **_kwargs):
    if "dump" in cmd:
        return _fake_dump_run(cmd, **_kwargs)
    if "get" in cmd:
        return _fake_get_run(cmd, **_kwargs)
    raise AssertionError(f"unhandled cmd: {cmd!r}")


def _load_doc_new_module():
    spec = importlib.util.spec_from_file_location(
        "coordinator_doc_new_dump_parity", _BIN_DIR / "coordinator-doc-new.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_doc_new = _load_doc_new_module()


# ---------------------------------------------------------------------------
# cli_shared.py: machine_local_dump_repos vs machine_local_get
# ---------------------------------------------------------------------------


def test_cli_shared_dump_matches_per_key_get_for_present_keys(monkeypatch):
    monkeypatch.setattr(cli_shared.subprocess, "run", _fake_dispatch_run)

    dumped = cli_shared.machine_local_dump_repos()
    per_key = {
        key: cli_shared.machine_local_get(key) for key in _FIXTURE_REGISTRY
    }

    assert dumped == per_key == _FIXTURE_REGISTRY


def test_cli_shared_dump_and_get_agree_on_absent_non_doe_claude_key(monkeypatch):
    """Default-on-absent parity for a key OTHER than repos.doe_claude (the
    only key with its own setdefault backstop) -- both paths must treat an
    unregistered key identically: absent from the dump dict, None from
    per-key get."""
    monkeypatch.setattr(cli_shared.subprocess, "run", _fake_dispatch_run)

    dumped = cli_shared.machine_local_dump_repos()
    got = cli_shared.machine_local_get("repos.absent_repo")

    assert "repos.absent_repo" not in dumped
    assert got is None


def test_cli_shared_dump_type_coercion_matches_get_degrade_to_none(monkeypatch):
    """A non-string / falsy dump JSON value must be filtered out by
    `machine_local_dump_repos` exactly as a `get` call that fails or
    returns empty stdout degrades to None -- neither path should ever hand
    a caller a non-string or empty `repos.*` value."""
    registry_with_null = dict(_FIXTURE_REGISTRY, **{"repos.broken_entry": None})

    def _dump_with_null(cmd, **_kwargs):
        assert "dump" in cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps(registry_with_null), "")

    def _get_for_broken_entry(cmd, **_kwargs):
        # The real `get` CLI never emits a JSON `null` -- an unresolvable
        # value comes back as empty stdout, matching machine_local_get's
        # own not-str-or-empty contract.
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli_shared.subprocess, "run", _dump_with_null)
    dumped = cli_shared.machine_local_dump_repos()
    assert "repos.broken_entry" not in dumped

    monkeypatch.setattr(cli_shared.subprocess, "run", _get_for_broken_entry)
    got = cli_shared.machine_local_get("repos.broken_entry")
    assert got is None


def test_cli_shared_dump_fails_closed_on_nonzero_returncode_with_parseable_stdout(monkeypatch):
    """Mutation-verify (Finding 1, this review): a `dump` subprocess that
    exits non-zero but still emits parseable JSON on stdout (partial
    write, crash after emitting some keys) must be REJECTED -- matching
    `machine_local_get`'s existing `returncode != 0` guard. Pre-fix,
    `machine_local_dump_repos` gated only on `stdout.strip()` truthiness
    plus successful `json.loads`, so this exact input would have been
    silently ACCEPTED as a partial `repos.*` table -- this test pins the
    fix and would fail red against that pre-fix behaviour."""
    def _nonzero_but_parseable(cmd, **_kwargs):
        assert "dump" in cmd
        # Partial dict -- as if the dump process crashed after emitting
        # only one key.
        return subprocess.CompletedProcess(
            cmd, 1, json.dumps({"repos.claude_klabauter": "/machine/claude-klabauter"}), "boom"
        )

    monkeypatch.setattr(cli_shared.subprocess, "run", _nonzero_but_parseable)
    assert cli_shared.machine_local_dump_repos() == {}


# ---------------------------------------------------------------------------
# coordinator-doc-new.py: _machine_local_dump_repos vs _machine_local_get
# (private twins of the above, same contract, separate module).
# ---------------------------------------------------------------------------


def test_doc_new_dump_matches_per_key_get_for_present_keys(monkeypatch):
    monkeypatch.setattr(_doc_new.subprocess, "run", _fake_dispatch_run)

    dumped = _doc_new._machine_local_dump_repos()
    per_key = {
        key: _doc_new._machine_local_get(key) for key in _FIXTURE_REGISTRY
    }

    assert dumped == per_key == _FIXTURE_REGISTRY


def test_doc_new_dump_and_get_agree_on_absent_non_doe_claude_key(monkeypatch):
    monkeypatch.setattr(_doc_new.subprocess, "run", _fake_dispatch_run)

    dumped = _doc_new._machine_local_dump_repos()
    got = _doc_new._machine_local_get("repos.absent_repo")

    assert "repos.absent_repo" not in dumped
    assert got is None


def test_doc_new_dump_fails_closed_on_nonzero_returncode_with_parseable_stdout(monkeypatch):
    """Mutation-verify (Finding 1, this review) for the coordinator-doc-new.py
    twin: pins the same fail-closed fix as
    `test_cli_shared_dump_fails_closed_on_nonzero_returncode_with_parseable_stdout`
    above -- would fail red against the pre-fix (returncode-blind) behaviour."""
    def _nonzero_but_parseable(cmd, **_kwargs):
        assert "dump" in cmd
        return subprocess.CompletedProcess(
            cmd, 1, json.dumps({"repos.claude_klabauter": "/machine/claude-klabauter"}), "boom"
        )

    monkeypatch.setattr(_doc_new.subprocess, "run", _nonzero_but_parseable)
    assert _doc_new._machine_local_dump_repos() == {}
