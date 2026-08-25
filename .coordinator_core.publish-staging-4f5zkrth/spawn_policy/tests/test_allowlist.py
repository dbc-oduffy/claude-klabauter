"""Tests for coordinator_core.spawn_policy.allowlist.

Written against the register block format documented in
`tasks/shell-spawn-regrowth-gate/PINNED-API.md`, using this module's own
fixture strings. C2 (the real `docs/reference/shell-out-carve-outs.md`
block) is authored concurrently and is not depended on here.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from coordinator_core.spawn_policy.allowlist import (
    is_sanctioned,
    load_allowlist,
    unpinned_entries,
)
from coordinator_core.spawn_policy.detect import SpawnKind, SpawnSite

_PINNED_BLOCK = textwrap.dedent(
    """
    # Shell-out carve-outs

    Some prose rationale here.

    ```yaml shell-out-allowlist
    - cls: a
      path: coordinator_core/install/some_module.py
      enclosing: _install_homebrew
      argv0: bash
      ordinal: 0
      argv_digest: "0123456789ab"
      reason: "3rd-party installer consumed as-published (Homebrew)"
      ruled_on: "2026-07-21"
    ```

    More prose after the block.
    """
)

_UNPINNED_BLOCK = textwrap.dedent(
    """
    ```yaml shell-out-allowlist
    - cls: b
      path: coordinator_core/hooks/git_hook.py
      enclosing: _emit_hook_body
      argv0: sh
      ordinal: 0
      argv_digest: null
      reason: "generated git-hook body execed by git via sh"
      ruled_on: "2026-07-21"
    ```
    """
)

_ZERO_BLOCKS = "Just prose, no fenced allowlist block here.\n"

_TWO_BLOCKS = textwrap.dedent(
    """
    ```yaml shell-out-allowlist
    - cls: a
      path: p.py
      enclosing: f
      argv0: bash
      ordinal: 0
      argv_digest: "aaaaaaaaaaaa"
      reason: "one"
      ruled_on: "2026-07-21"
    ```

    ```yaml shell-out-allowlist
    - cls: b
      path: q.py
      enclosing: g
      argv0: sh
      ordinal: 0
      argv_digest: "bbbbbbbbbbbb"
      reason: "two"
      ruled_on: "2026-07-21"
    ```
    """
)


def _write(
    tmp_path: pathlib.Path, text: str, name: str = "shell-out-carve-outs.md"
) -> pathlib.Path:
    doc = tmp_path / name
    doc.write_text(text)
    return doc


def test_load_allowlist_parses_pinned_block(tmp_path: pathlib.Path):
    doc = _write(tmp_path, _PINNED_BLOCK)
    entries = load_allowlist(doc)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.cls == "a"
    assert entry.path == "coordinator_core/install/some_module.py"
    assert entry.enclosing == "_install_homebrew"
    assert entry.argv0 == "bash"
    assert entry.ordinal == 0
    assert entry.argv_digest == "0123456789ab"
    assert entry.reason == "3rd-party installer consumed as-published (Homebrew)"
    assert entry.ruled_on == "2026-07-21"


def test_load_allowlist_zero_blocks_is_error(tmp_path: pathlib.Path):
    doc = _write(tmp_path, _ZERO_BLOCKS)
    with pytest.raises(ValueError):
        load_allowlist(doc)


def test_load_allowlist_two_blocks_is_error(tmp_path: pathlib.Path):
    doc = _write(tmp_path, _TWO_BLOCKS)
    with pytest.raises(ValueError):
        load_allowlist(doc)


def test_load_allowlist_null_digest_is_unpinned(tmp_path: pathlib.Path):
    doc = _write(tmp_path, _UNPINNED_BLOCK)
    entries = load_allowlist(doc)
    assert entries[0].argv_digest is None


def _site(**overrides) -> SpawnSite:
    defaults = dict(
        path="coordinator_core/install/some_module.py",
        enclosing="_install_homebrew",
        argv0="bash",
        ordinal=0,
        kind=SpawnKind.SHELL_BINARY,
        argv_digest="0123456789ab",
        lineno=42,
    )
    defaults.update(overrides)
    return SpawnSite(**defaults)


def test_is_sanctioned_exact_match(tmp_path: pathlib.Path):
    entries = load_allowlist(_write(tmp_path, _PINNED_BLOCK))
    assert is_sanctioned(_site(), entries) is True


def test_is_sanctioned_false_on_different_argv_digest_same_ordinal(
    tmp_path: pathlib.Path,
):
    entries = load_allowlist(_write(tmp_path, _PINNED_BLOCK))
    mismatched = _site(argv_digest="ffffffffffff")
    assert is_sanctioned(mismatched, entries) is False


def test_is_sanctioned_false_when_site_key_absent(tmp_path: pathlib.Path):
    entries = load_allowlist(_write(tmp_path, _PINNED_BLOCK))
    other = _site(path="other/file.py")
    assert is_sanctioned(other, entries) is False


def test_is_sanctioned_never_infers_from_rationale_alone(tmp_path: pathlib.Path):
    # A site that would satisfy the SAME rationale/class but isn't the exact
    # named entry (different enclosing) must not be sanctioned.
    entries = load_allowlist(_write(tmp_path, _PINNED_BLOCK))
    lookalike = _site(enclosing="_install_something_else")
    assert is_sanctioned(lookalike, entries) is False


def test_is_sanctioned_unpinned_entry_matches_on_site_key_alone(
    tmp_path: pathlib.Path,
):
    entries = load_allowlist(_write(tmp_path, _UNPINNED_BLOCK))
    site = _site(
        path="coordinator_core/hooks/git_hook.py",
        enclosing="_emit_hook_body",
        argv0="sh",
        ordinal=0,
        argv_digest="whatever-this-does-not-matter",
    )
    assert is_sanctioned(site, entries) is True


def test_unpinned_entries_filters_null_digest(tmp_path: pathlib.Path):
    pinned = load_allowlist(_write(tmp_path, _PINNED_BLOCK))
    assert unpinned_entries(pinned) == []

    unpinned = load_allowlist(_write(tmp_path, _UNPINNED_BLOCK, name="other.md"))
    assert len(unpinned_entries(unpinned)) == 1
