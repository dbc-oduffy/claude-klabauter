"""Tests for the machinery-root boundary move (chunk C3,
docs/plans/2026-09-02-state-keeps-the-work-not-the-machinery.md).

WHAT THIS PINS. `bump_foreign_repo_write.py`, `bump_outside_repo_write.py`
and `guard_multiprobe_banner.py` each used to construct the subagent-sandbox
path with a literal `Path(root) / "state" / "subagent-share" / session_id`
join. C1 relocated the real sandbox to `machinery_paths.share_dir` (`<repo_
root>/.coordinator-local/subagent-share/<session_id>`); C3 repoints these
three guards through that accessor instead of restating the old join. A
repoint miss here is silent and fails OPEN (module docstrings' own words):
the guard keeps computing a path nothing writes to any more, so every write
it was meant to recognize as in-bounds sandbox territory stops being
recognized, with no error anywhere.

The test asserts the NEGATIVE directly, on the guards' own verdicts/return
values -- never on message text (`docs/wiki/guard-messaging.md` register):
a candidate under the OLD `state/subagent-share/<sid>` shape is no longer
treated as in-bounds sandbox territory, and one under the NEW
`.coordinator-local/subagent-share/<sid>` root is.

`bump_outside_repo_write._target_is_always_allowed` is the one production
boolean that actually GATES a verdict on this path shape (AC9's own
always-allowed-roots carve-out) -- see that module's own
`test_ac9_subagent_sandbox_root_write_never_bumps` docstring for why the
end-to-end `check_bump_outside_repo_write` entry point cannot discriminate
old-vs-new here even post-fix (the sandbox path is ordinarily already INSIDE
the anchor's own git root either way, so the guard's own "resolves under
SOME git root" skip fires before `_target_is_always_allowed` is ever
reached) -- this suite therefore asserts the allow-list boolean directly,
the same pattern that sibling test already establishes.

`bump_foreign_repo_write._sandbox_root_hint` and `guard_multiprobe_banner.
_sandbox_script_hint` carry no bounds decision of their own (message-hint
only, per each module's own docstring) -- pinned here as a direct return-
value equality against `machinery_paths.share_dir`, not a bounds verdict,
so a future edit cannot silently regress either hint back to the old
literal join without this suite noticing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import bump_foreign_repo_write
from coordinator_core.bash_guards import bump_outside_repo_write
from coordinator_core.bash_guards import guard_multiprobe_banner
from coordinator_core.session import machinery_paths

pytestmark = [pytest.mark.cadence]

_SESSION_ID = "sess-c3-boundary"


@pytest.fixture(autouse=True)
def _clean_temp_scratch_carveout(monkeypatch, tmp_path):
    """`tmp_path` lives under the REAL system temp dir on every platform
    this suite runs on -- without repointing the shared classifier's
    recognized-temp-root primitives, every fixture path built under it
    would ALSO resolve as the AC9 system-temp carve-out and swallow the
    old-vs-new distinction this suite exists to prove. Mirrors
    `test_bump_outside_repo_write.py`'s own `_clean_bump_env` fixture,
    narrowed to just the temp-root repoint this file's fixtures need."""
    fake_system_temp = tmp_path / "not-the-real-system-temp"
    fake_system_temp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_system_temp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_system_temp))
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)


def _old_sandbox_path(git_root: str, session_id: str) -> str:
    """The literal join every one of these three guards used to compute,
    restated here ONLY as the negative fixture this suite probes against --
    never imported from production, so a stray revert of the repoint cannot
    make this helper agree with it by construction."""
    return str(Path(git_root) / "state" / "subagent-share" / session_id)


def test_new_root_write_is_in_bounds(tmp_path):
    """A write under the NEW machinery root's subagent-share bucket is
    treated as in-bounds sandbox territory -- the guard's own verdict
    (AC9's always-allowed-roots boolean), not its message text."""
    git_root = str(tmp_path)
    new_target = machinery_paths.share_dir(git_root, _SESSION_ID)
    os.makedirs(new_target, exist_ok=True)
    target_file = str(Path(new_target) / "note.txt")

    assert bump_outside_repo_write._target_is_always_allowed(
        target_file, git_root, _SESSION_ID, env=os.environ
    )


def test_old_root_write_is_no_longer_in_bounds(tmp_path):
    """The NEGATIVE this chunk exists to assert: a write under the OLD
    `state/subagent-share/<session_id>` path is NOT treated as in-bounds
    sandbox territory once the guard resolves through `machinery_paths`
    rather than a hand-built join -- a repoint miss would leave this
    silently `True` (fail open) instead."""
    git_root = str(tmp_path)
    old_target = _old_sandbox_path(git_root, _SESSION_ID)
    os.makedirs(old_target, exist_ok=True)
    target_file = str(Path(old_target) / "note.txt")

    assert not bump_outside_repo_write._target_is_always_allowed(
        target_file, git_root, _SESSION_ID, env=os.environ
    )


def test_bump_foreign_repo_write_sandbox_hint_resolves_through_machinery_paths(tmp_path):
    """`_sandbox_root_hint` (message-hint only, no bounds decision of its
    own) must agree with `machinery_paths.share_dir`, and must NOT still
    hand back the old literal join."""
    git_root = str(tmp_path)

    hint = bump_foreign_repo_write._sandbox_root_hint(git_root, _SESSION_ID)

    assert hint == machinery_paths.share_dir(git_root, _SESSION_ID)
    assert hint != _old_sandbox_path(git_root, _SESSION_ID)


def test_bump_outside_repo_write_sandbox_root_resolves_through_machinery_paths(tmp_path):
    """Same pin for `bump_outside_repo_write._sandbox_root` (the function
    `_target_is_always_allowed` itself calls to build its allow-list)."""
    git_root = str(tmp_path)

    sandbox = bump_outside_repo_write._sandbox_root(git_root, _SESSION_ID)

    assert sandbox == machinery_paths.share_dir(git_root, _SESSION_ID)
    assert sandbox != _old_sandbox_path(git_root, _SESSION_ID)


def test_guard_multiprobe_banner_script_hint_resolves_through_machinery_paths(tmp_path):
    """Same pin for `guard_multiprobe_banner._sandbox_script_hint`, which
    appends the scratch-script filename onto the SAME machinery-root sandbox
    directory the other two guards resolve."""
    git_root = str(tmp_path)

    hint = guard_multiprobe_banner._sandbox_script_hint(git_root, _SESSION_ID)

    expected = str(
        Path(machinery_paths.share_dir(git_root, _SESSION_ID))
        / guard_multiprobe_banner._SCRATCH_SCRIPT_NAME
    )
    assert hint == expected
    assert hint != str(
        Path(_old_sandbox_path(git_root, _SESSION_ID)) / guard_multiprobe_banner._SCRATCH_SCRIPT_NAME
    )


def test_sandbox_hints_empty_on_missing_input():
    """All three hints keep their existing "no fabricated path" contract --
    `""` when either input is empty, unchanged by the repoint."""
    assert bump_foreign_repo_write._sandbox_root_hint(None, _SESSION_ID) == ""
    assert bump_foreign_repo_write._sandbox_root_hint("/repo", "") == ""
    assert bump_outside_repo_write._sandbox_root(None, _SESSION_ID) == ""
    assert bump_outside_repo_write._sandbox_root("/repo", "") == ""
    assert guard_multiprobe_banner._sandbox_script_hint(None, _SESSION_ID) == ""
    assert guard_multiprobe_banner._sandbox_script_hint("/repo", "") == ""
