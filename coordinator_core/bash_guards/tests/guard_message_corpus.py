"""coordinator_core.bash_guards.tests.guard_message_corpus -- the
per-(guard, input) trigger corpus feeding C5's three-leg message-size gate.

Spec backlink: pln-runtime-measured-message-size--0669ac,
chunk C3, § Problem's "Correction: the capture seam must attribute bytes"
and Anti-scope's "Do not build the corpus from triggering inputs only."

ROW SCHEMA -- pinned here for every downstream chunk that appends rows to
this module (C3's advisory/platform rows, C10's DR-118 shim rows):

    CorpusRow(guard, row_id, input, expected_speaker, band, host_is_windows,
              setup=None)

  - ``guard``: the registered `dispatch.GuardEntry.name` (or a directory-
    derived proxy name for a write_guards/hooks row that carries no
    `GuardEntry` at all -- not needed by this chunk's 16 confinement rows,
    all of which are real `dispatch.py` registrations).
  - ``row_id``: a corpus-unique string identifying this cell (not just the
    guard name -- a guard contributes MULTIPLE rows: at least one firing,
    at least one non-firing, per Anti-scope's "non-triggering cells are
    REQUIRED, not optional").
  - ``input``: the command text this cell probes. For a row with no
    ``setup``, this is fired VERBATIM. For a row WITH a ``setup`` (a
    dynamic fixture -- e.g. a real git repo whose path cannot be a module-
    level literal), ``input`` is DOCUMENTATION ONLY (a ``<repo>``-templated
    example of the shape actually fired) -- the real command text is
    resolved at fire time by ``setup`` returning a ``_CMD_OVERRIDE_KEY``
    entry (see ``fire_row`` below). This keeps the corpus module itself
    side-effect-free on import (no git-repo construction, no monkeypatching)
    while still letting a row's *real* fired text depend on a fixture that
    can only exist at fire time.
  - ``expected_speaker``: whether this cell should CAUSE the guard to speak
    (produce a non-``None`` envelope with a deny reason) when fired. This
    chunk's 16 guards are single-band CONFINEMENT_DENY hard-denies with no
    suppression/rewrite leg, so "speaks" here means "denies" -- C2's own
    ``prose_bytes > 0`` speaker predicate is a stricter definition this
    chunk does not need to depend on to validate its own 16 rows (see
    ``test_expected_speaker_matches_measured_reality`` below).
  - ``band``: the `dispatch.GuardBand` this guard is registered under.
  - ``host_is_windows``: pinned explicitly per row (AC15) -- every row in
    this chunk pins ``False`` (none of the 16 confinement guards is
    platform-conditioned; the two `PLATFORM_CONDITIONED_DENY` guards,
    `multiprobe-banner`/`plumbing-and-loops`, are C3's advisory/platform
    rows, not this chunk's).
  - ``setup``: optional ``(scratch_dir, monkeypatch) -> Dict[str, str]``
    callable, invoked at FIRE time (never at import time) inside a fresh
    per-cell scratch directory and a fresh ``pytest.MonkeyPatch`` context.
    Its return dict is merged into the fired payload; two reserved keys are
    consumed by ``fire_row`` itself rather than passed through:
    ``_CMD_OVERRIDE_KEY`` (overrides the fired command text) and
    ``_CWD_OVERRIDE_KEY`` (overrides the fired ``cwd``). Every other key
    (e.g. ``"agent_id"``/``"agent_type"`` for an identity-gated guard) is
    merged flatly into the payload dict, exactly the shape
    ``_build_guard_chain``'s closures read (see
    ``block_subagent_commit.py`` et al. reading ``payload["agent_id"]``).

Per-cell isolation is load-bearing, not hygiene (state/lessons/2026-08-01-
adding-suppression-to-an-emitter-silently-breaks-*): ``fire_row`` mints a
FRESH session id and a FRESH scratch tempdir per call, following
``_alternative_liveness.fire_guard``'s ``_isolated_session_scope`` shape
and ``_guard_coverage.measure_probe_spray``'s per-run scratch-tempdir
pattern -- combined here at PER-CELL granularity (stricter than either
individual precedent), so a later row in the same test session never
observes another row's monkeypatch state, working directory, or any
session-scoped suppression latch. ``guard_inprocess_search._footer()`` is
the loudest example in this tree: its latch keys off the process
environment variable ``CLAUDE_CODE_SESSION_ID`` (never
``payload["session_id"]``), so ``fire_row`` also sets that env var to the
freshly-minted session id via the per-cell ``pytest.MonkeyPatch`` context
-- a fresh ``payload["session_id"]`` alone would NOT isolate this guard's
latch. This chunk's own 16 ``CONFINEMENT_ROWS`` never fire that guard;
``ADVISORY_REWRITE_ROWS``'s ``inprocess-search-fire``/``-control`` rows
do, and rely on this env-var isolation.

Capture seam: this module invokes guards ONLY through
``guard_message_capture.capture_one_guard`` (C1), never through
``dispatch._decision`` or `dispatch.evaluate_payload_json`'s short-
circuiting loop -- per Anti-scope's "Do not treat `_decision` as the
capture seam." Base command TEXT is pulled from each guard's own
``_setup_<name>`` factory in ``test_confinement_attack_corpus.py`` (the
SSOT for these 16 commands), by calling the factory (for its side effects
and its exact ``base_cmd`` return) and discarding the factory's own
``_decision``-bound closure -- only the plain string survives into this
module's corpus rows.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._message_size import proxy_band
from coordinator_core.bash_guards.tests.guard_message_capture import (
    GuardCapture,
    capture_one_guard,
)
from coordinator_core.bash_guards.tests.test_confinement_attack_corpus import (
    CONFINEMENT_GUARDS,
    GUARD_NAMES,
    _GUARD_SETUP,
    _build_load_bearing_repo,
)
from coordinator_core.hooks import em_report_altitude as _hook_em_report_altitude
from coordinator_core.hooks import (
    nudge_harness_directive_dispatch as _hook_nudge_harness_directive_dispatch,
)
from coordinator_core.hooks import nudge_unrouted_sizing as _hook_nudge_unrouted_sizing
from coordinator_core.ops import peer_notice_send as _ops_peer_notice_send
from coordinator_core.write_guards import engine as write_guards_engine

# ---------------------------------------------------------------------------
# Reserved `setup` return keys, consumed by `fire_row` and never forwarded
# into the fired payload.
# ---------------------------------------------------------------------------

_CMD_OVERRIDE_KEY = "__cmd__"
_CWD_OVERRIDE_KEY = "__cwd__"

#: Shared identity payloads -- same literals `test_cd_prefix_bypass.py`'s
#: own `_SUBAGENT_IDENTITY` and `TestReviewerBashOutsideAllowlistBypass`
#: use, duplicated here (not imported) because the plan's Anti-scope keeps
#: this corpus decoupled from that file's own `_decision`-bound helpers --
#: only its `_setup_<name>` factories are a shared dependency.
_EXECUTOR_IDENTITY: Dict[str, str] = {
    "agent_id": "deadbeef0123",
    "agent_type": "coordinator:executor",
}
_REVIEWER_IDENTITY: Dict[str, str] = {
    "agent_id": "deadbeef0123",
    "agent_type": "coordinator:code-reviewer",
}


@dataclass(frozen=True)
class CorpusRow:
    """One probed `(guard, input)` cell. See module docstring for the full
    schema this dataclass encodes."""

    guard: str
    row_id: str
    input: str
    expected_speaker: bool
    #: `dispatch.GuardBand` for a real bash_guards registration (C3a/C3b
    #: rows), or a plain `_message_size.proxy_band(...)`-derived `str` for a
    #: write_guards/hooks row with no `GuardBand` at all (C3c rows) -- see
    #: that helper's own docstring: a directory bucket, never dressed up as
    #: a duty-of-care classification.
    band: Union[dispatch.GuardBand, str]
    host_is_windows: bool
    setup: Optional[Callable[[Path, pytest.MonkeyPatch], Dict[str, str]]] = field(
        default=None
    )
    #: AUDIENCE AXIS (chunk C6, docs/plans/2026-08-13-guard-messages-stop-
    #: handing-agents-the-keys.md): `None` (every existing row, unchanged --
    #: backward-compatible default so no existing `CorpusRow(...)` call site
    #: needs editing) means "as-authored" -- whatever identity `row.setup`
    #: itself supplies (absent for most rows, which is EM-audience by
    #: `session.identity.resolves_em_audience`'s own "well-formed envelope,
    #: both legs empty -> True" contract; `_EXECUTOR_IDENTITY`/
    #: `_REVIEWER_IDENTITY` for the handful of identity-gated rows, which is
    #: subagent-audience). `fire_row_for_audience` below FORCES a row's
    #: identity to one of the two explicit values regardless of this field
    #: or the row's own `setup`, for a row that needs both audiences proven
    #: independent of its authored identity -- see that function's own
    #: docstring for why this is a firing-time override, not a second field
    #: consulted by `fire_row` itself (which stays audience-agnostic,
    #: unchanged).
    audience: Optional[str] = None


def _from_factory(setup_name: str) -> Callable[[Path, pytest.MonkeyPatch], Dict[str, str]]:
    """A `CorpusRow.setup` that invokes `test_confinement_attack_corpus.py`'s
    own `_setup_<name>` factory for its side effects and its exact
    `base_cmd` return, overriding the fired command with that string --
    per the module docstring's "pull base_cmd from the factory" contract.
    """
    factory = _GUARD_SETUP[setup_name]

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        _decide_fn, base_cmd = factory(scratch_dir, mp)
        return {_CMD_OVERRIDE_KEY: base_cmd}

    return setup


def _from_factory_with_identity(
    setup_name: str, identity: Dict[str, str]
) -> Callable[[Path, pytest.MonkeyPatch], Dict[str, str]]:
    """Like `_from_factory`, plus the flat `agent_id`/`agent_type` payload
    fields an identity-gated guard's closure reads directly off `payload`
    -- the factory itself only wires the MODULE-level identity-resolution
    monkeypatch (`_wire_subagent_identity`), it does not supply these
    payload fields; that is `_decision(cmd, **_SUBAGENT_IDENTITY)`'s job in
    the source file, which this corpus does not call.
    """
    base = _from_factory(setup_name)

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        extra = base(scratch_dir, mp)
        extra.update(identity)
        return extra

    return setup


def _control_from_factory(
    setup_name: str, control_cmd: str, identity: Optional[Dict[str, str]] = None
) -> Callable[[Path, pytest.MonkeyPatch], Dict[str, str]]:
    """A non-firing sibling of `_from_factory`/`_from_factory_with_identity`:
    still runs the factory's side effects (so an identity-gated or
    fixture-needing guard resolves correctly), but fires a caller-supplied
    benign command instead of the factory's own denying `base_cmd`."""
    factory = _GUARD_SETUP[setup_name]

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        factory(scratch_dir, mp)  # side effects only; base_cmd discarded
        extra: Dict[str, str] = {_CMD_OVERRIDE_KEY: control_cmd}
        if identity:
            extra.update(identity)
        return extra

    return setup


def _git_repo_setup(cmd_template: str) -> Callable[[Path, pytest.MonkeyPatch], Dict[str, str]]:
    """`destructive-git-clean`/`destructive-git-revert` need a REAL git
    repository with load-bearing tracked/untracked state -- their factories
    build one internally, but embed its path directly into the returned
    `base_cmd` string, so there is no separate `repo` value to reuse for a
    differently-shaped control cell. This builds the same fixture directly
    (`_build_load_bearing_repo`, the exact helper both factories call) and
    formats `cmd_template % repo` -- used for both the firing and the
    non-firing row of each of these two guards.
    """

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        repo = _build_load_bearing_repo(scratch_dir)
        return {
            _CMD_OVERRIDE_KEY: cmd_template % repo,
            _CWD_OVERRIDE_KEY: str(repo),
        }

    return setup


def _build_advisory_only_repo(scratch_dir: Path) -> Path:
    """A real git repo with an uncommitted tracked edit OUTSIDE any
    load-bearing prefix (`_is_loadbearing`'s own `state/`-rooted check) and
    no peer session's claim on it -- the shape `check_destructive_git_
    revert`'s advisory floor (2026-08-05) exists for: `affected` non-empty,
    `deny_paths` empty. Deliberately NOT `_build_load_bearing_repo` (that
    fixture anchors its tracked file under `state/`, exactly the prefix
    this row must avoid to stay in the advisory, not the deny, branch).
    """
    repo = scratch_dir / "advisory-only-repo"
    repo.mkdir()
    _git = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=str(repo), check=True, capture_output=True
    )
    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    tracked = repo / "app.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    _git("add", "app.py")
    _git("commit", "-qm", "baseline")
    tracked.write_text("x = 2\n", encoding="utf-8")
    return repo


def _git_repo_advisory_setup(cmd_template: str) -> Callable[[Path, pytest.MonkeyPatch], Dict[str, str]]:
    """`destructive-git-revert-advisory`'s own setup: same `cmd_template %
    repo` shape as `_git_repo_setup`, over `_build_advisory_only_repo`
    instead of `_build_load_bearing_repo`."""

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        repo = _build_advisory_only_repo(scratch_dir)
        return {
            _CMD_OVERRIDE_KEY: cmd_template % repo,
            _CWD_OVERRIDE_KEY: str(repo),
        }

    return setup


def _bump_confinement_anchor_repo(scratch_dir: Path) -> Path:
    """A real, minimal git repo to anchor a write-confinement bump firing
    row -- `docs/plans/2026-08-03-narrow-write-confinement-bump.md` chunk
    C2's own real-repo firing precedent, lighter than
    `test_bump_foreign_repo_write.py`'s `_set_anchor` (which needs a real
    settings-home `write_session_start_record` keyed on a session id
    `fire_row` mints AFTER `row.setup` runs, so this corpus cannot pre-write
    that record) -- `bump_applies`/`resolve_launch_anchor` are monkeypatched
    directly on each guard's own module instead (see the two setups below)."""
    anchor = scratch_dir / "anchor"
    anchor.mkdir()
    _git = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=str(anchor), check=True, capture_output=True
    )
    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (anchor / "README.md").write_text("init\n", encoding="utf-8")
    _git("add", "README.md")
    _git("commit", "-q", "-m", "init")
    return anchor


def _bump_foreign_repo_write_fire_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """C2's real-firing row for `bump-foreign-repo-write`: a real anchor
    repo and a real foreign sibling repo, with `bump_applies`/
    `resolve_launch_anchor` monkeypatched open on the guard's own module
    (both are only ever called via `check_bump_foreign_repo_write`'s own
    imported names -- no cross-module leak to patch, unlike the
    outside-repo guard's `session_anchor_has_git_repo` below). Everything
    downstream of that (`resolve_gitdir`, `resolve_git_root`,
    `bump_is_cleared`) runs against REAL git plumbing, so this row proves
    C2's rewritten FOREIGN-class copy actually fires end-to-end and stays
    under `MESSAGE_PROSE_CAP_BYTES`, not merely that the pure renderer does
    in isolation (`test_write_bump_message.py`'s own
    `test_every_variant_fits_the_message_prose_cap_bytes`)."""
    from coordinator_core.bash_guards import bump_foreign_repo_write as guard

    anchor = _bump_confinement_anchor_repo(scratch_dir)
    foreign = scratch_dir / "foreign"
    foreign.mkdir()
    _git = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=str(foreign), check=True, capture_output=True
    )
    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")

    mp.setattr(guard, "bump_applies", lambda *a, **k: True)
    mp.setattr(guard, "resolve_launch_anchor", lambda *a, **k: str(anchor))
    return {
        _CMD_OVERRIDE_KEY: f"git -C {foreign} commit --allow-empty -m x",
        _CWD_OVERRIDE_KEY: str(anchor),
    }


def _bump_outside_repo_write_fire_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """C2's real-firing row for `bump-outside-repo-write`: a real anchor
    repo and a plain non-repo scratch directory to write into.
    `resolve_launch_anchor` is monkeypatched on BOTH the guard's own module
    AND `_write_bump_applicability` itself -- the guard's
    `session_anchor_has_git_repo(...)` call is a bare imported name whose
    OWN function body (defined in `_write_bump_applicability.py`) calls
    THAT module's `resolve_launch_anchor`, which a guard-local patch alone
    never reaches. `target_is_bare_temp_scratch` is also patched open on the
    guard's module: `fire_row`'s own scratch dir lives under the real system
    temp root, which would otherwise exempt every candidate here via AC9's
    carve-out before this row ever reaches this guard's OWN predicate."""
    from coordinator_core.bash_guards import _write_bump_applicability as applicability
    from coordinator_core.bash_guards import bump_outside_repo_write as guard

    anchor = _bump_confinement_anchor_repo(scratch_dir)
    outside = scratch_dir / "outside"
    outside.mkdir()

    mp.setattr(guard, "bump_applies", lambda *a, **k: True)
    mp.setattr(guard, "resolve_launch_anchor", lambda *a, **k: str(anchor))
    mp.setattr(applicability, "resolve_launch_anchor", lambda *a, **k: str(anchor))
    mp.setattr(guard, "target_is_bare_temp_scratch", lambda *a, **k: False)

    src = anchor / "README.md"
    dest = outside / "newfile.txt"
    return {
        _CMD_OVERRIDE_KEY: f"cp {src} {dest}",
        _CWD_OVERRIDE_KEY: str(anchor),
    }


#: AUDIENCE AXIS (chunk C6) -- the two explicit audience values
#: `fire_row_for_audience` forces. `SUBAGENT_AUDIENCE` reuses this module's
#: own `_EXECUTOR_IDENTITY` (already the shared literal `test_cd_prefix_
#: bypass.py`'s own fixtures use, per that dict's own docstring) rather than
#: minting a third identity payload shape. `EM_AUDIENCE` is the EMPTY
#: identity dict -- no `agent_id` key at all -- which is exactly the
#: "well-formed envelope, both legs empty" shape `session.identity.
#: resolves_em_audience` resolves `True` for (a fresh per-cell `session_id`
#: with no backpointer file on disk resolves `subagent_type` to empty too),
#: matching every existing row that does not opt into an explicit identity
#: today.
SUBAGENT_AUDIENCE = "subagent"
EM_AUDIENCE = "em"


def fire_row_for_audience(row: CorpusRow, audience: str) -> GuardCapture:
    """Fire `row` exactly as `fire_row` would, EXCEPT the identity fields
    (`agent_id`/`agent_type`) in the final payload are forced to
    `SUBAGENT_AUDIENCE`'s or `EM_AUDIENCE`'s shape, overriding whatever
    `row.setup` itself supplied -- the audience-axis proof (AC-1/AC-5) needs
    the SAME underlying cell fired under BOTH audiences, independent of
    which identity (if any) the row happened to author for its own
    triggering purpose. `row.audience` itself is not consulted here (see
    that field's own docstring on `CorpusRow`) -- this is a firing-time
    override, always explicit at the call site."""
    if audience not in (SUBAGENT_AUDIENCE, EM_AUDIENCE):
        raise ValueError(f"unknown audience {audience!r}; expected {SUBAGENT_AUDIENCE!r} or {EM_AUDIENCE!r}")
    session_id = "guard-message-corpus-audience-%s-%s" % (audience, uuid.uuid4().hex)
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-audience-") as scratch:
        scratch_dir = Path(scratch)
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("CLAUDE_CODE_SESSION_ID", session_id)
            extra: Dict[str, Any] = dict(row.setup(scratch_dir, mp)) if row.setup else {}
            cmd = extra.pop(_CMD_OVERRIDE_KEY, row.input)
            cwd = extra.pop(_CWD_OVERRIDE_KEY, str(scratch_dir))
            payload: Dict[str, Any] = {
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
                "session_id": session_id,
                "cwd": cwd,
            }
            payload.update(extra)
            if audience == SUBAGENT_AUDIENCE:
                payload.update(_EXECUTOR_IDENTITY)
            else:
                payload.pop("agent_id", None)
                payload.pop("agent_type", None)
            return capture_one_guard(
                row.guard,
                cmd,
                session_id,
                cwd,
                payload,
                host_is_windows=row.host_is_windows,
            )


def fire_row(row: CorpusRow) -> GuardCapture:
    """Fire one `CorpusRow` through C1's capture seam.

    Mints a fresh session id AND a fresh scratch tempdir per call (see
    module docstring's isolation note), and runs `row.setup` (if any)
    inside a fresh `pytest.MonkeyPatch` context so a row's monkeypatched
    state never leaks into the next row fired in the same process --
    load-bearing for the identity-gated and blanket-git-add rows below,
    which monkeypatch module globals directly.
    """
    session_id = "guard-message-corpus-%s" % uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-") as scratch:
        scratch_dir = Path(scratch)
        with pytest.MonkeyPatch.context() as mp:
            # Review: coordinator:code-reviewer -- guard_inprocess_search's
            # _footer() session latch keys off the CLAUDE_CODE_SESSION_ID
            # process env var, never payload["session_id"]; setting it here
            # (matching test_guard_inprocess_search.py's own convention)
            # is what actually makes the per-cell isolation claim below true
            # for that guard, rather than relying on the fresh payload id.
            mp.setenv("CLAUDE_CODE_SESSION_ID", session_id)
            extra: Dict[str, Any] = dict(row.setup(scratch_dir, mp)) if row.setup else {}
            cmd = extra.pop(_CMD_OVERRIDE_KEY, row.input)
            cwd = extra.pop(_CWD_OVERRIDE_KEY, str(scratch_dir))
            payload: Dict[str, Any] = {
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
                "session_id": session_id,
                "cwd": cwd,
            }
            payload.update(extra)
            return capture_one_guard(
                row.guard,
                cmd,
                session_id,
                cwd,
                payload,
                host_is_windows=row.host_is_windows,
            )


# ---------------------------------------------------------------------------
# The 16 CONFINEMENT_DENY rows -- two cells per guard (one firing, one
# non-firing), pulling every `base_cmd` from `CONFINEMENT_GUARDS`'s own
# `_setup_<name>` factories per the module docstring's SSOT contract.
# Registration order mirrors `CONFINEMENT_GUARDS` itself.
# ---------------------------------------------------------------------------

_DENY = dispatch.GuardBand.CONFINEMENT_DENY

CONFINEMENT_ROWS: List[CorpusRow] = [
    CorpusRow(
        "no-verify",
        "no-verify-fire",
        'git commit --no-verify -m "msg"',
        True,
        _DENY,
        False,
        setup=_from_factory("no-verify"),
    ),
    CorpusRow(
        "no-verify",
        "no-verify-control",
        "git status",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "destructive-git-orphan",
        "destructive-git-orphan-fire",
        "git reset --hard $(echo HEAD~3)",
        True,
        _DENY,
        False,
        setup=_from_factory("destructive-git-orphan"),
    ),
    CorpusRow(
        "destructive-git-orphan",
        "destructive-git-orphan-control",
        "git status",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "destructive-rm",
        "destructive-rm-fire",
        "rm -rf $(echo /tmp/some-target)",
        True,
        _DENY,
        False,
        setup=_from_factory("destructive-rm"),
    ),
    CorpusRow(
        "destructive-rm",
        "destructive-rm-control",
        "ls -la /tmp",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "destructive-git-clean",
        "destructive-git-clean-fire",
        "git -C <repo> clean -fdx",
        True,
        _DENY,
        False,
        setup=_git_repo_setup("git -C %s clean -fdx"),
    ),
    CorpusRow(
        "destructive-git-clean",
        "destructive-git-clean-control",
        "git -C <repo> status",
        False,
        _DENY,
        False,
        setup=_git_repo_setup("git -C %s status"),
    ),
    CorpusRow(
        "destructive-git-revert",
        "destructive-git-revert-fire",
        "git -C <repo> stash",
        True,
        _DENY,
        False,
        setup=_git_repo_setup("git -C %s stash"),
    ),
    CorpusRow(
        "destructive-git-revert",
        "destructive-git-revert-control",
        "git -C <repo> status",
        False,
        _DENY,
        False,
        setup=_git_repo_setup("git -C %s status"),
    ),
    CorpusRow(
        # Advisory floor (2026-08-05): `affected` non-empty (a real dirty
        # tracked file) but nothing in it load-bearing/peer-claimed --
        # `check_destructive_git_revert` (this row) stays SILENT for this
        # exact input; the advisory now comes ONLY from the separate
        # `destructive-git-revert-advisory` guard registered in
        # ADVISORY_REWRITE (see that row in `ADVISORY_REWRITE_ROWS` below).
        # Review: staff-eng, Finding 0 -- an advisory returned from a
        # CONFINEMENT_DENY-registered guard would short-circuit
        # `evaluate_payload_json` and shadow every hard-deny guard
        # registered after it, so the two legs are split.
        "destructive-git-revert",
        "destructive-git-revert-advisory-input-no-fire",
        "git -C <repo> stash",
        False,
        _DENY,
        False,
        setup=_git_repo_advisory_setup("git -C %s stash"),
    ),
    CorpusRow(
        "blanket-git-add",
        "blanket-git-add-fire",
        "git add -A",
        True,
        _DENY,
        False,
        setup=_from_factory("blanket-git-add"),
    ),
    CorpusRow(
        "blanket-git-add",
        "blanket-git-add-control",
        "git add specific_file.py",
        False,
        _DENY,
        False,
        setup=_control_from_factory("blanket-git-add", "git add specific_file.py"),
    ),
    CorpusRow(
        "runaway-find",
        "runaway-find-fire",
        "find / -name '*.pyc'",
        True,
        _DENY,
        False,
        setup=_from_factory("runaway-find"),
    ),
    CorpusRow(
        "runaway-find",
        "runaway-find-control",
        "find . -name '*.pyc'",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "block-worktree-creation",
        "block-worktree-creation-fire",
        "git worktree add ../wt-1 feature-branch",
        True,
        _DENY,
        False,
        setup=_from_factory("block-worktree-creation"),
    ),
    CorpusRow(
        "block-worktree-creation",
        "block-worktree-creation-control",
        "git status",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "block-approval-sentinel-creation",
        "block-approval-sentinel-creation-fire",
        "touch .coordinator-doctrine-edit-approved",
        True,
        _DENY,
        False,
        setup=_from_factory("block-approval-sentinel-creation"),
    ),
    CorpusRow(
        "block-approval-sentinel-creation",
        "block-approval-sentinel-creation-control",
        "touch normal_file.txt",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "block-worktree-sentinel-creation",
        "block-worktree-sentinel-creation-fire",
        "touch .coordinator-override-worktree-guard",
        True,
        _DENY,
        False,
        setup=_from_factory("block-worktree-sentinel-creation"),
    ),
    CorpusRow(
        "block-worktree-sentinel-creation",
        "block-worktree-sentinel-creation-control",
        "touch normal_file.txt",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "block-reviewer-bash-outside-allowlist",
        "block-reviewer-bash-outside-allowlist-fire",
        "curl https://example.com",
        True,
        _DENY,
        False,
        setup=_from_factory_with_identity(
            "block-reviewer-bash-outside-allowlist", _REVIEWER_IDENTITY
        ),
    ),
    CorpusRow(
        "block-reviewer-bash-outside-allowlist",
        "block-reviewer-bash-outside-allowlist-control",
        "git status",
        False,
        _DENY,
        False,
        setup=_control_from_factory(
            "block-reviewer-bash-outside-allowlist",
            "git status",
            identity=_REVIEWER_IDENTITY,
        ),
    ),
    CorpusRow(
        "block-subagent-destructive-action",
        "block-subagent-destructive-action-fire",
        "git rebase -i HEAD~3",
        True,
        _DENY,
        False,
        setup=_from_factory_with_identity(
            "block-subagent-destructive-action", _EXECUTOR_IDENTITY
        ),
    ),
    CorpusRow(
        "block-subagent-destructive-action",
        "block-subagent-destructive-action-control",
        "git status",
        False,
        _DENY,
        False,
        setup=_control_from_factory(
            "block-subagent-destructive-action", "git status", identity=_EXECUTOR_IDENTITY
        ),
    ),
    CorpusRow(
        "block-subagent-commit",
        "block-subagent-commit-fire",
        'git commit -m "msg"',
        True,
        _DENY,
        False,
        setup=_from_factory_with_identity("block-subagent-commit", _EXECUTOR_IDENTITY),
    ),
    CorpusRow(
        "block-subagent-commit",
        "block-subagent-commit-control",
        "git status",
        False,
        _DENY,
        False,
        setup=_control_from_factory(
            "block-subagent-commit", "git status", identity=_EXECUTOR_IDENTITY
        ),
    ),
    CorpusRow(
        "check-test-suite-invocation",
        "check-test-suite-invocation-fire",
        "pytest",
        True,
        _DENY,
        False,
        setup=_from_factory_with_identity("check-test-suite-invocation", _EXECUTOR_IDENTITY),
    ),
    CorpusRow(
        "check-test-suite-invocation",
        "check-test-suite-invocation-control",
        "echo not a test run",
        False,
        _DENY,
        False,
        setup=_control_from_factory(
            "check-test-suite-invocation", "echo not a test run", identity=_EXECUTOR_IDENTITY
        ),
    ),
    # Drift fix (C3c, 2026-08-03): five more CONFINEMENT_DENY guards are live
    # `dispatch.py` registrations not present in `CONFINEMENT_GUARDS` (the
    # 16-row bank this block's own rows are pulled from) -- concurrent
    # sessions registered them after that bank was last regenerated, and the
    # module-level sanity assert below (comparing against the LIVE chain, not
    # this bank) failed on import until these rows existed. Non-firing
    # control rows only, same lighter-path precedent as
    # `bump-foreign-repo-write` in the ADVISORY_REWRITE block below -- a real
    # per-guard trigger fixture for each is a job for whichever chunk owns
    # `CONFINEMENT_GUARDS`'s next regeneration, not a silent scope-creep here.
    #
    # `block-dev-repo-sentinel-removal` (bare `check()`) is deliberately
    # ABSENT here (X2, 2026-08-06, apply-guard-class-census): C13 deleted its
    # CONFINEMENT_DENY `dispatch.py` registration entirely -- `check()` is no
    # longer reachable through the live chain at all, only directly callable
    # (unit-tested elsewhere). Its sole registered leg,
    # `block-dev-repo-sentinel-removal-advisory`, already has its own
    # fire+control pair in `ADVISORY_REWRITE_ROWS` below; a row here naming
    # the unregistered `check()` would fail
    # `test_corpus_imports_cleanly_and_every_row_guard_resolves` ("names an
    # unregistered guard").
    CorpusRow(
        "block-disarm-marker-sentinel-creation",
        "block-disarm-marker-sentinel-creation-control",
        "git status",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "block-stash-destruction",
        "block-stash-destruction-control",
        "git status",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "block-subagent-stash-creation",
        "block-subagent-stash-creation-control",
        "git status",
        False,
        _DENY,
        False,
    ),
    # AC10 coverage-gap closer (C7, docs/plans/2026-08-13-em-exercisable-
    # in-band-grant-route.md): `block-subagent-guard-grant` (chunk C3) is a
    # genuinely fireable CONFINEMENT_DENY guard with no corpus row until
    # this dispatch -- unlike its modelled sibling
    # `block-subagent-grant-acquisition` (still a named
    # `REGISTER_COVERAGE_EXEMPTIONS` gap, not touched here), this guard
    # wants a real fire+control pair, not an exemption. Custom setup (not
    # `_from_factory*`) because this guard is not one of
    # `CONFINEMENT_GUARDS`'s 16 `_setup_<name>` factories.
    CorpusRow(
        "block-subagent-guard-grant",
        "block-subagent-guard-grant-fire",
        'python3 -m coordinator_core.session.em_guard_grant grant "test reason"',
        True,
        _DENY,
        False,
        setup=lambda scratch_dir, mp: dict(_EXECUTOR_IDENTITY),
    ),
    CorpusRow(
        "block-subagent-guard-grant",
        "block-subagent-guard-grant-control",
        "python3 -m coordinator_core.session.em_guard_grant read",
        False,
        _DENY,
        False,
        setup=lambda scratch_dir, mp: dict(_EXECUTOR_IDENTITY),
    ),
    # AC2 coverage-gap closer, 2026-08-17. The comment above called
    # `block-subagent-grant-acquisition` a "still a named
    # REGISTER_COVERAGE_EXEMPTIONS gap, not touched here"; it is reachable from
    # `_build_guard_chain`, so AC2 counted it as an uncovered guard and this
    # module's own `test_ac2_every_reachable_guard_has_a_corpus_row` failed on
    # it. That failure was INVISIBLE to every directory-scoped run — see this
    # module's header note on `python_files` — which is why the gap outlived
    # the sibling row that closed the identical shape for
    # `block-subagent-guard-grant` above.
    #
    # A real fire+control pair rather than an exemption: it is genuinely
    # fireable, verified live here and already proven by the identical row in
    # `test_confinement_deny_band_shape._EXTRA_FIRING_ROWS`. Identity-gated on
    # the raw presence of `agent_id`, per its own module docstring's
    # "IDENTITY-GATE POSTURE" section.
    CorpusRow(
        "block-subagent-grant-acquisition",
        "block-subagent-grant-acquisition-fire",
        'python3 -m coordinator_core.session.claude_md_grant grant pm "test reason"',
        True,
        _DENY,
        False,
        setup=lambda scratch_dir, mp: dict(_EXECUTOR_IDENTITY),
    ),
    CorpusRow(
        "block-subagent-grant-acquisition",
        "block-subagent-grant-acquisition-control",
        "python3 -m coordinator_core.session.claude_md_grant read",
        False,
        _DENY,
        False,
        setup=lambda scratch_dir, mp: dict(_EXECUTOR_IDENTITY),
    ),
]

#: Sanity invariant this module itself relies on -- every one of
#: `CONFINEMENT_GUARDS`' 16 guard names appears in `CONFINEMENT_ROWS` at
#: least once (checked below in the self-test, and re-checked structurally
#: here so an import-time typo fails immediately rather than surfacing only
#: when a later chunk's suite runs). Subset, not equality (C3c, 2026-08-03):
#: `CONFINEMENT_ROWS` also carries five drift-fix rows (see above) for live
#: CONFINEMENT_DENY registrations `CONFINEMENT_GUARDS`/`GUARD_NAMES` do not
#: yet know about -- the AC2 closer test (bottom of this module) is the one
#: that must hold by equality against the LIVE chain, not this static bank.
#: Band-flip reconciliation (X2, 2026-08-06, apply-guard-class-census):
#: `test_confinement_attack_corpus.py`'s own `CONFINEMENT_GUARDS` bank is a
#: static list this chunk's write scope does not cover -- it still names
#: these two guards, but C13/C14 moved BOTH off `CONFINEMENT_DENY` onto
#: `ADVISORY_REWRITE` in the live `dispatch.py` chain (see this module's own
#: `ADVISORY_REWRITE_ROWS` for their real, band-correct rows now). Excluded
#: here, not silently dropped, so the subset check below still catches a
#: genuine future drift for every OTHER guard in the static bank.
_FLIPPED_TO_ADVISORY_REWRITE = {
    "block-subagent-plan-body-bash-write",
    "check-raw-pid-liveness",
}
#: Moved out of module scope into
#: `test_guard_corpus_registration_invariants.py` (docs/plans/2026-08-07-
#: install-dogfood-mechanical-residue.md, chunk C3, F13a) -- an import-time
#: bare `assert` here turned one missing registration into an import
#: failure for every test module that imports this corpus, masking whatever
#: was broken behind it. The computation these two invariants depend on
#: (`_FLIPPED_TO_ADVISORY_REWRITE`, `CONFINEMENT_GUARDS`, `GUARD_NAMES`,
#: `CONFINEMENT_ROWS`) stays here; only the assertions moved.


# ---------------------------------------------------------------------------
# C3b -- the 13+2 ADVISORY_REWRITE/PLATFORM_CONDITIONED_DENY rows.
#
# Live-measured correction (this chunk, 2026-08-03): AC2 requires "every
# guard reachable from `_build_guard_chain` in these two bands", not a fixed
# count -- structurally introspecting `_build_guard_chain` (never `.fn()`)
# finds 16 ADVISORY_REWRITE registrations and 2 PLATFORM_CONDITIONED_DENY
# registrations, not 13+2=15. The extra three beyond the dispatch brief's
# 13 are `offer-invoke-params-stdin`, `branch-set-precedence`, and
# `longlived-branch-naming` -- all three are real, live `dispatch.py`
# registrations in the ADVISORY_REWRITE band (grep-confirmed against
# `_build_guard_chain`'s own output, not a docstring count). AC2's own text
# ("every guard reachable... gets a corpus row") governs over the
# illustrative arithmetic, so all 18 get rows below, two cells each (one
# firing, one non-firing), following `CONFINEMENT_ROWS`'s own shape.
#
# "Speaks" here follows `guard_message_capture.py`'s own general definition
# (`envelope is not None`), NOT `CONFINEMENT_ROWS`'s narrower "denies"
# reading -- every guard in these two bands can return a non-None envelope
# that is `allow`+advisory/rewrite rather than `deny` (e.g.
# `git-commit-safe-commit-advise`'s allow+additionalContext), so "denies"
# would be the wrong predicate for this band. `fire_row`/`capture_one_guard`
# call `GuardEntry.fn()` directly (never `dispatch.evaluate_payload_json`'s
# loop), so this reads the guard's own raw return, before any outer-loop
# suppression (`_suppress_advisory`) is applied -- consistent with
# `CONFINEMENT_ROWS`'s own reading of the same seam.
# ---------------------------------------------------------------------------

_REWRITE = dispatch.GuardBand.ADVISORY_REWRITE
_PLATFORM = dispatch.GuardBand.PLATFORM_CONDITIONED_DENY


def _validate_commit_frontmatter_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """`validate-commit`'s Check 8 (frontmatter-mutation subject discipline)
    fires session-id-agnostically -- Check 5 (scoped-staging) is the only
    leg of this guard keyed to a real `.git/coordinator-sessions/<sid>/`
    directory (`test_check_validate_commit.py`'s own fixtures use a bare
    literal `"no-session"` for exactly this reason: a session id with no
    matching directory skips Check 5's block entirely without erroring,
    per `dispatch_checks.check_validate_commit`'s own "an ImportError
    degrades to... my_scope left unset" comment, which the same isdir()
    guard applies to identically). This corpus's `fire_row` mints its own
    session id per cell with no way for `setup` to learn it in advance (no
    reserved override key exists for it -- see module docstring's two
    reserved keys), so this fixture deliberately exercises Check 8 only,
    the leg that needs no session-directory match at all. Literals below
    (`_OLD`/`_NEW`, the staged path) are `test_check_validate_commit.py`'s
    own `TestCheckEightFrontmatterMutation._OLD`/`_NEW` and
    `_stage_frontmatter_change` helper, reproduced directly (not imported --
    that class's helper is nested under a test class, not a module-level
    export)."""
    root = scratch_dir
    _git = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=str(root), check=True, capture_output=True
    )
    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    target = root / "docs" / "plans" / "2026-07-16-foo.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nstatus: draft\nkind: plan\n---\nbody\n", encoding="utf-8")
    _git("add", "docs/plans/2026-07-16-foo.md")
    _git("commit", "-q", "-m", "seed frontmatter file")
    target.write_text("---\nstatus: ready\nkind: plan\n---\nbody\n", encoding="utf-8")
    _git("add", "docs/plans/2026-07-16-foo.md")
    return {
        _CMD_OVERRIDE_KEY: 'git commit -m "tweak plan"',
        _CWD_OVERRIDE_KEY: str(root),
    }


def _branch_set_precedence_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """`branch-set-precedence` never touches a real git subprocess -- every
    seam (`resolve_git_root`, `_is_hazard_repo`, `_other_canonical_
    branches`, `_ahead_of_main`, `_now`, `_today`) is monkeypatched on the
    guard's own module, exactly `test_guard_branch_set_precedence.py`'s own
    injection convention (module docstring: "monkeypatched on THIS module's
    own imported attribute"). `dispatch.py`'s registration calls
    `guard.check(payload)` with no `branch_set_provider` kwarg, so the
    `_other_canonical_branches` patch (not the kwarg) is the only way to
    feed a candidate branch through the real registered chain. Fixture
    values (branch names, 1h-old epoch, 12 commits ahead) are that test
    file's own `TestAdvisoryFiresWithBranchAndCount.test_advisory_fires_
    with_real_branch_and_count` literals."""
    from coordinator_core.bash_guards import guard_branch_set_precedence as guard

    fixed_now = 1722700000.0
    fixed_today = "2026-08-03"
    mp.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
    mp.setattr(guard, "_is_hazard_repo", lambda git_root: True)
    mp.setattr(guard, "_now", lambda: fixed_now)
    mp.setattr(guard, "_today", lambda: fixed_today)
    mp.setattr(
        guard,
        "_other_canonical_branches",
        lambda cwd=None: [("work/machine-b/2026-07-31", fixed_now - 3600)],
    )
    mp.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 12)
    # This fixture isolates the "fires with real name/count" property, not
    # the AC16 `should_prompt_rename` leg -- same isolation
    # `test_guard_branch_set_precedence.py`'s own `test_advisory_fires_
    # with_real_branch_and_count` applies (that leg has its own dedicated
    # coverage in that file's `TestRecencyFilter`, not re-derived here).
    mp.setattr(guard, "should_prompt_rename", lambda *a, **k: False)
    return {
        _CMD_OVERRIDE_KEY: "git checkout -b work/machine-b/2026-08-03",
        _CWD_OVERRIDE_KEY: "/repo",
    }


def _longlived_branch_naming_hazard_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """`longlived-branch-naming` needs only the AC13 repo-scoping gate
    patched open (`resolve_git_root`/`_is_hazard_repo`) -- no branch-set
    or git-log seam exists on this guard at all
    (`test_guard_longlived_branch_naming.py`'s own `_hazard_repo_by_default`
    fixture)."""
    from coordinator_core.bash_guards import guard_longlived_branch_naming as guard

    mp.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
    mp.setattr(guard, "_is_hazard_repo", lambda git_root: True)
    return {_CWD_OVERRIDE_KEY: "/repo"}


def _heredoc_repo_write_advise_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """`heredoc-repo-write-advise` needs a `cwd` (threaded to the guard as
    `git_root`, see dispatch.py's registration comment) that is NOT under
    the real `$TEMP`/`$TMP` -- `fire_row`'s own default `cwd` is a fresh
    `tempfile.TemporaryDirectory()`, which the guard's own scratch-root
    exclusion would correctly (but unhelpfully, for a firing-row fixture)
    classify as scratch and stay silent on. The fake, non-existent `/repo`
    is the SAME convention several rows above already use for an
    unrelated reason (a `resolve_git_root` stub target) -- reused here
    purely because it is a real absolute path outside any temp root, not
    because this guard resolves git roots itself (it never spawns git)."""
    return {_CWD_OVERRIDE_KEY: "/repo"}


def _noncanonical_branch_creation_hazard_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """X2 (2026-08-06, apply-guard-class-census): `block-noncanonical-
    branch-creation`'s own REPO SCOPING gate (module docstring) must resolve
    the fired `cwd` to a hazard repo before its canonical-shape predicate
    ever runs -- same open-the-gate shape as `_longlived_branch_naming_
    hazard_setup` immediately above, patched on THIS guard's own imported
    module attributes (`resolve_git_root`/`_is_hazard_repo`), not a
    `branch_set_provider` kwarg `dispatch.py`'s registration never passes."""
    from coordinator_core.bash_guards import block_noncanonical_branch_creation as guard

    mp.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
    mp.setattr(guard, "_is_hazard_repo", lambda git_root: True)
    return {_CWD_OVERRIDE_KEY: "/repo"}


ADVISORY_REWRITE_ROWS: List[CorpusRow] = [
    CorpusRow(
        # Advisory floor (2026-08-05, Review: staff-eng Finding 0):
        # registered in ADVISORY_REWRITE, after every CONFINEMENT_DENY
        # hard-deny guard -- see the paired non-firing
        # `destructive-git-revert` row in `CONFINEMENT_ROWS` above, which
        # proves the hard-deny leg stays silent for this exact input.
        "destructive-git-revert-advisory",
        "destructive-git-revert-advisory-fire",
        "git -C <repo> stash",
        True,
        _REWRITE,
        False,
        setup=_git_repo_advisory_setup("git -C %s stash"),
    ),
    CorpusRow(
        "destructive-git-revert-advisory",
        "destructive-git-revert-advisory-control",
        "git -C <repo> status",
        False,
        _REWRITE,
        False,
        setup=_git_repo_advisory_setup("git -C %s status"),
    ),
    # Review: staff-eng Finding 8 -- the stash pair above pinned this
    # guard's byte count for one verb only; `reset`/`checkout`/`restore`
    # each carry their own `harm` wording (see `dispatch_checks.py`'s
    # `_check_destructive_git_revert_full`, the "VERB-CONDITIONED" comment)
    # and were previously unpinned, so a wording change on any of the three
    # could silently clear `MESSAGE_PROSE_CAP_BYTES` with nothing in this
    # suite noticing. `_git_repo_advisory_setup` is already generic over
    # `cmd_template` -- reused verbatim, not re-implemented.
    CorpusRow(
        "destructive-git-revert-advisory",
        "destructive-git-revert-advisory-reset-fire",
        "git -C <repo> reset --hard",
        True,
        _REWRITE,
        False,
        setup=_git_repo_advisory_setup("git -C %s reset --hard"),
    ),
    CorpusRow(
        "destructive-git-revert-advisory",
        "destructive-git-revert-advisory-checkout-dot-fire",
        "git -C <repo> checkout .",
        True,
        _REWRITE,
        False,
        setup=_git_repo_advisory_setup("git -C %s checkout ."),
    ),
    CorpusRow(
        "destructive-git-revert-advisory",
        "destructive-git-revert-advisory-restore-dot-fire",
        "git -C <repo> restore .",
        True,
        _REWRITE,
        False,
        setup=_git_repo_advisory_setup("git -C %s restore ."),
    ),
    CorpusRow(
        # Two-leg split (2026-08-05, mirrors `destructive-git-revert-
        # advisory` immediately above -- same CONFINEMENT_DENY shadowing
        # hazard, `state/audits/2026-08-05-confinement-deny-band-return-
        # shapes.md`): registered in ADVISORY_REWRITE, after every
        # CONFINEMENT_DENY hard-deny guard. The paired non-firing
        # `block-dev-repo-sentinel-removal` row in `CONFINEMENT_ROWS`
        # (`test_confinement_deny_band_shape.py`'s own `_EXTRA_FIRING_
        # ROWS`) proves the hard-deny leg (`check()`) stays silent
        # (`None`) for this exact input.
        "block-dev-repo-sentinel-removal-advisory",
        "block-dev-repo-sentinel-removal-advisory-fire",
        "echo .coordinator-dev-repo | xargs rm",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "block-dev-repo-sentinel-removal-advisory",
        "block-dev-repo-sentinel-removal-advisory-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "offer-git-c",
        "offer-git-c-fire",
        "cd /tmp/repo && git status",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "offer-git-c",
        "offer-git-c-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        # Mechanical leg of the fleet-wide `.git/index.lock` contention
        # campaign -- see guard_no_optional_locks.py's own module docstring.
        "git-no-optional-locks",
        "git-no-optional-locks-fire",
        "git status",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "git-no-optional-locks",
        "git-no-optional-locks-control",
        "git diff --cached",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        # Self-heal leg of the same campaign -- always returns None (side-
        # effect-only guard, see guard_reap_stale_git_lock.py's own module
        # docstring), so a single non-firing control row is the correct
        # shape here, same lighter-path precedent as
        # `block-dev-repo-sentinel-removal-advisory`'s siblings above.
        "reap-stale-git-lock",
        "reap-stale-git-lock-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "validate-commit",
        "validate-commit-fire",
        'git commit -m "tweak plan"',
        True,
        _REWRITE,
        False,
        setup=_validate_commit_frontmatter_setup,
    ),
    CorpusRow(
        "validate-commit",
        "validate-commit-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "inprocess-search",
        "inprocess-search-fire",
        "grep -r foo .",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "inprocess-search",
        "inprocess-search-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "probe-spray",
        "probe-spray-fire",
        "echo alive",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "probe-spray",
        "probe-spray-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "block-illegal-filename",
        "block-illegal-filename-fire",
        "echo x > bad?name.txt",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "block-illegal-filename",
        "block-illegal-filename-control",
        "echo x > normal.txt",
        False,
        _REWRITE,
        False,
    ),
    # C5 (row 20, `docs/reference/guard-dialect-coverage.md`,
    # docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-
    # silent.md): this guard's heredoc/process-substitution/redirect scan is
    # POSIX-only, so a PowerShell-dialect command records SILENT and stays
    # non-speaking (envelope `None`) rather than a guessed clean -- pinned
    # here as a non-firing cell so a future regression that made this guard
    # start guessing on PowerShell input would flip `expected_speaker`.
    CorpusRow(
        "block-illegal-filename",
        "block-illegal-filename-powershell-silent",
        "echo x > bad?name.txt",
        False,
        _REWRITE,
        False,
        setup=lambda scratch_dir, mp: {"tool_name": "PowerShell"},
    ),
    CorpusRow(
        "find-exec-rewrite",
        "find-exec-rewrite-fire",
        "find . -name '*.py' -exec cat {} \\;",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "find-exec-rewrite",
        "find-exec-rewrite-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "grep-via-bash-rewrite",
        "grep-via-bash-rewrite-fire",
        "grep foo file.py",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "grep-via-bash-rewrite",
        "grep-via-bash-rewrite-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "sed-range-read-advise",
        "sed-range-read-advise-fire",
        "sed -n '10,20p' path/to/file.py",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "sed-range-read-advise",
        "sed-range-read-advise-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "cat-heredoc-write-advise",
        "cat-heredoc-write-advise-fire",
        "cat > out.txt <<'EOF'\nhello\nEOF",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "cat-heredoc-write-advise",
        "cat-heredoc-write-advise-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "heredoc-repo-write-advise",
        "heredoc-repo-write-advise-fire",
        "python3 - <<'PY'\nimport pathlib\npathlib.Path(\"coordinator_core/x.py\").write_text(\"hi\")\nPY",
        True,
        _REWRITE,
        False,
        setup=_heredoc_repo_write_advise_setup,
    ),
    CorpusRow(
        "heredoc-repo-write-advise",
        "heredoc-repo-write-advise-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "git-commit-safe-commit-advise",
        "git-commit-safe-commit-advise-fire",
        'git commit -m "msg"',
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "git-commit-safe-commit-advise",
        "git-commit-safe-commit-advise-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "multiprobe-banner-rewrite",
        "multiprobe-banner-rewrite-fire",
        'echo "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD',
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "multiprobe-banner-rewrite",
        "multiprobe-banner-rewrite-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "head-tail-plumbing-rewrite",
        "head-tail-plumbing-rewrite-fire",
        "find . -name '*.py' | head -n 5",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "head-tail-plumbing-rewrite",
        "head-tail-plumbing-rewrite-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "offer-invoke-params-stdin",
        "offer-invoke-params-stdin-fire",
        "python3 -m coordinator_core.invoke ceremony.scoped_git_commit "
        "'{\"message\": \"C1's half (build)\"}'",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "offer-invoke-params-stdin",
        "offer-invoke-params-stdin-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "grep-via-bash-guard",
        "grep-via-bash-guard-fire",
        "grep -rn TODO src/ | wc -l",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "grep-via-bash-guard",
        "grep-via-bash-guard-control",
        "grep -rn TODO src/",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "powershell-via-bash-guard",
        "powershell-via-bash-guard-fire",
        'powershell.exe -NoProfile -Command "$p=Get-Process -Id 44448 -EA '
        'SilentlyContinue; if($p){\\"ALIVE $($p.ProcessName)\\"}else{\'DEAD\'}; ..."',
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "powershell-via-bash-guard",
        "powershell-via-bash-guard-control-single-quoted",
        "pwsh -Command 'Write-Host $HOME'",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "branch-set-precedence",
        "branch-set-precedence-fire",
        "git checkout -b work/machine-b/2026-08-03",
        True,
        _REWRITE,
        False,
        setup=_branch_set_precedence_setup,
    ),
    CorpusRow(
        "branch-set-precedence",
        "branch-set-precedence-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "longlived-branch-naming",
        "longlived-branch-naming-fire",
        "git checkout -b feature/x",
        True,
        _REWRITE,
        False,
        setup=_longlived_branch_naming_hazard_setup,
    ),
    CorpusRow(
        "longlived-branch-naming",
        "longlived-branch-naming-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    # Band-flip reconciliation (X2, 2026-08-06, apply-guard-class-census):
    # C13/C14 moved these three guards CONFINEMENT_DENY -> ADVISORY_REWRITE
    # and their `check()` bodies to the allow+`additionalContext` envelope
    # shape -- moved here from `CONFINEMENT_ROWS` (same `guard`/`row_id`
    # naming, same underlying `check()` logic, only the band and expected
    # envelope shape changed) rather than re-derived, per the corpus's own
    # "pull base_cmd from the factory" contract.
    CorpusRow(
        "check-raw-pid-liveness",
        "check-raw-pid-liveness-fire",
        "kill -0 1234",
        True,
        _REWRITE,
        False,
        setup=_from_factory("check-raw-pid-liveness"),
    ),
    CorpusRow(
        "check-raw-pid-liveness",
        "check-raw-pid-liveness-control",
        "echo hi",
        False,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "block-subagent-plan-body-bash-write",
        "block-subagent-plan-body-bash-write-fire",
        "echo x >> docs/plans/foo.md",
        True,
        _REWRITE,
        False,
        setup=_from_factory_with_identity(
            "block-subagent-plan-body-bash-write", _EXECUTOR_IDENTITY
        ),
    ),
    CorpusRow(
        "block-subagent-plan-body-bash-write",
        "block-subagent-plan-body-bash-write-control",
        "cat docs/plans/foo.md",
        False,
        _REWRITE,
        False,
        setup=_control_from_factory(
            "block-subagent-plan-body-bash-write",
            "cat docs/plans/foo.md",
            identity=_EXECUTOR_IDENTITY,
        ),
    ),
    # `block-noncanonical-branch-creation` previously carried only a
    # non-firing control row (drift-fix precedent, `CONFINEMENT_ROWS`'s own
    # "real per-guard trigger fixture ... not a silent scope-creep here"
    # note) -- the band move is the natural point to add a real firing row,
    # since it needs a hazard-repo fixture this guard's message-shape
    # reconciliation already requires exercising the live `check()` path
    # for.
    CorpusRow(
        "block-noncanonical-branch-creation",
        "block-noncanonical-branch-creation-fire",
        "git checkout -b fix/foo",
        True,
        _REWRITE,
        False,
        setup=_noncanonical_branch_creation_hazard_setup,
    ),
    CorpusRow(
        "block-noncanonical-branch-creation",
        "block-noncanonical-branch-creation-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    # Drift fix (C3c, 2026-08-03): `bump-foreign-repo-write` is a live
    # ADVISORY_REWRITE registration (`dispatch.py:1232`) not present when
    # C3b wrote the block above -- a concurrent session registered it after
    # C3b's snapshot was taken, and the module-level sanity assert below
    # (comparing against the LIVE chain) failed on import until this row
    # existed.
    CorpusRow(
        "bump-foreign-repo-write",
        "bump-foreign-repo-write-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    # narrow-write-confinement-bump.md chunk C2 (2026-08-03): AC7's own
    # "not the incumbent bump rows' non-firing git status shape" -- this is
    # a genuine FIRING row through the real dispatch chain (see
    # `_bump_foreign_repo_write_fire_setup`), proving the rewritten
    # FOREIGN-class, EM-class copy actually denies and stays under
    # `MESSAGE_PROSE_CAP_BYTES` end-to-end, not merely in the pure renderer.
    # The other three variants this chunk adds (FOREIGN-subagent and both
    # PUBLISH-class templates) are NOT reachable through this real chain
    # yet: `destination_class` wiring into this guard is this plan's C4, a
    # separate atomic landing group not yet landed -- so a genuine
    # PUBLISH-class firing row cannot exist here until C4/C5 land. Those
    # three variants satisfy AC7 via the explicit `measure_envelope(...)`
    # assertion leg of its own OR clause instead
    # (`test_write_bump_message.py::test_every_variant_fits_the_message_prose_cap_bytes`).
    CorpusRow(
        "bump-foreign-repo-write",
        "bump-foreign-repo-write-fire",
        "git -C <foreign> commit --allow-empty -m x",
        True,
        _REWRITE,
        False,
        setup=_bump_foreign_repo_write_fire_setup,
    ),
    # Drift fix (C5, 2026-08-03): `bump-outside-repo-write` is C4's sibling
    # ADVISORY_REWRITE registration (`dispatch.py`, `bump-foreign-repo-write`'s
    # own registration comment block) -- landed uncommitted in this tree by a
    # concurrent session (docs/plans/2026-08-02-write-confinement-guards.md,
    # DoE-claude repo) after this block was last written, and the
    # module-level sanity assert below (comparing against the LIVE chain)
    # failed on import until this row existed.
    CorpusRow(
        "bump-outside-repo-write",
        "bump-outside-repo-write-control",
        "git status",
        False,
        _REWRITE,
        False,
    ),
    # narrow-write-confinement-bump.md chunk C2 -- same real-firing proof as
    # `bump-foreign-repo-write-fire` immediately above, for the outside-repo
    # sibling guard (see `_bump_outside_repo_write_fire_setup`).
    CorpusRow(
        "bump-outside-repo-write",
        "bump-outside-repo-write-fire",
        "cp <anchor>/README.md <outside>/newfile.txt",
        True,
        _REWRITE,
        False,
        setup=_bump_outside_repo_write_fire_setup,
    ),
]

PLATFORM_CONDITIONED_ROWS: List[CorpusRow] = [
    CorpusRow(
        "multiprobe-banner",
        "multiprobe-banner-fire",
        'echo "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD',
        True,
        _PLATFORM,
        False,
    ),
    CorpusRow(
        "multiprobe-banner",
        "multiprobe-banner-control",
        "git status",
        False,
        _PLATFORM,
        False,
    ),
    CorpusRow(
        "plumbing-and-loops",
        "plumbing-and-loops-fire",
        "find . -name '*.py' | head -n 5",
        True,
        _PLATFORM,
        False,
    ),
    CorpusRow(
        "plumbing-and-loops",
        "plumbing-and-loops-control",
        "git status",
        False,
        _PLATFORM,
        False,
    ),
]

#: Same import-time sanity shape as `CONFINEMENT_ROWS`'s own invariant above
#: -- every live ADVISORY_REWRITE/PLATFORM_CONDITIONED_DENY registration
#: (per `_build_guard_chain`'s own structural output, not a docstring count)
#: has at least one row here.
_LIVE_CHAIN_FOR_SANITY = dispatch._build_guard_chain(
    cmd="git status",
    session_id="guard-message-corpus-c3b-sanity",
    cwd="/tmp",
    payload={"tool_name": "Bash", "tool_input": {"command": "git status"}},
    policy_file=None,
    host_is_windows=False,
)
#: Moved out of module scope into
#: `test_guard_corpus_registration_invariants.py` (docs/plans/2026-08-07-
#: install-dogfood-mechanical-residue.md, chunk C3, F13a) -- same rationale
#: as `_FLIPPED_TO_ADVISORY_REWRITE` above. `_LIVE_CHAIN_FOR_SANITY` stays
#: here as the shared computation both moved assertions depend on.


# ---------------------------------------------------------------------------
# C3c -- write_guards/ and hooks/ rows, closing AC2 for both directories.
#
# Spec backlink: pln-runtime-measured-message-size--0669ac, the
# C3c dispatch stub. Neither directory carries a `dispatch.GuardBand` (that
# enum is a `bash_guards`-only concept on `GuardEntry`), so every row below
# is banded via `_message_size.proxy_band(...)` -- named honestly as a
# DIRECTORY BUCKET ("write_guards", "hooks"), never dressed up as a
# duty-of-care classification (§ Problem's own correction on this point).
#
# Firing mechanism differs by directory from C3a/C3b's `fire_row`, which is
# bash-command-shaped (a `cmd` string through `dispatch._build_guard_chain`):
#   - write_guards: `guard.check(payload)` invoked DIRECTLY per guard, via
#     `write_guards.engine._discover_guards()` -- never through
#     `engine.evaluate`, whose hard-deny/advisory two-phase loop is the same
#     first-non-None-wins short-circuit shape Anti-scope forbids treating as
#     a capture seam for `dispatch._decision` (this chunk applies the same
#     principle to the sibling engine).
#   - hooks: the three modules routed onto the shared `_hook_envelope`
#     chokepoint this wave (C6/C6b) -- `op(payload)` invoked directly, its
#     `{"message": str}` return wrapped into a `hookSpecificOutput.
#     additionalContext`-shaped dict so `_message_size.measure_envelope`
#     reads it identically to a bash/write envelope.
#
# Coverage note (NEEDS_COORDINATOR, recorded for C11): a live reconnaissance
# pass over the 12 hooks/ modules the plan's § Problem lists as already
# routed through `_hook_envelope` found most of them expose `async def
# _handler(params, ...)` (an MCP-tool-shaped op), not a plain `op(payload)
# -> dict | None` Stop-hook function -- `nudge_em_code_dispatch` is the one
# exception (it carries BOTH shapes; its sync `op()` is what
# `write_guards.nudge_em_code_dispatch` delegates to, and that guard's own
# row below covers it). A `(cmd, session_id, cwd, payload)`-shaped corpus row
# cannot invoke an async MCP-tool handler without a second, differently-
# shaped capture seam this chunk does not build. This chunk's hooks/ rows are
# therefore scoped to the three modules this wave actually routed onto the
# chokepoint (`em_report_altitude`, `nudge_harness_directive_dispatch`,
# `nudge_unrouted_sizing`) plus `nudge_em_code_dispatch` (covered via its
# write_guards row, same underlying `op()`) -- the remaining ~10 async-
# handler hooks/ modules are C11's re-derivation to classify and, if in
# scope, wire a seam for; re-litigating that census here would duplicate C11's
# own explicitly-scoped job rather than discharge it.
# ---------------------------------------------------------------------------

_WRITE_GUARDS_BAND = proxy_band("write_guards")
_HOOKS_BAND = proxy_band("hooks")


def _wg_lookup() -> Dict[str, Any]:
    """Fresh `{guard_name: _Guard}` lookup via `write_guards.engine.
    _discover_guards()` -- called per fire (not cached at module import
    time), matching this module's own no-import-time-side-effects
    discipline (module docstring) and `_discover_guards()`'s own
    already-re-run-per-call precedent in `engine.evaluate`."""
    guards, import_failed = write_guards_engine._discover_guards()
    assert not import_failed, (
        "write_guards module(s) failed to import during corpus fire: %s" % import_failed
    )
    return {g.name: g for g in guards}


@dataclass(frozen=True)
class WriteGuardCapture:
    """The write_guards sibling of `guard_message_capture.GuardCapture` --
    same `(name, band, envelope)` shape, `band` a plain `str` (the proxy
    band) since no `GuardBand` exists for this directory."""

    name: str
    band: str
    envelope: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class WriteGuardRow:
    """One `(write_guard, input)` cell. `payload_factory` returns the FULL
    PreToolUse payload dict `guard.check()` expects -- unlike `CorpusRow.
    setup` (which merges into an already-assembled bash payload), a
    write_guards payload has no `cmd`/`cwd` split to override, so the
    factory owns the whole dict. Invoked at FIRE time inside a fresh scratch
    dir and a fresh `pytest.MonkeyPatch` context, same per-cell isolation
    discipline as `fire_row` above."""

    guard: str
    row_id: str
    expected_speaker: bool
    payload_factory: Callable[[Path, pytest.MonkeyPatch], Dict[str, Any]]
    #: Rows for a guard whose real trigger needs environment/registry state
    #: this corpus does not stand up (a sibling DoE-claude checkout, a real
    #: publish-mirror registry entry, real session-start bookkeeping) are
    #: marked here with a written reason and excluded from the fire-
    #: verification test below -- registered (AC2) but not fire-asserted,
    #: same "lighter path" the plan's own C3c stub sanctions for
    #: `nudge_unrouted_sizing`. `None` means this row IS fire-asserted.
    unverified_reason: Optional[str] = None


def fire_write_guard_row(row: WriteGuardRow) -> WriteGuardCapture:
    lookup = _wg_lookup()
    guard = lookup[row.guard]
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-wg-") as scratch:
        scratch_dir = Path(scratch)
        with pytest.MonkeyPatch.context() as mp:
            payload = row.payload_factory(scratch_dir, mp)
            envelope = guard.check(payload)
    return WriteGuardCapture(name=row.guard, band=_WRITE_GUARDS_BAND, envelope=envelope)


def _wg_benign(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A shared, guard-agnostic non-firing control payload -- a plain Write
    to an ordinary file under the row's own fresh scratch dir, which no
    write_guards guard's trigger condition below matches."""
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(scratch_dir / "benign.txt"), "content": "hello\n"},
    }


def _wg_completion_monolith_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "archive/completed/2026-08.md", "content": "x"},
    }


def _wg_consumed_handoff_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    target = scratch_dir / "state" / "handoffs" / "foo.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nstatus: claimed\n---\nbody line\n", encoding="utf-8")
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "state/handoffs/foo.md",
            "old_string": "body line",
            "new_string": "body line, continued",
        },
        "cwd": str(scratch_dir),
    }


def _wg_duplicate_decision_record_id_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """Two `docs/decisions/*.md` files claiming the same `id:` -- absolute
    paths anchored at `scratch_dir` (unlike `_wg_consumed_handoff_fire`
    above, this guard does its own disk I/O of sibling files rather than
    matching on a path string, so it must not depend on the test process's
    real cwd)."""
    decisions = scratch_dir / "docs" / "decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / "DR-1-first.md").write_text("---\nid: DR-1\n---\n", encoding="utf-8")
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(decisions / "DR-1-duplicate.md"),
            "content": "---\nid: DR-1\n---\n",
        },
    }


def _wg_memory_store_cap_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    home = scratch_dir / "home"
    mem = home / ".claude" / "projects" / "-Some-project" / "memory"
    mem.mkdir(parents=True)
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))
    mp.delenv("CLAUDE_HOME", raising=False)
    content = "# Memory Index\n\n" + ("x" * 2100)
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(mem / "MEMORY.md"), "content": content},
    }


def _wg_cutover_phase_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    target = scratch_dir / "state" / "roadmap" / "foo" / "cutovers" / "bar.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nphase: old\n---\nbody\n", encoding="utf-8")
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "state/roadmap/foo/cutovers/bar.md",
            "old_string": "phase: old",
            "new_string": "phase: new",
        },
        "cwd": str(scratch_dir),
    }


def _wg_derived_global_doctrine_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    #: Set both HOME and USERPROFILE (not delenv USERPROFILE) so the
    #: redirect actually resolves on win32 too -- `Path.home()` there reads
    #: USERPROFILE (falling back to HOMEDRIVE/HOMEPATH), never HOME; a bare
    #: `delenv("USERPROFILE")` left `Path.home()` nothing to resolve and it
    #: raised `RuntimeError: Could not determine home directory.` (F13d,
    #: docs/plans/2026-08-07-install-dogfood-mechanical-residue.md, C3).
    mp.setenv("HOME", str(scratch_dir))
    mp.setenv("USERPROFILE", str(scratch_dir))
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(scratch_dir / ".claude" / "CLAUDE.md"), "content": "x"},
    }


def _wg_dev_repo_sentinel_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {"tool_name": "Write", "tool_input": {"file_path": ".coordinator-dev-repo"}}


def _wg_dev_side_mirror_wiki_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    from coordinator_core.write_guards import block_dev_side_mirror_wiki as guard_mod

    plugin_root = scratch_dir / "plugin"
    bundled = plugin_root / "docs" / "wiki"
    bundled.mkdir(parents=True)
    (bundled / "foo.md").write_text("bundled copy\n", encoding="utf-8")
    mp.setattr(guard_mod, "_resolve_plugin_root", lambda: str(plugin_root))
    mp.setenv("HOME", str(scratch_dir))
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(scratch_dir / ".claude" / "docs" / "wiki" / "foo.md"),
            "content": "dev-side edit\n",
        },
    }


def _wg_disarm_marker_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": ".coordinator-bash-guards-disarmed"},
    }


def _wg_em_hand_edit_pending_review_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    sidecar_dir = scratch_dir / "state" / "subagent-share" / "sess-c3c-01"
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "findings.md").write_text(
        "---\nagent_type: coordinator:code-reviewer\n---\n"
        "## Findings\n- target.py: something\n",
        encoding="utf-8",
    )
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "target.py",
            "old_string": "x",
            "new_string": "y",
        },
        "session_id": "sess-c3c-01",
        "cwd": str(scratch_dir),
    }


def _wg_sentinel_retained_review_sidecar_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    sidecar_dir = scratch_dir / "state" / "subagent-share" / "sess-nsr-01"
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "findings.md").write_text(
        "---\nagent_type: coordinator:code-reviewer\n---\n"
        "## Findings\n"
        "- target.py: something\n"
        "<!-- One entry per finding: `- [severity] <finding> "
        "— disposition: accepted | rejected | deferred — rationale: ...` -->\n",
        encoding="utf-8",
    )
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "target.py",
            "old_string": "x",
            "new_string": "y",
        },
        "session_id": "sess-nsr-01",
        "cwd": str(scratch_dir),
    }


def _wg_goals_log_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "goals-log.2026-08.jsonl", "content": "{}"},
    }


def _wg_home_dir_memo_delivery_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    mp.setenv("HOME", str(scratch_dir))
    mp.setenv("USERPROFILE", str(scratch_dir))
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(scratch_dir / ".claude" / "cross-repo" / "inbox" / "foo.md"),
            "content": "hand-written memo",
        },
    }


def _wg_illegal_filename_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {"tool_name": "Write", "tool_input": {"file_path": "bad?name.txt", "content": "x"}}


def _wg_memo_status_hand_edit_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    target = scratch_dir / "cross-repo" / "inbox" / "foo.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nstatus: open\n---\nbody\n", encoding="utf-8")
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "cross-repo/inbox/foo.md",
            "old_string": "status: open",
            "new_string": "status: actioned",
        },
        "cwd": str(scratch_dir),
    }


def _wg_oss_mirror_memo_delivery_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    from coordinator_core.write_guards import block_oss_mirror_memo_delivery as guard_mod

    mp.setattr(guard_mod, "read_publish_mirrors", lambda: {"m": {"path": str(scratch_dir)}})
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(scratch_dir / "cross-repo" / "inbox" / "foo.md"),
            "content": "hand-written memo",
        },
    }


def _wg_priority_ledger_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "state/priority-ledger/foo.yaml", "content": "x"},
    }


def _wg_subagent_archive_write_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "archive/foo/bar.md", "content": "x"},
        "agent_id": "deadbeef0123",
    }


def _wg_subagent_guard_grant_write_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """AC10 coverage-gap closer (C7, docs/plans/2026-08-13-em-exercisable-
    in-band-grant-route.md): fires leg (2) (the DR-260 unlock sentinel) of
    `block_subagent_guard_grant_write` -- no git-repo fixture required,
    unlike leg (1) (the durable grant record), matching the lighter-path
    precedent `block_subagent_grant_record_write`'s own corpus row (just
    above) is registered control-only for."""
    import tempfile as _tempfile

    from coordinator_core.session.guard_unlock_sentinel import _SENTINEL_PREFIX

    sentinel_path = str(
        Path(_tempfile.gettempdir()) / f"{_SENTINEL_PREFIX}deadbeef0123.some-guard"
    )
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": sentinel_path, "content": "1"},
        "agent_id": "deadbeef0123",
    }


def _wg_subagent_plan_body_write_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    from coordinator_core.write_guards import block_subagent_plan_body_write as guard_mod

    mp.setattr(guard_mod, "_resolve_git_root", lambda cwd: str(scratch_dir))
    mp.setattr(
        guard_mod, "_read_backpointer_subagent_type", lambda git_root, agent_id: "coordinator:executor"
    )
    mp.setattr(guard_mod, "_write_block_log", lambda *a, **kw: None)
    mp.setattr(guard_mod, "_write_hook_emit_log", lambda *a, **kw: None)
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": "docs/plans/foo.md", "old_string": "x", "new_string": "y"},
        "cwd": str(scratch_dir),
        "agent_id": "aexecutor-teammate-1234567890abcdef",
        "session_id": "sess-c3c-02",
    }


def _wg_unauthorized_claude_md_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "CLAUDE.md", "content": "x"},
        "agent_id": "deadbeef0123",
        "cwd": str(scratch_dir),
    }


def _wg_worktree_sentinel_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": ".coordinator-override-worktree-guard"},
    }


def _wg_check_claude_md_size_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    from coordinator_core.claude_md_budget import HARD_LIMIT_BYTES

    #: See `_wg_derived_global_doctrine_fire` above for why USERPROFILE is
    #: set, not deleted (F13d, C3).
    mp.setenv("HOME", str(scratch_dir))
    mp.setenv("USERPROFILE", str(scratch_dir))
    content = "x" * (HARD_LIMIT_BYTES + 5000)
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(scratch_dir / ".claude" / "CLAUDE.md"), "content": content},
    }


def _wg_concrete_path_citations_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    subprocess.run(["git", "init", "-q", str(scratch_dir)], check=True)
    target = scratch_dir / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    # Neutral stand-in, deliberately not a real codename: this guard echoes the
    # offending path straight back to the reader, so a codename here renders a
    # redaction placeholder into the corpus and trips B7 on our own fixture
    # rather than on anything the engine actually ships.
    offending = "the repo lives at " + "X:" + r"\some-checkout"
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": offending},
        "session_id": "sess-c3c-cpc",
    }


def _wg_doctrine_surface_edits_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": ".coordinator-doctrine-edit-approved"},
    }


def _wg_settings_json_write_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    from coordinator_core.write_guards import guard_settings_json_write as guard_mod

    mp.setenv("CLAUDE_CONFIG_DIR", str(scratch_dir))
    # `_foreign_path_match` is OS-conditioned: on a non-Windows host it looks
    # for a Windows-shaped drive-letter path (the "wrong OS leaked into this
    # settings file" signal), not a POSIX `/Users/...` path -- that branch is
    # the Windows-host leg instead. Force the non-Windows leg so this fires
    # deterministically regardless of the box this suite runs on.
    mp.setattr(guard_mod, "_is_windows", lambda: False)
    drive_path = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "x"  # abs-path-ok: synthetic fixture, not a real path
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(scratch_dir / "settings.json"),
            "content": '{"path": "%s"}' % drive_path,
        },
    }


def _wg_baton_body_bar_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/handoffs/2026-08-03-foo.md",
            "content": "---\ntitle: t\n---\n| a | b |\n| c | d |\n| e | f |\n",
        },
    }


def _wg_em_code_dispatch_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    from coordinator_core.hooks import nudge_em_code_dispatch as hook_mod

    # The F7 bootstrap/out-of-repo carve-out treats a synthetic `/repo/...`
    # path as outside any real git work-tree and bypasses the nudge for that
    # (unrelated) reason -- pinned off so this row exercises the size-floor
    # firing path this guard's own row is testing, mirroring
    # `test_nudge_em_code_dispatch.py`'s own `_outside_f7_carveout_scope`
    # fixture.
    mp.setattr(hook_mod, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    return {
        "tool_name": "Edit",
        "session_id": "sess-c3c-ecd",
        "tool_input": {
            "file_path": "/repo/pkg/module.py",
            "old_string": "def f():\n    return 1",
            "new_string": "def f():\n    return compute_something(x, y)",
        },
    }


def _wg_handoff_ac_shape_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    """`kind: spinoff` (not `session-handoff`, which the guard is silent on
    per its own docstring Fire condition) with a `## Acceptance criteria`
    heading whose body is prose bullets, not `- [ ]`/`- [x]` checkboxes --
    `parse_consumed_handoff_acceptance_criteria` returns `total == 0` for
    this shape, which is exactly the fire condition."""
    content = (
        "---\n"
        "kind: spinoff\n"
        "---\n"
        "## Acceptance criteria\n"
        "\n"
        "AC1 met, verified by tests.\n"
        "AC2 met, see the diff.\n"
    )
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "state/handoffs/foo.md", "content": content},
        "cwd": str(scratch_dir),
    }


def _wg_improvement_queue_write_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/improvement-queue/new-item.yaml",
            "content": "title: test\ndescription: a thing",
        },
    }


def _wg_new_sh_file_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    target = scratch_dir / "foo.sh"
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "echo hi\n"},
    }


def _wg_prose_queue_append_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    state_dir = scratch_dir / "state"
    state_dir.mkdir(exist_ok=True)
    target = state_dir / "bug-backlog.md"
    target.write_text("- 2026-07-01 | old | entry\n", encoding="utf-8")
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": "- 2026-07-01 | old | entry\n- 2026-08-03 | new | entry\n",
        },
    }


def _wg_prose_queue_creation_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    target = scratch_dir / "state" / "improvement-queue.md"
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": "- 2026-08-03 | new entry | details",
        },
    }


def _wg_tasks_state_folder_split_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "tasks/orientation_cache.md", "content": "x"},
    }


def _wg_plan_sidecar_family_split_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """`staff-eng-review` is a persona name, never one of the four row-39
    plan-derivable lenses, so `state/plan-sidecars/` is the wrong family for
    it -- the shape the guard exists to redirect."""
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/plan-sidecars/2026-08-03-some-plan.staff-eng-review.md",
            "content": "x",
        },
    }


def _wg_terminal_artifact_edit_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    target = scratch_dir / "docs" / "plans" / "foo.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nstatus: implemented\n---\nbody\n", encoding="utf-8")
    # `new_string` must carry a forward-binding instruction tell (`_INSTRUCTION_RE`
    # — must/should/do not/going forward). The guard checks the Edit's DELTA, not
    # the file, and deliberately stays silent for ordinary prose: recording
    # correspondence or history against a delivered plan is legitimate, and only
    # an instruction meant to constrain FUTURE work is what this guard targets.
    # A payload of plain text ("body2") exercised the silent path while the row
    # asserted a speaker, so the row failed on the fixture rather than on the guard.
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "docs/plans/foo.md",
            "old_string": "body",
            "new_string": "body\n\nAnti-scope: future sessions must not widen this surface.\n",
        },
        "cwd": str(scratch_dir),
    }


def _wg_windows_subprocess_popup_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "run.py",
            "content": 'import subprocess\nsubprocess.run(["powershell.exe", "-Command", "dir"])\n',
        },
    }


def _wg_shell_shaped_spawn_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    """`shell=True` is the canonical shell-shaped spawn `spawn_policy.
    sites_in_source` detects. The `.py` suffix is load-bearing -- the guard
    reconstructs a whole-file Python buffer before walking it, so a
    non-Python path is never scanned (which is also why `_wg_benign`'s
    `.txt` write is a valid non-firing control for this guard)."""
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "run.py",
            "content": 'import subprocess\nsubprocess.run("ls -la", shell=True)\n',
        },
    }


def _wg_unmarked_spawning_test_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A spawning test file carrying TWO unmarked functions, neither
    `spawns_process` nor a module-level `pytestmark`, at a realistic
    ABSOLUTE path -- the guard's real `file_path` always
    comes from the tool payload, which is always absolute, and a real
    firing routinely names more than one function; a bare relative
    `"test_thing.py"` with zero names was the smallest shape the guard can
    render and hid the over-cap failure from this suite's own message-size
    gate. `scratch_dir` gives a real absolute prefix without this row
    needing a real repo tree on disk (the guard never reads this path for
    a `Write`). The `test_`-prefixed basename is load-bearing twice over:
    it is what `_is_test_tree_path` gates on, and it is the same filename
    test the ratchet's own `_iter_test_files` uses to decide membership --
    so `_wg_benign`'s `.txt` write stays a valid non-firing control here
    for the path reason as well as the suffix one."""
    file_path = str(scratch_dir / "coordinator_core" / "tests" / "test_no_new_spawning_tests.py")
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": file_path,
            "content": (
                "import subprocess\n"
                "\n"
                "\n"
                "def test_rule2_new_spawning_files_ratchet():\n"
                '    subprocess.run(["git", "status"], check=True)\n'
                "\n"
                "\n"
                "def test_rule4_every_spawning_file_is_cadence_tiered():\n"
                '    subprocess.run(["git", "log"], check=True)\n'
            ),
        },
    }


def _wg_outbox_draft_frontmatter_shape_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """`status: open` -- a hand-authored draft copying the shape of a
    *received* memo, the exact incident this guard exists for (module
    docstring: "closes the... gap"). `validate_outbox_frontmatter` rejects
    `status: open` (only `draft`/`sent` are valid pre-send), so this fires."""
    content = (
        "---\n"
        "title: \"a memo\"\n"
        "from: \"claude-klabauter-em\"\n"
        "to: \"some-em\"\n"
        "created: 2026-08-07\n"
        "status: open\n"
        "delivery_mode: receiver-repo\n"
        "summary: \"a summary\"\n"
        "---\n"
        "body\n"
    )
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "state/memo-outbox/foo.md", "content": content},
        "cwd": str(scratch_dir),
    }


def _wg_peer_notice_unread_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """Sends a real peer notice (via `peer_notice_send._peer_notice_send`,
    the same op the guard's own dedicated test file uses) addressed to
    `session_id`, then returns the Edit payload for that same session --
    `nudge_peer_notice_unread.check` surfaces it as `additionalContext`.
    `main_worktree_root`/`harness_registry.snapshot` are monkeypatched the
    same way `test_nudge_peer_notice_unread.py::_send` does, scoped to
    this row's own scratch dir."""
    (scratch_dir / ".git").mkdir()
    session_id = "peer-notice-corpus-row"
    mp.setattr(_ops_peer_notice_send, "main_worktree_root", lambda p: scratch_dir)
    mp.setattr(_ops_peer_notice_send.harness_registry, "snapshot", lambda: {})
    _ops_peer_notice_send._peer_notice_send(
        {
            "target_session_id": session_id,
            "artifact_path": "a.py",
            "message": "I am editing this function",
            "from_session_id": "sender-1",
        },
        repo_root=scratch_dir,
    )
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": "whatever.py"},
        "session_id": session_id,
        "cwd": str(scratch_dir),
    }


def _wg_private_git_fact_resolver_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """FIRE-SET membership (module docstring): a hand-rolled `git rev-parse
    --show-toplevel` spawn inside a hot-path module (`write_guards/`) --
    `coordinator_core.git.repo_root.show_toplevel` is the offered
    non-spawning seam."""
    content = (
        "import subprocess\n"
        "subprocess.run(['git', 'rev-parse', '--show-toplevel'])\n"
    )
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "write_guards/private_resolver.py", "content": content},
    }


WRITE_GUARD_ROWS: List[WriteGuardRow] = [
    WriteGuardRow("block_completion_monolith_write", "fire", True, _wg_completion_monolith_fire),
    WriteGuardRow("block_completion_monolith_write", "control", False, _wg_benign),
    WriteGuardRow("block_consumed_handoff_edit", "fire", True, _wg_consumed_handoff_fire),
    WriteGuardRow("block_consumed_handoff_edit", "control", False, _wg_benign),
    WriteGuardRow(
        "block_duplicate_decision_record_id",
        "fire",
        True,
        _wg_duplicate_decision_record_id_fire,
    ),
    WriteGuardRow("block_duplicate_decision_record_id", "control", False, _wg_benign),
    WriteGuardRow(
        "bump_out_of_repo_tool_write",
        "control",
        False,
        _wg_benign,
        unverified_reason=(
            "AC2-registration-only: a real fire needs session_start.write_session_start_record "
            "plus two real git-repo fixtures (own/foreign) and a registry.toml -- more state than "
            "this row is worth per the plan's own lighter-path sanction (see "
            "test_bump_out_of_repo_tool_write.py's own _init_repo/_payload harness for the real "
            "fixture, reusable by a future chunk that wants it fire-verified)."
        ),
    ),
    WriteGuardRow("guard_memory_store_cap", "fire", True, _wg_memory_store_cap_fire),
    WriteGuardRow("guard_memory_store_cap", "control", False, _wg_benign),
    WriteGuardRow("block_cutover_phase_hand_edit", "fire", True, _wg_cutover_phase_fire),
    WriteGuardRow("block_cutover_phase_hand_edit", "control", False, _wg_benign),
    WriteGuardRow(
        "block_derived_global_doctrine_write", "fire", True, _wg_derived_global_doctrine_fire
    ),
    WriteGuardRow("block_derived_global_doctrine_write", "control", False, _wg_benign),
    WriteGuardRow("block_dev_repo_sentinel_write", "fire", True, _wg_dev_repo_sentinel_fire),
    WriteGuardRow("block_dev_repo_sentinel_write", "control", False, _wg_benign),
    WriteGuardRow("block_dev_side_mirror_wiki", "fire", True, _wg_dev_side_mirror_wiki_fire),
    WriteGuardRow("block_dev_side_mirror_wiki", "control", False, _wg_benign),
    WriteGuardRow("block_disarm_marker_sentinel_write", "fire", True, _wg_disarm_marker_fire),
    WriteGuardRow("block_disarm_marker_sentinel_write", "control", False, _wg_benign),
    WriteGuardRow(
        "block_em_hand_edit_pending_review_integration",
        "fire",
        True,
        _wg_em_hand_edit_pending_review_fire,
    ),
    WriteGuardRow("block_em_hand_edit_pending_review_integration", "control", False, _wg_benign),
    WriteGuardRow("block_goals_log_hand_write", "fire", True, _wg_goals_log_fire),
    WriteGuardRow("block_goals_log_hand_write", "control", False, _wg_benign),
    WriteGuardRow("block_home_dir_memo_delivery", "fire", True, _wg_home_dir_memo_delivery_fire),
    WriteGuardRow("block_home_dir_memo_delivery", "control", False, _wg_benign),
    WriteGuardRow("block_illegal_filename", "fire", True, _wg_illegal_filename_fire),
    WriteGuardRow("block_illegal_filename", "control", False, _wg_benign),
    WriteGuardRow("block_memo_status_hand_edit", "fire", True, _wg_memo_status_hand_edit_fire),
    WriteGuardRow("block_memo_status_hand_edit", "control", False, _wg_benign),
    WriteGuardRow(
        "block_oss_mirror_memo_delivery", "fire", True, _wg_oss_mirror_memo_delivery_fire
    ),
    WriteGuardRow("block_oss_mirror_memo_delivery", "control", False, _wg_benign),
    WriteGuardRow("block_priority_ledger_edit", "fire", True, _wg_priority_ledger_fire),
    WriteGuardRow("block_priority_ledger_edit", "control", False, _wg_benign),
    WriteGuardRow("block_subagent_archive_write", "fire", True, _wg_subagent_archive_write_fire),
    WriteGuardRow("block_subagent_archive_write", "control", False, _wg_benign),
    WriteGuardRow(
        "block_subagent_grant_record_write",
        "control",
        False,
        _wg_benign,
        unverified_reason=(
            "AC2-registration-only: a real fire needs a resolvable git common dir plus a "
            "coordinator-sessions/<sid>/claude-md-write-grant.json target under it -- more "
            "environment state than this row is worth per the plan's own lighter-path "
            "sanction (see block_subagent_grant_record_write.py's own test file for the "
            "real fixture, reusable by a future chunk that wants it fire-verified). Was "
            "discovered by write_guards.engine.discover_guard_names() but had NO corpus "
            "row at all until this dispatch (C7, docs/plans/2026-08-13-guard-messages-"
            "stop-handing-agents-the-keys.md) -- registering it closes that gap in the "
            "sweep, per AC-8."
        ),
    ),
    WriteGuardRow(
        "block_subagent_guard_grant_write",
        "fire",
        True,
        _wg_subagent_guard_grant_write_fire,
    ),
    WriteGuardRow("block_subagent_guard_grant_write", "control", False, _wg_benign),
    WriteGuardRow(
        "block_subagent_plan_body_write", "fire", True, _wg_subagent_plan_body_write_fire
    ),
    WriteGuardRow("block_subagent_plan_body_write", "control", False, _wg_benign),
    WriteGuardRow(
        "block_unauthorized_claude_md_write", "fire", True, _wg_unauthorized_claude_md_fire
    ),
    WriteGuardRow("block_unauthorized_claude_md_write", "control", False, _wg_benign),
    WriteGuardRow("block_worktree_sentinel_write", "fire", True, _wg_worktree_sentinel_fire),
    WriteGuardRow("block_worktree_sentinel_write", "control", False, _wg_benign),
    WriteGuardRow("check_claude_md_size", "fire", True, _wg_check_claude_md_size_fire),
    WriteGuardRow("check_claude_md_size", "control", False, _wg_benign),
    WriteGuardRow(
        "guard_concrete_path_citations", "fire", True, _wg_concrete_path_citations_fire
    ),
    WriteGuardRow("guard_concrete_path_citations", "control", False, _wg_benign),
    WriteGuardRow("guard_doctrine_surface_edits", "fire", True, _wg_doctrine_surface_edits_fire),
    WriteGuardRow("guard_doctrine_surface_edits", "control", False, _wg_benign),
    WriteGuardRow("guard_settings_json_write", "fire", True, _wg_settings_json_write_fire),
    WriteGuardRow("guard_settings_json_write", "control", False, _wg_benign),
    WriteGuardRow("nudge_baton_body_bar", "fire", True, _wg_baton_body_bar_fire),
    WriteGuardRow("nudge_baton_body_bar", "control", False, _wg_benign),
    WriteGuardRow("nudge_em_code_dispatch", "fire", True, _wg_em_code_dispatch_fire),
    WriteGuardRow("nudge_em_code_dispatch", "control", False, _wg_benign),
    WriteGuardRow("nudge_handoff_ac_shape", "fire", True, _wg_handoff_ac_shape_fire),
    WriteGuardRow("nudge_handoff_ac_shape", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_improvement_queue_write", "fire", True, _wg_improvement_queue_write_fire
    ),
    WriteGuardRow("nudge_improvement_queue_write", "control", False, _wg_benign),
    WriteGuardRow("nudge_new_sh_file_naked_python", "fire", True, _wg_new_sh_file_fire),
    WriteGuardRow("nudge_new_sh_file_naked_python", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_outbox_draft_frontmatter_shape",
        "fire",
        True,
        _wg_outbox_draft_frontmatter_shape_fire,
    ),
    WriteGuardRow("nudge_outbox_draft_frontmatter_shape", "control", False, _wg_benign),
    WriteGuardRow("nudge_peer_notice_unread", "fire", True, _wg_peer_notice_unread_fire),
    WriteGuardRow("nudge_peer_notice_unread", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_plan_sidecar_family_split", "fire", True, _wg_plan_sidecar_family_split_fire
    ),
    WriteGuardRow("nudge_plan_sidecar_family_split", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_private_git_fact_resolver", "fire", True, _wg_private_git_fact_resolver_fire
    ),
    WriteGuardRow("nudge_private_git_fact_resolver", "control", False, _wg_benign),
    WriteGuardRow("nudge_prose_queue_append", "fire", True, _wg_prose_queue_append_fire),
    WriteGuardRow("nudge_prose_queue_append", "control", False, _wg_benign),
    WriteGuardRow("nudge_prose_queue_creation", "fire", True, _wg_prose_queue_creation_fire),
    WriteGuardRow("nudge_prose_queue_creation", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_sentinel_retained_review_sidecar",
        "fire",
        True,
        _wg_sentinel_retained_review_sidecar_fire,
    ),
    WriteGuardRow("nudge_sentinel_retained_review_sidecar", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_tasks_state_folder_split", "fire", True, _wg_tasks_state_folder_split_fire
    ),
    WriteGuardRow("nudge_tasks_state_folder_split", "control", False, _wg_benign),
    WriteGuardRow("nudge_shell_shaped_spawn", "fire", True, _wg_shell_shaped_spawn_fire),
    WriteGuardRow("nudge_shell_shaped_spawn", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_unmarked_spawning_test", "fire", True, _wg_unmarked_spawning_test_fire
    ),
    WriteGuardRow("nudge_unmarked_spawning_test", "control", False, _wg_benign),
    WriteGuardRow("nudge_terminal_artifact_edit", "fire", True, _wg_terminal_artifact_edit_fire),
    WriteGuardRow("nudge_terminal_artifact_edit", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_windows_subprocess_popup", "fire", True, _wg_windows_subprocess_popup_fire
    ),
    WriteGuardRow("nudge_windows_subprocess_popup", "control", False, _wg_benign),
    WriteGuardRow(
        "validate_frontmatter_schema_advisory",
        "control",
        False,
        _wg_benign,
        unverified_reason=(
            "AC2-registration-only: this guard's real fire reads DoE-claude's live schema "
            "corpus/registry manifest off a sibling checkout (coordinator_doe_root()) -- not "
            "reproducible from a synthetic scratch dir without standing up that sibling tree, "
            "which is more environment state than this row is worth per the plan's own "
            "lighter-path sanction."
        ),
    ),
    WriteGuardRow(
        "validate_frontmatter_schema_deny",
        "control",
        False,
        _wg_benign,
        unverified_reason=(
            "AC2-registration-only: same sibling-checkout dependency as "
            "validate_frontmatter_schema_advisory above."
        ),
    ),
]


#: Sanity invariant this module itself relies on -- every guard `write_guards.
#: engine.discover_guard_names()` reports has at least one row above.
_WG_NAMES, _WG_IMPORT_FAILED = write_guards_engine.discover_guard_names()
#: Moved out of module scope into
#: `test_guard_corpus_registration_invariants.py` (docs/plans/2026-08-07-
#: install-dogfood-mechanical-residue.md, chunk C3, F13a) -- same rationale
#: as `_FLIPPED_TO_ADVISORY_REWRITE` above. `_WG_NAMES`/`_WG_IMPORT_FAILED`
#: stay here as the shared computation the moved assertions depend on; a
#: real import failure among the discovered write guards must still surface
#: loudly, now as a failing test rather than a collection error.


# ---------------------------------------------------------------------------
# hooks/ rows -- the three modules C6b routed onto the shared
# `_hook_envelope` chokepoint this wave. See the section docstring above for
# why the remaining hooks/ modules are out of this chunk's scope.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookCapture:
    name: str
    band: str
    envelope: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class HookRow:
    guard: str
    row_id: str
    expected_speaker: bool
    #: Returns the `op()`-shaped payload dict, and the `op` callable itself
    #: (each of the three hooks modules exposes its own `op(payload)`, no
    #: shared signature to factor out here).
    fire: Callable[[], Optional[Dict[str, Any]]]


def _hook_envelope_from_message(message: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Wrap a hooks `op()` return (`{"message": str}` or `None`) into the
    `hookSpecificOutput.additionalContext`-shaped dict `_message_size.
    measure_envelope` reads -- `op()` already strips the envelope down to
    the bare message string (C6b routed it through `_hook_envelope` and
    then unwrapped it back out for its own DoE-side consumer), so this is
    the inverse of that unwrap, not a new envelope shape."""
    if not message:
        return None
    text = message.get("message")
    if not isinstance(text, str) or not text:
        return None
    return {"hookSpecificOutput": {"additionalContext": text}}


def _fire_em_report_altitude(text: str) -> Optional[Dict[str, Any]]:
    """Fires `em_report_altitude.op()` inside a fresh scratch git repo, with
    the bark-once tally sentinel redirected via `COORDINATOR_EM_REPORT_
    ALTITUDE_TALLY_DIR` -- WITHOUT this, `cwd=None` resolves the tally
    against the REAL invoking repo's `.git/coordinator-sessions/<sid>/`
    (confirmed live: an earlier pass of this fixture left exactly that
    artifact behind in this checkout's `.git`), which is both a real-repo
    write this test suite must never make and, per state/lessons/2026-08-01-
    adding-suppression-to-an-emitter-silently-breaks-*, a stale-suppressed-
    output trap on any session id reused across a second call. Mirrors
    `test_em_report_altitude.py`'s own `_tally_isolation` fixture."""
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-era-") as scratch:
        scratch_dir = Path(scratch)
        os.makedirs(scratch_dir / ".git")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(
                "COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR",
                str(scratch_dir / ".tally-isolation"),
            )
            payload = {
                "session_id": "sess-%s" % uuid.uuid4().hex,
                "cwd": str(scratch_dir),
                "stop_hook_active": False,
                "last_assistant_message": text,
            }
            return _hook_envelope_from_message(_hook_em_report_altitude.op(payload))


def _fire_em_report_altitude_d2() -> Optional[Dict[str, Any]]:
    return _fire_em_report_altitude("See test.py:42 and foo/bar.py:10 for details.")


def _fire_em_report_altitude_control() -> Optional[Dict[str, Any]]:
    return _fire_em_report_altitude("Done.")


def _fire_nudge_harness_directive_dispatch() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-nhdd-") as scratch:
        scratch_dir = Path(scratch)
        os.makedirs(scratch_dir / ".git")
        transcript = scratch_dir / "transcript.jsonl"
        with open(transcript, "w", encoding="utf-8") as fh:
            import json as _json

            fh.write(_json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")
            fh.write(
                _json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "Want me to dispatch an executor for this?"}
                            ]
                        },
                    }
                )
                + "\n"
            )
        payload = {
            "session_id": "sess-c3c-nhdd",
            "transcript_path": str(transcript),
            "cwd": str(scratch_dir),
            "stop_hook_active": False,
        }
        return _hook_envelope_from_message(_hook_nudge_harness_directive_dispatch.op(payload))


def _fire_nudge_harness_directive_dispatch_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-nhdd-ctrl-") as scratch:
        scratch_dir = Path(scratch)
        os.makedirs(scratch_dir / ".git")
        transcript = scratch_dir / "transcript.jsonl"
        with open(transcript, "w", encoding="utf-8") as fh:
            import json as _json

            fh.write(_json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")
            fh.write(
                _json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Dispatched; verdict OK."}]},
                    }
                )
                + "\n"
            )
        payload = {
            "session_id": "sess-c3c-nhdd-ctrl",
            "transcript_path": str(transcript),
            "cwd": str(scratch_dir),
            "stop_hook_active": False,
        }
        return _hook_envelope_from_message(_hook_nudge_harness_directive_dispatch.op(payload))


def _fire_nudge_unrouted_sizing() -> Optional[Dict[str, Any]]:
    """The lighter path the C3c dispatch stub names for this module (§
    Enriched Dispatch Stubs, C3 -- `hooks/nudge_unrouted_sizing.py`): standing
    up the full session-state fixture (`_git_init`/`_write_touched`/
    `_write_sizing`) this module's own test file builds costs more than this
    row is worth, so this calls `_build_plan_message` directly with a
    synthetic route -- a lighter-weight measurement path than replaying
    `op()` end-to-end, per the stub's own suggestion. Wrapped into the same
    `hookSpecificOutput.additionalContext` shape as the other two hooks rows
    so it measures through `_message_size` identically, even though it never
    goes through `op()` or the `_hook_envelope` chokepoint itself."""
    text = _hook_nudge_unrouted_sizing._build_plan_message("docs/plans/foo.md", "ready")
    return {"hookSpecificOutput": {"additionalContext": text}}



# ---------------------------------------------------------------------------
# C12 -- corpus rows for the 23 hooks/ modules C3c/C10 left uncovered (0 of
# 3 already-covered em_report_altitude/nudge_harness_directive_dispatch/
# nudge_unrouted_sizing above). Per module, exactly one of three outcomes
# (no fourth): (1) a real firing row, (2) a named uncapturable reason, (3)
# "no agent-facing emitter" with evidence. C10's own comment above claims
# an `async def _handler` MCP-tool-shaped op "cannot invoke... without a
# second, differently-shaped capture seam" -- that claim does not survive
# contact with the actual modules: every async `_handler(params, repo_root)`
# below is a plain coroutine, fired the same way `test_subagent_arrival_
# check.py` et al. already do in this tree (`asyncio.run(_handler(...))`
# inside a zero-arg sync closure) -- no new harness infrastructure, just a
# wrapper this module already had the tools to write.
# ---------------------------------------------------------------------------

import asyncio as _hooks_asyncio

from coordinator_core.hooks import block_unenumerated_agent_type as _hook_block_unenumerated_agent_type
from coordinator_core.hooks import coordinator_reminder as _hook_coordinator_reminder
from coordinator_core.hooks import enforce_agent_model_pin as _hook_enforce_agent_model_pin
from coordinator_core.hooks import nudge_em_code_dispatch as _hook_nudge_em_code_dispatch
from coordinator_core.hooks import nudge_foreground_agent_dispatch as _hook_nudge_foreground_agent_dispatch
from coordinator_core.hooks import nudge_named_agent_report_delivery as _hook_nudge_named_agent_report_delivery
from coordinator_core.hooks import nudge_unauthorized_handoff as _hook_nudge_unauthorized_handoff
from coordinator_core.hooks import postuse_advisory_dispatch as _hook_postuse_advisory_dispatch
from coordinator_core.hooks import example_retrieval_repo_detect as _hook_example_retrieval_repo_detect
from coordinator_core.hooks import receiver_state_sensor as _hook_receiver_state_sensor
from coordinator_core.hooks import subagent_sidecar_fill_check as _hook_subagent_sidecar_fill_check
from coordinator_core.hooks import suggest_sonnet_research as _hook_suggest_sonnet_research
from coordinator_core.hooks import ue_knowledge_distrust as _hook_ue_knowledge_distrust
from coordinator_core.hooks import agent_completion_log as _hook_agent_completion_log
from coordinator_core.hooks import context_pressure_precompact as _hook_context_pressure_precompact
from coordinator_core.hooks import session_heartbeat as _hook_session_heartbeat
from coordinator_core.hooks import subagent_arrival_check as _hook_subagent_arrival_check
from coordinator_core.hooks import subagent_fabrication_check as _hook_subagent_fabrication_check
from coordinator_core.hooks import subagent_zero_tool_use as _hook_subagent_zero_tool_use
from coordinator_core.hooks import subagent_zero_tool_use_resolve as _hook_subagent_zero_tool_use_resolve
from coordinator_core.hooks import subagent_zero_tool_use_surface as _hook_subagent_zero_tool_use_surface
from coordinator_core.hooks import track_dispatched_agents as _hook_track_dispatched_agents
from coordinator_core.hooks import track_touched_files as _hook_track_touched_files


def _to_envelope_or_none(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize a raw hook return into the `envelope-or-None` shape `HookCapture`
    expects -- `no_advisory()` returns `{}` (falsy but not `None`), and the
    structured-JSON-RPC-result modules (subagent_arrival_check et al.) return a
    non-empty dict that carries no `hookSpecificOutput` key at all. Both count as
    "did not speak" for this corpus's purposes."""
    if not result:
        return None
    if not isinstance(result, dict) or "hookSpecificOutput" not in result:
        return None
    return result


# --- (1) agent_completion_log -- write-only PostToolUse op; module docstring's
# own Negative-spec: "Do NOT return advisory text ... always return
# no_advisory()." Verified live: params={} takes the no-repo_root early return.
def _fire_agent_completion_log_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_agent_completion_log._handler({}, repo_root=None))
    )


# --- (2) auto_push -- no register_op, no hookSpecificOutput/_envelope import
# anywhere in the module (grep-verified); it is a standalone `python3 -m
# coordinator_core.hooks.auto_push` post-commit CLI script whose only output is
# `print(..., file=sys.stderr)` console diagnostics for a human reading the
# commit's terminal output, never an additionalContext/permissionDecisionReason/
# deny envelope surfaced to the model. NOT FIRED here: its only entrypoint
# (`main()`) performs a real `git push` against the invoking checkout's actual
# remote -- there is no side-effect-free unit to call, and invoking it would be
# a real network mutation this corpus must never make. Classification 3 rests on
# the static evidence above, not a captured row.

# --- (3) block_unenumerated_agent_type -- real firing row: check() denies an
# unenumerated subagent_type via deny() -> real PreToolUse deny text.
def _fire_block_unenumerated_agent_type() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "totally-bogus-nonexistent-role-zz"},
    }
    return _to_envelope_or_none(_hook_block_unenumerated_agent_type.check(payload))


def _fire_block_unenumerated_agent_type_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {}}
    return _to_envelope_or_none(_hook_block_unenumerated_agent_type.check(payload))


# --- (4) context_pressure_precompact -- write-only PreCompact op; every branch
# in `_handler` returns `no_advisory()` (grep-verified: the module's only
# `return` besides early exits is `no_advisory()`). Verified live with params={}.
def _fire_context_pressure_precompact_noop() -> Optional[Dict[str, Any]]:
    # `_handler` here is a plain sync `def` (unlike most of this section's other
    # modules) -- confirmed by grep: no `async` on its definition line.
    return _to_envelope_or_none(_hook_context_pressure_precompact._handler({}, repo_root=None))


# --- (5) enforce_agent_model_pin -- real firing row: check() denies a pinned
# model violation. `resolve_model_pins` is monkeypatched exactly as
# hooks/tests/test_enforce_agent_model_pin.py's own `_patch_pins` does.
def _fire_enforce_agent_model_pin() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            _hook_enforce_agent_model_pin,
            "resolve_model_pins",
            lambda *, doe_root=None: (
                {
                    "coordinator:executor": {
                        "model": "sonnet",
                        # Deliberately no `<letter>:` before the slash -- the
                        # Windows-drive-letter guard-message ratchet's
                        # unanchored predicate would false-fire on the "e:"
                        # ending "test-fixture:" (AC9-R finding 1).
                        "_source_path": "test-fixture-source/coordinator/agents/executor.md",
                    }
                },
                None,
            ),
        )
        payload = {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "coordinator:executor", "model": "opus", "prompt": "go"},
        }
        return _to_envelope_or_none(_hook_enforce_agent_model_pin.check(payload))


def _fire_enforce_agent_model_pin_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            _hook_enforce_agent_model_pin,
            "resolve_model_pins",
            lambda *, doe_root=None: ({}, None),
        )
        payload = {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "coordinator:executor", "model": "opus", "prompt": "go"},
        }
        return _to_envelope_or_none(_hook_enforce_agent_model_pin.check(payload))


# --- (6) nudge_em_code_dispatch -- real firing row via `op()` (the sync
# direct-call shape this module ALSO carries per C10's own comment; the async
# `_handler` never receives old_string/new_string/content per this module's own
# MultiEdit negative-spec, so `op()` is the shape with an agent-facing message).
def _fire_nudge_em_code_dispatch() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_input": {"file_path": "src/module.py", "content": "def foo():\n    return 42\n"},
        "session_id": "sess-c12-nemcd-fire-%s" % uuid.uuid4().hex,
    }
    return _to_envelope_or_none(_hook_nudge_em_code_dispatch.op(payload))


def _fire_nudge_em_code_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_input": {"file_path": "docs/notes.md", "content": "some notes\n"},
        "session_id": "sess-c12-nemcd-ctrl-%s" % uuid.uuid4().hex,
    }
    return _to_envelope_or_none(_hook_nudge_em_code_dispatch.op(payload))


# --- (7) nudge_foreground_agent_dispatch -- real firing row: `run_in_background`
# present-and-false rewrites into a backgrounded dispatch via rewrite_input(),
# whose attached `context` is the AC9-fixed AUTO-REROUTED advisory text.
def _fire_nudge_foreground_agent_dispatch() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "run_in_background": "false",
        "session_id": "sess-c12-nfad-fire",
        "tool_input": {"prompt": "do the thing"},
    }
    return _to_envelope_or_none(_hook_nudge_foreground_agent_dispatch._handler(payload, repo_root=None))


def _fire_nudge_foreground_agent_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "run_in_background": "true",
        "session_id": "sess-c12-nfad-ctrl",
        "tool_input": {"prompt": "do the thing"},
    }
    return _to_envelope_or_none(_hook_nudge_foreground_agent_dispatch._handler(payload, repo_root=None))


# Review: code-reviewer — the fire-reroute fixture above always forwards a `prompt`,
# so it only ever exercises the reroute leg (_REROUTE_NOTICE). This fixture forwards
# no forwardable `prompt` (D8's "absent/empty/missing prompt" trio) on an otherwise
# identical present-and-false payload, taking the deny fallback branch instead, so
# the corpus's banned-vocabulary sweep also scans _DENY_MSG_TEMPLATE at least once.
def _fire_nudge_foreground_agent_dispatch_deny() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "run_in_background": "false",
        "session_id": "sess-c12-nfad-deny",
        "tool_input": {},
    }
    return _to_envelope_or_none(_hook_nudge_foreground_agent_dispatch._handler(payload, repo_root=None))


# --- (8) nudge_named_agent_report_delivery -- real firing row: a named Agent
# dispatch with no SendMessage-to-main delivery instruction advises (AC9 fix).
def _fire_nudge_named_agent_report_delivery() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "Investigate the thing. Report back: findings.",
            "name": "flag-emitter",
        },
    }
    return _to_envelope_or_none(_hook_nudge_named_agent_report_delivery._handler(payload))


def _fire_nudge_named_agent_report_delivery_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"prompt": "investigate X"}}
    return _to_envelope_or_none(_hook_nudge_named_agent_report_delivery._handler(payload))


# --- (9) nudge_unauthorized_handoff -- real firing row: a Write into
# state/handoffs/ with no authoring skill active and no kind:recovery
# frontmatter fires the nudge via post_advisory().
def _fire_nudge_unauthorized_handoff() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Write",
        "file_path": "state/handoffs/2026-08-12-example.md",
        "content": "no frontmatter here\n",
        "transcript_path": "",
    }
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_nudge_unauthorized_handoff._handler(payload, repo_root=None))
    )


def _fire_nudge_unauthorized_handoff_control() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Write",
        "file_path": "state/handoffs/2026-08-12-example.md",
        "content": "kind: recovery\n",
        "transcript_path": "",
    }
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_nudge_unauthorized_handoff._handler(payload, repo_root=None))
    )


# --- (10) platform_localize -- no register_op, no hookSpecificOutput/_envelope
# import anywhere in the module (grep-verified); its only entrypoint (`main()`)
# is a standalone settings-localization CLI writing `.claude/settings.local.json`
# and printing WARNING lines to stderr for a human, never an
# additionalContext/permissionDecisionReason/deny envelope. NOT FIRED here: unlike
# the read-only banner modules below, `main()`'s product is a real on-disk
# settings-local.json write with no side-effect-free unit underneath it small
# enough to fire safely inside this corpus; classification 3 rests on the static
# evidence above (no register_op decorator, no envelope import), not a captured
# row.

# --- (11) postuse_advisory_dispatch -- real firing row: with no session_id, the
# handler runs only the unauthorized-handoff fold-in leg (its own docstring:
# "the one check that does NOT depend on session_id... runs even when session_id
# is absent"), so the same Write-into-state/handoffs/ payload fires it via
# post_advisory() -- same underlying text as nudge_unauthorized_handoff's own row
# above, reached through the aggregator's own merge path instead.
def _fire_postuse_advisory_dispatch() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Write",
        "file_path": "state/handoffs/2026-08-12-example.md",
        "content": "no frontmatter here\n",
        "transcript_path": "",
    }
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_postuse_advisory_dispatch._handler(payload, repo_root=None))
    )


def _fire_postuse_advisory_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Read", "file_path": "", "content": "", "transcript_path": ""}
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_postuse_advisory_dispatch._handler(payload, repo_root=None))
    )


# --- (12) example_retrieval_repo_detect -- real firing row: `detect_banner(cwd)` returns
# the UNINITIALIZED banner string for a scratch dir carrying a `.example-retrieval-repo/
# manifest.json` marker with no `graph.db` beside it.
def _fire_example_retrieval_repo_detect() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-prd-") as scratch:
        scratch_dir = Path(scratch)
        os.makedirs(scratch_dir / ".example-retrieval-repo")
        (scratch_dir / ".example-retrieval-repo" / "manifest.json").write_text("{}", encoding="utf-8")
        banner = _hook_example_retrieval_repo_detect.detect_banner(str(scratch_dir))
        if not banner:
            return None
        return {"hookSpecificOutput": {"additionalContext": banner}}


def _fire_example_retrieval_repo_detect_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-prd-ctrl-") as scratch:
        banner = _hook_example_retrieval_repo_detect.detect_banner(scratch)
        if not banner:
            return None
        return {"hookSpecificOutput": {"additionalContext": banner}}


# --- (13) session_heartbeat -- write-only Pre+PostToolUse bookkeeping op; every
# branch in `_handler` returns `no_advisory()` (grep-verified). Verified live
# with params={} (no session_id -> the earliest no_advisory() branch).
def _fire_session_heartbeat_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_session_heartbeat._handler({}, repo_root=None))
    )


# --- (14) subagent_arrival_check -- structured JSON-RPC poll result, NOT an
# advisory envelope: `_handler`'s own docstring, "Returns the pinned {"state",
# "agent_id", "subagent_transcript_path", "reason"} shape directly (structured
# JSON-RPC result, not an advisory envelope)". Verified live: the result carries
# no `hookSpecificOutput` key at all.
def _fire_subagent_arrival_check_structured() -> Optional[Dict[str, Any]]:
    result = _hooks_asyncio.run(_hook_subagent_arrival_check._handler({}))
    assert "hookSpecificOutput" not in result, (
        "subagent_arrival_check began emitting an advisory envelope -- C12's "
        "no-agent-facing-emitter classification is stale"
    )
    return _to_envelope_or_none(result)


# --- (15) subagent_fabrication_check -- structured JSON-RPC verdict result, not
# an advisory envelope (own `_envelope()` helper returns a plain {"verdict", ...}
# dict, no `hookSpecificOutput`). Verified live with params={}.
def _fire_subagent_fabrication_check_structured() -> Optional[Dict[str, Any]]:
    result = _hooks_asyncio.run(_hook_subagent_fabrication_check._handler({}, repo_root=None))
    assert "hookSpecificOutput" not in result, (
        "subagent_fabrication_check began emitting an advisory envelope -- C12's "
        "no-agent-facing-emitter classification is stale"
    )
    return _to_envelope_or_none(result)


# --- (16) subagent_zero_tool_use -- write-only detector op; every branch in
# `_handler` returns `no_advisory()` (grep-verified). Verified live with
# params={} (missing repo_root/session_id/transcript_path -> the early
# no_advisory() branch).
def _fire_subagent_zero_tool_use_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_subagent_zero_tool_use._handler({}, repo_root=None))
    )


# --- (17) subagent_zero_tool_use_resolve -- structured JSON-RPC verdict result
# (own `_verdict()` helper), not an advisory envelope. Verified live with
# params={}.
def _fire_subagent_zero_tool_use_resolve_structured() -> Optional[Dict[str, Any]]:
    result = _hooks_asyncio.run(_hook_subagent_zero_tool_use_resolve._handler({}, repo_root=None))
    assert "hookSpecificOutput" not in result, (
        "subagent_zero_tool_use_resolve began emitting an advisory envelope -- "
        "C12's no-agent-facing-emitter classification is stale"
    )
    return _to_envelope_or_none(result)


# --- (18) subagent_zero_tool_use_surface -- structured JSON-RPC read result;
# own module docstring: "this op returns a plain dict" (not an advisory
# envelope). Verified live with params={}.
def _fire_subagent_zero_tool_use_surface_structured() -> Optional[Dict[str, Any]]:
    result = _hooks_asyncio.run(_hook_subagent_zero_tool_use_surface._handler({}, repo_root=None))
    assert "hookSpecificOutput" not in result, (
        "subagent_zero_tool_use_surface began emitting an advisory envelope -- "
        "C12's no-agent-facing-emitter classification is stale"
    )
    return _to_envelope_or_none(result)


# --- (19) suggest_sonnet_research -- real firing row: an unresolvable agent_id
# (not a named-teammate id, not bare hex) is "not suppressed", firing the
# DELEGATION REQUIRED advisory. `_has_deep_research_plugin` is monkeypatched to
# False exactly as hooks/test_suggest_sonnet_research.py's own `_run` does (a
# present deep-research plugin on the executing machine would otherwise
# suppress this row nondeterministically).
def _fire_suggest_sonnet_research() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_hook_suggest_sonnet_research, "_has_deep_research_plugin", lambda: False)
        payload = {"agent_id": "not-an-agent-id", "session_id": "abcdefgh"}
        return _to_envelope_or_none(_hooks_asyncio.run(_hook_suggest_sonnet_research._handler(payload)))


def _fire_suggest_sonnet_research_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_hook_suggest_sonnet_research, "_has_deep_research_plugin", lambda: False)
        payload = {"agent_id": "arscout-deadbeef123456ab", "session_id": "abcdefgh-full-session"}
        return _to_envelope_or_none(_hooks_asyncio.run(_hook_suggest_sonnet_research._handler(payload)))


# --- (20) track_dispatched_agents -- write-only dispatch-tracking op;
# `_handler`'s docstring: "Returns no_advisory() -- product is the on-disk write
# side-effect." Verified live with params={} (missing required fields ->
# an early no_advisory() branch).
def _fire_track_dispatched_agents_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_track_dispatched_agents._handler({}, repo_root=None))
    )


# --- (21) track_touched_files -- write-only touched-files-tracking op; module
# docstring: "Returns no_advisory() (empty dict) on every invocation path."
# Verified live with params={}.
def _fire_track_touched_files_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_track_touched_files._handler({}, repo_root=None))
    )


# --- (23) receiver_state_sensor -- write-only Stop/SubagentStop bookkeeping
# op; module docstring: "Always returns no_advisory() unconditionally -- the
# product is the on-disk write side-effect." No branch anywhere in the
# handler ever composes agent-facing text, so this is control-only (same
# shape as track_dispatched_agents/track_touched_files above), never a real
# fire row -- there is no rendered text for a fire cell to exist for.
# Verified live with params={} (missing session_id -> the early no_advisory()
# branch, before any I/O).
def _fire_receiver_state_sensor_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hooks_asyncio.run(_hook_receiver_state_sensor._handler({}, repo_root=None))
    )


# --- (24) subagent_sidecar_fill_check -- real firing row: a sidecar left
# `status: open` with no agent-authored body (only a heading + HTML comment,
# both stripped by detect_unfilled_sidecar's own content check) under
# state/subagent-share/<session_id>/ flags, and the handler returns
# post_advisory() naming the runnable check.
def _fire_subagent_sidecar_fill_check() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-sfc-") as scratch:
        scratch_dir = Path(scratch)
        (scratch_dir / ".git").mkdir()
        session_id = "sidecar-fill-check-corpus-row"
        share_dir = scratch_dir / "state" / "subagent-share" / session_id
        share_dir.mkdir(parents=True)
        (share_dir / "coordinatorexecutor-deadbeef.md").write_text(
            "---\nstatus: open\n---\n\n## Run Report\n<!-- fill this in -->\n",
            encoding="utf-8",
        )
        payload = {"session_id": session_id}
        return _to_envelope_or_none(
            _hook_subagent_sidecar_fill_check._handler(payload, repo_root=str(scratch_dir))
        )


def _fire_subagent_sidecar_fill_check_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-sfc-ctrl-") as scratch:
        scratch_dir = Path(scratch)
        (scratch_dir / ".git").mkdir()
        session_id = "sidecar-fill-check-corpus-row-control"
        payload = {"session_id": session_id}
        return _to_envelope_or_none(
            _hook_subagent_sidecar_fill_check._handler(payload, repo_root=str(scratch_dir))
        )


# --- (22) ue_knowledge_distrust -- real firing row: `run(cwd, plugin_root)`
# returns a non-empty UE-mistrust banner when a `.uproject` file is found under
# cwd. Fired against a scratch dir with a synthetic `.uproject`; `plugin_root`
# points at the same scratch dir so `_run_bootstrap`'s settings-merge write
# lands inside the scratch tree, never a real `.claude/` install.
def _fire_ue_knowledge_distrust() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-ukd-") as scratch:
        scratch_dir = Path(scratch)
        (scratch_dir / "Example.uproject").write_text("{}", encoding="utf-8")
        result = _hook_ue_knowledge_distrust.run(str(scratch_dir), str(scratch_dir))
        if not result.banner:
            return None
        return {"hookSpecificOutput": {"additionalContext": result.banner}}


def _fire_ue_knowledge_distrust_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-ukd-ctrl-") as scratch:
        result = _hook_ue_knowledge_distrust.run(scratch, scratch)
        if not result.banner:
            return None
        return {"hookSpecificOutput": {"additionalContext": result.banner}}


# --- (23) coordinator_reminder -- real firing row: `render_reminder()`
# unconditionally returns the static Quick-Orient heredoc text (module
# docstring: "If the file is missing... only the static heredoc section is
# returned" -- there is no input that makes it return ""). No non-firing
# control row exists for the same reason `nudge_unrouted_sizing` has none (see
# `test_every_hooks_row_guard_has_a_non_firing_control_row` below): a synthetic
# no-catalog call either produces the heredoc text or it does not exercise this
# guard's real render path at all.
def _fire_coordinator_reminder() -> Optional[Dict[str, Any]]:
    text = _hook_coordinator_reminder.render_reminder(None)
    if not text:
        return None
    return {"hookSpecificOutput": {"additionalContext": text}}


HOOK_ROWS: List[HookRow] = [
    HookRow("em_report_altitude", "fire-d2", True, _fire_em_report_altitude_d2),
    HookRow("em_report_altitude", "control", False, _fire_em_report_altitude_control),
    HookRow(
        "nudge_harness_directive_dispatch",
        "fire",
        True,
        _fire_nudge_harness_directive_dispatch,
    ),
    HookRow(
        "nudge_harness_directive_dispatch",
        "control",
        False,
        _fire_nudge_harness_directive_dispatch_control,
    ),
    HookRow("nudge_unrouted_sizing", "fire-plan-message", True, _fire_nudge_unrouted_sizing),
    # --- C12 additions: the 23 previously-uncovered hooks/ modules. ---
    HookRow("agent_completion_log", "noop-control", False, _fire_agent_completion_log_noop),
    HookRow(
        "block_unenumerated_agent_type",
        "fire-unenumerated-deny",
        True,
        _fire_block_unenumerated_agent_type,
    ),
    HookRow(
        "block_unenumerated_agent_type",
        "control-no-subagent-type",
        False,
        _fire_block_unenumerated_agent_type_control,
    ),
    HookRow(
        "context_pressure_precompact", "noop-control", False, _fire_context_pressure_precompact_noop
    ),
    HookRow("enforce_agent_model_pin", "fire-model-violation", True, _fire_enforce_agent_model_pin),
    HookRow(
        "enforce_agent_model_pin",
        "control-no-pins",
        False,
        _fire_enforce_agent_model_pin_control,
    ),
    HookRow("nudge_em_code_dispatch", "fire-code-write", True, _fire_nudge_em_code_dispatch),
    HookRow(
        "nudge_em_code_dispatch", "control-doc-write", False, _fire_nudge_em_code_dispatch_control
    ),
    HookRow(
        "nudge_foreground_agent_dispatch",
        "fire-reroute",
        True,
        _fire_nudge_foreground_agent_dispatch,
    ),
    HookRow(
        "nudge_foreground_agent_dispatch",
        "control-already-background",
        False,
        _fire_nudge_foreground_agent_dispatch_control,
    ),
    HookRow(
        "nudge_foreground_agent_dispatch",
        "fire-deny-no-prompt",
        True,
        _fire_nudge_foreground_agent_dispatch_deny,
    ),
    HookRow(
        "nudge_named_agent_report_delivery",
        "fire-named-no-delivery",
        True,
        _fire_nudge_named_agent_report_delivery,
    ),
    HookRow(
        "nudge_named_agent_report_delivery",
        "control-unnamed",
        False,
        _fire_nudge_named_agent_report_delivery_control,
    ),
    HookRow("nudge_unauthorized_handoff", "fire-handoff-write", True, _fire_nudge_unauthorized_handoff),
    HookRow(
        "nudge_unauthorized_handoff",
        "control-kind-recovery",
        False,
        _fire_nudge_unauthorized_handoff_control,
    ),
    HookRow(
        "postuse_advisory_dispatch", "fire-uh-fold", True, _fire_postuse_advisory_dispatch
    ),
    HookRow(
        "postuse_advisory_dispatch",
        "control-no-session-no-write",
        False,
        _fire_postuse_advisory_dispatch_control,
    ),
    HookRow("example_retrieval_repo_detect", "fire-uninitialized", True, _fire_example_retrieval_repo_detect),
    HookRow("example_retrieval_repo_detect", "control-no-marker", False, _fire_example_retrieval_repo_detect_control),
    HookRow("session_heartbeat", "noop-control", False, _fire_session_heartbeat_noop),
    HookRow(
        "subagent_arrival_check",
        "structured-result-control",
        False,
        _fire_subagent_arrival_check_structured,
    ),
    HookRow(
        "subagent_fabrication_check",
        "structured-result-control",
        False,
        _fire_subagent_fabrication_check_structured,
    ),
    HookRow("subagent_zero_tool_use", "noop-control", False, _fire_subagent_zero_tool_use_noop),
    HookRow(
        "subagent_zero_tool_use_resolve",
        "structured-result-control",
        False,
        _fire_subagent_zero_tool_use_resolve_structured,
    ),
    HookRow(
        "subagent_zero_tool_use_surface",
        "structured-result-control",
        False,
        _fire_subagent_zero_tool_use_surface_structured,
    ),
    HookRow("suggest_sonnet_research", "fire-garbage-agent-id", True, _fire_suggest_sonnet_research),
    HookRow(
        "suggest_sonnet_research",
        "control-named-teammate",
        False,
        _fire_suggest_sonnet_research_control,
    ),
    HookRow("track_dispatched_agents", "noop-control", False, _fire_track_dispatched_agents_noop),
    HookRow("track_touched_files", "noop-control", False, _fire_track_touched_files_noop),
    HookRow("receiver_state_sensor", "noop-control", False, _fire_receiver_state_sensor_noop),
    HookRow(
        "subagent_sidecar_fill_check",
        "fire-open-unfilled-sidecar",
        True,
        _fire_subagent_sidecar_fill_check,
    ),
    HookRow(
        "subagent_sidecar_fill_check",
        "control-no-flagged-sidecar",
        False,
        _fire_subagent_sidecar_fill_check_control,
    ),
    HookRow("ue_knowledge_distrust", "fire-uproject-detected", True, _fire_ue_knowledge_distrust),
    HookRow("ue_knowledge_distrust", "control-no-uproject", False, _fire_ue_knowledge_distrust_control),
    HookRow("coordinator_reminder", "fire-quick-orient", True, _fire_coordinator_reminder),
]


def fire_hook_row(row: HookRow) -> HookCapture:
    envelope = row.fire()
    return HookCapture(name=row.guard, band=_HOOKS_BAND, envelope=envelope)


# ---------------------------------------------------------------------------
# C10 -- DR-118 shim-relayed prose: mapping and coverage finding.
#
# Spec backlink: pln-runtime-measured-message-size--0669ac,
# chunk C10. § Problem claims the cap governs "the 73 modules above PLUS the
# message content behind those 19 [DoE-side, coordinator/hooks/scripts/]
# DR-118 pointer shims" because that prose is composed in coordinator_core
# and relayed verbatim by a shim DoE cannot edit (no policy, no composition
# -- DR-116's "resolve the engine root, hand over the raw payload... and
# degrade unconditionally"). This is neither a clean verification pass NOR a
# simple new-rows close -- it is PARTIAL, and both halves are recorded here
# so the split does not get flattened into a wrong number in C9's memo.
#
# Every coordinator_core entrypoint an engine-importing DoE shim can reach is
# one of exactly two shapes (grep-verified against coordinator_core/hooks/):
#
#   (1) Stop-hook direct-call shape -- a plain `def op(payload) -> dict |
#       None` with NO `@register_op` handler, called by a DoE-resident
#       stdin/stderr shim importing the module directly (confirmed by
#       em_report_altitude.py's own docstring: "Stop events are not routed
#       through the IPC daemon path... Transport here is the DoE-resident
#       stdin/stderr shim calling `op(payload)` directly"). Exactly four
#       modules carry this shape: `em_report_altitude`,
#       `nudge_harness_directive_dispatch`, `nudge_unrouted_sizing`,
#       `nudge_em_code_dispatch` (this one carries BOTH shapes -- see C3c's
#       own comment above). ALL FOUR ALREADY HAVE ROWS: the first three in
#       HOOK_ROWS above, the fourth via its WRITE_GUARD_ROWS entry (same
#       underlying `op()`, per C3c). This slice is a VERIFICATION PASS --
#       no new rows needed, cap-closure for the two named in C8's worklist
#       (`nudge_harness_directive_dispatch`, `nudge_unrouted_sizing`) is
#       already that chunk's job, not a new C10 obligation.
#
#   (2) IPC/`dispatch_from_hook` shape -- an `@register_op`-decorated async
#       handler, reached via the `coordinator_core.ipc.dispatch_from_hook`
#       seam DR-116/DR-118 built for exactly this purpose (a JSON-RPC
#       envelope round-trip, the shim relaying `response["result"]"). 17
#       further hooks/ modules carry ONLY this shape (grep for
#       `register_op(` under coordinator_core/hooks/, minus the four above):
#       `agent_completion_log`, `context_pressure_precompact`,
#       `coordinator_reminder`, `nudge_foreground_agent_dispatch`,
#       `nudge_named_agent_report_delivery`, `nudge_unauthorized_handoff`,
#       `postuse_advisory_dispatch`, `example_retrieval_repo_detect`,
#       `session_heartbeat`, `subagent_arrival_check`,
#       `subagent_zero_tool_use`, `subagent_zero_tool_use_resolve`,
#       `subagent_zero_tool_use_surface`, `suggest_sonnet_research`,
#       `track_dispatched_agents`, `track_touched_files`,
#       `ue_knowledge_distrust`. THESE HAVE NO CORPUS ROWS ANYWHERE in this
#       file, and C3c's own coverage note (above) explains why: an
#       `async def _handler(params, ...)` MCP-tool-shaped op "cannot invoke
#       ... without a second, differently-shaped capture seam this chunk
#       does not build" -- HookRow's `fire()` contract (a zero-arg sync
#       callable) does not fit an async MCP handler without new harness
#       infrastructure, the same gap C3c named and deferred to C11.
#
#       C11, however, is scoped purely as a doc-edit (re-deriving the
#       hooks/ prose-vs-stderr census predicate) -- it does not add corpus
#       coverage. No chunk in this plan currently builds an async capture
#       seam. NEEDS_COORDINATOR (for the EM, ahead of C9's memo): this
#       17-module population is real, uncovered DR-118-shim-relayed prose
#       surface -- closing it is new capture-harness work outside this
#       chunk's declared `change_kind: test-edit` / "C3's schema" framing,
#       not a same-shaped corpus-row addition. C9's report to DoE should
#       state coverage as "4 of the shim-reachable modules measured
#       end-to-end; 17 async-handler modules identified but not yet
#       captured," not claim the full 19-shim population is measured.
#
# Reconciling counts: 4 + 17 = 21 coordinator_core modules reachable by an
# engine-importing DoE shim, against DoE's own runtime-classified count of
# 19 pointer shims. The two counts are close but not proven identical --
# DoE's shim inventory lives in their tree (out of reach this session, per
# the plan's own review sidecar: "could not verify DoE-side claims (the 19
# shims...) -- cross-repo, out of tree"). This module's 21-module inventory
# is therefore the claude-klabauter-side upper bound on the shim-relayed surface, not
# a claim of an exact 19-to-21 name-for-name mapping.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# C8 -- install/ and contract/ single-call-render surfaces.
#
# Spec backlink: docs/plans/2026-08-12-message-text-stops-naming-an-
# unreachable-repo.md, chunk C8. Neither package carries a GuardEntry/
# write_guard/hook registration seam at all -- `install`/`contract` are
# plain scripts, not guards -- so these rows are fired with NO dispatch
# machinery, just a direct call to the one function that renders the whole
# surface in a single call. `StaticTextRow.text_fn` is invoked at FIRE time
# only (never at import time), matching this module's own no-import-side-
# effects discipline.
#
# Measured correction of the plan's premise (three executors this session,
# re-verified here): `install/`/`contract/` do NOT both render their whole
# message surface via `parser.format_help()` --
#   - `coordinator_core.install.sandbox_check` has no `parser.format_help()`
#     (`add_help=False`; `-h`/`--help` is a custom `store_true` `main()`
#     consumes itself) -- its full help surface renders through exactly one
#     call, `_usage_text()`, which `main()` prints verbatim. Covers the HELP
#     surface only -- this module's Reporter check labels (`.ok`/`.bad`/
#     `.skip`/`.info`) render per-check at runtime, outside `_usage_text()`,
#     and stay unratcheted (see the residue note in
#     `guard_message_register_lint.py`'s own module docstring).
#   - `coordinator_core.install.prereq_probe` has no argparse and no
#     `--help` at all (`argv` accepted, unused) -- there is no help surface
#     to capture, so it gets NO row here, deliberately.
#   - `coordinator_core.contract.emit_memo_schema` DOES render its whole
#     surface in one call: `emit_schemas()`, one `json.dumps` per entity
#     (`cross-repo-memo`, `archived-memo`), both entities per call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaticTextRow:
    """One package-level single-call render, fired with no guard/dispatch
    machinery at all. `package`/`guard`/`row_id` follow the same triad the
    other row types use for `RegisterViolation`/`RenderedCell` attribution;
    `text_fn` is a zero-arg callable returning the rendered text."""

    package: str
    guard: str
    row_id: str
    text_fn: Callable[[], str]


def _fire_sandbox_check_usage_text() -> str:
    from coordinator_core.install import sandbox_check

    return sandbox_check._usage_text()


def _fire_emit_memo_schema_output() -> str:
    """`emit_schemas()` side-effects real files (module docstring: writes
    `<name>.schema.json` per entity) -- fired against a fresh scratch
    tempdir, never the module's own directory, so this corpus row never
    touches `coordinator_core/contract/`'s real generated schema files.
    Rendered text mirrors what the module itself renders: one
    `json.dumps(schema, indent=2, ensure_ascii=False)` per entity (the
    same call `emit_schemas()`'s own body makes before writing), both
    entities joined -- "both entities per call" from this section's own
    header comment."""
    import json

    from coordinator_core.contract import emit_memo_schema

    with tempfile.TemporaryDirectory(
        prefix="guard-message-corpus-emit-memo-schema-"
    ) as scratch:
        schemas = emit_memo_schema.emit_schemas(out_dir=scratch)
    return "\n".join(
        json.dumps(schema, indent=2, ensure_ascii=False) for schema in schemas.values()
    )


STATIC_TEXT_ROWS: List[StaticTextRow] = [
    StaticTextRow(
        "install", "sandbox_check", "sandbox-check-usage-text", _fire_sandbox_check_usage_text
    ),
    StaticTextRow(
        "contract",
        "emit_memo_schema",
        "emit-memo-schema-output",
        _fire_emit_memo_schema_output,
    ),
]


def fire_static_text_row(row: StaticTextRow) -> str:
    return row.text_fn()


# ---------------------------------------------------------------------------
# Self-test surface (this chunk's own AC2/AC15 proof over its 16 rows).
# Deliberately kept in this same file, following `guard_message_capture.py`
# (C1)'s precedent -- not auto-discovered by the suite's `python_files =
# ["test_*.py"]` glob; run directly via
# `pytest coordinator_core/bash_guards/tests/guard_message_corpus.py`.
# Downstream chunks (C5's gate) import `CONFINEMENT_ROWS`/`fire_row` into
# their own `test_*.py` modules, which is how this corpus re-enters the
# auto-discovered suite for good. C3's own advisory/platform-band rows
# (a later serial pass on this same file) append to `CONFINEMENT_ROWS`'s
# sibling lists using this same `CorpusRow`/`fire_row` machinery.
# ---------------------------------------------------------------------------


def test_corpus_imports_cleanly_and_every_row_guard_resolves():
    """AC2's registration half: every row's `guard` name is a real
    `dispatch.py` registration (not a typo), resolved via the same
    `_build_guard_chain` structural introspection `test_guard_band_
    membership.py` already uses -- this test never calls `.fn()`."""
    chain = dispatch._build_guard_chain(
        cmd="git status",
        session_id="guard-message-corpus-registration-check",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "git status"}},
        policy_file=None,
        host_is_windows=False,
    )
    registered = {entry.name: entry.band for entry in chain}
    for row in CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS:
        assert row.guard in registered, "corpus row names an unregistered guard: %s" % row.guard
        assert registered[row.guard] == row.band, (
            "corpus row's declared band %r does not match dispatch.py's live "
            "registration %r for guard %s" % (row.band, registered[row.guard], row.guard)
        )


def test_every_confinement_guard_has_a_non_firing_control_row():
    """Anti-scope's "non-triggering cells are REQUIRED" check, enforced
    structurally: every guard in `CONFINEMENT_GUARDS` has at least one
    `expected_speaker=False` row in this corpus, not just firing rows.
    Superset, not equality (C3c, 2026-08-03): `CONFINEMENT_ROWS` also carries
    five drift-fix control-only rows for live registrations `CONFINEMENT_
    GUARDS` does not yet know about -- see that block's own comment."""
    non_firing_guards = {row.guard for row in CONFINEMENT_ROWS if not row.expected_speaker}
    assert non_firing_guards >= {
        name for name, _ in CONFINEMENT_GUARDS
    } - _FLIPPED_TO_ADVISORY_REWRITE


def test_every_advisory_and_platform_guard_has_a_non_firing_control_row():
    """C3b's own version of the check above -- every ADVISORY_REWRITE/
    PLATFORM_CONDITIONED_DENY guard in the live chain has at least one
    `expected_speaker=False` row."""
    non_firing = {row.guard for row in ADVISORY_REWRITE_ROWS if not row.expected_speaker}
    assert non_firing == {row.guard for row in ADVISORY_REWRITE_ROWS}
    non_firing_platform = {
        row.guard for row in PLATFORM_CONDITIONED_ROWS if not row.expected_speaker
    }
    assert non_firing_platform == {row.guard for row in PLATFORM_CONDITIONED_ROWS}


def test_expected_speaker_matches_measured_reality():
    """This chunk's own scoped-test obligation: fire every one of the 16
    confinement rows through C1's seam and assert `expected_speaker`
    predicts whether the guard actually denied. These 16 guards are all
    single-band CONFINEMENT_DENY hard-denies with no rewrite/suppression
    leg, so "speaks" reduces to "the envelope is not None" here -- a
    stricter prose-byte speaker predicate is C2's module, not re-derived in
    this chunk."""
    for row in CONFINEMENT_ROWS:
        capture = fire_row(row)
        assert capture.name == row.guard
        assert capture.band == row.band
        spoke = capture.envelope is not None
        assert spoke == row.expected_speaker, (
            "row %s: expected_speaker=%s but guard %s %s on input %r"
            % (
                row.row_id,
                row.expected_speaker,
                row.guard,
                "denied" if spoke else "stayed silent",
                row.input,
            )
        )


def test_advisory_and_platform_expected_speaker_matches_measured_reality():
    """C3b's own AC2 proof: fire every one of the 18 ADVISORY_REWRITE/
    PLATFORM_CONDITIONED_DENY rows through C1's seam and assert
    `expected_speaker` predicts whether `GuardEntry.fn()` returned a
    non-`None` envelope -- "speaks" here is the general C1 reading (allow+
    advisory/rewrite counts, not just deny), per this section's own
    docstring."""
    for row in ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS:
        capture = fire_row(row)
        assert capture.name == row.guard
        assert capture.band == row.band
        spoke = capture.envelope is not None
        assert spoke == row.expected_speaker, (
            "row %s: expected_speaker=%s but guard %s %s on input %r"
            % (
                row.row_id,
                row.expected_speaker,
                row.guard,
                "spoke" if spoke else "stayed silent",
                row.input,
            )
        )


# ---------------------------------------------------------------------------
# C3c's own self-test surface (write_guards/ + hooks/ rows).
# ---------------------------------------------------------------------------


def test_write_guard_rows_fire_as_expected():
    """C3c's AC2 proof for write_guards/: fire every WRITE_GUARD_ROWS cell
    through `guard.check()` directly (never through `engine.evaluate`'s
    two-phase short-circuit) and assert `expected_speaker` predicts whether
    the guard actually spoke -- skipping rows an `unverified_reason` marks
    as registration-only (see `WriteGuardRow` docstring)."""
    for row in WRITE_GUARD_ROWS:
        if row.unverified_reason is not None:
            continue
        capture = fire_write_guard_row(row)
        assert capture.name == row.guard
        assert capture.band == _WRITE_GUARDS_BAND
        spoke = capture.envelope is not None
        assert spoke == row.expected_speaker, (
            "write_guards row %s/%s: expected_speaker=%s but guard %s"
            % (row.guard, row.row_id, row.expected_speaker, "spoke" if spoke else "stayed silent")
        )


def test_every_write_guard_has_a_non_firing_control_row():
    """Anti-scope's "non-triggering cells are REQUIRED" check, C3c's own
    version: every write_guards guard has at least one
    `expected_speaker=False` row."""
    non_firing = {row.guard for row in WRITE_GUARD_ROWS if not row.expected_speaker}
    assert non_firing == {row.guard for row in WRITE_GUARD_ROWS}


def test_hook_rows_fire_as_expected():
    """C3c's AC2 proof for the three hooks/ modules routed onto the
    `_hook_envelope` chokepoint this wave: fire every HOOK_ROWS cell and
    assert `expected_speaker` predicts whether the module actually spoke."""
    for row in HOOK_ROWS:
        capture = fire_hook_row(row)
        assert capture.name == row.guard
        assert capture.band == _HOOKS_BAND
        spoke = capture.envelope is not None
        assert spoke == row.expected_speaker, (
            "hooks row %s/%s: expected_speaker=%s but module %s"
            % (row.guard, row.row_id, row.expected_speaker, "spoke" if spoke else "stayed silent")
        )


def test_every_hooks_row_guard_has_a_non_firing_control_row():
    """`nudge_unrouted_sizing` is the documented exception: its row calls
    `_build_plan_message` directly (the lighter path the C3c dispatch stub
    sanctions for this module) rather than firing `op()` end-to-end, so
    there is no "non-firing" cell to construct the same way -- a synthetic
    route/status pair either produces plan-message text or it does not
    exercise this guard's real message-composition path at all.

    `coordinator_reminder` (C12) is the same documented exception for a
    different reason: `render_reminder()` unconditionally returns the static
    Quick-Orient heredoc -- no input makes it return "", so there is no
    non-firing cell to construct."""
    non_firing = {row.guard for row in HOOK_ROWS if not row.expected_speaker}
    expected = {row.guard for row in HOOK_ROWS} - {"nudge_unrouted_sizing", "coordinator_reminder"}
    assert non_firing == expected


def test_ac2_every_reachable_guard_has_a_corpus_row():
    """THE AC2 CLOSER (C3c) -- the reason this chunk is last in the chain.

    Every guard reachable from `dispatch._build_guard_chain` (bash_guards,
    all three bands: CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS +
    PLATFORM_CONDITIONED_ROWS) or from `write_guards.engine.
    discover_guard_names()` (WRITE_GUARD_ROWS) must have at least one row
    in this corpus. Without this test, AC2 ("every guard... appears in the
    corpus; a guard added without a corpus row fails the suite") is a claim
    a docstring makes, not a gate the suite enforces -- a guard registered
    after this test was written, with no corpus row, breaks it."""
    bash_chain = dispatch._build_guard_chain(
        cmd="git status",
        session_id="guard-message-corpus-ac2-closer",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "git status"}},
        policy_file=None,
        host_is_windows=False,
    )
    bash_names = {e.name for e in bash_chain}
    corpus_bash_names = {
        row.guard for row in CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS
    }
    missing_bash = bash_names - corpus_bash_names
    assert not missing_bash, (
        "bash_guards guard(s) reachable from _build_guard_chain with no corpus row: %s"
        % sorted(missing_bash)
    )

    write_names, import_failed = write_guards_engine.discover_guard_names()
    assert not import_failed, "write_guards import failure(s): %s" % import_failed
    corpus_write_names = {row.guard for row in WRITE_GUARD_ROWS}
    missing_write = set(write_names) - corpus_write_names
    assert not missing_write, (
        "write_guards guard(s) reachable from discover_guard_names() with no corpus row: %s"
        % sorted(missing_write)
    )


def test_static_text_rows_fire_and_produce_non_empty_text():
    """C8's own AC7/AC8 proof: both `STATIC_TEXT_ROWS` entries fire without
    raising and produce non-empty rendered text -- `guard_message_register_
    lint.run_sweep()` lints whatever this produces, so a silently-empty
    render here would mean the B7 gate runs over nothing for these two
    rows while reporting green (the same dark-gate shape C3b's own
    `test_gate_fails_loud_if_collection_drops_to_zero` guards against for
    the other three packages)."""
    assert {row.package for row in STATIC_TEXT_ROWS} == {"install", "contract"}
    for row in STATIC_TEXT_ROWS:
        text = fire_static_text_row(row)
        assert text, "static text row %s/%s fired but produced empty text" % (
            row.package,
            row.row_id,
        )
