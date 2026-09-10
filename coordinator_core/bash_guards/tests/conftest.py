"""This package asserts guard logic against a fleet machine; the pin says so.

One copy of the fixture and its argument lives in
`coordinator_core/testing/fleet_pin.py` — the two conftests that need it were
byte-identical, 52 lines each, 37 of them the same prose twice.
"""

import pytest

from coordinator_core.testing.fleet_pin import assume_a_fleet_machine  # noqa: F401


@pytest.fixture(autouse=True)
def _assume_override_keys_doc_installed(monkeypatch):
    """Pin this package's dozens of ``operator_override_note`` call sites to
    the "settings-home wiki copy is installed" state: ``operator_override_note``
    gates the doc-pointer sentence on ``_helpers._override_keys_doc_reachable()``
    as well as on audience.

    Without this pin, every test in this package that asserts the rendered
    NOTE'S CONTENT (its lead sentence, the absence of a key literal, its
    byte budget) would pass or fail depending on whether the actual host
    running the suite happens to have run the settings-home install step --
    exactly the host-dependent flakiness this whole package exists to keep
    out. Those tests are validating the note's SHAPE for the common case
    (an EM on a host where install completed); the one test asserting the
    NEW degrade-to-silence behavior
    (``test_override_keys_doc_reachability.py``) explicitly overrides this
    pin back to unreachable for itself.

    Also refreshes ``_message_size._OVERRIDE_NOTE_TAIL`` (that module's own
    ``operator_override_note`` render, cached ONCE AT IMPORT -- see its
    docstring, "pure zero-argument-dependent constant" -- for the
    tail-byte-identity match `_tail_bytes` relies on). Import happens before
    this fixture ever runs, so without this second patch that cached copy
    would freeze whatever this process's REAL reachability was at import
    time while every live call in a test renders under this fixture's
    forced-True state -- the two would stop matching by identity and
    `_tail_bytes` would undercount.
    """
    from coordinator_core.bash_guards import _helpers, _message_size

    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: True)
    monkeypatch.setattr(
        _message_size,
        "_OVERRIDE_NOTE_TAIL",
        _helpers.operator_override_note(
            "", payload={"session_id": "message-size-measurement"}, git_root=None
        ),
    )
