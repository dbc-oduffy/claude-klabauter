"""Regression tests for git_hook_install's append-form CURRENCY branch.

THE DEFECT THESE PIN. `_ensure_hook` had three dispositions for an existing
hook. The whole-file-shim branch checks currency against `_hook_gen_stamp_line()`
and rewrites when stale -- a mechanism that exists because a hand-listed
substring check kept certifying stale hooks current, twice, by its own comment's
account. The append-form branch had no equivalent: start marker present and end
marker present meant `_chmod_x` then `left-append-form`, UNCONDITIONALLY, with no
comparison of any kind. `_append_block` emitted no stamp at all, so there was
nothing to compare even if the branch had wanted to.

The consequence was not a caveat, it was a second defect: every repo whose hook
carried an appended coordinator block kept whatever body it was installed with,
forever. Fixes landed in `_append_block` -- the 2026-08-31 MSYS drive-letter
normalisation among them -- reached only repos where no block existed yet. The
LEGACY (end-marker-less) path at least says so out loud ("Remove the stale block
by hand to pick up current fixes"); the modern path, where the end marker makes
the extent precisely identifiable and therefore safely replaceable, said nothing
and did nothing.

WHY THE STAMP AND NOT A BYTE COMPARE. `_append_block` interpolates a baked
interpreter path that legitimately differs between machines and between
resolutions on one machine. Byte-comparing the installed block against a
freshly generated one would rewrite a FOREIGN hook file on churn rather than on
drift. The stamp is the same predicate the whole-file branch already trusts.

WHY REFUSING STAYS THE DEFAULT FOR ANYTHING AMBIGUOUS. This branch rewrites
bytes inside a hook file somebody else owns, and `_ensure_hook`'s own docstring
is emphatic about refusing to guess rather than guessing and destroying. A
repeated start marker, an end marker preceding its start, or a missing end
marker each leave the file untouched, with a warning -- covered below.

MEASURED SCOPE at the time of the fix (2026-08-31, this box): ZERO append-form
coordinator blocks across 65 hook files in 20 machine-local-registered repos --
every installed hook is a whole-file shim. The gap was latent here, not live;
these tests are what keep it from becoming live silently.

Run:
    pytest coordinator/bin/lib/test_git_hook_append_block_refresh.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import git_hook_install as ghi  # noqa: E402

_HEADER = "coordinator Session-Id trailer injection"
_SCRIPT = "coordinator-prepare-commit-msg"
_INVOKE = '"$_PY" "$_T" "$@"'

_FOREIGN_ABOVE = "#!/bin/sh\n# somebody else's hook\necho pre-existing >/dev/null\n"
_FOREIGN_BELOW = "echo trailing-entry >/dev/null\n"


def _current_append_block() -> str:
    """The full appended text `ensure_prepare_commit_msg_hook` composes --
    block, `|| true` guard, END marker -- built the same way that caller does
    rather than restated here."""
    _start, end = ghi._append_markers(_HEADER)
    return (
        ghi._append_block("/fake/coord/bin", _SCRIPT, _HEADER, _INVOKE)
        + f" || true\n{end}"
    )


def _plant(tmp_path: Path, block_text: str, *, below: str = _FOREIGN_BELOW) -> Path:
    """A repo whose prepare-commit-msg hook is a FOREIGN chain with a
    coordinator block spliced into the middle of it."""
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "prepare-commit-msg"
    hook.write_text(
        _FOREIGN_ABOVE + block_text.lstrip("\n").rstrip("\n") + "\n" + below,
        encoding="utf-8",
        newline="",
    )
    return hook


def _ensure(tmp_path: Path, *, check_only: bool = False) -> str:
    outcome: list = []
    ghi._ensure_hook(
        "",
        hook_name="prepare-commit-msg",
        script_name=_SCRIPT,
        marker=_SCRIPT,
        fresh_body=ghi._shim_body("/fake/coord/bin", _SCRIPT, 'exec "$_PY" "$SCRIPT" "$@"'),
        append_block=_current_append_block(),
        header=_HEADER,
        root=str(tmp_path),
        outcome=outcome,
        check_only=check_only,
    )
    return outcome[0] if outcome else "unknown"


# ---------------------------------------------------------------------------
# The emitter now stamps.
# ---------------------------------------------------------------------------

def test_the_stamp_sits_inside_our_own_markers():
    """Nothing downstream can compare currency if the emitter never states it
    -- that is the half of the defect which made the other half unfixable --
    and a stamp emitted outside the block's extent would be orphaned by the
    very splice meant to refresh it. Asserting the stamp's POSITION covers
    both: a stamp between the markers is necessarily in the block."""
    full = _current_append_block()
    start, end = ghi._append_markers(_HEADER)
    lines = [ln.strip() for ln in full.splitlines()]
    assert lines.index(ghi._hook_gen_stamp_line()) > lines.index(start)
    assert lines.index(ghi._hook_gen_stamp_line()) < lines.index(end)


def test_the_stamp_line_is_inert_shell():
    """The block lands inside a foreign hook; a stamp that executed would be a
    behaviour change smuggled in by a currency mechanism."""
    stamp = ghi._hook_gen_stamp_line()
    assert stamp.lstrip().startswith("#")


# ---------------------------------------------------------------------------
# The branch now refreshes.
# ---------------------------------------------------------------------------

def test_current_block_is_left_alone(tmp_path):
    hook = _plant(tmp_path, _current_append_block())
    before = hook.read_text(encoding="utf-8")
    assert _ensure(tmp_path) == "already-current"
    assert hook.read_text(encoding="utf-8") == before


def test_stale_block_is_refreshed_in_place(tmp_path):
    """THE REGRESSION. A block one generation behind used to return
    `left-append-form` and keep its stale body forever."""
    stale = _current_append_block().replace(
        f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP}",
        f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP - 1}",
    )
    hook = _plant(tmp_path, stale)
    assert _ensure(tmp_path) == "refreshed-append-form"
    body = hook.read_text(encoding="utf-8")
    assert ghi._hook_gen_stamp_line() in body
    assert f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP - 1}" not in body


def test_a_refresh_preserves_the_foreign_hook_around_it(tmp_path):
    """The whole reason this branch refused to act: everything above the block
    is somebody else's, and everything below it may be too."""
    stale = _current_append_block().replace(
        f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP}",
        f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP - 1}",
    )
    hook = _plant(tmp_path, stale)
    _ensure(tmp_path)
    body = hook.read_text(encoding="utf-8")
    assert body.startswith(_FOREIGN_ABOVE)
    assert body.endswith(_FOREIGN_BELOW)


def test_a_refresh_is_idempotent_and_adds_no_blank_lines(tmp_path):
    """A splice that re-adds the block's own leading newline grows the hook by
    one blank line per install run, forever."""
    stale = _current_append_block().replace(
        f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP}",
        f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP - 1}",
    )
    hook = _plant(tmp_path, stale)
    assert _ensure(tmp_path) == "refreshed-append-form"
    first = hook.read_text(encoding="utf-8")
    assert _ensure(tmp_path) == "already-current"
    assert hook.read_text(encoding="utf-8") == first


def test_check_only_classifies_without_writing(tmp_path):
    """`check_only` reaches the same classification via the same predicate and
    performs no write -- the contract every other branch already honours."""
    stale = _current_append_block().replace(
        f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP}",
        f"coordinator-hook-gen: {ghi._HOOK_GEN_STAMP - 1}",
    )
    hook = _plant(tmp_path, stale)
    before = hook.read_text(encoding="utf-8")
    assert _ensure(tmp_path, check_only=True) == "refreshed-append-form"
    assert hook.read_text(encoding="utf-8") == before


def test_a_refresh_counts_as_healed_not_as_left_alone():
    """`refreshed-append-form` must reach the fleet report as a repair. The
    `left-*` states mean 'a foreign chain we deliberately did not touch', and a
    refresh is the opposite of not touching."""
    assert "refreshed-append-form" in ghi._HEALED_OUTCOMES


# ---------------------------------------------------------------------------
# Ambiguity still refuses.
# ---------------------------------------------------------------------------

def test_legacy_block_without_an_end_marker_is_still_left_alone(tmp_path):
    """Unchanged behaviour, asserted so the refresh path cannot quietly grow
    into the one shape whose extent genuinely cannot be identified."""
    start, end = ghi._append_markers(_HEADER)
    legacy = _current_append_block().replace(end, "")
    hook = _plant(tmp_path, legacy)
    before = hook.read_text(encoding="utf-8")
    assert _ensure(tmp_path) == "left-legacy-append-form"
    assert hook.read_text(encoding="utf-8") == before


def test_a_duplicated_start_marker_refuses_rather_than_guessing(tmp_path, capsys):
    start, _end = ghi._append_markers(_HEADER)
    hook = _plant(tmp_path, _current_append_block(), below=f"{start}\n" + _FOREIGN_BELOW)
    before = hook.read_text(encoding="utf-8")
    assert _ensure(tmp_path) == "left-append-form"
    assert hook.read_text(encoding="utf-8") == before
    assert "ambiguous" in capsys.readouterr().err


def test_block_extent_returns_none_on_every_unidentifiable_shape():
    start, end = ghi._append_markers(_HEADER)
    body = f"a\n{start}\nx\n{end}\nb\n"
    assert ghi._block_extent(body, start, end) == (1, 3)
    assert ghi._block_extent(f"a\n{start}\nx\n", start, end) is None
    assert ghi._block_extent(f"a\n{end}\nx\n{start}\n", start, end) is None
    assert ghi._block_extent(f"{start}\n{start}\nx\n{end}\n", start, end) is None
    assert ghi._block_extent("nothing here\n", start, end) is None


def test_block_extent_matches_markers_only_on_their_own_line():
    """A marker quoted inside a longer line is not a marker -- the same
    exact-line predicate `_has_line` uses."""
    start, end = ghi._append_markers(_HEADER)
    body = f'echo "{start}"\n{start}\nx\n{end}\n'
    assert ghi._block_extent(body, start, end) == (1, 3)


# ---------------------------------------------------------------------------
# The shape guard the sibling emitter already had, and this one did not.
# ---------------------------------------------------------------------------
#
# `test_hook_gen_stamp_bump_is_required_for_shape_changes` (in
# test_git_hook_install.py) pins `_shim_body`'s emitted shape against a
# checksum. `_append_block` had NO equivalent, even though both emitters'
# docstrings say the two must change together -- so the MSYS drive-letter fix
# landed in one and not the other, and this file's own stamp addition changed
# `_append_block`'s shape with nothing going red. That is the same defect class
# as everything else this module has been convicted of: a guard that checks the
# emitter it can see rather than the pair that must agree.
#
# The baked interpreter path is normalized out before hashing for the reason the
# sibling guard states at length: it is machine state, not body SHAPE, and
# hashing it would make this pass only on the box that last updated the constant.

_EXPECTED_APPEND_BLOCK_CHECKSUM = (
    "55270abbdbf5792b5892434fb460120f787e74546f316316fb91a4342128ebe3"
)


def _normalize_baked_py(body: str) -> str:
    import re

    return re.sub(r'^(_PY=")[^"]*(")$', r"\1<BAKED-INTERPRETER>\2", body, flags=re.M)


def test_stamp_bump_is_required_for_append_block_shape_changes():
    import hashlib

    block = ghi._append_block("/fake/coord/bin", _SCRIPT, _HEADER, _INVOKE)
    checksum = hashlib.sha256(
        _normalize_baked_py(block).encode("utf-8")
    ).hexdigest()
    assert checksum == _EXPECTED_APPEND_BLOCK_CHECKSUM, (
        f"_append_block's emitted shape changed (new checksum {checksum}). If the "
        "change alters what an INSTALLED block does, bump _HOOK_GEN_STAMP in "
        "coordinator/bin/lib/git_hook_install.py so already-installed blocks refresh "
        "to it; then update _EXPECTED_APPEND_BLOCK_CHECKSUM in this test. Check "
        "_shim_body for the same change before concluding only one emitter needed it."
    )
    # Hashing text that no longer carries the stamp would let this checksum go
    # stale in lockstep with an emitter that stopped stamping at all.
    assert ghi._hook_gen_stamp_line() in block
