"""test_env_forwarding_set -- design-time gate pinning `door_env_set.h`
(the generated X-macro list both C door legs expand) to
`env_forwarding.FORWARDING_SET`, the Python-side SSOT.

Same shape as `contract.cockpit_schema.emit_schema`'s pin test: regenerate
the artifact in-memory from the declared data, diff the result byte-for-
byte against the COMMITTED file, and fail loud if they disagree. A
hand-edited `door_env_set.h` is caught the same way a hand-edited schema
JSON would be -- the failure message tells the fixer to regenerate, not to
patch the header by hand.

Spec backlink: docs/plans/2026-09-01-the-warm-door-forwards-a-declared-env-set.md
chunk C1.

Negative spec: this test does NOT parse or execute door.c/door_posix.c --
that each leg's X-macro expansion actually compiles is a C-build-time
concern, not this Python test's job.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.warm.env_forwarding import (
    BORROW,
    FORWARDING_SET,
    OVERRIDE,
    REFUSE,
    EnvEntry,
    generate_header,
)

_HEADER_PATH = (
    Path(__file__).resolve().parents[1] / "door" / "door_env_set.h"
)


def test_committed_header_matches_generated_bytes():
    generated = generate_header().encode("utf-8")
    committed = _HEADER_PATH.read_bytes()
    assert committed == generated, (
        "door_env_set.h has drifted from env_forwarding.FORWARDING_SET -- "
        "regenerate it from generate_header() rather than hand-editing. "
        f"generated:\n{generated.decode('utf-8')}\n\n"
        f"committed:\n{committed.decode('utf-8')}"
    )


def test_header_carries_do_not_edit_banner():
    text = _HEADER_PATH.read_text(encoding="utf-8")
    assert "DO NOT EDIT" in text.splitlines()[0]
    assert "env_forwarding.py" in text.splitlines()[0]


def test_header_names_each_entry_exactly_once_as_a_token():
    text = _HEADER_PATH.read_text(encoding="utf-8")
    for entry in FORWARDING_SET:
        assert text.count(f"X({entry.name})") == 1, (
            f"{entry.name!r} must appear exactly once as an X(...) token "
            "in the generated header"
        )


def test_header_carries_no_mode_or_gate_data():
    text = _HEADER_PATH.read_text(encoding="utf-8")
    for mode in (BORROW, REFUSE, OVERRIDE):
        assert mode not in text.split("*/", 1)[-1].split("/*")[0], (
            f"mode {mode!r} must not appear as C-facing data in the "
            "generated header -- mode dispatch is Python-side only"
        )


def test_forwarding_set_is_typed_name_mode_pairs():
    assert len(FORWARDING_SET) > 0
    for entry in FORWARDING_SET:
        assert isinstance(entry, EnvEntry)
        assert isinstance(entry.name, str) and entry.name
        assert entry.mode in (BORROW, REFUSE, OVERRIDE)


def test_settings_home_is_the_sole_refuse_entry():
    refuse_entries = [e for e in FORWARDING_SET if e.mode == REFUSE]
    assert [e.name for e in refuse_entries] == ["COORDINATOR_SETTINGS_HOME"]


def test_override_entries_are_exactly_the_session_env_precedence_triple():
    from coordinator_core.session.core import SESSION_ENV_PRECEDENCE

    override_entries = [e.name for e in FORWARDING_SET if e.mode == OVERRIDE]
    assert override_entries == list(SESSION_ENV_PRECEDENCE)


def test_forwarding_set_is_exactly_the_widened_twelve_named_entries():
    assert [e.name for e in FORWARDING_SET] == [
        "COORDINATOR_SETTINGS_HOME",
        "COORDINATOR_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "MACHINE_LOCAL_REGISTRY_DIR",
        "CLAUDE_HOME",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_CONFIG_DIR",
        "MACHINE_LOCAL_IMPL",
        "COORDINATOR_ROOT",
        "DOE_ROOT",
        "CLAUDE_PROJECT_DIR",
    ]


def test_c7_widened_names_are_all_borrow_mode():
    widened_names = {
        "CLAUDE_HOME",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_CONFIG_DIR",
        "MACHINE_LOCAL_IMPL",
        "COORDINATOR_ROOT",
        "DOE_ROOT",
        "CLAUDE_PROJECT_DIR",
    }
    by_name = {e.name: e.mode for e in FORWARDING_SET}
    assert widened_names <= set(by_name)
    for name in widened_names:
        assert by_name[name] == BORROW


def test_claude_pid_is_never_an_entry():
    assert "CLAUDE_PID" not in [e.name for e in FORWARDING_SET]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
