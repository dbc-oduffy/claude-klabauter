"""SC-DR-016's condition of ratification, pinned in code.

`_cf_spinoff_roadmap_requires_graph` ships as a hard deny with no SC-DR-003
warn-first soak, cleared under SC-DR-016 (example-doctrine-repo
`coordinator/docs/wiki/scoped-safety-commits.md`) because its oracle is
self-contained. That clearance is CONDITIONAL: SC-DR-014's criterion (3) is
defined in terms of an override path, so the record requires a public override
env var, registered in the guard-override-keys reference table in the same
change that mints it. That pairing is the record's entire stated condition —
`test_registered_in_the_guard_override_keys_table` below is what discharges
it. A deny with no hatch does not qualify under SC-DR-016 at all.

These tests therefore assert three things a future edit could each break
independently — the deny still fires unaided (the hatch did not accidentally
become the default), the hatch actually suppresses it (it is a real escape
route, not a variable read and discarded), and the key is spelled exactly as
SC-DR-016 names it (a renamed key silently un-registers the guard from the
reference table an operator reads at decision time).

This file previously also asserted that the deny text itself had to NAME the
override key, reasoning that a hatch an operator cannot find from the deny
text discharges SC-DR-016 "on paper only." That was this file's own local
inference, not text in SC-DR-016 itself, and it is now retired: commit
`b1e1119f9` (ruling D5, `tasks/guard-messages-keys/DECISIONS.md`, peer
workstream "Guard messages stop handing agents the keys to their own cage")
deliberately dropped the `operator_override_note(...)` pointer from this
rule's deny leg so agents cannot self-unlock from the deny text. The hatch
itself is untouched and still fully enforced — by
`test_override_suppresses_the_deny`,
`test_key_is_spelled_exactly_as_scdr016_specifies`, and
`test_registered_in_the_guard_override_keys_table` — only the in-message
pointer is gone. See `docs/reference/guard-override-keys.md`, the
`schema_validate.py` consumer paragraph.

Negative-spec: none of these assert that an AGENT can take this route. The
override is a pre-launch operator knob — the deciding guard process is spawned
per event and reads its payload on stdin, so nothing inside a live session can
put the variable into its environment. `monkeypatch.setenv` here simulates the
operator's pre-launch environment, and is not a claim about in-session
reachability. See `docs/reference/guard-override-keys.md` § Security context.
"""

from __future__ import annotations

from coordinator_core.frontmatter.schema_validate import (
    _ROADMAP_GRAPH_OVERRIDE_ENV_VAR,
    _cf_spinoff_roadmap_requires_graph,
)

#: A roadmap baton missing every one of the five required graph fields.
_INCOMPLETE_BATON = {'kind': 'roadmap-baton'}


def test_key_is_spelled_exactly_as_scdr016_specifies():
    """SC-DR-016 names the key literally; the spelling is not ours to shorten.

    The table row in `docs/reference/guard-override-keys.md` and the operator's
    pre-launch `export` both key off this exact string.
    """
    assert _ROADMAP_GRAPH_OVERRIDE_ENV_VAR == 'COORDINATOR_OVERRIDE_ROADMAP_GRAPH_FIELDS'


def test_deny_fires_without_the_override(monkeypatch):
    monkeypatch.delenv(_ROADMAP_GRAPH_OVERRIDE_ENV_VAR, raising=False)

    err = _cf_spinoff_roadmap_requires_graph(_INCOMPLETE_BATON)

    assert err is not None
    assert err['error'] == 'required for kind=roadmap-baton'
    for field in ('roadmap_id', 'stub_id', 'wave', 'blocks', 'blocked_by'):
        assert field in err['field']


def test_deny_hint_is_usable_but_silent_on_the_override(monkeypatch):
    """Ruling D5 (`b1e1119f9`) retired the deny-text pointer on purpose.

    The hint still has to guide an operator to the fix — the missing-fields
    sentence and the SKILL.md pointer stay — but it must NOT name the override
    env var. That inversion is what stops a future edit from re-adding the
    pointer (via `operator_override_note` or otherwise) without reading D5.
    The hatch itself is unaffected — see `test_override_suppresses_the_deny`.
    """
    monkeypatch.delenv(_ROADMAP_GRAPH_OVERRIDE_ENV_VAR, raising=False)

    err = _cf_spinoff_roadmap_requires_graph(_INCOMPLETE_BATON)

    assert err is not None
    assert 'roadmap_id' in err['hint']
    assert 'SKILL.md' in err['hint']
    assert _ROADMAP_GRAPH_OVERRIDE_ENV_VAR not in err['hint']


def test_override_suppresses_the_deny(monkeypatch):
    monkeypatch.setenv(_ROADMAP_GRAPH_OVERRIDE_ENV_VAR, '1')

    assert _cf_spinoff_roadmap_requires_graph(_INCOMPLETE_BATON) is None


def test_override_is_flag_shaped_not_merely_present(monkeypatch):
    """`VAR=1`, matching every flag-shaped key in the override table.

    A truthiness read (`if os.environ.get(VAR)`) would make `VAR=0` — the
    spelling an operator reaches for to turn the hatch back OFF — silently
    enable it instead.
    """
    monkeypatch.setenv(_ROADMAP_GRAPH_OVERRIDE_ENV_VAR, '0')
    assert _cf_spinoff_roadmap_requires_graph(_INCOMPLETE_BATON) is not None

    monkeypatch.setenv(_ROADMAP_GRAPH_OVERRIDE_ENV_VAR, 'true')
    assert _cf_spinoff_roadmap_requires_graph(_INCOMPLETE_BATON) is not None


def test_override_does_not_widen_beyond_this_rule(monkeypatch):
    """The hatch suppresses the required-graph-fields deny and nothing else.

    A complete baton is unaffected either way, and a non-baton kind was never
    in this rule's scope — an override that started returning None for records
    the rule never judged would be indistinguishable from the rule being
    deleted.
    """
    complete = {
        'kind': 'roadmap-baton',
        'roadmap_id': 'rm-1',
        'stub_id': 'st-1',
        'wave': 1,
        'blocks': [],
        'blocked_by': [],
    }
    monkeypatch.setenv(_ROADMAP_GRAPH_OVERRIDE_ENV_VAR, '1')
    assert _cf_spinoff_roadmap_requires_graph(complete) is None
    monkeypatch.delenv(_ROADMAP_GRAPH_OVERRIDE_ENV_VAR, raising=False)
    assert _cf_spinoff_roadmap_requires_graph(complete) is None

    assert _cf_spinoff_roadmap_requires_graph({'kind': 'handoff'}) is None


def test_registered_in_the_guard_override_keys_table():
    """SC-DR-016 requires the table row IN THE SAME CHANGE as the deny.

    Reads the reference doc rather than trusting that whoever added the key
    also remembered the row — that pairing is the condition, so it is the thing
    worth asserting.
    """
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parents[2].parent
        / 'docs'
        / 'reference'
        / 'guard-override-keys.md'
    )
    text = doc.read_text(encoding='utf-8')

    assert f'| `{_ROADMAP_GRAPH_OVERRIDE_ENV_VAR}` |' in text
    assert '_cf_spinoff_roadmap_requires_graph' in text
