
from __future__ import annotations

from pathlib import Path

from coordinator_core.bash_guards import _helpers
from coordinator_core.bash_guards._helpers import (
    OVERRIDE_KEYS_DOC_DISPLAY,
    operator_override_note,
)

_real_reachable_check = _helpers._override_keys_doc_reachable

_EM_PAYLOAD = {"session_id": "sess-c1d-em"}


def test_note_degrades_to_silence_when_doc_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: False)
    note = operator_override_note("COORDINATOR_OVERRIDE_REACHABILITY_CHECK", payload=_EM_PAYLOAD)
    assert note == "", (
        "operator_override_note() named a doc pointer while "
        "_override_keys_doc_reachable() reports unreachable -- got: %r" % note
    )


def test_note_renders_when_doc_reachable(monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: True)
    note = operator_override_note("COORDINATOR_OVERRIDE_REACHABILITY_CHECK", payload=_EM_PAYLOAD)
    assert OVERRIDE_KEYS_DOC_DISPLAY in note


def test_reachable_check_reflects_a_real_file_on_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", _real_reachable_check)

    missing = tmp_path / "does-not-exist" / "guard-override-keys.md"
    monkeypatch.setattr(_helpers, "OVERRIDE_KEYS_DOC_DISPLAY", str(missing))
    assert _helpers._override_keys_doc_reachable() is False

    present = tmp_path / "guard-override-keys.md"
    present.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(_helpers, "OVERRIDE_KEYS_DOC_DISPLAY", str(present))
    assert _helpers._override_keys_doc_reachable() is True


def test_reachable_check_never_raises_on_a_malformed_display_constant(monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", _real_reachable_check)
    monkeypatch.setattr(_helpers, "OVERRIDE_KEYS_DOC_DISPLAY", "bad\x00path")
    assert _helpers._override_keys_doc_reachable() is False


def test_reachable_check_never_expands_into_the_rendered_message(monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: True)
    note = operator_override_note("COORDINATOR_OVERRIDE_REACHABILITY_CHECK", payload=_EM_PAYLOAD)
    assert str(Path(OVERRIDE_KEYS_DOC_DISPLAY).expanduser()) not in note
    assert OVERRIDE_KEYS_DOC_DISPLAY in note
