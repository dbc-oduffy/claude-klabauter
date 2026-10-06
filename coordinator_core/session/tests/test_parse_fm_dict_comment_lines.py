"""`_parse_fm_dict` never reads a commented-out `# key: v` line as a key."""

from __future__ import annotations

from coordinator_core.session.work_state import _parse_fm_dict


def test_commented_line_is_not_a_key() -> None:
    fm = _parse_fm_dict("status: open\n# predecessor_handoff: x\ntitle: t\n")
    assert fm == {"status": "open", "title": "t"}
