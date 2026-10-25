from coordinator_core.git import machine_local_paths as mlp


def test_prefix_match_and_legacy_root_non_match():
    paths = [".coordinator-local/subagent-share/s1/x.jsonl", "state/subagent-share/a", "src/a.py"]
    assert mlp.machine_local_paths(paths) == [".coordinator-local/subagent-share/s1/x.jsonl"]


def test_backslash_and_mixed_case_match():
    paths = [".Coordinator-Local\\Subagent-Share\\s\\x", "a/.coordinator-local/subagent-share/x"]
    assert mlp.machine_local_paths(paths) == [paths[0]]


def test_refusal_names_paths_and_caps():
    assert mlp.machine_local_refusal(["src/a.py"]) is None
    paths = [f".coordinator-local/subagent-share/{i}" for i in range(8)]
    msg = mlp.machine_local_refusal(paths)
    assert ".coordinator-local/subagent-share/0" in msg
    assert "8" in msg and "+3 more" in msg
    assert "git rm -r --cached .coordinator-local/subagent-share" in msg


def test_pathspec_is_icase_prefix():
    assert mlp.MACHINE_LOCAL_PATHSPEC == ":(icase).coordinator-local/subagent-share/"
