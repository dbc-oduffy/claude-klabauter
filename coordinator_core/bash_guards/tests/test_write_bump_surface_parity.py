"""Two-surface parity test for the write-confinement bump's destination-class
axis (C6).

Spec backlink: pln-narrow-the-write-confinement-b-d10f79, chunk
C6, "Two-surface parity test" (AC8, AC9). DoE's paired decision record ("two
extraction front-ends, one classification stack") is DoE-resident and does
NOT port -- this file, asserting the table itself, is C6's whole deliverable
here.

ONE SHARED TABLE, TWO FRONT-ENDS. The same three targets (a
`publish.mirrors.*` mirror, a foreign sibling source repo, and a path outside
any git repo) are run through:
  - the Bash surface -- `bump_foreign_repo_write.check_bump_foreign_repo_write`
    for the two in-repo targets, `bump_outside_repo_write.
    check_bump_outside_repo_write` for the outside-any-repo target (this
    mirrors production dispatch: which Bash guard fires depends on whether
    the write-sink target resolves to a git repo at all); and
  - the tool surface -- `bump_out_of_repo_tool_write.check`, which already
    collapses foreign/outside into one verdict (module docstring, "SIMPLER
    BY CONSTRUCTION").

Both front-ends already import every classifier from
`_write_bump_applicability` / `_write_bump_marker` / `_write_bump_message`
(confirmed at HEAD, this plan's Substrate re-verification) -- the risk this
file pins is DRIFT between the two call sites' own wiring, not duplicated
classification logic.

NOT VERIFIED AT PORT TIME, and this is the test that establishes it: whether
`resolve_agent_class`/`resolve_effective_types` -- and, after C3/C4/C5 land,
the `destination_class` kwarg each front-end passes to `render_bump_message`
-- agree between the two front-ends for the SAME target. Divergence is a
FINDING, not a test-authoring problem to paper over; the assertions below are
deliberately split (missing-kwarg vs cross-surface-disagreement vs
wrong-value) so a future failure names which of those three it is.

EXPECTED RED PENDING {C3, C4, C5}. `_write_bump_message.DESTINATION_PUBLISH`/
`DESTINATION_FOREIGN` and the `destination_class` keyword on
`render_bump_message` are C2/C4/C5's additions (not landed as of this
chunk -- see the plan's Atomic landing group note: C6 lands BEFORE or WITH
`{C3, C4, C5}`, never after). The import below degrades to local sentinel
placeholders rather than failing collection, so this file stays runnable and
the real assertions below fail with a clear, expected message until that
later wave wires the kwarg through both guard call sites.

Negative-spec:
  - Does NOT implement any of C3/C4/C5's production wiring -- see the plan's
    Anti-scope, "Do not collapse the two extraction front-ends", and this
    chunk's own dispatch brief. This file only asserts the contract.
  - Does NOT weaken the assertions to make the suite green today. A
    currently-red `test_ac8_*` here is the correct, expected state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards import bump_foreign_repo_write as fg_guard
from coordinator_core.bash_guards import bump_outside_repo_write as outside_guard
from coordinator_core.write_guards import bump_out_of_repo_tool_write as tool_guard

try:
    from coordinator_core.bash_guards._write_bump_message import (
        DESTINATION_FOREIGN,
        DESTINATION_PUBLISH,
    )
except ImportError:
    # C2 (destination-axis constants on _write_bump_message) has not landed
    # yet in this working tree -- see module docstring, "EXPECTED RED
    # PENDING {C3, C4, C5}". Sentinels keep this file collectible; the
    # actual assertions below (not this import) are what carries the red.
    DESTINATION_PUBLISH = "__DESTINATION_PUBLISH_NOT_YET_LANDED__"
    DESTINATION_FOREIGN = "__DESTINATION_FOREIGN_NOT_YET_LANDED__"

from coordinator_core.bash_guards.tests.test_bump_outside_repo_write import (
    _clean_bump_env,  # noqa: F401 -- shared isolation fixture, autouse once imported.
)

_MISSING = object()



def _posix(p) -> str:
    """POSIX-slash string form of a path for embedding in a bash
    command-line string -- the tokenizer under test parses commands as
    real bash/POSIX-sh syntax (backslash is an escape character), so a
    native Windows ``str(Path)`` (backslash-separated) embedded directly
    into a ``cmd`` string is not a realistic Bash-tool payload and
    silently corrupts the path once tokenized. Accepts a ``Path`` or a
    plain ``str``."""
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


def _write_registry(reg_dir: Path, mirror_path: str | None = None, mirror_owner: str = "some-owner-em") -> None:
    """Merged-registry fixture for `_all_publish_destinations`/
    `target_is_publish_destination` (C1) -- a real `[publish.mirrors.<key>]`
    nested table, not the flat-string shape that would silently fail to
    parse as a bracket table (per this plan's own D4/`_memo_resolver`
    sibling tests' documented trap)."""
    reg_dir.mkdir(parents=True, exist_ok=True)
    if mirror_path is None:
        return
    escaped = str(mirror_path).replace("\\", "\\\\")
    lines = [
        "[publish.mirrors.testmirror]",
        f'path = "{escaped}"',
        f'owner = "{mirror_owner}"',
    ]
    (reg_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _spy_render(captured: list) -> callable:
    """Replaces `render_bump_message` at a guard module's own call site so
    this test can inspect the kwargs each front-end actually passed --
    specifically the not-yet-existing `destination_class` kwarg C2/C4/C5 add
    -- without depending on the rendered message TEXT, which C2 is free to
    keep rewriting independently of this axis."""

    def _fake(**kwargs):
        captured.append(kwargs)
        return "stub bump message -- test double, never asserted on text"

    return _fake


def _build_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> dict:
    """One shared row-builder for the parametrized table below. `kind` is one
    of the three classes in the plan's own Design table:
    `publish_destination`, `foreign_source`, `outside_any_repo`."""
    home = tmp_path / "home"
    home.mkdir()
    anchor = _init_repo(tmp_path, "anchor")
    reg_dir = tmp_path / "registry"
    session_id = f"sess-parity-{kind}"

    if kind == "publish_destination":
        target_repo = _init_repo(tmp_path, "mirror-target")
        _write_registry(reg_dir, mirror_path=str(target_repo))
        target_file = target_repo / "published.txt"
        expected = DESTINATION_PUBLISH
    elif kind == "foreign_source":
        target_repo = _init_repo(tmp_path, "foreign-target")
        _write_registry(reg_dir)
        target_file = target_repo / "sibling.txt"
        expected = DESTINATION_FOREIGN
    elif kind == "outside_any_repo":
        target_repo = tmp_path / "outside-scratch"
        target_repo.mkdir()
        _write_registry(reg_dir)
        target_file = target_repo / "scratch.txt"
        expected = DESTINATION_FOREIGN
    else:  # pragma: no cover -- guarded by the parametrize list below
        raise ValueError(kind)

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(anchor))

    return {
        "kind": kind,
        "anchor": anchor,
        "target_repo": target_repo,
        "target_file": target_file,
        "session_id": session_id,
        "expected_destination_class": expected,
    }


# ---------------------------------------------------------------------------
# AC8 -- one shared table, identical destination class through both
# front-ends. AC9 -- bump_applies() asserted True before any verdict.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["publish_destination", "foreign_source", "outside_any_repo"])
def test_ac8_bash_and_tool_surfaces_agree_on_destination_class(tmp_path, monkeypatch, kind):
    case = _build_case(tmp_path, monkeypatch, kind)
    anchor = case["anchor"]
    target_repo = case["target_repo"]
    target_file = case["target_file"]
    session_id = case["session_id"]

    # AC9 -- non-negotiable precondition before asserting any guard verdict.
    assert applicability.bump_applies(session_id, cwd=str(anchor)) is True

    # Fixture sanity: C1's own pinned classifier agrees with this row's
    # intended shape before any guard is exercised.
    assert applicability.target_is_publish_destination(str(target_repo)) == (
        kind == "publish_destination"
    )

    bash_captured: list = []
    tool_captured: list = []
    monkeypatch.setattr(fg_guard, "render_bump_message", _spy_render(bash_captured))
    monkeypatch.setattr(outside_guard, "render_bump_message", _spy_render(bash_captured))
    monkeypatch.setattr(tool_guard, "render_bump_message", _spy_render(tool_captured))

    if kind == "outside_any_repo":
        cmd = f"echo hi > {_posix(target_file)}"
        bash_result = outside_guard.check_bump_outside_repo_write(cmd, session_id, str(anchor), {})
    else:
        cmd = f"git -C {_posix(target_repo)} commit --allow-empty -m x"
        bash_result = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(anchor), {})

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target_file)},
        "session_id": session_id,
        "cwd": str(anchor),
        "agent_id": "",
    }
    tool_result = tool_guard.check(payload)

    assert bash_result is not None, f"{kind}: Bash-surface guard did not bump for {target_repo}"
    assert tool_result is not None, f"{kind}: tool-surface guard did not bump for {target_repo}"

    # Fire-vs-no-fire alone (the `is not None` pair above) would not have
    # caught the envelope-class divergence `b280d1116` fixed -- one surface
    # composing a real `permissionDecision: "deny"` while the other only
    # returned `additionalContext`. Compare the actual decision value so the
    # next divergence of this class fails a test, not a live incident.
    bash_decision = bash_result["hookSpecificOutput"]["permissionDecision"]
    tool_decision = tool_result["hookSpecificOutput"]["permissionDecision"]
    assert bash_decision == tool_decision, (
        f"{kind}: DIVERGENCE -- Bash surface's envelope carried "
        f"permissionDecision={bash_decision!r} but the tool surface carried "
        f"{tool_decision!r} for the SAME target."
    )

    assert len(bash_captured) == 1, f"{kind}: expected exactly one Bash-surface render_bump_message call"
    assert len(tool_captured) == 1, f"{kind}: expected exactly one tool-surface render_bump_message call"

    bash_destination_class = bash_captured[0].get("destination_class", _MISSING)
    tool_destination_class = tool_captured[0].get("destination_class", _MISSING)

    assert bash_destination_class is not _MISSING, (
        f"{kind}: Bash-surface render_bump_message call carried no destination_class "
        "kwarg -- expected red until {C3, C4, C5} land (see plan Atomic landing group)."
    )
    assert tool_destination_class is not _MISSING, (
        f"{kind}: tool-surface render_bump_message call carried no destination_class "
        "kwarg -- expected red until {C3, C4, C5} land (see plan Atomic landing group)."
    )
    assert bash_destination_class == tool_destination_class, (
        f"{kind}: DIVERGENCE -- Bash surface resolved destination_class="
        f"{bash_destination_class!r} but the tool surface resolved "
        f"{tool_destination_class!r} for the SAME target. This is the drift C6 "
        "exists to catch, not a test-authoring problem to paper over."
    )
    assert bash_destination_class == case["expected_destination_class"], (
        f"{kind}: destination_class {bash_destination_class!r} does not match the "
        "class target_is_publish_destination() assigns this target."
    )


# ---------------------------------------------------------------------------
# Lessons-outbox parity -- a SEPARATE, focused test rather than a fourth
# `_build_case` row. `_build_case`'s shared assertions (`bash_result is not
# None`, `tool_result is not None`) assume every row BUMPS; the
# lessons-outbox case is the opposite shape (both surfaces must stay
# SILENT), so contorting the shared row-builder/assertion block to also
# express "silent" would blur, not share, the fixture. See dispatch brief:
# "if it does not [accommodate cleanly], add a separate focused parity test
# rather than contorting the builder."
# ---------------------------------------------------------------------------


def test_lessons_outbox_write_silent_on_both_surfaces(tmp_path, monkeypatch):
    """Both surfaces stay SILENT on a foreign-repo `state/lessons-outbox/`
    write (the false-positive `coordinator-lesson-promote` fix, mirrored
    from the tool surface's `_target_is_lessons_outbox_write` onto the Bash
    surface's own `bump_foreign_repo_write._target_is_lessons_outbox_write`
    -- the parity gap named in
    `state/improvement-queue/2026-08-03-bash-surface-write-bump-does-not-
    exempt-c1eb1f482b0f.yaml`)."""
    home = tmp_path / "home"
    home.mkdir()
    anchor = _init_repo(tmp_path, "anchor")
    doe_root = _init_repo(tmp_path, "doe-claude")
    reg_dir = tmp_path / "registry"
    _write_registry(reg_dir)
    session_id = "sess-parity-lessons-outbox"

    lessons_dir = doe_root / "state" / "lessons-outbox"
    lessons_dir.mkdir(parents=True)
    lessons_file = lessons_dir / "some-lesson.yaml"
    lessons_file.write_text("id: x\n", encoding="utf-8")

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(anchor))

    assert applicability.bump_applies(session_id, cwd=str(anchor)) is True

    cmd = f"echo hi > {_posix(lessons_file)}"
    bash_result = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(anchor), {})
    assert bash_result is None, (
        "Bash surface bumped on a foreign-repo state/lessons-outbox write -- "
        "the parity gap this test exists to close."
    )

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(lessons_file)},
        "session_id": session_id,
        "cwd": str(anchor),
        "agent_id": "",
    }
    tool_result = tool_guard.check(payload)
    assert tool_result is None, "tool surface bumped on a foreign-repo state/lessons-outbox write"


# ---------------------------------------------------------------------------
# AC7 -- the `~/.claude` destination class C1 (docs/plans/2026-08-10-carve-
# claude-out-and-close-the-backslash-bypass.md) introduced via
# `target_is_under_claude_home`. A SEPARATE, focused test rather than a
# fourth `_build_case` row, for the same reason the lessons-outbox case
# above is separate: every `_build_case` row assumes the target BUMPS, but
# `~/.claude` is the opposite shape -- both surfaces must stay SILENT (no
# `permissionDecision` envelope at all), so there is no `destination_class`
# value to compare the way AC8 compares one. The parity this test enforces
# is therefore "identical silence, not merely `is not None`" -- both
# surfaces are exercised against the SAME `~/.claude` target and BOTH must
# return `None`, which is the same substance AC8's cross-surface comparison
# has for a class that fires (a divergence here would show up as one
# surface returning an envelope and the other staying silent, exactly the
# shape `b280d1116` fixed on the firing classes).
# ---------------------------------------------------------------------------


def _init_claude_home_repo(home: Path) -> Path:
    """`~/.claude` as a REAL git checkout, mirroring `target_is_under_claude_
    home`'s own docstring ("`~/.claude` IS a real git checkout on this
    machine") -- a fixture that made `~/.claude` a bare directory would not
    exercise the unconditional-on-git-dir-state code path C1 added."""
    claude_home = home / ".claude"
    claude_home.mkdir()
    _git(str(claude_home), "init", "-q")
    _git(str(claude_home), "config", "user.email", "t@example.com")
    _git(str(claude_home), "config", "user.name", "Test")
    (claude_home / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(claude_home), "add", "README.md")
    _git(str(claude_home), "commit", "-q", "-m", "init")
    return claude_home


def test_claude_home_write_silent_on_both_surfaces(tmp_path, monkeypatch):
    """AC7: the `~/.claude` destination class stays SILENT on both the
    foreign-repo Bash guard and the tool guard -- `~/.claude` is a real git
    checkout, so a write into it is otherwise indistinguishable from an
    ordinary foreign-sibling-repo write except for C1's carve-out."""
    home = tmp_path / "home"
    home.mkdir()
    anchor = _init_repo(tmp_path, "anchor")
    claude_home = _init_claude_home_repo(home)
    reg_dir = tmp_path / "registry"
    _write_registry(reg_dir)
    session_id = "sess-parity-claude-home"

    target_file = claude_home / "some-doctrine-file.md"

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(anchor))

    assert applicability.bump_applies(session_id, cwd=str(anchor)) is True
    # Fixture sanity, matching the AC8 rows' own pre-guard sanity check.
    assert applicability.target_is_under_claude_home(str(target_file)) is True

    cmd = f"git -C {_posix(claude_home)} commit --allow-empty -m x"
    bash_result = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(anchor), {})
    assert bash_result is None, (
        "foreign-repo Bash guard bumped a ~/.claude write -- the AC7 parity "
        "this test exists to hold."
    )

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target_file)},
        "session_id": session_id,
        "cwd": str(anchor),
        "agent_id": "",
    }
    tool_result = tool_guard.check(payload)
    assert tool_result is None, (
        "tool guard bumped a ~/.claude write -- the AC7 parity this test "
        "exists to hold."
    )


def test_claude_home_env_call_site_consistency_under_injected_home(tmp_path, monkeypatch):
    """Dispatch-brief probe: C1's three call sites are NOT consistent about
    the `env` parameter threaded into `target_is_under_claude_home` --
    `bump_outside_repo_write.py` passes `env=env` explicitly, while
    `bump_foreign_repo_write.py` and `bump_out_of_repo_tool_write.py` call
    it bare (see `target_is_under_claude_home`'s own docstring, and
    `resolve_launch_anchor`'s docstring for the identical hazard named on
    the anchor side). This is a test that WOULD expose a cross-surface
    disagreement if that inconsistency were live -- not a claim that it is.

    FINDING (do not "fix" the inconsistency this test probes -- that is a
    reviewer's call per this chunk's own dispatch brief): it is NOT
    observable today. `check_bump_foreign_repo_write` and
    `check_bump_outside_repo_write` both resolve `env = os.environ`
    UNCONDITIONALLY, before either ever reaches `target_is_under_claude_
    home` -- there is no call-site-reachable seam on either bash guard that
    threads anything OTHER than live `os.environ` into that predicate, and
    `bump_out_of_repo_tool_write.check` never accepts an `env` override at
    all. The bare-vs-`env=env` shape difference is therefore a LATENT
    hazard for some future caller that threads a distinct env mapping
    through these functions, not a live divergence -- this test pins that
    by injecting the only env-mutation seam any of the three checkers
    currently honours (`monkeypatch.setenv`, which mutates the single
    shared `os.environ` all three read) and confirming all three surfaces
    still agree, including when the injected `HOME` does NOT match the
    session anchor's own home (the case that would surface a divergence
    first, since the exemption stops firing everywhere at once rather than
    on only one surface)."""
    home = tmp_path / "home"
    home.mkdir()
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    anchor = _init_repo(tmp_path, "anchor")
    claude_home = _init_claude_home_repo(home)
    reg_dir = tmp_path / "registry"
    _write_registry(reg_dir)
    session_id = "sess-parity-claude-home-env"

    target_file = claude_home / "some-doctrine-file.md"

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    session_start.write_session_start_record(session_id, launch_cwd=str(anchor))

    # HOME injected to a DIFFERENT directory than the one `claude_home` was
    # built under -- `target_is_under_claude_home` must now resolve `False`
    # everywhere, since none of the three checkers' `os.environ` disagrees
    # with any other's.
    monkeypatch.setenv("HOME", str(other_home))

    assert applicability.bump_applies(session_id, cwd=str(anchor)) is True
    assert applicability.target_is_under_claude_home(str(target_file)) is False

    cmd = f"git -C {_posix(claude_home)} commit --allow-empty -m x"
    bash_result = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(anchor), {})
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target_file)},
        "session_id": session_id,
        "cwd": str(anchor),
        "agent_id": "",
    }
    tool_result = tool_guard.check(payload)

    # Both surfaces must now BUMP (the ~/.claude exemption no longer applies
    # against the injected HOME) -- and, per this test's own docstring, they
    # do: no observable disagreement from the env-threading inconsistency
    # under the only injection seam available to a black-box caller.
    assert bash_result is not None, (
        "foreign-repo Bash guard stayed silent despite the injected HOME no "
        "longer matching claude_home -- would mask a cross-surface "
        "disagreement this probe exists to catch."
    )
    assert tool_result is not None, (
        "tool guard stayed silent despite the injected HOME no longer "
        "matching claude_home -- would mask a cross-surface disagreement "
        "this probe exists to catch."
    )
    bash_decision = bash_result["hookSpecificOutput"]["permissionDecision"]
    tool_decision = tool_result["hookSpecificOutput"]["permissionDecision"]
    assert bash_decision == tool_decision, (
        f"DIVERGENCE under injected HOME -- Bash surface carried "
        f"permissionDecision={bash_decision!r} but tool surface carried "
        f"{tool_decision!r} for the SAME ~/.claude target."
    )


def test_ordinary_foreign_write_still_bumps_on_both_surfaces(tmp_path, monkeypatch):
    """Sanity companion to the lessons-outbox exemption above -- an ORDINARY
    foreign-repo write (no `state/lessons-outbox` in its path) must still
    bump on both surfaces. Guards against an over-widened exemption
    accidentally silencing everything under a foreign repo."""
    home = tmp_path / "home"
    home.mkdir()
    anchor = _init_repo(tmp_path, "anchor")
    foreign_root = _init_repo(tmp_path, "foreign-target")
    reg_dir = tmp_path / "registry"
    _write_registry(reg_dir)
    session_id = "sess-parity-lessons-outbox-control"

    ordinary_file = foreign_root / "sibling.txt"

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record(session_id, launch_cwd=str(anchor))

    assert applicability.bump_applies(session_id, cwd=str(anchor)) is True

    cmd = f"git -C {_posix(foreign_root)} commit --allow-empty -m x"
    bash_result = fg_guard.check_bump_foreign_repo_write(cmd, session_id, str(anchor), {})
    assert bash_result is not None, "Bash surface failed to bump on an ordinary foreign-repo write"

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(ordinary_file)},
        "session_id": session_id,
        "cwd": str(anchor),
        "agent_id": "",
    }
    tool_result = tool_guard.check(payload)
    assert tool_result is not None, "tool surface failed to bump on an ordinary foreign-repo write"
