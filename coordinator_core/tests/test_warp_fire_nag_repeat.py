"""nag_text: full fire_step on a session's first nag, terse counted line after, never empty."""

from coordinator_core import warp_fire


def test_first_full_then_terse_counted(tmp_path):
    first = warp_fire.nag_text("s1", state_dir=tmp_path)
    assert first == warp_fire.fire_step()
    second = warp_fire.nag_text("s1", state_dir=tmp_path)
    third = warp_fire.nag_text("s1", state_dir=tmp_path)
    assert second.startswith("[engine-ask nag #2]")
    assert third.startswith("[engine-ask nag #3]")
    assert "\n" not in second and len(second) < len(first)
    assert warp_fire.fire_command() in second


def test_sessions_independent_and_sizing_carried(tmp_path):
    warp_fire.nag_text("a", state_dir=tmp_path)
    assert warp_fire.nag_text("b", "state/sizings/x.yaml", state_dir=tmp_path) == warp_fire.fire_step(
        "state/sizings/x.yaml"
    )
    assert "state/sizings/x.yaml" in warp_fire.nag_text("b", "state/sizings/x.yaml", state_dir=tmp_path)


def test_unwritable_store_still_returns_text(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    assert warp_fire.nag_text("s", state_dir=blocker / "sub") == warp_fire.fire_step()
