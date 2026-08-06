"""Tests for coordinator_core.hooks.em_report_altitude.

Covers the two detectors (D1 word-budget, D2 citation-density), their
composition, the suppression/fail-open surfaces, and the per-session
bark-once sentinel (fires at most once per session, global across both
detectors) — including three real-corpus-shaped fixtures per the pinned
dispatch brief.
"""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.hooks import em_report_altitude as m


def _payload(cwd, **over):
    base = {
        "session_id": "sess-test",
        "cwd": str(cwd),
        "stop_hook_active": False,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_env_hatch(monkeypatch):
    monkeypatch.delenv("COORDINATOR_EM_REPORT_ALTITUDE_OFF", raising=False)


@pytest.fixture(autouse=True)
def _tally_isolation(monkeypatch, tmp_path):
    """Redirect the has-fired sentinel into a per-test tmp dir via the env override.

    Without this, sentinel-writing tests would walk up from `cwd` looking for
    a real `.git` and could land fired-state in whatever repo happens to be
    the test-runner's cwd — exactly what
    `COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR` exists to prevent. Individual
    tests that specifically exercise the git-common-dir FALLBACK path
    override this by deleting the env var again.
    """
    tally_dir = tmp_path / ".tally-isolation"
    monkeypatch.setenv("COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR", str(tally_dir))
    return tally_dir


@pytest.fixture
def repo(tmp_path):
    """A tmp dir that looks like a git repo, so the sentinel has somewhere to live."""
    os.makedirs(tmp_path / ".git")
    return tmp_path


# ---------------------------------------------------------------------------
# D1 — word budget
# ---------------------------------------------------------------------------


def test_d1_fires_above_200_words(tmp_path):
    text = " ".join(["word"] * 201)
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is not None
    assert "201 words" in result["message"]


def test_d1_does_not_fire_at_200_words(tmp_path):
    text = " ".join(["word"] * 200)
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is None


def test_d1_does_not_fire_below_200_words(tmp_path):
    text = " ".join(["word"] * 50)
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is None


def test_fenced_code_exclusion_prevents_false_fire(tmp_path):
    """A 400-word message that is mostly a code fence must NOT fire D1."""
    code_block = "```\n" + "\n".join(["x = 1"] * 380) + "\n```"
    prose = " ".join(["done"] * 15)
    text = f"{prose}\n\n{code_block}"
    assert m._word_count(text) <= 200
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is None


def test_table_row_exclusion_lowers_count(tmp_path):
    table = "\n".join(
        ["| a | b |", "|---|---|"] + [f"| item-{i} | value-{i} |" for i in range(60)]
    )
    prose = " ".join(["done"] * 15)
    text = f"{prose}\n\n{table}"
    # Every table row would otherwise contribute several words each.
    assert m._word_count(text) <= 200
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is None


# ---------------------------------------------------------------------------
# D1 exclusion gaps — deliberate, over-counting-biased, pinned per
# _strip_excluded's docstring so a future "why did this fire" investigation
# doesn't have to re-derive the behavior.
# ---------------------------------------------------------------------------


def test_unclosed_fence_counts_as_prose(tmp_path):
    """An unclosed ``` fence has no matching close, so _FENCED_CODE_RE (which
    requires a closing ```) never matches it — its content is counted as
    ordinary prose. This is the safe-direction gap: it can only cause an
    over-count (a false D1 fire), never a missed one."""
    code_like = "\n".join([f"line_{i} = {i}" for i in range(210)])
    text = "```\n" + code_like  # deliberately never closed
    word_count = m._word_count(text)
    assert word_count > 200  # the unclosed fence's content was NOT stripped
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is not None


def test_pipeless_table_data_rows_count_but_separator_excluded(tmp_path):
    """A GFM table without outer pipes (`a | b` rather than `| a | b |`) is
    not recognized as table rows by _TABLE_ROW_RE (it requires leading and
    trailing pipes) — so its data rows count as prose. Only the separator
    row (`---|---`, all dashes/pipes/colons/space) still matches
    _TABLE_SEP_RE and gets excluded either way."""
    header = "a | b"
    sep = "---|---"
    rows = [f"item-{i} | value-{i}" for i in range(60)]
    prose = " ".join(["done"] * 15)
    text = "\n".join([prose, header, sep] + rows)

    stripped = m._strip_excluded(text)
    assert header in stripped
    assert all(row in stripped for row in rows)  # data rows NOT excluded
    assert sep not in stripped  # separator row still excluded


def test_indented_code_block_counts_as_prose(tmp_path):
    """A 4-space-indented code block (the other markdown code-block form,
    alongside fenced ```) is not recognized by _FENCED_CODE_RE (fence-only)
    or _is_table_row — so it counts as prose."""
    indented = "\n".join([f"    line_{i} = {i}" for i in range(210)])
    word_count = m._word_count(indented)
    assert word_count > 200  # not stripped as code


# ---------------------------------------------------------------------------
# D2 — citation density
# ---------------------------------------------------------------------------


def test_d2_fires_on_two_citations(tmp_path):
    text = "See coordinator_core/hooks/foo.py:42 and also /Users/example-operator/X/bar.py for context."
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is not None
    assert "citations" in result["message"]


def test_d2_does_not_fire_on_one_citation(tmp_path):
    text = "See coordinator_core/hooks/foo.py:42 for details."
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is None


def test_d2_file_line_regex_ignores_host_port(tmp_path):
    """example.com:8080 has no path separator and no recognized source-file
    extension — it must NOT count as a file:line citation (F1)."""
    text = "The service listens on example.com:8080 and also 127.0.0.1:5432 for the DB."
    file_line, abs_path = m._citation_count(text)
    assert file_line == 0
    assert abs_path == 0
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is None


def test_d2_file_line_regex_matches_bare_and_pathed_source_files(tmp_path):
    """foo.py:42 (extension on the explicit list, no separator needed) and
    path/to/foo.py:42 / coordinator_core/hooks/em_report_altitude.py:118
    (separator present) all count (F1)."""
    for token in (
        "foo.py:42",
        "path/to/foo.py:42",
        "coordinator_core/hooks/em_report_altitude.py:118",
    ):
        file_line, _ = m._citation_count(f"See {token} for details.")
        assert file_line == 1, token


def test_d2_recognizes_linux_home_and_opt_paths(tmp_path):
    """/home/... and /opt/... must count as absolute-path citations, not
    just /Users/... (F2 — multi-OS support is P0)."""
    text = "See /home/example-operator/repo/foo.py and /opt/tooling/bar.sh for context and one more note."
    file_line, abs_path = m._citation_count(text)
    assert abs_path == 2


def test_d2_windows_drive_letter_does_not_match_url_scheme(tmp_path):
    """The Windows drive-letter pattern requires a literal backslash after
    the drive letter, so a "scheme://" URL (forward slashes) never matches
    it — confirms the existing guard, per F2."""
    text = "See https://example.com/docs for the reference doc, thanks."
    _, abs_path = m._citation_count(text)
    assert abs_path == 0


def test_d2_fires_independent_of_length_short_message(tmp_path):
    text = "Relayed to /Users/example-operator/X/claude-klabauter/state/a.md and /Users/example-operator/X/example-doctrine-repo/cross-repo/b.md."
    assert m._word_count(text) < 20
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is not None
    assert "citations" in result["message"]


# ---------------------------------------------------------------------------
# Both-trip composition
# ---------------------------------------------------------------------------


def test_both_detectors_trip_together(tmp_path):
    long_prose = " ".join(["word"] * 210)
    text = (
        f"{long_prose} See coordinator_core/hooks/foo.py:42 and "
        "/Users/example-operator/X/bar.py:100 for more."
    )
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is not None
    assert "words" in result["message"]
    assert "citations" in result["message"]


def test_both_detectors_trip_on_the_one_fire_carries_both_measurements(repo):
    """The session's single firing must carry BOTH measurements when the
    triggering reply trips both detectors — neither gets dropped in favor
    of the other."""
    long_prose = " ".join(["word"] * 210)
    text = (
        f"{long_prose} See coordinator_core/hooks/foo.py:42 and "
        "/Users/example-operator/X/bar.py:100 for more."
    )
    result = m.op(_payload(repo, session_id="both-fire", last_assistant_message=text))
    assert result is not None
    assert "words" in result["message"]
    assert "citations" in result["message"]
    assert len(result["message"].splitlines()) == 2

    # And the session is now spent — a second qualifying reply gets nothing.
    again = m.op(_payload(repo, session_id="both-fire", last_assistant_message=text))
    assert again is None


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def test_stop_hook_active_suppresses(tmp_path):
    text = " ".join(["word"] * 300)
    result = m.op(_payload(tmp_path, stop_hook_active=True, last_assistant_message=text))
    assert result is None


def test_env_off_switch_suppresses(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_EM_REPORT_ALTITUDE_OFF", "1")
    text = " ".join(["word"] * 300)
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is None


def test_subagent_stop_suppresses(tmp_path):
    text = " ".join(["word"] * 300)
    result = m.op(_payload(tmp_path, agent_id="agent-1", last_assistant_message=text))
    assert result is None


def test_meta_discussion_suppressed(tmp_path):
    text = (
        "hooks.em_report_altitude fired because the message exceeded 200 words "
        + " ".join(["word"] * 210)
    )
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is None


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


def test_malformed_payload_returns_none():
    assert m.op(["not", "a", "dict"]) is None
    assert m.op(None) is None
    assert m.op("also not a dict") is None


def test_unreadable_transcript_path_returns_none(tmp_path):
    # transcript_path points at a directory, not a file — OSError inside
    # last_assistant_text, caught, falls through to "".
    result = m.op(_payload(tmp_path, transcript_path=str(tmp_path)))
    assert result is None


def test_no_message_and_no_transcript_returns_none(tmp_path):
    result = m.op(_payload(tmp_path))
    assert result is None


# ---------------------------------------------------------------------------
# Per-session bark-once sentinel
# ---------------------------------------------------------------------------


def test_sentinel_dir_override_used_when_set(repo, tmp_path):
    """COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR (set by the autouse isolation
    fixture) redirects the sentinel away from the repo's own .git entirely —
    the write must land under the override dir, not under repo/.git."""
    text = " ".join(["word"] * 300)
    session_id = "override-sess"
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=text))
    assert result is not None

    override_dir = tmp_path / ".tally-isolation"
    written = override_dir / session_id / "em-report-altitude-tally.json"
    assert written.is_file()
    assert not (repo / ".git" / "coordinator-sessions").exists()


def test_sentinel_falls_back_to_git_common_dir_when_env_unset(repo, monkeypatch):
    """With the override unset, the sentinel falls back to the git-common-dir
    location the sibling fire-once sentinel also uses."""
    monkeypatch.delenv("COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR", raising=False)
    text = " ".join(["word"] * 300)
    session_id = "fallback-sess"
    result = m.op(_payload(repo, session_id=session_id, last_assistant_message=text))
    assert result is not None

    fallback_path = (
        repo / ".git" / "coordinator-sessions" / session_id / "em-report-altitude-tally.json"
    )
    assert fallback_path.is_file()


def test_first_firing_emits(repo):
    text = " ".join(["word"] * 300)
    result = m.op(_payload(repo, session_id="bark-once", last_assistant_message=text))
    assert result is not None
    assert "let it stand" in result["message"]


def test_second_and_third_firing_emit_nothing(repo):
    text = " ".join(["word"] * 300)
    first = m.op(_payload(repo, session_id="bark-once-2", last_assistant_message=text))
    assert first is not None
    second = m.op(_payload(repo, session_id="bark-once-2", last_assistant_message=text))
    third = m.op(_payload(repo, session_id="bark-once-2", last_assistant_message=text))
    assert second is None
    assert third is None


def test_fresh_session_fires_again(repo):
    """A different session_id gets its own sentinel — bark-once is scoped
    per session, not process-global."""
    text = " ".join(["word"] * 300)
    a = m.op(_payload(repo, session_id="sess-a", last_assistant_message=text))
    b = m.op(_payload(repo, session_id="sess-b", last_assistant_message=text))
    assert a is not None
    assert b is not None
    assert "let it stand" in a["message"]
    assert "let it stand" in b["message"]


def test_sentinel_write_failure_does_not_change_return_value(repo, monkeypatch):
    """A sentinel write failure must never suppress or crash the advisory —
    the call that fired still returns its message; the honest consequence
    (documented in the module docstring) is that a later call in the same
    session may fire again, which is the safe direction for a backstop."""
    path = m._tally_path(_payload(repo, session_id="broken-sentinel"))
    os.makedirs(os.path.dirname(path))
    os.chmod(os.path.dirname(path), 0o500)  # write-blocked directory
    try:
        text = " ".join(["word"] * 300)
        result = m.op(_payload(repo, session_id="broken-sentinel", last_assistant_message=text))
        assert result is not None
        assert "300 words" in result["message"]
    finally:
        os.chmod(os.path.dirname(path), 0o700)


def test_stale_sentinel_content_does_not_matter(repo):
    """The sentinel is a plain has-fired flag now, not a parsed counter — any
    existing file at the path (however written) suppresses further firing,
    with no JSON parsing involved."""
    path = m._tally_path(_payload(repo, session_id="stale-sentinel"))
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not valid json at all")
    text = " ".join(["word"] * 300)
    result = m.op(_payload(repo, session_id="stale-sentinel", last_assistant_message=text))
    assert result is None


# ---------------------------------------------------------------------------
# Register — [comms] prefix, no per-detector taper (bark-once means every
# firing is the session's only firing, so every firing gets the full form).
# ---------------------------------------------------------------------------


def test_comms_prefix_not_altitude(tmp_path):
    text = " ".join(["word"] * 300)
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert "[comms]" in result["message"]
    assert "[altitude]" not in result["message"]


def test_d1_firing_full_form(tmp_path):
    text = " ".join(["word"] * 300)
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert "let it stand" in result["message"]


def test_d2_firing_full_form(tmp_path):
    text = "See coordinator_core/hooks/foo.py:42 and also /Users/example-operator/X/bar.py for context."
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert "Ignore this if the citation was the point" in result["message"]


def test_full_form_contains_release_clause(tmp_path):
    text = " ".join(["word"] * 300)
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert "if this one earned the length, let it stand" in result["message"].lower()


# ---------------------------------------------------------------------------
# Real-corpus-shaped fixtures (representative, not the literal originals) —
# a 353-word status report (D1), a 10-word reply carrying two absolute relay
# paths (D2 only), and a 29-word reply carrying one file.py:NNNN citation
# plus one more citation (D2).
# ---------------------------------------------------------------------------


def test_corpus_shape_353_word_status_report_fires_d1(tmp_path):
    sentence = (
        "Verified the fix lands cleanly and the regression suite stays green "
        "across every touched module in this pass today "
    )
    text = (sentence * 17).strip()  # ~ 17 * ~19 words ~= 353 words, no citations
    word_count = m._word_count(text)
    assert word_count > 200
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is not None
    assert "words" in result["message"]
    assert "citations" not in result["message"]


def test_corpus_shape_10_word_reply_two_absolute_paths_fires_d2_only(tmp_path):
    text = "Relayed the memo to /Users/example-operator/X/claude-klabauter/state/a.md and /Users/example-operator/X/example-doctrine-repo/b.md"
    word_count = m._word_count(text)
    assert word_count <= 20
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is not None
    assert "words (budget" not in result["message"]
    assert "citations" in result["message"]


def test_corpus_shape_29_word_reply_file_line_plus_citation_fires_d2(tmp_path):
    text = (
        "Fixed the precedence bug after tracing it through "
        "coordinator_core/hooks/foo.py:142 and confirmed the same regression "
        "touched bar/baz/qux.py:87 before landing the corrected patch cleanly"
    )
    word_count = m._word_count(text)
    assert word_count < 40
    result = m.op(_payload(tmp_path, last_assistant_message=text))
    assert result is not None
    assert "citations" in result["message"]


# ---------------------------------------------------------------------------
# Reuse of the sibling's transcript machinery — proves the import, not a copy.
# ---------------------------------------------------------------------------


def test_falls_back_to_transcript_when_field_absent(tmp_path):
    path = tmp_path / "transcript.jsonl"
    long_text = " ".join(["word"] * 250)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}}
            )
            + "\n"
        )
    result = m.op(_payload(tmp_path, transcript_path=str(path)))
    assert result is not None
    assert "250 words" in result["message"]
