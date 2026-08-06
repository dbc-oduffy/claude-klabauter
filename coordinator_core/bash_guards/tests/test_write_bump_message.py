"""Tests for coordinator_core.bash_guards._write_bump_message -- the
write-confinement speed bump's shared deny copy.

Spec backlink: docs/plans/2026-08-03-narrow-write-confinement-bump.md,
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


_TARGET_REPO = "example-doctrine-repo"
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


def test_em_message_contains_exact_clear_line(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    expected_line = marker.clear_line(gitdir, _SESSION_ID)
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert expected_line in text


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


def test_subagent_message_contains_exact_clear_line(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    expected_line = marker.clear_line(gitdir, _SESSION_ID)
    text = message.render_subagent_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    assert expected_line in text


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


def test_resolve_agent_class_no_agent_id_is_em_class():
    assert message.resolve_agent_class({}, None) == message.AGENT_CLASS_EM


def test_resolve_agent_class_bare_hex_agent_id_is_subagent_class():
    payload = {"agent_id": "a" * 16}
    assert message.resolve_agent_class(payload, None) == message.AGENT_CLASS_SUBAGENT


def test_resolve_agent_class_never_raises_on_missing_payload():
    # Fail-open defensive posture (see module docstring): None/empty inputs
    # degrade to EM-class rather than raising.
    assert message.resolve_agent_class(None, None) == message.AGENT_CLASS_EM


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


def test_em_clear_line_is_executable_verbatim(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    touch_path = _clear_command(text)[len("touch "):]

    assert not marker.marker_present(gitdir, _SESSION_ID)
    subprocess.run(["touch", touch_path], check=True)
    assert marker.marker_present(gitdir, _SESSION_ID)


def test_subagent_clear_line_is_executable_verbatim(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_subagent_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    touch_path = _clear_command(text)[len("touch "):]

    assert not marker.marker_present(gitdir, _SESSION_ID)
    subprocess.run(["touch", touch_path], check=True)
    assert marker.marker_present(gitdir, _SESSION_ID)


def test_publish_em_clear_line_is_executable_verbatim(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_publish_em_message(_TARGET_REPO, _MIRROR_OWNER, gitdir, _SESSION_ID)
    touch_path = _clear_command(text)[len("touch "):]

    assert not marker.marker_present(gitdir, _SESSION_ID)
    subprocess.run(["touch", touch_path], check=True)
    assert marker.marker_present(gitdir, _SESSION_ID)


def test_clear_line_matches_a_machine_where_none_has_ever_existed(tmp_path):
    """AC15's own framing: a machine with no prior marker file at all."""
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    assert list(gitdir.iterdir()) == [
        e for e in gitdir.iterdir() if not e.name.startswith(marker.MARKER_PREFIX)
    ]
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert marker.clear_line(gitdir, _SESSION_ID) in text


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
