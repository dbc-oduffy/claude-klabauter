"""test_cc_invoke_foreign_identity.py — the C3 gate on the split announcement,
and the State-1 remediation site C3 deliberately leaves untouched.

Spec backlink: docs/plans/2026-08-30-the-engine-stops-naming-its-own-repo.md § C3,
state/audits/2026-08-30-foreign-repo-identity-disposition-probe.md.

`_announce_engine_cli_split` itself stays unconditional (pinned by
`test_cc_invoke_engine_split_announcement.py`, unmodified by this chunk). The
new gate lives one call site up, in `require_dispatch_engine_on_path`'s own
wrapper `_reader_owns_one_of_the_split_trees`: INCIDENTAL for a reader whose
own repo is neither the CLI root nor the dispatch root, SUBJECT (announces)
for a reader who owns either.

`_state1_remediation_message` is the opposite disposition on the SAME probe:
SUBJECT on axis 3 because the reader genuinely must clone and register the
named repo to unblock. This module asserts it is UNCHANGED — still names
Claude-klabauter — because C3's body says the fix here is "declare it
subject-class in a comment and keep it", not suppress it.

Run: pytest coordinator/bin/tests/test_cc_invoke_foreign_identity.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

pytestmark = pytest.mark.cadence


class TestTheSplitAnnouncementGate:
    """`_reader_owns_one_of_the_split_trees` — the C3 gate wrapping
    `_announce_engine_cli_split` at its one production call site."""

    def test_silent_for_a_reader_who_owns_neither_root(self, monkeypatch):
        monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: r"X:\a-cli-root")  # abs-path-ok: synthetic fixture, never resolved on disk
        monkeypatch.setattr(
            "coordinator_core.git.repo_root.show_toplevel",
            lambda: r"X:\a-third-repo",  # abs-path-ok: synthetic fixture, never resolved on disk
        )
        assert (
            _mod._reader_owns_one_of_the_split_trees(r"X:\a-dispatch-root") is False  # abs-path-ok: synthetic fixture, never resolved on disk
        )

    def test_present_for_a_reader_who_owns_the_cli_root(self, monkeypatch):
        monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: r"X:\a-cli-root")  # abs-path-ok: synthetic fixture, never resolved on disk
        monkeypatch.setattr(
            "coordinator_core.git.repo_root.show_toplevel",
            lambda: r"X:\a-cli-root",  # abs-path-ok: synthetic fixture, never resolved on disk
        )
        assert (
            _mod._reader_owns_one_of_the_split_trees(r"X:\a-dispatch-root") is True  # abs-path-ok: synthetic fixture, never resolved on disk
        )

    def test_present_for_a_reader_who_owns_the_dispatch_root(self, monkeypatch):
        monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: r"X:\a-cli-root")  # abs-path-ok: synthetic fixture, never resolved on disk
        monkeypatch.setattr(
            "coordinator_core.git.repo_root.show_toplevel",
            lambda: r"X:\a-dispatch-root",  # abs-path-ok: synthetic fixture, never resolved on disk
        )
        assert (
            _mod._reader_owns_one_of_the_split_trees(r"X:\a-dispatch-root") is True  # abs-path-ok: synthetic fixture, never resolved on disk
        )

    def test_fails_open_when_the_readers_own_repo_is_unresolvable(self, monkeypatch):
        monkeypatch.setattr(_mod, "resolve_engine_root", lambda _f: r"X:\a-cli-root")  # abs-path-ok: synthetic fixture, never resolved on disk
        monkeypatch.setattr(
            "coordinator_core.git.repo_root.show_toplevel", lambda: None
        )
        assert (
            _mod._reader_owns_one_of_the_split_trees(r"X:\a-dispatch-root") is True  # abs-path-ok: synthetic fixture, never resolved on disk
        )

    def test_fails_open_on_any_unexpected_exception(self, monkeypatch):
        def _boom(_f):
            raise RuntimeError("no checkout found")

        monkeypatch.setattr(_mod, "resolve_engine_root", _boom)
        assert (
            _mod._reader_owns_one_of_the_split_trees(r"X:\a-dispatch-root") is True  # abs-path-ok: synthetic fixture, never resolved on disk
        )

    def test_require_dispatch_engine_on_path_skips_the_announce_call_when_ungated(
        self, monkeypatch, capsys
    ):
        """Integration half: `require_dispatch_engine_on_path` itself must not
        call the (still-unconditional) announcer when the gate says no."""
        monkeypatch.setattr(_mod, "_ENGINE_SPLIT_ANNOUNCED", False, raising=False)
        monkeypatch.setattr(_mod, "_reader_owns_one_of_the_split_trees", lambda _root: False)
        called = []
        monkeypatch.setattr(
            _mod, "_announce_engine_cli_split", lambda root: called.append(root)
        )
        monkeypatch.setattr(_mod, "_front_insert_on_path", lambda root: root)
        monkeypatch.setattr(_mod, "_resolve_claude_klabauter_root", lambda: r"X:\a-root")  # abs-path-ok: synthetic fixture, never resolved on disk
        fake_report = type(
            "Report", (), {"verdict": "explicit-not-divergent", "imported_file": None, "engine_root": None}
        )()
        monkeypatch.setattr(_mod, "_report_provenance", lambda *a, **k: fake_report)

        result = _mod.require_dispatch_engine_on_path()

        assert result == r"X:\a-root"  # abs-path-ok: synthetic fixture, never resolved on disk
        assert called == [], "the gate said no; the announcer must not have been called"


class TestState1RemediationStaysSubjectClass:
    """`_state1_remediation_message` is the falsifier's primary site and is
    KEPT unchanged by C3: SUBJECT-class because a reader who cannot resolve
    the engine cannot act on a message that hides which engine to clone."""

    def test_it_still_names_claude_klabauter_for_an_ordinary_state1_failure(self):
        message = _mod._state1_remediation_message("some.op", None)

        assert "claude-klabauter" in message

    def test_registry_timeout_variant_also_names_claude_klabauter(self):
        message = _mod._state1_remediation_message(
            "some.op", None, registry_read_timed_out=True
        )

        assert "claude-klabauter" in message
