"""
coordinator_core.ops.dispatch_emit.cloud_spawn_brief — the cloud-spawn brief emitter.

Purpose: bind a `create_session` argument block for a cloud child session from a fixed
template, so a cloud EM never hand-types the brief. A hand-typed brief drifts into asking the
child for side-channel writes, and the auto-mode classifier then refuses the whole spawn
(tripwire A-SPAWN-BRIEF-THAT-ASKS-FOR-SIDE-CHANNELS-IS-REFUSED-WHOLE).

Invariants:
  - A brief carries reads and exactly one write: a single comment on the parent's channel PR.
  - No template and no caller-supplied question may name a trigger, artifact, publish, push,
    branch, commit, scheduling or repo-attach verb. `refuse_side_channel_words` is the one gate,
    run over the finished brief, so a template edit or a hostile `question` fails the same way.
  - The block never carries `environment_id`; the child inherits the parent's environment.

Pure module: no I/O, no clock, no environment reads.
"""

from __future__ import annotations

import re
from typing import Any

KINDS = ("probe", "worker")

# Each entry is a stem: matched at a word start, so "pushed"/"triggers"/"branches" all trip.
FORBIDDEN_STEMS = (
    "trigger",
    "routine",
    "artifact",
    "publish",
    "push",
    "branch",
    "commit",
    "schedul",
    "send_later",
    "add_repo",
    "watch_url",
    "webhook",
    "fork",
)
_FORBIDDEN_RE = re.compile(r"\b(?:" + "|".join(FORBIDDEN_STEMS) + r")", re.IGNORECASE)

# The one write a brief may ask for; `count_report_writes` counts exactly this marker.
REPORT_WRITE_MARKER = "Your one write:"

_COMMON_READS = (
    "shell probes that only read",
    "ToolSearch",
    "get_session",
    "ListAgents",
    "read_documentation",
    "Read, Grep and Glob over the checkout",
)

_PROBE_TEMPLATE = """\
You are a probe session spawned by parent session {parent}. One question: {question}

Reads you may use: {reads}.
Test each capability the question names by calling it once. Mark anything you did not call \
UNTESTED and stop there.

{marker} a single comment on pull request #{pr} of {repo}, stating what worked, what was \
refused, and what is UNTESTED.

Do nothing else that changes state.
"""

_WORKER_TEMPLATE = """\
You are a worker session spawned by parent session {parent}. One task: {question}

Reads you may use: {reads}.
Answer from what you read in the checkout of {repo}. Mark anything you could not establish \
UNTESTED and stop there.

{marker} a single comment on pull request #{pr} of {repo}, carrying your answer and the \
paths you read.

Do nothing else that changes state.
"""

_TEMPLATES = {"probe": _PROBE_TEMPLATE, "worker": _WORKER_TEMPLATE}

_REPO_RE = re.compile(r"^(?:https://github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PR_URL_RE = re.compile(r"/pull/(\d+)/?$")
_MAX_QUESTION_CHARS = 400


class CloudSpawnBriefError(ValueError):
    """A brief input is malformed, or the finished brief asks for a side channel."""


def refuse_side_channel_words(text: str) -> None:
    """Raise if `text` names any side-channel verb; the gate for templates and questions alike."""
    hit = _FORBIDDEN_RE.search(text)
    if hit:
        raise CloudSpawnBriefError(
            f"brief names {hit.group(0)!r}: a spawn brief carries reads and one report write only"
        )


def count_report_writes(text: str) -> int:
    return text.count(REPORT_WRITE_MARKER)


def _repo(source_repo: Any) -> str:
    match = _REPO_RE.match(source_repo.strip()) if isinstance(source_repo, str) else None
    if not match:
        raise CloudSpawnBriefError(
            f"source_repo must be 'owner/repo' or a github.com URL, got {source_repo!r}"
        )
    return match.group(1)


def _pr_number(channel_pr: Any) -> int:
    if isinstance(channel_pr, bool):
        raise CloudSpawnBriefError(f"channel_pr must be a PR number or URL, got {channel_pr!r}")
    if isinstance(channel_pr, int) and channel_pr > 0:
        return channel_pr
    if isinstance(channel_pr, str):
        text = channel_pr.strip().lstrip("#")
        if text.isdigit() and int(text) > 0:
            return int(text)
        url = _PR_URL_RE.search(text)
        if url:
            return int(url.group(1))
    raise CloudSpawnBriefError(f"channel_pr must be a PR number or URL, got {channel_pr!r}")


def build_cloud_spawn(
    kind: Any,
    source_repo: Any,
    parent_session_id: Any,
    channel_pr: Any,
    question: Any,
) -> dict:
    """The `create_session` argument block: `{source_url, prompt, title}`.

    `source_url` is the child's only work-repo slot, and the channel PR lives on that same repo
    because a child can only comment on repos in its own GitHub scope.
    """
    if kind not in KINDS:
        raise CloudSpawnBriefError(f"kind must be one of {KINDS}, got {kind!r}")
    if not isinstance(parent_session_id, str) or not _SESSION_RE.match(parent_session_id):
        raise CloudSpawnBriefError(
            f"parent_session_id must be a session id, got {parent_session_id!r}"
        )
    if not isinstance(question, str) or not question.strip():
        raise CloudSpawnBriefError("question is required: the one thing the child answers")
    question = " ".join(question.split())
    if len(question) > _MAX_QUESTION_CHARS:
        raise CloudSpawnBriefError(f"question exceeds {_MAX_QUESTION_CHARS} characters")

    repo = _repo(source_repo)
    pr = _pr_number(channel_pr)
    prompt = _TEMPLATES[kind].format(
        parent=parent_session_id,
        question=question,
        reads=", ".join(_COMMON_READS),
        marker=REPORT_WRITE_MARKER,
        pr=pr,
        repo=repo,
    )
    refuse_side_channel_words(prompt)
    if count_report_writes(prompt) != 1:
        raise CloudSpawnBriefError("a spawn brief carries exactly one report write")
    return {
        "source_url": f"https://github.com/{repo}",
        "prompt": prompt,
        "title": f"{kind} for {parent_session_id}",
    }
