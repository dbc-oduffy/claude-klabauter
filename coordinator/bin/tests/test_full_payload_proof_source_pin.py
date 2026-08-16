"""test_full_payload_proof_source_pin -- regression coverage for
`percolate-full-payload-proof.py`'s commit-sha source pin (§ that module's
docstring 'SOURCE PIN').

Traced root cause (task: "the convergence proof cannot produce a valid
verdict on an active fleet"): the proof publishes the full payload twice,
minutes apart, into a wiped scratch destination and asserts the two runs
converge byte-identically. `publish.py`'s own `_git_materialize_ref` always
resolves `ref="HEAD"` live, at the moment it is called -- so on a genuinely
shared, actively-committed-to branch, pass 1 and pass 2 can publish from two
DIFFERENT commits. A byte difference between them then proves nothing about
transform determinism; it just proves the fleet kept moving. A live run
(state/audits/2026-08-05-blank-machine-install-readiness.md's session)
reproduced this exactly: 28 peer commits landed inside a 16-minute run, and
every divergent file corresponded one-for-one to one of those commits.

This file pins the two properties the fix (`_resolve_pinned_commit_shas`,
`_make_pinned_rev_parse`, `_parse_provenance_lines`) must hold, independent
of a real ~15 minute publish run:

  1. Both passes resolve HEAD for a pinned root's toplevel to the SAME
     pre-captured sha, even when the "live" HEAD a later call could see has
     since moved -- and that movement is recorded, never silently absorbed.
  2. A contributing root this harness cannot pin (no git toplevel, no
     resolvable HEAD, or -- mid-run -- a toplevel the pre-run pin walk never
     covered) is a loud `PinNotHonoredError`, never a silent fallback to an
     unpinned live HEAD read.

Run: python -m pytest coordinator/bin/tests/test_full_payload_proof_source_pin.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_proof_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_full_payload_proof_source_pin_under_test",
        _BIN_DIR / "percolate-full-payload-proof.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


proof = _load_proof_module()


# ---------------------------------------------------------------------------
# _parse_provenance_lines -- the verdict's cheapest verification hook.
# ---------------------------------------------------------------------------
class TestParseProvenanceLines:
    def test_single_line_parsed(self):
        stdout = "=== claude-klabauter-bin (mirror) ===\n  Provenance: /repo shipped from abc123\n"
        assert proof._parse_provenance_lines(stdout) == {"/repo": "abc123"}

    def test_multiple_roots_parsed(self):
        stdout = (
            "  Provenance: /repo shipped from abc123\n"
            "  Provenance: /other-repo shipped from def456\n"
        )
        assert proof._parse_provenance_lines(stdout) == {
            "/repo": "abc123",
            "/other-repo": "def456",
        }

    def test_unrelated_lines_ignored(self):
        stdout = "=== claude-klabauter-bin (mirror) ===\n  Synced: 3 file(s)\n"
        assert proof._parse_provenance_lines(stdout) == {}

    def test_no_provenance_lines_at_all_is_empty_dict(self):
        assert proof._parse_provenance_lines("") == {}


# ---------------------------------------------------------------------------
# _make_pinned_rev_parse -- both passes must resolve HEAD from the pin, not
# a freshly re-invoked live git call.
# ---------------------------------------------------------------------------
class _FakePath:
    """Minimal stand-in so `_make_pinned_rev_parse`'s wrapper can be driven
    without a real git repo -- only `str(path)` is ever used by the fake
    `real_rev_parse` below, matching how `publish.py`'s real
    `_git_rev_parse` treats its `path` argument (passed straight to
    `git -C <path> ...`)."""

    def __init__(self, value: str):
        self._value = value

    def __str__(self):
        return self._value


def _fake_real_rev_parse(live_head_by_toplevel, toplevel_by_path):
    """Builds a fake `real_rev_parse(path, *args)` -- `--show-toplevel`
    resolves `path` to its configured toplevel, `HEAD` resolves that
    toplevel to its currently-configured live head. Mirrors
    `publish._git_rev_parse`'s real two call shapes exactly."""

    def _rev_parse(path, *args):
        key = str(path)
        if args == ("--show-toplevel",):
            return toplevel_by_path.get(key)
        if args == ("HEAD",):
            return live_head_by_toplevel.get(key)
        raise AssertionError(f"unexpected _git_rev_parse args in test double: {args}")

    return _rev_parse


class TestMakePinnedRevParse:
    def test_non_head_call_passes_through_untouched(self):
        real = _fake_real_rev_parse({}, {"/root": "/root"})
        notes: list = []
        wrapped = proof._make_pinned_rev_parse(real, {}, notes, pass_number=1)
        assert wrapped(_FakePath("/root"), "--show-toplevel") == "/root"
        assert notes == []

    def test_pinned_head_returned_even_when_live_head_has_moved(self):
        real = _fake_real_rev_parse(
            live_head_by_toplevel={"/root": "sha-live-later"},
            toplevel_by_path={"/root": "/root"},
        )
        notes: list = []
        wrapped = proof._make_pinned_rev_parse(
            real, {"/root": "sha-pinned-at-start"}, notes, pass_number=2
        )
        assert wrapped(_FakePath("/root"), "HEAD") == "sha-pinned-at-start"

    def test_head_movement_is_recorded_not_silently_absorbed(self):
        real = _fake_real_rev_parse(
            live_head_by_toplevel={"/root": "sha-live-later"},
            toplevel_by_path={"/root": "/root"},
        )
        notes: list = []
        wrapped = proof._make_pinned_rev_parse(
            real, {"/root": "sha-pinned-at-start"}, notes, pass_number=2
        )
        wrapped(_FakePath("/root"), "HEAD")
        assert len(notes) == 1
        assert "sha-pinned-at-start" in notes[0]
        assert "sha-live-later" in notes[0]
        assert "pass 2" in notes[0]

    def test_no_movement_no_note(self):
        real = _fake_real_rev_parse(
            live_head_by_toplevel={"/root": "sha-pinned-at-start"},
            toplevel_by_path={"/root": "/root"},
        )
        notes: list = []
        wrapped = proof._make_pinned_rev_parse(
            real, {"/root": "sha-pinned-at-start"}, notes, pass_number=1
        )
        wrapped(_FakePath("/root"), "HEAD")
        assert notes == []

    def test_unpinned_toplevel_raises_loud_not_silent_fallback(self):
        real = _fake_real_rev_parse(
            live_head_by_toplevel={"/root": "sha-live"},
            toplevel_by_path={"/root": "/root"},
        )
        notes: list = []
        # pinned_shas deliberately does not cover "/root" -- simulates the
        # pre-run pin walk missing a root a pass later resolves.
        wrapped = proof._make_pinned_rev_parse(real, {}, notes, pass_number=1)
        with pytest.raises(proof.PinNotHonoredError):
            wrapped(_FakePath("/root"), "HEAD")

    def test_unresolvable_toplevel_raises_loud(self):
        real = _fake_real_rev_parse(live_head_by_toplevel={}, toplevel_by_path={})
        notes: list = []
        wrapped = proof._make_pinned_rev_parse(real, {"/root": "sha"}, notes, pass_number=1)
        with pytest.raises(proof.PinNotHonoredError):
            wrapped(_FakePath("/not-a-repo"), "HEAD")


# ---------------------------------------------------------------------------
# _resolve_pinned_commit_shas -- the pre-run walk that builds the pin map
# both passes' `_make_pinned_rev_parse` wrappers consult.
# ---------------------------------------------------------------------------
class _FakeTarget:
    def __init__(self, name, roots):
        self.name = name
        self._roots = roots


class _FakePublishModule:
    """Stands in for the real `publish` module -- only the three attributes
    `_resolve_pinned_commit_shas` actually reads."""

    def __init__(self, row_to_roots, toplevel_by_root, head_by_toplevel):
        self._row_to_roots = row_to_roots
        self._toplevel_by_root = toplevel_by_root
        self._head_by_toplevel = head_by_toplevel
        self.head_resolution_calls = []

    def parse_target_row(self, row):
        return _FakeTarget(row, self._row_to_roots[row])

    def _contributing_roots(self, target):
        return target._roots

    def _git_rev_parse(self, path, *args):
        key = str(path)
        if args == ("--show-toplevel",):
            return self._toplevel_by_root.get(key)
        if args == ("HEAD",):
            self.head_resolution_calls.append(key)
            return self._head_by_toplevel.get(key)
        raise AssertionError(f"unexpected args {args}")


class TestResolvePinnedCommitShas:
    def test_single_row_single_root_pins_by_toplevel(self):
        fake = _FakePublishModule(
            row_to_roots={"row1": [_FakePath("/repo/src")]},
            toplevel_by_root={"/repo/src": "/repo"},
            # `_resolve_pinned_commit_shas` re-wraps the resolved toplevel
            # string in `Path(...)` before the second `_git_rev_parse` call
            # (matching `_git_rev_parse(path: Path, ...)`'s declared type),
            # so the HEAD lookup key is `str(Path(toplevel))` -- which
            # normalizes separators on Windows -- not the raw literal.
            head_by_toplevel={str(Path("/repo")): "sha-abc"},
        )
        pins = proof._resolve_pinned_commit_shas(fake, ["row1"])
        assert pins == {"/repo": "sha-abc"}

    def test_two_rows_sharing_a_toplevel_resolve_head_only_once(self):
        fake = _FakePublishModule(
            row_to_roots={
                "row1": [_FakePath("/repo/a")],
                "row2": [_FakePath("/repo/b")],
            },
            toplevel_by_root={"/repo/a": "/repo", "/repo/b": "/repo"},
            # See test_single_row_single_root_pins_by_toplevel for why this
            # key must be `str(Path(...))`, not the raw literal.
            head_by_toplevel={str(Path("/repo")): "sha-abc"},
        )
        pins = proof._resolve_pinned_commit_shas(fake, ["row1", "row2"])
        assert pins == {"/repo": "sha-abc"}
        assert fake.head_resolution_calls == [str(Path("/repo"))]

    def test_rows_in_different_repos_get_independent_pins(self):
        fake = _FakePublishModule(
            row_to_roots={
                "row1": [_FakePath("/repo-a/src")],
                "row2": [_FakePath("/repo-b/src")],
            },
            toplevel_by_root={"/repo-a/src": "/repo-a", "/repo-b/src": "/repo-b"},
            # See test_single_row_single_root_pins_by_toplevel for why these
            # keys must be `str(Path(...))`, not the raw literal.
            head_by_toplevel={
                str(Path("/repo-a")): "sha-a",
                str(Path("/repo-b")): "sha-b",
            },
        )
        pins = proof._resolve_pinned_commit_shas(fake, ["row1", "row2"])
        assert pins == {"/repo-a": "sha-a", "/repo-b": "sha-b"}

    def test_unresolvable_toplevel_raises_before_either_pass_runs(self):
        fake = _FakePublishModule(
            row_to_roots={"row1": [_FakePath("/not-a-repo")]},
            toplevel_by_root={},
            head_by_toplevel={},
        )
        with pytest.raises(proof.PinNotHonoredError):
            proof._resolve_pinned_commit_shas(fake, ["row1"])

    def test_unresolvable_head_raises_before_either_pass_runs(self):
        fake = _FakePublishModule(
            row_to_roots={"row1": [_FakePath("/repo/src")]},
            toplevel_by_root={"/repo/src": "/repo"},
            head_by_toplevel={},  # unborn HEAD
        )
        with pytest.raises(proof.PinNotHonoredError):
            proof._resolve_pinned_commit_shas(fake, ["row1"])
