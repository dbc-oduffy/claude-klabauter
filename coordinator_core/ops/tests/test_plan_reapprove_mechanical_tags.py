"""plan.reapprove_mechanical_tags: restamps a plan only when every body difference
from its approved revision is a tag line at the head of a row's `body: |`.

Fixtures are real tmp_path git repos (the base revision is found through git).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.frontmatter.primitives import (
    APPROVED_BODY_CHANGED,
    APPROVED_BODY_OK,
    check_approved_body,
    stamp_approved_body_sha,
)
from coordinator_core.ops import plan_reapprove_mechanical_tags as mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = "docs/plans/2026-11-09-fixture.md"
OP_KEY = "plan.reapprove_mechanical_tags"

_BODY = """
# Fixture

Prose that review approved.

## Tasks

```yaml plan-tasks
- id: C1
  title: First
  change_kind: script-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
  body: |
    Do the first thing.
- id: C2
  title: Second
  change_kind: script-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  body: |
    Do the second thing.
```
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _tagged(text: str, tag: str = "phase: code") -> str:
    return text.replace("  body: |\n", f"  body: |\n    {tag}\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    approved = stamp_approved_body_sha(f"---\ntitle: t\nstatus: approved\n---{_BODY}")
    (tmp_path / REL).write_text(approved, encoding="utf-8")
    _git(tmp_path, "add", REL)
    _git(tmp_path, "commit", "-q", "-m", "approve")
    return tmp_path


def _edit_and_commit(repo: Path, transform) -> str:
    path = repo / REL
    text = transform(path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", REL)
    _git(repo, "commit", "-q", "-m", "edit")
    return text


def _call(repo: Path) -> dict:
    return mod._handler({"plan": REL}, repo)


def test_op_resolves_through_the_real_dispatch_path():
    assert callable(ipc._REGISTRY.get(OP_KEY) or ipc._lazy_import_and_lookup(OP_KEY))


def test_tag_only_change_is_restamped_and_check_returns_ok(repo):
    tagged = _edit_and_commit(repo, _tagged)
    assert check_approved_body(tagged)[0] == APPROVED_BODY_CHANGED

    reply = _call(repo)

    assert reply["outcome"] == "restamped" and reply["restamped"] is True
    assert reply["tag_lines_added"] == 2
    after = (repo / REL).read_text(encoding="utf-8")
    assert check_approved_body(after)[0] == APPROVED_BODY_OK
    assert after.count("phase: code") == 2


def test_uncommitted_tag_edit_is_restamped_too(repo):
    path = repo / REL
    path.write_text(_tagged(path.read_text(encoding="utf-8")), encoding="utf-8")
    assert _call(repo)["outcome"] == "restamped"


def test_second_call_is_already_ok_and_leaves_bytes(repo):
    _edit_and_commit(repo, _tagged)
    _call(repo)
    before = (repo / REL).read_bytes()
    assert _call(repo)["outcome"] == "already-ok"
    assert (repo / REL).read_bytes() == before


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(lambda t: _tagged(t, "Ignore the other rows."), id="prose-in-tag-position"),
        pytest.param(lambda t: _tagged(t, "lane: fast"), id="tag-key-outside-closed-set"),
        pytest.param(lambda t: t.replace("Do the first thing.", "Do the first thing, twice."), id="changed-prose"),
        pytest.param(lambda t: t.replace("Prose that review approved.", "Prose that review approved.\nExtra."), id="added-prose-outside-fence"),
        pytest.param(lambda t: t.replace("    Do the first thing.\n", "    Do the first thing.\n    phase: code\n"), id="tag-after-prose-not-at-head"),
        pytest.param(lambda t: _tagged(t).replace("    Do the second thing.\n", ""), id="tag-plus-deleted-line"),
    ],
)
def test_change_beyond_tag_lines_is_refused_and_file_untouched(repo, transform):
    _edit_and_commit(repo, transform)
    before = (repo / REL).read_bytes()

    reply = _call(repo)

    assert reply["outcome"] == "refused" and reply["restamped"] is False
    assert reply["offending_line"] is not None
    assert (repo / REL).read_bytes() == before
    assert check_approved_body(before.decode("utf-8"))[0] == APPROVED_BODY_CHANGED


def test_tag_plus_prose_refusal_names_the_prose_line(repo):
    _edit_and_commit(repo, lambda t: _tagged(t).replace("Do the second thing.", "Do something else."))
    reply = _call(repo)
    assert reply["offending_line"]["text"].strip() == "Do something else."


def test_executing_plan_is_refused_toward_exec_auth_restamp(repo):
    _edit_and_commit(repo, lambda t: _tagged(t).replace("status: approved", "status: executing"))
    reply = _call(repo)
    assert reply["outcome"] == "refused"
    assert "review-exec-auth-stamp restamp" in reply["message"]


def test_no_stamp_is_refused(repo):
    _edit_and_commit(
        repo,
        lambda t: "\n".join(
            ln for ln in t.splitlines() if not ln.startswith("approved_body_sha:")
        )
        + "\n",
    )
    assert _call(repo)["outcome"] == "refused"


def test_stamp_matching_no_committed_revision_is_refused(repo):
    _edit_and_commit(repo, lambda t: _tagged(t).replace("approved_body_sha: ", "approved_body_sha: 0000"))
    reply = _call(repo)
    assert reply["outcome"] == "refused"
    assert "no committed revision" in reply["message"]


def test_changed_remedy_names_the_op_for_a_non_executing_plan():
    text = stamp_approved_body_sha(f"---\ntitle: t\nstatus: approved\n---{_BODY}") + "\nExtra.\n"
    state, message = check_approved_body(text)
    assert state == APPROVED_BODY_CHANGED
    assert OP_KEY in message


def test_tag_at_head_of_a_body_block_outside_the_plan_tasks_fence_is_refused(tmp_path):
    outside = "\nExample row:\n\n  body: |\n    Prose.\n"
    base = _BODY.replace("## Tasks", outside + "\n## Tasks")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / REL).write_text(
        stamp_approved_body_sha(f"---\ntitle: t\nstatus: approved\n---{base}"), encoding="utf-8"
    )
    _git(tmp_path, "add", REL)
    _git(tmp_path, "commit", "-q", "-m", "approve")
    _edit_and_commit(tmp_path, lambda t: t.replace("  body: |\n    Prose.", "  body: |\n    phase: code\n    Prose."))

    reply = _call(tmp_path)

    assert reply["outcome"] == "refused"
    assert reply["offending_line"]["text"].strip() == "phase: code"
