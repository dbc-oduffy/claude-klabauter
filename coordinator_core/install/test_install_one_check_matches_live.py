"""test_install_one_check_matches_live — AC5 of
docs/plans/2026-08-26-the-installers-two-halves-come-from-two-repos.md.

The invariant: `_install_one(check_only=True)`'s verdict on a differing
destination must match what `_install_one(check_only=False)` actually does to
that same destination. Testing check mode alone cannot state that — the defect
was never "check is wrong", it was "check and live disagree" — so every test
here runs BOTH against identical fixtures and compares outcomes.

What went wrong before C3: the check branch raised
`check failed: <name> is stale` for anything not byte-identical, exempting only
`write_strategy in ("careful", "refuse")`, while the live path reached the
terminal `else` and preserved the same file as operator-customized at exit 0.
`--check-only` therefore demanded a state the installer refused to produce, and
the manifest-declared entry point could not reach exit 0 on a host carrying any
preserve-class divergence. Reported by doe-claude-em against
`settings-manifest.md` — a `.md`, so outside the `.py`/`.sh` force-overwrite
classes.

Anti-scope: this does NOT assert that preserving is the CORRECT call for a
given file. `_install_one` cannot distinguish genuine operator customization
from stale-template drift at this seam and does not try — that is
state/bug-backlog/2026-08-26-preserve-on-diff-cannot-tell-operator-cu-73edbfc56ba9.yaml,
deliberately out of scope here. A test that grew an opinion about which files
"should" be preserved would be asserting the heuristic that record exists to
say we do not have.

Sibling: `test_install_one_overwrite_policy.py` covers the live-path
overwrite-vs-preserve matrix and declares `check_only` out of its scope. This
file is that skipped cell.

Run: python -m pytest coordinator_core/install/test_install_one_check_matches_live.py -q
"""
from __future__ import annotations

import pytest

from coordinator_core.install.substrate import SubstrateFatalError, _install_one

pytestmark = [pytest.mark.cadence]

# (filename, expect_live_rewrites, write_strategy, force_overwrite) — one
# preserve-class and one force-class name from each side of the policy in
# `_install_one`'s own docstring, PLUS the `careful`/`refuse` strategies
# explicitly crossed with both `force_overwrite` values (Review:
# coordinator:code-reviewer P2 — the table previously only ever exercised
# the default `write_strategy="force"` family via suffix/name-derived
# classification, so it could never reach the `careful`/`refuse` branches
# the biconditional below is fully capable of catching).
_CASES = [
    ("settings-manifest.md", False, "force", None),   # the reported file: preserve-on-diff
    ("registry.toml", False, "force", None),          # config: preserve-on-diff
    ("some-tool.py", True, "force", None),             # code: force-overwrite-on-diff
    ("machine-local", True, "force", None),            # extension-less wrapper: force-overwrite
    # careful: only rewrites when explicitly force_overwrite=True (mirrors
    # `setup_hook_files`'s real call site); force_overwrite=False is
    # indistinguishable from plain preserve-on-diff.
    ("careful-tracked.md", False, "careful", False),
    ("careful-tracked.md", True, "careful", True),
    # refuse: NEVER rewrites, regardless of force_overwrite — the
    # git-identity-probe-unavailable degrade always preserves.
    ("refuse-tracked.md", False, "refuse", False),
    ("refuse-tracked.md", False, "refuse", True),
]


def _fixture(tmp_path, name: str):
    src = tmp_path / "src" / name
    dst = tmp_path / "dst" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("shipped template\n", encoding="utf-8")
    dst.write_text("diverged on disk\n", encoding="utf-8")
    return src, dst


def _install_one_kwargs(tmp_path, name: str, write_strategy: str, force_overwrite):
    """Assemble the kwargs `_install_one` needs for a given `write_strategy`.

    `write_strategy="careful"` requires the careful-write manifest/base
    kwargs (AC19 blast-radius guard) even in a test fixture — the function
    fatals rather than guess at them.
    """
    kwargs = {"write_strategy": write_strategy}
    if force_overwrite is not None:
        kwargs["force_overwrite"] = force_overwrite
    if write_strategy == "careful":
        kwargs["careful_relative_path"] = name
        kwargs["careful_manifest_relative_paths"] = frozenset({name})
        kwargs["careful_install_base"] = tmp_path
    return kwargs


@pytest.mark.parametrize("name,live_rewrites,write_strategy,force_overwrite", _CASES)
def test_check_verdict_matches_what_live_actually_does(
    tmp_path, name, live_rewrites, write_strategy, force_overwrite
):
    """check_only must not FATAL on a destination live would leave alone."""
    src, dst = _fixture(tmp_path, name)
    before = dst.read_text(encoding="utf-8")
    kwargs = _install_one_kwargs(tmp_path, name, write_strategy, force_overwrite)

    check_raised = None
    try:
        _install_one(src, dst, False, "machine-local", True, **kwargs)
    except SubstrateFatalError as exc:
        check_raised = exc
    # check mode is a dry run: it must never touch the destination.
    assert dst.read_text(encoding="utf-8") == before, (
        f"{name}: check_only=True mutated {dst} — it is a dry run"
    )

    _install_one(src, dst, False, "machine-local", False, **kwargs)
    live_rewrote = dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")

    assert live_rewrote is live_rewrites, (
        f"{name}: live path behaviour changed — this test's expectation is stale, "
        "or the overwrite policy moved"
    )
    assert (check_raised is not None) is live_rewrote, (
        f"{name}: check mode {'raised' if check_raised else 'passed'} but live "
        f"{'rewrote' if live_rewrote else 'preserved'} the destination — check "
        "may only report stale for what live would actually rewrite"
    )


def test_the_reported_file_no_longer_fatals(tmp_path):
    """The literal regression: settings-manifest.md diverged on disk.

    Named separately from the matrix because this is the case that made the
    macOS install entry point unable to reach exit 0, and a matrix row is easy
    to delete without noticing what it stood for.
    """
    src, dst = _fixture(tmp_path, "settings-manifest.md")
    _install_one(src, dst, False, "machine-local", True)  # must not raise


def test_absent_destination_still_reports_in_check_mode(tmp_path):
    """The asymmetry fix must not have silenced the absent case.

    A destination that does not exist yet is not a preserve-vs-rewrite
    question — live installs it unconditionally, so check must report it.
    Without this, 'check agrees with live' could be satisfied by check
    reporting nothing at all.
    """
    src = tmp_path / "src" / "settings-manifest.md"
    dst = tmp_path / "dst" / "settings-manifest.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("shipped template\n", encoding="utf-8")

    with pytest.raises(SubstrateFatalError, match="absent"):
        _install_one(src, dst, False, "machine-local", True)
