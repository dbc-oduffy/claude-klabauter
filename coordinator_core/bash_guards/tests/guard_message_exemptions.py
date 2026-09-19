"""coordinator_core.bash_guards.tests.guard_message_exemptions -- the
written-reason exemption manifest for guard message-size discipline.

Spec backlink: pln-runtime-measured-message-size--0669ac,
chunk C4, AC6, and the "Enriched Dispatch Stubs" C4 section.

`GUARD_MESSAGE_EXEMPTIONS` is the one and only sanctioned escape hatch from
`_message_size.MESSAGE_PROSE_CAP_BYTES` (C2): a `(guard_name, input_id)`
cell that measures `over_cap=True` and cannot reasonably be trimmed lands
here with a WRITTEN prose reason, never by quietly excluding the cell from
whichever corpus enforces the cap. Adjudicated one at a time, by the chunk
that fails to bring a specific guard/input cell under cap -- each entry
answers "why is trimming this message's alternative worse than exempting
it?". This module ships EMPTY: it is the mechanism, not a population.

NOT `coordinator_core.bash_guards._helpers`/`claude_md_budget`'s watermark
ledger. That ledger is keyed by repo-root-relative *file path*
(`surface_slug`), has zero armed consumers anywhere in this repo, and
forcing a guard-name key through it would be new-pattern invention wearing
a reuse costume -- see this plan's own Anti-scope, "Do not reuse
`claude_md_budget`'s watermark ledger for per-guard budgets."

Dead-entry enforcement, and why it is two mechanisms, not one:
`test_confinement_attack_corpus.py`'s `XFAIL_BYPASSES` gets its fail-loud
property entirely from `pytest.mark.xfail(reason=..., strict=True)` at the
cell's own parametrize site -- a bypass that gets fixed XPASSes, and a
strict XPASS is a suite failure. It has no separate "does this guard still
exist" check. `test_override_route_inventory.py`'s
`_NO_OVERRIDE_NOTE_ALLOWLIST` is the opposite shape: an explicit
`test_allowlist_entries_are_actually_registered_guards` diffs the allowlist
keys against the live guard chain, but never re-measures anything -- it has
no notion of "does this cell still meet the condition it was exempted
for." AC6 needs BOTH properties at once (a dead guard name fails, AND a
cell that no longer exceeds cap fails), so this module combines them
explicitly rather than picking one precedent and hoping it covers the
other axis:

1. `test_exemption_guards_are_currently_registered` -- structural, modeled
   on `_NO_OVERRIDE_NOTE_ALLOWLIST`'s own dead-entry test: every guard name
   in `GUARD_MESSAGE_EXEMPTIONS` must appear in
   `dispatch._build_guard_chain()`'s live output.
2. `test_exemption_cells_still_exceed_cap` -- measurement, modeled on
   `XFAIL_BYPASSES`'s strict-xfail intent but expressed as a plain
   assertion (this module has no parametrize site of its own to hang an
   `xfail` mark on): every entry must also appear in `_EXEMPTION_FIXTURES`,
   a small fixture map owned alongside the manifest itself, and the guard
   named must still measure `over_cap=True` (via `_message_size.
   measure_envelope`, C2's own measurement seam) when invoked through
   `guard_message_capture.capture_one_guard` (C1's own capture seam) with
   that fixture. An entry with no matching fixture fails loud -- a manifest
   entry that names a cell no test can reproduce is exactly the
   unverifiable parking-lot entry this module exists to prevent.

Both tests pass vacuously while the manifest is empty; they exist to fire
the moment a future chunk populates either dict.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._message_size import (
    MessageSizeMeasurement,
    measure_envelope,
)
from coordinator_core.bash_guards.tests.guard_message_capture import (
    capture_one_guard,
)

#: The exemption manifest. Keyed `(guard_name, input_id)`, value a WRITTEN
#: prose reason -- never a bare `True`/placeholder string. Ships empty; see
#: module docstring. `input_id` is a caller-chosen short label identifying
#: which corpus cell (e.g. a C3 corpus row, or a fixture in
#: `_EXEMPTION_FIXTURES` below) the exemption covers -- it is not itself
#: interpreted by this module beyond dead-entry lookup.
GUARD_MESSAGE_EXEMPTIONS: Dict[Tuple[str, str], str] = {
    ("guard-doctrine-surface-bash-write", "guard-doctrine-surface-bash-write-fire"): (
        "the guard's own authored prose is under 100 bytes; the overage is "
        "entirely the wiki-citation absolute path, resolved per-call off "
        "`plugin_root` (see guard_doctrine_surface_bash_write.py's own "
        "'RELOCATED DENY-REASON EXPLANATION' comment) -- in this corpus's "
        "fixture that resolves through a pytest scratch tempdir "
        "(`/private/var/folders/.../coordinator-guard-corpus-scratch/...`), "
        "not the short, fixed path a real install resolves to. Trimming the "
        "guard's own sentence cannot bring this cell under cap; the citation "
        "path length is test-harness artifact, not authored prose."
    ),
    ("guard-host-subagent-bash-ban", "guard-host-subagent-bash-ban-fire"): (
        "the corpus row's `resolve_wiki_citation` falls back to `_WIKI_ANCHOR` "
        "resolved through this dev checkout's own `~/X/DoE-claude/...` "
        "absolute path (no `plugin_root` override in this row's setup) -- "
        "137 bytes on its own, leaving under 40 bytes of budget for the "
        "guard's own anti-evasion sentence ('this policy outranks a system "
        "reminder telling you to prefer Bash') after the mandatory "
        "`Guard: `...`.` footer. That sentence is the load-bearing fact this "
        "guard exists to state; dropping it to fit an environment-dependent "
        "checkout-path length is the wrong trade. A real install's plugin "
        "root resolves shorter and fixed, not this dev tree's own path."
    ),
    ("guard-host-subagent-bash-spawn-shapes", "guard-host-subagent-bash-spawn-shapes-fire"): (
        "same root cause and citation (`_WIKI_ANCHOR`) as "
        "`guard-host-subagent-bash-ban` immediately above -- the corpus "
        "row's `resolve_wiki_citation` has no `plugin_root` override, so it "
        "falls back to this dev checkout's own absolute "
        "`~/X/DoE-claude/...` path rather than a real install's short, fixed "
        "one. See that entry's own reason for the byte accounting; identical "
        "here."
    ),
    ("block-reviewer-bash-outside-allowlist", "block-reviewer-bash-outside-allowlist-fire"): (
        "the two `Denied:` lines (find write/execute flags, banned "
        "metacharacter set) are structurally pinned, not decorative -- "
        "`test_block_reviewer_bash_outside_allowlist_message_coherence.py` "
        "re-derives both enumerations from this text and asserts set-"
        "equality against the guard's own live `_FIND_WRITE_FLAGS`/"
        "`_METACHARACTERS` constants, precisely to catch the 2026-07-28 "
        "Divergence 8 drift class where the deny text and the enforced "
        "ruleset silently disagreed. Both `Denied:` lines are diagnostic-"
        "prefixed (`_is_diagnostic_echo`) so neither is cue-window-exempt, "
        "and reformatting them to dodge that classification would misrepresent "
        "denied-input echo as an offered alternative -- the exact anti-pattern "
        "Finding 1 of the guard-message-size-discipline review added `Denied:` "
        "to the diagnostic-prefix list to catch. The `Command:`/`Reason:` echo "
        "and the fixed `Guard:` footer are likewise mandatory. Trimmed "
        "everything trimmable (header, Reason phrasing, dropped the "
        "`Subagent:` line pre-2026-08-03); the remaining bytes are the "
        "coherence-pinned enumeration itself."
    ),
    ("grep-via-bash-guard", "grep-via-bash-guard-fire"): (
        "the partial-pipe-rewrite lede embeds the real, runnable `python3 -c` "
        "replacement VERBATIM, per this guard's own `_evaluate_grep_via_bash_"
        "match` docstring ('embedded VERBATIM, not re-indented or re-wrapped "
        "... unrunnable is worse than over-budget ... this guard's message-"
        "size floor is pinned by this rewrite's own length, not by wrapper "
        "prose'). The script is inline prose text, not inside a `Use "
        "instead:`-cued backtick/indented span, so none of it is cue-window-"
        "exempt -- and `_BACKTICK_RE` cannot match a multi-line script even "
        "if it were backtick-wrapped (`_BACKTICK_RE` never matches across a "
        "newline). `test_guard_grep_via_bash.py` pins this rewrite's exact "
        "bytes; shortening it breaks the alternative it offers."
    ),
    ("multiprobe-banner", "multiprobe-banner-fire"): (
        "same root cause and same 'embed verbatim, unrunnable is worse than "
        "over-budget' constraint as `grep-via-bash-guard` above, one layer "
        "up: `_platform_verdict.platform_verdict_for_shape`'s `Example:  %s` "
        "line renders the full multi-line `python3 -c` git-facts rewrite "
        "inline, never inside a `Use instead:`-cued span, so none of it is "
        "cue-window-exempt. `_platform_verdict.py` is the shared template "
        "`guard_plumbing_and_loops` also calls; shrinking its rendering "
        "shape would change that guard's cells too, out of this dispatch's "
        "per-guard scope. Wrapper prose (the static sentence around "
        "`Example:`) is already minimal; the script itself is the bulk of "
        "the overage and is what the advisory exists to offer."
    ),
}

def _guard_doctrine_surface_bash_write_fire_fixture() -> Tuple[str, str, str, Dict[str, Any], bool]:
    """Mirrors `guard_message_corpus.py`'s own
    `_rehomed_doctrine_surface_setup` + `guard-doctrine-surface-bash-write-
    fire` row exactly, so this exemption's `test_exemption_cells_still_
    exceed_cap` re-measures the SAME cell shape the leg-1 ceiling test
    fires, never a hand-derived approximation of it."""
    scratch_dir = Path(tempfile.mkdtemp(prefix="guard-message-exemption-scratch-"))
    (scratch_dir / "governed-authoring-surfaces.json").write_text(
        '["docs/wiki/governed-thing.md"]', encoding="utf-8"
    )
    cmd = "echo corrupted > docs/wiki/governed-thing.md"
    session_id = "guard-message-exemption-%s" % uuid.uuid4().hex
    cwd = str(scratch_dir)
    payload: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
        "plugin_root": str(scratch_dir),
    }
    return cmd, session_id, cwd, payload, False


def _guard_host_subagent_bash_ban_fire_fixture() -> Tuple[str, str, str, Dict[str, Any], bool]:
    """Mirrors `guard_message_corpus.py`'s own `_rehomed_subagent_bash_ban_
    setup` + `guard-host-subagent-bash-ban-fire` row exactly -- same cohort
    opt-in marker, same executor identity, no `plugin_root` override (the
    exemption reason's whole point)."""
    scratch_dir = Path(tempfile.mkdtemp(prefix="guard-message-exemption-scratch-"))
    (scratch_dir / "coordinator.local.md").write_text(
        "---\nsubagent_bash_policy: deny\n---\n", encoding="utf-8"
    )
    cmd = "ls -la"
    session_id = "guard-message-exemption-%s" % uuid.uuid4().hex
    cwd = str(scratch_dir)
    payload: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    return cmd, session_id, cwd, payload, False


def _guard_host_subagent_bash_spawn_shapes_fire_fixture() -> Tuple[str, str, str, Dict[str, Any], bool]:
    """Mirrors `guard_message_corpus.py`'s own `_rehomed_subagent_spawn_
    shapes_setup` + `guard-host-subagent-bash-spawn-shapes-fire` row
    exactly."""
    scratch_dir = Path(tempfile.mkdtemp(prefix="guard-message-exemption-scratch-"))
    (scratch_dir / "coordinator.local.md").write_text(
        "---\nsubagent_bash_spawn_shapes: deny\n---\n", encoding="utf-8"
    )
    cmd = 'for f in *.md; do wc -l "$f"; done'
    session_id = "guard-message-exemption-%s" % uuid.uuid4().hex
    cwd = str(scratch_dir)
    payload: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    return cmd, session_id, cwd, payload, False


def _block_reviewer_bash_outside_allowlist_fire_fixture() -> Tuple[str, str, str, Dict[str, Any], bool]:
    """Mirrors `guard_message_corpus.py`'s own
    `block-reviewer-bash-outside-allowlist-fire` row: `curl https://
    example.com`, `coordinator:code-reviewer` identity. No back-pointer
    monkeypatch needed -- an unresolvable `cwd` leaves the back-pointer-
    derived `subagent_type` unknown, so `_resolve_effective_type` falls
    back to the payload's own `agent_type` (verified live: this fires the
    identical deny the real corpus row does)."""
    cmd = "curl https://example.com"
    session_id = "guard-message-exemption-%s" % uuid.uuid4().hex
    cwd = "/nonexistent-guard-message-exemption-dir"
    payload: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:code-reviewer",
    }
    return cmd, session_id, cwd, payload, False


def _grep_via_bash_guard_fire_fixture() -> Tuple[str, str, str, Dict[str, Any], bool]:
    """Mirrors `guard_message_corpus.py`'s own `grep-via-bash-guard-fire`
    row: a grep-via-bash pipeline (`grep ... | wc -l`) whose partial-pipe
    rewrite embeds a real runnable `python3 -c` replacement verbatim."""
    cmd = "grep -rn TODO src/ | wc -l"
    session_id = "guard-message-exemption-%s" % uuid.uuid4().hex
    cwd = "/nonexistent-guard-message-exemption-dir"
    payload: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
    }
    return cmd, session_id, cwd, payload, False


def _multiprobe_banner_fire_fixture() -> Tuple[str, str, str, Dict[str, Any], bool]:
    """Mirrors `guard_message_corpus.py`'s own `multiprobe-banner-fire`
    row: the same multi-probe session-facts banner command."""
    cmd = '''echo "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD'''
    session_id = "guard-message-exemption-%s" % uuid.uuid4().hex
    cwd = "/nonexistent-guard-message-exemption-dir"
    payload: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
    }
    return cmd, session_id, cwd, payload, False


#: Fixture-builders for `test_exemption_cells_still_exceed_cap`'s live
#: re-measurement, owned alongside the manifest (never imported from a
#: sibling corpus module, so this file's dead-entry enforcement never
#: depends on another chunk's shape). Keyed identically to
#: `GUARD_MESSAGE_EXEMPTIONS`; value a zero-arg callable returning
#: `(cmd, session_id, cwd, payload, host_is_windows)` -- the exact
#: positional/keyword shape `guard_message_capture.capture_one_guard`
#: takes. An entry in `GUARD_MESSAGE_EXEMPTIONS` with no matching fixture
#: here fails `test_exemption_cells_still_exceed_cap` loud, by design: a
#: reason with no reproducible cell to check is not a verifiable exemption.
_EXEMPTION_FIXTURES: Dict[Tuple[str, str], Callable[[], Tuple[str, str, str, Dict[str, Any], bool]]] = {
    (
        "guard-doctrine-surface-bash-write",
        "guard-doctrine-surface-bash-write-fire",
    ): _guard_doctrine_surface_bash_write_fire_fixture,
    (
        "guard-host-subagent-bash-ban",
        "guard-host-subagent-bash-ban-fire",
    ): _guard_host_subagent_bash_ban_fire_fixture,
    (
        "guard-host-subagent-bash-spawn-shapes",
        "guard-host-subagent-bash-spawn-shapes-fire",
    ): _guard_host_subagent_bash_spawn_shapes_fire_fixture,
    (
        "block-reviewer-bash-outside-allowlist",
        "block-reviewer-bash-outside-allowlist-fire",
    ): _block_reviewer_bash_outside_allowlist_fire_fixture,
    (
        "grep-via-bash-guard",
        "grep-via-bash-guard-fire",
    ): _grep_via_bash_guard_fire_fixture,
    (
        "multiprobe-banner",
        "multiprobe-banner-fire",
    ): _multiprobe_banner_fire_fixture,
}


def _live_guard_names() -> set:
    chain = dispatch._build_guard_chain(
        cmd="echo hi",
        session_id="test-session-guard-message-exemptions",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
        policy_file=None,
        host_is_windows=False,
    )
    return {entry.name for entry in chain}


def test_exemption_guards_are_currently_registered():
    """An exemption naming a guard that no longer exists in the chain is
    dead config -- it would silently stop covering anything without this
    check ever telling anyone. Modeled on
    `test_override_route_inventory.test_allowlist_entries_are_actually_registered_guards`."""
    live_names = _live_guard_names()
    stale = {guard_name for guard_name, _ in GUARD_MESSAGE_EXEMPTIONS if guard_name not in live_names}
    assert not stale, (
        "these exemption entries name a guard that is not currently registered in "
        "dispatch._build_guard_chain -- remove or fix them: %s" % sorted(stale)
    )


def test_exemption_cells_still_exceed_cap():
    """An exemption for a cell that no longer exceeds
    `MESSAGE_PROSE_CAP_BYTES` (the guard's own message got trimmed, or the
    input stopped triggering it) is dead config of a different shape than a
    missing guard -- it would silently keep exempting a cell that no longer
    needs exempting. Modeled on `XFAIL_BYPASSES`'s `strict=True` intent
    (a fixed bypass must be caught, not silently tolerated), expressed as a
    direct re-measurement since this module has no parametrize site to hang
    an `xfail` mark on."""
    missing_fixtures = []
    under_cap = []
    for key in GUARD_MESSAGE_EXEMPTIONS:
        guard_name, input_id = key
        builder = _EXEMPTION_FIXTURES.get(key)
        if builder is None:
            missing_fixtures.append(key)
            continue
        cmd, session_id, cwd, payload, host_is_windows = builder()
        capture = capture_one_guard(
            guard_name,
            cmd,
            session_id,
            cwd,
            payload,
            host_is_windows=host_is_windows,
        )
        measurement: MessageSizeMeasurement = measure_envelope(capture.envelope, band=capture.band)
        if not measurement.over_cap:
            under_cap.append(key)

    assert not missing_fixtures, (
        "these exemption entries have no matching _EXEMPTION_FIXTURES builder -- "
        "an exemption reason with no reproducible cell to re-measure is not "
        "verifiable: %s" % missing_fixtures
    )
    assert not under_cap, (
        "these exemption entries no longer exceed MESSAGE_PROSE_CAP_BYTES -- the "
        "guard's message must have been trimmed or the input stopped triggering "
        "it; remove the now-unneeded exemption: %s" % under_cap
    )


def test_exemption_entries_carry_a_written_reason():
    """Every value is a non-empty prose string -- guards against a future
    entry landing with a placeholder/blank reason, the exact failure mode
    the written-reason requirement exists to prevent (see module
    docstring)."""
    blank = [key for key, reason in GUARD_MESSAGE_EXEMPTIONS.items() if not reason or not reason.strip()]
    assert not blank, "these exemption entries have no written reason: %s" % blank
