"""`ceremony.commit_v2` refuses a `repo` param instead of ignoring it.

The op has no `repo` parameter: its target worktree is keyed from the caller
(`_OP_KEY_SCOPE = "common_dir"`), and `repo_root` is a D3 consistency assertion,
never the resolution source. Before this refusal an unrecognised `repo` key was
accepted silently, so a call naming one repo committed against another.

Measured live: a dispatched committer on a DoE-claude wave passed a `repo` key
naming DoE-claude and the op resolved its pathspec under claude-klabauter, the
session's own tree, reporting `cannot read
state/2026-09-10-completion-entry-post-quote-oracle.md: [Errno 2] No such file
or directory` against a path that exists in the repo it named. The
committer read that as broken infrastructure and halted the wave; the EM adopted
the file by hand (`592e3da61a`).

A silent accept is the failure here, not the keying. An op that ignores a param
which looks like it selects the target hands the caller a wrong answer with the
shape of a right one.
"""

from __future__ import annotations

from coordinator_core.ops.ceremony import commit_v2


def _call(**extra):
    params = {
        "paths": ["README.md"],
        "message": "irrelevant -- the refusal fires before any commit work",
    }
    params.update(extra)
    return commit_v2._handler(params)


def test_a_repo_param_is_refused_by_name():
    out = _call(repo="../some-other-repo")
    assert "error" in out or out.get("committed") is not True, (
        "a `repo` param must not reach commit work"
    )
    text = str(out)
    assert "params.repo" in text, (
        "the refusal does not name the offending param, so a caller cannot act on it"
    )


def test_the_refusal_says_where_the_repo_actually_comes_from():
    text = str(_call(repo="../some-other-repo"))
    assert "common_dir" in text, (
        "the refusal names no alternative -- a caller told only that its param is "
        "wrong will try another spelling of the same param"
    )
    assert "repo_root" in text, (
        "the refusal does not distinguish `repo` from the `repo_root` assertion "
        "param, which is the confusion that produced it"
    )


def test_no_repo_param_still_reaches_validation():
    out = commit_v2._handler({"paths": [], "deleted_paths": [], "message": "m"})
    assert "params.repo" not in str(out), (
        "the repo refusal fires on a call that never named a repo"
    )
