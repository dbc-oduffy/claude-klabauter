"""On a example-retrieval-repo-served repo the grep-steering guards offer example-retrieval-repo, never a grep rewrite."""

import pytest

from coordinator_core.bash_guards import _rag_serving
from coordinator_core.bash_guards._dc_rewrites import check_grep_via_bash_rewrite
from coordinator_core.bash_guards._rewrite_support import _EXAMPLE_RETRIEVAL_REPO_HINT
from coordinator_core.bash_guards.rewrite_repo_root_search import check as repo_root_check


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _serve(monkeypatch, served):
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: served)


def _payload(cmd, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}


def _assert_rag_advisory(out):
    assert out is not None and out.verdict_kind == "advisory"
    assert out.updated_input is None
    assert _EXAMPLE_RETRIEVAL_REPO_HINT in out.em_text
    assert out.members[0].template.startswith("example-retrieval-repo project_semantic_search")


@pytest.mark.parametrize("cmd", ["grep -rn foo .", "rg foo", "grep -rn foo -- ."])
def test_repo_root_search_offers_rag_when_served(repo, monkeypatch, cmd):
    _serve(monkeypatch, True)
    _assert_rag_advisory(repo_root_check(_payload(cmd, repo)))


def test_repo_root_search_still_rewrites_when_unserved(repo, monkeypatch):
    _serve(monkeypatch, False)
    out = repo_root_check(_payload("grep -rn foo .", repo))
    assert out.verdict_kind == "rewrite"
    assert out.updated_input["command"].startswith("git grep")


@pytest.mark.parametrize("cmd", ["grep -n foo a.py", "grep -rn foo src"])
def test_grep_via_bash_offers_rag_when_served(repo, monkeypatch, cmd):
    _serve(monkeypatch, True)
    _assert_rag_advisory(check_grep_via_bash_rewrite(cmd, payload={"cwd": str(repo)}))


def test_grep_via_bash_still_rewrites_when_unserved(repo, monkeypatch):
    _serve(monkeypatch, False)
    out = check_grep_via_bash_rewrite("grep -n foo a.py", payload={"cwd": str(repo)})
    assert out is not None and out.verdict_kind == "rewrite"
    assert out.updated_input["command"]


def test_grep_via_bash_override_still_wins_when_served(repo, monkeypatch):
    _serve(monkeypatch, True)
    out = check_grep_via_bash_rewrite(
        "grep -n foo a.py", payload={"cwd": str(repo), "env": {"COORDINATOR_ALLOW_GREP_VIA_BASH": "1"}}
    )
    assert out is None
