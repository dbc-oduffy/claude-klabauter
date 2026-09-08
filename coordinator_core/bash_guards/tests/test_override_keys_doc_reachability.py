"""Pin for bug row acf32cc7bf74 (2026-09-07, measured in an Anthropic-hosted
cloud container): a positively-resolved EM used to get the override-keys
doc pointer unconditionally, even on a host where the settings-home wiki
copy (`install/substrate.py::_install_seed_wikis`'s claude-klabauter-sourced leg) was
never installed -- a remediation line pointing at a file the reader cannot
open, costing a lookup and returning nothing.

Fix: `operator_override_note` now also gates the pointer sentence on
`_helpers._override_keys_doc_reachable()`, degrading to the same `""` the
audience axis already returns for a non-EM reader. Package-wide default
(`conftest.py::_assume_override_keys_doc_installed`) pins every OTHER test
in this package to the reachable state so their content-shape assertions
stay host-independent; this module explicitly overrides that pin to prove
the unreachable branch.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.bash_guards import _helpers
from coordinator_core.bash_guards._helpers import (
    OVERRIDE_KEYS_DOC_DISPLAY,
    operator_override_note,
)

#: Captured at import time, before this package's conftest autouse fixture
#: (`_assume_override_keys_doc_installed`) has a chance to monkeypatch the
#: module attribute of the same name -- the two filesystem-probe tests below
#: restore this real implementation so they exercise the actual check, not
#: the package-wide "assume installed" stub.
_real_reachable_check = _helpers._override_keys_doc_reachable

_EM_PAYLOAD = {"session_id": "sess-c1d-em"}


def test_note_degrades_to_silence_when_doc_unreachable(monkeypatch) -> None:
    """The actual bug: unreachable must drop the sentence, not render a dead
    pointer. Overrides the package's default-reachable conftest pin."""
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: False)
    note = operator_override_note("COORDINATOR_OVERRIDE_REACHABILITY_CHECK", payload=_EM_PAYLOAD)
    assert note == "", (
        "operator_override_note() named a doc pointer while "
        "_override_keys_doc_reachable() reports unreachable -- got: %r" % note
    )


def test_note_renders_when_doc_reachable(monkeypatch) -> None:
    """Sanity control for the test above: the SAME EM payload, with
    reachability forced True, still gets the pointer -- proves the new gate
    only suppresses on unreachable, it does not silently swallow the EM
    case outright."""
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: True)
    note = operator_override_note("COORDINATOR_OVERRIDE_REACHABILITY_CHECK", payload=_EM_PAYLOAD)
    assert OVERRIDE_KEYS_DOC_DISPLAY in note


def test_reachable_check_reflects_a_real_file_on_disk(tmp_path, monkeypatch) -> None:
    """Unit-tests `_override_keys_doc_reachable()` itself against a real
    filesystem, independent of `operator_override_note`'s gating -- proves
    the check is a genuine existence probe, not a hardcoded stub."""
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", _real_reachable_check)

    missing = tmp_path / "does-not-exist" / "guard-override-keys.md"
    monkeypatch.setattr(_helpers, "OVERRIDE_KEYS_DOC_DISPLAY", str(missing))
    assert _helpers._override_keys_doc_reachable() is False

    present = tmp_path / "guard-override-keys.md"
    present.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(_helpers, "OVERRIDE_KEYS_DOC_DISPLAY", str(present))
    assert _helpers._override_keys_doc_reachable() is True


def test_reachable_check_never_raises_on_a_malformed_display_constant(monkeypatch) -> None:
    """`_override_keys_doc_reachable` must never raise -- it runs on the hot
    PreToolUse path via `operator_override_note`, and the contract every
    sibling resolver in this module keeps ("never raises") applies here
    too. A null byte is the one input `Path()`/`os.stat` reliably rejects
    with a `ValueError` on every OS."""
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", _real_reachable_check)
    monkeypatch.setattr(_helpers, "OVERRIDE_KEYS_DOC_DISPLAY", "bad\x00path")
    assert _helpers._override_keys_doc_reachable() is False


def test_reachable_check_never_expands_into_the_rendered_message(monkeypatch) -> None:
    """DR-290 form 2's invariant survives the new gate: the MESSAGE still
    carries the literal, never-expanded `~/...` string -- expansion is used
    internally by the reachability check ONLY, never rendered."""
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: True)
    note = operator_override_note("COORDINATOR_OVERRIDE_REACHABILITY_CHECK", payload=_EM_PAYLOAD)
    assert str(Path(OVERRIDE_KEYS_DOC_DISPLAY).expanduser()) not in note
    assert OVERRIDE_KEYS_DOC_DISPLAY in note
