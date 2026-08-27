"""Tests for `coordinator_core.root_channel_reconcile` -- the reconciliation
read over the channels that each answer "where is root X?".

Both incidents the module was built from are pinned here as cases, not as
prose: the pointer-vs-registry split (a drive-lettered path in a pointer file
on a POSIX box) and the shape of a channel that names a path nothing on this
machine has.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import root_channel_reconcile as rcr


@pytest.fixture
def machine_local(tmp_path: Path) -> Path:
    d = tmp_path / "machine-local"
    d.mkdir()
    return d


def _stub_registry(monkeypatch: pytest.MonkeyPatch, values: dict) -> None:
    """Bind every registry channel read to `values`, so a test never depends
    on the real box's registry."""
    monkeypatch.setattr(rcr, "_read_registry", lambda key: (values.get(key), False))


def test_all_channels_agreeing_and_present_is_ok(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(
        monkeypatch,
        {
            "repos.claude_klabauter": str(real),
            "publish.mirrors.claude_klabauter.path": str(real),
        },
    )
    (machine_local / ".claude-klabauter-root").write_text(str(real), encoding="utf-8")

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert report.ok
    assert report.disagreeing == ()
    assert report.absent_targets == ()
    assert rcr.disagreement_message([report]) is None


def test_pointer_disagreeing_with_registry_is_named_with_both_paths(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INCIDENT 1 (2026-08-22 install dogfood): the pointer file carried a
    foreign-platform path for the published mirror while the registry key was
    correct all along. The two channels must not both be reported as fine,
    and the message must carry the pointer file's own path -- an operator who
    is told only "no engine build stamp" has nothing to edit."""
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real)})
    pointer = machine_local / ".claude-klabauter-root"
    # The incident's pointer held a foreign-platform path, but what this test needs
    # is only that the target NOT EXIST. Spelling that as a literal is the same bet
    # the production bug was: `X:/claude-klabauter` is a real checkout on the primary
    # box, so the literal resolved and `absent_targets` came back empty. Derive it
    # from tmp_path instead, which is absent by construction on every host.
    pointer.write_text(str(tmp_path / "no-such-klabauter"), encoding="utf-8")

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert not report.ok
    assert "pointer .claude-klabauter-root" in report.disagreeing
    assert "registry repos.claude_klabauter" in report.disagreeing
    assert report.absent_targets == ("pointer .claude-klabauter-root",)

    message = rcr.disagreement_message([report])
    assert message is not None
    assert str(pointer) in message, "the message must name the file to edit"
    assert "path does not exist" in message


def test_a_present_path_that_disagrees_is_still_a_finding(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disagreement and absence are independent axes. Two channels naming two
    directories that BOTH exist is the publish-lands-where-nothing-dispatches
    case, and existence must not launder it into a pass."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _stub_registry(
        monkeypatch,
        {
            "repos.claude_klabauter": str(a),
            "publish.mirrors.claude_klabauter.path": str(b),
        },
    )

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert not report.ok
    assert report.absent_targets == ()
    assert len(report.disagreeing) == 2


def test_silent_channels_never_count_as_disagreement(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box that writes the root down ONCE is correct, not split. Only
    channels that actually spoke are compared -- otherwise every single-source
    box would report a permanent false finding, which is the wallpaper that
    gets a check ignored."""
    real = tmp_path / "claude-klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real)})

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert report.ok
    assert [c.value for c in report.channels if c.value] == [str(real)]


def test_trailing_separator_and_dot_segments_are_not_a_disagreement(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compare-form only. A trailing slash or a `./` segment is spelling, not
    a split, and reporting it would train an operator to ignore the check."""
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real) + "/"})
    (machine_local / ".claude-klabauter-root").write_text(
        str(tmp_path) + "/./klabauter\n", encoding="utf-8"
    )

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert report.ok


def test_unreadable_channel_is_reported_and_never_raises(
    machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel that cannot be read is a distinct operator problem from a
    channel that said nothing, and neither may propagate an exception into a
    caller that is already failing."""
    monkeypatch.setattr(rcr, "_read_registry", lambda key: (None, True))
    pointer = machine_local / ".claude-klabauter-root"
    pointer.mkdir()  # a directory where a file is expected -> unreadable

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert all(c.unreadable for c in report.channels)
    assert report.ok, "nothing spoke, so there is nothing to reconcile"


def test_unknown_root_name_raises_keyerror(machine_local: Path) -> None:
    """A name not in `_ROOTS` is a caller bug, not a box condition."""
    with pytest.raises(KeyError):
        rcr.reconcile("not_a_root", machine_local)


def test_cli_exits_nonzero_on_a_split_and_zero_when_reconciled(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real)})
    pointer = machine_local / ".claude-klabauter-root"
    pointer.write_text(str(tmp_path / "gone"), encoding="utf-8")

    assert rcr.main(["--machine-local", str(machine_local)]) == 1
    assert str(pointer) in capsys.readouterr().out

    pointer.write_text(str(real), encoding="utf-8")
    assert rcr.main(["--machine-local", str(machine_local)]) == 0


def test_message_cites_no_decision_record(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anti-pattern this whole slice exists to remove: a message citing a
    ruling for a condition the ruling does not cover. Channels disagreeing is
    a data condition -- a DR reference here would send the reader to a
    decision record instead of to their pointer file."""
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(tmp_path)})
    (machine_local / ".claude-klabauter-root").write_text(
        str(tmp_path / "elsewhere"), encoding="utf-8"
    )

    message = rcr.disagreement_message([rcr.reconcile("claude_klabauter", machine_local)])

    assert message is not None
    assert "DR-" not in message
