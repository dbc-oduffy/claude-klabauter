"""The folded ledger charter does not charge out-of-footprint dirt the run did not write (run 16cd3b)."""
from coordinator_core.ops.dispatch_emit import ask_compose


def _charter(relayed: bool = True) -> str:
    return ask_compose._folded_ledger_charter(
        relayed=relayed, ledger_rel="l.md", record_dir="r", run_dir="d", in_run_commit=False
    )


def test_out_of_footprint_dirt_the_run_did_not_write_is_an_observation_not_not_met():
    charter = _charter()
    for anchor in ("outside the manifest's write footprint", "never makes a row not_met", "as an observation"):
        assert anchor in charter


def test_dirt_the_run_wrote_or_inside_the_footprint_keeps_its_judgement():
    assert "Dirt the run wrote, or inside the footprint, keeps its judgement." in _charter()


def test_pre_existing_dirt_rule_still_holds_beside_the_new_clause():
    charter = _charter(relayed=False)
    assert "write_baseline shows unchanged since stage was not delivered by this run" in charter
    assert charter.index("pre-existing dirt never meets a row") < charter.index("never makes a row not_met")


def test_no_revert_or_clean_verb_is_offered_for_outside_paths():
    charter = _charter().lower()
    assert "revert" not in charter
    assert "git checkout" not in charter
