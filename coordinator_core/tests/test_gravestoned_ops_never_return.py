"""An op whose ledger entry says it never returns does not come back.

`CLAUDE.md` § The brightline rules that kill means kill forever, and
`state/kill-ledger.md` records each cut. The ledger is prose: until this file
existed, nothing failed when a killed op id was registered again. A
re-registration is five one-line table edits and the op then works — it
dispatches, its tests pass — so the only signal it had ever been convicted was
a ledger section nobody opens during the edit.

THE ROSTER IS DERIVED, and derived from `Returns-when`, not from `Status`.
Status is the wrong key twice over: its vocabulary is free prose (45 distinct
strings across 73 entries), and 19 of the 38 entries reading dead are SUSPENDED
— refused by a table that a PM ruling may lift, which is the opposite of never.
`Returns-when: never…` is the sentence that actually means never, and the eight
entries carrying it are this file's roster. Hand-maintaining the list instead
was tried first and rejected on measurement: it caught one of the eight.

THE FLOOR IS THE POINT. A regex over a doc ~50 sessions edit stops matching the
day someone rewords "Never in this shape", and a guard that silently derives an
empty roster is green forever — the failure mode this file exists to end,
reproduced one layer up. `_DERIVED_FLOOR` fails that reword instead of
absorbing it, the ratchet shape `test_op_suspension_ratchet.py` already uses.
Lowering it needs the same thing raising the suspension bar needs: an argument.

Negative-spec:
  - Does NOT assert the requirement the op served is unserved. A gravestone
    forbids the SHAPE, not the job — `queue.age_ping`'s requirement is
    discharged today by `orientation/expired_grant_signal.py`, which is correct
    and must not read here as a violation.
  - Does NOT read `Status`. An entry may say CUT and be legitimately rebuilt
    (`memo.send`, `fleet.archive_actioned_memos`, both live again and both
    saying so); those carry no never-Returns-when and never enter the roster.
  - Does NOT measure anything. The conviction is in the ledger; this file pins
    the outcome, not the evidence.
  - Does NOT treat suspension as sufficient. A suspended op refuses today and
    may be reinstated tomorrow by an edit to a table; an entry saying never is
    asserted against RESOLUTION, so lifting one goes red here as well.

REVERSING A KILL means editing that entry's Returns-when with a PM ruling
behind it. It does not mean editing this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


#: Entries whose `Returns-when` began with "never" when this guard was written.
#: A drop below this is a regex that stopped matching, not a kill that was
#: reversed — reversing one lowers this line and says which entry changed.
_DERIVED_FLOOR = 8

#: An op key, as opposed to the module filenames some headings carry: dotted and
#: lowercase, with the trailing segment not a source extension. `coverage.py`
#: (K-065) satisfies the dotted-lowercase shape and is a FILE — without this
#: exclusion it collects as an op row that can never resolve, i.e. a permanently
#: green test asserting nothing.
_SOURCE_EXTENSIONS = ("py", "md", "json", "yaml", "yml", "sh", "mjs")
_OP_KEY_RE = re.compile(
    r"^[a-z_]+\.(?!(?:" + "|".join(_SOURCE_EXTENSIONS) + r")$)[a-z_]+$"
)

_RETURNS_WHEN_RE = re.compile(r"\*\*Returns-when\.?\*\*\s*(.{0,40})", re.S)


def _ledger_path() -> Path:
    return Path(__file__).resolve().parents[2] / "state" / "kill-ledger.md"


#: The ledger is source-only -- `state/` is not part of the published mirror
#: payload, so this module cannot assert anything there. `parametrize` reads the
#: ledger at COLLECTION time, so an absent file is a collection ERROR that fails
#: the whole tier rather than one test: it took the assembled-mirror gate down on
#: every publish, which is what fail-closed looks like when the payload is fine
#: and the test is not. Skip visibly instead -- never return an empty roster,
#: which would read as green.
if not _ledger_path().is_file():
    pytest.skip(
        "state/kill-ledger.md is absent -- source-only, not in the published "
        "mirror payload; nothing here is assertable against this tree",
        allow_module_level=True,
    )


def _never_returns() -> list[tuple[str, str]]:
    """[(ledger section, op key)] for every entry whose Returns-when says never.

    Headings that name no op key — a family sweep (`K-103..K-115`), an entry
    about an output rather than an op (`K-056`) — carry a real never and no
    assertable subject, so they raise the floor without producing a row.
    """
    text = _ledger_path().read_text(encoding="utf-8")
    rows: list[tuple[str, str]] = []
    for section in re.split(r"^## (?=K-\d+)", text, flags=re.M)[1:]:
        heading = section.split("\n", 1)[0]
        matched = _RETURNS_WHEN_RE.search(section)
        if not (matched and matched.group(1).lstrip().lower().startswith("never")):
            continue
        for candidate in re.findall(r"`([^`]+)`", heading):
            if _OP_KEY_RE.match(candidate):
                rows.append((section.split(" ", 1)[0].strip(), candidate))
                break
    return rows


def test_the_derivation_still_matches_the_ledger() -> None:
    text = _ledger_path().read_text(encoding="utf-8")
    found = sum(
        1
        for section in re.split(r"^## (?=K-\d+)", text, flags=re.M)[1:]
        if (m := _RETURNS_WHEN_RE.search(section))
        and m.group(1).lstrip().lower().startswith("never")
    )
    assert found >= _DERIVED_FLOOR, (
        f"only {found} kill-ledger entries parse as Returns-when: never, against a "
        f"floor of {_DERIVED_FLOOR}. Either a kill was reversed — lower the floor and "
        f"name the entry — or an entry was reworded past this file's regex, which "
        f"silently empties the roster below."
    )


@pytest.mark.parametrize("section,op_key", _never_returns(), ids=lambda v: str(v))
def test_an_op_that_never_returns_is_not_dispatchable(section: str, op_key: str) -> None:
    #: Resolution, never a `_REGISTRY` membership read. Ops register lazily, so
    #: `key not in _REGISTRY` is true of every op nobody has imported yet — the
    #: assertion passes for a LIVE op and ships as an arm that cannot go red.
    #: `get_op_handler` runs the same lazy-import fallback dispatch runs.
    from coordinator_core.ipc import get_op_handler
    from coordinator_core.op_budget_suspension import OpSuspendedError

    try:
        handler = get_op_handler(op_key)
    except OpSuspendedError:
        #: Refused by the suspension table. That is not this entry's contract —
        #: suspension is liftable and the ledger says never — but the op is
        #: unreachable today, so the assertion below is what would catch a lift.
        return
    assert handler is None, (
        f"{op_key} resolves to a handler, and {section}'s Returns-when says it never "
        f"comes back. Reverse the kill in the ledger with a PM ruling behind it, or "
        f"drop the registration."
    )
