"""An accepted revise note reaches every leg as binding EM context, live and on a refire."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_notes
from coordinator_core.ops.dispatch_emit.tests.test_refire_carries_notes_every_mint_path import (  # noqa: F401
    _executor_prompt,
    _seed_notes,
    _spy_texts,
    GONE,
    LIVE_A,
    LIVE_B,
)
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    ASK,
    BASE,
    _fire,
    _halt,
    composed,
    repo,
)

LEDGER_OPEN = "Requirements ledger: read "
UNFRAMED = ("ledger grades", "unconfirmed (session task list only)")


def _ledger_legs(text):
    """The text from each ledger clause to the next one: what a planner, reviewer or judge leg reads."""
    at = [i for i in range(len(text)) if text.startswith(LEDGER_OPEN, i)]
    return [text[a:b] for a, b in zip(at, at[1:] + [len(text)])]


def test_every_ledger_reading_leg_carries_the_note_as_binding_em_context(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed_notes(repo, ASK)
    _fire(repo)
    text = seen["script"] + "\n".join((seen["kw"].get("staged_out") or {}).values())
    legs = _ledger_legs(text.replace("\\'", "'"))
    assert len(legs) >= 4
    for leg in legs:
        assert run_notes.HEADING in leg
        assert f"> {LIVE_A}" in leg and f"> {LIVE_B}" in leg
        assert f"> {GONE}" not in leg
        assert not any(u in leg for u in UNFRAMED)
    assert run_notes.HEADING in _executor_prompt(seen["script"])


def test_a_note_accepted_after_compose_reaches_the_view_the_legs_read(repo, composed):  # noqa: F811
    run_id = "ask-20261106T000000-live00"
    rl.start(repo, run_id)
    rl.revise(repo, run_id, note="no camera-fed scale")
    view = (rl.run_dir(repo, run_id) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert run_notes.VIEW_HEADING in view and "> no camera-fed scale" in view
    assert not any(u in view for u in UNFRAMED)


def test_a_retracted_note_still_reads_as_not_to_be_acted_on(repo, composed):  # noqa: F811
    run_id = "ask-20261106T000000-live01"
    rl.start(repo, run_id)
    rl.revise(repo, run_id, note="drop this")
    rl.revise(repo, run_id, retract_note={"note_id": "N1", "reason": "applied by hand"})
    view = (rl.run_dir(repo, run_id) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert "> drop this" not in view
    assert "was retracted (applied by hand)" in view and "not to be acted on" in view
    assert json.loads((rl.run_dir(repo, run_id) / rl.NOTES_FILE).read_text().splitlines()[0])["note"] == "drop this"
