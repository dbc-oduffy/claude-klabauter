"""executor_merge_act: body code spans that make an executor run a git merge the guard denies."""

import pytest

from coordinator_core.ops.dispatch_emit.em_only_act import executor_merge_act

MERGES = [
    "git merge --no-ff feature",
    "git merge feature",
    "git -C repo merge --no-ff feature",
    "git -c user.name=a merge feature",
    "git.exe merge feature",
    "tools\\git.exe merge feature",
    "git fetch && git merge origin/main",
    "git merge -m 'prefer --ff-only merges' feature",
    "git --namespace ns merge feature",
    "FOO=1 git merge feature",
    "env GIT_X=1 git merge feature",
    "env git merge --no-ff feature",
    "command git merge feature",
    "time git merge feature",
]


def _row(body, **extra):
    return {"id": "C1", "writes": ["a.py"], "body": body, **extra}


@pytest.mark.parametrize("span", MERGES)
def test_inline_span_flags(span):
    assert executor_merge_act(_row(f"Then run `{span}` last.")) == span


@pytest.mark.parametrize("span", MERGES)
def test_fenced_span_flags(span):
    assert executor_merge_act(_row(f"Steps:\n```\n{span}\n```\n")) == span


@pytest.mark.parametrize("span", MERGES)
def test_performer_em_without_code_write_returns_none(span):
    row = _row(f"Run `{span}`.", performer="em", writes=["notes.md"])
    assert executor_merge_act(row) is None


@pytest.mark.parametrize(
    "body",
    [
        "Run `git merge --ff-only feature`.",
        "Run `git -C repo merge --ff-only feature`.",
        "Merge the branch afterwards.",
        "Run `git log` then `git status`.",
        "Run `git merge-base HEAD main`.",
        "Run `git commit -m 'merge work'`.",
        "Run `echo git merge feature`.",
        "Run `git --namespace ns merge --ff-only feature`.",
        "",
    ],
)
def test_non_acts_return_none(body):
    assert executor_merge_act(_row(body)) is None


def test_non_string_body_returns_none():
    assert executor_merge_act({"id": "C1", "writes": ["a"], "body": None}) is None


def test_first_hit_wins():
    body = "Run `git status` then `git merge a` then `git merge b`."
    assert executor_merge_act(_row(body)) == "git merge a"
