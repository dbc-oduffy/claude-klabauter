"""The real-shape cold ask_gate benchmark names the dominant wait and refuses an unresolvable settings home."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import cold_ask_gate_real_shape_waits as bench


def test_dominant_wait_is_the_largest_and_ties_keep_list_order():
    waits = dict.fromkeys(bench.WAITS, 1.0)
    waits["child_idle_wait_ms"] = 9.0
    assert bench.dominant_wait(waits) == "child_idle_wait_ms"
    assert bench.dominant_wait(dict.fromkeys(bench.WAITS, 3.0)) == bench.WAITS[0]


def test_attempts_are_read_from_a_hung_error_and_from_a_completed_envelope():
    rec = [{"phase": "backstop", "wall_ms": 30000.0}, {"phase": "await_ceiling", "wall_ms": 120000.0}]
    hung = {"error": {"data": {"diagnosis": {"attempts": rec}}}}
    done = {"result": {}, "data": {"diagnosis": {"attempts": rec[:1]}}}
    assert bench.attempts_wall_ms(hung) == (150000.0, rec)
    assert bench.attempts_wall_ms(done) == (30000.0, rec[:1])
    assert bench.attempts_wall_ms({"result": {}}) == (0.0, [])


def test_an_unresolvable_settings_home_is_a_typed_refusal_naming_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv(bench.SETTINGS_HOME_KEY, str(tmp_path / "absent"))
    with pytest.raises(bench.SettingsHomeKeyMissing, match=bench.SETTINGS_HOME_KEY):
        bench.resolve_settings_home()


def test_a_tree_without_git_is_refused(tmp_path):
    with pytest.raises(bench.RepoRootMissing):
        bench.resolve_repo_root(tmp_path)


@pytest.mark.cadence
def test_measure_bills_every_wait_and_names_one_as_dominant(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv(bench.SETTINGS_HOME_KEY, str(home))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = bench.measure(bench.resolve_repo_root(repo))
    assert set(report["waits"]) == set(bench.WAITS)
    assert all(v >= 0.0 for v in report["waits"].values())
    assert report["dominant_wait"] in bench.WAITS
    assert report["waits"][report["dominant_wait"]] == max(report["waits"].values())
    assert report["child_procs"] >= 1 and report["child_rc"] == 0, report
    assert report["settings_home"] in (str(home.resolve()), str(home))
    assert isinstance(report["engine_stamped"], bool)
