"""coordinator_core.ops.tests.test_warm_allowlist_separation -- chunk C13
(state/dispatch-briefs/2026-09-01-the-dogfooded-install-stops-lying-about/
C13.md): `warm_entrypoint_allowlist.json` used to be read for two unrelated
gates through the SAME `entrypoints` key -- `invoke_from_argv.py`'s
warm-load allowlist (fail-closed: absence refuses) and `substrate.py`'s
door-eligibility cutover list (best-effort: absence degrades harmlessly).
A dogfooder who wanted to disable the broken door cutover could only do so
by emptying `entrypoints`, which also disabled the unrelated, working
warm-load gate. This split each gate onto its own key: `entrypoints` stays
the warm-load allowlist (unchanged reader, unchanged semantics);
`door_eligible_entrypoints` is the new, independently-editable door key.

PRIOR ART, and it changes how this suite verifies:
state/lessons/2026-08-31-retiring-a-key-turns-its-absence-assertions-
green.yaml (item 4) documents a prior incident on THIS FILE where retiring
a key turned its absence-assertions green -- the tests passed because the
thing they asserted about had stopped existing. A green suite is therefore
not evidence here.

So this suite (a) greps for the literal `entrypoints` key still being
present and read by the warm-load path after the split, and (b) asserts
the warm-load gate still REFUSES an entrypoint absent from its list by
actually exercising the refusal (`_invoke_from_argv` raising
`ValueError`/`EntrypointNotWarmLoadableError`), never by asserting a key's
absence or presence alone.

Negative-spec: does not assert anything about `door_eligible_entrypoints`
being non-empty or about `substrate.py`'s cutover behaviour end-to-end --
that surface already has coverage in
`coordinator_core/install/tests/test_forwarder_routes_through_door.py`
(`test_door_eligible_forwarder_names_reads_the_committed_allowlist` and its
missing-allowlist sibling). This suite is scoped to the SEPARATION itself:
that the two gates now read distinct keys and that neither key's removal
silently satisfies an assertion meant for the other.

Spec backlink: state/dispatch-briefs/2026-09-01-the-dogfooded-install-
    stops-lying-about/C13.md
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.ops.invoke_from_argv as ifa
from coordinator_core.ops.invoke_from_argv import _ALLOWLIST_PATH, _invoke_from_argv
from coordinator_core.install import substrate

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_committed_allowlist_carries_both_keys_as_distinct_lists():
    """Grep-shaped: the literal keys both exist on the committed file after
    the split, each a list of strings. This is the assertion the prior
    incident's lesson calls for -- a key's PRESENCE checked directly,
    rather than inferring separation from a passing gate test that could
    pass for the wrong reason (the key never existing at all)."""
    data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    assert "entrypoints" in data, (
        "'entrypoints' must remain on the committed allowlist -- it is "
        "invoke_from_argv.py's warm-load gate and its removal here would "
        "be indistinguishable, from this test alone, from the gate having "
        "been correctly separated"
    )
    assert "door_eligible_entrypoints" in data, (
        "'door_eligible_entrypoints' is the new, independently-editable "
        "door-cutover key substrate.py now reads instead of 'entrypoints'"
    )
    assert isinstance(data["entrypoints"], list)
    assert all(isinstance(n, str) and n for n in data["entrypoints"])
    assert isinstance(data["door_eligible_entrypoints"], list)
    assert all(isinstance(n, str) and n for n in data["door_eligible_entrypoints"])


def test_warm_load_gate_still_refuses_an_absent_entrypoint_by_exercising_it(monkeypatch):
    """Not a key-absence assertion -- an actual call through
    `_invoke_from_argv` with an entrypoint that is not on the (patched,
    empty) warm-load allowlist, asserting the refusal fires. This is the
    exact shape the prior-incident lesson demands: prove the refusal by
    exercising it, since a retired key would make an absence-only
    assertion pass for the wrong reason."""
    real_bin = _PROJECT_ROOT / "coordinator" / "bin" / "cross-repo-memo.py"
    assert real_bin.is_file(), "setup error: real proving CLI must exist"

    monkeypatch.setattr(ifa, "_WARM_ENTRYPOINT_ALLOWLIST", frozenset())

    with pytest.raises(ValueError, match="allowlist") as excinfo:
        _invoke_from_argv({
            "argv": ["list"],
            "cwd": str(_PROJECT_ROOT),
            "entrypoint": "cross-repo-memo",
        })
    assert "cross-repo-memo" in str(excinfo.value)


def test_warm_load_gate_reads_entrypoints_key_not_door_eligible_key(monkeypatch, tmp_path):
    """Point `_ALLOWLIST_PATH` at a fixture where `entrypoints` is empty but
    `door_eligible_entrypoints` carries the name under test -- if the
    warm-load loader were still (or again) reading the door key, this name
    would resolve. It must not: the warm-load gate reads ONLY
    `entrypoints`, independent of `door_eligible_entrypoints`."""
    fixture = tmp_path / "warm_entrypoint_allowlist.json"
    fixture.write_text(
        json.dumps({
            "entrypoints": [],
            "door_eligible_entrypoints": ["cross-repo-memo"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(ifa, "_ALLOWLIST_PATH", fixture)
    reloaded = ifa._load_allowlist()
    assert "cross-repo-memo" not in reloaded, (
        "the warm-load loader must read 'entrypoints' only -- a name "
        "present solely under 'door_eligible_entrypoints' must not "
        "warm-load"
    )


def test_door_cutover_reads_door_eligible_key_not_entrypoints_key(monkeypatch, tmp_path):
    """Mirror of the previous test from substrate.py's side: point
    `_DOOR_ELIGIBLE_ALLOWLIST_PATH` at a fixture where `entrypoints` carries
    a name but `door_eligible_entrypoints` does not -- the door cutover
    must NOT pick it up. Proves the split is real in both directions, not
    just that the door key exists."""
    fixture = tmp_path / "warm_entrypoint_allowlist.json"
    fixture.write_text(
        json.dumps({
            "entrypoints": ["cross-repo-memo"],
            "door_eligible_entrypoints": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(substrate, "_DOOR_ELIGIBLE_ALLOWLIST_PATH", fixture)
    names = substrate._door_eligible_forwarder_names()
    assert "cross-repo-memo" not in names, (
        "the door cutover must read 'door_eligible_entrypoints' only -- a "
        "name present solely under 'entrypoints' must not be treated as "
        "door-eligible"
    )


def test_clearing_door_eligible_key_alone_does_not_touch_warm_load_gate(monkeypatch, tmp_path):
    """The concrete scenario the ledger item names: a dogfooder disables the
    door cutover by clearing 'door_eligible_entrypoints' alone, and the
    warm-load gate for an unrelated, working name is unaffected."""
    fixture = tmp_path / "warm_entrypoint_allowlist.json"
    fixture.write_text(
        json.dumps({
            "entrypoints": ["cross-repo-memo"],
            "door_eligible_entrypoints": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(ifa, "_ALLOWLIST_PATH", fixture)
    monkeypatch.setattr(substrate, "_DOOR_ELIGIBLE_ALLOWLIST_PATH", fixture)

    assert substrate._door_eligible_forwarder_names() == frozenset(), (
        "door cutover must be disabled when its own key is cleared"
    )
    assert "cross-repo-memo" in ifa._load_allowlist(), (
        "clearing the door key alone must not disable the unrelated, "
        "working warm-load gate for a name still on 'entrypoints'"
    )
