"""Pins the signoff_provenance readers, builder, countersign and APM admissibility."""
import pytest

from coordinator_core.ops import signoff_provenance as sp


def _apm():
    return sp.build_signoff("apm", "ruled: fine", "docs/decisions/x.md", "2026-10-09")


def test_build_pm_and_apm_shapes():
    assert sp.build_signoff("pm", "ship it", None, "2026-10-09") == {
        "source": "pm", "pm_quote": "ship it", "on": "2026-10-09"}
    assert _apm() == {"source": "apm", "apm_ruling": "ruled: fine",
                      "ruling_ref": "docs/decisions/x.md", "on": "2026-10-09"}


@pytest.mark.parametrize("args", [
    ("apm", "r", None, "2026-10-09"),
    ("apm", "r", "/abs/path.md", "2026-10-09"),
    ("apm", "r", "C:\\abs\\p.md", "2026-10-09"),  # abs-path-ok: refusal fixture
    ("apm", "r\nx", "ref", "2026-10-09"),
    ("apm", "r", "re\nf", "2026-10-09"),
    ("pm", "q", None, "2026\n"),
    ("pm", "  ", None, "2026-10-09"),
    ("ceo", "q", None, "2026-10-09"),
])
def test_build_refusals(args):
    with pytest.raises(ValueError):
        sp.build_signoff(*args)


def test_countersign_keeps_apm_in_history():
    pm = sp.countersign(_apm(), "I checked", "2026-10-10")
    assert pm["source"] == "pm" and pm["pm_quote"] == "I checked"
    assert pm["history"] == [_apm()]
    s = sp.read_row({"pm_approved": True, "signoff": pm})
    assert s.source == "pm" and s.history == (_apm(),) and not s.unrecorded


def test_countersign_refuses_pm_prior():
    with pytest.raises(ValueError):
        sp.countersign(sp.build_signoff("pm", "q", None, "2026-10-09"), "again", "2026-10-10")


def test_apm_admissible():
    assert sp.apm_admissible("approve the grouping")
    assert not sp.apm_admissible("ok to merge to main")
    assert not sp.apm_admissible("Publish the release")
    assert not sp.apm_admissible("ok to push to main")
    assert not sp.apm_admissible("force-push the branch")
    assert not sp.apm_admissible("")
    assert not sp.apm_admissible(None)


def test_read_row():
    assert sp.read_row({"pm_approved": False}) is None
    legacy = sp.read_row({"pm_approved": True})
    assert legacy.unrecorded and legacy.source is None
    s = sp.read_row({"pm_approved": True, "signoff": _apm()})
    assert (s.source, s.words, s.ruling_ref, s.surface) == ("apm", "ruled: fine", "docs/decisions/x.md", "row")


def test_read_grouping():
    assert sp.read_grouping({"status": "pending"}) is None
    assert sp.read_grouping({"status": "approved", "approver": "G-EM"}).unrecorded
    s = sp.read_grouping({"status": "approved", "signoff": sp.build_signoff("pm", "go", None, "2026-10-09")})
    assert s.source == "pm" and s.words == "go" and s.surface == "grouping"


def test_read_sizing_accepted():
    assert sp.read_sizing_accepted(None) is None
    assert sp.read_sizing_accepted({"source": "engine-size-rule"}) is None
    legacy_pm = sp.read_sizing_accepted({"pm_quote": "yes", "on": "2026-10-01"})
    assert legacy_pm.source == "pm" and legacy_pm.words == "yes" and not legacy_pm.unrecorded
    assert sp.read_sizing_accepted({"pm_quote": " "}).unrecorded
    apm = sp.read_sizing_accepted(_apm())
    assert apm.source == "apm" and apm.ruling_ref == "docs/decisions/x.md"
    both = sp.read_sizing_accepted(sp.countersign(_apm(), "mine", "2026-10-10"))
    assert both.source == "pm" and len(both.history) == 1


def test_read_exec_stamp():
    base = {"execution_authorized_note": "n", "execution_authorized_at": "2026-10-09"}
    assert sp.read_exec_stamp({}) is None
    assert sp.read_exec_stamp({**base, "execution_authorized_by": "engine-size-rule"}) is None
    assert sp.read_exec_stamp({**base, "execution_authorized_by": "PM"}).source == "pm"
    assert sp.read_exec_stamp({**base, "execution_authorized_by": "APM"}).source == "apm"
    d = sp.read_exec_stamp({**base, "execution_authorized_by": "G-EM standing delegation"})
    assert d.source == "apm" and d.words == "n" and d.on == "2026-10-09"
