"""Pins the report interface of `commit_leg_profile` that C2, C6 and C7 read.

The builder runs against a tiny tmp_path repo, never a clone of this repo.
"""
import subprocess

import pytest

from coordinator_core.benchmarks import commit_leg_profile as clp

pytestmark = pytest.mark.spawns_process

_KEYS = {"mode", "sign", "paths", "head", "committed", "sha", "parent_ms", "children_ms", "spawns", "phases"}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.fixture
def tiny_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "probe")
    _git(repo, "config", "user.email", "probe@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    for i in range(4):
        (repo / f"f{i}.md").write_text(f"v{i}\n", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_report_pins_keys_and_commit(tiny_repo):
    rep = clp.build_report(tiny_repo, "phased", 3, "off")
    assert _KEYS <= set(rep)
    assert rep["committed"] is True and rep["sha"]
    assert rep["mode"] == "phased" and rep["sign"] == "off" and rep["paths"] == 3
    assert rep["head"] != rep["sha"]
    assert rep["parent_ms"] >= 0 and rep["children_ms"] >= 0 and rep["spawns"] >= 0
    assert rep["phases"], "no phase was bracketed"
    for name, value in rep["phases"].items():
        assert isinstance(value, float) and value >= 0, name


def test_sign_off_writes_clone_local_config(tiny_repo):
    _git(tiny_repo, "config", "commit.gpgsign", "true")
    clp.build_report(tiny_repo, "phased", 1, "off")
    out = subprocess.run(["git", "-C", str(tiny_repo), "config", "--local", "commit.gpgsign"],
                         capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    assert out.strip() == "false"


def test_memoised_report_commits_and_names_label(tiny_repo):
    rep = clp.build_report(tiny_repo, "phased", 2, "off", memoise="commit._identity")
    assert rep["memoised"] == "commit._identity"
    assert rep["memoise_served"] >= 0
    assert rep["committed"] is True
    assert _KEYS <= set(rep)
    assert "caller_phases" not in rep


def test_memoise_refuses_non_allowlisted_label(tiny_repo, capsys):
    with pytest.raises(ValueError, match="allowlist"):
        clp.build_report(tiny_repo, "phased", 1, "off", memoise="commit.cas_ref")
    with pytest.raises(SystemExit) as exc:
        clp.main([str(tiny_repo), "phased", "--memoise", "commit.cas_ref"])
    assert exc.value.code == 2
    assert "commit._identity" in capsys.readouterr().err


def test_with_caller_brackets_both_callers(tiny_repo):
    rep = clp.build_report(tiny_repo, "phased", 2, "off", with_caller=True)
    assert _KEYS <= set(rep) and rep["committed"] is True
    assert set(rep["caller_phases"]) == {"caller.compose_message", "caller.apply_missing_trailers"}
    for v in rep["caller_phases"].values():
        assert isinstance(v, float) and v >= 0
    assert not any(k.startswith("caller.") for k in rep["phases"])
    assert "memoised" not in rep


def test_sub_phases_added_without_changing_top_level_phases(tiny_repo):
    rep = clp.build_report(tiny_repo, "phased", 2, "off", sub_phases=True)
    assert rep["committed"] is True and _KEYS <= set(rep)
    assert set(rep["sub_phases"]) == set(rep["sub_counts"])
    for label in ("git_objects._read_pack_object_at", "git_state._parse_tree_entries",
                  "import.coordinator_core.session.core",
                  "import.coordinator_core.git.commit_trailers"):
        assert label in rep["sub_phases"], label
    for v in rep["sub_phases"].values():
        assert isinstance(v, float) and v >= 0
    for v in rep["sub_counts"].values():
        assert isinstance(v, int) and v >= 0
    assert not any(k in rep["phases"] for k in rep["sub_phases"])
    plain = clp.build_report(tiny_repo, "phased", 2, "off")
    assert set(plain["phases"]) == set(rep["phases"])


def test_sub_phase_keys_absent_without_flag(tiny_repo):
    rep = clp.build_report(tiny_repo, "phased", 1, "off")
    assert "sub_phases" not in rep and "sub_counts" not in rep


def test_outermost_only_bracket_counts_recursion_once():
    import types

    subs = clp._SubPhases()
    mod = types.SimpleNamespace(__name__="pkg.m")
    mod.f = lambda n: 0 if n == 0 else mod.f(n - 1)
    subs._wrap(mod, "f")
    assert mod.f(3) == 0
    assert subs.counts["m.f"] == 1
    subs.restore()
    assert not getattr(mod.f, "_sub_phase_wrapped", False)


def test_commits_two_yields_two_distinct_commits(tiny_repo):
    rep = clp.build_report(tiny_repo, "phased", 2, "off", commits=2)
    assert len(rep["per_commit"]) == 2
    assert all(e["committed"] is True for e in rep["per_commit"])
    assert len({e["sha"] for e in rep["per_commit"]}) == 2
    assert rep["sha"] == rep["per_commit"][0]["sha"]
    assert rep["parent_ms"] == rep["per_commit"][0]["parent_ms"]
    for e in rep["per_commit"]:
        assert {"parent_ms", "children_ms", "spawns", "phases"} <= set(e)


def test_commits_two_with_memoise_does_not_serve_stale_head(tiny_repo):
    rep = clp.build_report(tiny_repo, "phased", 2, "off", memoise="commit.head_sha", commits=2)
    assert all(e["committed"] is True for e in rep["per_commit"])


def test_rejects_zero_commits(tiny_repo):
    with pytest.raises(ValueError):
        clp.build_report(tiny_repo, "phased", 1, "off", commits=0)


def test_rejects_unknown_arms(tiny_repo):
    with pytest.raises(ValueError):
        clp.build_report(tiny_repo, "bogus", 1, "off")
    with pytest.raises(ValueError):
        clp.build_report(tiny_repo, "phased", 1, "bogus")
