"""Tests for coordinator_core.bash_guards._write_bump_message -- the
write-confinement speed bump's shared deny copy.

Spec backlink: pln-narrow-the-write-confinement-b-d10f79,
chunk C2. Covers AC3 (publish-class copy names the mirror's owner and never
"repos you don't own"), AC7 (every new/changed variant fits
`_message_size.MESSAGE_PROSE_CAP_BYTES`, measured by `measure_envelope` --
satisfied here via the explicit-assertion leg of AC7's OR, not a
`guard_message_corpus.py` firing row: the two live Bash dispatch guards
(`bash_guards/bump_foreign_repo_write.py`, `bash_guards/bump_outside_repo_write.py`,
corpus rows `bump-foreign-repo-write`/`bump-outside-repo-write`) are not yet
wired onto `destination_class` -- that wiring is this plan's C4/C5, a
separate atomic landing group -- so a genuine PUBLISH-class firing row
cannot exist through the real Bash-guard chain until they land; the
`Write`/`Edit`/`MultiEdit` tool surface, `write_guards/bump_out_of_repo_tool_write.py`,
already resolves and passes `destination_class`/`destination_owner` and is
unaffected by this gap), and AC15 (publish-class copy names the durable
source-side alternative, no PM-checking/ownership-violation vocabulary).

Also covers this chunk's own negative constraints (no violation/blocked/
denied vocabulary, no env-var/config bypass named) across all four
templates, and the pre-existing clear-line-is-executable-verbatim property
`render_bump_message`'s callers still depend on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_marker as marker
from coordinator_core.bash_guards import _write_bump_message as message
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES, measure_envelope


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


_TARGET_REPO = "coordinator-claude"
_SESSION_REPO = "claude-klabauter"
_SESSION_ID = "751ab9de-9319-4d63-b174-36145a4a3045"
_SANDBOX_ROOT = "state/subagent-share/751ab9de-9319-4d63-b174-36145a4a3045"
_MIRROR_OWNER = "claude-central-em"

_FORBIDDEN_WORDS = ("violation", "violated", "blocked", "denied", "deny", "denial")

#: Env-var/config-bypass vocabulary this suite's guards have historically
#: (and wrongly, per `_blanket_disarm.py`) advertised. Case-sensitive on the
#: env-var-shaped tokens; the sentence-level phrase check is case-insensitive.
_BYPASS_TOKENS = ("DISARM", "BYPASS", "SKIP_GUARD", "NO_VERIFY")


def _clear_command(text: str) -> str:
    """Pull the `touch ...` clear line back out of a rendered message. The
    line is now INDENTED (two leading spaces, see module docstring's
    "BUDGET" mechanic 3) -- callers must strip before matching, unlike the
    incumbent's column-0 line."""
    return next(
        line.strip() for line in text.split("\n") if line.strip().startswith("touch ")
    )


# ---------------------------------------------------------------------------
# FOREIGN-class copy -- names the target repo, session repo, memo channel
# ---------------------------------------------------------------------------


def test_em_message_names_target_repo(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert _TARGET_REPO in text
    assert _SESSION_REPO in text


def test_em_message_names_memo_channel(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert "cross-repo-memo" in text


def test_em_message_never_contains_the_clear_line(tmp_path):
    """Inverted 2026-08-13 (C4d, docs/plans/2026-08-13-guard-messages-stop-
    handing-agents-the-keys.md AC-2): the EM channel used to carry this
    line verbatim -- a fully-parameterized, ready-to-paste `touch
    allow-xrepo-write-<session-id>` recipe is exactly what AC-1/AC-2
    forbid for either audience, EM included. This test used to assert the
    line PRESENT; it now asserts it ABSENT, the same property
    `test_subagent_message_never_contains_the_clear_line` already pinned
    for the subagent channel."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    forbidden_line = marker.clear_line(gitdir, _SESSION_ID)
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert forbidden_line not in text


def test_subagent_message_names_target_repo(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_subagent_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    assert _TARGET_REPO in text
    assert _SESSION_REPO in text


def test_subagent_message_names_sandbox_route(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_subagent_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    assert _SANDBOX_ROOT in text


def test_subagent_message_never_contains_the_clear_line(tmp_path):
    """SUBAGENT CHANNEL NEVER CARRIES THE UNLOCK (module docstring) -- the
    subagent-class renderer must not carry the marker/`touch`/session-id
    unlock recipe the EM-class renderer does."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    forbidden_line = marker.clear_line(gitdir, _SESSION_ID)
    text = message.render_subagent_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    assert forbidden_line not in text


# ---------------------------------------------------------------------------
# Agent-class copy -- reads correctly for every agent class that can receive it
# ---------------------------------------------------------------------------


def test_em_message_addresses_the_pm_not_a_dispatching_em(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert "check with your PM" in text
    # Wrong-class failure mode named by the 2026-08-02 executor-confinement
    # lesson: an EM has no dispatching EM to report back to.
    assert "dispatched you" not in text


def test_subagent_message_addresses_the_dispatching_em_not_a_pm(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_subagent_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    assert "no PM here" in text
    assert "the EM that dispatched you" in text
    # Wrong-class failure mode: a subagent's message must not instruct it to
    # go check with a PM it does not have.
    assert "check with your PM" not in text


def test_render_bump_message_dispatches_em_class(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_bump_message(
        agent_class=message.AGENT_CLASS_EM,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
    )
    assert "check with your PM" in text


def test_render_bump_message_dispatches_subagent_class(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_bump_message(
        agent_class=message.AGENT_CLASS_SUBAGENT,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
        sandbox_root=_SANDBOX_ROOT,
    )
    assert "the EM that dispatched you" in text


def test_render_bump_message_defaults_to_foreign_destination_class(tmp_path):
    """Every existing call site (`bump_foreign_repo_write.py`,
    `bump_outside_repo_write.py`, `bump_out_of_repo_tool_write.py`) calls
    `render_bump_message` with no `destination_class` keyword -- the default
    must keep producing today's FOREIGN-class copy unchanged."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_bump_message(
        agent_class=message.AGENT_CLASS_EM,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
    )
    assert text == message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)


# ---------------------------------------------------------------------------
# resolve_agent_class
# ---------------------------------------------------------------------------


# Review: staff-eng (AC-3) -- resolve_agent_class was fail-open to EM-class
# on an empty/missing payload or git_root; AC-3 names it alongside
# annotate_deny and the dispatch seam for inversion. Tests below follow the
# corrected production behaviour: unresolvable audience now degrades to
# subagent-class (the safer template), not EM-class.


def test_resolve_agent_class_empty_payload_is_subagent_class():
    # An empty/unresolvable payload can no longer positively resolve EM --
    # it degrades to the safer subagent-class template.
    assert message.resolve_agent_class({}, None) == message.AGENT_CLASS_SUBAGENT


def test_resolve_agent_class_no_agent_id_with_real_envelope_is_em_class(tmp_path):
    root = _init_repo(tmp_path)
    payload = {"session_id": "s1"}
    assert message.resolve_agent_class(payload, str(root)) == message.AGENT_CLASS_EM


def test_resolve_agent_class_bare_hex_agent_id_is_subagent_class():
    payload = {"agent_id": "a" * 16}
    assert message.resolve_agent_class(payload, None) == message.AGENT_CLASS_SUBAGENT


def test_resolve_agent_class_never_raises_on_missing_payload():
    # Fail-open defensive posture (see module docstring): None/empty inputs
    # degrade to subagent-class -- the safer template -- rather than
    # raising or resolving EM.
    assert message.resolve_agent_class(None, None) == message.AGENT_CLASS_SUBAGENT


# ---------------------------------------------------------------------------
# AC15 / PM ruling -- publish-class copy is durability-framed, owner-aware
# ---------------------------------------------------------------------------


def test_publish_em_message_names_the_mirror_and_owner(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_publish_em_message(_TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID)
    assert _TARGET_REPO in text
    assert _MIRROR_OWNER in text


def test_publish_subagent_message_names_the_mirror_and_owner(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_publish_subagent_message(
        _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    assert _TARGET_REPO in text
    assert _MIRROR_OWNER in text


@pytest.mark.parametrize(
    "text_fn",
    [
        lambda gitdir: message.render_publish_em_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID
        ),
        lambda gitdir: message.render_publish_subagent_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT
        ),
    ],
)
def test_publish_copy_names_durable_source_side_alternative(tmp_path, text_fn):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = text_fn(gitdir)
    assert "plugin-extraction-and-distribution.md" in text
    assert "Publish-Repo Content Authoring" in text


@pytest.mark.parametrize(
    "text_fn",
    [
        lambda gitdir: message.render_publish_em_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID
        ),
        lambda gitdir: message.render_publish_subagent_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT
        ),
    ],
)
def test_publish_copy_never_says_repos_you_dont_own(tmp_path, text_fn):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = text_fn(gitdir)
    assert "repos you don't own" not in text


@pytest.mark.parametrize(
    "text_fn",
    [
        lambda gitdir: message.render_publish_em_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID
        ),
        lambda gitdir: message.render_publish_subagent_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT
        ),
    ],
)
def test_publish_copy_has_no_pm_checking_or_memo_pointer(tmp_path, text_fn):
    """AC15 / the PM ruling: publishing into a mirror is not forbidden, so
    the copy must never send the reader to a PM or cross-repo-memo -- both
    of those are FOREIGN-class-only vocabulary."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = text_fn(gitdir)
    assert "PM" not in text
    assert "cross-repo-memo" not in text


def test_render_bump_message_dispatches_publish_em_class(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_bump_message(
        agent_class=message.AGENT_CLASS_EM,
        destination_class=message.DESTINATION_PUBLISH,
        destination_owner=_MIRROR_OWNER,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
    )
    assert text == message.render_publish_em_message(_TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID)


def test_render_bump_message_dispatches_publish_subagent_class(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_bump_message(
        agent_class=message.AGENT_CLASS_SUBAGENT,
        destination_class=message.DESTINATION_PUBLISH,
        destination_owner=_MIRROR_OWNER,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
        sandbox_root=_SANDBOX_ROOT,
    )
    assert text == message.render_publish_subagent_message(
        _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )


def test_destination_class_constants_are_the_only_two_values():
    assert {message.DESTINATION_FOREIGN, message.DESTINATION_PUBLISH} == {"foreign", "publish"}


# ---------------------------------------------------------------------------
# AC7 -- every one of the four templates fits MESSAGE_PROSE_CAP_BYTES (220
# BYTES), measured by measure_envelope, satisfied via the explicit-assertion
# leg of AC7's OR (see module docstring for why a corpus firing row cannot
# exist for the PUBLISH class until this plan's C4/C5 lands).
# ---------------------------------------------------------------------------


def _measure(text: str):
    envelope = {"hookSpecificOutput": {"permissionDecisionReason": text}}
    return measure_envelope(envelope)


#: R1 (docs/plans/2026-08-08-the-bump-message-never-showed-the-operat.md)
#: added a `raw_target`-populated branch to every template, rendering BOTH
#: the raw token and the resolved path -- strictly longer prose than the
#: pre-R1 short branch. A realistically long MSYS-spelled absolute path,
#: not a three-character stub -- a stub would pass while the real
#: production shape (an operator-typed `/x/claude-klabauter/...` or
#: `/c/Users/...` token) fails.
_LONG_RAW_MSYS_TOKEN = "/c/Users/example-operator/AppData/Local/Temp/claude/X--claude-klabauter/foreign-scratch-target.txt"
_LONG_RESOLVED_NATIVE_PATH = (
    r"C:\Users\example-operator\AppData\Local\Temp\claude\X--claude-klabauter\foreign-scratch-target.txt"
)


@pytest.mark.parametrize(
    "label,text_fn",
    [
        (
            "foreign-em",
            lambda gitdir: message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID),
        ),
        (
            "foreign-subagent",
            lambda gitdir: message.render_subagent_message(
                _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
            ),
        ),
        (
            "publish-em",
            lambda gitdir: message.render_publish_em_message(
                _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID
            ),
        ),
        (
            "publish-subagent",
            lambda gitdir: message.render_publish_subagent_message(
                _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT
            ),
        ),
        (
            "foreign-em-raw-target",
            lambda gitdir: message.render_em_message(
                _LONG_RESOLVED_NATIVE_PATH,
                _SESSION_REPO,
                gitdir,
                _SESSION_ID,
                raw_target=_LONG_RAW_MSYS_TOKEN,
            ),
        ),
        (
            "foreign-subagent-raw-target",
            lambda gitdir: message.render_subagent_message(
                _LONG_RESOLVED_NATIVE_PATH,
                _SESSION_REPO,
                gitdir,
                _SESSION_ID,
                _SANDBOX_ROOT,
                raw_target=_LONG_RAW_MSYS_TOKEN,
            ),
        ),
        (
            "publish-em-raw-target",
            lambda gitdir: message.render_publish_em_message(
                _LONG_RESOLVED_NATIVE_PATH,
                _MIRROR_OWNER,
                gitdir,
                _SESSION_ID,
                raw_target=_LONG_RAW_MSYS_TOKEN,
            ),
        ),
        (
            "publish-subagent-raw-target",
            lambda gitdir: message.render_publish_subagent_message(
                _LONG_RESOLVED_NATIVE_PATH,
                _MIRROR_OWNER,
                gitdir,
                _SESSION_ID,
                _SANDBOX_ROOT,
                raw_target=_LONG_RAW_MSYS_TOKEN,
            ),
        ),
    ],
)
def test_every_variant_fits_the_message_prose_cap_bytes(tmp_path, label, text_fn):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = text_fn(gitdir)
    measurement = _measure(text)
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES, (
        "%s: prose_bytes=%d exceeds cap=%d"
        % (label, measurement.prose_bytes, MESSAGE_PROSE_CAP_BYTES)
    )
    assert measurement.over_cap is False, label


# ---------------------------------------------------------------------------
# AC15 -- the emitted clear line is executable verbatim
# ---------------------------------------------------------------------------


def test_em_message_carries_no_executable_clear_recipe(tmp_path):
    """Inverted 2026-08-13 (C4d): this test used to prove the EM channel's
    `touch` line executes verbatim to clear the bump -- that pasteable
    recipe is gone from the EM channel entirely now (AC-2), so the
    property to pin is its absence, not its executability."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert "touch " not in text
    assert marker.MARKER_PREFIX not in text


def test_publish_em_message_carries_no_executable_clear_recipe(tmp_path):
    """Inverted 2026-08-13 (C4d) -- same property as
    `test_em_message_carries_no_executable_clear_recipe`, for the PUBLISH
    class EM template."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_publish_em_message(_TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID)
    assert "touch " not in text
    assert marker.MARKER_PREFIX not in text


def test_clear_line_never_rendered_even_on_a_machine_where_none_has_ever_existed(tmp_path):
    """Inverted 2026-08-13 (C4d): AC15's original framing (a machine with
    no prior marker file at all) used to prove the clear line rendered
    correctly in that state; the clear line is gone from the EM channel
    entirely now, so the property is that it never renders, regardless of
    on-disk marker state."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    assert list(gitdir.iterdir()) == [
        e for e in gitdir.iterdir() if not e.name.startswith(marker.MARKER_PREFIX)
    ]
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert marker.clear_line(gitdir, _SESSION_ID) not in text


# ---------------------------------------------------------------------------
# SUBAGENT CHANNEL NEVER CARRIES THE UNLOCK (module docstring) -- pins the
# 2026-08-13 fix for the class named in cross-repo/inbox/2026-08-13-project-
# cockpit-em-guard-advisories-read-as-injection-to-subagents.md, mirroring
# state/audits/2026-08-11-guard-text-injection-mechanism-proof.md's fix on
# the neighbouring emit leg. Property over BOTH subagent renderers, every
# surface, every destination class, and (for the FOREIGN class) with and
# without a populated raw_target -- not a single string comparison, so a
# future edit cannot quietly reintroduce the button.
# ---------------------------------------------------------------------------


def _subagent_variant_text_fns():
    return [
        ("foreign-subagent", lambda gitdir: message.render_subagent_message(
            _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
        )),
        ("foreign-subagent-tool-surface", lambda gitdir: message.render_subagent_message(
            _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT,
            surface=message.SURFACE_TOOL,
        )),
        ("foreign-subagent-raw-target", lambda gitdir: message.render_subagent_message(
            _LONG_RESOLVED_NATIVE_PATH, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT,
            raw_target=_LONG_RAW_MSYS_TOKEN,
        )),
        ("publish-subagent", lambda gitdir: message.render_publish_subagent_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT
        )),
        ("publish-subagent-tool-surface", lambda gitdir: message.render_publish_subagent_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT,
            surface=message.SURFACE_TOOL,
        )),
        ("publish-subagent-raw-target", lambda gitdir: message.render_publish_subagent_message(
            _LONG_RESOLVED_NATIVE_PATH, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT,
            raw_target=_LONG_RAW_MSYS_TOKEN,
        )),
    ]


@pytest.mark.parametrize("label,text_fn", _subagent_variant_text_fns())
def test_subagent_renderers_never_carry_the_unlock_mechanism(tmp_path, label, text_fn):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = text_fn(gitdir)
    assert marker.MARKER_PREFIX not in text, label
    assert "allow-xrepo-write-" not in text, label
    assert "touch " not in text, label


@pytest.mark.parametrize("label,text_fn", _subagent_variant_text_fns())
def test_subagent_renderers_via_dispatch_never_carry_the_unlock_mechanism(tmp_path, label, text_fn):
    """Same property, reached through `render_bump_message`'s single entry
    point -- the seam every real call site actually uses."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    is_publish = label.startswith("publish")
    text = message.render_bump_message(
        agent_class=message.AGENT_CLASS_SUBAGENT,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
        sandbox_root=_SANDBOX_ROOT,
        destination_class=message.DESTINATION_PUBLISH if is_publish else message.DESTINATION_FOREIGN,
        destination_owner=_MIRROR_OWNER if is_publish else "",
    )
    assert marker.MARKER_PREFIX not in text
    assert "allow-xrepo-write-" not in text
    assert "touch " not in text


# ---------------------------------------------------------------------------
# Negative constraints -- no violation/blocked/denied vocabulary, no bypass
# ---------------------------------------------------------------------------


def _all_variant_text_fns():
    return [
        lambda gitdir: message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID),
        lambda gitdir: message.render_subagent_message(
            _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
        ),
        lambda gitdir: message.render_publish_em_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID
        ),
        lambda gitdir: message.render_publish_subagent_message(
            _TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID, _SANDBOX_ROOT
        ),
    ]


@pytest.mark.parametrize("text_fn", _all_variant_text_fns())
def test_no_violation_blocked_denied_vocabulary(tmp_path, text_fn):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    # Exclude the indented `touch <path>` line: the path embeds pytest's own
    # tmp_path, which can incidentally contain a forbidden substring (e.g.
    # this test's own node id) -- the vocabulary check is about the PROSE,
    # not a path a test harness happened to generate.
    prose = "\n".join(
        line for line in text_fn(gitdir).split("\n") if not line.strip().startswith("touch ")
    ).lower()
    for word in _FORBIDDEN_WORDS:
        assert word not in prose, f"forbidden vocabulary {word!r} found in message copy"


@pytest.mark.parametrize("text_fn", _all_variant_text_fns())
def test_no_env_var_or_config_bypass_named(tmp_path, text_fn):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    # Same tmp_path-collision rationale as test_no_violation_blocked_denied_vocabulary.
    prose = "\n".join(
        line for line in text_fn(gitdir).split("\n") if not line.strip().startswith("touch ")
    ).upper()
    for token in _BYPASS_TOKENS:
        assert token not in prose, f"bypass-shaped token {token!r} found in message copy"


# ---------------------------------------------------------------------------
# Module docstring / negative-spec pin -- both axes stay module-level
# constants of behaviour, not ad hoc per-call composition
# ---------------------------------------------------------------------------


def test_agent_class_constants_are_the_only_two_values():
    assert {message.AGENT_CLASS_EM, message.AGENT_CLASS_SUBAGENT} == {"em", "subagent"}


# ---------------------------------------------------------------------------
# Self-attribution -- pins the fix for a named prior incident (a dispatched
# executor once read this guard's own message as prompt injection and
# ignored it; see module docstring, "SELF-ATTRIBUTION") against silent
# regression by a future byte-driven rewrite of any of the four templates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text_fn", _all_variant_text_fns())
def test_every_variant_self_attributes_as_coordinator_guard(tmp_path, text_fn):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = text_fn(gitdir)
    assert text.startswith("Coordinator guard"), (
        "self-attribution missing/moved -- a reader could mistake this "
        "message for prompt injection lifted from the write target's own "
        "content (see module docstring, SELF-ATTRIBUTION)"
    )


# ---------------------------------------------------------------------------
# R1 (docs/plans/2026-08-08-the-bump-message-never-showed-the-operat.md) --
# the raw pre-translation token, shown distinguishably from the resolved
# path, and never printed twice when the two are identical.
# ---------------------------------------------------------------------------

_RAW_MSYS_TOKEN = "/c/Users/example-operator/foreign/scratch.txt"
_RESOLVED_NATIVE_PATH = r"C:\Users\example-operator\foreign\scratch.txt"


def test_em_message_shows_both_raw_and_resolved_when_they_differ(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(
        _RESOLVED_NATIVE_PATH, _SESSION_REPO, gitdir, _SESSION_ID, raw_target=_RAW_MSYS_TOKEN
    )
    assert f"`{_RAW_MSYS_TOKEN}`" in text
    assert f"`{_RESOLVED_NATIVE_PATH}`" in text


def test_em_message_does_not_double_print_when_raw_equals_resolved(tmp_path):
    """AC2's own named failure mode inverted: when a caller has determined
    raw and resolved are the SAME value, it passes `raw_target=""` (the
    default) rather than the resolved value twice -- the message must then
    degrade to the single pre-R1 form, byte-identical to no `raw_target` at
    all."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    with_default = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    with_empty_raw = message.render_em_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, raw_target=""
    )
    assert with_default == with_empty_raw
    assert with_default.count(f"`{_TARGET_REPO}`") == 1


def test_subagent_message_shows_both_raw_and_resolved_when_they_differ(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_subagent_message(
        _RESOLVED_NATIVE_PATH,
        _SESSION_REPO,
        gitdir,
        _SESSION_ID,
        _SANDBOX_ROOT,
        raw_target=_RAW_MSYS_TOKEN,
    )
    assert f"`{_RAW_MSYS_TOKEN}`" in text
    assert f"`{_RESOLVED_NATIVE_PATH}`" in text


@pytest.mark.parametrize(
    "text_fn",
    [
        lambda gitdir: message.render_publish_em_message(
            _RESOLVED_NATIVE_PATH, _MIRROR_OWNER, gitdir, _SESSION_ID, raw_target=_RAW_MSYS_TOKEN
        ),
        lambda gitdir: message.render_publish_subagent_message(
            _RESOLVED_NATIVE_PATH,
            _MIRROR_OWNER,
            gitdir,
            _SESSION_ID,
            _SANDBOX_ROOT,
            raw_target=_RAW_MSYS_TOKEN,
        ),
    ],
)
def test_publish_variants_show_both_raw_and_resolved_when_they_differ(tmp_path, text_fn):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = text_fn(gitdir)
    assert f"`{_RAW_MSYS_TOKEN}`" in text
    assert f"`{_RESOLVED_NATIVE_PATH}`" in text


def test_render_bump_message_threads_raw_target_through_to_em_template(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_bump_message(
        agent_class=message.AGENT_CLASS_EM,
        target_repo=_RESOLVED_NATIVE_PATH,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
        raw_target=_RAW_MSYS_TOKEN,
    )
    assert text == message.render_em_message(
        _RESOLVED_NATIVE_PATH, _SESSION_REPO, gitdir, _SESSION_ID, raw_target=_RAW_MSYS_TOKEN
    )


def test_render_bump_message_default_raw_target_is_backward_compatible(tmp_path):
    """AC4: `raw_target` is optional with a safe default -- every pre-R1
    caller (which never passes it) keeps rendering byte-identical output."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    with_kwarg_omitted = message.render_bump_message(
        agent_class=message.AGENT_CLASS_EM,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
    )
    with_explicit_empty = message.render_bump_message(
        agent_class=message.AGENT_CLASS_EM,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
        raw_target="",
    )
    assert with_kwarg_omitted == with_explicit_empty


# ---------------------------------------------------------------------------
# `surface` axis (chunk 3, state/bug-backlog/2026-08-10-cross-repo-write-
# boundary-denies-on-bash-b6fd16ed9ab9.yaml) -- the clear-line offer must
# describe its ACTUAL scope, which diverges between the two Bash guards
# (per-target marker) and the tool surface (session-wide marker).
# ---------------------------------------------------------------------------


def test_default_surface_renders_byte_identical_bash_copy(tmp_path):
    """`surface` defaults to `SURFACE_BASH` -- both pre-chunk-3 phrases this
    test used to assert are gone from the EM channel entirely (2026-08-13,
    C4d, AC-2: the clear-offer phrase only ever appeared alongside the now-
    removed `touch` recipe -- neither surface's wording renders here any
    more)."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert "clear this target" not in text
    assert "session-wide" not in text


def test_surface_no_longer_changes_em_rendering(tmp_path):
    """Inverted 2026-08-13 (C4d): the tool surface's session-wide clear
    offer this test used to assert is gone along with the whole clear
    recipe (AC-2) -- `surface` is accepted for call-site parity only
    (module docstring) and no longer changes the EM template's rendered
    text at all."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text_tool = message.render_em_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, surface=message.SURFACE_TOOL
    )
    text_bash = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert text_tool == text_bash
    assert "session-wide" not in text_tool
    assert "clear this target" not in text_tool


@pytest.mark.parametrize(
    "render_fn,args,is_subagent",
    [
        (message.render_em_message, (_TARGET_REPO, _SESSION_REPO), False),
        (message.render_subagent_message, (_TARGET_REPO, _SESSION_REPO), True),
        # Review: coordinator:code-reviewer (P2) -- the PUBLISH-class
        # renderers were entirely absent from this parametrize, despite
        # being reachable under `surface=SURFACE_TOOL` from
        # `bump_out_of_repo_tool_write.py`'s own real call site
        # (`destination_class=DESTINATION_PUBLISH`, always `surface=
        # SURFACE_TOOL`).
        (message.render_publish_em_message, (_TARGET_REPO, _MIRROR_OWNER), False),
        (message.render_publish_subagent_message, (_TARGET_REPO, _MIRROR_OWNER), True),
    ],
)
def test_surface_tool_variants_fit_the_prose_cap(tmp_path, render_fn, args, is_subagent):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    if is_subagent:
        text = render_fn(*args, gitdir, _SESSION_ID, _SANDBOX_ROOT, surface=message.SURFACE_TOOL)
    else:
        text = render_fn(*args, gitdir, _SESSION_ID, surface=message.SURFACE_TOOL)
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": text,
        }
    }
    measurement = measure_envelope(envelope)
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES, (
        text,
        measurement.prose_bytes,
    )


@pytest.mark.parametrize(
    "render_fn,args,is_subagent",
    [
        (
            message.render_em_message,
            (_LONG_RESOLVED_NATIVE_PATH, _SESSION_REPO),
            False,
        ),
        (
            message.render_subagent_message,
            (_LONG_RESOLVED_NATIVE_PATH, _SESSION_REPO),
            True,
        ),
        (
            message.render_publish_em_message,
            (_LONG_RESOLVED_NATIVE_PATH, _MIRROR_OWNER),
            False,
        ),
        (
            message.render_publish_subagent_message,
            (_LONG_RESOLVED_NATIVE_PATH, _MIRROR_OWNER),
            True,
        ),
    ],
)
def test_surface_tool_variants_with_long_raw_target_fit_the_prose_cap(
    tmp_path, render_fn, args, is_subagent
):
    """Review: coordinator:code-reviewer (P2) -- `SURFACE_TOOL`'s clear-offer
    phrase ("stand the boundary down, session-wide", 39 bytes) is +21 bytes
    over `SURFACE_BASH`'s ("clear this target", 18 bytes), and the pre-
    existing `test_every_variant_fits_the_message_prose_cap_bytes` proves the
    `raw_target`-populated variants are the ones that stress the 220-byte cap
    hardest. Neither gap (PUBLISH renderers, nor a populated `raw_target`)
    was exercised under `SURFACE_TOOL` before this test; both are reachable
    together from `bump_out_of_repo_tool_write.py`'s real call site, which
    always threads a real `raw_target=file_path`."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    if is_subagent:
        text = render_fn(
            *args,
            gitdir,
            _SESSION_ID,
            _SANDBOX_ROOT,
            raw_target=_LONG_RAW_MSYS_TOKEN,
            surface=message.SURFACE_TOOL,
        )
    else:
        text = render_fn(
            *args,
            gitdir,
            _SESSION_ID,
            raw_target=_LONG_RAW_MSYS_TOKEN,
            surface=message.SURFACE_TOOL,
        )
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": text,
        }
    }
    measurement = measure_envelope(envelope)
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES, (
        text,
        measurement.prose_bytes,
    )


def test_render_bump_message_threads_surface_to_the_foreign_em_template(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_bump_message(
        agent_class=message.AGENT_CLASS_EM,
        target_repo=_TARGET_REPO,
        session_repo=_SESSION_REPO,
        gitdir=gitdir,
        session_id=_SESSION_ID,
        surface=message.SURFACE_TOOL,
    )
    assert text == message.render_em_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, surface=message.SURFACE_TOOL
    )
