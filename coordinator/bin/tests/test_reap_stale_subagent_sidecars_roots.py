"""Root selection and the tracked/untracked split in `reap-stale-subagent-sidecars.py`.

WHY THIS EXISTS. The script had no test file at all, and the machinery
relocation turned that into a real hazard: repointing its walk from
`state/subagent-share/` to `.coordinator-local/subagent-share/` looked
correct and silently retired the history-preserving leg. `.coordinator-local/`
is gitignored, so `git ls-files` can never match anything under it -- every
candidate then classified as untracked and every reap downgraded from
`git rm` to a plain unlogged `os.remove`, with no test and no output change
to say so. A code-reviewer caught it on `196fbbc71e`; this pins it.

WHAT IS PINNED, and what deliberately is not. These cover the two facts that
regression turned on -- that `_tracked_paths` cannot be called without
naming which root it is asking about, and that it actually distinguishes a
tracked path under `state/` from a gitignored one under
`.coordinator-local/`. They do NOT drive `main()`: that needs the liveness
engine, the repo-root resolver and the dispatch-engine bootstrap, and a test
that mocks all three would pin the mocks rather than the behaviour. The
delete paths themselves stay covered by dry-run inspection, not by a test
that deletes.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Spawns a real `git ls-files` against a temp repo, like this directory's
# other git-touching tests.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SCRIPT = Path(__file__).resolve().parents[1] / "reap-stale-subagent-sidecars.py"


def _load_module():
    """Load the hyphenated CLI by path -- mirrors `test_percolate_mirror.py::_load_module`."""
    spec = importlib.util.spec_from_file_location("_reap_stale_subagent_sidecars", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reap():
    return _load_module()


def _git(root: str, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    """A temp repo whose `.gitignore` mirrors this one: the machinery root is
    ignored, `state/` is not."""
    root = str(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(".coordinator-local/\n", encoding="utf-8")

    for rel in (
        os.path.join("state", "subagent-share", "sid-old", "tracked.md"),
        os.path.join(".coordinator-local", "subagent-share", "sid-new", "ignored.md"),
    ):
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("sidecar\n", encoding="utf-8")

    _git(root, "add", "--", ".gitignore", "state")
    _git(root, "commit", "-q", "-m", "seed")
    return root


class TestTrackedPathsRequiresARoot:
    def test_under_is_keyword_only_and_has_no_default(self, reap):
        """The regression this file exists for. `under` defaulted to
        `"state/subagent-share"`; once a second root existed, a caller that
        omitted it silently classified every candidate as untracked."""
        sig = inspect.signature(reap._tracked_paths)
        under = sig.parameters["under"]

        assert under.kind is inspect.Parameter.KEYWORD_ONLY
        assert under.default is inspect.Parameter.empty

    def test_calling_without_under_raises(self, reap):
        with pytest.raises(TypeError):
            reap._tracked_paths("/nonexistent", ["a.md"])


class TestTrackedPathsDistinguishesTheTwoRoots:
    def test_a_tracked_legacy_path_is_reported_tracked(self, reap, repo):
        rel = os.path.join("state", "subagent-share", "sid-old", "tracked.md")

        tracked = reap._tracked_paths(repo, [rel], under=os.path.join("state", "subagent-share"))

        assert tracked == {rel}

    def test_a_gitignored_machinery_path_is_never_tracked(self, reap, repo):
        """Not an incidental miss: nothing under a gitignored root can EVER
        appear in `git ls-files`, which is why that root's reap must go
        through `os.remove` and why scoping the check to it alone silently
        retires the `git rm` leg."""
        rel = os.path.join(".coordinator-local", "subagent-share", "sid-new", "ignored.md")

        tracked = reap._tracked_paths(
            repo, [rel], under=os.path.join(".coordinator-local", "subagent-share")
        )

        assert tracked == set()

    def test_a_check_scoped_to_one_root_classifies_nothing_in_the_other(self, reap, repo):
        """Why the caller unions one scoped spawn PER root rather than making
        a single call: `under` narrows `git ls-files` to one directory, so a
        legacy path asked about under the machinery root comes back unclassified
        even though it is genuinely tracked."""
        legacy_rel = os.path.join("state", "subagent-share", "sid-old", "tracked.md")

        tracked = reap._tracked_paths(
            repo, [legacy_rel], under=os.path.join(".coordinator-local", "subagent-share")
        )

        assert tracked == set()
