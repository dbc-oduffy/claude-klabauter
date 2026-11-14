"""CHECK 1 reads only the reset's own ref position for a subshell target.

A path-limited `git reset -q HEAD -- <paths>` moves no ref, so a `$(...)` elsewhere in
the same multi-line command (the hand-land recipe) must not deny it; a subshell in the
reset's own ref position still must.
"""

import pytest

from coordinator_core.bash_guards._dc_git_misc import (
    _reset_own_ref_part,
    check_destructive_git_orphan,
)

HAND_LAND = (
    "export GIT_INDEX_FILE=.git/tmp-index\n"
    "OLD=$(git rev-parse HEAD)\n"
    "NEW=$(git commit-tree -p $OLD -m land HEAD^{tree})\n"
    "git update-ref refs/heads/work $NEW $OLD\n"
    "unset GIT_INDEX_FILE\n"
    "git reset -q HEAD -- a/one.py b/two.py\n"
    "echo done=$(git rev-parse HEAD)"
)


def _denied(cmd: str) -> bool:
    rec = check_destructive_git_orphan(cmd)
    return bool(rec) and "subshell-resolved" in str(rec)


@pytest.mark.parametrize(
    "cmd",
    [
        HAND_LAND,
        "OLD=$(git rev-parse HEAD)\ngit reset -q HEAD -- a.py",
        "git reset -q HEAD -- a.py\nX=$(date)",
        "git reset -q HEAD -- $(git ls-files -m)",
    ],
)
def test_subshell_elsewhere_is_not_the_reset_target(cmd):
    assert check_destructive_git_orphan(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git reset --hard $(git rev-parse origin/main)",
        "git reset --hard $(\n  git rev-parse origin/main\n)",
        "git reset --soft $(git rev-parse x -- y) -- a.py",
        "OLD=1\ngit reset --mixed $(git rev-parse origin/main) -- a.py",
        "git reset --hard `git rev-parse origin/main`\necho ok",
        "git reset -q HEAD -- a.py\ngit reset --hard $(git rev-parse origin/main)",
    ],
)
def test_subshell_in_reset_ref_position_still_denies(cmd):
    assert _denied(cmd)


def test_ref_part_cuts_at_top_level_newline_and_dashdash_only():
    assert _reset_own_ref_part(" HEAD -- a\n$(x)") == " HEAD "
    assert _reset_own_ref_part(" HEAD\n$(x)") == " HEAD"
    assert _reset_own_ref_part(" $(a -- b\nc) -- p") == " $(a -- b\nc) "
