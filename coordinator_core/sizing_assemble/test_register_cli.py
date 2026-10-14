"""
coordinator_core.sizing_assemble.test_register_cli — `sizing-assemble --register`.

Run (from repo root):
    python3 -m pytest coordinator_core/sizing_assemble/test_register_cli.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa

_REL = "state/sizings/20260101-a.yaml"
_SIZING = (
    "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: XL\n"
    "  provisional: true\nroute: pm-decision\ndetents: []\nfork: null\nxl_exit: null\n"
    "status: routed\npremise:\n  provenance: read\n  evidence: fixture\n"
)
_REGISTER = (
    "sources:\n  - doc: docs/x.md\n    ref: abc\nrows:\n"
    "  - id: a\n    source_text: text a\n    surface: cli\n    status: open\n"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _REL).write_text(_SIZING, encoding="utf-8")
    (tmp_path / "reg.yaml").write_text(_REGISTER, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_register_with_write_exits_zero_and_carries_register(repo):
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0
    doc = yaml.safe_load((repo / _REL).read_text(encoding="utf-8"))
    assert doc["requirement_register"]["rows"][0]["id"] == "a"
    assert doc["requirement_register"]["rollup"]["open"] == 1


def test_register_without_write_is_usage(repo):
    assert sa.main(["--register", "reg.yaml"]) == sa.EXIT_USAGE


def test_register_with_pm_resolution_is_usage(repo):
    argv = ["--register", "reg.yaml", "--pm-resolution", "k", "--pm-quote", "q", "--write", _REL]
    assert sa.main(argv) == sa.EXIT_USAGE


def test_register_with_xl_exit_is_usage(repo):
    argv = ["--register", "reg.yaml", "--xl-exit", "shape", "--pm-quote", "q", "--write", _REL]
    assert sa.main(argv) == sa.EXIT_USAGE


def test_missing_yaml_is_usage(repo):
    assert sa.main(["--register", "nope.yaml", "--write", _REL]) == sa.EXIT_USAGE


def test_invalid_register_exits_one(repo):
    (repo / "reg.yaml").write_text(_REGISTER.replace("cli", "bogus"), encoding="utf-8")
    before = (repo / _REL).read_bytes()
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == sa.EXIT_BUSINESS_FAIL
    assert (repo / _REL).read_bytes() == before


def test_register_twice_is_idempotent_and_supersede_is_accepted(repo):
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0
    assert sa.main(["--register", "reg.yaml", "--supersede", "--write", _REL]) == 0


_TURN9 = (
    "I'd like the shape that comes back from the hairless build to be used for the scalp, "
    "not the old mesh; and keep the   groom\nparameters exposed."
)
_PHRASE = "use the _shape_ that comes back from the hairless build"
_TURN9_TEXT = f"first, {_PHRASE} for the scalp.\nthen keep the groom   parameters exposed."
_WITH_TURNS = _SIZING + (
    "pm_verbatims:\n"
    "  - turn: 2\n    text: \"I need to take a nap, back soon\"\n"
    f"  - turn: 9\n    text: {yaml.safe_dump(_TURN9_TEXT).strip()}\n"
)


def _reg(rows_text: str, extra: str = "") -> str:
    return f"sources:\n  - doc: docs/x.md\n    ref: abc\nrows:\n{rows_text}{extra}"


_PARAPHRASE = (
    "  - id: a\n    source_text: shape from the hairless build feeds the scalp\n"
    "    surface: cli\n    status: open\n"
)


@pytest.fixture
def turns_repo(repo):
    (repo / _REL).write_text(_WITH_TURNS, encoding="utf-8")
    return repo


@pytest.mark.parametrize("extra", [[], ["--supersede"]])
def test_paraphrased_register_over_uncovered_turns_is_refused(turns_repo, capsys, extra):
    (turns_repo / "reg.yaml").write_text(_reg(_PARAPHRASE), encoding="utf-8")
    before = (turns_repo / _REL).read_bytes()
    code = sa.main(["--register", "reg.yaml", *extra, "--write", _REL])
    assert code == sa.EXIT_BUSINESS_FAIL
    out = capsys.readouterr().out
    assert "turn 9" in out and "turn 2" in out
    assert (turns_repo / _REL).read_bytes() == before


def test_verbatim_row_plus_unrelated_turn_records(turns_repo):
    rows = (
        f"  - id: a\n    source_text: \"{_PHRASE}\"\n    surface: cli\n    status: open\n"
    )
    extra = "unrelated_turns:\n  - turn: 2\n    reason: conversational, no instruction\n"
    (turns_repo / "reg.yaml").write_text(_reg(rows, extra), encoding="utf-8")
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0


def test_unrelated_turn_not_embedded_is_refused(turns_repo, capsys):
    rows = f"  - id: a\n    source_text: \"{_PHRASE}\"\n    surface: cli\n    status: open\n"
    extra = (
        "unrelated_turns:\n  - turn: 2\n    reason: chat\n  - turn: 5\n    reason: chat\n"
    )
    (turns_repo / "reg.yaml").write_text(_reg(rows, extra), encoding="utf-8")
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == sa.EXIT_BUSINESS_FAIL
    assert "turn 5" in capsys.readouterr().out


def _register_of(repo):
    return yaml.safe_load((repo / _REL).read_text(encoding="utf-8"))["requirement_register"]


def test_register_overlay_from_stdin_drops_waives_and_adds_with_defaults(turns_repo, monkeypatch):
    import io

    full = f"  - id: a\n    source_text: \"{_PHRASE}\"\n    surface: cli\n    status: open\n"
    full += "  - id: nap\n    source_text: take a nap\n    surface: internal\n    status: open\n"
    extra = "unrelated_turns:\n  - turn: 2\n    reason: chat\n"
    (turns_repo / "reg.yaml").write_text(_reg(full, extra), encoding="utf-8")
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0

    overlay = (
        "drop: [nap]\nunrelated_turns:\n  - turn: 2\n    reason: conversational\n"
        "rows:\n  - id: b\n    source_text: keep the groom parameters exposed\n"
        "  - id: a\n    surface: ui\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(overlay))
    assert sa.main(["--register", "-", "--write", _REL]) == 0
    reg = _register_of(turns_repo)
    assert [(r["id"], r["surface"], r["status"]) for r in reg["rows"]] == [
        ("a", "ui", "open"), ("b", "internal", "open"),
    ]
    assert reg["rows"][1]["claimed_by"] == []
    assert reg["unrelated_turns"] == [{"turn": 2, "reason": "conversational"}]


def test_overlay_dropping_an_unknown_row_is_refused(repo, capsys):
    assert sa.main(["--register", "reg.yaml", "--write", _REL]) == 0
    (repo / "over.yaml").write_text("drop: [zz]\n", encoding="utf-8")
    before = (repo / _REL).read_bytes()
    assert sa.main(["--register", "over.yaml", "--write", _REL]) == sa.EXIT_BUSINESS_FAIL
    assert "zz" in capsys.readouterr().out
    assert (repo / _REL).read_bytes() == before


def test_derive_register_backfills_a_sizing_that_embeds_turns(turns_repo, capsys):
    assert sa.main(["--derive-register", "--write", _REL]) == 0
    reg = _register_of(turns_repo)
    assert [r["id"] for r in reg["rows"]] == ["T2", "T9"]
    assert all(r["status"] == "open" and r["surface"] == "internal" for r in reg["rows"])
    assert sa.main(["--derive-register", "--write", _REL]) == 0  # identical: idempotent


_RELAYED = (
    _SIZING.replace(
        "intent: Test intent, verbatim.",
        "intent_source: em-elaborated\n"
        "intent: the tag-cut op refuses a version with no CHANGELOG section",
    )
    + "pm_verbatims:\n"
    "  - turn: 11\n    text: \"I don't want to leave things still open\"\n"
    "  - turn: 12\n    text: \"make the tag-cut refusal name the CHANGELOG file\"\n"
)


def test_derive_register_keeps_a_differently_worded_pm_turn_as_a_row_on_relayed_ask(repo):
    (repo / _REL).write_text(_RELAYED, encoding="utf-8")
    assert sa.main(["--derive-register", "--write", _REL]) == 0
    reg = _register_of(repo)
    assert [r["id"] for r in reg["rows"]] == ["T11", "T12"]
    assert "unrelated_turns" not in reg


def test_derive_register_keeps_every_turn_as_a_row_when_intent_is_pm_verbatim():
    doc = yaml.safe_load(_RELAYED)
    doc["intent_source"] = "pm-verbatim"
    rows, unrelated, _, _ = sa.derive_register(doc)
    assert [r["id"] for r in rows] == ["T11", "T12", "I"] and unrelated == []


def test_help_prints_multiline_usage_with_register_shape(repo, capsys):
    assert sa.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) > 1
    register_block = out[out.index("or: --register"):out.index("or: --derive-register")]
    assert "sources" in register_block and "{doc, ref}" in register_block


def test_derive_register_without_pm_words_is_refused(repo):
    assert sa.main(["--derive-register", "--write", _REL]) == sa.EXIT_BUSINESS_FAIL
