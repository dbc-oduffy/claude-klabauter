"""stated_parts: lettered-part parsing and dropped-part detection over a delivery note."""

from coordinator_core.ops.dispatch_emit.stated_parts import dropped_parts, lettered_parts

_ASK = "Do (a) the objective-reacher gating fix and (b) remnant completion."
_NOTE = (
    "Notes N2/N3 are EM framing. The judge fires in 2 of 51 cells gating question in N1(a)/N2 "
    "is not part of the sizing's exit criterion and was not investigated in this run."
)


def test_lettered_parts_parses_spans():
    parts = lettered_parts(_ASK)
    assert [p.label for p in parts] == ["a", "b"]
    assert parts[0].text == "the objective-reacher gating fix"
    assert parts[1].text == "remnant completion."


def test_lettered_parts_ignores_attached_refs_and_unanchored_runs():
    assert lettered_parts("see N1(a) and N2(b)") == ()
    assert lettered_parts("(b) alone") == ()


def test_dropped_part_found_from_real_note_shape():
    got = dropped_parts(_NOTE, ruled=())
    assert [d.label for d in got] == ["a"]
    assert "not part of" in got[0].phrase


def test_ruled_label_is_excluded():
    assert dropped_parts(_NOTE, ruled=["a"]) == ()
    assert dropped_parts(_NOTE, ruled=["A"]) == ()


def test_delivered_part_not_flagged():
    assert dropped_parts("(a) fix landed. (b) remnant completion done.") == ()


def test_lettered_span_owns_its_phrase():
    got = dropped_parts("(a) gating fix is out of scope. (b) remnant completion done.")
    assert [(d.label, d.phrase) for d in got] == [("a", "out of scope")]
    assert got[0].text == "gating fix is out of scope."


def test_ledger_row_id_flagged():
    got = dropped_parts("R2 was not done.")
    assert [d.label for d in got] == ["R2"]
    assert dropped_parts("R2 was not done.", ruled=["r2"]) == ()


def test_empty_inputs():
    assert lettered_parts("") == ()
    assert dropped_parts("") == ()
