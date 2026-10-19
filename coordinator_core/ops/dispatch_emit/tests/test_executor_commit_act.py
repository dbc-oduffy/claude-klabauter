"""executor_commit_act: body code spans that make an executor commit or deploy."""

import pytest

from coordinator_core.ops.dispatch_emit.em_only_act import executor_commit_act

SPANS = [
    "git commit -m x",
    "git -C repo -c user.name=a commit -m x",
    "git.exe commit -m x",
    "python -m coordinator_core.invoke ceremony.commit_v2 --params '{}'",
    "coordinator-safe-commit -m x",
    "firebase deploy --only hosting",
    "firebase.cmd deploy --only hosting",
    "tools\\firebase.exe deploy",
    "bin/firebase deploy",
    "gcloud app deploy",
    "pnpm deploy:hosting",
    "npm run deploy",
    "yarn deploy:prod",
    "bun run deploy",
]


def _row(body, **extra):
    return {"id": "C1", "writes": ["a.py"], "body": body, **extra}


@pytest.mark.parametrize("span", SPANS)
def test_inline_span_flags(span):
    assert executor_commit_act(_row(f"Then run `{span}` last.")) == span


@pytest.mark.parametrize("span", SPANS)
def test_fenced_span_flags(span):
    assert executor_commit_act(_row(f"Steps:\n```\n{span}\n```\n")) == span


@pytest.mark.parametrize("span", SPANS)
def test_performer_em_returns_none(span):
    assert executor_commit_act(_row(f"Run `{span}`.", performer="em")) is None


@pytest.mark.parametrize(
    "body",
    [
        "Commit the work and deploy it afterwards.",
        "Run `git log` then `git status`.",
        "Run `pnpm build` and `npm run test`.",
        "```\ngit diff --stat\n```",
        "",
    ],
)
def test_non_acts_return_none(body):
    assert executor_commit_act(_row(body)) is None


def test_non_string_body_returns_none():
    assert executor_commit_act({"id": "C1", "writes": ["a"], "body": None}) is None


def test_first_hit_wins():
    body = "Run `git status` then `git commit -m x` then `firebase deploy`."
    assert executor_commit_act(_row(body)) == "git commit -m x"
