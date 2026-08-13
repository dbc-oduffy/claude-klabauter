"""Portability regression for the override-doc pointer rendered into every
guard message (`operator_override_note`, via
`bash_guards._helpers._resolve_override_keys_doc_display` /
`resolve_override_keys_doc_display`).

2026-08-05 (PM-raised, break-class): this pointer used to PREFER an
in-process-resolved absolute path (env var, else a direct read of the
`.claude-klabauter-root` pointer file), falling back to the repo-qualified hint
(`OVERRIDE_KEYS_DOC_DISPLAY`) only when that resolution failed. On a host
where it resolved, the rendered guard message carried the operator's home
directory and repo checkout name -- a machine-path leak reaching every
guard-message surface (agent transcripts, handoffs, cross-repo memos, pasted
logs) that `check-machine-path-leak.py` never covered (that checker's scope
is `settings.json`/`working-repos.yaml` structural leaf values only; it never
scanned Python source or runtime-rendered guard text).

That is one half of the defect. The other, and the reason this is
break-class rather than a privacy nit: the absolute form ONLY EVER resolved
correctly in the SAME process, on the SAME machine, that rendered it. A
POSIX-joined absolute path is meaningless on Windows (this repo holds
Windows first-class); an absolute path naming THIS operator's home directory
resolves to nothing for any other username or checkout location -- i.e.
every machine but the one that produced it. This engine ships as an OSS
mirror, so every downstream reader on every platform would have received a
guard pointer naming the maintainer's laptop. A pointer that resolves only
in the process that rendered it is simply broken, not "correct but
sensitive" -- fixing it is a portability fix, not a redaction exercise.

The fix collapsed the resolver to an unconditional return of the
repo-qualified, PORTABLE hint (`OVERRIDE_KEYS_DOC_DISPLAY`, e.g.
"claude-klabauter docs/reference/guard-override-keys.md"). This module pins
the invariant that fix establishes -- NOT "the string does not contain this operator's
home directory" (a symptom check that would pass on a host still fully
carrying the bug, e.g. under a different username or on a Windows-rooted
checkout) -- but the actual property: the rendered pointer is
never an absolute filesystem path, on any platform, regardless of what
environment variables or on-disk pointer files are present when it renders.

Spec backlink: PM-raised break-class dispatch, 2026-08-05, this commit.
"""

from __future__ import annotations

import ntpath
import os
import posixpath

import pytest

from coordinator_core.bash_guards import _helpers as h


def _looks_like_an_absolute_path(candidate: str) -> bool:
    """True if any WHITESPACE-DELIMITED token in ``candidate`` is an
    absolute filesystem path under EITHER POSIX or Windows path semantics --
    checked against both unconditionally (not gated on `sys.platform`),
    because the invariant this pins is "resolves the same way regardless of
    which platform renders it or which platform reads it", not "is not
    absolute on the platform running this test"."""
    return any(
        posixpath.isabs(token) or ntpath.isabs(token)
        for token in candidate.split()
    )


class TestOverrideKeysDocDisplayIsPortable:
    def test_constant_is_not_an_absolute_path(self) -> None:
        assert not _looks_like_an_absolute_path(h.OVERRIDE_KEYS_DOC_DISPLAY)

    def test_constant_is_repo_qualified_relative_form(self) -> None:
        assert h.OVERRIDE_KEYS_DOC_DISPLAY == "claude-klabauter " + h.OVERRIDE_KEYS_DOC
        assert not os.path.isabs(h.OVERRIDE_KEYS_DOC)

    def test_resolver_returns_the_portable_constant_unconditionally(self) -> None:
        assert h._resolve_override_keys_doc_display() == h.OVERRIDE_KEYS_DOC_DISPLAY
        assert h.resolve_override_keys_doc_display() == h.OVERRIDE_KEYS_DOC_DISPLAY

    @pytest.mark.parametrize(
        "claude_klabauter_root_env",
        [
            "/Users/someoperator/claude-klabauter",
            r"C:\Users\someoperator\claude-klabauter",
            "/home/ci-runner/checkouts/claude-klabauter",
        ],
    )
    def test_resolver_ignores_machine_root_state(
        self, monkeypatch: pytest.MonkeyPatch, claude_klabauter_root_env: str
    ) -> None:
        """The pre-fix resolver's whole defect was PREFERRING exactly this
        kind of machine-local state. Proving the render is now identical
        regardless of what `CLAUDE_KLABAUTER_ROOT` names is the direct regression test
        for "does not resolve only in the process that rendered it" -- an
        env var that changes the render is the same bug whether or not this
        particular assertion happens to run on a host where it currently
        does."""
        monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", claude_klabauter_root_env)
        assert h._resolve_override_keys_doc_display() == h.OVERRIDE_KEYS_DOC_DISPLAY

    def test_resolver_ignores_absence_of_machine_root_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        assert h._resolve_override_keys_doc_display() == h.OVERRIDE_KEYS_DOC_DISPLAY


class TestOperatorOverrideNoteIsPortable:
    """`operator_override_note` is the actual per-firing string every guard
    message emits -- the constant/resolver tests above are necessary but not
    sufficient, since a future edit could reintroduce an absolute path at
    the call site without touching the resolver itself."""

    def test_note_contains_no_absolute_path_token(self) -> None:
        note = h.operator_override_note(
            "COORDINATOR_OVERRIDE_EXAMPLE", payload={"session_id": "sess-c1d-em"}
        )
        assert not _looks_like_an_absolute_path(note)

    def test_note_is_stable_regardless_of_machine_root_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = h.operator_override_note(
            "COORDINATOR_OVERRIDE_EXAMPLE", payload={"session_id": "sess-c1d-em"}
        )
        monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "/Users/someoperator/claude-klabauter")
        assert (
            h.operator_override_note(
                "COORDINATOR_OVERRIDE_EXAMPLE",
                payload={"session_id": "sess-c1d-em"},
            )
            == baseline
        )
