"""Regression tests for the D3 (2026-07-29) handoff-kind-enum branch that
`state/bug-backlog/2026-07-29-the-d3-out-of-enum-handoff-kind-uncondit-
4a0948383d56.yaml` reported as shipping with zero coverage in either guard.

The deny sibling (`validate_frontmatter_schema_deny._evaluate_handoff_kind_
enum`) already gained a dedicated test class
(`TestHandoffKindOffEnumUnconditionalDeny` in
test_validate_frontmatter_schema_deny.py) in a later commit on this branch,
but two gaps from the backlog item remained uncovered by that class:

1. The advisory sibling (`validate_frontmatter_schema_advisory.
   _handoff_kind_off_enum_fires`) had NO dedicated test at all — only an
   incidental assertion (inside the deny suite's own tests) that it stands
   down when the deny fires.
2. Neither guard had a test for the reviewer's "nit" half of the finding:
   `raw_kind` was unconditionally coerced via `str(raw_kind)` before the
   enum comparison, so a non-scalar `kind` (a YAML list/mapping) produced a
   garbled off-enum message instead of standing down to let the ordinary
   JSON-schema type check name the real defect.

This file is new (not an edit to the existing suites) per this fix wave's
footprint; it exercises both guards directly against a live DoE-claude
schema checkout, mirroring the existing suites' `_pin_doe_root` /
`_payload` conventions.

Spec backlink: state/bug-backlog/2026-07-29-the-d3-out-of-enum-handoff-kind-uncondit-4a0948383d56.yaml
"""

from __future__ import annotations

import pytest

from coordinator_core.testing.doe_root import doe_root_and_present
from coordinator_core.write_guards import validate_frontmatter_schema_advisory as advisory_guard
from coordinator_core.write_guards import validate_frontmatter_schema_deny as deny_guard

_doe_root, _doe_present = doe_root_and_present()


@pytest.fixture(autouse=True)
def _pin_doe_root(monkeypatch):
    if not _doe_present:
        pytest.skip("sibling DoE-claude checkout not found")
    monkeypatch.setattr(deny_guard, "coordinator_doe_root", lambda: _doe_root)
    monkeypatch.setattr(advisory_guard, "coordinator_doe_root", lambda: _doe_root)


def _payload(tool_name, file_path, cwd, **tool_input_extra):
    tool_input = {"file_path": file_path}
    tool_input.update(tool_input_extra)
    return {"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd}


_VALID_FM = (
    "---\nkind: {kind}\ntitle: t\ncreated: 2026-07-29\nbranch: main\n"
    "status: open\npredecessor: none\ncategory: infra\n"
    "summary: a one-line summary\n---\nbody"
)


def _write_handoff(tmp_path, name, kind_line):
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    fp = handoff_dir / name
    fp.write_text(_VALID_FM.format(kind=kind_line), encoding="utf-8")
    return fp


class TestAdvisoryHandoffKindOffEnumStandDown:
    """Dedicated coverage for `_handoff_kind_off_enum_fires` — previously
    only exercised incidentally as a side-assertion inside the deny
    sibling's suite, never against the advisory guard's own `check()`
    behavior end-to-end.
    """

    @pytest.mark.parametrize("strict", ["0", "1"])
    def test_off_enum_kind_never_warns_here_regardless_of_strict(
        self, tmp_path, monkeypatch, strict
    ):
        if strict == "1":
            monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = _write_handoff(tmp_path, "off-enum.md", "not-a-real-kind")
        payload = _payload("Edit", str(fp), str(tmp_path), old_string="body", new_string="body2")

        assert advisory_guard.check(payload) is None, (
            "advisory must stand down for an off-enum kind in both modes — "
            "this branch is the deny sibling's unconditional territory"
        )
        assert deny_guard.check(payload) is not None, (
            "sanity: the deny sibling must actually fire on this payload, "
            "otherwise the advisory stand-down above is vacuous"
        )

    def test_absent_kind_stays_silent(self, tmp_path):
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        fp = handoff_dir / "absent-kind.md"
        fp.write_text(
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: none\ncategory: infra\nsummary: a one-line summary\n"
            "---\nbody",
            encoding="utf-8",
        )
        payload = _payload("Edit", str(fp), str(tmp_path), old_string="body", new_string="body2")
        assert advisory_guard.check(payload) is None

    def test_valid_kind_stays_silent(self, tmp_path):
        fp = _write_handoff(tmp_path, "valid-kind.md", "session-handoff")
        payload = _payload("Edit", str(fp), str(tmp_path), old_string="body", new_string="body2")
        assert advisory_guard.check(payload) is None

    @pytest.mark.parametrize("strict", ["0", "1"])
    def test_legacy_alias_kind_never_fires_this_branch(self, tmp_path, monkeypatch, strict):
        if strict == "1":
            monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        schema = {
            "properties": {
                "kind": {
                    "enum": [
                        "session-handoff", "spinoff", "roadmap-baton", "goal-seed",
                        "roadmap-seed", "recovery",
                    ]
                }
            }
        }
        assert not advisory_guard._handoff_kind_off_enum_fires(
            "handoff", schema, {"kind": "spinoff-roadmap"}
        ), "a retired pre-rename alias must de-alias, not fire this branch"


class TestHandoffKindNonScalarCoercionRegression:
    """The reviewer's nit half of the backlog item: `raw_kind` was
    unconditionally coerced via `str(raw_kind)` before the enum
    comparison. A non-scalar `kind` (YAML list/mapping) must stand down in
    THIS branch on both guards — not be blindly stringified into a
    garbled off-enum message — leaving the base JSON-schema validation to
    name the real defect (wrong type for `kind`).
    """

    _ENUM_VALUES = [
        "session-handoff", "spinoff", "roadmap-baton", "goal-seed",
        "roadmap-seed", "recovery",
    ]
    _SCHEMA = {"properties": {"kind": {"enum": _ENUM_VALUES}}}

    def test_deny_stands_down_on_list_kind(self):
        message = deny_guard._evaluate_handoff_kind_enum(
            "handoff", self._SCHEMA, {"kind": ["session-handoff", "spinoff"]}
        )
        assert message is None, (
            "a non-scalar kind must not be str()-coerced and denied by this "
            "branch — it must stand down for the base schema type check"
        )

    def test_deny_stands_down_on_mapping_kind(self):
        message = deny_guard._evaluate_handoff_kind_enum(
            "handoff", self._SCHEMA, {"kind": {"nested": "mapping"}}
        )
        assert message is None

    def test_advisory_stands_down_on_list_kind(self):
        fires = advisory_guard._handoff_kind_off_enum_fires(
            "handoff", self._SCHEMA, {"kind": ["session-handoff", "spinoff"]}
        )
        assert fires is False

    def test_advisory_stands_down_on_mapping_kind(self):
        fires = advisory_guard._handoff_kind_off_enum_fires(
            "handoff", self._SCHEMA, {"kind": {"nested": "mapping"}}
        )
        assert fires is False

    @pytest.mark.parametrize("strict", ["0", "1"])
    def test_end_to_end_list_kind_never_produces_a_garbled_message(
        self, tmp_path, monkeypatch, strict
    ):
        """Full `check()` path: a YAML-list `kind` must never surface the
        Python `str(list)` rendering (e.g. "['session-handoff', 'spinoff']")
        in any guard's output — whatever fires instead (or nothing) must
        come from the ordinary schema-shape validator, not this branch's
        now-defunct blind coercion.
        """
        if strict == "1":
            monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = _write_handoff(tmp_path, "list-kind.md", "[session-handoff, spinoff]")
        payload = _payload("Edit", str(fp), str(tmp_path), old_string="body", new_string="body2")

        deny_result = deny_guard.check(payload)
        advisory_result = advisory_guard.check(payload)

        for result in (deny_result, advisory_result):
            if result is None:
                continue
            rendered = str(result)
            assert "['session-handoff'" not in rendered
            assert "not a recognized handoff kind" not in rendered
