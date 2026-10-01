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
``_alternative_liveness.fire_guard``'s ``_isolated_session_scope``
scratch-tempdir pattern -- applied here at PER-CELL granularity (stricter
than that precedent), so a later row in the same test session never
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
import json as _json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest.mock
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


_CMD_OVERRIDE_KEY = "__cmd__"
_CWD_OVERRIDE_KEY = "__cwd__"

#: Neutral, repo-local scratch parent every `tempfile.TemporaryDirectory(...)`
#: call below is rooted under, instead of the bare `tempfile.gettempdir()`
#: default. `gettempdir()` resolves under the real machine's user profile
#: (`C:\Users\<name>\AppData\Local\Temp` on Windows) -- on a real developer
#: box that path segment is itself a REDACTION-class token in
#: `setup/percolate-hooks/percolate-store.yaml` (a real person's name/
#: username), so any fixture that renders `str(scratch_dir)` into a
#: guard/hook's own rendered text (the containment-denial reason, an
#: advisory rewrite embedding the scratch cwd, ...) trips B7 purely because
#: of WHICH MACHINE ran the suite, never because of anything the guard
#: actually composed. `guard_concrete_path_citations`'s own fixture already
#: worked around this one call site with a hand-picked synthetic literal
#: (a drive-letter-rooted stand-in path, see `_wg_concrete_path_citations_fire`
#: below); this is the same fix generalized to every OTHER `tempfile.
#: TemporaryDirectory` call in this module, at one source, rather than
#: hand-patching each fixture's rendered text individually. `scratch/`
#: matches this repo's own `.gitignore` at any depth, so nothing lands
#: under version control; each `TemporaryDirectory` still self-cleans on
#: context exit, same as before.
#:
#: A SIBLING of the repo root, never a directory NESTED under it: several
#: fixtures below (`bump-outside-repo-write`, `bump-foreign-repo-write`,
#: the `block_consumed_handoff_edit`/`block_cutover_phase_hand_edit`/
#: `block_memo_status_hand_edit` "no git root resolved" legs, ...) assert
#: on the scratch dir having NO git-repo ancestor at all -- measured live:
#: an earlier revision of this constant nested the scratch parent inside
#: this repo's own checkout, which put a REAL `.git` back above every
#: scratch dir (this repo's own) and silenced every one of those rows.
#:
#: NOT the drive root, and never again a bare sibling of the repo root.
#: `parents[3].parent` resolved to the top of whatever drive the checkout
#: lives on, so running this suite MINTED a permanent
#: `<drive>:/coordinator-guard-corpus-scratch` directory there -- the
#: `TemporaryDirectory` children self-clean, the parent does not. PM
#: ruling 2026-08-28: nothing writes to a drive root; every repo has its
#: own scratch folder. The system temp root satisfies the two properties
#: this constant actually needs -- no `.git` ancestor, and already an
#: exempt member of `FIXTURE_SCRATCH_ROOTS` below, so a fixture path
#: rendered into a guard message is still recognized as an echo and not
#: reported as a redaction leak.
_NEUTRAL_SCRATCH_PARENT = Path(tempfile.gettempdir()) / "coordinator-guard-corpus-scratch"


#: The roots every fixture tempdir in this module is minted under -- the
#: SSOT both message lints read to tell a fixture ECHO from a leak.
#:
#: Co-located with `_NEUTRAL_SCRATCH_PARENT` on purpose. Both
#: `test_no_machine_absolute_path_in_guard_messages.py` (B7-shaped absolute
#: paths) and `guard_message_register_lint.py` (B7 proper, redaction tokens)
#: scan the text a guard renders, and a guard naming its real target back to
#: the agent is the guard doing its job -- so both have to be able to
#: recognize a path THIS module handed the guard as its input. Keying that on
#: a location stated anywhere else is what broke: the abs-path lint keyed on
#: `tempfile.gettempdir()` because that is where this corpus used to mint,
#: `d1cf0b986` moved the mint root out to a repo sibling (to stop
#: `guard_inprocess_search`'s footer latch minting phantom session dirs into
#: the live `.git/coordinator-sessions/` hub), and the exemption stopped
#: recognizing its own fixtures -- 15 spans across 13 guards reported as
#: leaks, from this one line. Defined HERE, next to the mint root, a
#: relocation cannot desync the two again.
#:
#: BY IDENTITY, never "looks like a scratch dir": a substring test would
#: also clear a root a guard HARDCODED into its own prose, which is the leak
#: class both lints exist for.
FIXTURE_SCRATCH_ROOTS: tuple = (
    tempfile.gettempdir(),
    str(_NEUTRAL_SCRATCH_PARENT),
)


def is_fixture_scratch_path(candidate: str) -> bool:
    return any(candidate.startswith(root) for root in FIXTURE_SCRATCH_ROOTS)


#: What a fixture-minted scratch path is rewritten to before a message is
#: MEASURED (never before it is lint-scanned -- the leak lints must still see
#: the real rendered text). Length is the point: a plausible checkout root,
#: short and fixed, standing in for the nested pytest tempdir the fixture
#: actually handed the guard.
#: abs-path-ok: a synthetic stand-in, deliberately resembling no host. It is
#: never opened, resolved or compared against -- only its LENGTH is used, as
#: a representative checkout root. A `machine-local` lookup here would
#: reintroduce the host-dependence this constant exists to remove.
_MEASUREMENT_STANDIN_ROOT = "/Users/dev/repo"


def normalize_fixture_scratch_paths(text: str) -> str:
    """Rewrite every fixture-minted scratch path in `text` down to
    `_MEASUREMENT_STANDIN_ROOT` plus its final segment.

    WHY THE MEASUREMENT NEEDS THIS AND THE LINTS DO NOT. The prose cap is a
    budget on what a guard's AUTHOR wrote. A guard that names its target back
    to the agent renders whatever path it was handed, and this corpus hands
    it `<tempdir>/coordinator-guard-corpus-scratch/<per-row tempdir>/...` --
    on this host, 131 bytes before a single authored word, against a 220-byte
    cap. Three cells were carrying "cannot be trimmed" exemptions that were
    really "the fixture inflated them": measured 274/336/331 bytes here,
    172/136/131 against a real checkout root. An exemption recording a false
    reason is exactly the drift the exemption ratchet exists to catch.

    It also makes the measurement HOST-INDEPENDENT, which it was not: TMPDIR
    is ~50 bytes on this box and a handful on a Linux CI runner, so the same
    message measured different sizes depending on where the suite ran, and a
    cell could pass one box and fail another with no code change between.

    The leak lints (`test_no_machine_absolute_path_in_guard_messages.py`,
    `guard_message_register_lint.py`) deliberately do NOT use this -- they
    ask whether a guard LEAKED a real machine path, which is a question about
    the literal text, and `fixture_scratch_spans` already tells them which
    spans are this module's own echo rather than a leak.
    """
    for start, end in sorted(fixture_scratch_spans(text), reverse=True):
        original = text[start:end]
        tail = original.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        text = text[:start] + _MEASUREMENT_STANDIN_ROOT + "/" + tail + text[end:]
    return text


def normalize_envelope_for_measurement(envelope):
    if not isinstance(envelope, dict):
        return envelope
    hso = envelope.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return envelope
    new_hso = dict(hso)
    for field in ("additionalContext", "permissionDecisionReason"):
        value = new_hso.get(field)
        if isinstance(value, str) and value:
            new_hso[field] = normalize_fixture_scratch_paths(value)
    new_envelope = dict(envelope)
    new_envelope["hookSpecificOutput"] = new_hso
    return new_envelope


def fixture_scratch_spans(text: str) -> List[tuple]:
    spans: List[tuple] = []
    # Both separator forms, because a guard is free to normalise before it
    # renders: `guard_doctrine_surface_bash_write` does `.replace(chr(92), "/")`
    # on its resolved plugin root, so the backslash literal this tuple holds
    # never appears in that message and the operator's username segment fell
    # out of every span -- B7 then reported it as a leak the guard had written.
    # Matching only the native form made the exemption HOST-SHAPED: green
    # wherever the operator's username is not a redaction token, red where it is.
    roots: List[str] = []
    for _root in FIXTURE_SCRATCH_ROOTS:
        for _variant in (_root, _root.replace(chr(92), "/")):
            if _variant not in roots:
                roots.append(_variant)
    for root in roots:
        start = text.find(root)
        while start != -1:
            end = start + len(root)
            while end < len(text) and text[end] not in " \t\n\r\"'`()[]<>":
                end += 1
            spans.append((start, end))
            start = text.find(root, start + 1)
    return spans


def _neutral_scratch_parent() -> str:
    """The scratch parent every fixture tempdir is minted under.

    TWO constraints, and satisfying one while breaking the other is the whole
    history of this line.

    1. OUTSIDE any git repo. `guard_inprocess_search._footer()`'s latch resolves
       its path by walking UPWARD from `cwd` for a `.git` entry. A scratch inside
       the repo makes that walk reach the real repo root, and the latch lands in
       the live `.git/coordinator-sessions/` hub -- as a phantom session dir for
       the two sites that mint their own `CLAUDE_CODE_SESSION_ID`, and, worse,
       silently under the REAL session's id for the ten that leave it ambient,
       where it suppresses the operator's footer paragraph for the rest of their
       session with nothing to see. Measured 2026-08-26 (claude-klabauter-e6): eight
       phantom dirs, and `test_liveness.py::TestLiveSessionIdsCorpus` red for as
       long as they sit there. With the parent outside every repo the upward walk
       finds no root at all, so `_latch_path` returns None and the write never
       happens -- eliminated, not redirected.
    2. NAMED AS A FIXTURE ROOT BY THE ABS-PATH LINT. `test_no_machine_absolute_
       path_in_guard_messages.py` fires this corpus and reports any absolute
       path in the rendered text; its exemption 2 clears paths rooted under a
       root the corpus MINTS under, identified by identity via
       `_FIXTURE_SCRATCH_ROOTS`. Wherever this constant points, that tuple has
       to name it -- satisfying constraint 1 by relocating and forgetting this
       is exactly what `d1cf0b986` did, and it reddened the lint with 15 spans
       across 13 guards that were all this one line.

    Constraint 2 REPLACED an earlier "no user-home segment" constraint, whose
    B7 premise (a rendered fixture path leaks the operator's username into
    agent-facing text) does not hold: these paths are minted and read inside a
    test process, and the path a guard names in a real session is the
    operator's own, which the lint's own exemption 2 rules is the guard doing
    its job. It was also unsatisfiable -- see `test_scratch_parent_is_outside_
    the_repo_and_is_exempt_here`, which pins both live constraints.
    """
    _NEUTRAL_SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    return str(_NEUTRAL_SCRATCH_PARENT)

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

    guard: str
    row_id: str
    input: str
    expected_speaker: bool
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
    factory = _GUARD_SETUP[setup_name]

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        factory(scratch_dir, mp)
        extra: Dict[str, str] = {_CMD_OVERRIDE_KEY: control_cmd}
        if identity:
            extra.update(identity)
        return extra

    return setup


def _git_repo_setup(cmd_template: str) -> Callable[[Path, pytest.MonkeyPatch], Dict[str, str]]:

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        repo = _build_load_bearing_repo(scratch_dir)
        return {
            _CMD_OVERRIDE_KEY: cmd_template % shlex.quote(str(repo).replace("\\", "/")),
            _CWD_OVERRIDE_KEY: str(repo),
        }

    return setup


def _stale_write_setup(make_stale: bool) -> Callable[[Path, pytest.MonkeyPatch], Dict[str, str]]:
    from coordinator_core.session import touch_record as _tr

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        session_id = os.environ["CLAUDE_CODE_SESSION_ID"]
        repo = Path(os.path.realpath(_build_load_bearing_repo(scratch_dir)))
        rel = "state/tracked.md"
        target = repo / "state" / "tracked.md"
        _tr.append_touch_claims(
            [rel],
            session_id,
            str(repo),
            content_hashes={rel: _tr.compute_content_hash(target) or ""},
        )
        if make_stale:
            target.write_text("content this session never read\n", encoding="utf-8")
        return {
            _CMD_OVERRIDE_KEY: "echo replacement > %s" % rel,
            _CWD_OVERRIDE_KEY: str(repo),
        }

    return setup


def _build_advisory_only_repo(scratch_dir: Path) -> Path:
    repo = scratch_dir / "advisory-only-repo"
    repo.mkdir()
    _git = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags()
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

    def setup(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, str]:
        repo = _build_advisory_only_repo(scratch_dir)
        return {
            _CMD_OVERRIDE_KEY: cmd_template % repo,
            _CWD_OVERRIDE_KEY: str(repo),
        }

    return setup


def _bump_confinement_anchor_repo(scratch_dir: Path) -> Path:
    anchor = scratch_dir / "anchor"
    anchor.mkdir()
    _git = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=str(anchor), check=True, capture_output=True, **no_console_creationflags()
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
        ["git", *args], cwd=str(foreign), check=True, capture_output=True, **no_console_creationflags()
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
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-audience-", dir=_neutral_scratch_parent()) as scratch:
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
    session_id = "guard-message-corpus-%s" % uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        with pytest.MonkeyPatch.context() as mp:
            # guard_inprocess_search's
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

def _rehomed_doctrine_surface_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    # PR #103 made the doctrine-edit gate opt-in
    # (`coordinator.feature.doctrine_edit_gate`, off by default) -- this
    # guard is a no-op unless the gate is on, so the corpus row must turn
    # it on itself rather than rely on the ambient machine profile (see
    # `coordinator_core/tests/test_doctrine_edit_gate_opt_in.py`'s own
    # `_set_gate`).
    from coordinator_core import machine_profile as _machine_profile

    mp.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", "on")
    _machine_profile.reset_cache()
    plugin_root = Path(
        tempfile.mkdtemp(
            prefix="guard-message-corpus-doctrine-surface-", dir=str(Path.home())
        )
    )
    (plugin_root / "governed-authoring-surfaces.json").write_text(
        '["docs/wiki/governed-thing.md"]', encoding="utf-8"
    )
    return {"plugin_root": str(plugin_root)}


def _rehomed_repo_setup_claude_home_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """This guard denies only when the scaffold mechanism's resolved target
    root EQUALS Claude Home, so both sides must name the same scratch path:
    `CLAUDE_CONFIG_DIR` declares it and the overridden command targets it. A
    fixed literal would compare the real machine's Claude Home against an
    invented path and never fire."""
    home = scratch_dir / ".claude"
    home.mkdir(exist_ok=True)
    return {
        "env": {"CLAUDE_CONFIG_DIR": str(home)},
        _CMD_OVERRIDE_KEY: (
            "python3 -m coordinator_core.install.scaffold_structure --root %s" % home
        ),
    }


def _rehomed_subagent_bash_ban_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """Host-opt-in guard: inert until `coordinator.local.md` declares
    `subagent_bash_policy: deny` at or above cwd AND the payload carries an
    agent_id the identity resolver RECOGNISES. `deadbeef0123` is bare hex and
    resolves; a shape like `a1` does not and is EM-treated (allow) -- the
    named divergence from DoE's raw non-empty-string test."""
    (scratch_dir / "coordinator.local.md").write_text(
        "---" + chr(10) + "subagent_bash_policy: deny" + chr(10) + "---" + chr(10),
        encoding="utf-8",
    )
    return dict(_EXECUTOR_IDENTITY)


def _p4_verb_fence_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    """`p4-verb-fence` (C6, docs/plans/2026-09-12-perforce-second-class-
    commit-and-shelve.md) is MARKER-GATED (D1): `check()` returns allow
    immediately for any repo whose `coordinator.local.md` does not declare
    `vcs_mirror: p4`, before any command-text parsing runs. Declaring the
    marker in the scratch dir (which `fire_row` also uses as `cwd` by
    default) is the only setup this guard needs -- its own `_find_repo_
    root_no_spawn` walk starts at `cwd` itself, so no subdirectory nesting
    is required."""
    (scratch_dir / "coordinator.local.md").write_text(
        "---" + chr(10) + "vcs_mirror: p4" + chr(10) + "---" + chr(10),
        encoding="utf-8",
    )
    return {}


def _rehomed_subagent_spawn_shapes_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    (scratch_dir / "coordinator.local.md").write_text(
        "---" + chr(10) + "subagent_bash_spawn_shapes: deny" + chr(10) + "---" + chr(10),
        encoding="utf-8",
    )
    return dict(_EXECUTOR_IDENTITY)


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
        "stale-write",
        "stale-write-fire",
        "echo replacement > state/tracked.md",
        True,
        _DENY,
        False,
        setup=_stale_write_setup(make_stale=True),
    ),
    CorpusRow(
        "stale-write",
        "stale-write-control",
        "echo replacement > state/tracked.md",
        False,
        _DENY,
        False,
        setup=_stale_write_setup(make_stale=False),
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
        "destructive-git-revert",
        "destructive-git-revert-whole-tree-restore-fire",
        "git -C <repo> restore .",
        True,
        _DENY,
        False,
        setup=_git_repo_advisory_setup("git -C %s restore ."),
    ),
    CorpusRow(
        # ADVISORY_REWRITE (see that row in `ADVISORY_REWRITE_ROWS` below).
        # An advisory returned from a
        # CONFINEMENT_DENY-registered guard would short-circuit
        # `evaluate_payload_json` and shadow every hard-deny guard
        # registered after it, so the two legs are split.
        "destructive-git-revert",
        "destructive-git-revert-whole-tree-stash-fire",
        "git -C <repo> stash",
        True,
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
        "block-subagent-findings-reject",
        "block-subagent-findings-reject-fire",
        "python3 -m coordinator_core.ops.review_findings_ledger reject F1 reason",
        True,
        _DENY,
        False,
        setup=lambda scratch_dir, mp: dict(_EXECUTOR_IDENTITY),
    ),
    CorpusRow(
        "block-subagent-findings-reject",
        "block-subagent-findings-reject-control",
        "python3 -m coordinator_core.ops.review_findings_ledger verify",
        False,
        _DENY,
        False,
        setup=lambda scratch_dir, mp: dict(_EXECUTOR_IDENTITY),
    ),
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
    CorpusRow(
        "guard-doctrine-surface-bash-write",
        "guard-doctrine-surface-bash-write-fire",
        "echo corrupted > docs/wiki/governed-thing.md",
        True,
        _DENY,
        False,
        setup=_rehomed_doctrine_surface_setup,
    ),
    CorpusRow(
        "guard-doctrine-surface-bash-write",
        "guard-doctrine-surface-bash-write-control",
        "cat docs/wiki/governed-thing.md",
        False,
        _DENY,
        False,
        setup=_rehomed_doctrine_surface_setup,
    ),
    CorpusRow(
        "guard-repo-setup-claude-home-refusal",
        "guard-repo-setup-claude-home-refusal-fire",
        "python3 -m coordinator_core.install.scaffold_structure --root <claude-home>",
        True,
        _DENY,
        False,
        setup=_rehomed_repo_setup_claude_home_setup,
    ),
    CorpusRow(
        "guard-repo-setup-claude-home-refusal",
        "guard-repo-setup-claude-home-refusal-control",
        "python3 -m coordinator_core.install.scaffold_structure --root ./somewhere-else",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "guard-host-subagent-bash-ban",
        "guard-host-subagent-bash-ban-fire",
        "ls -la",
        True,
        _DENY,
        False,
        setup=_rehomed_subagent_bash_ban_setup,
    ),
    CorpusRow(
        "guard-host-subagent-bash-ban",
        "guard-host-subagent-bash-ban-control",
        "ls -la",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "guard-host-subagent-bash-spawn-shapes",
        "guard-host-subagent-bash-spawn-shapes-fire",
        'for f in *.md; do wc -l "$f"; done',
        True,
        _DENY,
        False,
        setup=_rehomed_subagent_spawn_shapes_setup,
    ),
    CorpusRow(
        "guard-host-subagent-bash-spawn-shapes",
        "guard-host-subagent-bash-spawn-shapes-control",
        "cat one.md",
        False,
        _DENY,
        False,
        setup=_rehomed_subagent_spawn_shapes_setup,
    ),
    CorpusRow(
        "block-fleet-delegation-creation",
        "block-fleet-delegation-creation-fire",
        "touch fleet-delegation.json",
        True,
        _DENY,
        False,
    ),
    CorpusRow(
        "block-fleet-delegation-creation",
        "block-fleet-delegation-creation-control",
        "touch normal_file.txt",
        False,
        _DENY,
        False,
    ),
    CorpusRow(
        "p4-verb-fence",
        "p4-verb-fence-fire",
        "p4.exe -p ssl:host:1666 -c client submit",
        True,
        _DENY,
        False,
        setup=_p4_verb_fence_setup,
    ),
    CorpusRow(
        "p4-verb-fence",
        "p4-verb-fence-control",
        "p4 info",
        False,
        _DENY,
        False,
        setup=_p4_verb_fence_setup,
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
# finds 14 ADVISORY_REWRITE registrations and 2 PLATFORM_CONDITIONED_DENY
# registrations, not 13+2=15. The extra one beyond the dispatch brief's
# 13 is `offer-invoke-params-stdin` -- a real, live `dispatch.py`
# registration in the ADVISORY_REWRITE band (grep-confirmed against
# `_build_guard_chain`'s own output, not a docstring count). `branch-set-
# precedence` and `longlived-branch-naming` were deleted (docs/plans/
# 2026-08-21-the-advisory-band-gets-smaller-cheaper-and-honest.md, C6) --
# no rows for either below. AC2's own text ("every guard reachable... gets
# a corpus row") governs over the illustrative arithmetic, so all 16 get
# rows below, two cells each (one firing, one non-firing), following
# `CONFINEMENT_ROWS`'s own shape.
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
    root = scratch_dir
    _git = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags()
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


def _heredoc_repo_write_advise_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    return {_CWD_OVERRIDE_KEY: "/repo"}


def _noncanonical_branch_creation_hazard_setup(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, str]:
    from coordinator_core.bash_guards import block_noncanonical_branch_creation as guard

    mp.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
    mp.setattr(guard, "_is_hazard_repo", lambda git_root: True)
    return {_CWD_OVERRIDE_KEY: "/repo"}


ADVISORY_REWRITE_ROWS: List[CorpusRow] = [
    CorpusRow(
        "destructive-git-revert-advisory",
        "destructive-git-revert-advisory-control",
        "git -C <repo> status",
        False,
        _REWRITE,
        False,
        setup=_git_repo_advisory_setup("git -C %s status"),
    ),
    # The stash pair above pinned this
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
        "git -C <repo> reset --hard HEAD",
        True,
        _REWRITE,
        False,
        setup=_git_repo_advisory_setup("git -C %s reset --hard HEAD"),
    ),
    CorpusRow(
        "destructive-git-revert-advisory",
        "destructive-git-revert-advisory-checkout-force-fire",
        "git -C <repo> checkout -f",
        True,
        _REWRITE,
        False,
        setup=_git_repo_advisory_setup("git -C %s checkout -f"),
    ),
    CorpusRow(
        "block-venv-creation",
        "block-venv-creation-fire",
        "python -m venv .venv",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "block-venv-creation",
        "block-venv-creation-control",
        "git status",
        False,
        _REWRITE,
        False,
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
        "headless-claude-plugin-dir",
        "headless-claude-plugin-dir-control",
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
    # coordinator-content-repo repo) after this block was last written, and the
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
    CorpusRow(
        "bump-outside-repo-write",
        "bump-outside-repo-write-fire",
        "cp <anchor>/README.md <outside>/newfile.txt",
        True,
        _REWRITE,
        False,
        setup=_bump_outside_repo_write_fire_setup,
    ),
    # C4 coverage-gap closer, 2026-08-30 (two-ratchet-gates-the-work-outran):
    # `stash-apply-verification-advisory` (dispatch.py:2537, landed 2026-08-30)
    # had no corpus row. Genuinely fireable -- `check_apply_advisory` is a
    # pure text classifier (`_STASH_WORD_RE` + subcommand match on `apply`,
    # see `block_stash_destruction.py`'s "APPLY ADVISORY LEG"), no fixture
    # repo needed since it never executes git, so a real fire+control pair.
    CorpusRow(
        "stash-apply-verification-advisory",
        "stash-apply-verification-advisory-fire",
        "git stash apply",
        True,
        _REWRITE,
        False,
    ),
    CorpusRow(
        "stash-apply-verification-advisory",
        "stash-apply-verification-advisory-control",
        "git status",
        False,
        _REWRITE,
        False,
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
    CorpusRow(
        "plumbing-and-loops",
        "plumbing-and-loops-label-echo-fire",
        'echo "--- status ---"; pwd; git status',
        True,
        _PLATFORM,
        False,
    ),
    CorpusRow(
        "plumbing-and-loops",
        "plumbing-and-loops-label-echo-control",
        "echo hi",
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
    with pytest.MonkeyPatch.context() as env_mp:
        for flag in write_guards_engine._ENV_GATED_GUARDS.values():
            env_mp.setenv(flag, "1")
        guards, import_failed = write_guards_engine._discover_guards()
    assert not import_failed, (
        "write_guards module(s) failed to import during corpus fire: %s" % import_failed
    )
    return {g.name: g for g in guards}


@dataclass(frozen=True)
class WriteGuardCapture:

    name: str
    band: str
    envelope: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class WriteGuardRow:

    guard: str
    row_id: str
    expected_speaker: bool
    payload_factory: Callable[[Path, pytest.MonkeyPatch], Dict[str, Any]]
    unverified_reason: Optional[str] = None


def fire_write_guard_row(row: WriteGuardRow) -> WriteGuardCapture:
    lookup = _wg_lookup()
    guard = lookup[row.guard]
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-wg-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        with pytest.MonkeyPatch.context() as mp:
            payload = row.payload_factory(scratch_dir, mp)
            envelope = guard.check(payload)
    return WriteGuardCapture(name=row.guard, band=_WRITE_GUARDS_BAND, envelope=envelope)


def _wg_benign(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
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


def _wg_confined_agent_write_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    from coordinator_core.write_guards import block_confined_agent_write as guard_mod

    mp.setattr(guard_mod, "_resolve_subagent_identity", lambda raw, session_id: raw)
    mp.setattr(guard_mod, "resolve_repo_root", lambda cwd: str(scratch_dir))
    mp.setattr(
        guard_mod,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, expected_em_session_id=None: "coordinator:code-reviewer",
    )
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(scratch_dir / "outside-sandbox.md"), "content": "x"},
        "cwd": str(scratch_dir),
        "agent_id": "deadbeef0123",
        "session_id": "sess-c3c-caw",
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


def _wg_strict_dispatch_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    subprocess.run(["git", "init", "-q", str(scratch_dir)], check=True, **no_console_passthrough_kwargs())
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(scratch_dir / "app.py"), "content": "x = 1\n"},
        "session_id": "sess-corpus-strict-dispatch",
        "cwd": str(scratch_dir),
    }


def _wg_sizing_schema_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    subprocess.run(["git", "init", "-q", str(scratch_dir)], check=True, **no_console_passthrough_kwargs())
    target = scratch_dir / "state" / "sizings" / "s.yaml"
    target.parent.mkdir(parents=True)
    # Every OTHER required field present (an otherwise-valid document) so
    # only `pm_resolution` trips -- a document missing every required field
    # (the earlier fixture) makes the guard's own `"; ".join(parts)` message
    # legitimately huge, which is B5/leg-1 cap fallout from the FIXTURE, not
    # a defect in the guard's composition.
    content = (
        "schema: sizing-object\n"
        "intent: test intent\n"
        "estimate:\n"
        "  tshirt: S\n"
        "  provisional: true\n"
        "route: dispatch\n"
        "detents: []\n"
        "fork: null\n"
        "xl_exit: null\n"
        "status: sized\n"
        "premise:\n"
        "  provenance: read\n"
        "  evidence: test evidence\n"
        "pm_resolution: not-a-mapping\n"
    )
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": content},
        "cwd": str(scratch_dir),
    }


def _wg_process_time_figure_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    subprocess.run(["git", "init", "-q", str(scratch_dir)], check=True, **no_console_passthrough_kwargs())
    target = scratch_dir / "docs" / "research" / "note.md"
    target.parent.mkdir(parents=True)
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "The op took 900 ms of process time.\n"},
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
    mp.setenv("MACHINE_LOCAL_REPOS_CONTENT_ROOT", str(scratch_dir / "authoring"))
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


def _wg_hand_authored_sidecar_creation_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """Fires `block_hand_authored_sidecar_creation.check`: a Write CREATING
    a new file under `state/subagent-share/<session-id>/<leaf>.md` whose
    content carries no non-empty `agent_type:` frontmatter -- the exact
    hand-authored-scaffold shape the 2026-08-16 incident produced."""
    sidecar_path = scratch_dir / "state" / "subagent-share" / "sess-1" / "hand-made.md"
    return {
        "tool_name": "Write",
        "cwd": str(scratch_dir),
        "tool_input": {
            "file_path": str(sidecar_path),
            "content": "---\nstatus: open\n---\n\nhand-authored body\n",
        },
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
    import tempfile as _tempfile

    from coordinator_core.session.guard_unlock_sentinel import _SENTINEL_PREFIX

    # Deliberately the REAL `tempfile.gettempdir()`, not `_neutral_scratch_
    # parent()` -- unlike every other fixture in this module, this guard's
    # OWN check logic (`guard_unlock_sentinel.sentinel_path`) independently
    # resolves `tempfile.gettempdir()` itself (module docstring: "Do NOT
    # hardcode `/tmp` or reach for a different temp resolution") to decide
    # whether a write targets the live unlock-sentinel location. Redirecting
    # this row's OWN write target elsewhere makes the two paths diverge and
    # the guard never recognizes its own row -- verified live (silent
    # instead of firing) after routing this one row through the neutral
    # parent. This row's denial text embeds a real machine tempdir path
    # (and therefore, on a real developer box, that machine's own
    # REDACTION-class username) for the same load-bearing reason
    # `_bt_python3_invocation`'s real interpreter path does -- reported, not
    # fixed here (see the cluster-D grind's own report).
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
    subprocess.run(["git", "init", "-q", str(scratch_dir)], check=True, **no_console_passthrough_kwargs())
    target = scratch_dir / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
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
    mp.setattr(guard_mod, "_is_windows", lambda: False)
    drive_path = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "x"
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


def _wg_handoff_author_lint_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A `Write` to a not-yet-existing `state/handoffs/*.md` path whose
    `summary:` is over the 140-char cap -- `SUMMARY_OVER_CAP`, one of the
    three codes `nudge_handoff_author_lint` relays."""
    summary = "x" * 150
    content = f'---\nkind: spinoff\nsummary: "{summary}"\n---\n# Body\n'
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "state/handoffs/bar.md", "content": content},
        "cwd": str(scratch_dir),
    }


def _wg_dangling_sizing_citation_fire(scratch_dir: Path, mp: pytest.MonkeyPatch) -> Dict[str, Any]:
    (scratch_dir / ".git").mkdir()
    content = (
        "---\n"
        "kind: plan\n"
        "sizing_object: state/sizings/2026-09-20-never-written.yaml\n"
        "---\n"
        "# A plan citing a sizing object that was never written\n"
    )
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "docs/plans/2026-09-20-corpus-row.md", "content": content},
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


def _wg_fleet_delegation_write_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """Fires `block_fleet_delegation_write.check`: a Write targeting the
    resolved `<settings_home()>/fleet-delegation.json`. `COORDINATOR_
    SETTINGS_HOME` (rung 0 of `_settings_home.settings_home`'s own
    precedence, pure env read, no external call) is redirected to this
    row's own scratch dir so the guard's target resolves under it rather
    than the real machine's settings-home."""
    mp.setenv("COORDINATOR_SETTINGS_HOME", str(scratch_dir))
    target = scratch_dir / "fleet-delegation.json"
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "{}"},
    }


def _wg_foreign_family_sidecar_write_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    from coordinator_core.write_guards import (
        block_foreign_family_sidecar_write as guard_mod,
    )

    mp.setattr(guard_mod, "resolve_repo_root", lambda cwd: str(scratch_dir))
    mp.setattr(guard_mod, "_resolve_subagent_identity", lambda raw, session_id: raw)
    mp.setattr(
        guard_mod,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, expected_em_session_id=None: "coordinator:review-integrator",
    )
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/subagent-share/sess-fff/coordinator-review-integrator.siblingagent0123.md",
            "content": "integrated_from: [x]\n",
        },
        "cwd": str(scratch_dir),
        "agent_id": "deadbeef0123",
        "session_id": "sess-fff",
    }


def _wg_session_display_name_as_identifier_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """Fires `nudge_session_display_name_as_identifier.check`: an
    attribution-verb construction ("ESTABLISHED AND FIXED BY
    claude-klabauter-49", the live incident's own shape) inside a body
    write to an in-scope record class (`state/lessons/`)."""
    return {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/lessons/2026-08-30-a-thing.md",
            "content": "ESTABLISHED AND FIXED BY claude-klabauter-49.\n",
        },
    }


def _wg_wiki_changelog_prose_advisory_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    from coordinator_core.write_guards import wiki_changelog_prose_advisory as guard_mod

    (scratch_dir / "docs" / "wiki").mkdir(parents=True, exist_ok=True)
    mp.setattr(guard_mod, "resolve_repo_root", lambda cwd: str(scratch_dir))
    wiki_path = str(scratch_dir / "docs" / "wiki" / "some-doctrine.md")
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": wiki_path, "content": "Ruled 2026-08-01 that X."},
        "cwd": str(scratch_dir),
    }


def _wg_p4_checkout_before_edit_fire(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """Fires `p4_checkout_before_edit.check`: a READ-ONLY target in a
    marker (`vcs_mirror: p4`) repo. Patched exactly as this guard's own
    `_ReadOnlyFixture.setup` (`test_p4_checkout_before_edit.py`) patches
    it -- `resolve_repo_root`/`workspace.is_p4_repo`/`_resolve_repo_key`/
    `workspace.identity`/`ensure_session_change` all stubbed so the only
    live call is `runner.run`, faked here to a `binary` headType (the
    simplest deny leg: one fstat spawn, no edit spawn, D5's own
    'binary is a type property, not a lock' reading). This exercises the
    guard's own applicability gates (marker + not-writable), not a real p4
    spawn -- same isolation `CorpusRow`/`WriteGuardRow` rows use elsewhere
    in this module."""
    import os
    import stat as _stat

    from coordinator_core.p4 import runner as _runner, workspace as _workspace
    from coordinator_core.write_guards import p4_checkout_before_edit as guard_mod

    target = scratch_dir / "locked.txt"
    target.write_text("hi", encoding="utf-8")
    os.chmod(target, _stat.S_IREAD)

    mp.setattr(guard_mod, "resolve_repo_root", lambda cwd: str(scratch_dir))
    mp.setattr(_workspace, "is_p4_repo", lambda root: True)
    mp.setattr(guard_mod, "_resolve_repo_key", lambda root: "p4-studio/probe")
    mp.setattr(
        _workspace,
        "identity",
        lambda repo_key: _workspace.P4Identity(
            port="p4.example.com:1666", user="bob", client="bob-ws", client_root="/root"
        ),
    )
    mp.setattr(guard_mod, "ensure_session_change", lambda root, sid: 101)
    mp.setattr(
        _runner,
        "run",
        lambda *a, **kw: _runner.P4Result(ok=True, stdout="... headType binary\n"),
    )

    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
        "cwd": str(scratch_dir),
        "session_id": "guard-message-corpus-p4-checkout",
    }


def _wg_p4_checkout_before_edit_control(
    scratch_dir: Path, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """Non-firing control: a WRITABLE target in the same marker repo.
    `_is_writable` short-circuits to allow before any p4 spawn, so
    `runner.run` is left unpatched here -- a real spawn would prove the
    zero-spawn-on-writable contract broken, not merely fail the test the
    hard way."""
    from coordinator_core.p4 import workspace as _workspace
    from coordinator_core.write_guards import p4_checkout_before_edit as guard_mod

    target = scratch_dir / "writable.txt"
    target.write_text("hi", encoding="utf-8")

    mp.setattr(guard_mod, "resolve_repo_root", lambda cwd: str(scratch_dir))
    mp.setattr(_workspace, "is_p4_repo", lambda root: True)

    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
        "cwd": str(scratch_dir),
        "session_id": "guard-message-corpus-p4-checkout-control",
    }


WRITE_GUARD_ROWS: List[WriteGuardRow] = [
    WriteGuardRow("block_completion_monolith_write", "fire", True, _wg_completion_monolith_fire),
    WriteGuardRow("block_completion_monolith_write", "control", False, _wg_benign),
    WriteGuardRow(
        "block_confined_agent_write", "fire", True, _wg_confined_agent_write_fire
    ),
    WriteGuardRow("block_confined_agent_write", "control", False, _wg_benign),
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
    WriteGuardRow("block_em_strict_dispatch_code_write", "fire", True, _wg_strict_dispatch_fire),
    WriteGuardRow("block_em_strict_dispatch_code_write", "control", False, _wg_benign),
    WriteGuardRow("block_sizing_object_schema_violation", "fire", True, _wg_sizing_schema_fire),
    WriteGuardRow("block_sizing_object_schema_violation", "control", False, _wg_benign),
    WriteGuardRow("nudge_unattributed_process_time_figure", "fire", True, _wg_process_time_figure_fire),
    WriteGuardRow("nudge_unattributed_process_time_figure", "control", False, _wg_benign),
    WriteGuardRow("block_dev_repo_sentinel_write", "fire", True, _wg_dev_repo_sentinel_fire),
    WriteGuardRow("block_dev_repo_sentinel_write", "control", False, _wg_benign),
    WriteGuardRow("block_dev_side_mirror_wiki", "fire", True, _wg_dev_side_mirror_wiki_fire),
    WriteGuardRow("block_dev_side_mirror_wiki", "control", False, _wg_benign),
    WriteGuardRow("block_disarm_marker_sentinel_write", "fire", True, _wg_disarm_marker_fire),
    WriteGuardRow("block_disarm_marker_sentinel_write", "control", False, _wg_benign),
    WriteGuardRow(
        "block_fleet_delegation_write", "fire", True, _wg_fleet_delegation_write_fire
    ),
    WriteGuardRow("block_fleet_delegation_write", "control", False, _wg_benign),
    WriteGuardRow(
        "block_foreign_family_sidecar_write",
        "fire",
        True,
        _wg_foreign_family_sidecar_write_fire,
    ),
    WriteGuardRow("block_foreign_family_sidecar_write", "control", False, _wg_benign),
    WriteGuardRow("block_goals_log_hand_write", "fire", True, _wg_goals_log_fire),
    WriteGuardRow("block_goals_log_hand_write", "control", False, _wg_benign),
    WriteGuardRow(
        "block_hand_authored_sidecar_creation",
        "fire",
        True,
        _wg_hand_authored_sidecar_creation_fire,
    ),
    WriteGuardRow("block_hand_authored_sidecar_creation", "control", False, _wg_benign),
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
    WriteGuardRow(
        "nudge_dangling_sizing_citation", "fire", True, _wg_dangling_sizing_citation_fire
    ),
    WriteGuardRow("nudge_dangling_sizing_citation", "control", False, _wg_benign),
    WriteGuardRow("nudge_em_code_dispatch", "fire", True, _wg_em_code_dispatch_fire),
    WriteGuardRow("nudge_em_code_dispatch", "control", False, _wg_benign),
    WriteGuardRow("nudge_handoff_ac_shape", "fire", True, _wg_handoff_ac_shape_fire),
    WriteGuardRow("nudge_handoff_ac_shape", "control", False, _wg_benign),
    WriteGuardRow(
        "nudge_handoff_author_lint", "fire", True, _wg_handoff_author_lint_fire
    ),
    WriteGuardRow("nudge_handoff_author_lint", "control", False, _wg_benign),
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
        "nudge_session_display_name_as_identifier",
        "fire",
        True,
        _wg_session_display_name_as_identifier_fire,
    ),
    WriteGuardRow("nudge_session_display_name_as_identifier", "control", False, _wg_benign),
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
            "AC2-registration-only: this guard's real fire reads coordinator-content-repo's live schema "
            "corpus/registry manifest off a sibling checkout (coordinator_content_root()) -- not "
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
    WriteGuardRow(
        "p4_checkout_before_edit", "fire", True, _wg_p4_checkout_before_edit_fire
    ),
    WriteGuardRow(
        "p4_checkout_before_edit", "control", False, _wg_p4_checkout_before_edit_control
    ),
    WriteGuardRow(
        "wiki_changelog_prose_advisory",
        "fire",
        True,
        _wg_wiki_changelog_prose_advisory_fire,
    ),
    WriteGuardRow("wiki_changelog_prose_advisory", "control", False, _wg_benign),
]


_WG_NAMES, _WG_IMPORT_FAILED = write_guards_engine.discover_guard_names()
#: Moved out of module scope into
#: `test_guard_corpus_registration_invariants.py` (docs/plans/2026-08-07-
#: install-dogfood-mechanical-residue.md, chunk C3, F13a) -- same rationale
#: as `_FLIPPED_TO_ADVISORY_REWRITE` above. `_WG_NAMES`/`_WG_IMPORT_FAILED`
#: stay here as the shared computation the moved assertions depend on; a
#: real import failure among the discovered write guards must still surface
#: loudly, now as a failing test rather than a collection error.


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
    fire: Callable[[], Optional[Dict[str, Any]]]


def _hook_envelope_from_message(message: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
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
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-era-", dir=_neutral_scratch_parent()) as scratch:
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
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-nhdd-", dir=_neutral_scratch_parent()) as scratch:
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
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-nhdd-ctrl-", dir=_neutral_scratch_parent()) as scratch:
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
    text = _hook_nudge_unrouted_sizing._build_plan_message("docs/plans/foo.md", "ready")
    return {"hookSpecificOutput": {"additionalContext": text}}


import asyncio as _real_asyncio


class _HooksAsyncioShim:
    """Local shim over the stdlib `asyncio` module (never mutates the real
    module) -- some `hooks.*` handlers below are plain `def` now (dispatch
    offloads them via `asyncio.to_thread`; see
    `_ZERO_AWAIT_PRE_EXISTING_ALLOWLIST`'s discharge note), so a call site's
    result may already be a resolved value rather than a coroutine.
    `asyncio.run` only accepts a coroutine -- pass a plain value straight
    through instead."""

    def run(self, result, *args, **kwargs):
        if _real_asyncio.iscoroutine(result):
            return _real_asyncio.run(result, *args, **kwargs)
        return result

    def __getattr__(self, name):
        return getattr(_real_asyncio, name)


_hooks_asyncio = _HooksAsyncioShim()

from coordinator_core.hooks import block_unenumerated_agent_type as _hook_block_unenumerated_agent_type
from coordinator_core.hooks import cater_subagent_start as _hook_cater_subagent_start
from coordinator_core.hooks import nudge_autonomous_askuserquestion as _hook_nudge_autonomous_askuserquestion
from coordinator_core.hooks import plan_persistence_check as _hook_plan_persistence_check
from coordinator_core.hooks import watchdog_undischarged_next_move as _hook_watchdog_undischarged_next_move
from coordinator_core.session import machinery_paths
from coordinator_core.hooks import coordinator_reminder as _hook_coordinator_reminder
from coordinator_core.hooks import enforce_agent_model_pin as _hook_enforce_agent_model_pin
from coordinator_core.hooks import nudge_em_code_dispatch as _hook_nudge_em_code_dispatch
from coordinator_core.hooks import nudge_foreground_agent_dispatch as _hook_nudge_foreground_agent_dispatch
from coordinator_core.hooks import nudge_named_agent_report_delivery as _hook_nudge_named_agent_report_delivery
from coordinator_core.hooks import nudge_unauthorized_handoff as _hook_nudge_unauthorized_handoff
from coordinator_core.hooks import postuse_advisory_dispatch as _hook_postuse_advisory_dispatch
from coordinator_core.hooks import example_retrieval_repo_detect as _hook_example_retrieval_repo_detect
from coordinator_core.hooks import flag_em_poll_in_flight as _hook_flag_em_poll_in_flight
from coordinator_core.hooks import guard_terminal_review as _hook_guard_terminal_review
from coordinator_core.hooks import preuse_search_dispatch as _hook_preuse_search_dispatch
from coordinator_core.hooks import receiver_state_sensor as _hook_receiver_state_sensor
from coordinator_core.hooks import subagent_sidecar_fill_check as _hook_subagent_sidecar_fill_check
from coordinator_core.hooks import suggest_sonnet_research as _hook_suggest_sonnet_research
from coordinator_core.hooks import ue_knowledge_distrust as _hook_ue_knowledge_distrust
from coordinator_core.hooks import agent_completion_log as _hook_agent_completion_log
from coordinator_core.hooks import agent_postuse_dispatch as _hook_agent_postuse_dispatch
from coordinator_core.hooks import context_pressure_precompact as _hook_context_pressure_precompact
from coordinator_core.hooks import subagent_arrival_check as _hook_subagent_arrival_check
from coordinator_core.hooks import subagent_fabrication_check as _hook_subagent_fabrication_check
from coordinator_core.hooks import subagent_review_mark as _hook_subagent_review_mark
from coordinator_core.hooks import subagent_zero_tool_use as _hook_subagent_zero_tool_use
from coordinator_core.hooks import subagent_zero_tool_use_resolve as _hook_subagent_zero_tool_use_resolve
from coordinator_core.hooks import subagent_zero_tool_use_surface as _hook_subagent_zero_tool_use_surface
from coordinator_core.hooks import track_dispatched_agents as _hook_track_dispatched_agents
from coordinator_core.hooks import track_touched_files as _hook_track_touched_files
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs


from coordinator_core.hooks import allow_emitted_workflow_fire as _hook_allow_emitted_workflow_fire
from coordinator_core.hooks import assert_em_role as _hook_assert_em_role
from coordinator_core.hooks import block_dispatch_suite_invocation as _hook_block_dispatch_suite_invocation
from coordinator_core.hooks import block_ungranted_opus_subagent as _hook_block_ungranted_opus_subagent
from coordinator_core.hooks import block_workflow_foreign_emission as _hook_block_workflow_foreign_emission
from coordinator_core.hooks import block_workflow_unmodeled_agent as _hook_block_workflow_unmodeled_agent
from coordinator_core.hooks import block_worktree_tool as _hook_block_worktree_tool
from coordinator_core.hooks import check_claude_md_size as _hook_check_claude_md_size
from coordinator_core.hooks import derive_global_doctrine_live_copy as _hook_derive_global_doctrine_live_copy
from coordinator_core.hooks import derive_setup_copies as _hook_derive_setup_copies
from coordinator_core.hooks import doctrine_changelog_prose as _hook_doctrine_changelog_prose_data
from coordinator_core.hooks import enforce_agent_dispatch_mode as _hook_enforce_agent_dispatch_mode
from coordinator_core.hooks import group_em_autofire as _hook_group_em_autofire
from coordinator_core.hooks import guard_config_change_hookstack_selfdefence as _hook_guard_config_change_hookstack_selfdefence
from coordinator_core.hooks import guard_doctrine_changelog_prose as _hook_guard_doctrine_changelog_prose
from coordinator_core.hooks import guard_doctrine_surface_bash_write as _hook_guard_doctrine_surface_bash_write
from coordinator_core.hooks import guard_doctrine_surface_ratio as _hook_guard_doctrine_surface_ratio
from coordinator_core.hooks import guard_handoff_summary_cap_on_write as _hook_guard_handoff_summary_cap_on_write
from coordinator_core.hooks import guard_hook_generation_self_probe as _hook_guard_hook_generation_self_probe
from coordinator_core.hooks import guard_host_subagent_bash_ban as _hook_guard_host_subagent_bash_ban
from coordinator_core.hooks import guard_host_subagent_bash_spawn_shapes as _hook_guard_host_subagent_bash_spawn_shapes
from coordinator_core.hooks import guard_kira_verdict_routed as _hook_guard_kira_verdict_routed
from coordinator_core.hooks import guard_manufactured_blocker as _hook_guard_manufactured_blocker
from coordinator_core.hooks import guard_named_dispatch_tool_restriction as _hook_guard_named_dispatch_tool_restriction
from coordinator_core.hooks import guard_posix_invocation_doctrine_write as _hook_guard_posix_invocation_doctrine_write
from coordinator_core.hooks import guard_python_syntax_on_write as _hook_guard_python_syntax_on_write
from coordinator_core.hooks import guard_repo_setup_claude_home_refusal as _hook_guard_repo_setup_claude_home_refusal
from coordinator_core.hooks import guard_test_tree_git_fixture_spawn as _hook_guard_test_tree_git_fixture_spawn
from coordinator_core.hooks import nudge_cross_repo_cwd_boundary as _hook_nudge_cross_repo_cwd_boundary
from coordinator_core.hooks import nudge_initiative_goals_ladder as _hook_nudge_initiative_goals_ladder
from coordinator_core.hooks import nudge_multiwave_workflow as _hook_nudge_multiwave_workflow
from coordinator_core.hooks import nudge_plan_test_surface_tier as _hook_nudge_plan_test_surface_tier
from coordinator_core.hooks import nudge_workflow_authoring_trampoline as _hook_nudge_workflow_authoring_trampoline
from coordinator_core.hooks import offer_exploration_tier_dispatch as _hook_offer_exploration_tier_dispatch
from coordinator_core.hooks import postuse_stop_family_dispatch as _hook_postuse_stop_family_dispatch
from coordinator_core.hooks import postusefailure_cross_repo_memo_remediate as _hook_postusefailure_cross_repo_memo_remediate
from coordinator_core.hooks import preuse_agent_dispatch as _hook_preuse_agent_dispatch
from coordinator_core.hooks import preuse_bash_dispatch as _hook_preuse_bash_dispatch
from coordinator_core.hooks import preuse_skill_dispatch as _hook_preuse_skill_dispatch
from coordinator_core.hooks import preuse_write_dispatch as _hook_preuse_write_dispatch
from coordinator_core.hooks import project_orientation as _hook_project_orientation
from coordinator_core.hooks import runtime_tripwire_em_check as _hook_runtime_tripwire_em_check
from coordinator_core.hooks import session_start_announce_job_mode as _hook_session_start_announce_job_mode
from coordinator_core.hooks import session_start_cloud_focus as _hook_session_start_cloud_focus
from coordinator_core.hooks import session_start_guard_plane_check as _hook_session_start_guard_plane_check
from coordinator_core.hooks import session_start_watch_presence as _hook_session_start_watch_presence
from coordinator_core.hooks import sessionstart_async_dispatch as _hook_sessionstart_async_dispatch
from coordinator_core.hooks import sessionstart_bin_drift_refresh as _hook_sessionstart_bin_drift_refresh
from coordinator_core.hooks import sessionstart_dispatch as _hook_sessionstart_dispatch
from coordinator_core.hooks import sessionstart_ensure_http_forwarder as _hook_sessionstart_ensure_http_forwarder
from coordinator_core.hooks import stop_dispatch as _hook_stop_dispatch
from coordinator_core.hooks import strip_worktree_isolation as _hook_strip_worktree_isolation
import coordinator_core.ops.session.guard_hook_generation_self_probe as _ops_guard_hook_generation_self_probe


def _run_maybe_async(result: Any) -> Any:
    if _hooks_asyncio.iscoroutine(result):
        return _hooks_asyncio.run(result)
    return result


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


def _fire_agent_completion_log_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_agent_completion_log._handler({}, repo_root=None))
    )


def _fire_agent_postuse_dispatch_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_agent_postuse_dispatch._handler({}, repo_root=None))
    )


def _fire_cater_subagent_start_missing_provisioning() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_cater_subagent_start._handler({}, repo_root=None))
    )


def _fire_subagent_review_mark_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_subagent_review_mark._handler({}, repo_root=None))
    )


# --- AC10 coverage-gap closers, 2026-08-31. Three hooks modules landed
# between 2026-08-18 and 2026-08-31 with neither a corpus row nor a named
# exemption, which `test_ac10_coverage_gap_is_empty_or_named_exemption`
# reports red. Two get real firing rows below; `sessionend_archive_session`
# gets a named exemption instead (see REGISTER_COVERAGE_EXEMPTIONS -- its
# only entrypoint archives a REAL session claim directory, and it emits no
# agent-facing text at all).


def _fire_nudge_autonomous_askuserquestion() -> Optional[Dict[str, Any]]:
    text = _hook_nudge_autonomous_askuserquestion._compose_advisory("default")
    return {"hookSpecificOutput": {"additionalContext": text}}


def _fire_nudge_autonomous_askuserquestion_control() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hook_nudge_autonomous_askuserquestion._handler(
            {"payload": {"agent_id": "corpus-control-agent"}}
        )
    )


def _fire_plan_persistence_check_persisted() -> Optional[Dict[str, Any]]:
    text = _hook_plan_persistence_check._persisted_text(
        'git add -- docs/plans/2026-01-01-corpus.md && '
        'git commit -m "plan: corpus" -- docs/plans/2026-01-01-corpus.md'
    )
    return {"hookSpecificOutput": {"additionalContext": text}}


def _fire_plan_persistence_check_control() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hook_plan_persistence_check._handler({"payload": {"tool_name": "Write"}})
    )


def _fire_watchdog_undischarged_next_move_stop() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(dir=_neutral_scratch_parent()) as tmp:
        os.makedirs(os.path.join(tmp, ".git"), exist_ok=True)
        session_id = "corpus-watchdog-session"
        ledger_path = machinery_paths.ledger_path(tmp, session_id)
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        record = {
            "obligation_id": "corpus-obligation-1",
            "seam": "corpus-seam",
            "next_action": "/handoff",
            "discharged": False,
            "fired": False,
        }
        with open(ledger_path, "w", encoding="utf-8") as handle:
            handle.write(_json.dumps(record) + "\n")
        return _to_envelope_or_none(
            _hook_watchdog_undischarged_next_move._handle_stop(
                {
                    "session_id": session_id,
                    "cwd": tmp,
                    "transcript_path": os.path.join(tmp, "transcript.jsonl"),
                }
            )
        )


def _fire_watchdog_undischarged_next_move_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(dir=_neutral_scratch_parent()) as tmp:
        os.makedirs(os.path.join(tmp, ".git"), exist_ok=True)
        session_id = "corpus-watchdog-control-session"
        return _to_envelope_or_none(
            _hook_watchdog_undischarged_next_move._handle_stop(
                {
                    "session_id": session_id,
                    "cwd": tmp,
                    "transcript_path": os.path.join(tmp, "transcript.jsonl"),
                }
            )
        )


def _fire_block_unenumerated_agent_type() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "totally-bogus-nonexistent-role-zz"},
    }
    return _to_envelope_or_none(_hook_block_unenumerated_agent_type.check(payload))


def _fire_block_unenumerated_agent_type_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    return _to_envelope_or_none(_hook_block_unenumerated_agent_type.check(payload))


def _fire_context_pressure_precompact_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(_hook_context_pressure_precompact._handler({}, repo_root=None))


def _fire_enforce_agent_model_pin() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            _hook_enforce_agent_model_pin,
            "resolve_model_pins",
            lambda *, content_root=None: (
                {
                    "coordinator:executor": {
                        "model": "sonnet",
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
            lambda *, content_root=None: ({}, None),
        )
        payload = {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "coordinator:executor", "model": "opus", "prompt": "go"},
        }
        return _to_envelope_or_none(_hook_enforce_agent_model_pin.check(payload))


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


# The fire-reroute fixture above always forwards a `prompt`,
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


def _fire_nudge_unauthorized_handoff() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Write",
        "file_path": "state/handoffs/2026-08-12-example.md",
        "content": "no frontmatter here\n",
        "transcript_path": "",
    }
    return _to_envelope_or_none(
        _run_maybe_async(_hook_nudge_unauthorized_handoff._handler(payload, repo_root=None))
    )


def _fire_nudge_unauthorized_handoff_control() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Write",
        "file_path": "state/handoffs/2026-08-12-example.md",
        "content": "kind: recovery\n",
        "transcript_path": "",
    }
    return _to_envelope_or_none(
        _run_maybe_async(_hook_nudge_unauthorized_handoff._handler(payload, repo_root=None))
    )


def _fire_postuse_advisory_dispatch() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Write",
        "file_path": "state/handoffs/2026-08-12-example.md",
        "content": "no frontmatter here\n",
        "transcript_path": "",
    }
    return _to_envelope_or_none(
        _run_maybe_async(_hook_postuse_advisory_dispatch._handler(payload, repo_root=None))
    )


def _fire_postuse_advisory_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Read", "file_path": "", "content": "", "transcript_path": ""}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_postuse_advisory_dispatch._handler(payload, repo_root=None))
    )


# --- (12) example_retrieval_repo_detect -- real firing row: `detect_banner(cwd)` returns
# the UNINITIALIZED banner string for a scratch dir carrying a `.project-rag/
# manifest.json` marker with no `graph.db` beside it. The example-game-repo-dedupe
# probe's OWN upward walk is stubbed out here (real `.example-game-repo`/`Saved/
# ExampleGameRepoProjectRag` markers on the machine's own home directory, several
# levels above a bare `tempfile.gettempdir()` scratch dir, are within
# `_find_marker_upward`'s `_MAX_LEVELS=6` walk on a real developer box and
# silently short-circuit `detect_banner` to "" before the manifest marker
# this row exists to exercise is ever reached -- corroborated live: this
# row failed on a box with a real `C:\Users\<name>\.example-game-repo`) -- the
# manifest lookup itself is left real, only the example-game-repo legs are forced
# absent so this row exercises the UNINITIALIZED banner path regardless of
def _git_scratch_repo(scratch: str) -> None:
    subprocess.run(["git", "init", "-q", scratch], check=True, **no_console_passthrough_kwargs())


def _fire_flag_em_poll_in_flight_with(transcript_tools: List[str], run_in_flight: bool) -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-fepif-", dir=_neutral_scratch_parent()) as scratch:
            session_id = "sess-fepif"
            if run_in_flight:
                (Path(scratch) / ("workflow-run-%s-t1.json" % session_id)).write_text(
                    _json.dumps({"run_id": "run-1"}), encoding="utf-8"
                )
            transcript = Path(scratch) / "transcript.jsonl"
            transcript.write_text(
                "".join(
                    _json.dumps(
                        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}
                    )
                    + "\n"
                    for name in transcript_tools
                ),
                encoding="utf-8",
            )
            mp.setattr(_hook_flag_em_poll_in_flight, "_tmpdir", lambda: scratch)
            payload = {
                "tool_name": "TaskList",
                "session_id": session_id,
                "transcript_path": str(transcript),
            }
            return _to_envelope_or_none(_run_maybe_async(_hook_flag_em_poll_in_flight._handler(payload)))


def _fire_flag_em_poll_in_flight() -> Optional[Dict[str, Any]]:
    return _fire_flag_em_poll_in_flight_with(["TaskList"], run_in_flight=True)


def _fire_flag_em_poll_in_flight_control() -> Optional[Dict[str, Any]]:
    return _fire_flag_em_poll_in_flight_with(["Bash"], run_in_flight=True)


def _fire_guard_terminal_review_with(commit_path: str) -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gtr-", dir=_neutral_scratch_parent()) as scratch:
        _git_scratch_repo(scratch)
        (Path(scratch) / commit_path).write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", scratch, "add", commit_path], check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "-C", scratch, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
             "-m", "work", "-m", "Session-Id: sess-gtr"],
            check=True,
            **no_console_passthrough_kwargs(),
        )
        payload = {"payload": {"session_id": "sess-gtr", "cwd": scratch, "stop_hook_active": False}}
        return _to_envelope_or_none(_run_maybe_async(_hook_guard_terminal_review._guard_terminal_review_handler(payload)))


def _fire_guard_terminal_review() -> Optional[Dict[str, Any]]:
    return _fire_guard_terminal_review_with("app.py")


def _fire_guard_terminal_review_control() -> Optional[Dict[str, Any]]:
    return _fire_guard_terminal_review_with("notes.md")


def _fire_preuse_search_dispatch_with(tool_name: str) -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-psd-", dir=_neutral_scratch_parent()) as scratch:
        _git_scratch_repo(scratch)
        rag_dir = Path(scratch) / ".project-rag"
        rag_dir.mkdir()
        (rag_dir / "graph.db").write_text("", encoding="utf-8")
        payload = {
            "tool_name": tool_name,
            "tool_input": {"pattern": "def parse_widget"},
            "session_id": "11111111-2222-3333-4444-555555555555",
            "cwd": scratch,
        }
        return _to_envelope_or_none(_run_maybe_async(_hook_preuse_search_dispatch._handler(payload)))


def _fire_preuse_search_dispatch() -> Optional[Dict[str, Any]]:
    return _fire_preuse_search_dispatch_with("Grep")


def _fire_preuse_search_dispatch_control() -> Optional[Dict[str, Any]]:
    return _fire_preuse_search_dispatch_with("Bash")


def _fire_example_retrieval_repo_detect() -> Optional[Dict[str, Any]]:
    real_find_marker_upward = _hook_example_retrieval_repo_detect._find_marker_upward

    def _stub_find_marker_upward(start_dir: str, marker: str, max_levels: int = 6) -> Optional[str]:
        if marker in (".example-game-repo", os.path.join("Saved", "ExampleGameRepoProjectRag")):
            return None
        return real_find_marker_upward(start_dir, marker, max_levels)

    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-prd-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        os.makedirs(scratch_dir / ".project-rag")
        (scratch_dir / ".project-rag" / "state.json").write_text("{}", encoding="utf-8")
        with unittest.mock.patch.object(
            _hook_example_retrieval_repo_detect, "_find_marker_upward", _stub_find_marker_upward
        ):
            banner = _hook_example_retrieval_repo_detect.detect_banner(str(scratch_dir))
        if not banner:
            return None
        return {"hookSpecificOutput": {"additionalContext": banner}}


def _fire_example_retrieval_repo_detect_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-prd-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        banner = _hook_example_retrieval_repo_detect.detect_banner(scratch)
        if not banner:
            return None
        return {"hookSpecificOutput": {"additionalContext": banner}}


# --- (14) subagent_arrival_check -- structured JSON-RPC poll result, NOT an
# advisory envelope: `_handler`'s own docstring, "Returns the pinned {"state",
# "agent_id", "subagent_transcript_path", "reason"} shape directly (structured
# JSON-RPC result, not an advisory envelope)". Verified live: the result carries
# no `hookSpecificOutput` key at all.
def _fire_subagent_arrival_check_structured() -> Optional[Dict[str, Any]]:
    result = _run_maybe_async(_hook_subagent_arrival_check._handler({}))
    assert "hookSpecificOutput" not in result, (
        "subagent_arrival_check began emitting an advisory envelope -- C12's "
        "no-agent-facing-emitter classification is stale"
    )
    return _to_envelope_or_none(result)


# --- (15) subagent_fabrication_check -- structured JSON-RPC verdict result, not
# an advisory envelope (own `_envelope()` helper returns a plain {"verdict", ...}
# dict, no `hookSpecificOutput`). Verified live with params={}.
def _fire_subagent_fabrication_check_structured() -> Optional[Dict[str, Any]]:
    result = _run_maybe_async(_hook_subagent_fabrication_check._handler({}, repo_root=None))
    assert "hookSpecificOutput" not in result, (
        "subagent_fabrication_check began emitting an advisory envelope -- C12's "
        "no-agent-facing-emitter classification is stale"
    )
    return _to_envelope_or_none(result)


def _fire_subagent_zero_tool_use_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_subagent_zero_tool_use._handler({}, repo_root=None))
    )


# --- (17) subagent_zero_tool_use_resolve -- structured JSON-RPC verdict result
# (own `_verdict()` helper), not an advisory envelope. Verified live with
# params={}.
def _fire_subagent_zero_tool_use_resolve_structured() -> Optional[Dict[str, Any]]:
    result = _run_maybe_async(_hook_subagent_zero_tool_use_resolve._handler({}, repo_root=None))
    assert "hookSpecificOutput" not in result, (
        "subagent_zero_tool_use_resolve began emitting an advisory envelope -- "
        "C12's no-agent-facing-emitter classification is stale"
    )
    return _to_envelope_or_none(result)


# --- (18) subagent_zero_tool_use_surface -- structured JSON-RPC read result;
# own module docstring: "this op returns a plain dict" (not an advisory
# envelope). Verified live with params={}.
def _fire_subagent_zero_tool_use_surface_structured() -> Optional[Dict[str, Any]]:
    result = _run_maybe_async(_hook_subagent_zero_tool_use_surface._handler({}, repo_root=None))
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
        return _to_envelope_or_none(_run_maybe_async(_hook_suggest_sonnet_research._handler(payload)))


def _fire_suggest_sonnet_research_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_hook_suggest_sonnet_research, "_has_deep_research_plugin", lambda: False)
        payload = {"agent_id": "arscout-deadbeef123456ab", "session_id": "abcdefgh-full-session"}
        return _to_envelope_or_none(_run_maybe_async(_hook_suggest_sonnet_research._handler(payload)))


def _fire_track_dispatched_agents_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_track_dispatched_agents._handler({}, repo_root=None))
    )


def _fire_track_touched_files_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_track_touched_files._handler({}, repo_root=None))
    )


def _fire_receiver_state_sensor_noop() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_receiver_state_sensor._handler({}, repo_root=None))
    )


def _fire_subagent_sidecar_fill_check() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-sfc-", dir=_neutral_scratch_parent()) as scratch:
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
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-sfc-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        (scratch_dir / ".git").mkdir()
        session_id = "sidecar-fill-check-corpus-row-control"
        payload = {"session_id": session_id}
        return _to_envelope_or_none(
            _hook_subagent_sidecar_fill_check._handler(payload, repo_root=str(scratch_dir))
        )


def _fire_ue_knowledge_distrust() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-ukd-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        (scratch_dir / "Example.uproject").write_text("{}", encoding="utf-8")
        result = _hook_ue_knowledge_distrust.run(str(scratch_dir), str(scratch_dir))
        if not result.banner:
            return None
        return {"hookSpecificOutput": {"additionalContext": result.banner}}


def _fire_ue_knowledge_distrust_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-hooks-ukd-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        result = _hook_ue_knowledge_distrust.run(scratch, scratch)
        if not result.banner:
            return None
        return {"hookSpecificOutput": {"additionalContext": result.banner}}


def _fire_coordinator_reminder() -> Optional[Dict[str, Any]]:
    text = _hook_coordinator_reminder.render_reminder(None)
    if not text:
        return None
    return {"hookSpecificOutput": {"additionalContext": text}}


def _fire_allow_emitted_workflow_fire() -> Optional[Dict[str, Any]]:
    import hashlib

    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-aewf-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        script = scratch_dir / "run.mjs"
        script.write_text("console.log(1)")
        sha = hashlib.sha256(script.read_bytes()).hexdigest()
        receipt = script.with_name(script.name + ".emitted.json")
        receipt.write_text(_json.dumps({"sha256": sha, "session_id": "sess-x", "plan": "plan.md"}))
        payload = {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(scratch_dir),
            "session_id": "sess-x",
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_allow_emitted_workflow_fire._handler(payload))
        )


def _fire_allow_emitted_workflow_fire_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-aewf-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        script = scratch_dir / "run.mjs"
        script.write_text("console.log(1)")
        payload = {"tool_name": "Workflow", "tool_input": {"scriptPath": str(script)}, "cwd": str(scratch_dir)}
        return _to_envelope_or_none(
            _run_maybe_async(_hook_allow_emitted_workflow_fire._handler(payload))
        )


def _fire_assert_em_role() -> Optional[Dict[str, Any]]:
    payload = {"payload": {"cwd": "/tmp", "session_id": "sess-aer"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_assert_em_role._handler(payload)))


def _fire_block_dispatch_suite_invocation() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "tool_input": {"prompt": "Now run the full test suite: pytest coordinator_core/"},
    }
    return _to_envelope_or_none(
        _run_maybe_async(_hook_block_dispatch_suite_invocation._handler(payload))
    )


def _fire_block_dispatch_suite_invocation_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"prompt": "Please implement the feature."}}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_block_dispatch_suite_invocation._handler(payload))
    )


def _fire_block_ungranted_opus_subagent() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose", "model": "opus"}}
    return _to_envelope_or_none(_hook_block_ungranted_opus_subagent.check(payload))


def _fire_block_ungranted_opus_subagent_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose", "model": "sonnet"}}
    return _to_envelope_or_none(_hook_block_ungranted_opus_subagent.check(payload))


def _fire_block_workflow_foreign_emission() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-bwfe-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        script = scratch_dir / "run.mjs"
        script.write_text("console.log(1)")
        receipt = script.with_name(script.name + ".emitted.json")
        receipt.write_text(_json.dumps({"sha256": "deadbeef", "session_id": "sess-x"}))
        payload = {
            "tool_name": "Workflow",
            "tool_input": {"scriptPath": str(script)},
            "cwd": str(scratch_dir),
            "session_id": "sess-y",
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_block_workflow_foreign_emission._handler(payload))
        )


def _fire_block_workflow_foreign_emission_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-bwfe-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        script = scratch_dir / "run.mjs"
        script.write_text("console.log(1)")
        payload = {"tool_name": "Workflow", "tool_input": {"scriptPath": str(script)}, "cwd": str(scratch_dir)}
        return _to_envelope_or_none(
            _run_maybe_async(_hook_block_workflow_foreign_emission._handler(payload))
        )


def _fire_block_workflow_unmodeled_agent() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-bwua-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        transcript = scratch_dir / "t.jsonl"
        transcript.write_text(_json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-1"}}) + "\n")
        payload = {
            "tool_name": "Workflow",
            "tool_input": {"script": 'await agent("do the thing")'},
            "transcript_path": str(transcript),
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_block_workflow_unmodeled_agent._handler(payload))
        )


def _fire_block_workflow_unmodeled_agent_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-bwua-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        transcript = scratch_dir / "t.jsonl"
        transcript.write_text(_json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-1"}}) + "\n")
        payload = {
            "tool_name": "Workflow",
            "tool_input": {"script": 'await agent("do the thing", {model: "sonnet"})'},
            "transcript_path": str(transcript),
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_block_workflow_unmodeled_agent._handler(payload))
        )


def _fire_block_worktree_tool() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_block_worktree_tool._handler({"tool_name": "EnterWorktree"}))
    )


def _fire_block_worktree_tool_control() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_block_worktree_tool._handler({"tool_name": "ExitWorktree"}))
    )


def _fire_check_claude_md_size() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ccms-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        target = scratch_dir / "CLAUDE.md"
        target.write_text("hello")
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "x" * 50000}}
        return _to_envelope_or_none(_run_maybe_async(_hook_check_claude_md_size._handler(payload)))


def _fire_check_claude_md_size_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ccms-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        target = scratch_dir / "CLAUDE.md"
        target.write_text("hello")
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "small"}}
        return _to_envelope_or_none(_run_maybe_async(_hook_check_claude_md_size._handler(payload)))


def _fire_derive_global_doctrine_live_copy() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-dgdlc-", dir=_neutral_scratch_parent()) as scratch:
            scratch_dir = Path(scratch)
            repo_root = scratch_dir / "coordinator-claude"
            repo_root.mkdir()
            (repo_root / ".coordinator-dev-repo").write_text("1")
            tracked_dir = repo_root / "global-doctrine"
            tracked_dir.mkdir()
            tracked = tracked_dir / "CLAUDE.md"
            tracked.write_text("doctrine content")
            home_dir = scratch_dir / "home"
            (home_dir / ".claude").mkdir(parents=True)
            mp.setenv("CLAUDE_PLUGIN_ROOT", str(repo_root / "coordinator"))
            mp.setattr(Path, "home", staticmethod(lambda: home_dir))
            payload = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(tracked)},
            }
            return _to_envelope_or_none(
                _run_maybe_async(_hook_derive_global_doctrine_live_copy._handler(payload))
            )


def _fire_derive_global_doctrine_live_copy_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-dgdlc-ctrl-", dir=_neutral_scratch_parent()) as scratch:
            scratch_dir = Path(scratch)
            repo_root = scratch_dir / "coordinator-claude"
            repo_root.mkdir()
            (repo_root / ".coordinator-dev-repo").write_text("1")
            tracked_dir = repo_root / "global-doctrine"
            tracked_dir.mkdir()
            home_dir = scratch_dir / "home"
            (home_dir / ".claude").mkdir(parents=True)
            mp.setenv("CLAUDE_PLUGIN_ROOT", str(repo_root / "coordinator"))
            mp.setattr(Path, "home", staticmethod(lambda: home_dir))
            payload = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(tracked_dir / "unrelated.md")},
            }
            return _to_envelope_or_none(
                _run_maybe_async(_hook_derive_global_doctrine_live_copy._handler(payload))
            )


def _fire_derive_setup_copies() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-dsc-", dir=_neutral_scratch_parent()) as scratch:
            scratch_dir = Path(scratch)
            repo_root = scratch_dir / "coordinator-claude"
            repo_root.mkdir()
            (repo_root / ".coordinator-dev-repo").write_text("1")
            canonical_dir = repo_root / "setup" / "percolate-hooks"
            canonical_dir.mkdir(parents=True)
            canonical = canonical_dir / "percolate-store.yaml"
            canonical.write_text("a: 1")
            mp.setenv("CLAUDE_PLUGIN_ROOT", str(repo_root / "coordinator"))
            payload = {"tool_name": "Write", "tool_input": {"file_path": str(canonical)}}
            return _to_envelope_or_none(_run_maybe_async(_hook_derive_setup_copies._handler(payload)))


def _fire_derive_setup_copies_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-dsc-ctrl-", dir=_neutral_scratch_parent()) as scratch:
            scratch_dir = Path(scratch)
            repo_root = scratch_dir / "coordinator-claude"
            repo_root.mkdir()
            (repo_root / ".coordinator-dev-repo").write_text("1")
            canonical_dir = repo_root / "setup" / "percolate-hooks"
            canonical_dir.mkdir(parents=True)
            mp.setenv("CLAUDE_PLUGIN_ROOT", str(repo_root / "coordinator"))
            payload = {"tool_name": "Write", "tool_input": {"file_path": str(canonical_dir / "unrelated.txt")}}
            return _to_envelope_or_none(_run_maybe_async(_hook_derive_setup_copies._handler(payload)))


def _fire_enforce_agent_dispatch_mode() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"name": "bad/name"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_enforce_agent_dispatch_mode._handler(payload)))


def _fire_enforce_agent_dispatch_mode_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "coordinator:executor"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_enforce_agent_dispatch_mode._handler(payload)))


def _fire_group_em_autofire() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        def _fake_handler(params, repo_root=None):
            return {"nomination": {"claimed": True, "message": "ok"}, "roster": [], "digest": {}}

        from coordinator_core import ipc as _ipc

        _real_lookup = _ipc.get_op_handler  # dispatch reads this name too
        mp.setattr(
            _ipc,
            "get_op_handler",
            lambda name: _fake_handler if name == "groupem.enter" else _real_lookup(name),
        )
        payload = {"command_name": "group-em", "session_id": "sess-1", "cwd": "/tmp"}
        return _to_envelope_or_none(_hook_group_em_autofire._handler(payload))


def _fire_group_em_autofire_control() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(_hook_group_em_autofire._handler({"command_name": "not-group-em"}))


def _fire_guard_doctrine_changelog_prose() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gdcp-", dir=_neutral_scratch_parent()) as scratch:
            skills_dir = Path(scratch) / "skills"
            skills_dir.mkdir()
            mp.setattr(_hook_doctrine_changelog_prose_data, "DOCTRINE_MD_DIRS", (skills_dir.resolve(),))
            target = skills_dir / "foo.md"
            payload = {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": str(target),
                    "content": "This rule was retired on 2026-01-01, superseded by DR-047.\n",
                },
            }
            return _to_envelope_or_none(
                _run_maybe_async(_hook_guard_doctrine_changelog_prose._handler(payload))
            )


def _fire_guard_doctrine_changelog_prose_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gdcp-ctrl-", dir=_neutral_scratch_parent()) as scratch:
            skills_dir = Path(scratch) / "skills"
            skills_dir.mkdir()
            mp.setattr(_hook_doctrine_changelog_prose_data, "DOCTRINE_MD_DIRS", (skills_dir.resolve(),))
            target = skills_dir / "foo.md"
            payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "This rule requires X.\n"}}
            return _to_envelope_or_none(
                _run_maybe_async(_hook_guard_doctrine_changelog_prose._handler(payload))
            )


def _fire_guard_doctrine_surface_bash_write() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Bash", "tool_input": {"command": 'echo "hello" > CLAUDE.md'}}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_guard_doctrine_surface_bash_write._handler(payload))
    )


def _fire_guard_doctrine_surface_bash_write_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Bash", "tool_input": {"command": "cat CLAUDE.md"}}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_guard_doctrine_surface_bash_write._handler(payload))
    )


def _fire_guard_doctrine_surface_ratio() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gdsr-", dir=_neutral_scratch_parent()) as scratch:
            wiki_dir = Path(scratch) / "wiki"
            wiki_dir.mkdir()
            mp.setattr(_hook_doctrine_changelog_prose_data, "DOCTRINE_MD_DIRS", (wiki_dir.resolve(),))
            mp.setattr(
                _hook_guard_doctrine_surface_ratio,
                "tier_boundaries_for",
                lambda surface: {
                    "ceiling": 4000,
                    "tiers": [
                        {"max": 4000, "ratio": 2, "credit_scope": "surface"},
                        {"max": 12000, "ratio": 5, "credit_scope": "file"},
                        {"max": None, "ratio": 10, "credit_scope": "file"},
                    ],
                },
            )
            target = wiki_dir / "foo.md"
            target.write_text("a" * 2000)
            payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "a" * 2000 + "b" * 2000}}
            return _to_envelope_or_none(
                _run_maybe_async(_hook_guard_doctrine_surface_ratio._handler(payload))
            )


def _fire_guard_doctrine_surface_ratio_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gdsr-ctrl-", dir=_neutral_scratch_parent()) as scratch:
            wiki_dir = Path(scratch) / "wiki"
            wiki_dir.mkdir()
            mp.setattr(_hook_doctrine_changelog_prose_data, "DOCTRINE_MD_DIRS", (wiki_dir.resolve(),))
            mp.setattr(
                _hook_guard_doctrine_surface_ratio,
                "tier_boundaries_for",
                lambda surface: {
                    "ceiling": 4000,
                    "tiers": [
                        {"max": 4000, "ratio": 2, "credit_scope": "surface"},
                        {"max": 12000, "ratio": 5, "credit_scope": "file"},
                        {"max": None, "ratio": 10, "credit_scope": "file"},
                    ],
                },
            )
            target = wiki_dir / "foo.md"
            target.write_text("a" * 2000)
            payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "a" * 2001}}
            return _to_envelope_or_none(
                _run_maybe_async(_hook_guard_doctrine_surface_ratio._handler(payload))
            )


def _fire_guard_handoff_summary_cap_on_write() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ghsc-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "state" / "handoffs" / "foo.md"
        target.parent.mkdir(parents=True)
        content = "---\nsummary: %s\n---\nbody\n" % ("x" * 150)
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}}
        return _to_envelope_or_none(
            _run_maybe_async(_hook_guard_handoff_summary_cap_on_write._handler(payload))
        )


def _fire_guard_handoff_summary_cap_on_write_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ghsc-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "state" / "handoffs" / "foo.md"
        target.parent.mkdir(parents=True)
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "---\nsummary: short\n---\nbody\n"},
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_guard_handoff_summary_cap_on_write._handler(payload))
        )


def _fire_guard_hook_generation_self_probe() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_ops_guard_hook_generation_self_probe, "run_self_probe", lambda config_dir: "self-probe advisory text")
        return _to_envelope_or_none(_run_maybe_async(_hook_guard_hook_generation_self_probe._handler({})))


def _fire_guard_hook_generation_self_probe_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_ops_guard_hook_generation_self_probe, "run_self_probe", lambda config_dir: "")
        return _to_envelope_or_none(_run_maybe_async(_hook_guard_hook_generation_self_probe._handler({})))


def _fire_guard_host_subagent_bash_ban() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ghsb-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        (scratch_dir / "coordinator.local.md").write_text("---\nsubagent_bash_policy: deny\n---\n")
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": str(scratch_dir),
            "agent_id": "aexecutor-1234567890abcdef",
        }
        return _to_envelope_or_none(_run_maybe_async(_hook_guard_host_subagent_bash_ban._handler(payload)))


def _fire_guard_host_subagent_bash_ban_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_guard_host_subagent_bash_ban._handler(payload)))


def _fire_guard_host_subagent_bash_spawn_shapes() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ghsss-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        (scratch_dir / "coordinator.local.md").write_text("---\nsubagent_bash_spawn_shapes: deny\n---\n")
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat foo.txt | head -20"},
            "cwd": str(scratch_dir),
            "agent_id": "aexecutor-1234567890abcdef",
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_guard_host_subagent_bash_spawn_shapes._handler(payload))
        )


def _fire_guard_host_subagent_bash_spawn_shapes_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Bash", "tool_input": {"command": "npm test"}}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_guard_host_subagent_bash_spawn_shapes._handler(payload))
    )


def _fire_guard_kira_verdict_routed() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _hook_guard_kira_verdict_routed._guard_kira_verdict_routed_handler({"payload": {}})
    )


def _fire_guard_kira_verdict_routed_control() -> Optional[Dict[str, Any]]:
    payload = {"payload": {"session_id": "sess-x", "cwd": "."}}
    return _to_envelope_or_none(
        _hook_guard_kira_verdict_routed._guard_kira_verdict_routed_handler(payload)
    )


def _fire_guard_manufactured_blocker() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gmb-", dir=_neutral_scratch_parent()) as scratch:
        transcript = Path(scratch) / "t.jsonl"
        with open(transcript, "w", encoding="utf-8") as fh:
            fh.write(
                _json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Proceeding with the next step."}]},
                    }
                )
                + "\n"
            )
        payload = {"payload": {"transcript_path": str(transcript), "session_id": "sess-gmb", "cwd": scratch}}
        return _to_envelope_or_none(_hook_guard_manufactured_blocker._handler(payload))


def _fire_guard_manufactured_blocker_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gmb-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        transcript = Path(scratch) / "t.jsonl"
        with open(transcript, "w", encoding="utf-8") as fh:
            fh.write(
                _json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Done. Fixed, verified, closed."}]},
                    }
                )
                + "\n"
            )
        payload = {"payload": {"transcript_path": str(transcript), "session_id": "sess-gmb-2", "cwd": scratch}}
        return _to_envelope_or_none(_hook_guard_manufactured_blocker._handler(payload))


def _fire_guard_named_dispatch_tool_restriction() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "name": "peer1"}}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_guard_named_dispatch_tool_restriction._handler(payload))
    )


def _fire_guard_named_dispatch_tool_restriction_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore"}}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_guard_named_dispatch_tool_restriction._handler(payload))
    )


def _fire_guard_posix_invocation_doctrine_write() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gpid-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "coordinator" / "skills" / "foo.md"
        target.parent.mkdir(parents=True)
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(target),
                "content": '${COORDINATOR_HOME:-$HOME/.claude}/bin/some-cli',
            },
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_guard_posix_invocation_doctrine_write._handler(payload))
        )


def _fire_guard_posix_invocation_doctrine_write_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gpid-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "coordinator" / "skills" / "foo.md"
        target.parent.mkdir(parents=True)
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "plain text"}}
        return _to_envelope_or_none(
            _run_maybe_async(_hook_guard_posix_invocation_doctrine_write._handler(payload))
        )


def _fire_guard_python_syntax_on_write() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gpsw-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "coordinator_core" / "hooks" / "fake_mod.py"
        target.parent.mkdir(parents=True)
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": "def f(:\n    pass"}}
        return _to_envelope_or_none(_run_maybe_async(_hook_guard_python_syntax_on_write._handler(payload)))


def _fire_guard_python_syntax_on_write_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gpsw-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "coordinator_core" / "hooks" / "fake_mod.py"
        target.parent.mkdir(parents=True)
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "def f():\n    pass\n"},
        }
        return _to_envelope_or_none(_run_maybe_async(_hook_guard_python_syntax_on_write._handler(payload)))


def _fire_guard_repo_setup_claude_home_refusal() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-grschr-", dir=_neutral_scratch_parent()) as scratch:
            mp.setenv("CLAUDE_CONFIG_DIR", scratch)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "python3 -m coordinator_core.install.scaffold_structure"},
                "cwd": scratch,
            }
            return _to_envelope_or_none(
                _run_maybe_async(_hook_guard_repo_setup_claude_home_refusal._handler(payload))
            )


def _fire_guard_repo_setup_claude_home_refusal_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-grschr-ctrl-", dir=_neutral_scratch_parent()) as scratch:
            mp.setenv("CLAUDE_CONFIG_DIR", scratch)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "python3 -m coordinator_core.install.scaffold_structure --dry-run"},
                "cwd": scratch,
            }
            return _to_envelope_or_none(
                _run_maybe_async(_hook_guard_repo_setup_claude_home_refusal._handler(payload))
            )


def _fire_guard_test_tree_git_fixture_spawn() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gttgfs-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "tests" / "test_foo.py"
        target.parent.mkdir(parents=True)
        content = "import subprocess\nsubprocess.run(['git', 'commit', '-m', 'x'])\n"
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}}
        return _to_envelope_or_none(
            _run_maybe_async(_hook_guard_test_tree_git_fixture_spawn._handler(payload))
        )


def _fire_guard_test_tree_git_fixture_spawn_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-gttgfs-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "tests" / "test_foo.py"
        target.parent.mkdir(parents=True)
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "def test_x():\n    assert True\n"},
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_guard_test_tree_git_fixture_spawn._handler(payload))
        )


def _fire_nudge_initiative_goals_ladder() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-nigl-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        os.makedirs(os.path.join(scratch, ".git"))
        goals_dir = scratch_dir / "state" / "goals"
        goals_dir.mkdir(parents=True)
        (goals_dir / "g1.yaml").write_text("goal_id: g1\n")
        init_dir = scratch_dir / "state" / "initiatives"
        init_dir.mkdir(parents=True)
        target = init_dir / "myinit.yaml"
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "label: Improve caching\n"},
            "cwd": scratch,
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_nudge_initiative_goals_ladder._handler(payload))
        )


def _fire_nudge_initiative_goals_ladder_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-nigl-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        scratch_dir = Path(scratch)
        os.makedirs(os.path.join(scratch, ".git"))
        goals_dir = scratch_dir / "state" / "goals"
        goals_dir.mkdir(parents=True)
        (goals_dir / "g1.yaml").write_text("goal_id: g1\n")
        init_dir = scratch_dir / "state" / "initiatives"
        init_dir.mkdir(parents=True)
        target = init_dir / "myinit.yaml"
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(target),
                "content": "label: Improve caching\ngoals:\n  - g1\n",
            },
            "cwd": scratch,
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_nudge_initiative_goals_ladder._handler(payload))
        )


def _fire_nudge_multiwave_workflow() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-nmw-", dir=_neutral_scratch_parent()) as scratch:
            os.makedirs(os.path.join(scratch, ".git"))
            mp.setattr(_hook_nudge_multiwave_workflow, "show_toplevel", lambda *a, **kw: scratch)
            session_id = "12345678-1234-4123-8123-%012x" % (uuid.uuid4().int % (16**12))
            result = None
            for _ in range(4):
                payload = {
                    "tool_name": "Agent",
                    "tool_input": {"subagent_type": "coordinator:executor"},
                    "session_id": session_id,
                }
                result = _run_maybe_async(_hook_nudge_multiwave_workflow._handler(payload))
            return _to_envelope_or_none(result)


def _fire_nudge_multiwave_workflow_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_nudge_multiwave_workflow._handler(payload)))


def _fire_nudge_plan_test_surface_tier() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-nptst-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "docs" / "plans" / "foo.md"
        target.parent.mkdir(parents=True)
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(target),
                "content": "## Test surface\nNow run the full test suite: pytest coordinator_core/\n",
            },
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_nudge_plan_test_surface_tier._handler(payload))
        )


def _fire_nudge_plan_test_surface_tier_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-nptst-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        target = Path(scratch) / "docs" / "plans" / "foo.md"
        target.parent.mkdir(parents=True)
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(target),
                "content": "## Test surface\nRun the chunk's own tests: foo/test_bar.py\n",
            },
        }
        return _to_envelope_or_none(
            _run_maybe_async(_hook_nudge_plan_test_surface_tier._handler(payload))
        )


def _fire_nudge_workflow_authoring_trampoline() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-nwat-", dir=_neutral_scratch_parent()) as scratch:
            os.makedirs(os.path.join(scratch, ".git"))
            mp.setattr(_hook_nudge_workflow_authoring_trampoline, "show_toplevel", lambda *a, **kw: scratch)
            payload = {
                "tool_name": "Skill",
                "tool_input": {"skill": "workflow-authoring"},
                "session_id": "12345678-1234-4123-8123-123456789012",
            }
            return _to_envelope_or_none(
                _run_maybe_async(_hook_nudge_workflow_authoring_trampoline._handler(payload))
            )


def _fire_nudge_workflow_authoring_trampoline_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Skill", "tool_input": {"skill": "other-skill"}, "session_id": "sess-x"}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_nudge_workflow_authoring_trampoline._handler(payload))
    )


def _fire_offer_exploration_tier_dispatch() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "coordinator:executor", "prompt": "Find where the config is loaded."},
        "cwd": "/nonexistent-repo-root-xyz",
        "session_id": "12345678-1234-4123-8123-123456789012",
    }
    return _to_envelope_or_none(_hook_offer_exploration_tier_dispatch._handler(payload))


def _fire_offer_exploration_tier_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "coordinator:executor", "prompt": "Fix the bug in config loading."},
        "cwd": "/nonexistent-repo-root-xyz",
        "session_id": "12345678-1234-4123-8123-123456789012",
    }
    return _to_envelope_or_none(_hook_offer_exploration_tier_dispatch._handler(payload))


def _fire_postuse_stop_family_dispatch() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-psfd-", dir=_neutral_scratch_parent()) as scratch:
            repo_root = Path(scratch) / "coordinator-claude"
            repo_root.mkdir()
            (repo_root / ".coordinator-dev-repo").write_text("1")
            canonical_dir = repo_root / "setup" / "percolate-hooks"
            canonical_dir.mkdir(parents=True)
            canonical = canonical_dir / "percolate-store.yaml"
            canonical.write_text("a: 1")
            mp.setenv("CLAUDE_PLUGIN_ROOT", str(repo_root / "coordinator"))
            payload = {"tool_name": "Write", "tool_input": {"file_path": str(canonical)}}
            return _to_envelope_or_none(
                _run_maybe_async(_hook_postuse_stop_family_dispatch._handler(payload))
            )


def _fire_postuse_stop_family_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/nope-not-tracked.txt"}}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_postuse_stop_family_dispatch._handler(payload))
    )


def _fire_preuse_agent_dispatch() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "totally-bogus-nonexistent-role-zz"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_preuse_agent_dispatch._handler(payload)))


def _fire_preuse_agent_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Bash", "tool_input": {"command": "true"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_preuse_agent_dispatch._handler(payload)))


def _fire_preuse_bash_dispatch() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-pbd-", dir=_neutral_scratch_parent()) as scratch:
        os.makedirs(os.path.join(scratch, ".git"))
        (Path(scratch) / "coordinator.local.md").write_text("---\nsubagent_bash_policy: deny\n---\n")
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": scratch,
            "agent_id": "aexecutor-1234567890abcdef",
            "session_id": "sess-pbd",
        }
        return _to_envelope_or_none(_run_maybe_async(_hook_preuse_bash_dispatch._handler(payload)))


def _fire_preuse_bash_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Bash", "tool_input": {"command": "true"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_preuse_bash_dispatch._handler(payload)))


def _fire_preuse_skill_dispatch() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-psd-", dir=_neutral_scratch_parent()) as scratch:
            os.makedirs(os.path.join(scratch, ".git"))
            mp.setattr(
                __import__(
                    "coordinator_core.hooks.nudge_workflow_authoring_trampoline", fromlist=["show_toplevel"]
                ),
                "show_toplevel",
                lambda *a, **kw: scratch,
            )
            payload = {
                "tool_name": "Skill",
                "tool_input": {"skill": "workflow-authoring"},
                "session_id": "12345678-1234-4123-8123-123456789012",
            }
            return _to_envelope_or_none(_run_maybe_async(_hook_preuse_skill_dispatch._handler(payload)))


def _fire_preuse_skill_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Skill", "tool_input": {"skill": "other"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_preuse_skill_dispatch._handler(payload)))


def _fire_preuse_write_dispatch() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-pwd-", dir=_neutral_scratch_parent()) as scratch:
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "CLAUDE.md", "content": "x"},
            "agent_id": "deadbeef0123",
            "cwd": scratch,
        }
        return _to_envelope_or_none(_run_maybe_async(_hook_preuse_write_dispatch._handler(payload)))


def _fire_preuse_write_dispatch_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/nope-untracked.txt"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_preuse_write_dispatch._handler(payload)))


def _fire_project_orientation() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(_hook_project_orientation._handler({}))


def _fire_runtime_tripwire_em_check() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-rtec-", dir=_neutral_scratch_parent()) as scratch:
            os.makedirs(os.path.join(scratch, ".git"))
            mp.setattr(
                _hook_runtime_tripwire_em_check,
                "_check_push_failures",
                lambda git_root, session_id: "push failures detected: 3 in a row",
            )
            payload = {"payload": {"cwd": scratch, "session_id": "sess-rtec-probe"}}
            return _to_envelope_or_none(_hook_runtime_tripwire_em_check._handler(payload))


def _fire_runtime_tripwire_em_check_control() -> Optional[Dict[str, Any]]:
    payload = {
        "payload": {
            "cwd": "/tmp",
            "session_id": "sess-rtec-probe",
            "agent_id": "aexecutor-1234567890",
        }
    }
    return _to_envelope_or_none(_hook_runtime_tripwire_em_check._handler(payload))


def _fire_session_start_announce_job_mode() -> Optional[Dict[str, Any]]:
    payload = {"payload": {"session_id": "sess-x"}}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_session_start_announce_job_mode._handler(payload))
    )


def _fire_session_start_guard_plane_check() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ssgpc-", dir=_neutral_scratch_parent()) as scratch:
            mp.setenv("CLAUDE_CODE_REMOTE", "true")
            mp.setenv("CLAUDE_CONFIG_DIR", scratch)
            mp.delenv("CLAUDE_PROJECT_DIR", raising=False)
            return _to_envelope_or_none(
                _run_maybe_async(_hook_session_start_guard_plane_check._handler({}))
            )


def _fire_session_start_guard_plane_check_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("CLAUDE_CODE_REMOTE", raising=False)
        return _to_envelope_or_none(
            _run_maybe_async(_hook_session_start_guard_plane_check._handler({}))
        )


def _fire_sessionstart_async_dispatch() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_hook_sessionstart_ensure_http_forwarder, "_probe_bind_wins", lambda *a, **kw: None)
        mp.setenv("CLAUDE_PLUGIN_ROOT", "/nonexistent-plugin-root-xyz")
        return _to_envelope_or_none(
            _run_maybe_async(_hook_sessionstart_async_dispatch._handler({}))
        )


def _fire_sessionstart_async_dispatch_control() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_sessionstart_async_dispatch._handler({}))
    )


def _fire_sessionstart_bin_drift_refresh() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-sbdr-", dir=_neutral_scratch_parent()) as scratch:
            scratch_dir = Path(scratch)
            templates_bin = scratch_dir / "templates_bin"
            templates_bin.mkdir()
            (templates_bin / "foo.py").write_text("print('new')\n")
            bin_dir = scratch_dir / "bin"
            bin_dir.mkdir()
            (bin_dir / "foo.py").write_text("print('old')\n")
            import coordinator_core.hooks.support.bin_impl_drift as _bin_impl_drift

            mp.setattr(_bin_impl_drift, "_templates_bin", lambda: templates_bin)
            mp.setattr(_hook_sessionstart_bin_drift_refresh, "settings_home", lambda: scratch_dir)
            return _to_envelope_or_none(
                _run_maybe_async(_hook_sessionstart_bin_drift_refresh._handler({}))
            )


def _fire_sessionstart_bin_drift_refresh_control() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-sbdr-ctrl-", dir=_neutral_scratch_parent()) as scratch:
            scratch_dir = Path(scratch)
            (scratch_dir / "bin").mkdir()
            mp.setattr(_hook_sessionstart_bin_drift_refresh, "settings_home", lambda: scratch_dir)
            return _to_envelope_or_none(
                _run_maybe_async(_hook_sessionstart_bin_drift_refresh._handler({}))
            )


def _fire_sessionstart_dispatch() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            _ops_guard_hook_generation_self_probe, "run_self_probe", lambda config_dir: "self-probe advisory text"
        )
        mp.setattr(_hook_sessionstart_dispatch, "_guard_settings_integrity_handler", lambda payload: {})
        mp.setattr(_hook_sessionstart_dispatch, "_guard_hooks_kill_switch_detail_handler", lambda payload: {})
        return _to_envelope_or_none(_run_maybe_async(_hook_sessionstart_dispatch._handler({})))


def _fire_sessionstart_ensure_http_forwarder() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_hook_sessionstart_ensure_http_forwarder, "_probe_bind_wins", lambda *a, **kw: None)
        mp.setenv("CLAUDE_PLUGIN_ROOT", "/nonexistent-plugin-root-xyz")
        return _to_envelope_or_none(
            _run_maybe_async(_hook_sessionstart_ensure_http_forwarder._handler({}))
        )


def _fire_sessionstart_ensure_http_forwarder_control() -> Optional[Dict[str, Any]]:
    return _to_envelope_or_none(
        _run_maybe_async(_hook_sessionstart_ensure_http_forwarder._handler({}))
    )


def _fire_stop_dispatch() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-sd-", dir=_neutral_scratch_parent()) as scratch:
            os.makedirs(os.path.join(scratch, ".git"))
            mp.setenv("COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR", str(Path(scratch) / ".tally"))
            payload = {
                "payload": {
                    "session_id": "sess-%s" % uuid.uuid4().hex,
                    "cwd": scratch,
                    "stop_hook_active": False,
                    "last_assistant_message": "See test.py:42 and foo/bar.py:10 for details.",
                }
            }
            return _to_envelope_or_none(_run_maybe_async(_hook_stop_dispatch._handler(payload)))


def _fire_stop_dispatch_control() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-sd-ctrl-", dir=_neutral_scratch_parent()) as scratch:
        subprocess.run(["git", "init", "-q", scratch], check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "-C", scratch, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
            check=True,
            **no_console_passthrough_kwargs(),
        )
        payload = {
            "payload": {
                "session_id": "sess-%s" % uuid.uuid4().hex,
                "cwd": scratch,
                "stop_hook_active": False,
                "last_assistant_message": "Done.",
            }
        }
        return _to_envelope_or_none(_run_maybe_async(_hook_stop_dispatch._handler(payload)))


def _fire_strip_worktree_isolation() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Workflow", "tool_input": {"isolation": "worktree"}}
    return _to_envelope_or_none(_run_maybe_async(_hook_strip_worktree_isolation._handler(payload)))


def _fire_strip_worktree_isolation_control() -> Optional[Dict[str, Any]]:
    payload = {"tool_name": "Workflow", "tool_input": {}}
    return _to_envelope_or_none(_run_maybe_async(_hook_strip_worktree_isolation._handler(payload)))


def _fire_guard_config_change_hookstack_selfdefence() -> Optional[Dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ccshsd-", dir=_neutral_scratch_parent()) as scratch:
        settings_path = Path(scratch) / "settings.local.json"
        settings_path.write_text(_json.dumps({"disableAllHooks": True}), encoding="utf-8")
        payload = {"source": "local_settings", "file_path": str(settings_path)}
        return _to_envelope_or_none(
            _run_maybe_async(_hook_guard_config_change_hookstack_selfdefence._handler(payload))
        )


def _fire_guard_config_change_hookstack_selfdefence_control() -> Optional[Dict[str, Any]]:
    payload = {"source": "some_other_source", "file_path": "/nonexistent-xyz.json"}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_guard_config_change_hookstack_selfdefence._handler(payload))
    )


def _fire_nudge_cross_repo_cwd_boundary() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-ncrcb-", dir=_neutral_scratch_parent()) as scratch:
            sibling = Path(scratch) / "claude-klabauter"
            sibling.mkdir()
            mp.setenv("COORDINATOR_ENGINE_ROOT", str(sibling))
            payload = {
                "old_cwd": str(Path(scratch) / "elsewhere"),
                "new_cwd": str(sibling / "sub"),
            }
            return _to_envelope_or_none(
                _run_maybe_async(_hook_nudge_cross_repo_cwd_boundary._handler(payload))
            )


def _fire_nudge_cross_repo_cwd_boundary_control() -> Optional[Dict[str, Any]]:
    payload = {"old_cwd": "/tmp/same-place", "new_cwd": "/tmp/same-place"}
    return _to_envelope_or_none(
        _run_maybe_async(_hook_nudge_cross_repo_cwd_boundary._handler(payload))
    )


def _fire_postusefailure_cross_repo_memo_remediate() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Bash",
        "error": "Exit code 127: command not found",
        "tool_input": {"command": "cross-repo-memo send foo"},
    }
    return _to_envelope_or_none(
        _run_maybe_async(_hook_postusefailure_cross_repo_memo_remediate._handler(payload))
    )


def _fire_postusefailure_cross_repo_memo_remediate_control() -> Optional[Dict[str, Any]]:
    payload = {
        "tool_name": "Bash",
        "error": "Exit code 0",
        "tool_input": {"command": "cross-repo-memo send foo"},
    }
    return _to_envelope_or_none(
        _run_maybe_async(_hook_postusefailure_cross_repo_memo_remediate._handler(payload))
    )


def _fire_session_start_cloud_focus() -> Optional[Dict[str, Any]]:
    payload = {
        "env": {
            "CLAUDE_CODE_REMOTE": "true",
            "COORDINATOR_CLOUD_FOCUS_REPO": "someone/some-nonexistent-repo-xyz",
        }
    }
    return _to_envelope_or_none(_run_maybe_async(_hook_session_start_cloud_focus._handler(payload)))


def _fire_session_start_cloud_focus_control() -> Optional[Dict[str, Any]]:
    payload = {"env": {}}
    return _to_envelope_or_none(_run_maybe_async(_hook_session_start_cloud_focus._handler(payload)))


def _fire_session_start_watch_presence() -> Optional[Dict[str, Any]]:
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-sswp-", dir=_neutral_scratch_parent()) as scratch:
            settings_home_dir = Path(scratch) / "settings-home"
            repo_root = Path(scratch) / "repo"
            repo_root.mkdir()
            mp.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
            from coordinator_core.group_em import atomic_record as _atomic_record

            uhura_dir = settings_home_dir / "state" / "uhura"
            uhura_dir.mkdir(parents=True)
            record = {
                "schema_version": 1,
                "peer_name": "test-uhura-peer",
            }
            key = _atomic_record.repo_key(str(repo_root))
            (uhura_dir / f"{key}.json").write_text(_json.dumps(record), encoding="utf-8")
            payload = {"cwd": str(repo_root)}
            return _to_envelope_or_none(
                _run_maybe_async(_hook_session_start_watch_presence._handler(payload))
            )


def _fire_session_start_watch_presence_control() -> Optional[Dict[str, Any]]:
    # `read_liveness` always returns a non-empty `verdict` (even "absent" --
    # see its own docstring, "`absent` stays ONE verdict"), so a bare fresh
    # repo still speaks the watch line. The true no-op leg is the liveness
    # READ failing outright (`compute_context`'s own `except Exception: pass`
    # arm), forced here rather than relied on from repo state.
    with pytest.MonkeyPatch.context() as mp:
        with tempfile.TemporaryDirectory(prefix="guard-message-corpus-sswp-ctrl-", dir=_neutral_scratch_parent()) as scratch:
            settings_home_dir = Path(scratch) / "settings-home"
            repo_root = Path(scratch) / "repo"
            repo_root.mkdir()
            mp.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))

            def _raise(*_a, **_kw):
                raise RuntimeError("no liveness in this scratch fixture")

            mp.setattr(
                _hook_session_start_watch_presence.group_em_watch_heartbeat,
                "read_liveness",
                _raise,
            )
            payload = {"cwd": str(repo_root)}
            return _to_envelope_or_none(
                _run_maybe_async(_hook_session_start_watch_presence._handler(payload))
            )


HOOK_ROWS: List[HookRow] = [
    HookRow(
        "guard_config_change_hookstack_selfdefence",
        "fire-disable-all-hooks",
        True,
        _fire_guard_config_change_hookstack_selfdefence,
    ),
    HookRow(
        "guard_config_change_hookstack_selfdefence",
        "control",
        False,
        _fire_guard_config_change_hookstack_selfdefence_control,
    ),
    HookRow(
        "nudge_cross_repo_cwd_boundary",
        "fire-crosses-boundary",
        True,
        _fire_nudge_cross_repo_cwd_boundary,
    ),
    HookRow(
        "nudge_cross_repo_cwd_boundary",
        "control",
        False,
        _fire_nudge_cross_repo_cwd_boundary_control,
    ),
    HookRow(
        "postusefailure_cross_repo_memo_remediate",
        "fire-exit-127",
        True,
        _fire_postusefailure_cross_repo_memo_remediate,
    ),
    HookRow(
        "postusefailure_cross_repo_memo_remediate",
        "control",
        False,
        _fire_postusefailure_cross_repo_memo_remediate_control,
    ),
    HookRow(
        "session_start_cloud_focus",
        "fire-no-checkout-match",
        True,
        _fire_session_start_cloud_focus,
    ),
    HookRow(
        "session_start_cloud_focus",
        "control",
        False,
        _fire_session_start_cloud_focus_control,
    ),
    HookRow(
        "session_start_watch_presence",
        "fire-uhura-record",
        True,
        _fire_session_start_watch_presence,
    ),
    HookRow(
        "session_start_watch_presence",
        "control",
        False,
        _fire_session_start_watch_presence_control,
    ),
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
    HookRow("agent_completion_log", "noop-control", False, _fire_agent_completion_log_noop),
    HookRow(
        "agent_postuse_dispatch", "noop-control", False, _fire_agent_postuse_dispatch_noop
    ),
    HookRow(
        "cater_subagent_start",
        "fire-missing-provisioning",
        True,
        _fire_cater_subagent_start_missing_provisioning,
    ),
    HookRow("subagent_review_mark", "noop-control", False, _fire_subagent_review_mark_noop),
    HookRow(
        "plan_persistence_check",
        "fire-persisted-advisory",
        True,
        _fire_plan_persistence_check_persisted,
    ),
    HookRow(
        "plan_persistence_check",
        "control",
        False,
        _fire_plan_persistence_check_control,
    ),
    HookRow(
        "nudge_autonomous_askuserquestion",
        "fire-advisory",
        True,
        _fire_nudge_autonomous_askuserquestion,
    ),
    HookRow(
        "nudge_autonomous_askuserquestion",
        "control",
        False,
        _fire_nudge_autonomous_askuserquestion_control,
    ),
    HookRow(
        "watchdog_undischarged_next_move",
        "fire-stop-undischarged",
        True,
        _fire_watchdog_undischarged_next_move_stop,
    ),
    HookRow(
        "watchdog_undischarged_next_move",
        "control",
        False,
        _fire_watchdog_undischarged_next_move_control,
    ),
    HookRow(
        "block_unenumerated_agent_type",
        "fire-unenumerated-deny",
        True,
        _fire_block_unenumerated_agent_type,
    ),
    HookRow(
        "block_unenumerated_agent_type",
        "control-non-agent-tool",
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
    HookRow("flag_em_poll_in_flight", "fire-second-poll", True, _fire_flag_em_poll_in_flight),
    HookRow("flag_em_poll_in_flight", "control-intervening-tool", False, _fire_flag_em_poll_in_flight_control),
    HookRow("guard_terminal_review", "fire-unreviewed-code-commit", True, _fire_guard_terminal_review),
    HookRow("guard_terminal_review", "control-doc-only-commit", False, _fire_guard_terminal_review_control),
    HookRow("preuse_search_dispatch", "fire-first-grep", True, _fire_preuse_search_dispatch),
    HookRow("preuse_search_dispatch", "control-non-search-tool", False, _fire_preuse_search_dispatch_control),
    HookRow("example_retrieval_repo_detect", "fire-uninitialized", True, _fire_example_retrieval_repo_detect),
    HookRow("example_retrieval_repo_detect", "control-no-marker", False, _fire_example_retrieval_repo_detect_control),
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
    HookRow("allow_emitted_workflow_fire", "fire-verifying-receipt", True, _fire_allow_emitted_workflow_fire),
    HookRow("allow_emitted_workflow_fire", "control-no-receipt", False, _fire_allow_emitted_workflow_fire_control),
    HookRow("assert_em_role", "fire-always", True, _fire_assert_em_role),
    HookRow("block_dispatch_suite_invocation", "fire-suite-command", True, _fire_block_dispatch_suite_invocation),
    HookRow(
        "block_dispatch_suite_invocation",
        "control-non-suite",
        False,
        _fire_block_dispatch_suite_invocation_control,
    ),
    HookRow("block_ungranted_opus_subagent", "fire-ungranted-opus", True, _fire_block_ungranted_opus_subagent),
    HookRow(
        "block_ungranted_opus_subagent",
        "control-sonnet",
        False,
        _fire_block_ungranted_opus_subagent_control,
    ),
    HookRow("block_workflow_foreign_emission", "fire-sha-mismatch", True, _fire_block_workflow_foreign_emission),
    HookRow(
        "block_workflow_foreign_emission",
        "control-no-receipt",
        False,
        _fire_block_workflow_foreign_emission_control,
    ),
    HookRow("block_workflow_unmodeled_agent", "fire-zero-modeled", True, _fire_block_workflow_unmodeled_agent),
    HookRow(
        "block_workflow_unmodeled_agent",
        "control-modeled",
        False,
        _fire_block_workflow_unmodeled_agent_control,
    ),
    HookRow("block_worktree_tool", "fire-enter-worktree", True, _fire_block_worktree_tool),
    HookRow("block_worktree_tool", "control-exit-worktree", False, _fire_block_worktree_tool_control),
    HookRow("check_claude_md_size", "fire-admission-denied", True, _fire_check_claude_md_size),
    HookRow("check_claude_md_size", "control-small-write", False, _fire_check_claude_md_size_control),
    HookRow(
        "derive_global_doctrine_live_copy", "fire-tracked-write", True, _fire_derive_global_doctrine_live_copy
    ),
    HookRow(
        "derive_global_doctrine_live_copy",
        "control-unrelated-write",
        False,
        _fire_derive_global_doctrine_live_copy_control,
    ),
    HookRow("derive_setup_copies", "fire-canonical-write", True, _fire_derive_setup_copies),
    HookRow("derive_setup_copies", "control-unrelated-write", False, _fire_derive_setup_copies_control),
    HookRow("enforce_agent_dispatch_mode", "fire-illegal-name", True, _fire_enforce_agent_dispatch_mode),
    HookRow(
        "enforce_agent_dispatch_mode", "control-no-concern", False, _fire_enforce_agent_dispatch_mode_control
    ),
    HookRow("group_em_autofire", "fire-claimed", True, _fire_group_em_autofire),
    HookRow("group_em_autofire", "control-not-group-em", False, _fire_group_em_autofire_control),
    HookRow("guard_doctrine_changelog_prose", "fire-changelog-shaped", True, _fire_guard_doctrine_changelog_prose),
    HookRow(
        "guard_doctrine_changelog_prose",
        "control-present-tense",
        False,
        _fire_guard_doctrine_changelog_prose_control,
    ),
    HookRow(
        "guard_doctrine_surface_bash_write", "fire-bash-write", True, _fire_guard_doctrine_surface_bash_write
    ),
    HookRow(
        "guard_doctrine_surface_bash_write",
        "control-read-only",
        False,
        _fire_guard_doctrine_surface_bash_write_control,
    ),
    HookRow("guard_doctrine_surface_ratio", "fire-ratio-advisory", True, _fire_guard_doctrine_surface_ratio),
    HookRow(
        "guard_doctrine_surface_ratio",
        "control-under-floor",
        False,
        _fire_guard_doctrine_surface_ratio_control,
    ),
    HookRow(
        "guard_handoff_summary_cap_on_write", "fire-over-cap", True, _fire_guard_handoff_summary_cap_on_write
    ),
    HookRow(
        "guard_handoff_summary_cap_on_write",
        "control-under-cap",
        False,
        _fire_guard_handoff_summary_cap_on_write_control,
    ),
    HookRow(
        "guard_hook_generation_self_probe", "fire-probe-text", True, _fire_guard_hook_generation_self_probe
    ),
    HookRow(
        "guard_hook_generation_self_probe",
        "control-empty-probe",
        False,
        _fire_guard_hook_generation_self_probe_control,
    ),
    HookRow("guard_host_subagent_bash_ban", "fire-deny-policy", True, _fire_guard_host_subagent_bash_ban),
    HookRow(
        "guard_host_subagent_bash_ban",
        "control-no-policy",
        False,
        _fire_guard_host_subagent_bash_ban_control,
    ),
    HookRow(
        "guard_host_subagent_bash_spawn_shapes",
        "fire-deny-spawn-shape",
        True,
        _fire_guard_host_subagent_bash_spawn_shapes,
    ),
    HookRow(
        "guard_host_subagent_bash_spawn_shapes",
        "control-no-policy",
        False,
        _fire_guard_host_subagent_bash_spawn_shapes_control,
    ),
    HookRow("guard_kira_verdict_routed", "fire-eval-failure", True, _fire_guard_kira_verdict_routed),
    HookRow(
        "guard_kira_verdict_routed", "control-no-verdict", False, _fire_guard_kira_verdict_routed_control
    ),
    HookRow("guard_manufactured_blocker", "fire-declarative-stall", True, _fire_guard_manufactured_blocker),
    HookRow(
        "guard_manufactured_blocker",
        "control-real-completion",
        False,
        _fire_guard_manufactured_blocker_control,
    ),
    HookRow(
        "guard_named_dispatch_tool_restriction",
        "fire-named-explore",
        True,
        _fire_guard_named_dispatch_tool_restriction,
    ),
    HookRow(
        "guard_named_dispatch_tool_restriction",
        "control-unnamed-explore",
        False,
        _fire_guard_named_dispatch_tool_restriction_control,
    ),
    HookRow(
        "guard_posix_invocation_doctrine_write",
        "fire-posix-invocation",
        True,
        _fire_guard_posix_invocation_doctrine_write,
    ),
    HookRow(
        "guard_posix_invocation_doctrine_write",
        "control-plain-text",
        False,
        _fire_guard_posix_invocation_doctrine_write_control,
    ),
    HookRow("guard_python_syntax_on_write", "fire-syntax-error", True, _fire_guard_python_syntax_on_write),
    HookRow(
        "guard_python_syntax_on_write",
        "control-valid-syntax",
        False,
        _fire_guard_python_syntax_on_write_control,
    ),
    HookRow(
        "guard_repo_setup_claude_home_refusal",
        "fire-claude-home-target",
        True,
        _fire_guard_repo_setup_claude_home_refusal,
    ),
    HookRow(
        "guard_repo_setup_claude_home_refusal",
        "control-dry-run",
        False,
        _fire_guard_repo_setup_claude_home_refusal_control,
    ),
    HookRow(
        "guard_test_tree_git_fixture_spawn",
        "fire-fixture-construction",
        True,
        _fire_guard_test_tree_git_fixture_spawn,
    ),
    HookRow(
        "guard_test_tree_git_fixture_spawn",
        "control-no-git-call",
        False,
        _fire_guard_test_tree_git_fixture_spawn_control,
    ),
    HookRow("nudge_initiative_goals_ladder", "fire-no-goals", True, _fire_nudge_initiative_goals_ladder),
    HookRow(
        "nudge_initiative_goals_ladder",
        "control-goals-present",
        False,
        _fire_nudge_initiative_goals_ladder_control,
    ),
    HookRow("nudge_multiwave_workflow", "fire-burst-threshold", True, _fire_nudge_multiwave_workflow),
    HookRow(
        "nudge_multiwave_workflow", "control-explore-type", False, _fire_nudge_multiwave_workflow_control
    ),
    HookRow("nudge_plan_test_surface_tier", "fire-tier-fu", True, _fire_nudge_plan_test_surface_tier),
    HookRow(
        "nudge_plan_test_surface_tier",
        "control-tier-t",
        False,
        _fire_nudge_plan_test_surface_tier_control,
    ),
    HookRow(
        "nudge_workflow_authoring_trampoline",
        "fire-skill-open",
        True,
        _fire_nudge_workflow_authoring_trampoline,
    ),
    HookRow(
        "nudge_workflow_authoring_trampoline",
        "control-other-skill",
        False,
        _fire_nudge_workflow_authoring_trampoline_control,
    ),
    HookRow(
        "offer_exploration_tier_dispatch", "fire-read-only-shaped", True, _fire_offer_exploration_tier_dispatch
    ),
    HookRow(
        "offer_exploration_tier_dispatch",
        "control-write-shaped",
        False,
        _fire_offer_exploration_tier_dispatch_control,
    ),
    HookRow(
        "postuse_stop_family_dispatch", "fire-canonical-write", True, _fire_postuse_stop_family_dispatch
    ),
    HookRow(
        "postuse_stop_family_dispatch",
        "control-untracked-write",
        False,
        _fire_postuse_stop_family_dispatch_control,
    ),
    HookRow("preuse_agent_dispatch", "fire-unenumerated-type", True, _fire_preuse_agent_dispatch),
    HookRow("preuse_agent_dispatch", "control-empty-input", False, _fire_preuse_agent_dispatch_control),
    HookRow("preuse_bash_dispatch", "fire-host-ban", True, _fire_preuse_bash_dispatch),
    HookRow("preuse_bash_dispatch", "control-no-chain-hit", False, _fire_preuse_bash_dispatch_control),
    HookRow("preuse_skill_dispatch", "fire-trampoline-leg", True, _fire_preuse_skill_dispatch),
    HookRow("preuse_skill_dispatch", "control-unmatched-verb", False, _fire_preuse_skill_dispatch_control),
    HookRow("preuse_write_dispatch", "fire-claude-md-grant", True, _fire_preuse_write_dispatch),
    HookRow("preuse_write_dispatch", "control-untracked-write", False, _fire_preuse_write_dispatch_control),
    HookRow("project_orientation", "fire-always", True, _fire_project_orientation),
    HookRow("runtime_tripwire_em_check", "fire-push-failures", True, _fire_runtime_tripwire_em_check),
    HookRow(
        "runtime_tripwire_em_check", "control-subagent-gate", False, _fire_runtime_tripwire_em_check_control
    ),
    HookRow("session_start_announce_job_mode", "fire-always", True, _fire_session_start_announce_job_mode),
    HookRow(
        "session_start_guard_plane_check", "fire-remote-no-hooks", True, _fire_session_start_guard_plane_check
    ),
    HookRow(
        "session_start_guard_plane_check",
        "control-not-remote",
        False,
        _fire_session_start_guard_plane_check_control,
    ),
    HookRow("sessionstart_async_dispatch", "fire-forwarder-disclosure", True, _fire_sessionstart_async_dispatch),
    HookRow(
        "sessionstart_async_dispatch",
        "control-no-plugin-root",
        False,
        _fire_sessionstart_async_dispatch_control,
    ),
    HookRow("sessionstart_bin_drift_refresh", "fire-stale-bin", True, _fire_sessionstart_bin_drift_refresh),
    HookRow(
        "sessionstart_bin_drift_refresh",
        "control-no-drift",
        False,
        _fire_sessionstart_bin_drift_refresh_control,
    ),
    HookRow("sessionstart_dispatch", "fire-composed-legs", True, _fire_sessionstart_dispatch),
    HookRow(
        "sessionstart_ensure_http_forwarder", "fire-probe-undetermined", True, _fire_sessionstart_ensure_http_forwarder
    ),
    HookRow(
        "sessionstart_ensure_http_forwarder",
        "control-no-plugin-root",
        False,
        _fire_sessionstart_ensure_http_forwarder_control,
    ),
    HookRow("stop_dispatch", "fire-em-report-altitude", True, _fire_stop_dispatch),
    HookRow("stop_dispatch", "control-no-legs-fire", False, _fire_stop_dispatch_control),
    HookRow("strip_worktree_isolation", "fire-worktree-isolation", True, _fire_strip_worktree_isolation),
    HookRow(
        "strip_worktree_isolation", "control-no-isolation", False, _fire_strip_worktree_isolation_control
    ),
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
#       envelope round-trip, the shim relaying `response["result"]"). 16
#       further hooks/ modules carry ONLY this shape (grep for
#       `register_op(` under coordinator_core/hooks/, minus the four above):
#       `agent_completion_log`, `context_pressure_precompact`,
#       `coordinator_reminder`, `nudge_foreground_agent_dispatch`,
#       `nudge_named_agent_report_delivery`, `nudge_unauthorized_handoff`,
#       `postuse_advisory_dispatch`, `example_retrieval_repo_detect`,
#       `subagent_arrival_check`,
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
#       16-module population is real, uncovered DR-118-shim-relayed prose
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


@dataclass(frozen=True)
class StaticTextRow:

    package: str
    guard: str
    row_id: str
    text_fn: Callable[[], str]


def _fire_sandbox_check_usage_text() -> str:
    from coordinator_core.install import sandbox_check

    return sandbox_check._usage_text()


def _fire_emit_memo_schema_output() -> str:
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


def test_scratch_parent_is_outside_the_repo_and_is_exempt_here():
    """Both of `_neutral_scratch_parent`'s constraints, pinned together.

    Asserted as properties rather than against an expected literal: the correct
    value is different on every machine, and a literal would pin this box.

    THE SECOND LEG USED TO ASSERT "no user-home segment", AND THAT WAS AN
    UNSATISFIABLE CONJUNCTION -- retired 2026-08-26, not relaxed. Its stated
    rationale was B7: a rendered scratch path carrying the operator's username
    lands in agent-facing message text. That premise is wrong at the boundary
    it names. These paths are minted by a test fixture and read by a test
    process; no agent ever sees one. What a guard renders in a REAL session is
    the operator's own real path, which the guard is naming back on purpose --
    the class `test_no_machine_absolute_path_in_guard_messages.py`'s own
    exemption 2 already rules is "doing its job, not leaking a doc/root
    pointer". B7's subject is a path a guard RESOLVES AND HARDCODES into its
    own prose (the `_resolve_override_keys_doc_display` defect that ratchet
    exists for), never a path a fixture handed the guard as its input.

    And the conjunction had no solution to hold out for. Leg 1 needs a
    directory outside every git repo; the retired leg needed one with no
    user-home segment; the lint additionally matches `/var/...`, `/tmp/...`,
    and every drive-lettered Windows path unconditionally. On POSIX that
    leaves root-owned `/opt`-style roots, unwritable by default; on Windows
    every absolute path carries a drive letter, and `tempfile.gettempdir()`
    itself sits under the user home. A checkout at `~/X/claude-klabauter` --
    the fleet-normal shape -- fails the retired leg for a reason no code in
    this repo can fix. A pin whose only remedy is "move your checkout" pins
    the operator, not the constraint.

    What replaces it is the invariant that actually discharges leg 1's cost.
    Moving the scratch out of the repo (`d1cf0b986`) is what put fixture paths
    beyond the abs-path lint's `gettempdir()`-keyed fixture-echo exemption and
    put the operator's username inside every fired path for B7 proper --
    reddening both. `FIXTURE_SCRATCH_ROOTS` now lives beside the mint root so
    that cannot desync again, and this leg pins the one relationship that
    remains statable: the mint root is among the roots the lints exempt.
    """
    scratch = _NEUTRAL_SCRATCH_PARENT.resolve()
    repo_root = Path(__file__).resolve().parents[3]

    assert repo_root not in scratch.parents and scratch != repo_root, (
        "scratch parent %s is inside the repo at %s -- guard_inprocess_search's "
        "footer latch resolves by walking UPWARD for a .git entry, so every "
        "fixture tempdir minted here writes into the live "
        ".git/coordinator-sessions/ hub: a phantom session dir for the sites "
        "that mint their own CLAUDE_CODE_SESSION_ID, and a silent write under "
        "the real operator's id for the ones that leave it ambient."
        % (scratch, repo_root)
    )

    assert str(_NEUTRAL_SCRATCH_PARENT) in FIXTURE_SCRATCH_ROOTS, (
        "scratch parent %s is not among this module's own FIXTURE_SCRATCH_ROOTS "
        "%r, so every fixture path a guard correctly names back reads as a leak "
        "to both message lints. Whatever this constant becomes, that tuple has "
        "to name it." % (_NEUTRAL_SCRATCH_PARENT, FIXTURE_SCRATCH_ROOTS)
    )


def test_corpus_imports_cleanly_and_every_row_guard_resolves():
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
    non-firing cell to construct.

    `cater_subagent_start` (cluster-D grind, 2026-08-26) is the same
    exception for the same reason as `coordinator_reminder`: `compose_
    catering`'s role-framing leg is unconditional and LAST (module
    docstring, "role framing LAST and unconditional") -- verified live,
    even `params={}`/`repo_root=None` (nothing resolved, no sidecar, no
    provisioning) still composes the dispatched-worker-role preamble plus a
    "sidecar provisioning did not complete" note. The only quiet arm
    (`isinstance(payload, dict)` false) is unreachable through `_handler`
    without crashing it first (`field(params, ...)` dereferences `params`
    before `compose_catering` ever sees it), so there is no non-firing cell
    reachable through the real `_handler` entrypoint this row exercises.

    W4 landing wave (2026-09-19) adds four more exceptions, each verified
    live by reading every `return`/branch in the module:
      - `assert_em_role`: `_handler` always returns `context_only(...)`
        with a leading `"\\n"` seeded into `parts` before any per-entry
        logic runs -- no input makes it return `no_advisory()`.
      - `project_orientation`: `_handler` has no `no_advisory()` return at
        all (grep-verified) -- both its early-return and fall-through
        paths return `context_only(...)`, even when every per-banner `try`
        block above it silently no-ops.
      - `session_start_announce_job_mode`: fires `context_only(...)` on
        every path once `resolve_mode` imports cleanly (always true in
        this tree); the only `no_advisory()` legs are an unresolvable
        import or a `resolve_mode` exception, neither reachable from a
        synthetic non-firing payload without breaking the module itself.
      - `sessionstart_dispatch`: a fan-in whose own composed legs
        (`project_orientation` above, `guard_settings_integrity`/
        `guard_hooks_kill_switch_detail`) already speak unconditionally
        on this corpus's own real-repo-anchored run, so its own
        CONCATENATE-ALL aggregate has no reachable silent cell here
        either -- same shape as `cater_subagent_start`'s own fan-in
        exception above."""
    non_firing = {row.guard for row in HOOK_ROWS if not row.expected_speaker}
    expected = {row.guard for row in HOOK_ROWS} - {
        "nudge_unrouted_sizing",
        "coordinator_reminder",
        "cater_subagent_start",
        "assert_em_role",
        "project_orientation",
        "session_start_announce_job_mode",
        "sessionstart_dispatch",
    }
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
